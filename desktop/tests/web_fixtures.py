"""Shared helpers for the research-terminal FastAPI tests."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import duckdb

from market_monitor.market_data_version import advance_market_data_version


def write_silver(data_root: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    """Write a small Silver partition and matching synthetic Catalog evidence."""
    path = Path(data_root) / "silver" / "fixture.parquet"
    previous_version_file = Path(data_root) / "state" / "market_data_version.json"
    had_version = previous_version_file.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE bars ("
            "instrument_id VARCHAR, market VARCHAR, asset_type VARCHAR, "
            "period VARCHAR, bar_open_time VARCHAR, bar_json VARCHAR)"
        )
        for row in rows:
            payload = dict(row)
            connection.execute(
                "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?)",
                [
                    str(payload["instrument_id"]),
                    str(payload["market"] or ""),
                    str(payload["asset_type"] or ""),
                    str(payload["period"] or "1d"),
                    str(payload["bar_open_time"]),
                    json.dumps(payload, ensure_ascii=False),
                ],
            )
        quoted = str(path).replace("'", "''")
        connection.execute(f"COPY bars TO '{quoted}' (FORMAT PARQUET)")
        row_count, data_cutoff = connection.execute(
            "SELECT count(*), max(bar_open_time) FROM bars"
        ).fetchone()
    finally:
        connection.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog = Path(data_root) / "catalog.duckdb"
    with duckdb.connect(str(catalog)) as metadata:
        metadata.execute(
            "CREATE TABLE IF NOT EXISTS partitions ("
            "partition_id VARCHAR PRIMARY KEY, file_path VARCHAR NOT NULL, row_count BIGINT NOT NULL, "
            "data_cutoff VARCHAR NOT NULL, sha256 VARCHAR NOT NULL, source_run_id VARCHAR NOT NULL, "
            "status VARCHAR NOT NULL, updated_at VARCHAR NOT NULL)"
        )
        metadata.execute(
            "INSERT INTO partitions VALUES (?, ?, ?, ?, ?, ?, 'COMPLETE', ?) "
            "ON CONFLICT(partition_id) DO UPDATE SET row_count=excluded.row_count, "
            "data_cutoff=excluded.data_cutoff, sha256=excluded.sha256, updated_at=excluded.updated_at",
            ["synthetic-fixture", "silver/fixture.parquet", int(row_count),
             str(data_cutoff or ""), digest, "synthetic-fixture",
             datetime.now(timezone.utc).isoformat()],
        )
    if had_version:
        advance_market_data_version(data_root, changed_file=path, sha256=digest)
    return path


def write_gold_metrics(data_root: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    """Write a local catalog.duckdb with a minimal gold_metrics table."""
    catalog = Path(data_root) / "catalog.duckdb"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(catalog))
    try:
        connection.execute(
            "CREATE TABLE gold_metrics ("
            "instrument_id VARCHAR, trading_date VARCHAR, metric_name VARCHAR, "
            "value DOUBLE, metric_id VARCHAR)"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO gold_metrics VALUES (?, ?, ?, ?, ?)",
                [
                    str(row["instrument_id"]),
                    str(row["trading_date"]),
                    str(row["metric_name"]),
                    float(row["value"]),
                    str(row["metric_id"]),
                ],
            )
    finally:
        connection.close()
    return catalog


def make_bar(
    instrument_id: str,
    day: str,
    *,
    market: str = "CN",
    asset_type: str = "STOCK",
    period: str = "1d",
    close: float = 10.0,
    open_: float = 10.0,
    high: float = 10.2,
    low: float = 9.8,
    volume: float = 1000.0,
    amount: float = 10000.0,
) -> dict[str, Any]:
    """Build one bar_json payload matching the local silver schema."""
    return {
        "instrument_id": instrument_id,
        "symbol": instrument_id.split(".")[-1],
        "name": f"fixture-{instrument_id}",
        "market": market,
        "asset_type": asset_type,
        "period": period,
        "trading_date": day,
        "bar_open_time": f"{day}T09:30:00",
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
        "source": "fixture",
        "quality_status": "OK",
        "fetched_at": f"{day}T15:00:00+08:00",
    }


def silver_row(instrument_id: str, day: str, **kwargs: Any) -> dict[str, Any]:
    """Build one row for ``write_silver`` with required top-level columns."""
    payload = make_bar(instrument_id, day, **kwargs)
    return {
        "instrument_id": instrument_id,
        "market": payload["market"],
        "asset_type": payload["asset_type"],
        "period": payload["period"],
        "bar_open_time": payload["bar_open_time"],
        **payload,
    }

