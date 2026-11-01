"""Restart-gap repair uses synthetic calendars and an isolated temporary Catalog."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from market_monitor import futures_member_rank_update as update
from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_RELATIVE_PATH, sync_futures_trading_calendar
from market_monitor.storage import MarketStore


DAYS = ("2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30")
VENUES = ("CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX")


@pytest.fixture(autouse=True)
def no_user_credentials(monkeypatch):
    monkeypatch.setattr("market_monitor.dce_credentials.load_accounts", lambda: [])


def test_configured_dce_api_uses_two_accounts_in_parent_writer(tmp_path, monkeypatch):
    accounts = [{"api_key": "synthetic-A", "secret": "synthetic-secret-A"},
                {"api_key": "synthetic-B", "secret": "synthetic-secret-B"}]
    monkeypatch.setattr("market_monitor.dce_credentials.load_accounts", lambda: accounts)
    monkeypatch.setattr(update.subprocess, "run", lambda *args, **kwargs: pytest.fail("must use authorized API"))
    capture = update._run_capture(project_root=tmp_path, data_root=tmp_path, source="DCE", trading_day=DAYS[-1])
    assert capture["captureStatus"] == "AUTHORIZED_API_READY"
    seen = []
    monkeypatch.setattr("market_monitor.dce_authorized_backfill.DceClient", lambda *args, **kwargs: object())

    class Pool:
        def __init__(self, root, clients):
            seen.append(len(clients))

        def close(self):
            seen.append("closed")

    monkeypatch.setattr("market_monitor.dce_parallel_backfill.DceClientPool", Pool)
    monkeypatch.setattr("market_monitor.dce_authorized_backfill.backfill", lambda *args, **kwargs: {"status": "PASS"})
    assert update._import_source(tmp_path, "DCE", DAYS[-1])["status"] == "PASS"
    assert seen == [2, "closed"]


def _coverage(day: str, venue: str, *, status: str = "PASS", official: bool = True) -> dict[str, object]:
    return {
        "trading_day": day, "exchange": venue, "status": status,
        "contract_count": 0, "record_count": 0,
        "source": ("official-synthetic-" if official else "synthetic-wrapper-") + venue.lower(),
        "error": "synthetic unavailable" if status == "FAILED" else None,
        "collected_at": day + "T08:00:00+00:00", "quality_status": status,
    }


def _persist(root: Path, rows: list[dict[str, object]]) -> None:
    store = MarketStore(root)
    try:
        store.upsert_futures_member_position_coverage(rows)
    finally:
        store.close()


def _prepare(
    root: Path, monkeypatch: pytest.MonkeyPatch, days: tuple[str, ...] = DAYS,
    missing: set[tuple[str, str]] | None = None,
) -> None:
    sync_futures_trading_calendar(
        root, fetcher=lambda: [(day,) for day in days],
        retrieved_at="2026-10-03T00:00:00+00:00",
    )
    monkeypatch.setattr(update, "sync_futures_trading_calendar", lambda _root: {"provider": CALENDAR_PROVIDER})
    monkeypatch.setattr(update, "default_backfill_range", lambda _root: ("2025-09-30", days[-1]))
    omitted = missing or set()
    _persist(root, [
        _coverage(day, venue, status="NO_PUBLICATION" if venue == "GFEX" else "PASS")
        for day in days for venue in VENUES if (day, venue) not in omitted
    ])


def _import_success(root: Path, source: str, day: str) -> dict[str, object]:
    _persist(root, [_coverage(day, venue) for venue in update._SOURCE_VENUES[source]])
    return {"status": "PASS"}


def test_all_908_official_covered_days_skip_every_download_and_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = date(2023, 1, 3)
    days = tuple((start + timedelta(days=i)).isoformat() for i in range(1272)
                 if (start + timedelta(days=i)).weekday() < 5)[:908]
    assert len(days) == 908
    _prepare(tmp_path, monkeypatch, days)
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: pytest.fail("covered history was downloaded"))
    monkeypatch.setattr(update, "_import_source", lambda *_args: pytest.fail("covered history was reimported"))
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert result["expectedDays"] == 908
    assert result["pendingSourceDays"] == result["updatedSourceDays"] == 0
    assert result["tradingDay"] == days[-1]
    assert result["startDay"] == "2023-01-01"
    assert result["skippedSources"] == list(update._SOURCE_VENUES)


def test_restart_fills_old_missing_days_even_when_latest_day_is_covered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = {(DAYS[0], "DCE"), (DAYS[1], "DCE"), (DAYS[1], "INE"), (DAYS[2], "CZCE")}
    _prepare(tmp_path, monkeypatch, missing=missing)
    captures: list[tuple[str, str]] = []
    imports: list[tuple[str, str]] = []

    def capture(**kwargs: str) -> dict[str, str]:
        captures.append((kwargs["source"], kwargs["trading_day"]))
        return {"status": "PASS", "captureStatus": "PASS_RAW"}

    def importer(root: Path, source: str, day: str) -> dict[str, object]:
        assert threading.current_thread() is threading.main_thread()
        imports.append((source, day))
        return _import_success(root, source, day)

    monkeypatch.setattr(update, "_run_capture", capture)
    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert result["tradingDay"] == DAYS[-1]
    assert result["pendingSourceDays"] == result["updatedSourceDays"] == 4
    assert sorted(captures) == sorted([("DCE", DAYS[0]), ("DCE", DAYS[1]), ("SHFE", DAYS[1]), ("CZCE", DAYS[2])])
    assert imports == [("DCE", DAYS[0]), ("DCE", DAYS[1]), ("SHFE", DAYS[1]), ("CZCE", DAYS[2])]
    assert result["missingDaysByVenue"] == {}
    assert "skippedSources" not in result  # Existing admin messages must not call a repaired run a no-op.
    captures.clear()
    imports.clear()
    retry = update.run_latest_member_rank_update(tmp_path)
    assert retry["status"] == "PASS" and retry["pendingSourceDays"] == 0
    assert not captures and not imports


def test_nonofficial_pass_does_not_suppress_an_old_official_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE")})
    _persist(tmp_path, [_coverage(DAYS[0], "DCE", official=False)])
    captures: list[str] = []
    monkeypatch.setattr(update, "_run_capture", lambda **kw: captures.append(kw["trading_day"]) or {"status": "PASS"})
    monkeypatch.setattr(update, "_import_source", _import_success)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert captures == [DAYS[0]]


def test_failure_does_not_import_or_fabricate_no_publication_and_other_sources_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE"), (DAYS[1], "DCE"), (DAYS[0], "CZCE")})
    imports: list[tuple[str, str]] = []

    def capture(**kwargs: str) -> dict[str, str]:
        if (kwargs["source"], kwargs["trading_day"]) == ("DCE", DAYS[0]):
            return {"status": "FAILED", "captureStatus": "FAILED", "detail": "HTTP 404"}
        return {"status": "PASS", "captureStatus": "PASS_RAW"}

    def importer(root: Path, source: str, day: str) -> dict[str, object]:
        imports.append((source, day))
        return _import_success(root, source, day)

    monkeypatch.setattr(update, "_run_capture", capture)
    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PARTIAL"
    assert result["coveredVenuesAfter"] == sorted(VENUES)
    assert result["missingVenues"] == ["DCE"]
    assert result["missingDaysByVenue"] == {"DCE": [DAYS[0]]}
    assert result["sources"]["DCE"]["status"] == "PARTIAL"
    assert result["sources"]["CZCE"]["status"] == "PASS"
    assert imports == [("CZCE", DAYS[0]), ("DCE", DAYS[1])]
    assert DAYS[0] not in update._official_coverage_by_day(tmp_path) or "DCE" not in update._official_coverage_by_day(tmp_path)[DAYS[0]]
    captured_retry: list[tuple[str, str]] = []
    monkeypatch.setattr(update, "_run_capture", lambda **kw: captured_retry.append((kw["source"], kw["trading_day"])) or {"status": "PASS"})
    retry = update.run_latest_member_rank_update(tmp_path)
    assert retry["status"] == "PASS"
    assert captured_retry == [("DCE", DAYS[0])]


def test_import_failure_and_unwritten_success_remain_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "CFFEX"), (DAYS[0], "SHFE"), (DAYS[0], "INE"), (DAYS[0], "CZCE")})
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: {"status": "PASS", "captureStatus": "PASS_RAW"})

    def importer(root: Path, source: str, day: str) -> dict[str, object]:
        if source == "CFFEX":
            raise ValueError("synthetic SHA mismatch")
        if source == "SHFE":
            return {"status": "PASS"}  # A claimed status is not persisted coverage evidence.
        return _import_success(root, source, day)

    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PARTIAL"
    assert result["missingDaysByVenue"] == {"CFFEX": [DAYS[0]], "INE": [DAYS[0]], "SHFE": [DAYS[0]]}
    assert result["sources"]["CFFEX"]["days"][0]["importStatus"] == "FAILED"
    assert result["sources"]["SHFE"]["status"] == "FAILED"
    assert result["sources"]["CZCE"]["status"] == "PASS"


def test_five_source_capture_workers_run_concurrently_and_imports_remain_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    day = DAYS[-1]
    _prepare(tmp_path, monkeypatch, days=(day,), missing={(day, venue) for venue in VENUES})
    barrier = threading.Barrier(5)
    lock = threading.Lock()
    active = maximum = 0
    captured: list[str] = []

    def capture(**kwargs: str) -> dict[str, str]:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        with lock:
            captured.append(kwargs["source"])
            active -= 1
        return {"status": "PASS"}

    def importer(root: Path, source: str, trading_day: str) -> dict[str, object]:
        assert threading.current_thread() is threading.main_thread()
        assert active == 0 and len(captured) == 5
        return _import_success(root, source, trading_day)

    monkeypatch.setattr(update, "_run_capture", capture)
    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert maximum == 5
    assert result["pendingSourceDays"] == 5
    assert result["updatedSourceDays"] == 5


def test_gfex_verified_empty_publication_is_accepted_only_after_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "GFEX")})
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: {"status": "PASS", "captureStatus": "PASS_RAW"})

    def importer(root: Path, source: str, day: str) -> dict[str, str]:
        assert source == "GFEX"
        _persist(root, [_coverage(day, "GFEX", status="NO_PUBLICATION")])
        return {"status": "PASS_WITH_UNAVAILABLE"}

    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert result["sources"]["GFEX"]["days"][0]["importStatus"] == "PASS_WITH_UNAVAILABLE"


def test_future_calendar_dates_and_holidays_are_never_captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, days=(*DAYS, "2026-10-08"))
    monkeypatch.setattr(update, "default_backfill_range", lambda _root: ("2025-09-30", DAYS[-1]))
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: pytest.fail("future or holiday was captured"))
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert result["expectedDays"] == 4
    assert result["endDay"] == DAYS[-1]


@pytest.mark.parametrize("returncode,summary", [
    (1, {"status": "FAILED", "errorCode": "HTTP_404"}),
    (0, {"status": "NO_PUBLICATION"}),
    (0, {"status": "PARTIAL_RAW"}),
])
def test_capture_subprocess_never_promotes_refusal_or_unverified_empty_to_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, summary: dict,
) -> None:
    monkeypatch.setattr(update.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=returncode, stdout=json.dumps(summary), stderr="",
    ))
    result = update._run_capture(project_root=tmp_path, data_root=tmp_path, source="GFEX", trading_day=DAYS[0])
    assert result["status"] == "FAILED"


def test_capture_timeout_and_unhandled_source_errors_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("synthetic", 300)

    monkeypatch.setattr(update.subprocess, "run", timed_out)
    result = update._run_capture(project_root=tmp_path, data_root=tmp_path, source="DCE", trading_day=DAYS[0])
    assert result["status"] == "FAILED" and "300" in result["detail"]


@pytest.mark.parametrize("failure", [
    {"status": "FAILED", "httpStatus": 412, "detail": "source rejected"},
    {"status": "FAILED", "httpStatus": 429, "detail": "source rejected"},
    {"status": "FAILED", "httpStatus": 503, "detail": "retries exhausted"},
    {"status": "FAILED", "captureStatus": "BLOCKED", "detail": "source rejected"},
    {"status": "FAILED", "detail": "WAF verification challenge"},
])
def test_documented_host_refusal_defers_only_that_source_and_keeps_date_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: dict,
) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE"), (DAYS[1], "DCE"), (DAYS[0], "CZCE")})
    captures: list[tuple[str, str]] = []

    def capture(**kwargs: str) -> dict[str, str]:
        captures.append((kwargs["source"], kwargs["trading_day"]))
        return failure if kwargs["source"] == "DCE" else {"status": "PASS"}

    monkeypatch.setattr(update, "_run_capture", capture)
    monkeypatch.setattr(update, "_import_source", _import_success)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PARTIAL"
    assert sorted(captures) == sorted([("DCE", DAYS[0]), ("CZCE", DAYS[0])])
    assert result["sources"]["DCE"]["days"][1]["status"] == "DEFERRED"
    assert result["sources"]["CZCE"]["status"] == "PASS"
    assert result["missingDaysByVenue"] == {"DCE": [DAYS[0], DAYS[1]]}


@pytest.mark.parametrize("import_result", [
    {"status": "FAILED", "report": {"status": "FAILED", "apiCode": 501}},
    {"status": "FAILED", "report": {"status": "FAILED", "apiCode": 429}},
    {"status": "BUDGET_PAUSED", "report": {"status": "BUDGET_PAUSED"}},
    {"status": "BATCH_LIMIT", "report": {"status": "BATCH_LIMIT"}},
    {"status": "TLS_BLOCKED", "report": {"status": "TLS_BLOCKED"}},
])
def test_dce_import_refusal_defers_later_dates_without_stopping_other_venues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, import_result: dict,
) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE"), (DAYS[1], "DCE"),
                                         (DAYS[1], "CZCE")})
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: {"status": "PASS"})
    imports: list[tuple[str, str]] = []

    def importer(root: Path, source: str, day: str) -> dict:
        imports.append((source, day))
        return import_result if source == "DCE" else _import_success(root, source, day)

    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert imports == [("DCE", DAYS[0]), ("CZCE", DAYS[1])]
    assert result["status"] == "PARTIAL"
    assert result["sources"]["DCE"]["days"][1]["status"] == "DEFERRED"
    assert result["sources"]["CZCE"]["status"] == "PASS"
    assert result["missingDaysByVenue"] == {"DCE": [DAYS[0], DAYS[1]]}


def test_dce_missing_report_does_not_pause_other_dates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE"), (DAYS[1], "DCE")})
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: {"status": "PASS"})
    imports: list[str] = []

    def importer(root: Path, source: str, day: str) -> dict:
        imports.append(day)
        if day == DAYS[0]:
            return {"status": "FAILED", "report": {"status": "FAILED", "apiCode": 404}}
        return _import_success(root, source, day)

    monkeypatch.setattr(update, "_import_source", importer)
    result = update.run_latest_member_rank_update(tmp_path)
    assert imports == [DAYS[0], DAYS[1]]
    assert result["missingDaysByVenue"] == {"DCE": [DAYS[0]]}


def _authorized_batch(day: str, *, processed: int = 25, completed: int = 50) -> dict:
    return {"status": "BATCH_LIMIT", "report": {
        "status": "BATCH_LIMIT", "currentDay": day, "processedContracts": processed,
        "dayCompletedContracts": completed, "dayTotalContracts": 250,
        "dayPendingContracts": 250 - completed,
    }}


def test_authorized_dce_batch_progress_is_resumable_not_an_emulator_failure(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE"), (DAYS[1], "DCE")})
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: {
        "status": "PASS", "captureStatus": "AUTHORIZED_API_READY",
    })
    monkeypatch.setattr(update, "_import_source", lambda _root, _source, day: _authorized_batch(day))
    result = update.run_latest_member_rank_update(tmp_path)
    source = result["sources"]["DCE"]
    assert source["status"] == "PARTIAL"
    assert source["importStatus"] == "BATCH_LIMIT"
    assert source["captureStatus"] == "AUTHORIZED_API_READY"
    assert "50/250" in source["detail"] and "剩余200" in source["detail"]
    assert source["days"][1]["status"] == "DEFERRED"
    assert result["continuation"] == {
        "reason": "DCE_AUTHORIZED_BATCH_LIMIT", "source": "DCE_AUTHORIZED_API",
        "tradingDay": DAYS[0], "processedContracts": 25, "completedContracts": 50,
        "totalContracts": 250, "remainingContracts": 200, "retryAfterSeconds": 900,
    }


def test_authorized_batch_without_progress_or_other_source_failure_does_not_fast_retry(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch, missing={(DAYS[0], "DCE"), (DAYS[0], "CZCE")})
    monkeypatch.setattr(update, "_run_capture", lambda **kw: {
        "status": "PASS", "captureStatus": "AUTHORIZED_API_READY",
    } if kw["source"] == "DCE" else {"status": "FAILED", "detail": "synthetic 503"})
    monkeypatch.setattr(update, "_import_source", lambda _root, _source, day: _authorized_batch(day))
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["sources"]["DCE"]["status"] == "PARTIAL"
    assert "continuation" not in result
    assert update._dce_batch_continuation(_authorized_batch(DAYS[0], processed=0), DAYS[0]) is None
    assert update._dce_batch_continuation(_authorized_batch(DAYS[0]), DAYS[1]) is None


@pytest.mark.parametrize("outcome", [
    {"status": "FAILED", "httpStatus": 404, "detail": "official HTTP 404"},
    {"status": "FAILED", "captureStatus": "MISSING_REPORT", "detail": "missing file"},
    {"status": "FAILED", "detail": "unknown parser failure"},
])
def test_a_single_missing_report_or_unknown_error_does_not_pause_a_host(outcome: dict) -> None:
    assert update._source_pause_required(outcome) is False


def test_capture_result_preserves_explicit_rejection_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = {"status": "FAILED", "errorType": "GfexSourceError", "httpStatus": 567,
               "apiCode": "synthetic-blocked", "reason": "explicit source refusal"}
    monkeypatch.setattr(update.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps(summary), stderr="",
    ))
    result = update._run_capture(project_root=tmp_path, data_root=tmp_path, source="GFEX", trading_day=DAYS[0])
    assert result["httpStatus"] == 567
    assert result["reason"] == result["detail"] == "explicit source refusal"
    assert update._source_pause_required(result)


def _freeze_calendar_cutoff(monkeypatch: pytest.MonkeyPatch, *, hour: int = 18) -> None:
    monkeypatch.setattr(update, "datetime", SimpleNamespace(
        now=lambda _zone: datetime(2026, 10, 3, hour, tzinfo=update.BEIJING),
    ))


def test_complete_local_calendar_and_rank_coverage_need_zero_external_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, days=(*DAYS, "2026-12-31"))
    _freeze_calendar_cutoff(monkeypatch)
    monkeypatch.setattr(update, "default_backfill_range", lambda _root: ("2025-09-30", DAYS[-1]))
    monkeypatch.setattr(update, "sync_futures_trading_calendar", lambda _root: pytest.fail("verified future calendar must work offline"))
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: pytest.fail("covered ranks must not request a source"))
    monkeypatch.setattr(update, "_import_source", lambda *_args: pytest.fail("covered ranks must not be reimported"))
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS" and result["pendingSourceDays"] == 0
    assert result["calendarProvider"] == CALENDAR_PROVIDER
    assert result["expectedDays"] == 4


@pytest.mark.parametrize("missing_calendar", [False, True])
def test_missing_or_stale_calendar_refreshes_only_when_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_calendar: bool,
) -> None:
    _prepare(tmp_path, monkeypatch)
    _freeze_calendar_cutoff(monkeypatch)
    if missing_calendar:
        (tmp_path / CALENDAR_RELATIVE_PATH).unlink()
    refreshes: list[Path] = []

    def refresh_calendar(root: Path) -> dict:
        refreshes.append(root)
        return sync_futures_trading_calendar(
            root, fetcher=lambda: [(day,) for day in (*DAYS, "2026-12-31")],
            retrieved_at="2026-10-03T10:00:00+00:00",
        )

    monkeypatch.setattr(update, "sync_futures_trading_calendar", refresh_calendar)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS"
    assert refreshes == [tmp_path.resolve()]
    update.run_latest_member_rank_update(tmp_path)
    assert refreshes == [tmp_path.resolve()]


def test_failed_calendar_refresh_uses_concurrent_verified_local_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    _freeze_calendar_cutoff(monkeypatch)

    def failed_refresh(root: Path) -> None:
        sync_futures_trading_calendar(
            root, fetcher=lambda: [(day,) for day in (*DAYS, "2026-12-31")],
            retrieved_at="2026-10-03T10:00:00+00:00",
        )
        raise OSError("synthetic network failure after another task refreshed the calendar")

    monkeypatch.setattr(update, "sync_futures_trading_calendar", failed_refresh)
    result = update.run_latest_member_rank_update(tmp_path)
    assert result["status"] == "PASS" and result["calendarProvider"] == CALENDAR_PROVIDER


def test_unresolved_stale_calendar_network_failure_never_claims_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    _freeze_calendar_cutoff(monkeypatch)

    def failed_refresh(_root: Path) -> None:
        raise OSError("synthetic stale calendar unavailable")

    monkeypatch.setattr(update, "sync_futures_trading_calendar", failed_refresh)
    monkeypatch.setattr(update, "_run_capture", lambda **_kwargs: pytest.fail("calendar unresolved"))
    with pytest.raises(OSError, match="synthetic stale calendar unavailable"):
        update.run_latest_member_rank_update(tmp_path)
