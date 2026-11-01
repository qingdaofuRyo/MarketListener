"""Run registered native-backed collection tasks without a catalog writer.

The worker accepts only reviewed dataset identifiers. It returns a versioned
JSON persistence plan; the service remains the sole writer of that plan. Each
task gets a fresh process so a V8 fatal exit cannot terminate the web service
or poison another task's native runtime.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import CollectionTask, CollectionTaskResult


ISOLATED_DATASETS = frozenset({
    "HK_STOCK_BAR", "HK_INDEX_BAR", "GLOBAL_INDEX_BAR", "FUTURE_MAIN_BAR",
    "FUTURE_INDEX_BAR", "CN_MARGIN", "MACRO_SERIES", "A_SHARE_BREADTH",
    "HSGT_FLOW", "CN_ZT_POOL", "CN_ZT_OPEN_RETURN", "FUTURE_GLOBAL_BAR",
})
WIRE_VERSION = 1
MAX_RESULT_BYTES = 128 * 1024 * 1024
_PERSIST_FIELDS = {
    "bar_writes", "gold_metrics", "member_position_ranks",
    "member_position_coverage", "member_trade_ranks", "member_position_raw_artifacts",
}
_METADATA_FIELDS = {
    "dataset_id", "dataset_name", "source", "status", "rows", "detail",
    "error", "started_at", "completed_at", "persist",
}


class IsolatedCollectionError(RuntimeError):
    """A failed child has produced no persistence plan."""


def _json_scalar(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    # Pandas frames can contain NumPy scalars. Preserve their numeric type,
    # rather than silently stringifying quantities and prices.
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return value.item()
    raise TypeError(f"unsupported collection value: {type(value).__name__}")


def _record_list(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise IsolatedCollectionError(f"invalid isolated {name}")
    return value


def _reject_nonfinite(_value: str) -> Any:
    raise ValueError("nonfinite JSON value")


def _decode_results(payload: Any, dataset_id: str) -> list[CollectionTaskResult]:
    from .collector import BarWrite, CollectionTaskResult, PersistPlan
    from .storage import PartitionKey

    if not isinstance(payload, dict) or set(payload) != {"version", "dataset_id", "results"}:
        raise IsolatedCollectionError("invalid isolated result envelope")
    if type(payload["version"]) is not int or payload["version"] != WIRE_VERSION or payload["dataset_id"] != dataset_id:
        raise IsolatedCollectionError("isolated result identity/version mismatch")
    allowed = {dataset_id}
    if dataset_id == "FUTURE_MAIN_BAR":
        allowed.add("FUTURES_BREADTH")
    items = _record_list(payload["results"], "results")
    if not items or len(items) > len(allowed):
        raise IsolatedCollectionError("invalid isolated result count")
    results: list[CollectionTaskResult] = []
    seen: set[str] = set()
    for item in items:
        if set(item) != _METADATA_FIELDS:
            raise IsolatedCollectionError("invalid isolated result fields")
        identity = item["dataset_id"]
        if not isinstance(identity, str) or identity not in allowed or identity in seen:
            raise IsolatedCollectionError("isolated dataset identity mismatch")
        seen.add(identity)
        expected_source = "local-computed" if identity == "FUTURES_BREADTH" else "akshare"
        if item["source"] != expected_source or item["status"] not in {"PASS", "PARTIAL_FAILURE", "FAILED", "BLOCKED", "SKIPPED"}:
            raise IsolatedCollectionError("invalid isolated source/status")
        if type(item["rows"]) is not int or item["rows"] < 0:
            raise IsolatedCollectionError("invalid isolated row count")
        if any(not isinstance(item[field], str) for field in ("dataset_name", "detail", "started_at", "completed_at")):
            raise IsolatedCollectionError("invalid isolated metadata")
        if item["error"] is not None and not isinstance(item["error"], str):
            raise IsolatedCollectionError("invalid isolated error")
        plan = item["persist"]
        if not isinstance(plan, dict) or set(plan) != _PERSIST_FIELDS:
            raise IsolatedCollectionError("invalid isolated persistence fields")
        for field in _PERSIST_FIELDS:
            _record_list(plan[field], field)
        # The registered AKShare tasks produce bars and Gold. Official member
        # facts retain their separate authorized collection and import path.
        if any(plan[field] for field in _PERSIST_FIELDS - {"bar_writes", "gold_metrics"}):
            raise IsolatedCollectionError("unexpected member facts in isolated result")
        writes: list[BarWrite] = []
        for write in plan["bar_writes"]:
            if set(write) != {"key", "bars", "data_cutoff"} or not isinstance(write["data_cutoff"], str):
                raise IsolatedCollectionError("invalid isolated bar write")
            key = write["key"]
            if not isinstance(key, dict) or set(key) != {"market", "asset_type", "period", "year", "partition_id"}:
                raise IsolatedCollectionError("invalid isolated partition key")
            if type(key["year"]) is not int or not 1900 <= key["year"] <= 2200:
                raise IsolatedCollectionError("invalid isolated partition year")
            if any(not isinstance(key[field], str) or not key[field]
                   for field in ("market", "asset_type", "period", "partition_id")):
                raise IsolatedCollectionError("invalid isolated partition identity")
            if any("/" in key[field] or "\\" in key[field] or key[field] in {".", ".."}
                   for field in ("market", "asset_type", "period", "partition_id")):
                raise IsolatedCollectionError("invalid isolated partition path")
            writes.append(BarWrite(PartitionKey(**key), _record_list(write["bars"], "bars"), write["data_cutoff"]))
        metadata = {field: item[field] for field in _METADATA_FIELDS - {"persist"}}
        results.append(CollectionTaskResult(**metadata, persist=PersistPlan(
            bar_writes=writes, gold_metrics=plan["gold_metrics"],
        )))
    if dataset_id not in seen:
        raise IsolatedCollectionError("isolated primary result is missing")
    return results


def execute_isolated_task(
    task: CollectionTask, *, timeout_seconds: float, limit_futures: int, limit_cn_stocks: int,
) -> list[CollectionTaskResult]:
    """Fetch in a bounded child; never open a database in this function."""
    if task.dataset_id not in ISOLATED_DATASETS or task.source != "akshare":
        raise IsolatedCollectionError("dataset is not registered for isolation")
    if timeout_seconds <= 0 or limit_futures <= 0 or limit_cn_stocks <= 0:
        raise IsolatedCollectionError("invalid isolated collection limits")
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, environment.get("PYTHONPATH"))))
    environment["PYTHONUTF8"] = "1"
    with TemporaryDirectory(prefix="marketlistener-collector-", ignore_cleanup_errors=True) as directory:
        output = Path(directory) / "result.json"
        command = [
            sys.executable, "-m", "market_monitor.collector_isolation",
            "--dataset", task.dataset_id, "--output", str(output),
            "--limit-futures", str(limit_futures), "--limit-cn-stocks", str(limit_cn_stocks),
        ]
        try:
            result = subprocess.run(
                command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=timeout_seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as error:
            raise IsolatedCollectionError(f"isolated collection timeout after {timeout_seconds:g}s") from error
        if result.returncode != 0:
            raise IsolatedCollectionError(f"isolated collection exited with code {result.returncode}")
        if not output.is_file() or output.stat().st_size > MAX_RESULT_BYTES:
            raise IsolatedCollectionError("isolated collection result missing or oversized")
        try:
            payload = json.loads(output.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)
            return _decode_results(payload, task.dataset_id)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise IsolatedCollectionError("invalid isolated collection result") from error


def _deny_catalog(*_args: Any, **_kwargs: Any) -> Any:
    raise IsolatedCollectionError("database access is forbidden in collection workers")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-database collection worker")
    parser.add_argument("--dataset", choices=sorted(ISOLATED_DATASETS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit-futures", type=int, required=True)
    parser.add_argument("--limit-cn-stocks", type=int, required=True)
    arguments = parser.parse_args(argv)
    if arguments.limit_futures <= 0 or arguments.limit_cn_stocks <= 0:
        parser.error("collection limits must be positive")
    from . import collector, storage

    # Guard the formal storage API as well as its native connection entry point.
    # This applies only in this dedicated child, never in the service process.
    collector.MarketStore = _deny_catalog  # type: ignore[assignment]
    storage.MarketStore = _deny_catalog  # type: ignore[assignment]
    storage.duckdb.connect = _deny_catalog
    task = next(item for item in collector.build_collection_tasks(
        limit_futures=arguments.limit_futures, limit_cn_stocks=arguments.limit_cn_stocks,
    ) if item.dataset_id == arguments.dataset)
    outcome = collector._execute_task(task)
    results = outcome if isinstance(outcome, list) else [outcome]
    payload = {"version": WIRE_VERSION, "dataset_id": arguments.dataset,
               "results": [asdict(item) for item in results]}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_json_scalar)
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise IsolatedCollectionError("isolated collection result oversized")
    temporary = arguments.output.with_suffix(".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
