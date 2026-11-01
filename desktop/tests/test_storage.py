from datetime import datetime
import hashlib
import os

import duckdb
import pytest

from market_monitor.storage import MarketStore, PartitionKey
from market_monitor.market_data_version import market_data_version, revision_is_settled
import market_monitor.market_data_version as versions


def bars(close: float) -> list[dict[str, object]]:
    return [{"instrument_id": "CN.SSE.STOCK.600519", "bar_open_time": "2026-08-03T09:30:00+08:00", "close": close}]


def key() -> PartitionKey:
    return PartitionKey("CN", "STOCK", "15m", 2026, "CN-STOCK-15m-20260803")


def test_bronze_and_silver_are_recorded_with_an_idempotent_partition(tmp_path) -> None:
    store = MarketStore(tmp_path / "data")
    run_id = store.begin_run("test_provider")
    bronze = store.write_bronze(run_id, "test_provider", {"payload": [{"raw": 1}]})
    first = store.write_silver_bars(key(), bars(100.0), "2026-08-03T10:00:00+08:00", run_id)
    second = store.write_silver_bars(key(), bars(100.0), "2026-08-03T10:00:00+08:00", run_id)
    store.finish_run(run_id, "PASS")
    metadata = store.partition_metadata(key().partition_id)
    assert bronze.is_file() and first == second and first.is_file()
    assert duckdb.sql(f"SELECT count(*) FROM read_parquet('{first.as_posix()}')").fetchone()[0] == 1
    assert metadata and metadata[1] == 1 and metadata[4] == "COMPLETE"


def test_failed_retry_does_not_replace_previous_complete_partition(tmp_path, monkeypatch) -> None:
    store = MarketStore(tmp_path / "data")
    run_id = store.begin_run("test_provider")
    partition = store.write_silver_bars(key(), bars(100.0), "2026-08-03T10:00:00+08:00", run_id)
    before = partition.read_bytes()
    monkeypatch.setattr("market_monitor.storage.os.replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.write_silver_bars(key(), bars(101.0), "2026-08-03T10:15:00+08:00", run_id)
    assert partition.read_bytes() == before


def test_new_fetch_timestamp_does_not_rewrite_identical_silver_fact(tmp_path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        run_id = store.begin_run("repeat-provider")
        first = [{**bars(100.0)[0], "fetched_at": "2026-08-03T10:01:00+08:00"}]
        path = store.write_silver_bars(key(), first, "2026-08-03T10:00:00+08:00", run_id)
        original_bytes = path.read_bytes()
        original_revision = market_data_version(root)
        repeated = [{**first[0], "fetched_at": "2026-08-04T10:01:00+08:00"}]
        assert store.write_silver_bars(key(), repeated, "2026-08-04T10:00:00+08:00", run_id) == path
        assert path.read_bytes() == original_bytes
        assert market_data_version(root) == original_revision
        assert not list(path.parent.glob("*.tmp"))

        corrected = [{**repeated[0], "close": 101.0}]
        store.write_silver_bars(key(), corrected, "2026-08-04T10:00:00+08:00", run_id)
        assert market_data_version(root) != original_revision
        assert path.read_bytes() != original_bytes
    finally:
        store.close()


def test_identical_market_fact_repairs_a_wrong_catalog_sha(tmp_path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        run_id = store.begin_run("catalog-repair")
        first = [{**bars(100.0)[0], "fetched_at": "2026-08-03T10:00:00+08:00"}]
        path = store.write_silver_bars(key(), first, first[0]["bar_open_time"], run_id)
        original_revision = market_data_version(root)
        store.connection.execute("UPDATE partitions SET sha256=? WHERE partition_id=?",
                                 ["0" * 64, key().partition_id])
        repeated = [{**first[0], "fetched_at": "2026-08-04T10:00:00+08:00"}]
        store.write_silver_bars(key(), repeated, repeated[0]["bar_open_time"], run_id)
        assert market_data_version(root) != original_revision
        assert store.partition_metadata(key().partition_id)[3] == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        store.close()


def test_failed_parquet_promotion_settles_verified_old_file(tmp_path, monkeypatch) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        run_id = store.begin_run("synthetic-failed-promotion")
        path = store.write_silver_bars(key(), bars(100.0), "2026-08-03T10:00:00+08:00", run_id)
        before = path.read_bytes()
        old_revision = market_data_version(root)
        original_replace = os.replace

        def fail_parquet_only(source, target):
            if str(target).endswith(".parquet"):
                raise OSError("synthetic promotion failure")
            return original_replace(source, target)

        monkeypatch.setattr("market_monitor.storage.os.replace", fail_parquet_only)
        with pytest.raises(OSError, match="synthetic promotion failure"):
            store.write_silver_bars(key(), bars(101.0), "2026-08-03T10:15:00+08:00", run_id)
        assert path.read_bytes() == before
        current = market_data_version(root)
        assert current != old_revision
        assert revision_is_settled(root, current)
    finally:
        store.close()


def test_same_market_fact_replays_an_abandoned_pending_write(tmp_path, monkeypatch) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        run_id = store.begin_run("replay-after-interruption")
        first = [{**bars(100.0)[0], "fetched_at": "2026-08-03T10:00:00+08:00"}]
        path = store.write_silver_bars(key(), first, first[0]["bar_open_time"], run_id)
        _, abandoned = versions.advance_market_data_version(
            root, changed_file=path, sha256=hashlib.sha256(b"abandoned staged file").hexdigest(),
            pending=True,
        )
        monkeypatch.setattr(versions, "_process_is_running", lambda _pid: False)
        repeated = [{**first[0], "fetched_at": "2026-08-04T10:00:00+08:00"}]
        store.write_silver_bars(key(), repeated, repeated[0]["bar_open_time"], run_id)
        assert market_data_version(root) != abandoned
        assert versions.pending_market_writes(root) == {"status": "CLEAR", "count": 0}
        assert revision_is_settled(root, market_data_version(root))
    finally:
        store.close()
