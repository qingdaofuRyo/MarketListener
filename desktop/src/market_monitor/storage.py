"""Bronze snapshots, immutable Silver partitions, and DuckDB run metadata."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import duckdb

from .market_data_version import advance_market_data_version, market_data_version
from .market_query_cache import apply_kline_cache_update


@dataclass(frozen=True)
class PartitionKey:
    market: str
    asset_type: str
    period: str
    year: int
    partition_id: str

    def relative_path(self) -> Path:
        return Path(
            "silver",
            f"market={self.market}",
            f"asset_type={self.asset_type}",
            f"period={self.period}",
            f"year={self.year}",
            f"{self.partition_id}.parquet",
        )


class MarketStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(root / "catalog.duckdb"))
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def begin_run(self, provider: str) -> str:
        run_id = f"run-{uuid4().hex}"
        self.connection.execute(
            "INSERT INTO runs VALUES (?, ?, 'RUNNING', ?, NULL, NULL)",
            [run_id, provider, _now()],
        )
        return run_id

    def finish_run(self, run_id: str, status: str, detail: str | None = None) -> None:
        self.connection.execute(
            "UPDATE runs SET status=?, completed_at=?, detail=? WHERE run_id=?",
            [status, _now(), detail, run_id],
        )

    def write_bronze(self, run_id: str, provider: str, response: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> Path:
        directory = self.root / "bronze" / provider
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{run_id}.json"
        self._atomic_json(target, response)
        return target

    def write_silver_bars(
        self,
        key: PartitionKey,
        bars: Sequence[Mapping[str, Any]],
        data_cutoff: str,
        source_run_id: str,
        *,
        update_query_cache: bool = True,
    ) -> Path:
        if not bars:
            raise ValueError("A Silver partition requires at least one normalized bar")
        target = self.root / key.relative_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_parquet = target.with_suffix(f".{uuid4().hex}.parquet")
        try:
            escaped_parquet = str(staging_parquet).replace("'", "''")
            existing: list[dict[str, str]] = []
            if target.is_file():
                existing = [
                    {"bar_json": row[0], "instrument_id": row[1], "bar_period": row[2], "bar_open_time": row[3]}
                    for row in self.connection.execute(
                        f"SELECT bar_json, instrument_id, bar_period, bar_open_time FROM read_parquet('{str(target).replace(chr(39), chr(39) * 2)}')"
                    ).fetchall()
                ]
            rows: dict[tuple[str, str, str], dict[str, str]] = {
                (row["instrument_id"], row["bar_period"], row["bar_open_time"]): row for row in existing
            }
            for bar in bars:
                key_tuple = (_bar_instrument_id(bar), str(bar.get("period", "")), str(bar.get("bar_open_time", "")))
                rows[key_tuple] = {
                    "bar_json": json.dumps(bar, ensure_ascii=False),
                    "instrument_id": key_tuple[0],
                    "bar_period": key_tuple[1],
                    "bar_open_time": key_tuple[2],
                }
            self.connection.execute(
                "CREATE OR REPLACE TEMP TABLE _silver_stage (bar_json VARCHAR, instrument_id VARCHAR, bar_period VARCHAR, bar_open_time VARCHAR)"
            )
            stage_rows = list(rows.values())
            # Registering a small DataFrame lets DuckDB ingest a batch in C
            # instead of crossing the Python/SQL boundary once per K line.
            # It is especially important for offline TDX imports, which can
            # contain hundreds of millions of historical minute bars.
            try:
                import pandas as pd

                frame = pd.DataFrame.from_records(
                    stage_rows,
                    columns=["bar_json", "instrument_id", "bar_period", "bar_open_time"],
                )
                self.connection.register("_silver_stage_rows", frame)
                try:
                    self.connection.execute("INSERT INTO _silver_stage SELECT * FROM _silver_stage_rows")
                finally:
                    self.connection.unregister("_silver_stage_rows")
            except ImportError:  # pragma: no cover - pandas is supplied by AkShare in production
                self.connection.executemany(
                    "INSERT INTO _silver_stage VALUES (?, ?, ?, ?)",
                    [(row["bar_json"], row["instrument_id"], row["bar_period"], row["bar_open_time"]) for row in stage_rows],
                )
            self.connection.execute(
                f"COPY (SELECT bar_json, instrument_id, bar_period, bar_open_time FROM _silver_stage) TO '{escaped_parquet}' (FORMAT PARQUET)"
            )
            self.connection.execute("DROP TABLE _silver_stage")
            row_count = self.connection.execute(
                f"SELECT count(*) FROM read_parquet('{escaped_parquet}') WHERE bar_json IS NOT NULL"
            ).fetchone()[0]
            if row_count != len(rows):
                raise RuntimeError(f"Silver validation row mismatch: expected {len(rows)}, got {row_count}")
            checksum = _sha256(staging_parquet)
            # The immutable Parquet write is authoritative.  Advance the
            # revision only after it succeeds, then update the optional chart
            # query cache in-place.  If that cache update fails it is safely
            # rebuilt on the next chart read.
            previous_revision = market_data_version(self.root)
            os.replace(staging_parquet, target)
            self.connection.execute(
                """INSERT INTO partitions VALUES (?, ?, ?, ?, ?, ?, 'COMPLETE', ?)
                ON CONFLICT(partition_id) DO UPDATE SET file_path=excluded.file_path, row_count=excluded.row_count,
                data_cutoff=excluded.data_cutoff, sha256=excluded.sha256, source_run_id=excluded.source_run_id,
                status=excluded.status, updated_at=excluded.updated_at""",
                [key.partition_id, str(target.relative_to(self.root)), row_count, data_cutoff, checksum, source_run_id, _now()],
            )
            recorded_previous, current_revision = advance_market_data_version(self.root)
            if update_query_cache:
                apply_kline_cache_update(self.root, recorded_previous or previous_revision, current_revision, bars)
            return target
        finally:
            staging_parquet.unlink(missing_ok=True)

    def partition_metadata(self, partition_id: str) -> tuple[Any, ...] | None:
        return self.connection.execute(
            "SELECT file_path, row_count, data_cutoff, sha256, status FROM partitions WHERE partition_id=?",
            [partition_id],
        ).fetchone()

    def upsert_dataset(self, dataset_json: Mapping[str, Any]) -> None:
        """登记或更新一份 Data Catalog 数据集定义。"""

        dataset_id = str(dataset_json["dataset_id"])
        self.connection.execute(
            """INSERT INTO datasets (dataset_id, dataset_json, registered_at) VALUES (?, ?, ?)
            ON CONFLICT(dataset_id) DO UPDATE SET dataset_json=excluded.dataset_json, registered_at=excluded.registered_at""",
            [dataset_id, json.dumps(dataset_json, ensure_ascii=False, sort_keys=True), _now()],
        )
        self.connection.commit()

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT dataset_id, dataset_json, registered_at FROM datasets ORDER BY dataset_id"
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            document = json.loads(row[1])
            document["registered_at"] = row[2]
            output.append(document)
        return output

    def register_default_datasets(self) -> int:
        """写入 Data Catalog 内置数据集；返回本次新增数量。"""

        from market_monitor.dataset_catalog import DEFAULT_DATASETS, validate_dataset_definition

        registered = 0
        for definition in DEFAULT_DATASETS:
            validate_dataset_definition(definition)
            self.upsert_dataset(definition.to_dict())
            registered += 1
        return registered

    def upsert_gold_metrics(self, metrics: Sequence[Mapping[str, Any]]) -> int:
        """把 Gold 派生指标写入 gold_metrics 表；返回写入行数。"""

        for metric in metrics:
            self.connection.execute(
                """INSERT INTO gold_metrics (metric_id, instrument_id, trading_date, period, metric_name, value,
                definition, calculation_method, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(metric_id) DO UPDATE SET value=excluded.value, timestamp=excluded.timestamp""",
                [
                    metric["metric_id"], metric["instrument_id"], metric["trading_date"], metric["period"],
                    metric["metric_name"], metric["value"], metric["definition"], metric["calculation_method"],
                    metric["timestamp"],
                ],
            )
        self.connection.commit()
        return len(metrics)

    def _atomic_json(self, target: Path, data: Any) -> None:
        staging = target.with_suffix(f".{uuid4().hex}.json")
        try:
            staging.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)

    def _create_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR PRIMARY KEY, provider VARCHAR NOT NULL, status VARCHAR NOT NULL,
                started_at VARCHAR NOT NULL, completed_at VARCHAR, detail VARCHAR
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS partitions (
                partition_id VARCHAR PRIMARY KEY, file_path VARCHAR NOT NULL, row_count BIGINT NOT NULL,
                data_cutoff VARCHAR NOT NULL, sha256 VARCHAR NOT NULL, source_run_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL, updated_at VARCHAR NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS datasets (
                dataset_id VARCHAR PRIMARY KEY, dataset_json VARCHAR NOT NULL, registered_at VARCHAR NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS gold_metrics (
                metric_id VARCHAR PRIMARY KEY, instrument_id VARCHAR NOT NULL, trading_date VARCHAR NOT NULL,
                period VARCHAR NOT NULL, metric_name VARCHAR NOT NULL, value DOUBLE NOT NULL,
                definition VARCHAR NOT NULL, calculation_method VARCHAR NOT NULL, timestamp VARCHAR NOT NULL
            )"""
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bar_instrument_id(bar: Mapping[str, Any]) -> str:
    key = bar.get("instrument_key")
    if isinstance(key, Mapping):
        return ".".join(str(key.get(part, "")) for part in ("country_or_market", "exchange", "asset_type", "code"))
    return str(key if key is not None else bar.get("instrument_id", ""))
