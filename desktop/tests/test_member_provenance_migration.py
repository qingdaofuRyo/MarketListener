"""The startup provenance upgrade must be one-time and restartable."""

from pathlib import Path
from typing import Any

import duckdb
import pytest

from market_monitor.storage import MarketStore


_TABLES = (
    "futures_member_position_ranks",
    "futures_member_position_coverage",
    "futures_member_trade_ranks",
)
_COLLECTED_AT = "2026-10-14T16:00:00+08:00"
_PROVENANCE = (
    "publisher", "adapter", "retrieved_at", "quality_status", "legacy_adapter",
    "source_url", "payload_sha256",
)


class _ConnectionSpy:
    def __init__(self, connection: duckdb.DuckDBPyConnection, fail_table: str | None = None) -> None:
        self.connection = connection
        self.fail_table = fail_table
        self.statements: list[str] = []

    def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        normalized = " ".join(query.split())
        self.statements.append(normalized)
        if self.fail_table and normalized.startswith(f"UPDATE {self.fail_table} SET adapter="):
            raise OSError("synthetic interruption during provenance backfill")
        return self.connection.execute(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)


def _legacy_catalog(root: Path) -> None:
    """Reproduce the pre-marker schema with already-added, nullable columns."""
    store = MarketStore(root)
    try:
        store.connection.execute(
            "DELETE FROM market_schema_migrations WHERE migration_key LIKE 'member-provenance-v1:%'"
        )
        for table in _TABLES:
            identity: dict[str, Any] = {
                "trading_day": "2026-10-14", "exchange": "SHFE", "collected_at": _COLLECTED_AT,
            }
            if table == "futures_member_position_coverage":
                identity.update(status="PASS", contract_count=1, record_count=1)
            else:
                identity.update(
                    contract_code="AG2612", product_code="AG", rank=1,
                    member_key="synthetic-member", member_name="合成测试席位",
                )
                if table == "futures_member_position_ranks":
                    identity.update(side="long", position=10)
                else:
                    identity.update(volume=20)
            for source in ("akshare-synthetic", "official-synthetic", "verified-synthetic"):
                row = {**identity, "source": source}
                if source == "verified-synthetic":
                    row.update(
                        publisher="verified-publisher", adapter="verified-adapter",
                        retrieved_at="2026-10-14T17:00:00+08:00", quality_status="PASS",
                        legacy_adapter="verified-legacy", source_url="https://example.invalid/source",
                        payload_sha256="a" * 64,
                    )
                columns = tuple(row)
                store.connection.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
    finally:
        store.close()


def _markers(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0] for row in connection.execute(
            "SELECT migration_key FROM market_schema_migrations "
            "WHERE migration_key LIKE 'member-provenance-v1:%'"
        ).fetchall()
    }


def _assert_backfilled(connection: duckdb.DuckDBPyConnection) -> None:
    for table in _TABLES:
        rows = {
            row[0]: row[1:] for row in connection.execute(
                f"SELECT source, {', '.join(_PROVENANCE)} FROM {table}"
            ).fetchall()
        }
        assert rows["akshare-synthetic"] == (
            "SHFE", "akshare-synthetic", _COLLECTED_AT, "LEGACY_WRAPPED",
            "akshare-synthetic", None, None,
        )
        assert rows["official-synthetic"] == (
            "SHFE", "official-synthetic", _COLLECTED_AT, "UNVERIFIED", None, None, None,
        )
        assert rows["verified-synthetic"] == (
            "verified-publisher", "verified-adapter", "2026-10-14T17:00:00+08:00", "PASS",
            "verified-legacy", "https://example.invalid/source", "a" * 64,
        )


def test_existing_nullable_provenance_is_backfilled_and_marked(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    _legacy_catalog(root)

    store = MarketStore(root)
    try:
        _assert_backfilled(store.connection)
        assert _markers(store.connection) == {f"member-provenance-v1:{table}" for table in _TABLES}
        assert store.connection.execute(
            "SELECT count(*) FROM market_schema_migrations WHERE completed_at IS NULL OR completed_at=''"
        ).fetchone() == (0,)
    finally:
        store.close()


def test_completed_migrations_do_not_update_rows_on_next_open(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "legacy"
    _legacy_catalog(root)
    store = MarketStore(root)
    store.close()

    spy = _ConnectionSpy(duckdb.connect(str(root / "catalog.duckdb")))
    monkeypatch.setattr("market_monitor.storage.connect_catalog", lambda _root: spy)
    store = MarketStore(root)
    try:
        _assert_backfilled(store.connection)
        assert not [
            statement for statement in spy.statements
            if any(statement.startswith(f"UPDATE {table} ") for table in _TABLES)
        ]
        assert not [
            statement for statement in spy.statements
            if any(statement.startswith(f"SELECT 1 FROM {table} WHERE publisher IS NULL") for table in _TABLES)
        ]
    finally:
        store.close()


@pytest.mark.parametrize("failed_table", _TABLES)
def test_interrupted_backfill_rolls_back_and_retries(
    tmp_path: Path, monkeypatch, failed_table: str,
) -> None:
    root = tmp_path / "legacy"
    _legacy_catalog(root)
    spy = _ConnectionSpy(duckdb.connect(str(root / "catalog.duckdb")), fail_table=failed_table)
    try:
        with monkeypatch.context() as patch:
            patch.setattr("market_monitor.storage.connect_catalog", lambda _root: spy)
            with pytest.raises(OSError, match="synthetic interruption"):
                MarketStore(root)
        assert f"member-provenance-v1:{failed_table}" not in _markers(spy.connection)
        assert spy.connection.execute(
            f"SELECT publisher, adapter, retrieved_at, quality_status, legacy_adapter "
            f"FROM {failed_table} WHERE source='akshare-synthetic'"
        ).fetchone() == (None, None, None, None, None)
        assert "ROLLBACK" in spy.statements
    finally:
        spy.close()

    store = MarketStore(root)
    try:
        _assert_backfilled(store.connection)
        assert _markers(store.connection) == {f"member-provenance-v1:{table}" for table in _TABLES}
    finally:
        store.close()
