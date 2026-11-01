"""Cheap trading-day policy for the service-owned futures refresh queue.

The page reads status only.  This policy never fetches or writes provider data;
it selects existing serial operations after their configured observation window.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .futures_calendar import FuturesTradingCalendar
from .operations import Operation, OperationKind, OperationStatus


BEIJING = ZoneInfo("Asia/Shanghai")
CHECK_INTERVAL_SECONDS = 15 * 60
RANK_CONTINUATION_CHECK_INTERVAL_SECONDS = 60
AUTO_KINDS = (
    OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
    OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE,
    OperationKind.FUTURES_MEMBER_RANK_UPDATE,
)
_WINDOWS = {
    OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE: time(18, 30),
    OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE: time(19, 0),
    OperationKind.FUTURES_MEMBER_RANK_UPDATE: time(20, 0),
}
_RETRY_AFTER = {
    OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE: timedelta(hours=2),
    OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE: timedelta(hours=12),
    OperationKind.FUTURES_MEMBER_RANK_UPDATE: timedelta(hours=4),
}
_ACTIVE = {OperationStatus.QUEUED, OperationStatus.RUNNING}
_RANK_CONTINUATION_RETRY = timedelta(minutes=15)
_RANK_CONTINUATION_MARKER = "DCE_AUTHORIZED_BATCH_CONTINUATION"
_RANK_CONTINUATION_PATTERN = re.compile(
    rf"\b{_RANK_CONTINUATION_MARKER}:(\d{{4}}-\d{{2}}-\d{{2}}):([1-9]\d*):([1-9]\d*):([1-9]\d*)(?=$|[\s；;。])"
)


def member_rank_continuation_marker(report: dict[str, Any]) -> str | None:
    """Serialize only a validated, advancing authorized-API checkpoint."""

    continuation = report.get("continuation")
    if not isinstance(continuation, dict) or (
        continuation.get("reason") != "DCE_AUTHORIZED_BATCH_LIMIT"
        or continuation.get("source") != "DCE_AUTHORIZED_API"
    ):
        return None
    day = continuation.get("tradingDay")
    numbers = [continuation.get(key) for key in (
        "processedContracts", "completedContracts", "totalContracts", "remainingContracts",
    )]
    if not isinstance(day, str) or any(type(value) is not int for value in numbers):
        return None
    try:
        if datetime.fromisoformat(day).date().isoformat() != day:
            return None
    except ValueError:
        return None
    processed, completed, total, remaining = numbers
    if not 0 < processed <= completed < total <= 100_000 or remaining != total - completed:
        return None
    return f"{_RANK_CONTINUATION_MARKER}:{day}:{processed}:{completed}:{total}"


def _rank_continuation(detail: str | None) -> tuple[str, int, int, int] | None:
    match = _RANK_CONTINUATION_PATTERN.search(detail or "")
    if match is None:
        return None
    day, processed_text, completed_text, total_text = match.groups()
    processed, completed, total = map(int, (processed_text, completed_text, total_text))
    if not 0 < processed <= completed < total <= 100_000:
        return None
    return day, processed, completed, total


def _retry_after(kind: OperationKind, latest: Operation, jobs: list[Operation],
                 calendar: FuturesTradingCalendar, target: str) -> timedelta:
    """A capped successful batch continues sooner; refusals retain source backoff."""

    if kind != OperationKind.FUTURES_MEMBER_RANK_UPDATE or latest.status != OperationStatus.PARTIAL_FAILURE:
        return _RETRY_AFTER[kind]
    progress = _rank_continuation(latest.detail)
    if progress is None or progress[0] not in calendar.trading_days or progress[0] > target:
        return _RETRY_AFTER[kind]
    previous = [item for item in jobs if item.created_at < latest.created_at
                and (prior := _rank_continuation(item.detail)) is not None
                and prior[0] == progress[0]]
    if previous:
        prior = _rank_continuation(max(previous, key=lambda item: item.created_at).detail)
        if prior is not None and progress[2] <= prior[2]:
            return _RETRY_AFTER[kind]
    return _RANK_CONTINUATION_RETRY


def target_day(calendar: FuturesTradingCalendar | None, kind: OperationKind,
               now: datetime) -> tuple[str, datetime] | None:
    """Return the latest verified day whose conservative publication window opened."""

    if calendar is None or not calendar.trading_days:
        return None
    local = now.astimezone(BEIJING)
    for day in reversed(calendar.trading_days):
        opened = datetime.combine(datetime.fromisoformat(day).date(), _WINDOWS[kind], BEIJING)
        if opened <= local:
            return day, opened.astimezone(timezone.utc)
    return None


def due_operations(calendar: FuturesTradingCalendar | None, operations: Iterable[Operation],
                   now: datetime) -> list[tuple[OperationKind, str | None]]:
    """Deduplicate by active job and retry partial/failed jobs with source backoff."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(BEIJING)
    history = list(operations)
    due: list[tuple[OperationKind, str | None]] = []
    for kind in AUTO_KINDS:
        jobs = [item for item in history if item.kind == kind]
        if any(item.status in _ACTIVE for item in jobs):
            continue
        if (kind == OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE and calendar is not None
                and calendar.trading_days and calendar.trading_days[-1] < local.date().isoformat()
                and local.time() >= _WINDOWS[kind]):
            # The calendar may have ended within the same year. Probe it once
            # per day so a new trading day can become a verified target.
            probe_opened = datetime.combine(local.date(), _WINDOWS[kind], BEIJING).astimezone(timezone.utc)
            latest = max((item for item in jobs if datetime.fromisoformat(item.created_at) >= probe_opened),
                         key=lambda item: item.created_at, default=None)
            if latest is None:
                due.append((kind, None))
            elif (latest.status not in {OperationStatus.PASS, OperationStatus.CANCELLED}
                  and now - datetime.fromisoformat(latest.completed_at or latest.created_at) >= _RETRY_AFTER[kind]):
                due.append((kind, None))
            continue
        target = target_day(calendar, kind, now)
        if target is None:
            # One daily job can repair a missing calendar.  Other source jobs
            # wait for a verified day instead of guessing from weekdays.
            if kind != OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE or calendar is not None:
                continue
            latest = max(jobs, key=lambda item: item.created_at, default=None)
            if latest is None or now - datetime.fromisoformat(latest.created_at) >= timedelta(hours=12):
                due.append((kind, None))
            continue
        day, opened = target
        relevant = [item for item in jobs if datetime.fromisoformat(item.created_at) >= opened]
        if not relevant:
            due.append((kind, day))
            continue
        latest = max(relevant, key=lambda item: item.created_at)
        if latest.status in {OperationStatus.PASS, OperationStatus.CANCELLED}:
            continue
        if latest.status == OperationStatus.FAILED and latest.detail == "process stopped before operation completed":
            due.append((kind, day))
            continue
        retry_from = latest.completed_at or latest.created_at
        if now - datetime.fromisoformat(retry_from) >= _retry_after(kind, latest, jobs, calendar, day):
            due.append((kind, day))
    return due


def due_rank_continuation(calendar: FuturesTradingCalendar | None,
                          operations: Iterable[Operation], now: datetime) -> bool:
    """Check only an advancing DCE batch without scanning local TDX files."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if calendar is None:
        return False
    history = list(operations)
    kind = OperationKind.FUTURES_MEMBER_RANK_UPDATE
    jobs = [item for item in history if item.kind == kind]
    if not jobs or any(item.status in _ACTIVE for item in jobs):
        return False
    latest = max(jobs, key=lambda item: item.created_at)
    target = target_day(calendar, kind, now)
    if (target is None or latest.status != OperationStatus.PARTIAL_FAILURE
            or _rank_continuation(latest.detail) is None
            or _retry_after(kind, latest, jobs, calendar, target[0]) != _RANK_CONTINUATION_RETRY):
        return False
    retry_from = datetime.fromisoformat(latest.completed_at or latest.created_at)
    if now - retry_from < _RANK_CONTINUATION_RETRY:
        return False
    return any(item_kind == kind for item_kind, _ in due_operations(calendar, history, now))


__all__ = ("AUTO_KINDS", "CHECK_INTERVAL_SECONDS", "due_operations", "target_day",
           "RANK_CONTINUATION_CHECK_INTERVAL_SECONDS", "due_rank_continuation",
           "member_rank_continuation_marker")
