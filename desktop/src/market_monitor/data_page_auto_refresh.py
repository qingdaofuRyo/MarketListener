"""Conservative after-close scheduling for data-page sources."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .operations import Operation, OperationKind, OperationStatus


BEIJING = ZoneInfo("Asia/Shanghai")
AUTO_KINDS = (OperationKind.EQUITY_DASHBOARD_UPDATE, OperationKind.DISCLOSURES_UPDATE,
              OperationKind.A_SHARE_DELISTED_UPDATE)
_WINDOWS = {OperationKind.EQUITY_DASHBOARD_UPDATE: time(19, 0),
            OperationKind.DISCLOSURES_UPDATE: time(20, 30),
            OperationKind.A_SHARE_DELISTED_UPDATE: time(19, 0)}
_RETRY_AFTER = {OperationKind.EQUITY_DASHBOARD_UPDATE: timedelta(hours=4),
                OperationKind.DISCLOSURES_UPDATE: timedelta(hours=6),
                OperationKind.A_SHARE_DELISTED_UPDATE: timedelta(hours=4)}


def data_target(kind: OperationKind, now: datetime) -> tuple[str, datetime] | None:
    if now.tzinfo is None or kind not in AUTO_KINDS:
        raise ValueError("Invalid data-page schedule request")
    local = now.astimezone(BEIJING)
    day = local.date()
    opened = datetime.combine(day, _WINDOWS[kind], BEIJING)
    if opened > local:
        day -= timedelta(days=1)
        opened = datetime.combine(day, _WINDOWS[kind], BEIJING)
    return day.isoformat(), opened.astimezone(timezone.utc)


def due_data_updates(operations: Iterable[Operation], now: datetime) -> list[OperationKind]:
    if now.tzinfo is None:
        raise ValueError("timezone required")
    now_utc = now.astimezone(timezone.utc)
    history = list(operations)
    due = []
    for kind in AUTO_KINDS:
        target = data_target(kind, now)
        if target is None:
            continue
        if any(item.kind == kind and item.status in {OperationStatus.QUEUED, OperationStatus.RUNNING}
               for item in history):
            continue
        _, opened = target
        relevant = []
        invalid_time = False
        for item in history:
            if item.kind != kind:
                continue
            try:
                created = datetime.fromisoformat(item.created_at)
                if created.tzinfo is None:
                    raise ValueError("naive timestamp")
                if created.astimezone(timezone.utc) >= opened:
                    relevant.append((created.astimezone(timezone.utc), item))
            except (ValueError, TypeError, OverflowError):
                invalid_time = True
                break
        if invalid_time:
            continue
        if not relevant:
            due.append(kind)
            continue
        latest = max(relevant, key=lambda entry: entry[0])[1]
        if latest.status in {OperationStatus.PASS, OperationStatus.CANCELLED}:
            continue
        if latest.status == OperationStatus.FAILED and latest.detail == "process stopped before operation completed":
            due.append(kind)
            continue
        try:
            retry_from = datetime.fromisoformat(latest.completed_at or latest.created_at)
            if retry_from.tzinfo is None:
                raise ValueError("naive timestamp")
            elapsed = now_utc - retry_from.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if elapsed >= _RETRY_AFTER[kind]:
            due.append(kind)
    return due


__all__ = ("AUTO_KINDS", "data_target", "due_data_updates")
