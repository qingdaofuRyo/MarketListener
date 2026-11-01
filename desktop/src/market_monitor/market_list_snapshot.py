"""Versioned market list pages and one-query daily tail projections."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable, Iterable
from uuid import uuid4
from weakref import WeakValueDictionary

import duckdb

from .market_query_cache import get_kline_query_store
from .market_query_cache import KLineCacheUnavailable
from .market_data_version import market_data_version

_LOCK = threading.RLock()
_SNAPSHOTS: OrderedDict[tuple[str, str, str], list[dict[str, Any]]] = OrderedDict()
_BUILD_LOCKS: WeakValueDictionary[tuple[str, str, str], threading.RLock] = WeakValueDictionary()


def list_snapshot(root: Path, version: str, identity: str,
                  builder: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Reuse exactly the same decorated rows across filters and pagination."""
    key = (str(root.resolve()), version, identity)
    digest = sha256(repr(key).encode()).hexdigest()
    path = root / "cache" / "market-list" / f"{digest}.json.gz"
    with _LOCK:
        cached = _SNAPSHOTS.get(key)
        if cached is not None:
            _SNAPSHOTS.move_to_end(key)
            return cached
        build_lock = _BUILD_LOCKS.setdefault(key, threading.RLock())
    # A slow HK/stock calculation must not lock already-ready futures lists.
    # The same category/version still has one builder and one atomic file.
    with build_lock:
        with _LOCK:
            cached = _SNAPSHOTS.get(key)
            if cached is not None:
                _SNAPSHOTS.move_to_end(key)
                return cached
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
            cached = payload["items"] if payload["version"] == version else None
        except (OSError, ValueError, KeyError, TypeError):
            cached = None
        if cached is None:
            cached = builder()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{uuid4().hex}.tmp")
            try:
                with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=1) as stream:
                    json.dump({"version": version, "items": cached}, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            for stale in sorted(path.parent.glob("*.json.gz"), key=lambda value: value.stat().st_mtime_ns, reverse=True)[128:]:
                stale.unlink(missing_ok=True)
        with _LOCK:
            _SNAPSHOTS[key] = cached
            while len(_SNAPSHOTS) > 32:
                _SNAPSHOTS.popitem(last=False)
        return cached


def batch_daily_tails(root: Path, identifiers: Iterable[str], limit: int = 45) -> dict[str, list[dict[str, Any]]]:
    """Read daily windows in one Parquet query, not one connection per symbol."""
    requested = tuple(dict.fromkeys(value for value in identifiers if value))
    if not requested:
        return {}
    store = get_kline_query_store(root)
    actual_revision = store.ensure_ready()
    if actual_revision != market_data_version(root):
        # A stale query index is useful for explicitly stale chart responses,
        # but cannot be labelled as the new version's financial publication.
        raise KLineCacheUnavailable("Daily projection index is not current")
    placeholders = ",".join("?" for _ in requested)
    with duckdb.connect(str(store.path), read_only=True) as connection:
        meta = connection.execute(
            f"SELECT instrument_id,max(latest_bar_at) FROM instrument_file WHERE period='1d' AND instrument_id IN ({placeholders}) GROUP BY instrument_id",
            list(requested),
        ).fetchall()
        bounds = {}
        for identifier, latest in meta:
            try:
                bounds[str(identifier)] = (datetime.fromisoformat(str(latest).replace("Z", "+00:00")) - timedelta(days=180)).isoformat()[:10]
            except ValueError:
                bounds[str(identifier)] = "0001-01-01"
        paths = connection.execute(
            f"SELECT instrument_id,file_path,latest_bar_at FROM instrument_file WHERE period='1d' AND instrument_id IN ({placeholders})",
            list(requested),
        ).fetchall()
    files = list(dict.fromkeys(str(path) for identifier, path, latest in paths if str(latest) >= bounds[str(identifier)]))
    if not files:
        return {}
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with duckdb.connect(":memory:") as connection:
        columns = {str(row[0]) for row in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?,union_by_name=true,hive_partitioning=false)",
            [files],
        ).fetchall()}
        if "bar_period" in columns and "period" in columns:
            period_column = "coalesce(p.bar_period,p.period)"
        elif "bar_period" in columns:
            period_column = "p.bar_period"
        elif "period" in columns:
            period_column = "p.period"
        else:
            raise KLineCacheUnavailable("Daily partitions lack a period column")
        connection.execute("CREATE TEMP TABLE _daily_requested(instrument_id VARCHAR,lower_bound VARCHAR)")
        connection.executemany("INSERT INTO _daily_requested VALUES (?,?)", list(bounds.items()))
        rows = connection.execute(
            f"""WITH daily AS (
                 SELECT p.instrument_id,p.bar_open_time,arg_max(p.bar_json,
                   coalesce(json_extract_string(p.bar_json,'$.fetched_at'),'')) payload
                 FROM read_parquet(?,union_by_name=true,hive_partitioning=false) p
                 JOIN _daily_requested r USING(instrument_id)
                 WHERE {period_column}='1d' AND p.bar_open_time>=r.lower_bound
                 GROUP BY p.instrument_id,p.bar_open_time)
               SELECT instrument_id,payload FROM daily
               QUALIFY row_number() OVER(PARTITION BY instrument_id ORDER BY bar_open_time DESC)<=?
               ORDER BY instrument_id,bar_open_time""", [files, limit],
        ).fetchall()
    for identifier, payload in rows:
        try:
            bar = json.loads(str(payload))
        except ValueError:
            continue
        if isinstance(bar, dict):
            result[str(identifier)].append(bar)
    return dict(result)
