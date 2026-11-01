import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_monitor.tdx_import_workflow import (
    STATE_FILE, recover_interrupted_workflow, run_local_terminal_imports, save_state,
    terminal_import_selection,
)
from market_monitor.market_query_cache import get_kline_query_store
from market_monitor.market_data_version import market_data_version
from market_monitor.process_runtime import process_start_token
from market_monitor.storage import MarketStore, PartitionKey
from web_fixtures import silver_row


def test_live_import_owner_is_not_overwritten(tmp_path: Path):
    initial = {"status": "RUNNING", "ownerPid": os.getpid(), "terminals": {"financial": {"status": "RUNNING"}}}
    save_state(tmp_path / STATE_FILE, initial)
    with pytest.raises(RuntimeError, match="正在运行"):
        recover_interrupted_workflow(tmp_path)
    assert json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8")) == initial


def test_process_creation_token_is_stable_for_current_owner():
    token = process_start_token(os.getpid())
    assert token is not None
    assert process_start_token(os.getpid()) == token


@pytest.mark.parametrize("actual", ["fixture-current-lifecycle", None])
def test_live_matching_or_unknown_process_identity_still_blocks_recovery(tmp_path: Path, monkeypatch, actual):
    initial = {"status": "RUNNING", "ownerPid": os.getpid(),
               "ownerProcessStartedAt": "fixture-current-lifecycle", "terminals": {"financial": {"status": "RUNNING"}}}
    save_state(tmp_path / STATE_FILE, initial)
    monkeypatch.setattr("market_monitor.process_runtime.process_start_token", lambda pid: actual)
    with pytest.raises(RuntimeError, match="正在运行"):
        recover_interrupted_workflow(tmp_path)
    assert json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8")) == initial


def test_reused_live_pid_does_not_block_the_interrupted_owner(tmp_path: Path, monkeypatch):
    save_state(tmp_path / STATE_FILE, {"status": "RUNNING", "ownerPid": os.getpid(),
                                     "ownerProcessStartedAt": "fixture-exited-lifecycle",
                                     "terminals": {"financial": {"status": "RUNNING"}}})
    monkeypatch.setattr("market_monitor.process_runtime.process_start_token", lambda pid: "fixture-reused-lifecycle")
    assert recover_interrupted_workflow(tmp_path) == []
    state = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "PARTIAL_FAILURE"
    assert state["terminals"]["financial"]["status"] == "PARTIAL_FAILURE"


def test_legacy_interrupted_workflow_marks_only_unfinished_terminal(tmp_path: Path):
    initial = {"status": "RUNNING", "terminals": {
        "financial": {"status": "RUNNING"}, "futures": {"status": "NO_CHANGE"}}}
    save_state(tmp_path / STATE_FILE, initial)
    assert recover_interrupted_workflow(tmp_path) == []
    state = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "PARTIAL_FAILURE"
    assert state["terminals"]["financial"]["status"] == "PARTIAL_FAILURE"
    assert state["terminals"]["futures"]["status"] == "NO_CHANGE"


def _interrupted_import_with_checkpoint(root: Path, catalog_status: str):
    instrument = "CN.SSE.STOCK.600519.TDX_LOCAL"
    store = MarketStore(root)
    try:
        baseline = store.begin_run("fixture-baseline")
        bar = silver_row(instrument, "2026-09-24")
        store.write_silver_bars(
            PartitionKey("CN", "STOCK", "1d", 2026, "baseline-import"),
            [bar], bar["bar_open_time"], baseline, update_query_cache=False,
        )
        store.finish_run(baseline, "COMPLETE")
    finally:
        store.close()
    cache = get_kline_query_store(root)
    cache.rebuild()
    store = MarketStore(root)
    try:
        run_id = store.begin_run("import-tdx-local:通达信金融终端:tdx-cn-v2")
        bar = silver_row(instrument, "2026-09-30")
        partition = store.write_silver_bars(
            PartitionKey("CN", "STOCK", "1d", 2026, "checkpoint-import"),
            [bar], bar["bar_open_time"], run_id, update_query_cache=False,
        )
        if catalog_status != "RUNNING":
            store.finish_run(run_id, catalog_status, "已提交文件检查点；尚未刷新查询缓存")
    finally:
        store.close()
    save_state(root / "state/tdx_local_import.json", {"files": {
        "vipdoc/sh/lday/sh600519.day": {"size": 64, "mtime_ns": 100,
                                       "normalization_version": "tdx-cn-v2"},
    }})
    state = {"status": "RUNNING", "terminals": {"financial": {"status": "RUNNING", "runId": run_id}}}
    save_state(root / STATE_FILE, state)
    return instrument, run_id, partition, cache, state


@pytest.mark.parametrize("catalog_status", ["COMPLETE", "PARTIAL_FAILURE", "FAILED"])
def test_finalized_run_checkpoint_recovers_missing_cache_without_reimport(tmp_path: Path, catalog_status: str):
    instrument, run_id, _partition, cache, _state = _interrupted_import_with_checkpoint(tmp_path, catalog_status)
    checkpoint = (tmp_path / "state/tdx_local_import.json").read_bytes()
    recovered = recover_interrupted_workflow(tmp_path)
    assert recovered == [{"terminal": "financial", "runId": run_id, "rows": 1, "partitions": 1}]
    assert [bar["trading_date"] for bar in cache.read_bars(instrument, "1d", 10)] == ["2026-09-24", "2026-09-30"]
    assert (tmp_path / "state/tdx_local_import.json").read_bytes() == checkpoint
    with_status = MarketStore(tmp_path)
    try:
        assert with_status.connection.execute("SELECT status FROM runs WHERE run_id=?", [run_id]).fetchone()[0] == catalog_status
        assert with_status.connection.execute("SELECT count(*) FROM partitions WHERE source_run_id=?", [run_id]).fetchone()[0] == 1
    finally:
        with_status.close()
    assert json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["status"] == "PARTIAL_FAILURE"


def test_failed_cache_recovery_keeps_workflow_retryable_after_run_finalization(tmp_path: Path, monkeypatch):
    instrument, run_id, _partition, cache, initial = _interrupted_import_with_checkpoint(tmp_path, "RUNNING")
    attempts = []
    original_rebuild = cache.rebuild

    def rebuild():
        attempts.append(1)
        return (SimpleNamespace(ready=False, revision="stale")
                if len(attempts) == 1 else original_rebuild())

    monkeypatch.setattr(cache, "rebuild", rebuild)
    with pytest.raises(RuntimeError, match="缓存恢复失败"):
        recover_interrupted_workflow(tmp_path)
    assert json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8")) == initial
    store = MarketStore(tmp_path)
    try:
        assert store.connection.execute("SELECT status FROM runs WHERE run_id=?", [run_id]).fetchone()[0] == "PARTIAL_FAILURE"
    finally:
        store.close()
    assert recover_interrupted_workflow(tmp_path) == [{"terminal": "financial", "runId": run_id, "rows": 1, "partitions": 1}]
    assert attempts == [1, 1]
    assert [bar["trading_date"] for bar in cache.read_bars(instrument, "1d", 10)] == ["2026-09-24", "2026-09-30"]


def test_interrupted_workflow_uses_saved_base_revision_for_incremental_recovery(tmp_path: Path, monkeypatch):
    instrument, run_id, partition, cache, state = _interrupted_import_with_checkpoint(tmp_path, "RUNNING")
    base_revision = cache.status().revision
    assert base_revision
    state["terminals"]["financial"]["marketRevisionAtStart"] = base_revision
    save_state(tmp_path / STATE_FILE, state)
    captured = []
    from market_monitor.market_query_cache import refresh_kline_cache_partitions as original_refresh

    def refresh(root, files, **kwargs):
        captured.append((list(files), kwargs))
        return original_refresh(root, files, **kwargs)

    monkeypatch.setattr("market_monitor.market_query_cache.refresh_kline_cache_partitions", refresh)
    recovered = recover_interrupted_workflow(tmp_path)
    assert recovered == [{"terminal": "financial", "runId": run_id, "rows": 1, "partitions": 1}]
    assert captured == [([partition], {
        "expected_base_revision": base_revision,
        "target_revision": market_data_version(tmp_path),
    })]
    assert cache.status().ready is True
    assert [bar["trading_date"] for bar in cache.read_bars(instrument, "1d", 10)] == ["2026-09-24", "2026-09-30"]


def test_interrupted_workflow_never_steals_a_later_terminal_run(tmp_path: Path):
    _instrument, original_run_id, _partition, cache, state = _interrupted_import_with_checkpoint(tmp_path, "RUNNING")
    store = MarketStore(tmp_path)
    try:
        later_run_id = store.begin_run("import-tdx-local:通达信金融终端:tdx-cn-v2")
        store.finish_run(later_run_id, "COMPLETE")
    finally:
        store.close()
    assert later_run_id != original_run_id
    save_state(tmp_path / STATE_FILE, state)

    recovered = recover_interrupted_workflow(tmp_path)
    assert recovered[0]["runId"] == original_run_id
    assert cache.status().ready is True
    store = MarketStore(tmp_path)
    try:
        statuses = {
            row[0]: row[1] for row in store.connection.execute(
                "SELECT run_id,status FROM runs WHERE run_id IN (?,?)", [original_run_id, later_run_id],
            ).fetchall()
        }
    finally:
        store.close()
    assert statuses == {original_run_id: "PARTIAL_FAILURE", later_run_id: "COMPLETE"}


def test_financial_progress_is_visible_before_import_completes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("market_monitor.futures_bulk.run_bulk_futures", lambda root, **kwargs: {"写入K线": 0})

    def financial(root, *, progress_callback):
        progress_callback({"rows": 250_000, "importedFiles": 30, "scannedFiles": 90})
        current = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
        assert current["ownerPid"] == os.getpid()
        assert current["ownerProcessStartedAt"] == process_start_token(os.getpid())
        assert current["terminals"]["financial"]["status"] == "RUNNING"
        assert current["terminals"]["financial"]["rows"] == 250_000
        assert current["terminals"]["financial"]["scannedFiles"] == 90
        return {"写入K线": 250_000, "导入文件": 30, "扫描文件": 90}

    monkeypatch.setattr("market_monitor.tdx_local.run_tdx_local_import", financial)
    result = run_local_terminal_imports(tmp_path)
    assert result["status"] == "PASS"


def test_unknown_futures_download_skips_only_its_raw_files(tmp_path: Path, monkeypatch):
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    save_state(checkpoint, {"futures": {"future-5m": {"status": "RUNNING_UNKNOWN"}}})
    monkeypatch.setattr("market_monitor.futures_bulk.run_bulk_futures",
                        lambda *_args, **_kwargs: pytest.fail("unknown futures files must not be read"))
    calls = []
    monkeypatch.setattr("market_monitor.tdx_local.run_tdx_local_import",
                        lambda root, **_kwargs: calls.append(root) or {"写入K线": 10, "扫描文件": 1})
    result = run_local_terminal_imports(tmp_path)
    assert calls == [tmp_path]
    assert result["selectedTerminals"] == ["financial"]
    assert result["blockedTerminals"] == {"futures": "RUNNING_UNKNOWN"}
    assert result["selectedStatus"] == "PASS"
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["terminals"]["financial"]["rows"] == 10
    assert "futures" not in result["terminals"]


def test_unreadable_download_checkpoint_blocks_both_imports(tmp_path: Path, monkeypatch):
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{broken", encoding="utf-8")
    assert terminal_import_selection(tmp_path) == (
        (), {"futures": "CHECKPOINT_UNREADABLE", "financial": "CHECKPOINT_UNREADABLE"},
    )
    monkeypatch.setattr("market_monitor.futures_bulk.run_bulk_futures",
                        lambda *_args, **_kwargs: pytest.fail("must not read files"))
    monkeypatch.setattr("market_monitor.tdx_local.run_tdx_local_import",
                        lambda *_args, **_kwargs: pytest.fail("must not read files"))
    result = run_local_terminal_imports(tmp_path)
    assert result["selectedTerminals"] == []
    assert result["status"] == "PARTIAL_FAILURE"
