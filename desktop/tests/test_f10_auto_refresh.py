from datetime import datetime, timedelta, timezone

import pytest

from market_monitor.f10_auto_refresh import due_f10_updates, due_revenue_publication_recovery
from market_monitor.operations import Operation, OperationKind, OperationStatus


def test_f10_daily_windows_deduplicate_active_and_failed_attempts():
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 31, 20, tzinfo=tz)
    expected = [OperationKind.F10_UPDATE_CN, OperationKind.F10_UPDATE_HK, OperationKind.REVENUE_UPDATE]
    assert due_f10_updates([], now) == expected
    items = [Operation("a", expected[0], OperationStatus.PARTIAL_FAILURE, now.isoformat()),
             Operation("b", expected[1], OperationStatus.RUNNING, (now - timedelta(days=1)).isoformat())]
    assert due_f10_updates(items, now) == [OperationKind.REVENUE_UPDATE]
    assert OperationKind.F10_UPDATE_CN not in due_f10_updates(items, now + timedelta(hours=22))
    assert OperationKind.F10_UPDATE_CN in due_f10_updates(items, now + timedelta(days=1))


def _revenue_batch(start: datetime, *, status=OperationStatus.PARTIAL_FAILURE,
                   updated=24, remaining=5514, failed=1):
    end = start + timedelta(minutes=1)
    op = Operation("revenue", OperationKind.REVENUE_UPDATE, status, start.isoformat(),
                   started_at=start.isoformat(), completed_at=end.isoformat())
    summary = {"status": "PARTIAL_FAILURE", "updated": updated, "remaining": remaining,
               "failed": failed, "completedAt": end.isoformat()}
    return op, summary


def test_productive_revenue_backlog_uses_two_hour_slots_and_catches_only_latest():
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 10, 31, 8, tzinfo=tz)
    op, summary = _revenue_batch(start)
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(hours=1, minutes=59), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], start + timedelta(hours=2), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], start + timedelta(hours=8, minutes=30), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], start + timedelta(days=1, hours=-7), revenue_summary=summary)
    assert OperationKind.F10_UPDATE_CN not in due_f10_updates([Operation("cn", OperationKind.F10_UPDATE_CN,
        OperationStatus.PASS, start.isoformat())], start + timedelta(hours=2), revenue_summary=summary)


def test_revenue_backlog_enforces_minimum_gap_and_deduplicates_active():
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 10, 31, 9, 59, tzinfo=tz)
    op, summary = _revenue_batch(start)
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(minutes=1), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(minutes=90), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], start + timedelta(minutes=91), revenue_summary=summary)
    active = Operation("active", OperationKind.REVENUE_UPDATE, OperationStatus.RUNNING, start.isoformat())
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([active], start + timedelta(hours=4), revenue_summary=summary)


def test_revenue_cooldown_starts_at_completion_after_long_queue_wait():
    tz = timezone(timedelta(hours=8))
    queued = datetime(2026, 10, 31, 8, tzinfo=tz)
    started = queued + timedelta(hours=3, minutes=59)
    completed = started + timedelta(minutes=2)
    op = Operation("delayed", OperationKind.REVENUE_UPDATE, OperationStatus.PARTIAL_FAILURE, queued.isoformat(),
                   started_at=started.isoformat(), completed_at=completed.isoformat())
    summary = {"status": "PARTIAL_FAILURE", "updated": 24, "remaining": 5514, "failed": 1,
               "completedAt": completed.isoformat()}
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], completed + timedelta(minutes=89), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], completed + timedelta(minutes=90), revenue_summary=summary)


@pytest.mark.parametrize("updated,remaining,failed", [(0, 20, 8), (0, 2, 2), (5, 20, 8)])
def test_unproductive_revenue_source_backs_off_at_least_12_hours(updated, remaining, failed):
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 10, 31, 18, tzinfo=tz)
    op, summary = _revenue_batch(start, updated=updated, remaining=remaining, failed=failed)
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(hours=2), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(hours=11), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(hours=12), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], start + timedelta(hours=12, minutes=1), revenue_summary=summary)


def test_revenue_summary_must_match_last_operation_and_no_backlog_uses_daily():
    tz = timezone(timedelta(hours=8))
    start = datetime(2026, 10, 31, 8, tzinfo=tz)
    op, summary = _revenue_batch(start)
    summary["completedAt"] = (start - timedelta(minutes=1)).isoformat()
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(hours=2), revenue_summary=summary)
    op, summary = _revenue_batch(start, updated=25, remaining=0, failed=0)
    summary["status"] = "PASS"
    assert OperationKind.REVENUE_UPDATE not in due_f10_updates([op], start + timedelta(hours=2), revenue_summary=summary)
    assert OperationKind.REVENUE_UPDATE in due_f10_updates([op], start + timedelta(hours=12), revenue_summary=summary)


def test_publication_recovery_is_local_deduplicated_and_rate_limited():
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 31, 12, tzinfo=tz)
    assert not due_revenue_publication_recovery([], now, pending=False)
    assert due_revenue_publication_recovery([], now, pending=True)
    for kind in (OperationKind.REVENUE_UPDATE, OperationKind.F10_REVENUE_REPUBLISH):
        active = Operation("active", kind, OperationStatus.RUNNING, now.isoformat())
        assert not due_revenue_publication_recovery([active], now, pending=True)
    latest = Operation("recovery", OperationKind.F10_REVENUE_REPUBLISH, OperationStatus.FAILED,
                       (now - timedelta(hours=1)).isoformat(), completed_at=(now - timedelta(minutes=29)).isoformat())
    assert not due_revenue_publication_recovery([latest], now, pending=True)
    assert due_revenue_publication_recovery([latest], now + timedelta(minutes=1), pending=True)
    latest.completed_at = "invalid"
    assert not due_revenue_publication_recovery([latest], now, pending=True)
