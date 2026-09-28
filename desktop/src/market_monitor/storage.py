"""Bronze snapshots, immutable Silver partitions, and DuckDB run metadata."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import duckdb

from .market_data_version import advance_market_data_version, market_data_version
from .market_query_cache import apply_kline_cache_update


_SCHEMA_INIT_LOCK = threading.RLock()


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
        with _SCHEMA_INIT_LOCK:
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

    def record_futures_official_daily(
        self, rows: Sequence[Mapping[str, Any]], coverage: Mapping[str, Any]
    ) -> int:
        """Keep one exchange/day's normalized daily facts and source result isolated."""

        day = str(coverage["trade_date"])
        exchange = str(coverage["exchange"])
        adapter = str(coverage["adapter"])
        if coverage["status"] not in {"PASS", "FAILED", "UNSUPPORTED", "NO_PUBLICATION", "DEFERRED"}:
            raise ValueError("invalid official futures daily coverage status")
        if coverage["status"] == "PASS" and not rows:
            raise ValueError("PASS coverage requires normalized daily rows")
        if any(
            row["trade_date"] != day or row["exchange"] != exchange or row["adapter"] != adapter
            for row in rows
        ):
            raise ValueError("official futures daily rows must belong to one source day")
        if coverage["status"] != "PASS" and rows:
            raise ValueError("failed official source cannot publish normalized rows")
        columns = _OFFICIAL_FUTURES_DAILY_COLUMNS
        coverage_columns = _OFFICIAL_FUTURES_DAILY_COVERAGE_COLUMNS
        prepared_rows = [
            {
                **row,
                "volume_sidedness": row.get("volume_sidedness"),
                "open_interest_sidedness": row.get("open_interest_sidedness"),
                "turnover_quality_status": row.get("turnover_quality_status") or (
                    "PASS" if row.get("turnover_cny") is not None else "UNVERIFIED"
                ),
                "parser_version": row.get("parser_version") or str(row["adapter"]),
            }
            for row in rows
        ]
        try:
            self.connection.execute("BEGIN TRANSACTION")
            if coverage["status"] == "PASS":
                self.connection.execute(
                    "DELETE FROM futures_contract_daily_official WHERE trade_date=? AND exchange=? AND adapter=?",
                    [day, exchange, adapter],
                )
                self.connection.executemany(
                    "INSERT INTO futures_contract_daily_official (" + ", ".join(columns) + ") VALUES ("
                    + ", ".join("?" for _ in columns) + ")",
                    [[row[column] for column in columns] for row in prepared_rows],
                )
            self.connection.execute(
                "INSERT INTO futures_contract_daily_official_coverage ("
                + ", ".join(coverage_columns) + ") VALUES ("
                + ", ".join("?" for _ in coverage_columns) + ") "
                "ON CONFLICT(trade_date, exchange, adapter) DO UPDATE SET "
                + ", ".join(
                    f"{column}=excluded.{column}" for column in coverage_columns
                    if column not in {"trade_date", "exchange", "adapter"}
                ),
                [coverage[column] for column in coverage_columns],
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return len(rows)

    def list_futures_official_daily(
        self, *, start_day: str | None = None, end_day: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        columns = _OFFICIAL_FUTURES_DAILY_COLUMNS
        rows = self.connection.execute(
            "SELECT " + ", ".join(columns) + " FROM futures_contract_daily_official"
            + where + " ORDER BY trade_date, exchange, contract_code",
            parameters,
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def list_futures_official_daily_coverage(
        self, *, start_day: str | None = None, end_day: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        columns = _OFFICIAL_FUTURES_DAILY_COVERAGE_COLUMNS
        rows = self.connection.execute(
            "SELECT " + ", ".join(columns) + " FROM futures_contract_daily_official_coverage"
            + where + " ORDER BY trade_date, exchange",
            parameters,
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def replace_futures_contract_turnover_heat(
        self, rows: Sequence[Mapping[str, Any]], *, formula_version: str
    ) -> int:
        if not formula_version or any(row["formula_version"] != formula_version for row in rows):
            raise ValueError("all contract turnover heat rows must use one formula_version")
        columns = _FUTURES_CONTRACT_TURNOVER_HEAT_COLUMNS
        try:
            self.connection.execute("BEGIN TRANSACTION")
            self.connection.execute(
                "DELETE FROM futures_contract_turnover_heat_daily WHERE formula_version=?", [formula_version]
            )
            if rows:
                self.connection.executemany(
                    "INSERT INTO futures_contract_turnover_heat_daily (" + ", ".join(columns) + ") VALUES ("
                    + ", ".join("?" for _ in columns) + ")",
                    [[row[column] for column in columns] for row in rows],
                )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return len(rows)

    def list_futures_contract_turnover_heat(
        self, *, formula_version: str, start_day: str | None = None, end_day: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["formula_version = ?"]
        parameters = [formula_version]
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        columns = _FUTURES_CONTRACT_TURNOVER_HEAT_COLUMNS
        rows = self.connection.execute(
            "SELECT " + ", ".join(columns) + " FROM futures_contract_turnover_heat_daily WHERE "
            + " AND ".join(clauses) + " ORDER BY trade_date",
            parameters,
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def replace_futures_structure_daily(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        chart_id: str,
        direction: str,
        formula_version: str,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> int:
        """Atomically replace one precomputed futures-structure date slice."""

        if not chart_id or not direction or not formula_version:
            raise ValueError("chart_id, direction and formula_version are required")
        if start_day is not None and end_day is not None and start_day > end_day:
            raise ValueError("start_day must not be after end_day")
        for row in rows:
            _validate_futures_structure_row(row)
            if (
                row["chart_id"] != chart_id
                or row["direction"] != direction
                or row["formula_version"] != formula_version
            ):
                raise ValueError("structure replacement rows must share chart, direction and formula version")
        clauses = ["chart_id = ?", "direction = ?", "formula_version = ?"]
        parameters: list[Any] = [chart_id, direction, formula_version]
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        values = [[row[column] for column in _FUTURES_STRUCTURE_DAILY_COLUMNS] for row in rows]
        placeholders = ", ".join("?" for _ in _FUTURES_STRUCTURE_DAILY_COLUMNS)
        try:
            self.connection.execute("BEGIN TRANSACTION")
            self.connection.execute(
                f"DELETE FROM futures_structure_daily WHERE {' AND '.join(clauses)}", parameters
            )
            if values:
                self.connection.executemany(
                    f"INSERT INTO futures_structure_daily ({', '.join(_FUTURES_STRUCTURE_DAILY_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return len(values)

    def upsert_futures_structure_baseline(self, row: Mapping[str, Any]) -> None:
        """Persist immutable stack ordering metadata for one chart direction."""

        _validate_futures_structure_baseline(row)
        values = [
            row["chart_id"], row["direction"], row["baseline_version"], row["baseline_day"],
            row["threshold"], json.dumps(row["stack_order"], ensure_ascii=False),
            json.dumps(row["primary_members"], ensure_ascii=False),
            json.dumps(row["other_members"], ensure_ascii=False), row["formula_version"],
            row["price_basis"], row["source"], row["created_at"],
        ]
        self.connection.execute(
            """INSERT INTO futures_structure_baseline
            (chart_id, direction, baseline_version, baseline_day, threshold, stack_order_json,
             primary_members_json, other_members_json, formula_version, price_basis, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chart_id, direction, formula_version) DO NOTHING""",
            values,
        )
        self.connection.commit()

    def get_futures_structure_baseline(
        self, *, chart_id: str, direction: str, formula_version: str
    ) -> dict[str, Any] | None:
        """Read fixed baseline metadata for an exact formula version."""

        row = self.connection.execute(
            """SELECT chart_id, direction, baseline_version, baseline_day, threshold, stack_order_json,
            primary_members_json, other_members_json, formula_version, price_basis, source, created_at
            FROM futures_structure_baseline
            WHERE chart_id=? AND direction=? AND formula_version=?""",
            [chart_id, direction, formula_version],
        ).fetchone()
        return _futures_structure_baseline_dict(row) if row else None

    def delete_futures_structure_baseline(
        self, *, chart_id: str, direction: str, formula_version: str
    ) -> None:
        """Remove one baseline only for an explicitly requested rebuild."""

        self.connection.execute(
            "DELETE FROM futures_structure_baseline WHERE chart_id=? AND direction=? AND formula_version=?",
            [chart_id, direction, formula_version],
        )
        self.connection.commit()

    def list_futures_structure_daily(
        self,
        *,
        chart_id: str,
        direction: str,
        formula_version: str,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read stored structure rows; this method never scans Silver."""

        clauses = ["chart_id = ?", "direction = ?", "formula_version = ?"]
        parameters: list[Any] = [chart_id, direction, formula_version]
        if start_day is not None:
            clauses.append("trade_date >= ?")
            parameters.append(start_day)
        if end_day is not None:
            clauses.append("trade_date <= ?")
            parameters.append(end_day)
        cursor = self.connection.execute(
            f"SELECT {', '.join(_FUTURES_STRUCTURE_DAILY_COLUMNS)} FROM futures_structure_daily "
            f"WHERE {' AND '.join(clauses)} ORDER BY trade_date, member_key",
            parameters,
        )
        return [
            dict(zip(_FUTURES_STRUCTURE_DAILY_COLUMNS, row, strict=True))
            for row in cursor.fetchall()
        ]

    def replace_futures_structure_input_coverage(
        self, rows: Sequence[Mapping[str, Any]], *, chart_id: str, formula_version: str,
    ) -> int:
        columns = _FUTURES_STRUCTURE_INPUT_COVERAGE_COLUMNS
        prepared = [
            {
                **row,
                "missing_volume_count": int(row.get("missing_volume_count") or 0),
                "missing_exchange_count": int(row.get("missing_exchange_count") or 0),
                "missing_exchanges_json": row.get("missing_exchanges_json") or "[]",
                "unverified_volume_count": int(row.get("unverified_volume_count") or 0),
                "unverified_open_interest_count": int(row.get("unverified_open_interest_count") or 0),
                "quality_status": row.get("quality_status") or "PARTIAL",
            }
            for row in rows
        ]
        try:
            self.connection.execute("BEGIN TRANSACTION")
            self.connection.execute(
                "DELETE FROM futures_structure_input_coverage WHERE chart_id=? AND formula_version=?",
                [chart_id, formula_version],
            )
            if prepared:
                self.connection.executemany(
                    "INSERT INTO futures_structure_input_coverage (" + ", ".join(columns) + ") VALUES ("
                    + ", ".join("?" for _ in columns) + ")",
                    [[row[column] for column in columns] for row in prepared],
                )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return len(rows)

    def list_futures_structure_input_coverage(
        self, *, chart_id: str, formula_version: str,
    ) -> list[dict[str, Any]]:
        columns = _FUTURES_STRUCTURE_INPUT_COVERAGE_COLUMNS
        rows = self.connection.execute(
            "SELECT " + ", ".join(columns) + " FROM futures_structure_input_coverage "
            "WHERE chart_id=? AND formula_version=? ORDER BY trade_date",
            [chart_id, formula_version],
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def upsert_futures_member_position_ranks(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Persist exchange-published rank rows without inventing absent directions."""

        if not rows:
            return 0
        prepared = [_member_rank_with_provenance(row) for row in rows]
        for row in prepared:
            _validate_futures_member_position_rank(row)
        columns = _FUTURES_MEMBER_POSITION_RANK_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"trading_day", "exchange", "contract_code", "side", "rank", "source"}
        )
        self.connection.executemany(
            f"""INSERT INTO futures_member_position_ranks ({', '.join(columns)}) VALUES ({placeholders})
            ON CONFLICT(trading_day, exchange, contract_code, side, rank, source) DO UPDATE SET {updates}""",
            [[row[column] for column in columns] for row in prepared],
        )
        self.connection.commit()
        return len(rows)

    def latest_futures_member_position_day(self, *, commodity_only: bool = True) -> str | None:
        """Return the latest persisted ranking date, not a calendar approximation."""

        clause = " WHERE exchange <> 'CFFEX'" if commodity_only else ""
        row = self.connection.execute(
            f"SELECT max(trading_day) FROM futures_member_position_ranks{clause}"
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def latest_futures_member_position_observation_day(self, *, commodity_only: bool = True) -> str | None:
        """Return the newest rank *or coverage* observation day.

        A fully failed exchange collection intentionally has no rank rows.  It
        must still be visible to the read API as a real, dated source failure
        rather than being hidden behind the previous successful rank day.
        """

        clause = " WHERE exchange <> 'CFFEX'" if commodity_only else ""
        row = self.connection.execute(
            "SELECT max(trading_day) FROM ("
            f"SELECT trading_day FROM futures_member_position_ranks{clause} "
            "UNION ALL "
            f"SELECT trading_day FROM futures_member_position_coverage{clause}"
            ")"
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def list_futures_member_position_ranks(
        self,
        *,
        trading_day: str | None = None,
        exchange: str | None = None,
        contract_code: str | None = None,
        product_code: str | None = None,
        member_key: str | None = None,
        commodity_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Read raw published rank rows; this never derives missing rankings as zero."""

        clauses: list[str] = []
        parameters: list[str] = []
        if trading_day is not None:
            clauses.append("trading_day = ?")
            parameters.append(trading_day)
        if exchange is not None:
            clauses.append("exchange = ?")
            parameters.append(exchange)
        if contract_code is not None:
            clauses.append("contract_code = ?")
            parameters.append(contract_code)
        if product_code is not None:
            clauses.append("product_code = ?")
            parameters.append(product_code)
        if member_key is not None:
            clauses.append("member_key = ?")
            parameters.append(member_key)
        if commodity_only:
            clauses.append("exchange <> 'CFFEX'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self.connection.execute(
            f"SELECT {', '.join(_FUTURES_MEMBER_POSITION_RANK_COLUMNS)} "
            f"FROM futures_member_position_ranks{where} "
            "ORDER BY trading_day, exchange, contract_code, side, rank, member_key",
            parameters,
        )
        return [
            dict(zip(_FUTURES_MEMBER_POSITION_RANK_COLUMNS, row, strict=True))
            for row in cursor.fetchall()
        ]

    def upsert_futures_member_position_coverage(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Persist the source result for every exchange, including failures.

        Rank rows alone cannot prove that a source attempted all requested
        exchanges.  This table is therefore deliberately separate from the
        published ranking facts and never fabricates a zero-position row.
        """

        if not rows:
            return 0
        prepared = [_member_coverage_with_provenance(row) for row in rows]
        for row in prepared:
            _validate_futures_member_position_coverage(row)
        columns = _FUTURES_MEMBER_POSITION_COVERAGE_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"trading_day", "exchange", "source"}
        )
        self.connection.executemany(
            f"""INSERT INTO futures_member_position_coverage ({', '.join(columns)}) VALUES ({placeholders})
            ON CONFLICT(trading_day, exchange, source) DO UPDATE SET {updates}""",
            [[row[column] for column in columns] for row in prepared],
        )
        self.connection.commit()
        return len(rows)

    def list_futures_member_position_coverage(
        self,
        *,
        trading_day: str | None = None,
        exchange: str | None = None,
        commodity_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Read source-level rank coverage without treating failures as absent."""

        clauses: list[str] = []
        parameters: list[str] = []
        if trading_day is not None:
            clauses.append("trading_day = ?")
            parameters.append(trading_day)
        if exchange is not None:
            clauses.append("exchange = ?")
            parameters.append(exchange)
        if commodity_only:
            clauses.append("exchange <> 'CFFEX'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self.connection.execute(
            f"SELECT {', '.join(_FUTURES_MEMBER_POSITION_COVERAGE_COLUMNS)} "
            f"FROM futures_member_position_coverage{where} "
            "ORDER BY trading_day, exchange, source",
            parameters,
        )
        return [
            dict(zip(_FUTURES_MEMBER_POSITION_COVERAGE_COLUMNS, row, strict=True))
            for row in cursor.fetchall()
        ]

    def upsert_futures_member_trade_ranks(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Persist the exchange's member volume leaderboard separately from positions."""

        if not rows:
            return 0
        prepared = [_member_trade_rank_with_provenance(row) for row in rows]
        for row in prepared:
            _validate_futures_member_trade_rank(row)
        columns = _FUTURES_MEMBER_TRADE_RANK_COLUMNS
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in columns
            if column not in {"trading_day", "exchange", "contract_code", "rank", "member_key", "source"}
        )
        self.connection.executemany(
            f"INSERT INTO futures_member_trade_ranks ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(trading_day, exchange, contract_code, rank, member_key, source) DO UPDATE SET " + updates,
            [[row[column] for column in columns] for row in prepared],
        )
        self.connection.commit()
        return len(rows)

    def list_futures_member_trade_ranks(
        self, *, trading_day: str | None = None, exchange: str | None = None,
        product_code: str | None = None, contract_code: str | None = None,
        member_key: str | None = None, commodity_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (
            ("trading_day", trading_day), ("exchange", exchange),
            ("product_code", product_code), ("contract_code", contract_code),
            ("member_key", member_key),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        if commodity_only:
            clauses.append("exchange <> 'CFFEX'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self.connection.execute(
            f"SELECT {', '.join(_FUTURES_MEMBER_TRADE_RANK_COLUMNS)} FROM futures_member_trade_ranks{where} "
            "ORDER BY trading_day, exchange, contract_code, rank, member_key",
            parameters,
        )
        return [dict(zip(_FUTURES_MEMBER_TRADE_RANK_COLUMNS, row, strict=True)) for row in cursor.fetchall()]

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
        self._ensure_futures_contract_turnover_schema()
        self._ensure_futures_structure_schema()
        self._ensure_futures_member_position_rank_schema()
        self._ensure_futures_member_position_coverage_schema()
        self._ensure_futures_member_trade_rank_schema()

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

    def _ensure_futures_contract_turnover_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_contract_daily_official (
                trade_date VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
                contract_code VARCHAR NOT NULL, product_code VARCHAR NOT NULL,
                close DOUBLE, settlement DOUBLE,
                volume DOUBLE, open_interest DOUBLE, turnover_raw DOUBLE,
                turnover_cny DOUBLE, turnover_unit VARCHAR,
                turnover_sidedness VARCHAR, source_url VARCHAR NOT NULL,
                adapter VARCHAR NOT NULL, payload_sha256 VARCHAR NOT NULL,
                retrieved_at VARCHAR NOT NULL, quality_status VARCHAR NOT NULL,
                volume_sidedness VARCHAR, open_interest_sidedness VARCHAR,
                turnover_quality_status VARCHAR,
                parser_version VARCHAR,
                PRIMARY KEY(trade_date, exchange, contract_code, adapter)
            )"""
        )
        column_info = self.connection.execute(
            "PRAGMA table_info('futures_contract_daily_official')"
        ).fetchall()
        existing_columns = {str(row[1]) for row in column_info}
        not_null_columns = {str(row[1]) for row in column_info if bool(row[3])}
        if "open_interest" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE futures_contract_daily_official ADD COLUMN open_interest DOUBLE"
            )
        for column in ("close", "turnover_raw", "turnover_cny", "turnover_unit", "turnover_sidedness"):
            if column in not_null_columns:
                self.connection.execute(
                    f"ALTER TABLE futures_contract_daily_official ALTER COLUMN {column} DROP NOT NULL"
                )
        added_columns: set[str] = set()
        for column in ("volume_sidedness", "open_interest_sidedness", "turnover_quality_status", "parser_version"):
            if column not in existing_columns:
                self.connection.execute(
                    f"ALTER TABLE futures_contract_daily_official ADD COLUMN {column} VARCHAR"
                )
                added_columns.add(column)
        if "turnover_quality_status" in added_columns:
            self.connection.execute(
                "UPDATE futures_contract_daily_official SET turnover_quality_status="
                "CASE WHEN turnover_cny IS NOT NULL THEN 'PASS' ELSE 'UNVERIFIED' END "
                "WHERE turnover_quality_status IS NULL"
            )
        if "parser_version" in added_columns:
            self.connection.execute(
                "UPDATE futures_contract_daily_official SET parser_version=adapter "
                "WHERE parser_version IS NULL"
            )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_contract_daily_official_coverage (
                trade_date VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
                status VARCHAR NOT NULL, record_count INTEGER NOT NULL,
                usable_count INTEGER NOT NULL, adapter VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL, payload_sha256 VARCHAR,
                error VARCHAR, retrieved_at VARCHAR NOT NULL,
                PRIMARY KEY(trade_date, exchange, adapter)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_contract_turnover_heat_daily (
                formula_version VARCHAR NOT NULL, trade_date VARCHAR NOT NULL,
                total_contract_count INTEGER NOT NULL,
                valid_contract_count INTEGER NOT NULL,
                missing_direction_count INTEGER NOT NULL,
                up_contract_count INTEGER NOT NULL,
                down_contract_count INTEGER NOT NULL,
                flat_contract_count INTEGER NOT NULL,
                turnover_valid_contract_count INTEGER NOT NULL,
                turnover_missing_contract_count INTEGER NOT NULL,
                up_turnover_cny DOUBLE NOT NULL,
                down_turnover_cny DOUBLE NOT NULL,
                flat_turnover_cny DOUBLE NOT NULL,
                total_deposit_cny DOUBLE,
                direction_coverage DOUBLE NOT NULL,
                turnover_coverage DOUBLE NOT NULL,
                deposit_coverage DOUBLE NOT NULL,
                count_score_daily DOUBLE,
                turnover_score_daily DOUBLE,
                count_score_10d DOUBLE,
                turnover_score_10d DOUBLE,
                divergence DOUBLE,
                is_warmup BOOLEAN NOT NULL,
                data_quality_status VARCHAR NOT NULL,
                missing_exchanges_json VARCHAR NOT NULL,
                source_cutoff VARCHAR NOT NULL,
                calculation_method VARCHAR NOT NULL,
                calculated_at VARCHAR NOT NULL,
                PRIMARY KEY(formula_version, trade_date)
            )"""
        )

    def _ensure_futures_structure_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_structure_daily (
                chart_id VARCHAR NOT NULL,
                direction VARCHAR NOT NULL,
                trade_date VARCHAR NOT NULL,
                member_key VARCHAR NOT NULL,
                member_name VARCHAR NOT NULL,
                value DOUBLE NOT NULL,
                input_row_count INTEGER NOT NULL,
                missing_row_count INTEGER NOT NULL,
                data_quality_status VARCHAR NOT NULL,
                formula_version VARCHAR NOT NULL,
                price_basis VARCHAR,
                source VARCHAR NOT NULL,
                calculated_at VARCHAR NOT NULL,
                PRIMARY KEY(chart_id, direction, formula_version, trade_date, member_key)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_structure_baseline (
                chart_id VARCHAR NOT NULL,
                direction VARCHAR NOT NULL,
                baseline_version VARCHAR NOT NULL,
                baseline_day VARCHAR NOT NULL,
                threshold DOUBLE NOT NULL,
                stack_order_json VARCHAR NOT NULL,
                primary_members_json VARCHAR NOT NULL,
                other_members_json VARCHAR NOT NULL,
                formula_version VARCHAR NOT NULL,
                price_basis VARCHAR,
                source VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                PRIMARY KEY(chart_id, direction, formula_version)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_structure_input_coverage (
                chart_id VARCHAR NOT NULL, direction VARCHAR NOT NULL,
                formula_version VARCHAR NOT NULL, trade_date VARCHAR NOT NULL,
                input_contract_count INTEGER NOT NULL,
                missing_settlement_count INTEGER NOT NULL,
                missing_open_interest_count INTEGER NOT NULL,
                missing_multiplier_count INTEGER NOT NULL,
                missing_products_json VARCHAR NOT NULL,
                missing_contracts_json VARCHAR NOT NULL,
                missing_volume_count INTEGER NOT NULL DEFAULT 0,
                missing_exchange_count INTEGER NOT NULL DEFAULT 0,
                missing_exchanges_json VARCHAR NOT NULL DEFAULT '[]',
                unverified_volume_count INTEGER NOT NULL DEFAULT 0,
                unverified_open_interest_count INTEGER NOT NULL DEFAULT 0,
                quality_status VARCHAR NOT NULL DEFAULT 'PARTIAL',
                source VARCHAR NOT NULL, calculated_at VARCHAR NOT NULL,
                PRIMARY KEY(chart_id, direction, formula_version, trade_date)
            )"""
        )
        existing_coverage_columns = {
            str(row[1]) for row in self.connection.execute(
                "PRAGMA table_info('futures_structure_input_coverage')"
            ).fetchall()
        }
        extra_coverage_columns = {
            "missing_volume_count": "INTEGER",
            "missing_exchange_count": "INTEGER",
            "missing_exchanges_json": "VARCHAR",
            "unverified_volume_count": "INTEGER",
            "unverified_open_interest_count": "INTEGER",
            "quality_status": "VARCHAR",
        }
        added_coverage_columns = False
        for column, definition in extra_coverage_columns.items():
            if column not in existing_coverage_columns:
                self.connection.execute(
                    f"ALTER TABLE futures_structure_input_coverage ADD COLUMN {column} {definition}"
                )
                added_coverage_columns = True
        if added_coverage_columns:
            self.connection.execute(
                "UPDATE futures_structure_input_coverage SET "
                "missing_volume_count=coalesce(missing_volume_count, 0), "
                "missing_exchange_count=coalesce(missing_exchange_count, 0), "
                "missing_exchanges_json=coalesce(missing_exchanges_json, '[]'), "
                "unverified_volume_count=coalesce(unverified_volume_count, 0), "
                "unverified_open_interest_count=coalesce(unverified_open_interest_count, 0), "
                "quality_status=coalesce(quality_status, 'PARTIAL')"
            )

    def _ensure_futures_member_position_rank_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_member_position_ranks (
                trading_day VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL,
                contract_code VARCHAR NOT NULL,
                product_code VARCHAR NOT NULL,
                side VARCHAR NOT NULL,
                rank INTEGER NOT NULL,
                member_key VARCHAR NOT NULL,
                member_name VARCHAR NOT NULL,
                position DOUBLE NOT NULL,
                position_change DOUBLE,
                source VARCHAR NOT NULL,
                collected_at VARCHAR NOT NULL,
                publisher VARCHAR,
                source_url VARCHAR,
                adapter VARCHAR,
                retrieved_at VARCHAR,
                payload_sha256 VARCHAR,
                quality_status VARCHAR,
                legacy_adapter VARCHAR,
                PRIMARY KEY(trading_day, exchange, contract_code, side, rank, source)
            )"""
        )
        self._ensure_member_position_provenance_columns("futures_member_position_ranks")

    def _ensure_futures_member_position_coverage_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_member_position_coverage (
                trading_day VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                contract_count INTEGER NOT NULL,
                record_count INTEGER NOT NULL,
                source VARCHAR NOT NULL,
                error VARCHAR,
                collected_at VARCHAR NOT NULL,
                publisher VARCHAR,
                source_url VARCHAR,
                adapter VARCHAR,
                retrieved_at VARCHAR,
                payload_sha256 VARCHAR,
                quality_status VARCHAR,
                legacy_adapter VARCHAR,
                PRIMARY KEY(trading_day, exchange, source)
            )"""
        )
        self._ensure_member_position_provenance_columns("futures_member_position_coverage")

    def _ensure_futures_member_trade_rank_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS futures_member_trade_ranks (
                trading_day VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
                contract_code VARCHAR NOT NULL, product_code VARCHAR NOT NULL,
                rank INTEGER NOT NULL, member_key VARCHAR NOT NULL, member_name VARCHAR NOT NULL,
                volume DOUBLE NOT NULL, volume_change DOUBLE, source VARCHAR NOT NULL,
                collected_at VARCHAR NOT NULL, publisher VARCHAR, source_url VARCHAR,
                adapter VARCHAR, retrieved_at VARCHAR, payload_sha256 VARCHAR,
                quality_status VARCHAR, legacy_adapter VARCHAR,
                PRIMARY KEY(trading_day, exchange, contract_code, rank, member_key, source)
            )"""
        )
        self._ensure_member_position_provenance_columns("futures_member_trade_ranks")

    def _ensure_member_position_provenance_columns(self, table: str) -> None:
        columns = {
            str(row[1]) for row in self.connection.execute(f"PRAGMA table_info('{table}')").fetchall()
        }
        for column in _FUTURES_MEMBER_POSITION_PROVENANCE_COLUMNS:
            if column not in columns:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR")
        self.connection.execute(
            f"UPDATE {table} SET publisher=exchange WHERE publisher IS NULL"
        )
        self.connection.execute(
            f"UPDATE {table} SET adapter=source WHERE adapter IS NULL"
        )
        self.connection.execute(
            f"UPDATE {table} SET retrieved_at=collected_at WHERE retrieved_at IS NULL"
        )
        self.connection.execute(
            f"UPDATE {table} SET legacy_adapter=source "
            "WHERE legacy_adapter IS NULL AND source LIKE 'akshare-%'"
        )
        self.connection.execute(
            f"UPDATE {table} SET quality_status="
            "CASE WHEN source LIKE 'akshare-%' THEN 'LEGACY_WRAPPED' ELSE 'UNVERIFIED' END "
            "WHERE quality_status IS NULL"
        )


_OFFICIAL_FUTURES_DAILY_COLUMNS = (
    "trade_date", "exchange", "contract_code", "product_code", "close", "settlement",
    "volume", "open_interest", "turnover_raw", "turnover_cny", "turnover_unit", "turnover_sidedness",
    "source_url", "adapter", "payload_sha256", "retrieved_at", "quality_status",
    "volume_sidedness", "open_interest_sidedness", "turnover_quality_status", "parser_version",
)

_OFFICIAL_FUTURES_DAILY_COVERAGE_COLUMNS = (
    "trade_date", "exchange", "status", "record_count", "usable_count", "adapter",
    "source_url", "payload_sha256", "error", "retrieved_at",
)

_FUTURES_CONTRACT_TURNOVER_HEAT_COLUMNS = (
    "formula_version", "trade_date", "total_contract_count", "valid_contract_count",
    "missing_direction_count", "up_contract_count", "down_contract_count", "flat_contract_count",
    "turnover_valid_contract_count", "turnover_missing_contract_count", "up_turnover_cny",
    "down_turnover_cny", "flat_turnover_cny", "total_deposit_cny", "direction_coverage",
    "turnover_coverage", "deposit_coverage", "count_score_daily", "turnover_score_daily",
    "count_score_10d", "turnover_score_10d", "divergence", "is_warmup",
    "data_quality_status", "missing_exchanges_json", "source_cutoff", "calculation_method",
    "calculated_at",
)

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


_FUTURES_STRUCTURE_DAILY_COLUMNS = (
    "chart_id",
    "direction",
    "trade_date",
    "member_key",
    "member_name",
    "value",
    "input_row_count",
    "missing_row_count",
    "data_quality_status",
    "formula_version",
    "price_basis",
    "source",
    "calculated_at",
)

_FUTURES_STRUCTURE_INPUT_COVERAGE_COLUMNS = (
    "chart_id", "direction", "formula_version", "trade_date",
    "input_contract_count", "missing_settlement_count",
    "missing_open_interest_count", "missing_multiplier_count",
    "missing_products_json", "missing_contracts_json", "source", "calculated_at",
    "missing_volume_count", "missing_exchange_count", "missing_exchanges_json",
    "unverified_volume_count", "unverified_open_interest_count", "quality_status",
)


def _validate_futures_structure_row(row: Mapping[str, Any]) -> None:
    missing = [column for column in _FUTURES_STRUCTURE_DAILY_COLUMNS if column not in row]
    if missing:
        raise ValueError(f"futures structure row missing columns: {missing}")
    for field in (
        "chart_id", "direction", "trade_date", "member_key", "member_name", "formula_version",
        "source", "calculated_at",
    ):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    _finite_number(row["value"], "value")
    for field in ("input_row_count", "missing_row_count"):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if row["missing_row_count"] > row["input_row_count"]:
        raise ValueError("missing_row_count must not exceed input_row_count")
    if row["data_quality_status"] not in {"PASS", "PARTIAL"}:
        raise ValueError("unsupported futures structure data_quality_status")
    if row["price_basis"] is not None and (
        not isinstance(row["price_basis"], str) or not row["price_basis"].strip()
    ):
        raise ValueError("price_basis must be a non-empty string or null")


def _validate_futures_structure_baseline(row: Mapping[str, Any]) -> None:
    required = (
        "chart_id", "direction", "baseline_version", "baseline_day", "threshold", "stack_order",
        "primary_members", "other_members", "formula_version", "price_basis", "source", "created_at",
    )
    missing = [field for field in required if field not in row]
    if missing:
        raise ValueError(f"futures structure baseline missing columns: {missing}")
    for field in (
        "chart_id", "direction", "baseline_version", "baseline_day", "formula_version", "source", "created_at",
    ):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    threshold = _finite_number(row["threshold"], "threshold")
    if not 0 <= threshold < 1:
        raise ValueError("threshold must be between 0 and 1, with zero for rank-based selection")
    for field in ("stack_order", "primary_members", "other_members"):
        if not isinstance(row[field], list):
            raise ValueError(f"{field} must be a list")
    if row["price_basis"] is not None and (
        not isinstance(row["price_basis"], str) or not row["price_basis"].strip()
    ):
        raise ValueError("price_basis must be a non-empty string or null")


def _futures_structure_baseline_dict(row: Sequence[Any]) -> dict[str, Any]:
    fields = (
        "chart_id", "direction", "baseline_version", "baseline_day", "threshold", "stack_order_json",
        "primary_members_json", "other_members_json", "formula_version", "price_basis", "source", "created_at",
    )
    value = dict(zip(fields, row, strict=True))
    return {
        "chart_id": value["chart_id"],
        "direction": value["direction"],
        "baseline_version": value["baseline_version"],
        "baseline_day": value["baseline_day"],
        "threshold": value["threshold"],
        "stack_order": json.loads(value["stack_order_json"]),
        "primary_members": json.loads(value["primary_members_json"]),
        "other_members": json.loads(value["other_members_json"]),
        "formula_version": value["formula_version"],
        "price_basis": value["price_basis"],
        "source": value["source"],
        "created_at": value["created_at"],
    }


_FUTURES_MEMBER_POSITION_PROVENANCE_COLUMNS = (
    "publisher", "source_url", "adapter", "retrieved_at",
    "payload_sha256", "quality_status", "legacy_adapter",
)

_FUTURES_MEMBER_POSITION_RANK_COLUMNS = (
    "trading_day",
    "exchange",
    "contract_code",
    "product_code",
    "side",
    "rank",
    "member_key",
    "member_name",
    "position",
    "position_change",
    "source",
    "collected_at",
    *_FUTURES_MEMBER_POSITION_PROVENANCE_COLUMNS,
)

_FUTURES_MEMBER_POSITION_COVERAGE_COLUMNS = (
    "trading_day",
    "exchange",
    "status",
    "contract_count",
    "record_count",
    "source",
    "error",
    "collected_at",
    *_FUTURES_MEMBER_POSITION_PROVENANCE_COLUMNS,
)

_FUTURES_MEMBER_TRADE_RANK_COLUMNS = (
    "trading_day", "exchange", "contract_code", "product_code", "rank",
    "member_key", "member_name", "volume", "volume_change", "source", "collected_at",
    *_FUTURES_MEMBER_POSITION_PROVENANCE_COLUMNS,
)


def _member_rank_with_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    prepared = dict(row)
    source = str(prepared.get("source") or "")
    prepared["publisher"] = prepared.get("publisher") or prepared.get("exchange")
    prepared.setdefault("source_url", None)
    prepared["adapter"] = prepared.get("adapter") or source
    prepared["retrieved_at"] = prepared.get("retrieved_at") or prepared.get("collected_at")
    prepared.setdefault("payload_sha256", None)
    prepared["quality_status"] = prepared.get("quality_status") or (
        "LEGACY_WRAPPED" if source.startswith("akshare-") else "UNVERIFIED"
    )
    prepared["legacy_adapter"] = prepared.get("legacy_adapter") or (
        source if source.startswith("akshare-") else None
    )
    return prepared


def _member_coverage_with_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    return _member_rank_with_provenance(row)


def _member_trade_rank_with_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    return _member_rank_with_provenance(row)


def _validate_futures_member_position_rank(row: Mapping[str, Any]) -> None:
    missing = [column for column in _FUTURES_MEMBER_POSITION_RANK_COLUMNS if column not in row]
    if missing:
        raise ValueError(f"member position rank row missing columns: {missing}")
    for field in (
        "trading_day", "exchange", "contract_code", "product_code", "side", "member_key",
        "member_name", "source", "collected_at",
    ):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if row["side"] not in {"LONG", "SHORT"}:
        raise ValueError("member position rank side must be LONG or SHORT")
    rank = row["rank"]
    if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
        raise ValueError("member position rank must be a positive integer")
    if _finite_number(row["position"], "position") < 0:
        raise ValueError("position must be non-negative")
    if row["position_change"] is not None:
        _finite_number(row["position_change"], "position_change")
    for field in ("publisher", "adapter", "retrieved_at", "quality_status"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")


def _validate_futures_member_position_coverage(row: Mapping[str, Any]) -> None:
    missing = [column for column in _FUTURES_MEMBER_POSITION_COVERAGE_COLUMNS if column not in row]
    if missing:
        raise ValueError(f"member position coverage row missing columns: {missing}")
    for field in ("trading_day", "exchange", "status", "source", "collected_at"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if row["status"] not in {"PASS", "PARTIAL", "FAILED", "UNSUPPORTED", "NO_PUBLICATION", "DEFERRED"}:
        raise ValueError("unsupported member position coverage status")
    for field in ("contract_count", "record_count"):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    error = row["error"]
    if error is not None and (not isinstance(error, str) or not error.strip()):
        raise ValueError("error must be a non-empty string or null")
    for field in ("publisher", "adapter", "retrieved_at", "quality_status"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")


def _validate_futures_member_trade_rank(row: Mapping[str, Any]) -> None:
    missing = [column for column in _FUTURES_MEMBER_TRADE_RANK_COLUMNS if column not in row]
    if missing:
        raise ValueError(f"member trade rank row missing columns: {missing}")
    for field in (
        "trading_day", "exchange", "contract_code", "product_code", "member_key",
        "member_name", "source", "collected_at", "publisher", "adapter",
        "retrieved_at", "quality_status",
    ):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    rank = row["rank"]
    if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
        raise ValueError("trade rank must be a positive integer")
    if _finite_number(row["volume"], "volume") < 0:
        raise ValueError("volume must be non-negative")
    if row["volume_change"] is not None:
        _finite_number(row["volume_change"], "volume_change")


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
