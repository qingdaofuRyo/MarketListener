from __future__ import annotations

import threading
from pathlib import Path

from market_monitor.operations import OperationKind, OperationManager, OperationStatus


def test_operation_manager_deduplicates_active_kind_and_persists(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler() -> str:
        entered.set()
        assert release.wait(3)
        return "PASS"

    manager = OperationManager(tmp_path, {OperationKind.MARKET_UPDATE: handler})
    first, created = manager.submit(OperationKind.MARKET_UPDATE)
    assert created is True
    assert entered.wait(3)
    duplicate, created = manager.submit(OperationKind.MARKET_UPDATE)
    assert created is False
    assert duplicate.operation_id == first.operation_id
    release.set()
    for _ in range(30):
        current = manager.list()[0]
        if current.status == OperationStatus.PASS:
            break
        threading.Event().wait(0.02)
    assert manager.list()[0].status == OperationStatus.PASS
    assert OperationManager(tmp_path, {OperationKind.MARKET_UPDATE: handler}).list()[0].status == OperationStatus.PASS


def test_operation_manager_cancels_queued_operation(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def first_handler() -> str:
        entered.set()
        assert release.wait(3)
        return "PASS"

    manager = OperationManager(
        tmp_path,
        {OperationKind.MARKET_UPDATE: first_handler, OperationKind.STATUS_REFRESH: lambda: "PASS"},
    )
    manager.submit(OperationKind.MARKET_UPDATE)
    assert entered.wait(3)
    queued, _ = manager.submit(OperationKind.STATUS_REFRESH)
    assert manager.cancel(queued.operation_id).status == OperationStatus.CANCELLED
    release.set()


def test_operation_manager_emits_operation_and_domain_events(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    manager = OperationManager(
        tmp_path,
        {OperationKind.STATUS_REFRESH: lambda: "PASS"},
        event_sink=events.append,
    )

    manager.submit(OperationKind.STATUS_REFRESH)
    for _ in range(30):
        if any(event.get("category") == "Quality" for event in events):
            break
        threading.Event().wait(0.02)

    assert any(event.get("category") == "Operation" for event in events)
    assert any(event.get("category") == "Quality" for event in events)


def test_market_operation_emits_provider_and_failure_exception_events(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    manager = OperationManager(tmp_path, {OperationKind.MARKET_UPDATE: lambda: (_ for _ in ()).throw(RuntimeError("provider timeout"))}, event_sink=events.append)

    manager.submit(OperationKind.MARKET_UPDATE)
    for _ in range(30):
        if any(event.get("category") == "Exception" for event in events):
            break
        threading.Event().wait(0.02)

    assert {"Market", "Provider", "Quality", "Exception"}.issubset({str(event.get("category")) for event in events})


def test_tdx_operations_emit_download_market_and_coverage_quality_events(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    manager = OperationManager(
        tmp_path,
        {
            OperationKind.TDX_AFTERHOURS_DOWNLOAD: lambda: "PASS: synthetic download",
            OperationKind.TDX_COVERAGE_CHECK: lambda: "PASS: synthetic coverage check",
        },
        event_sink=events.append,
    )

    download, _ = manager.submit(OperationKind.TDX_AFTERHOURS_DOWNLOAD)
    coverage, _ = manager.submit(OperationKind.TDX_COVERAGE_CHECK)
    for _ in range(30):
        completed = {operation.operation_id: operation.status for operation in manager.list()}
        if completed.get(download.operation_id) == OperationStatus.PASS and completed.get(coverage.operation_id) == OperationStatus.PASS:
            break
        threading.Event().wait(0.02)

    assert {operation.operation_id: operation.status for operation in manager.list()} == {
        coverage.operation_id: OperationStatus.PASS,
        download.operation_id: OperationStatus.PASS,
    }
    categories = {
        operation_id: {str(event.get("category")) for event in events if event.get("operationId") == operation_id}
        for operation_id in (download.operation_id, coverage.operation_id)
    }
    assert categories[download.operation_id] == {"Operation", "Market"}
    assert categories[coverage.operation_id] == {"Operation", "Quality"}
