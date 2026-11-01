"""Trading-day refresh scheduling is bounded and does not fetch on reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

from market_monitor.futures_auto_refresh import (due_operations, due_rank_continuation,
                                                  target_day, member_rank_continuation_marker)
from market_monitor.futures_calendar import FuturesTradingCalendar
from market_monitor.operations import Operation, OperationKind, OperationStatus


def _calendar() -> FuturesTradingCalendar:
    return cast(FuturesTradingCalendar, SimpleNamespace(trading_days=("2026-10-19", "2026-10-20")))


def _job(kind: OperationKind, status: OperationStatus, created_at: str) -> Operation:
    return Operation("synthetic-job", kind, status, created_at,
                     completed_at=created_at if status not in {OperationStatus.QUEUED,
                                                                  OperationStatus.RUNNING} else None)


def test_each_source_waits_for_its_window_and_uses_verified_calendar() -> None:
    calendar = _calendar()
    before = datetime(2026, 10, 20, 10, 29, tzinfo=timezone.utc)
    after = datetime(2026, 10, 20, 10, 30, tzinfo=timezone.utc)
    assert target_day(calendar, OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, before)[0] == "2026-10-19"
    assert target_day(calendar, OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, after)[0] == "2026-10-20"
    assert target_day(calendar, OperationKind.FUTURES_MEMBER_RANK_UPDATE, after)[0] == "2026-10-19"
    assert due_operations(None, [], after) == [(OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, None)]


def test_partial_retries_have_source_backoff_and_pass_stops_same_day() -> None:
    calendar = _calendar()
    now = datetime(2026, 10, 20, 12, 10, tzinfo=timezone.utc)
    jobs = [
        _job(OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, OperationStatus.PASS,
             "2026-10-20T10:40:00+00:00"),
        _job(OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE, OperationStatus.PARTIAL_FAILURE,
             "2026-10-20T11:10:00+00:00"),
        _job(OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.PARTIAL_FAILURE,
             "2026-10-20T12:05:00+00:00"),
    ]
    assert due_operations(calendar, jobs, now) == []
    later = datetime(2026, 10, 20, 16, 6, tzinfo=timezone.utc)
    assert due_operations(calendar, jobs, later) == [
        (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20")]
    jobs[-1].status = OperationStatus.QUEUED
    assert due_operations(calendar, jobs, later) == []


def test_cancelled_job_is_not_resubmitted_until_next_trading_day() -> None:
    calendar = _calendar()
    cancelled = _job(OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.CANCELLED,
                     "2026-10-20T12:05:00+00:00")
    now = datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20") not in due_operations(
        calendar, [cancelled], now)


def test_interrupted_job_resumes_without_waiting_for_retry_backoff() -> None:
    interrupted = _job(OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, OperationStatus.FAILED,
                       "2026-10-20T10:35:00+00:00")
    interrupted.detail = "process stopped before operation completed"
    now = datetime(2026, 10, 20, 10, 40, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, "2026-10-20") in due_operations(
        _calendar(), [interrupted], now)


def test_calendar_horizon_is_rechecked_without_guessing_a_trading_day() -> None:
    old = cast(FuturesTradingCalendar, SimpleNamespace(trading_days=("2026-10-19",)))
    now = datetime(2026, 10, 20, 10, 35, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, None) in due_operations(old, [], now)
    passed = _job(OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, OperationStatus.PASS,
                  "2026-10-20T10:31:00+00:00")
    assert (OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, None) not in due_operations(old, [passed], now)


def test_long_partial_job_waits_from_completion_in_verified_day_branch() -> None:
    job = Operation(
        "slow-rank", OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.PARTIAL_FAILURE,
        "2026-10-20T12:05:00+00:00", completed_at="2026-10-20T16:59:00+00:00",
    )
    before = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20") not in due_operations(
        _calendar(), [job], before,
    )
    after = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20") in due_operations(
        _calendar(), [job], after,
    )


def test_long_partial_job_waits_from_completion_in_calendar_probe_branch() -> None:
    old = cast(FuturesTradingCalendar, SimpleNamespace(trading_days=("2026-10-19",)))
    job = Operation(
        "slow-probe", OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationStatus.PARTIAL_FAILURE, "2026-10-20T10:35:00+00:00",
        completed_at="2026-10-20T11:29:00+00:00",
    )
    before = datetime(2026, 10, 20, 11, 30, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, None) not in due_operations(
        old, [job], before,
    )
    after = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, None) in due_operations(
        old, [job], after,
    )


def _continuation(completed: int = 50) -> dict:
    return {"continuation": {
        "reason": "DCE_AUTHORIZED_BATCH_LIMIT", "source": "DCE_AUTHORIZED_API",
        "tradingDay": "2026-10-20", "processedContracts": 25,
        "completedContracts": completed, "totalContracts": 250,
        "remainingContracts": 250 - completed,
    }}


def test_advancing_authorized_batch_continues_after_fifteen_minutes_from_completion() -> None:
    completed_at = datetime(2026, 10, 20, 12, 10, tzinfo=timezone.utc)
    marker = member_rank_continuation_marker(_continuation())
    assert marker == "DCE_AUTHORIZED_BATCH_CONTINUATION:2026-10-20:25:50:250"
    job = Operation("batch", OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.PARTIAL_FAILURE,
                    "2026-10-20T12:01:00+00:00", completed_at=completed_at.isoformat(),
                    detail=f"PARTIAL_FAILURE: 授权API分批进度；{marker}")
    expected = (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20")
    assert expected not in due_operations(_calendar(), [job], completed_at + timedelta(minutes=14))
    assert expected in due_operations(_calendar(), [job], completed_at + timedelta(minutes=15))
    assert not due_rank_continuation(_calendar(), [job], completed_at + timedelta(minutes=14, seconds=59))
    assert due_rank_continuation(_calendar(), [job], completed_at + timedelta(minutes=15))
    active = Operation("active", OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.RUNNING,
                       "2026-10-20T12:26:00+00:00")
    assert not due_rank_continuation(_calendar(), [job, active], completed_at + timedelta(minutes=16))


def test_no_checkpoint_growth_keeps_normal_rank_backoff() -> None:
    marker = member_rank_continuation_marker(_continuation())
    previous = Operation("old", OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.PARTIAL_FAILURE,
                         "2026-10-20T12:01:00+00:00", detail=marker)
    latest = Operation("new", OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.PARTIAL_FAILURE,
                       "2026-10-20T12:20:00+00:00", completed_at="2026-10-20T12:30:00+00:00", detail=marker)
    expected = (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20")
    assert expected not in due_operations(_calendar(), [previous, latest],
                                          datetime(2026, 10, 20, 12, 45, tzinfo=timezone.utc))
    assert expected in due_operations(_calendar(), [previous, latest],
                                      datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc))
    # The faster watcher never accelerates a batch that did not advance.
    assert not due_rank_continuation(_calendar(), [previous, latest],
                                     datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc))


def test_new_trading_day_does_not_bypass_batch_completion_cooldown() -> None:
    calendar = cast(FuturesTradingCalendar,
                    SimpleNamespace(trading_days=("2026-10-20", "2026-10-21")))
    job = Operation("prior-day", OperationKind.FUTURES_MEMBER_RANK_UPDATE,
                    OperationStatus.PARTIAL_FAILURE, "2026-10-20T12:01:00+00:00",
                    completed_at="2026-10-21T11:58:00+00:00",
                    detail=member_rank_continuation_marker(_continuation()))
    assert not due_rank_continuation(calendar, [job], datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
    assert due_rank_continuation(calendar, [job], datetime(2026, 10, 21, 12, 13, tzinfo=timezone.utc))


def test_invalid_batch_marker_or_refusal_does_not_shorten_retry() -> None:
    expected = (OperationKind.FUTURES_MEMBER_RANK_UPDATE, "2026-10-20")
    for detail in ("原因=API_501", "DCE_AUTHORIZED_BATCH_CONTINUATION:2026-10-20:0:50:250",
                   "DCE_AUTHORIZED_BATCH_CONTINUATION:2026-10-21:25:50:250",
                   "DCE_AUTHORIZED_BATCH_CONTINUATION:2026-10-20:25:50:250:unexpected"):
        job = Operation("bad", OperationKind.FUTURES_MEMBER_RANK_UPDATE, OperationStatus.PARTIAL_FAILURE,
                        "2026-10-20T12:01:00+00:00", completed_at="2026-10-20T12:10:00+00:00", detail=detail)
        assert expected not in due_operations(_calendar(), [job],
                                              datetime(2026, 10, 20, 12, 25, tzinfo=timezone.utc))
        assert not due_rank_continuation(_calendar(), [job],
                                         datetime(2026, 10, 20, 12, 25, tzinfo=timezone.utc))
    no_progress = _continuation()
    no_progress["continuation"]["processedContracts"] = 0
    assert member_rank_continuation_marker(no_progress) is None
    inconsistent = _continuation()
    inconsistent["continuation"]["remainingContracts"] = 199
    assert member_rank_continuation_marker(inconsistent) is None
