from pathlib import Path
from types import SimpleNamespace
import threading
import time

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


def test_inventory_attributes_latest_samples_only_to_their_own_period(tmp_path: Path, monkeypatch):
    connection = duckdb.connect()
    connection.execute("CREATE TABLE instrument_file(instrument_id VARCHAR, file_path VARCHAR, market VARCHAR, "
                       "asset_type VARCHAR, period VARCHAR, row_count BIGINT, earliest_bar_at VARCHAR, latest_bar_at VARCHAR)")
    connection.execute("CREATE TABLE instrument_period(instrument_id VARCHAR, period VARCHAR)")
    connection.execute("CREATE TABLE instrument_latest(instrument_id VARCHAR, market VARCHAR, asset_type VARCHAR, "
                       "period VARCHAR, bar_json VARCHAR)")
    for period in ("1d", "5m", "1w"):
        connection.execute("INSERT INTO instrument_file VALUES ('X', ?, 'CN', 'FUTURE', ?, 7, '2026-01-01', '2026-10-30')",
                           [period, period])
        connection.execute("INSERT INTO instrument_period VALUES ('X', ?)", [period])
    connection.execute("INSERT INTO instrument_latest VALUES ('X', 'CN', 'FUTURE', '5m', ?)",
                       ['{"source":"tdx_futures_local","quality_status":"PASS","open":1}'])
    monkeypatch.setattr(sources, "_manifest_connection", lambda _: connection)
    inventory, total = sources._local_inventory(tmp_path)
    grouped = {item["period"]: item for item in inventory}
    assert total == 1 and grouped["5m"]["sources"] == ["tdx_futures_local"]
    assert grouped["5m"]["sourceSampleCounts"] == {"tdx_futures_local": 1}
    assert grouped["1d"]["sources"] == [] and grouped["1w"]["sources"] == []
    assert grouped["1d"]["instruments"] == 1 and grouped["1d"]["rows"] == 7
    assert grouped["1d"]["earliestBarAt"] == "2026-01-01" and grouped["1d"]["latestBarAt"] == "2026-10-30"
    assert grouped["1d"]["fieldCoverageSamples"] == 0


def test_storage_snapshot_returns_immediately_then_publishes_complete_result(tmp_path: Path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(sources, "market_data_version", lambda _root: "synthetic-revision")

    def slow_inventory(_root):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return {"available": True, "bytes": 17, "files": 1, "missingFiles": 0,
                "groups": [], "scope": "MANIFEST_SILVER_FILES", "generatedAt": "synthetic"}

    monkeypatch.setattr(sources, "_storage_inventory", slow_inventory)
    first = sources._storage_snapshot(tmp_path)
    assert first["snapshotStatus"] == "BUILDING"
    assert first["bytes"] is None
    assert started.wait(2)
    assert sources._storage_snapshot(tmp_path)["snapshotStatus"] == "BUILDING"
    release.set()
    for _ in range(100):
        current = sources._storage_snapshot(tmp_path)
        if current["snapshotStatus"] == "CURRENT":
            break
        time.sleep(.01)
    assert current["bytes"] == 17
    assert current["sourceRevision"] == "synthetic-revision"
    assert calls == [1]


def test_catalog_lock_is_reported_separately_from_empty_directory(tmp_path: Path, monkeypatch):
    (tmp_path / "catalog.duckdb").write_bytes(b"synthetic-present")
    monkeypatch.setattr(sources, "_local_inventory", lambda _root: ([], 0))
    monkeypatch.setattr(sources, "_storage_snapshot", lambda _root: {"available": False, "snapshotStatus": "BUILDING"})

    def unavailable(*_args):
        raise duckdb.IOException("synthetic catalog lock")

    monkeypatch.setattr(sources, "_registered_datasets", unavailable)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(data_root=tmp_path)))
    result = sources._inventory_payload(request)
    assert result["metadata"]["catalogStatus"] == "TEMPORARILY_UNAVAILABLE"
    assert result["storage"]["snapshotStatus"] == "BUILDING"
