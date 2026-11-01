"""Synthetic Catalog/Silver reconciliation; never operates on user data."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from market_monitor import market_silver_catalog_audit as audit_module
from market_monitor.market_data_version import advance_market_data_version, market_data_version
from market_monitor.market_silver_catalog_audit import audit_silver_catalog
from market_monitor.storage import MarketStore, PartitionKey


def _fixture(root: Path) -> tuple[MarketStore, Path, str]:
    store = MarketStore(root)
    bar = {
        "instrument_id": "CN.DCE.FUTURE.JM2701", "period": "1d",
        "bar_open_time": "2026-10-09T15:00:00+08:00", "close": 1200.0,
    }
    run_id = store.begin_run("synthetic-audit")
    path = store.write_silver_bars(
        PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-audit"),
        [bar], bar["bar_open_time"], run_id,
    )
    return store, path, market_data_version(root)


def _audit(root: Path, revision: str, store: MarketStore, **kwargs):
    return audit_silver_catalog(root, revision, catalog_connection=store.connection, **kwargs)


def _codes(report) -> set[str]:
    return {item.code for item in report.issues}


def test_matching_catalog_silver_sha_and_parquet_row_count_pass(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store, path, revision = _fixture(root)
    try:
        report = _audit(root, revision, store)
        assert report.ok and report.status == "PASS"
        assert report.catalog_partitions == report.physical_files == report.verified_files == 1
        assert report.files == (path,)
        assert report.hashed_bytes == path.stat().st_size
        assert report.catalog_digest and len(report.catalog_digest) == 64
    finally:
        store.close()


def test_closed_catalog_can_be_audited_through_read_only_connection(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store, path, revision = _fixture(root)
    store.close()
    report = audit_silver_catalog(root, revision)
    assert report.ok and report.files == (path,)


def test_unpublished_tmp_is_not_a_parquet_partition(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store, path, revision = _fixture(root)
    try:
        shutil.copy2(path, path.with_name(path.name + ".unpublished.tmp"))
        report = _audit(root, revision, store)
        assert report.ok and report.files == (path,)
    finally:
        store.close()


@pytest.mark.parametrize("damage,code", [
    ("orphan", "ORPHAN_PARQUET"),
    ("missing", "MISSING_PARQUET"),
    ("wrong_sha", "SHA_MISMATCH"),
    ("wrong_count", "ROW_COUNT_MISMATCH"),
    ("duplicate", "DUPLICATE_CATALOG_PATH"),
    ("bad_status", "INVALID_CATALOG_STATUS"),
    ("traversal", "INVALID_CATALOG_PATH"),
])
def test_catalog_physical_differences_fail_closed(tmp_path: Path, damage: str, code: str) -> None:
    root = tmp_path / "data"
    store, path, revision = _fixture(root)
    try:
        if damage == "orphan":
            shutil.copy2(path, path.with_name("orphan.parquet"))
        elif damage == "missing":
            path.unlink()
        elif damage == "wrong_sha":
            store.connection.execute("UPDATE partitions SET sha256=?", ["0" * 64])
        elif damage == "wrong_count":
            store.connection.execute("UPDATE partitions SET row_count=2")
        elif damage == "duplicate":
            store.connection.execute(
                "INSERT INTO partitions SELECT 'synthetic-duplicate', file_path, row_count, "
                "data_cutoff, sha256, source_run_id, status, updated_at FROM partitions LIMIT 1"
            )
        elif damage == "bad_status":
            store.connection.execute("UPDATE partitions SET status='PARTIAL'")
        elif damage == "traversal":
            store.connection.execute("UPDATE partitions SET file_path='../outside.parquet'")
        report = _audit(root, revision, store)
        assert report.status == "FAILED"
        assert not report.ok and report.files == ()
        assert code in _codes(report)
    finally:
        store.close()


def test_pending_revision_and_missing_revision_file_do_not_initialize_state(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    store = MarketStore(root)
    try:
        version_path = root / "state" / "market_data_version.json"
        assert not version_path.exists()
        report = _audit(root, "caller-observed-revision", store)
        assert report.status == "RETRY" and "REVISION_UNSETTLED" in _codes(report)
        assert not version_path.exists()

        revision = advance_market_data_version(root, pending=True)[1]
        report = _audit(root, revision, store)
        assert report.status == "RETRY" and "REVISION_UNSETTLED" in _codes(report)
    finally:
        store.close()


def test_revision_change_during_streaming_hash_is_retry(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    store, _path, revision = _fixture(root)
    original = audit_module._stream_sha256

    def change_revision(path: Path, **kwargs) -> str:
        digest = original(path, **kwargs)
        advance_market_data_version(root)
        return digest

    try:
        monkeypatch.setattr(audit_module, "_stream_sha256", change_revision)
        report = _audit(root, revision, store)
        assert report.status == "RETRY" and "REVISION_CHANGED" in _codes(report)
        assert report.files == ()
    finally:
        store.close()


def test_catalog_change_during_hash_is_retry(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    store, _path, revision = _fixture(root)
    original = audit_module._stream_sha256

    def change_catalog(path: Path, **kwargs) -> str:
        digest = original(path, **kwargs)
        store.connection.execute("UPDATE partitions SET sha256=?", ["f" * 64])
        return digest

    try:
        monkeypatch.setattr(audit_module, "_stream_sha256", change_catalog)
        report = _audit(root, revision, store)
        assert report.status == "RETRY" and "CATALOG_CHANGED" in _codes(report)
    finally:
        store.close()


def test_audit_respects_file_and_byte_budgets(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store, _path, revision = _fixture(root)
    try:
        assert "INVALID_AUDIT_ARGUMENT" in _codes(_audit(root, revision, store, max_files=0))
        report = _audit(root, revision, store, max_total_bytes=1)
        assert report.status == "FAILED"
        assert "BYTE_LIMIT_EXCEEDED" in _codes(report)
        second = {
            "instrument_id": "CN.DCE.FUTURE.JM2705", "period": "1d",
            "bar_open_time": "2026-10-09T15:00:00+08:00", "close": 1201.0,
        }
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "synthetic-audit-2"),
            [second], second["bar_open_time"], store.begin_run("synthetic-audit-2"),
        )
        report = _audit(root, market_data_version(root), store, max_files=1)
        assert "FILE_LIMIT_EXCEEDED" in _codes(report)
    finally:
        store.close()


def test_time_budget_reports_only_the_actual_limit(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store, _path, revision = _fixture(root)
    try:
        report = _audit(root, revision, store, max_seconds=1e-9)
        assert report.status == "RETRY"
        assert _codes(report) == {"AUDIT_TIME_LIMIT_EXCEEDED"}
    finally:
        store.close()
