"""Ordinary market windows and local terminal file-change scheduling."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_monitor.market_auto_refresh import (
    due_local_market_updates, due_market_update, due_tdx_import, market_target,
    tdx_sources_changed,
)
from market_monitor.operations import Operation, OperationKind, OperationStatus
from market_monitor.tdx_local import _state_path


def _job(kind: OperationKind, status: OperationStatus, created: str,
         *, completed: str | None = None, detail: str | None = None) -> Operation:
    return Operation("test-operation", kind, status, created, completed_at=completed, detail=detail)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    data_root = tmp_path / "data"
    financial = tmp_path / "financial"
    futures = tmp_path / "futures"
    stock = financial / "vipdoc" / "sh" / "lday" / "sh600000.day"
    contract = futures / "vipdoc" / "ds" / "lday" / "29#M2601.day"
    (financial / "vipdoc" / "ds").mkdir(parents=True)
    stock.parent.mkdir(parents=True)
    contract.parent.mkdir(parents=True)
    stock.write_bytes(b"s" * 32)
    contract.write_bytes(b"f" * 32)
    monkeypatch.setenv("TDX_ROOT", str(financial))
    monkeypatch.setenv("TDX_FUTURES_ROOT", str(futures))
    _write_json(_state_path(data_root, financial, start_date=None, end_date=None), {
        "normalization_version": "tdx-cn-v2",
        "files": {
            "vipdoc/sh/lday/sh600000.day": {
                "size": stock.stat().st_size, "mtime_ns": stock.stat().st_mtime_ns,
                "normalization_version": "tdx-cn-v2",
            },
        },
    })
    _write_json(data_root / "state" / "futures_import.json", {
        "files": {
            "vipdoc/ds/lday/29#M2601.day": {
                "size": contract.stat().st_size, "mtime_ns": contract.stat().st_mtime_ns,
            },
        },
    })
    _write_json(data_root / "state" / "tdx_import_workflow.json", {"status": "PASS"})
    return data_root, stock, contract


def test_market_window_deduplicates_after_close_and_retries_from_completion() -> None:
    before = datetime(2026, 10, 21, 12, 29, tzinfo=timezone.utc)
    after = datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)
    assert market_target(before)[0] == "2026-10-20"
    assert market_target(after)[0] == "2026-10-21"
    yesterday = _job(OperationKind.MARKET_UPDATE, OperationStatus.PASS,
                     "2026-10-20T12:35:00+00:00")
    assert not due_market_update([yesterday], before)
    assert due_market_update([yesterday], after)
    today = _job(OperationKind.MARKET_UPDATE, OperationStatus.PASS,
                 "2026-10-21T12:31:00+00:00")
    assert not due_market_update([today], after)
    today.status = OperationStatus.PARTIAL_FAILURE
    today.completed_at = "2026-10-21T17:59:00+00:00"
    assert not due_market_update([today], datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc))
    assert due_market_update([today], datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc))


def test_market_active_and_interrupted_jobs_respect_queue_state() -> None:
    now = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)
    active = _job(OperationKind.MARKET_UPDATE, OperationStatus.QUEUED,
                  "2026-10-21T12:31:00+00:00")
    assert not due_market_update([active], now)
    interrupted = _job(
        OperationKind.MARKET_UPDATE, OperationStatus.FAILED,
        "2026-10-21T12:31:00+00:00", completed="2026-10-21T12:59:00+00:00",
        detail="process stopped before operation completed",
    )
    assert due_market_update([interrupted], now)


@pytest.mark.parametrize("bad_time", ["invalid", "2026-10-21T12:31:00", ""])
def test_market_invalid_history_does_not_block_local_import(tmp_path: Path, monkeypatch, bad_time: str) -> None:
    from market_monitor import market_auto_refresh
    monkeypatch.setattr(market_auto_refresh, "due_tdx_import", lambda *_args: True)
    broken = _job(OperationKind.MARKET_UPDATE, OperationStatus.FAILED, bad_time)
    now = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)
    assert not due_market_update([broken], now)
    assert due_local_market_updates(tmp_path, [broken], now) == [OperationKind.TDX_LOCAL_IMPORT]


def test_market_invalid_completion_time_does_not_guess_retry() -> None:
    failed = _job(OperationKind.MARKET_UPDATE, OperationStatus.PARTIAL_FAILURE,
                  "2026-10-21T12:31:00+00:00", completed="2026-10-21T13:00:00")
    assert not due_market_update([failed], datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc))


def test_market_latest_attempt_uses_actual_time_across_offsets() -> None:
    older = _job(OperationKind.MARKET_UPDATE, OperationStatus.PASS, "2026-10-21T21:00:00+08:00")
    newer = _job(OperationKind.MARKET_UPDATE, OperationStatus.FAILED,
                 "2026-10-21T14:00:00+00:00", completed="2026-10-21T14:01:00+00:00")
    assert not due_market_update([older, newer], datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc))
    assert due_market_update([older, newer], datetime(2026, 10, 21, 20, 1, tzinfo=timezone.utc))


def test_tdx_scan_detects_same_day_new_and_modified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, stock, contract = _sources(tmp_path, monkeypatch)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    completed = _job(OperationKind.TDX_LOCAL_IMPORT, OperationStatus.PASS,
                     "2026-10-21T13:30:00+00:00", completed="2026-10-21T13:31:00+00:00")
    assert not tdx_sources_changed(data_root)
    assert not due_tdx_import(data_root, [completed], now)

    new_stock = stock.with_name("sh000001.day")
    new_stock.write_bytes(b"n" * 32)
    assert tdx_sources_changed(data_root)
    assert due_tdx_import(data_root, [completed], now)

    new_stock.unlink()
    contract.write_bytes(b"f" * 64)
    assert tdx_sources_changed(data_root)
    assert due_local_market_updates(data_root, [completed], now)[0] == OperationKind.TDX_LOCAL_IMPORT


def test_tdx_interrupted_workflow_resumes_and_partial_waits_from_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _stock, _contract = _sources(tmp_path, monkeypatch)
    _write_json(data_root / "state" / "tdx_import_workflow.json", {"status": "RUNNING"})
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    interrupted = _job(
        OperationKind.TDX_LOCAL_IMPORT, OperationStatus.FAILED,
        "2026-10-21T13:30:00+00:00", completed="2026-10-21T13:59:00+00:00",
        detail="process stopped before operation completed",
    )
    assert due_tdx_import(data_root, [interrupted], now)
    partial = _job(
        OperationKind.TDX_LOCAL_IMPORT, OperationStatus.PARTIAL_FAILURE,
        "2026-10-21T07:00:00+00:00", completed="2026-10-21T13:59:00+00:00",
    )
    assert not due_tdx_import(data_root, [partial], now)
    assert due_tdx_import(data_root, [partial], now + timedelta(hours=6, minutes=1))


def test_stale_partial_source_mismatch_does_not_repeat_without_new_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, stock, contract = _sources(tmp_path, monkeypatch)
    checkpoint_path = _state_path(data_root, stock.parents[3], start_date=None, end_date=None)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["files"]["vipdoc/sh/lday/sh600000.day"]["size"] = 1
    _write_json(checkpoint_path, checkpoint)
    old_ns = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    os.utime(stock, ns=(old_ns, old_ns))
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    partial = _job(
        OperationKind.TDX_LOCAL_IMPORT, OperationStatus.PARTIAL_FAILURE,
        "2026-10-21T13:30:00+00:00", completed="2026-10-21T13:59:00+00:00",
    )
    _write_json(data_root / "state" / "tdx_import_workflow.json", {"status": "PARTIAL_FAILURE"})
    assert tdx_sources_changed(data_root)
    assert not due_tdx_import(data_root, [partial], now + timedelta(hours=7))

    contract.write_bytes(b"f" * 64)
    fresh_ns = int(datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    os.utime(contract, ns=(fresh_ns, fresh_ns))
    assert due_tdx_import(data_root, [partial], now + timedelta(hours=7))


def test_tdx_import_waits_while_financial_ui_download_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, stock, _contract = _sources(tmp_path, monkeypatch)
    stock.write_bytes(b"changed" * 32)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    running = _job(OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD, OperationStatus.RUNNING,
                   "2026-10-21T13:30:00+00:00")
    assert not due_tdx_import(data_root, [running], now)
    futures_running = _job(OperationKind.TDX_FUTURES_AUTO_DOWNLOAD, OperationStatus.RUNNING,
                           "2026-10-21T13:30:00+00:00")
    assert not due_tdx_import(data_root, [futures_running], now)
    checkpoint_path = data_root / "state" / "tdx_download_ui" / "checkpoints.json"
    _write_json(checkpoint_path, {"financial": {
        "extended-day": {"status": "RUNNING_UNKNOWN", "slot": "2026-10-20T23:00:00+08:00"},
    }})
    assert not due_tdx_import(data_root, [], now)
    _write_json(checkpoint_path, {"futures": {
        "future-5m": {"status": "RUNNING_UNKNOWN", "slot": "2026-10-20T23:00:00+08:00"},
    }})
    assert due_tdx_import(data_root, [], now)  # Financial files changed and are safe to import.


def test_blocked_futures_file_changes_do_not_trigger_repeated_financial_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, stock, contract = _sources(tmp_path, monkeypatch)
    _write_json(data_root / "state" / "tdx_download_ui" / "checkpoints.json", {"futures": {
        "future-5m": {"status": "RUNNING_UNKNOWN"},
    }})
    contract.write_bytes(b"changed-futures" * 32)
    now = datetime(2026, 10, 21, 14, tzinfo=timezone.utc)
    assert not due_tdx_import(data_root, [], now)
    assert not tdx_sources_changed(data_root, selected_terminals=("financial",))
    stock.write_bytes(b"changed-financial" * 32)
    assert due_tdx_import(data_root, [], now)
    _write_json(data_root / "state" / "tdx_import_workflow.json", {
        "status": "PARTIAL_FAILURE", "selectedStatus": "PASS",
        "blockedTerminals": {"futures": "RUNNING_UNKNOWN"},
    })
    recent = _job(OperationKind.TDX_LOCAL_IMPORT, OperationStatus.PARTIAL_FAILURE,
                  "2026-10-21T13:58:00+00:00", completed="2026-10-21T13:59:00+00:00")
    assert due_tdx_import(data_root, [recent], now)
