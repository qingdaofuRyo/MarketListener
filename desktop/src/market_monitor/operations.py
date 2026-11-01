"""Allow-listed market-write queue with an isolated terminal GUI lane."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class OperationKind(StrEnum):
    BACKUP_CREATE = "BACKUP_CREATE"
    MARKET_READMODEL_PUBLISH = "MARKET_READMODEL_PUBLISH"
    MARKET_UPDATE = "MARKET_UPDATE"
    F10_UPDATE_CN = "F10_UPDATE_CN"
    F10_UPDATE_HK = "F10_UPDATE_HK"
    REVENUE_UPDATE = "REVENUE_UPDATE"
    F10_REVENUE_REPUBLISH = "F10_REVENUE_REPUBLISH"
    REPORT_PROCESS = "REPORT_PROCESS"
    REPORT_VERIFY = "REPORT_VERIFY"
    CHAIN_REBUILD = "CHAIN_REBUILD"
    ATLAS_BUILD = "ATLAS_BUILD"
    ANDROID_PACKAGE_BUILD = "ANDROID_PACKAGE_BUILD"
    STATUS_REFRESH = "STATUS_REFRESH"
    TDX_COVERAGE_CHECK = "TDX_COVERAGE_CHECK"
    TDX_AFTERHOURS_DOWNLOAD = "TDX_AFTERHOURS_DOWNLOAD"
    TDX_FINANCIAL_LAUNCH = "TDX_FINANCIAL_LAUNCH"
    TDX_FINANCIAL_AUTO_DOWNLOAD = "TDX_FINANCIAL_AUTO_DOWNLOAD"
    TDX_FUTURES_LAUNCH = "TDX_FUTURES_LAUNCH"
    TDX_FUTURES_AUTO_DOWNLOAD = "TDX_FUTURES_AUTO_DOWNLOAD"
    TDX_LOCAL_IMPORT = "TDX_LOCAL_IMPORT"
    FUTURES_HEAT_UPDATE = "FUTURES_HEAT_UPDATE"
    FUTURES_OFFICIAL_DAILY_UPDATE = "FUTURES_OFFICIAL_DAILY_UPDATE"
    FUTURES_CONTRACT_INFO_UPDATE = "FUTURES_CONTRACT_INFO_UPDATE"
    FUTURES_SETTLEMENT_PARAMS_UPDATE = "FUTURES_SETTLEMENT_PARAMS_UPDATE"
    FUTURES_MARKET_FINANCIAL_UPDATE = "FUTURES_MARKET_FINANCIAL_UPDATE"
    FUTURES_MEMBER_RANK_UPDATE = "FUTURES_MEMBER_RANK_UPDATE"
    FUTURES_STRUCTURE_UPDATE = "FUTURES_STRUCTURE_UPDATE"
    EQUITY_DASHBOARD_UPDATE = "EQUITY_DASHBOARD_UPDATE"
    A_SHARE_DELISTED_UPDATE = "A_SHARE_DELISTED_UPDATE"
    DISCLOSURES_UPDATE = "DISCLOSURES_UPDATE"


class OperationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PASS = "PASS"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_ACTIVE = {OperationStatus.QUEUED, OperationStatus.RUNNING}
_GUI_KINDS = {
    OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
    OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
}
_GUI_EXCLUSIVE_MAIN_KINDS = {
    OperationKind.TDX_LOCAL_IMPORT,
    OperationKind.BACKUP_CREATE,
    OperationKind.TDX_AFTERHOURS_DOWNLOAD,
    OperationKind.TDX_FINANCIAL_LAUNCH,
    OperationKind.TDX_FUTURES_LAUNCH,
}
_DAILY_REFRESH_KINDS = {
    OperationKind.MARKET_UPDATE,
    OperationKind.EQUITY_DASHBOARD_UPDATE,
    OperationKind.A_SHARE_DELISTED_UPDATE,
    OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
    OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE,
}


@dataclass
class Operation:
    operation_id: str
    kind: OperationKind
    status: OperationStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    detail: str | None = None
    phase: str = "QUEUED"
    completed_units: int = 0
    total_units: int | None = None
    checkpoint: str | None = None
    cancel_requested: bool = False
    cancel_supported: bool = False
    heartbeat_at: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


OperationHandler = Callable[[], str | None]


class OperationCancelled(RuntimeError):
    """The handler reached a safe checkpoint after cancellation was requested."""


@dataclass(frozen=True)
class ContextOperationHandler:
    run: Callable[["OperationContext"], str | None]


class OperationContext:
    def __init__(self, manager: "OperationManager", operation_id: str) -> None:
        self.manager = manager
        self.operation_id = operation_id

    def progress(self, phase: str, completed: int, total: int | None,
                 *, checkpoint: str | None = None) -> None:
        if (not phase or len(phase) > 80 or completed < 0 or
                total is not None and (total < 0 or completed > total) or
                checkpoint is not None and len(checkpoint) > 256):
            raise ValueError("invalid operation progress")
        with self.manager._lock:
            item = self.manager._find(self.operation_id)
            if item is None or item.status != OperationStatus.RUNNING:
                raise OperationCancelled("任务已结束")
            item.phase = phase
            item.completed_units = completed
            item.total_units = total
            item.checkpoint = checkpoint
            item.heartbeat_at = _now()
            self.manager._save()

    def check_cancelled(self) -> None:
        with self.manager._lock:
            item = self.manager._find(self.operation_id)
            if item is None or item.cancel_requested:
                raise OperationCancelled("已在安全检查点取消")


class OperationManager:
    """Keep market writers serial and each terminal's GUI download independent."""

    def __init__(self, data_root: Path,
                 handlers: dict[OperationKind, OperationHandler | ContextOperationHandler],
                 *, event_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.data_root = Path(data_root)
        self.handlers = handlers
        self.event_sink = event_sink
        self._lock = threading.RLock()
        self._queue_changed = threading.Condition(self._lock)
        self._operations: list[Operation] = self._load()
        self._worker: threading.Thread | None = None
        self._gui_workers: dict[OperationKind, threading.Thread] = {}

    def list(self) -> list[Operation]:
        with self._lock:
            return list(reversed(self._operations))

    def submit(self, kind: OperationKind) -> tuple[Operation, bool]:
        with self._lock:
            for operation in self._operations:
                if operation.kind == kind and operation.status in _ACTIVE:
                    self._ensure_worker()
                    return operation, False
            operation = Operation(uuid4().hex, kind, OperationStatus.QUEUED, _now(),
                                  cancel_supported=isinstance(self.handlers.get(kind), ContextOperationHandler))
            self._operations.append(operation)
            self._save()
            self._emit(operation)
            self._ensure_worker()
            self._queue_changed.notify_all()
            return operation, True

    def cancel(self, operation_id: str) -> Operation | None:
        with self._lock:
            operation = self._find(operation_id)
            if operation is None:
                return None
            if operation.status == OperationStatus.QUEUED:
                operation.status = OperationStatus.CANCELLED
                operation.phase = "CANCELLED"
                operation.completed_at = _now()
                self._save()
                self._emit(operation)
                self._queue_changed.notify_all()
            elif operation.status == OperationStatus.RUNNING:
                if operation.cancel_supported:
                    operation.cancel_requested = True
                    self._save()
            return operation

    def _find(self, operation_id: str) -> Operation | None:
        return next((item for item in self._operations if item.operation_id == operation_id), None)

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="marketlistener-operations", daemon=True)
            self._worker.start()
        for kind in _GUI_KINDS:
            worker = self._gui_workers.get(kind)
            if (any(item.kind == kind and item.status == OperationStatus.QUEUED
                    for item in self._operations)
                    and (worker is None or not worker.is_alive())):
                worker = threading.Thread(target=self._run_gui, args=(kind,),
                                          name=f"marketlistener-gui-{kind.value.lower()}", daemon=True)
                self._gui_workers[kind] = worker
                worker.start()
        self._queue_changed.notify_all()

    def _run(self) -> None:
        self._run_lane(gui_kind=None)

    def _run_gui(self, kind: OperationKind) -> None:
        self._run_lane(gui_kind=kind)

    def _next_operation(self, *, gui_kind: OperationKind | None) -> tuple[Operation | None, bool]:
        pending = [item for item in self._operations if item.status == OperationStatus.QUEUED]
        if gui_kind is not None:
            candidates = [item for item in pending if item.kind == gui_kind]
            if not candidates:
                return None, False
            if any(item.kind in _GUI_EXCLUSIVE_MAIN_KINDS and item.status == OperationStatus.RUNNING
                   for item in self._operations):
                return None, True
            return candidates[0], False

        candidates = [item for item in pending if item.kind not in _GUI_KINDS]
        if not candidates:
            return None, False
        if any(item.kind in _GUI_KINDS and item.status in _ACTIVE for item in self._operations):
            candidates = [item for item in candidates if item.kind not in _GUI_EXCLUSIVE_MAIN_KINDS]
            if not candidates:
                return None, True
        for kind in (OperationKind.MARKET_READMODEL_PUBLISH, OperationKind.BACKUP_CREATE,
                     OperationKind.TDX_FINANCIAL_LAUNCH, OperationKind.TDX_FUTURES_LAUNCH):
            selected = next((item for item in candidates if item.kind == kind), None)
            if selected is not None:
                return selected, False
        selected = next((item for item in candidates if item.kind in {
            OperationKind.TDX_LOCAL_IMPORT, OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE,
        }), None)
        if selected is not None:
            return selected, False
        if candidates[0].kind == OperationKind.FUTURES_MEMBER_RANK_UPDATE:
            # A ranking API batch can take many minutes. Let already-queued
            # daily refreshes publish first, then resume the ranking batch.
            daily = next((item for item in candidates if item.kind in _DAILY_REFRESH_KINDS), None)
            if daily is not None:
                return daily, False
        return candidates[0], False

    def _run_lane(self, *, gui_kind: OperationKind | None) -> None:
        while True:
            with self._queue_changed:
                next_operation, waiting = self._next_operation(gui_kind=gui_kind)
                if next_operation is None:
                    if waiting:
                        self._queue_changed.wait()
                        continue
                    if gui_kind is not None:
                        self._gui_workers.pop(gui_kind, None)
                    else:
                        self._worker = None
                    return
                next_operation.status = OperationStatus.RUNNING
                next_operation.phase = "RUNNING"
                next_operation.started_at = _now()
                next_operation.heartbeat_at = next_operation.started_at
                self._save()
                self._emit(next_operation)
                self._queue_changed.notify_all()
            heartbeat_stop = threading.Event()

            def heartbeat() -> None:
                while not heartbeat_stop.wait(2):
                    with self._lock:
                        if next_operation.status != OperationStatus.RUNNING:
                            return
                        next_operation.heartbeat_at = _now()
                        self._save()

            pulse = threading.Thread(target=heartbeat, name="marketlistener-operation-heartbeat", daemon=True)
            pulse.start()
            try:
                handler = self.handlers[next_operation.kind]
                detail = (handler.run(OperationContext(self, next_operation.operation_id))
                          if isinstance(handler, ContextOperationHandler) else handler())
                status = OperationStatus.PARTIAL_FAILURE if detail and "PARTIAL_FAILURE" in detail else OperationStatus.PASS
            except OperationCancelled as error:
                detail, status = str(error), OperationStatus.CANCELLED
            except Exception as error:  # service exceptions become explicit operation failures
                detail, status = str(error), OperationStatus.FAILED
            finally:
                heartbeat_stop.set()
                pulse.join(timeout=1)
            with self._queue_changed:
                next_operation.status = status
                next_operation.phase = status.value
                next_operation.detail = detail
                next_operation.result = {"status": status.value, "summary": detail,
                                         "cancelRequested": next_operation.cancel_requested,
                                         "cancellationApplied": status == OperationStatus.CANCELLED}
                next_operation.completed_at = _now()
                self._save()
                self._emit(next_operation)
                self._queue_changed.notify_all()

    def _emit(self, operation: Operation) -> None:
        if self.event_sink is None:
            return
        try:
            event = {"operationId": operation.operation_id, "operation": operation.kind, "status": operation.status, "detail": operation.detail}
            self.event_sink({"category": "Operation", **event})
            self.event_sink({"category": _operation_category(operation.kind), "eventType": "Operation", **event})
            if operation.kind == OperationKind.MARKET_UPDATE:
                self.event_sink({"category": "Provider", "eventType": "Operation", **event})
            if operation.status in {OperationStatus.PARTIAL_FAILURE, OperationStatus.FAILED}:
                self.event_sink({"category": "Quality", "eventType": "Operation", **event})
            if operation.status == OperationStatus.FAILED:
                self.event_sink({"category": "Exception", "eventType": "Operation", **event})
        except Exception:
            return

    def _load(self) -> list[Operation]:
        path = self.data_root / "operations.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        loaded: list[Operation] = []
        for row in rows:
            try:
                operation = Operation(
                    operation_id=str(row["operation_id"]), kind=OperationKind(row["kind"]), status=OperationStatus(row["status"]),
                    created_at=str(row["created_at"]), started_at=row.get("started_at"), completed_at=row.get("completed_at"), detail=row.get("detail"),
                    phase=str(row.get("phase") or row["status"]), completed_units=int(row.get("completed_units") or 0),
                    total_units=row.get("total_units"), checkpoint=row.get("checkpoint"),
                    cancel_requested=bool(row.get("cancel_requested", False)), heartbeat_at=row.get("heartbeat_at"),
                    cancel_supported=isinstance(self.handlers.get(OperationKind(row["kind"])), ContextOperationHandler),
                    result=row.get("result"),
                )
            except (KeyError, ValueError, TypeError):
                continue
            if operation.status == OperationStatus.RUNNING:
                operation.status = OperationStatus.FAILED
                operation.phase = "FAILED"
                operation.completed_at = _now()
                operation.detail = "process stopped before operation completed"
            loaded.append(operation)
        return loaded

    def _save(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        target = self.data_root / "operations.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps([item.to_dict() for item in self._operations], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _operation_category(kind: OperationKind) -> str:
    if kind == OperationKind.MARKET_UPDATE:
        return "Market"
    if kind in {OperationKind.F10_UPDATE_CN, OperationKind.F10_UPDATE_HK, OperationKind.REVENUE_UPDATE,
                OperationKind.F10_REVENUE_REPUBLISH}:
        return "F10"
    if kind in {OperationKind.REPORT_PROCESS, OperationKind.REPORT_VERIFY}:
        return "Report"
    if kind in {OperationKind.CHAIN_REBUILD, OperationKind.ATLAS_BUILD}:
        return "Industry"
    if kind == OperationKind.ANDROID_PACKAGE_BUILD:
        return "Android"
    if kind in {
        OperationKind.TDX_AFTERHOURS_DOWNLOAD,
        OperationKind.TDX_FINANCIAL_LAUNCH,
        OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
        OperationKind.TDX_FUTURES_LAUNCH,
        OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
        OperationKind.TDX_LOCAL_IMPORT,
        OperationKind.FUTURES_HEAT_UPDATE,
        OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationKind.FUTURES_CONTRACT_INFO_UPDATE,
        OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE,
        OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE,
        OperationKind.FUTURES_MEMBER_RANK_UPDATE,
        OperationKind.FUTURES_STRUCTURE_UPDATE,
        OperationKind.EQUITY_DASHBOARD_UPDATE,
        OperationKind.A_SHARE_DELISTED_UPDATE,
        OperationKind.DISCLOSURES_UPDATE,
    }:
        return "Market"
    return "Quality"


__all__ = ("ContextOperationHandler", "Operation", "OperationCancelled", "OperationContext",
           "OperationKind", "OperationManager", "OperationStatus")
