"""Bronze snapshots, immutable Silver partitions, and DuckDB run metadata."""

from __future__ import annotations

import hashlib
import json
import math
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

    def upsert_futures_long_short_heat(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Persist replayable daily futures heat outputs without a user-weighted total."""

        if not rows:
            return 0
        for row in rows:
            _validate_futures_long_short_heat_row(row)
        columns = _FUTURES_LONG_SHORT_HEAT_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"formula_version", "trade_date"}
        )
        values = [[row[column] for column in columns] for row in rows]
        self.connection.executemany(
            f"""INSERT INTO futures_long_short_heat_daily ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(formula_version, trade_date) DO UPDATE SET {updates}""",
            values,
        )
        self.connection.commit()
        return len(values)

    def list_futures_long_short_heat(
        self,
        *,
        start_day: str | None = None,
        end_day: str | None = None,
        formula_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read Gold heat rows in trading-day order; this never scans Silver."""

        clauses: list[str] = []
        parameters: list[str] = []
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        if formula_version is not None:
            clauses.append("formula_version = ?")
            parameters.append(formula_version)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self.connection.execute(
            f"SELECT {', '.join(_FUTURES_LONG_SHORT_HEAT_COLUMNS)} "
            f"FROM futures_long_short_heat_daily{where} ORDER BY trade_date, formula_version",
            parameters,
        )
        return [
            dict(zip(_FUTURES_LONG_SHORT_HEAT_COLUMNS, row, strict=True))
            for row in cursor.fetchall()
        ]

    def replace_futures_long_short_heat(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        formula_version: str,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> int:
        """Atomically replace one formula version (or a bounded date slice)."""

        if not formula_version:
            raise ValueError("formula_version is required")
        if start_day is not None and end_day is not None and start_day > end_day:
            raise ValueError("start_day must not be after end_day")
        if any(row.get("formula_version") != formula_version for row in rows):
            raise ValueError("all replacement rows must use formula_version")
        for row in rows:
            _validate_futures_long_short_heat_row(row)
        clauses = ["formula_version = ?"]
        parameters: list[Any] = [formula_version]
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        columns = _FUTURES_LONG_SHORT_HEAT_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        values = [[row[column] for column in columns] for row in rows]
        try:
            self.connection.execute("BEGIN TRANSACTION")
            self.connection.execute(
                f"DELETE FROM futures_long_short_heat_daily WHERE {' AND '.join(clauses)}",
                parameters,
            )
            if values:
                self.connection.executemany(
                    f"INSERT INTO futures_long_short_heat_daily ({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return len(values)

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
        self._ensure_futures_long_short_heat_schema()

    def _ensure_futures_long_short_heat_schema(self) -> None:
        table_name = "futures_long_short_heat_daily"
        exists = self.connection.execute(
            """SELECT count(*) FROM information_schema.tables
            WHERE table_schema=current_schema() AND table_name=?""",
            [table_name],
        ).fetchone()[0]
        if not exists:
            self.connection.execute(_futures_heat_table_ddl(table_name))
            return
        primary = self.connection.execute(
            """SELECT constraint_column_names FROM duckdb_constraints()
            WHERE table_name=? AND constraint_type='PRIMARY KEY'""",
            [table_name],
        ).fetchone()
        primary_columns = tuple(primary[0]) if primary else ()
        if primary_columns == ("formula_version", "trade_date"):
            return
        if primary_columns != ("trade_date",):
            raise RuntimeError(
                "unsupported futures_long_short_heat_daily primary key; expected legacy trade_date"
            )
        migration_table = f"_futures_heat_migration_{uuid4().hex}"
        self.connection.execute("BEGIN TRANSACTION")
        try:
            self.connection.execute(_futures_heat_table_ddl(migration_table))
            self.connection.execute(
                f"INSERT INTO {migration_table} ({', '.join(_FUTURES_LONG_SHORT_HEAT_COLUMNS)}) "
                f"SELECT {', '.join(_FUTURES_LONG_SHORT_HEAT_COLUMNS)} FROM {table_name}"
            )
            self.connection.execute(f"DROP TABLE {table_name}")
            self.connection.execute(f"ALTER TABLE {migration_table} RENAME TO {table_name}")
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise


_FUTURES_LONG_SHORT_HEAT_COLUMNS = (
    "trade_date",
    "total_variety_count",
    "valid_variety_count",
    "missing_variety_count",
    "up_variety_count",
    "down_variety_count",
    "flat_variety_count",
    "fund_valid_variety_count",
    "fund_missing_variety_count",
    "up_fund",
    "down_fund",
    "flat_fund",
    "return_coverage",
    "fund_coverage",
    "breadth_score_daily",
    "fund_score_daily",
    "breadth_score_10d",
    "fund_score_10d",
    "divergence",
    "is_warmup",
    "data_quality_status",
    "formula_version",
    "source_cutoff",
    "calculation_method",
    "calculated_at",
)


def _validate_futures_long_short_heat_row(row: Mapping[str, Any]) -> None:
    missing = [column for column in _FUTURES_LONG_SHORT_HEAT_COLUMNS if column not in row]
    if missing:
        raise ValueError(f"futures heat row missing columns: {missing}")
    count_fields = (
        "total_variety_count",
        "valid_variety_count",
        "missing_variety_count",
        "up_variety_count",
        "down_variety_count",
        "flat_variety_count",
        "fund_valid_variety_count",
        "fund_missing_variety_count",
    )
    for field in count_fields:
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if row["total_variety_count"] != row["valid_variety_count"] + row["missing_variety_count"]:
        raise ValueError("total variety count must equal valid plus missing")
    if row["valid_variety_count"] != sum(
        row[field] for field in ("up_variety_count", "down_variety_count", "flat_variety_count")
    ):
        raise ValueError("valid variety count must equal up plus down plus flat")
    if row["valid_variety_count"] != row["fund_valid_variety_count"] + row["fund_missing_variety_count"]:
        raise ValueError("fund valid plus missing must equal valid variety count")
    for field in ("up_fund", "down_fund", "flat_fund"):
        value = _finite_number(row[field], field)
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
    for field in ("return_coverage", "fund_coverage"):
        value = _finite_number(row[field], field)
        if not 0 <= value <= 1:
            raise ValueError(f"{field} must be in [0, 1]")
    for field in (
        "breadth_score_daily",
        "fund_score_daily",
        "breadth_score_10d",
        "fund_score_10d",
    ):
        if row[field] is not None and not -100 <= _finite_number(row[field], field) <= 100:
            raise ValueError(f"{field} must be in [-100, 100]")
    if row["divergence"] is not None and not -200 <= _finite_number(row["divergence"], "divergence") <= 200:
        raise ValueError("divergence must be in [-200, 200]")
    if not isinstance(row["is_warmup"], bool):
        raise ValueError("is_warmup must be boolean")
    if row["data_quality_status"] not in {"PASS", "PARTIAL", "UNAVAILABLE"}:
        raise ValueError("unsupported futures heat data_quality_status")
    for field in ("trade_date", "formula_version", "source_cutoff", "calculation_method", "calculated_at"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _futures_heat_table_ddl(table_name: str) -> str:
    return f"""CREATE TABLE {table_name} (
        trade_date VARCHAR NOT NULL,
        total_variety_count INTEGER NOT NULL,
        valid_variety_count INTEGER NOT NULL,
        missing_variety_count INTEGER NOT NULL,
        up_variety_count INTEGER NOT NULL,
        down_variety_count INTEGER NOT NULL,
        flat_variety_count INTEGER NOT NULL,
        fund_valid_variety_count INTEGER NOT NULL,
        fund_missing_variety_count INTEGER NOT NULL,
        up_fund DOUBLE NOT NULL,
        down_fund DOUBLE NOT NULL,
        flat_fund DOUBLE NOT NULL,
        return_coverage DOUBLE NOT NULL,
        fund_coverage DOUBLE NOT NULL,
        breadth_score_daily DOUBLE,
        fund_score_daily DOUBLE,
        breadth_score_10d DOUBLE,
        fund_score_10d DOUBLE,
        divergence DOUBLE,
        is_warmup BOOLEAN NOT NULL,
        data_quality_status VARCHAR NOT NULL,
        formula_version VARCHAR NOT NULL,
        source_cutoff VARCHAR NOT NULL,
        calculation_method VARCHAR NOT NULL,
        calculated_at VARCHAR NOT NULL,
        PRIMARY KEY(formula_version, trade_date)
    )"""


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
