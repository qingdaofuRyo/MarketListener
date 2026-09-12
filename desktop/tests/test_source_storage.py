from pathlib import Path

import duckdb

from market_monitor.web_api import sources


def test_storage_counts_unique_registered_files_and_skips_external(tmp_path: Path, monkeypatch):
    root = tmp_path / "data"
    silver = root / "silver"
    silver.mkdir(parents=True)
    shared = silver / "shared.parquet"
    shared.write_bytes(b"fixture-data")
    absent = silver / "missing.parquet"
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"not-market-data")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE instrument_file(file_path VARCHAR, market VARCHAR, asset_type VARCHAR, period VARCHAR)")
    for path, market in [(shared, "CN"), (shared, "CN"), (shared, "HK"), (absent, "CN"), (outside, "CN")]:
        connection.execute("INSERT INTO instrument_file VALUES (?, ?, 'STOCK', '1d')", [str(path), market])
    monkeypatch.setattr(sources, "_manifest_connection", lambda _: connection)
    result = sources._storage_inventory(root)
    assert result["available"] is True
    assert result["bytes"] == len(b"fixture-data")
    assert result["files"] == 1
    assert result["missingFiles"] == 2
    assert result["groups"][0]["market"] == "SHARED"
    assert result["groups"][0]["updatedAt"]


def test_storage_unavailable_is_not_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sources, "_manifest_connection", lambda _: None)
    result = sources._storage_inventory(tmp_path)
    assert result["available"] is False
    assert result["bytes"] is None


def test_storage_empty_ready_manifest_is_zero(tmp_path: Path, monkeypatch):
    connection = duckdb.connect()
    connection.execute("CREATE TABLE instrument_file(file_path VARCHAR, market VARCHAR, asset_type VARCHAR, period VARCHAR)")
    monkeypatch.setattr(sources, "_manifest_connection", lambda _: connection)
    result = sources._storage_inventory(tmp_path)
    assert result["available"] is True
    assert result["bytes"] == 0
