"""Bounded F10 refresh scheduling; report dates remain source facts."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .operations import Operation, OperationKind, OperationStatus

_BEIJING = ZoneInfo("Asia/Shanghai")
_KINDS = (OperationKind.F10_UPDATE_CN, OperationKind.F10_UPDATE_HK, OperationKind.REVENUE_UPDATE)
_REVENUE_BACKLOG_TIMES = tuple(time(hour) for hour in range(8, 23, 2))


def due_revenue_publication_recovery(operations: Iterable[Operation], now: datetime, *, pending: bool) -> bool:
    """Schedule local-only recovery with deduplication and a 30-minute retry delay."""
    if now.tzinfo is None:
        raise ValueError("timezone required")
    if not pending:
        return False
    items = list(operations)
    related = {OperationKind.REVENUE_UPDATE, OperationKind.F10_REVENUE_REPUBLISH}
    if any(item.kind in related and item.status in {OperationStatus.QUEUED, OperationStatus.RUNNING}
           for item in items):
        return False
    recovery = [item for item in items if item.kind == OperationKind.F10_REVENUE_REPUBLISH]
    if not recovery:
        return True
    try:
        latest = max(recovery, key=lambda item: _timestamp(item.created_at))
        return now.astimezone(timezone.utc) - _timestamp(latest.completed_at) >= timedelta(minutes=30)
    except (TypeError, ValueError, OverflowError):
        return False


def _last_slot(local: datetime, times: tuple[time, ...]) -> datetime:
    today = [datetime.combine(local.date(), value, _BEIJING) for value in times]
    previous = [value for value in today if value <= local]
    return (previous[-1] if previous else today[-1] - timedelta(days=1)).astimezone(timezone.utc)


def _timestamp(value: str | None) -> datetime:
    parsed = datetime.fromisoformat(value or "")
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def _latest_revenue(items: list[Operation]) -> Operation | None:
    rows = [item for item in items if item.kind == OperationKind.REVENUE_UPDATE]
    return max(rows, key=lambda item: _timestamp(item.created_at)) if rows else None


def _summary_matches(item: Operation, summary: Mapping[str, Any]) -> bool:
    return _timestamp(item.started_at) <= _timestamp(summary.get("completedAt")) <= _timestamp(item.completed_at)


def _revenue_can_start(items: list[Operation], summary: Mapping[str, Any] | None, now: datetime) -> bool:
    try:
        latest = _latest_revenue(items)
        if latest is None:
            return True
        elapsed = now.astimezone(timezone.utc) - _timestamp(latest.completed_at)
        if elapsed < timedelta(minutes=90):
            return False
        blocked = latest.status in {OperationStatus.FAILED, OperationStatus.CANCELLED}
        if summary and _summary_matches(latest, summary):
            failed, updated, remaining = summary.get("failed"), summary.get("updated"), summary.get("remaining")
            if all(type(value) is int and value >= 0 for value in (failed, updated, remaining)):
                blocked = blocked or (remaining > 0 and (failed >= 8 or updated == 0))
        return not blocked or elapsed >= timedelta(hours=12)
    except (TypeError, ValueError, OverflowError):
        return False


def _can_accelerate_revenue(items: list[Operation], summary: Mapping[str, Any] | None) -> bool:
    """Accelerate only a verified, productive last batch; back off on source failures."""
    if not summary or summary.get("status") not in {"PARTIAL", "PARTIAL_FAILURE"}:
        return False
    remaining, updated, failed = summary.get("remaining"), summary.get("updated"), summary.get("failed")
    if any(type(value) is not int or value < 0 for value in (remaining, updated, failed)):
        return False
    if remaining == 0 or updated == 0 or failed >= 8:
        return False
    revenue = [item for item in items if item.kind == OperationKind.REVENUE_UPDATE]
    if not revenue or any(item.status in {OperationStatus.RUNNING, OperationStatus.QUEUED} for item in revenue):
        return False
    try:
        latest = _latest_revenue(items)
        if latest is None:
            return False
        if latest.status not in {OperationStatus.PASS, OperationStatus.PARTIAL_FAILURE}:
            return False
        return _summary_matches(latest, summary)
    except (TypeError, ValueError, OverflowError):
        return False


def due_f10_updates(operations: Iterable[Operation], now: datetime, *,
                    revenue_summary: Mapping[str, Any] | None = None) -> list[OperationKind]:
    """Daily F10 checks; productive revenue backlogs use bounded two-hour slots."""
    if now.tzinfo is None:
        raise ValueError("timezone required")
    local = now.astimezone(_BEIJING)
    items = list(operations)
    daily_slot = _last_slot(local, (time(19, 30),))
    revenue_slot = (_last_slot(local, _REVENUE_BACKLOG_TIMES)
                    if _can_accelerate_revenue(items, revenue_summary) else daily_slot)
    due = []
    for kind in _KINDS:
        if kind == OperationKind.REVENUE_UPDATE and not _revenue_can_start(items, revenue_summary, now):
            continue
        slot = revenue_slot if kind == OperationKind.REVENUE_UPDATE else daily_slot
        attempted = False
        for item in items:
            if item.kind != kind:
                continue
            if item.status in {OperationStatus.RUNNING, OperationStatus.QUEUED}:
                attempted = True
                break
            try:
                created = datetime.fromisoformat(item.created_at)
                if created.tzinfo is None:
                    raise ValueError("naive operation time")
                if created.astimezone(timezone.utc) >= slot:
                    attempted = True
                    break
            except (TypeError, ValueError):
                attempted = True  # Unknown timing must not cause repeated provider requests.
                break
        if not attempted:
            due.append(kind)
    return due
