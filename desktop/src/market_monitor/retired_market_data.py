"""Recoverably remove market series retired by an explicit R4 decision."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

import duckdb

from .market_data_version import advance_market_data_version


_CRYPTO_IDS = ("GLOBAL.CRYPTO.BTCUSDT", "GLOBAL.CRYPTO.ETHUSDT")
_CRYPTO_METRIC_IDS = ("BTC_USD", "ETH_USD")
_HZ_PATTERN = "HK.HKEX.INDEX.HZ%.TDX_LOCAL"
_HK_FUND_PATTERN = "HK.HKEX.FUND.%.TDX_LOCAL"
_UNOWNED_TDX_FUTURE_PATTERN = "CN.TDX.FUTURE.%.CONTRACT.TDX"


def prune_retired_market_data(data_root: Path) -> dict[str, Any]:
    """Remove Binance BTC/ETH, legacy ``27#HZ``, and ``49#`` rows from active storage.

    Original Parquet files are moved to an ignored, timestamped backup before
    filtered replacements are promoted.  TongdaXin installation files are
    never modified.
    """

    data_root = Path(data_root)
    catalog_path = data_root / "catalog.duckdb"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"未找到 MarketListener 数据目录: {catalog_path}")
    token = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{uuid4().hex[:8]}"
    work_root = data_root / "retired_market_data" / token
    staging_root = work_root / "staging"
    backup_root = work_root / "backup"
    predicate = (
        "instrument_id IN ('GLOBAL.CRYPTO.BTCUSDT', 'GLOBAL.CRYPTO.ETHUSDT') "
        f"OR instrument_id LIKE '{_HZ_PATTERN}' OR instrument_id LIKE '{_HK_FUND_PATTERN}' "
        f"OR instrument_id LIKE '{_UNOWNED_TDX_FUTURE_PATTERN}'"
    )
    plans: list[dict[str, Any]] = []
    with duckdb.connect(str(catalog_path)) as catalog:
        candidates = catalog.execute(
            "SELECT partition_id, file_path FROM partitions "
            "WHERE partition_id = 'GLOBAL-CRYPTO-1d-2026' "
            "OR (partition_id LIKE 'TDX-LOCAL-%' "
            "AND (replace(file_path, chr(92), chr(47)) LIKE '%market=HK/asset_type=INDEX/%' "
            "OR replace(file_path, chr(92), chr(47)) LIKE '%market=HK/asset_type=FUND/%'))"
        ).fetchall()
        indexed_candidates = _unowned_future_files(data_root)
        if indexed_candidates:
            known = {str(row[1]).replace("\\", "/").casefold() for row in candidates}
            for row in catalog.execute(
                "SELECT partition_id, file_path FROM partitions"
            ).fetchall():
                relative_key = str(row[1]).replace("\\", "/").casefold()
                absolute_key = str((data_root / str(row[1])).resolve()).replace("\\", "/").casefold()
                if (relative_key in indexed_candidates or absolute_key in indexed_candidates) and relative_key not in known:
                    candidates.append(row)
                    known.add(relative_key)
        for partition_id, relative_value in candidates:
            relative = Path(str(relative_value))
            source = data_root / relative
            if not source.is_file():
                continue
            with duckdb.connect(database=":memory:") as reader:
                retired, retained = reader.execute(
                    f"SELECT count(*) FILTER (WHERE {predicate}), count(*) FILTER (WHERE NOT ({predicate})) "
                    "FROM read_parquet(?)",
                    [str(source)],
                ).fetchone()
                if not retired:
                    continue
                staged = staging_root / relative
                cutoff = None
                digest = None
                if retained:
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged_sql = str(staged).replace("'", "''")
                    reader.execute(
                        f"COPY (SELECT * FROM read_parquet(?) WHERE NOT ({predicate})) "
                        f"TO '{staged_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                        [str(source)],
                    )
                    cutoff = reader.execute(
                        "SELECT max(bar_open_time) FROM read_parquet(?)", [str(staged)]
                    ).fetchone()[0]
                    digest = _sha256(staged)
            plans.append({
                "partition_id": str(partition_id),
                "relative": relative,
                "source": source,
                "staged": staged if retained else None,
                "retired": int(retired),
                "retained": int(retained),
                "cutoff": str(cutoff) if cutoff is not None else None,
                "sha256": digest,
            })

        promoted: list[dict[str, Any]] = []
        try:
            for plan in plans:
                backup = backup_root / plan["relative"]
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(plan["source"], backup)
                plan["backup"] = backup
                staged = plan["staged"]
                if staged is not None:
                    plan["source"].parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, plan["source"])
                promoted.append(plan)
        except Exception:
            for plan in reversed(promoted):
                plan["source"].unlink(missing_ok=True)
                os.replace(plan["backup"], plan["source"])
            raise

        try:
            catalog.execute("BEGIN TRANSACTION")
            for plan in plans:
                if plan["retained"]:
                    catalog.execute(
                        "UPDATE partitions SET row_count=?, data_cutoff=?, sha256=?, updated_at=? "
                        "WHERE partition_id=?",
                        [
                            plan["retained"], plan["cutoff"], plan["sha256"], _now(), plan["partition_id"],
                        ],
                    )
                else:
                    catalog.execute("DELETE FROM partitions WHERE partition_id=?", [plan["partition_id"]])
            catalog.execute(
                "DELETE FROM gold_metrics WHERE instrument_id IN (?, ?)", list(_CRYPTO_METRIC_IDS)
            )
            catalog.execute("DELETE FROM datasets WHERE dataset_id='CRYPTO_BAR'")
            catalog.execute("COMMIT")
        except Exception:
            catalog.execute("ROLLBACK")
            for plan in reversed(promoted):
                plan["source"].unlink(missing_ok=True)
                os.replace(plan["backup"], plan["source"])
            raise

    _previous_revision, revision = advance_market_data_version(data_root)
    _prune_query_cache(data_root, revision)
    report = {
        "status": "COMPLETE",
        "retiredRows": sum(plan["retired"] for plan in plans),
        "retainedRowsInTouchedPartitions": sum(plan["retained"] for plan in plans),
        "touchedPartitions": len(plans),
        "backupRoot": str(backup_root),
        "dataVersion": revision,
        "tongdaxinSourceFilesModified": False,
    }
    work_root.mkdir(parents=True, exist_ok=True)
    (work_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shutil.rmtree(staging_root, ignore_errors=True)
    return report


def _prune_query_cache(data_root: Path, revision: str) -> None:
    path = data_root / "state" / "kline_query.duckdb"
    if not path.is_file():
        return
    predicate = (
        "instrument_id IN ('GLOBAL.CRYPTO.BTCUSDT', 'GLOBAL.CRYPTO.ETHUSDT') "
        f"OR instrument_id LIKE '{_HZ_PATTERN}' OR instrument_id LIKE '{_HK_FUND_PATTERN}' "
        f"OR instrument_id LIKE '{_UNOWNED_TDX_FUTURE_PATTERN}'"
    )
    with duckdb.connect(str(path)) as cache:
        cache.execute("BEGIN TRANSACTION")
        try:
            for table in ("instrument_file", "instrument_period", "instrument_latest", "derived_bars"):
                cache.execute(f"DELETE FROM {table} WHERE {predicate}")
            rows = int(cache.execute("SELECT coalesce(sum(row_count), 0) FROM instrument_period").fetchone()[0])
            cache.execute("DELETE FROM cache_meta")
            cache.execute(
                "INSERT INTO cache_meta VALUES (?, ?, ?, ?, ?)",
                [2, revision, rows, _now(), 0.0],
            )
            cache.execute("COMMIT")
        except Exception:
            cache.execute("ROLLBACK")
            raise


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unowned_future_files(data_root: Path) -> set[str]:
    path = data_root / "state" / "kline_query.duckdb"
    rows: list[tuple[Any, ...]] = []
    if path.is_file():
        with duckdb.connect(str(path), read_only=True) as cache:
            rows = cache.execute(
                "SELECT DISTINCT file_path FROM instrument_file WHERE instrument_id LIKE ?",
                [_UNOWNED_TDX_FUTURE_PATTERN],
            ).fetchall()
    if not rows:
        future_root = data_root / "silver" / "market=CN" / "asset_type=FUTURE"
        if future_root.is_dir():
            glob = (future_root / "**" / "*.parquet").as_posix()
            with duckdb.connect(database=":memory:") as reader:
                rows = reader.execute(
                    "SELECT DISTINCT filename FROM read_parquet(?, filename=true, union_by_name=true, "
                    "hive_partitioning=true) WHERE instrument_id LIKE ?",
                    [glob, _UNOWNED_TDX_FUTURE_PATTERN],
                ).fetchall()
    output: set[str] = set()
    for row in rows:
        if not row or not row[0]:
            continue
        value = Path(str(row[0]))
        output.add(str(value).replace("\\", "/").casefold())
        output.add(str(value.resolve()).replace("\\", "/").casefold())
    return output


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
