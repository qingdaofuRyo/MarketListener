"""A serial, allow-listed operation queue for local write actions."""

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
    MARKET_UPDATE = "MARKET_UPDATE"
    F10_UPDATE_CN = "F10_UPDATE_CN"
    F10_UPDATE_HK = "F10_UPDATE_HK"
    REVENUE_UPDATE = "REVENUE_UPDATE"
    REPORT_PROCESS = "REPORT_PROCESS"
    REPORT_VERIFY = "REPORT_VERIFY"
    CHAIN_REBUILD = "CHAIN_REBUILD"
    ATLAS_BUILD = "ATLAS_BUILD"
    ANDROID_PACKAGE_BUILD = "ANDROID_PACKAGE_BUILD"
    STATUS_REFRESH = "STATUS_REFRESH"
    TDX_COVERAGE_CHECK = "TDX_COVERAGE_CHECK"
    TDX_AFTERHOURS_DOWNLOAD = "TDX_AFTERHOURS_DOWNLOAD"
    TDX_LOCAL_IMPORT = "TDX_LOCAL_IMPORT"
    FUTURES_HEAT_UPDATE = "FUTURES_HEAT_UPDATE"
    FUTURES_OFFICIAL_DAILY_UPDATE = "FUTURES_OFFICIAL_DAILY_UPDATE"
    FUTURES_STRUCTURE_UPDATE = "FUTURES_STRUCTURE_UPDATE"


class OperationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PASS = "PASS"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_ACTIVE = {OperationStatus.QUEUED, OperationStatus.RUNNING}


@dataclass
class Operation:
    operation_id: str
    kind: OperationKind
    status: OperationStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


OperationHandler = Callable[[], str | None]


class OperationManager:
    """Persisted FIFO queue.  A kind can occur at most once while active."""

    def __init__(self, data_root: Path, handlers: dict[OperationKind, OperationHandler], *, event_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.data_root = Path(data_root)
        self.handlers = handlers
        self.event_sink = event_sink
        self._lock = threading.RLock()
        self._operations: list[Operation] = self._load()
        self._worker: threading.Thread | None = None

    def list(self) -> list[Operation]:
        with self._lock:
            return list(reversed(self._operations))

    def submit(self, kind: OperationKind) -> tuple[Operation, bool]:
        with self._lock:
            for operation in self._operations:
                if operation.kind == kind and operation.status in _ACTIVE:
                    return operation, False
            operation = Operation(uuid4().hex, kind, OperationStatus.QUEUED, _now())
            self._operations.append(operation)
            self._save()
            self._emit(operation)
            self._ensure_worker()
            return operation, True

    def cancel(self, operation_id: str) -> Operation | None:
        with self._lock:
            operation = next((item for item in self._operations if item.operation_id == operation_id), None)
            if operation is None:
                return None
            if operation.status == OperationStatus.QUEUED:
                operation.status = OperationStatus.CANCELLED
                operation.completed_at = _now()
                self._save()
                self._emit(operation)
            return operation

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="marketlistener-operations", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                next_operation = next((item for item in self._operations if item.status == OperationStatus.QUEUED), None)
                if next_operation is None:
                    return
                next_operation.status = OperationStatus.RUNNING
                next_operation.started_at = _now()
                self._save()
                self._emit(next_operation)
            try:
                detail = self.handlers[next_operation.kind]()
                status = OperationStatus.PARTIAL_FAILURE if detail and "PARTIAL_FAILURE" in detail else OperationStatus.PASS
            except Exception as error:  # service exceptions become explicit operation failures
                detail, status = str(error), OperationStatus.FAILED
            with self._lock:
                next_operation.status = status
                next_operation.detail = detail
                next_operation.completed_at = _now()
                self._save()
                self._emit(next_operation)

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
                )
            except (KeyError, ValueError, TypeError):
                continue
            if operation.status == OperationStatus.RUNNING:
                operation.status = OperationStatus.FAILED
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
    if kind in {OperationKind.F10_UPDATE_CN, OperationKind.F10_UPDATE_HK, OperationKind.REVENUE_UPDATE}:
        return "F10"
    if kind in {OperationKind.REPORT_PROCESS, OperationKind.REPORT_VERIFY}:
        return "Report"
    if kind in {OperationKind.CHAIN_REBUILD, OperationKind.ATLAS_BUILD}:
        return "Industry"
    if kind == OperationKind.ANDROID_PACKAGE_BUILD:
        return "Android"
    if kind in {
        OperationKind.TDX_AFTERHOURS_DOWNLOAD,
        OperationKind.TDX_LOCAL_IMPORT,
        OperationKind.FUTURES_HEAT_UPDATE,
        OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationKind.FUTURES_STRUCTURE_UPDATE,
    }:
        return "Market"
    return "Quality"


__all__ = ("Operation", "OperationKind", "OperationManager", "OperationStatus")
