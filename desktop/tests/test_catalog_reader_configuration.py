"""Catalog readers must coexist with the product's writable DuckDB handle."""

from pathlib import Path

import duckdb
import pytest

from market_monitor.futures_weekly_heat_gold import read_weekly_heat_gold
from market_monitor.storage import MarketStore, connect_catalog
from market_monitor.web_api.sources import _catalog_tables, _registered_datasets


def test_catalog_inventory_and_heat_reader_share_writer_configuration(tmp_path: Path) -> None:
    store = MarketStore(tmp_path)
    try:
        tables = _catalog_tables(tmp_path)
        assert any(item["name"] == "runs" for item in tables)
        assert _registered_datasets(tmp_path, []) == []
        assert read_weekly_heat_gold(tmp_path)["available"] is False
    finally:
        store.close()


def test_weekly_heat_get_does_not_initialize_an_empty_catalog(tmp_path: Path) -> None:
    database = tmp_path / "catalog.duckdb"
    duckdb.connect(str(database)).close()

    assert read_weekly_heat_gold(tmp_path)["available"] is False

    connection = duckdb.connect(str(database))
    try:
        assert connection.execute("SHOW TABLES").fetchall() == []
    finally:
        connection.close()


def test_catalog_connection_retries_only_a_bounded_lock_window(tmp_path: Path, monkeypatch) -> None:
    original = duckdb.connect
    attempts = []
    waits = []

    def busy_twice(path):
        attempts.append(path)
        if len(attempts) <= 2:
            raise duckdb.IOException("temporary file lock")
        return original(path)

    monkeypatch.setattr("market_monitor.storage.duckdb.connect", busy_twice)
    monkeypatch.setattr("market_monitor.storage.time.sleep", waits.append)
    connection = connect_catalog(tmp_path)
    connection.close()
    assert len(attempts) == 3
    assert waits == [0.05, 0.1]

    attempts.clear()

    def always_busy(path):
        attempts.append(path)
        raise duckdb.IOException("persistent file lock")

    monkeypatch.setattr("market_monitor.storage.duckdb.connect", always_busy)
    with pytest.raises(duckdb.IOException, match="persistent file lock"):
        connect_catalog(tmp_path)
    assert len(attempts) == 5
