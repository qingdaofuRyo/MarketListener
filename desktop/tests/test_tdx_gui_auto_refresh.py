"""Financial-terminal UI downloads are due once per 23:00 Beijing slot."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_monitor.operations import Operation, OperationKind, OperationStatus
from market_monitor.tdx_download_ui import FINANCIAL, FUTURES
from market_monitor.tdx_gui_auto_refresh import due_financial_download, due_futures_download
from market_monitor.tdx_gui_auto_refresh import batch_start_dates
from market_monitor.tdx_gui_auto_refresh import run_financial_download_worker
from market_monitor.tdx_gui_auto_refresh import run_futures_download_worker
from market_monitor import tdx_gui_auto_refresh
from market_monitor.tdx_gui_auto_refresh import financial_minute_start
from market_monitor.futures_calendar import sync_futures_trading_calendar


def _job(status: OperationStatus, created: datetime, *, completed: datetime | None = None) -> Operation:
    return Operation("fixture", OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD, status,
                     created.isoformat(), completed_at=completed.isoformat() if completed else None)


def _checkpoint(root: Path, status: str, slot: str) -> None:
    path = root / "state" / "tdx_download_ui" / "checkpoints.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"financial": {
        batch.key: {"slot": slot, "status": status} for batch in FINANCIAL
    }}), encoding="utf-8")


def test_due_download_waits_for_slot_and_deduplicates_completed_ui(tmp_path: Path) -> None:
    before = datetime(2026, 10, 21, 14, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
    assert due_financial_download(tmp_path, [], before)
    _checkpoint(tmp_path, "UI_DONE_UNVERIFIED", "2026-10-20T23:00:00+08:00")
    assert not due_financial_download(tmp_path, [], before)
    assert due_financial_download(tmp_path, [], after)


def test_due_download_retries_login_failure_from_completion_and_never_replays_uncertain_ui(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    old = _job(OperationStatus.PARTIAL_FAILURE, now - timedelta(hours=2),
               completed=now - timedelta(minutes=2))
    assert not due_financial_download(tmp_path, [old], now)
    assert due_financial_download(tmp_path, [old], now + timedelta(minutes=31))
    _checkpoint(tmp_path, "RUNNING_UNKNOWN", "2026-10-20T23:00:00+08:00")
    assert not due_financial_download(tmp_path, [old], now + timedelta(hours=2))


def test_host_initializing_retries_after_ten_minutes_not_immediately(tmp_path: Path) -> None:
    completed = datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc)
    operation = Operation(
        "host-initializing", OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
        OperationStatus.PARTIAL_FAILURE,
        (completed - timedelta(minutes=2)).isoformat(), completed_at=completed.isoformat(),
        detail="PARTIAL_FAILURE: 原因=MARKET_HOST_INITIALIZING；保留窗口",
    )
    assert not due_financial_download(tmp_path, [operation], completed + timedelta(minutes=9))
    assert due_financial_download(tmp_path, [operation], completed + timedelta(minutes=10))


def test_futures_disconnected_host_gets_short_retry_but_other_failures_wait(tmp_path: Path) -> None:
    completed = datetime(2026, 10, 27, 15, 0, 20, tzinfo=timezone.utc)
    operation = Operation(
        "host-disconnected", OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
        OperationStatus.PARTIAL_FAILURE,
        (completed - timedelta(seconds=18)).isoformat(), completed_at=completed.isoformat(),
        detail="PARTIAL_FAILURE: 原因=FUTURES_MARKET_HOST_DISCONNECTED；保留窗口",
    )
    assert not due_futures_download(tmp_path, [operation], completed + timedelta(minutes=9))
    assert due_futures_download(tmp_path, [operation], completed + timedelta(minutes=10))
    operation.detail = "PARTIAL_FAILURE: 原因=TERMINAL_DOWNLOAD_CANCELLED"
    assert not due_futures_download(tmp_path, [operation], completed + timedelta(minutes=10))
    assert due_futures_download(tmp_path, [operation], completed + timedelta(minutes=30))


def test_due_download_skips_active_or_cancelled_operation(tmp_path: Path) -> None:
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    assert not due_financial_download(tmp_path, [_job(OperationStatus.RUNNING, now)], now)
    assert not due_financial_download(tmp_path, [_job(OperationStatus.CANCELLED, now)], now)


def test_due_futures_download_uses_independent_two_batch_checkpoint(tmp_path: Path) -> None:
    now = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
    assert due_futures_download(tmp_path, [], now)
    path = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"futures": {
        batch.key: {"slot": "2026-10-21T23:00:00+08:00", "status": "UI_DONE_UNVERIFIED"}
        for batch in FUTURES
    }}), encoding="utf-8")
    assert not due_futures_download(tmp_path, [], now)
    assert due_futures_download(tmp_path, [], now + timedelta(days=1))


def test_missed_23_slot_is_due_at_00_30_and_after_three_days_at_18_50(tmp_path: Path) -> None:
    tz = timezone(timedelta(hours=8))
    after_midnight = datetime(2026, 10, 27, 0, 30, tzinfo=tz)
    assert tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], after_midnight) == {
        "due": True, "reason": "NEW_SLOT", "slot": "2026-10-26T23:00:00+08:00",
    }
    financial = tdx_gui_auto_refresh.financial_download_decision(tmp_path, [], after_midnight)
    assert financial["due"] is True and financial["slot"] == "2026-10-26T23:00:00+08:00"
    assert financial["skipBatches"] == {}

    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({
        "financial": {batch.key: {"slot": "2026-10-26T23:00:00+08:00",
                                  "status": "UI_DONE_UNVERIFIED"} for batch in FINANCIAL},
        "futures": {batch.key: {"slot": "2026-10-26T23:00:00+08:00",
                                "status": "UI_DONE_UNVERIFIED"} for batch in FUTURES},
    }), encoding="utf-8")
    assert not due_financial_download(tmp_path, [], after_midnight)
    assert not due_futures_download(tmp_path, [], after_midnight)

    returned = datetime(2026, 10, 29, 18, 50, tzinfo=tz)
    assert tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], returned)["due"] is True
    assert tdx_gui_auto_refresh.financial_download_decision(tmp_path, [], returned)["due"] is True
    assert batch_start_dates(tmp_path, "financial", returned)["cn-day"] == date(2026, 10, 26)
    assert batch_start_dates(tmp_path, "futures", returned)["future-day"] == date(2026, 10, 26)


def test_futures_weekend_skip_requires_raw_coverage_and_reports_import_pending(
    tmp_path: Path,
) -> None:
    sync_futures_trading_calendar(
        tmp_path, fetcher=lambda: [("2026-10-22",), ("2026-10-23",), ("2026-10-26",)],
    )
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"futures": {
        batch.key: {"slot": "2026-10-24T23:00:00+08:00", "status": "UI_DONE_UNVERIFIED"}
        for batch in FUTURES
    }}), encoding="utf-8")
    groups = []
    for prefix in ("28", "29", "30", "47", "66"):
        for period in ("1d", "5m"):
            day = "2026-10-26" if period == "5m" and prefix in {"28", "29", "30"} else "2026-10-23"
            groups.append(_group(f"ds.{prefix}.{period}", day, imported=day, imported_stale=True))
    scan = datetime(2026, 10, 24, 13, tzinfo=timezone(timedelta(hours=8)))
    _local_status(tmp_path, scan, futures=groups, inventoryAvailable=False)
    sunday = datetime(2026, 10, 25, 23, tzinfo=timezone(timedelta(hours=8)))
    assert tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], sunday) == {
        "due": False, "reason": "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED", "slot": "2026-10-25T23:00:00+08:00",
    }
    assert not due_futures_download(tmp_path, [], sunday)
    for item in groups:
        item["importedRangeStale"] = False
    _local_status(tmp_path, scan, futures=groups)
    # A group's importedLatestDate is a maximum, never proof of every contract.
    assert (tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], sunday)["reason"]
            == "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED")
    assert due_futures_download(tmp_path, [], sunday - timedelta(days=1)) is False
    next(item for item in groups if item["key"] == "ds.29.5m")["commonLatestDate"] = "2026-10-23"
    _local_status(tmp_path, scan, futures=groups)
    assert due_futures_download(tmp_path, [], sunday)
    checkpoint.unlink()
    _local_status(tmp_path, scan, futures=[
        _group(f"ds.{prefix}.{period}",
               "2026-10-26" if period == "5m" and prefix in {"28", "29", "30"} else "2026-10-23")
        for prefix in ("28", "29", "30", "47", "66") for period in ("1d", "5m")
    ])
    assert due_futures_download(tmp_path, [], sunday)


def test_weekend_after_friday_complete_skips_saturday_but_monday_close_is_new_data(
    tmp_path: Path,
) -> None:
    tz = timezone(timedelta(hours=8))
    sync_futures_trading_calendar(
        tmp_path, fetcher=lambda: [("2026-10-22",), ("2026-10-23",), ("2026-10-26",)],
    )
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"futures": {
        batch.key: {"slot": "2026-10-23T23:00:00+08:00", "status": "UI_DONE_UNVERIFIED"}
        for batch in FUTURES
    }}), encoding="utf-8")
    groups = [_group(f"ds.{prefix}.{period}",
                     "2026-10-26" if prefix in {"28", "29", "30"} and period == "5m"
                     else "2026-10-23")
              for prefix in ("28", "29", "30", "47", "66") for period in ("1d", "5m")]
    _local_status(tmp_path, datetime(2026, 10, 24, 13, tzinfo=tz), futures=groups)
    for now in (datetime(2026, 10, 24, 23, tzinfo=tz),
                datetime(2026, 10, 25, 0, 30, tzinfo=tz),
                datetime(2026, 10, 25, 23, tzinfo=tz)):
        assert tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], now)["reason"] == (
            "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED"
        )
    # Monday's day session is already closed by 18:50. A weekend snapshot may
    # not suppress the catch-up run merely because latest_slot is still Sunday.
    monday = datetime(2026, 10, 26, 18, 50, tzinfo=tz)
    assert tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], monday)["due"] is True


def test_weekend_never_overrides_uncertain_futures_checkpoint(tmp_path: Path, monkeypatch) -> None:
    slot = "2026-10-25T23:00:00+08:00"
    path = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"futures": {
        FUTURES[0].key: {"slot": slot, "status": "RUNNING_UNKNOWN"},
    }}), encoding="utf-8")
    monkeypatch.setattr(tdx_gui_auto_refresh, "_futures_weekend_coverage",
                        lambda *_args: "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED")
    now = datetime.fromisoformat(slot)
    assert tdx_gui_auto_refresh.futures_download_decision(tmp_path, [], now)["reason"] == "RECONCILIATION_REQUIRED"
    assert not due_futures_download(tmp_path, [], now)


def test_weekend_without_verified_next_futures_day_does_not_claim_raw_recent(tmp_path: Path) -> None:
    sunday = datetime(2026, 10, 25, 23, tzinfo=timezone(timedelta(hours=8)))
    assert tdx_gui_auto_refresh._futures_weekend_coverage(tmp_path, sunday.date()) is None
    sync_futures_trading_calendar(tmp_path, fetcher=lambda: [("2026-10-22",), ("2026-10-23",)])
    assert tdx_gui_auto_refresh._futures_weekend_coverage(tmp_path, sunday.date()) is None


def test_interrupted_batch_retry_keeps_previous_start_date(tmp_path: Path) -> None:
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"financial": {"extended-day": {
        "slot": "2026-10-24T23:00:00+08:00", "status": "INTERRUPTED_UNVERIFIED",
        "startDate": "2026-10-21",
    }}}), encoding="utf-8")
    monday = datetime(2026, 10, 26, 23, tzinfo=timezone(timedelta(hours=8)))
    starts = batch_start_dates(tmp_path, "financial", monday, selected_keys={"extended-day"})
    assert starts == {"extended-day": date(2026, 10, 21)}


def test_unknown_gui_recovery_waits_for_backoff_then_allows_late_start(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import tdx_unknown_recovery

    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"financial": {"extended-day": {
        "slot": "2026-10-24T23:00:00+08:00", "status": "RUNNING_UNKNOWN",
    }}}), encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(tdx_unknown_recovery, "reconcile_unknown_download",
                        lambda _root, terminal, _history: (seen.append(terminal) or
                                                            {"status": "INTERRUPTED_UNVERIFIED"}))
    tz = timezone(timedelta(hours=8))
    assert tdx_gui_auto_refresh.reconcile_due_unknown_downloads(
        tmp_path, [], datetime(2026, 10, 25, 23, 20, tzinfo=tz)) == {}
    assert tdx_gui_auto_refresh.reconcile_due_unknown_downloads(
        tmp_path, [], datetime(2026, 10, 25, 23, 31, tzinfo=tz)) == {
            "financial": "INTERRUPTED_UNVERIFIED",
        }
    assert tdx_gui_auto_refresh.reconcile_due_unknown_downloads(
        tmp_path, [], datetime(2026, 10, 26, 18, 50, tzinfo=tz)) == {
            "financial": "INTERRUPTED_UNVERIFIED",
        }
    assert seen == ["financial", "financial"]


def test_financial_weekend_skips_current_cn_batches_but_keeps_extended_due(tmp_path: Path) -> None:
    scan = datetime(2026, 10, 24, 13, tzinfo=timezone(timedelta(hours=8)))
    groups = [_group(f"{market}.STOCK.{period}", "2026-10-23", imported="2026-10-23",
                     imported_stale=True)
              for market in ("sh", "sz", "bj") for period in ("1d", "5m")]
    groups.extend(_group(f"{market}.B_SHARE.{period}", "2026-10-23", imported="2026-10-23",
                         imported_stale=True)
                  for market in ("sh", "sz") for period in ("1d", "5m"))
    _local_status(tmp_path, scan, financial=groups, inventoryAvailable=False)
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"financial": {
        batch.key: {"slot": "2026-10-24T23:00:00+08:00", "status": "UI_DONE_UNVERIFIED"}
        for batch in FINANCIAL if batch.key.startswith("cn-")
    }}), encoding="utf-8")
    sunday = datetime(2026, 10, 25, 23, tzinfo=timezone(timedelta(hours=8)))
    decision = tdx_gui_auto_refresh.financial_download_decision(tmp_path, [], sunday)
    assert decision["due"] is True
    assert decision["skipBatches"] == {
        "cn-day": "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED",
        "cn-5m": "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED",
    }
    assert (tdx_gui_auto_refresh.financial_cn_coverage(tmp_path, sunday.date(), sunday)
            == "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED")
    next(item for item in groups if item["key"] == "sz.B_SHARE.5m")["commonLatestDate"] = "2026-10-22"
    _local_status(tmp_path, scan, financial=groups, inventoryAvailable=False)
    assert tdx_gui_auto_refresh.financial_cn_coverage(tmp_path, sunday.date(), sunday) is None
    next(item for item in groups if item["key"] == "sz.B_SHARE.5m")["commonLatestDate"] = "2026-10-23"
    _local_status(tmp_path, scan, financial=groups, inventoryAvailable=False)
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    saved["financial"].update({
        batch.key: {"slot": sunday.isoformat(), "status": "UI_DONE_UNVERIFIED"}
        for batch in FINANCIAL if batch.key.startswith("extended-")
    })
    checkpoint.write_text(json.dumps(saved), encoding="utf-8")
    assert not due_financial_download(tmp_path, [], sunday)


def test_financial_monday_close_invalidates_weekend_skip_but_late_completion_avoids_cn_replay(
    tmp_path: Path,
) -> None:
    tz = timezone(timedelta(hours=8))
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"financial": {
        batch.key: {"slot": "2026-10-23T23:00:00+08:00", "status": "UI_DONE_UNVERIFIED"}
        for batch in FINANCIAL if batch.key.startswith("cn-")
    }}), encoding="utf-8")
    friday_groups = [_group(f"{market}.STOCK.{period}", "2026-10-23")
                     for market in ("sh", "sz", "bj") for period in ("1d", "5m")]
    friday_groups.extend(_group(f"{market}.B_SHARE.{period}", "2026-10-23")
                         for market in ("sh", "sz") for period in ("1d", "5m"))
    _local_status(tmp_path, datetime(2026, 10, 24, 13, tzinfo=tz), financial=friday_groups)
    sunday = datetime(2026, 10, 25, 23, tzinfo=tz)
    assert tdx_gui_auto_refresh.financial_download_decision(tmp_path, [], sunday)["skipBatches"]
    monday_early = datetime(2026, 10, 26, 18, 50, tzinfo=tz)
    early_decision = tdx_gui_auto_refresh.financial_download_decision(tmp_path, [], monday_early)
    assert early_decision["due"] is True and early_decision["skipBatches"] == {}

    prior = json.loads(checkpoint.read_text(encoding="utf-8"))
    for item in prior["financial"].values():
        item.update(slot="2026-10-25T23:00:00+08:00",
                    finishedAt="2026-10-26T18:55:00+08:00")
    checkpoint.write_text(json.dumps(prior), encoding="utf-8")
    monday_groups = [_group(item["key"], "2026-10-26") for item in friday_groups]
    _local_status(tmp_path, datetime(2026, 10, 26, 20, tzinfo=tz), financial=monday_groups)
    monday_night = datetime(2026, 10, 26, 23, tzinfo=tz)
    night_decision = tdx_gui_auto_refresh.financial_download_decision(tmp_path, [], monday_night)
    assert night_decision["due"] is True
    assert set(night_decision["skipBatches"]) == {"cn-day", "cn-5m"}


def test_financial_worker_passes_skip_batch_and_owned_pid_without_restarting_cn(
    tmp_path: Path, monkeypatch,
) -> None:
    python = tmp_path / "tools" / "python311-x86" / "python.exe"
    python.parent.mkdir(parents=True)
    executable = bytearray(0x90)
    executable[:2] = b"MZ"
    executable[0x3C:0x40] = (0x80).to_bytes(4, "little")
    executable[0x80:0x86] = b"PE\x00\x00\x4c\x01"
    python.write_bytes(executable)
    commands: list[list[str]] = []
    monkeypatch.setattr(tdx_gui_auto_refresh, "financial_cn_coverage",
                        lambda *_args: "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED")
    def starts(_root, _terminal, _now, *, selected_keys):
        assert selected_keys == {"extended-day", "extended-5m"}
        return {key: date(2026, 10, 23) for key in selected_keys}
    monkeypatch.setattr(tdx_gui_auto_refresh, "batch_start_dates", starts)
    monkeypatch.setattr(tdx_gui_auto_refresh.subprocess, "run", lambda command, **_kwargs:
                        (commands.append(command) or type("Result", (), {"returncode": 0})()))
    assert run_financial_download_worker(
        tmp_path, owned_pid=4242, owned_token="windows-filetime:synthetic") == (0, None)
    command = commands[0]
    assert command[command.index("--owned-pid") + 1] == "4242"
    assert command[command.index("--owned-token") + 1] == "windows-filetime:synthetic"
    assert {command[index + 1] for index, item in enumerate(command) if item == "--skip-batch"} == {
        "cn-day:RAW_MAJORITY_RECENT_SILVER_UNVERIFIED",
        "cn-5m:RAW_MAJORITY_RECENT_SILVER_UNVERIFIED",
    }
    assert {command[index + 1].split(":", 1)[0]
            for index, item in enumerate(command) if item == "--batch-start"} == {
                "extended-day", "extended-5m",
            }


def test_download_worker_never_falls_back_to_64bit_runtime(tmp_path: Path) -> None:
    assert run_financial_download_worker(tmp_path) == (None, "TDX_32BIT_RUNTIME_MISSING")


def test_download_worker_uses_verified_x86_python_and_saved_login(
    tmp_path: Path, monkeypatch,
) -> None:
    python = tmp_path / "tools" / "python311-x86" / "python.exe"
    python.parent.mkdir(parents=True)
    executable = bytearray(0x90)
    executable[:2] = b"MZ"
    executable[0x3C:0x40] = (0x80).to_bytes(4, "little")
    executable[0x80:0x86] = b"PE\x00\x00\x4c\x01"
    python.write_bytes(executable)
    called: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        called.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(tdx_gui_auto_refresh.subprocess, "run", run)
    monkeypatch.setattr(tdx_gui_auto_refresh, "batch_start_dates", lambda *_args, **_kwargs: {
        batch.key: date(2026, 10, 20) for batch in FINANCIAL
    })
    assert run_financial_download_worker(tmp_path) == (0, None)
    assert called[0][0] == str(python)
    assert called[0][called[0].index("--terminal") + 1] == "financial"
    assert "--auto-login-saved" in called[0]
    assert [called[0][index + 1] for index, value in enumerate(called[0])
            if value == "--batch-start"] == [f"{batch.key}:2026-10-20" for batch in FINANCIAL]
    assert "--extended-minute-start" not in called[0]


def _local_status(root: Path, now: datetime, *, financial: list[dict] | None = None,
                  futures: list[dict] | None = None, **flags: object) -> None:
    path = root / "state" / "tdx_local_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checkedAt": now.isoformat(), "inventoryAvailable": True,
               "inventoryStale": False, "roots": [
                   {"terminal": "financial", "available": True, "groups": financial or []},
                   {"terminal": "futures", "available": True, "groups": futures or []},
               ]}
    payload.update(flags)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _group(key: str, day: str, *, count: int = 20, common: int = 20,
           imported: str | None = None, imported_stale: bool = False) -> dict:
    return {"key": key, "fileCount": count, "commonLatestCount": common,
            "commonLatestDate": day, "importedLatestDate": imported,
            "importedRangeStale": imported_stale,
            "rawStatus": "ENDPOINTS_ONLY", "invalidFileCount": 0}


def test_batch_starts_are_per_scope_and_overlap_checkpoint_or_lagging_inventory(tmp_path: Path) -> None:
    now = datetime(2026, 10, 23, 2, tzinfo=timezone(timedelta(hours=8)))
    checkpoint = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"financial": {
        "cn-day": {"status": "UI_DONE_UNVERIFIED", "slot": "2026-10-21T23:00:00+08:00"},
        "cn-5m": {"status": "UI_DONE_UNVERIFIED", "slot": "2026-10-22T23:00:00+08:00"},
        "extended-day": {"status": "UI_DONE_UNVERIFIED", "slot": "2026-10-20T23:00:00+08:00"},
        "extended-5m": {"status": "UI_DONE_UNVERIFIED", "slot": "2026-10-22T23:00:00+08:00"},
    }, "futures": {
        "future-day": {"status": "UI_DONE_UNVERIFIED", "slot": "2026-10-22T23:00:00+08:00"},
        "future-5m": {"status": "UI_DONE_UNVERIFIED", "slot": "2026-10-20T23:00:00+08:00"},
    }}), encoding="utf-8")
    _local_status(tmp_path, now, financial=[
        _group("sh.STOCK.1d", "2026-10-22"),
        _group("sz.STOCK.1d", "2026-10-19"),
        _group("bj.STOCK.1d", "2026-10-22"),
        _group("sh.STOCK.5m", "2026-10-20"),
        _group("ds.31.1d", "2026-10-22"),
        _group("ds.102.1d", "2026-10-18"),
        _group("ds.31.5m", "2026-10-21"),
        _group("ds.16.1d", "2026-09-01", count=9),
        _group("ds.17.5m", "2026-09-01", count=20, common=9),
    ], futures=[
        _group("ds.28.1d", "2026-10-21"),
        _group("ds.29.1d", "2026-10-19"),
        _group("ds.47.5m", "2026-10-18"),
        _group("ds.68.1d", "2026-08-01"),
        _group("ds.30.5m", "2026-08-01", count=20, common=9),
    ])
    assert batch_start_dates(tmp_path, "financial", now) == {
        "cn-day": date(2026, 10, 19), "cn-5m": date(2026, 10, 20),
        "extended-day": date(2026, 10, 18), "extended-5m": date(2026, 10, 21),
    }
    assert batch_start_dates(tmp_path, "futures", now) == {
        "future-day": date(2026, 10, 19), "future-5m": date(2026, 10, 18),
    }
    assert financial_minute_start(tmp_path, now) == date(2026, 10, 21)


def test_batch_starts_use_bounded_fallback_without_trusted_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 10, 23, 2, tzinfo=timezone(timedelta(hours=8)))
    assert batch_start_dates(tmp_path, "financial", now) == {
        batch.key: date(2026, 9, 21) for batch in FINANCIAL
    }
    _local_status(tmp_path, now - timedelta(days=1),
                  financial=[_group("sh.STOCK.1d", "2026-09-01")])
    assert batch_start_dates(tmp_path, "financial", now)["cn-day"] == date(2026, 9, 21)


@pytest.mark.parametrize("flags", [
    {"inventoryAvailable": False, "inventoryStale": False},
    {"inventoryAvailable": True, "inventoryStale": True},
])
def test_batch_starts_use_raw_endpoints_when_silver_inventory_unavailable(tmp_path: Path, flags: dict) -> None:
    now = datetime(2026, 10, 30, 20, tzinfo=timezone(timedelta(hours=8)))
    _local_status(tmp_path, now, financial=[
        _group("ds.31.1d", "2026-10-28", imported="2026-10-01"),
        _group("ds.31.5m", "2026-10-28", imported="2026-10-01"),
    ], **flags)
    starts = batch_start_dates(tmp_path, "financial", now)
    assert starts["extended-day"] == date(2026, 10, 28)
    assert starts["extended-5m"] == date(2026, 10, 28)


def test_batch_starts_do_not_use_unavailable_terminal_raw_endpoints(tmp_path: Path) -> None:
    now = datetime(2026, 10, 30, 20, tzinfo=timezone(timedelta(hours=8)))
    _local_status(tmp_path, now, financial=[_group("ds.31.1d", "2026-10-28")])
    path = tmp_path / "state" / "tdx_local_status.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["roots"][0]["available"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert batch_start_dates(tmp_path, "financial", now)["extended-day"] == date(2026, 9, 28)


def test_batch_starts_reject_old_evidence_and_clamp_future_days(tmp_path: Path) -> None:
    now = datetime(2026, 10, 23, 2, tzinfo=timezone(timedelta(hours=8)))
    _local_status(tmp_path, now, futures=[_group("ds.29.1d", "2026-09-01")])
    with pytest.raises(ValueError, match="explicit backfill"):
        batch_start_dates(tmp_path, "futures", now)
    _local_status(tmp_path, now, futures=[_group("ds.29.1d", "2026-10-29")])
    assert batch_start_dates(tmp_path, "futures", now)["future-day"] == date(2026, 10, 22)


def test_batch_starts_reject_old_checkpoint_even_when_inventory_is_current(tmp_path: Path) -> None:
    now = datetime(2026, 10, 23, 2, tzinfo=timezone(timedelta(hours=8)))
    path = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"financial": {"extended-5m": {
        "status": "UI_DONE_UNVERIFIED", "slot": "2026-09-01T23:00:00+08:00",
    }}}), encoding="utf-8")
    _local_status(tmp_path, now, financial=[_group("ds.31.5m", "2026-10-22")])
    with pytest.raises(ValueError, match="extended-5m requires explicit backfill"):
        batch_start_dates(tmp_path, "financial", now)


def test_batch_starts_use_lagging_committed_silver_only_as_earlier_bound(tmp_path: Path) -> None:
    now = datetime(2026, 10, 23, 2, tzinfo=timezone(timedelta(hours=8)))
    _local_status(tmp_path, now, financial=[
        _group("sh.STOCK.1d", "2026-10-22", imported="2026-10-20"),
        _group("sz.STOCK.1d", "2026-10-22", imported="2026-10-21"),
        _group("sh.STOCK.5m", "2026-10-22", imported="2026-10-01", imported_stale=True),
    ])
    starts = batch_start_dates(tmp_path, "financial", now)
    assert starts["cn-day"] == date(2026, 10, 20)
    assert starts["cn-5m"] == date(2026, 10, 22)


def test_futures_worker_uses_current_64bit_python_and_exact_terminal(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(tdx_gui_auto_refresh.subprocess, "run", run)
    assert run_futures_download_worker(tmp_path) == (0, None)
    assert commands[0][commands[0].index("--terminal") + 1] == "futures"
    assert "--auto-login-saved" not in commands[0]
    assert len([item for item in commands[0] if item == "--batch-start"]) == 2
