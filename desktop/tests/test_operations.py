from __future__ import annotations

import threading
import json
from pathlib import Path
from fastapi.testclient import TestClient

from market_monitor.operations import ContextOperationHandler, OperationKind, OperationManager, OperationStatus


def test_restart_resumes_existing_queue_and_prioritizes_local_import(tmp_path: Path) -> None:
    operations = [{"operation_id": "remote", "kind": "MARKET_UPDATE", "status": "QUEUED",
                   "created_at": "2026-10-07T00:00:00+00:00"},
                  {"operation_id": "local", "kind": "TDX_LOCAL_IMPORT", "status": "QUEUED",
                   "created_at": "2026-10-07T00:00:01+00:00"}]
    (tmp_path / "operations.json").write_text(json.dumps(operations), encoding="utf-8")
    order = []
    completed = threading.Event()

    def remote():
        order.append("remote")
        completed.set()
        return "PASS"

    manager = OperationManager(tmp_path, {
        OperationKind.MARKET_UPDATE: remote,
        OperationKind.TDX_LOCAL_IMPORT: lambda: order.append("local") or "PASS",
    })
    existing, created = manager.submit(OperationKind.TDX_LOCAL_IMPORT)
    assert not created and existing.operation_id == "local"
    assert completed.wait(3)
    assert order == ["local", "remote"]


def test_compound_futures_update_precedes_queued_remote_work(tmp_path: Path) -> None:
    operations = [{"operation_id": "remote", "kind": "MARKET_UPDATE", "status": "QUEUED",
                   "created_at": "2026-10-09T00:00:00+00:00"},
                  {"operation_id": "futures", "kind": "FUTURES_MARKET_FINANCIAL_UPDATE", "status": "QUEUED",
                   "created_at": "2026-10-09T00:00:01+00:00"}]
    (tmp_path / "operations.json").write_text(json.dumps(operations), encoding="utf-8")
    order = []
    completed = threading.Event()

    def remote():
        order.append("remote")
        completed.set()
        return "PASS"

    manager = OperationManager(tmp_path, {
        OperationKind.MARKET_UPDATE: remote,
        OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE: lambda: order.append("futures") or "PASS",
    })
    _, created = manager.submit(OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE)
    assert created is False
    assert completed.wait(3)
    assert order == ["futures", "remote"]


def test_queued_daily_refresh_runs_before_slow_member_rank_batch(tmp_path: Path) -> None:
    kinds = ["FUTURES_MEMBER_RANK_UPDATE", "MARKET_UPDATE", "STATUS_REFRESH"]
    rows = [{"operation_id": kind, "kind": kind, "status": "QUEUED",
             "created_at": f"2026-10-27T00:00:0{index}+00:00"}
            for index, kind in enumerate(kinds)]
    (tmp_path / "operations.json").write_text(json.dumps(rows), encoding="utf-8")
    order: list[str] = []
    completed = threading.Event()

    def task(kind: str):
        def run() -> str:
            order.append(kind)
            if kind == "STATUS_REFRESH":
                completed.set()
            return "PASS"
        return run

    manager = OperationManager(tmp_path, {OperationKind(kind): task(kind) for kind in kinds})
    _, created = manager.submit(OperationKind.FUTURES_MEMBER_RANK_UPDATE)
    assert not created
    assert completed.wait(3)
    assert order == ["MARKET_UPDATE", "FUTURES_MEMBER_RANK_UPDATE", "STATUS_REFRESH"]


def test_due_gui_downloads_exclude_import_and_backup(tmp_path: Path) -> None:
    kinds = ["MARKET_UPDATE", "BACKUP_CREATE", "TDX_LOCAL_IMPORT",
             "TDX_FINANCIAL_AUTO_DOWNLOAD", "TDX_FUTURES_AUTO_DOWNLOAD"]
    rows = [{"operation_id": kind, "kind": kind, "status": "QUEUED",
             "created_at": f"2026-10-22T00:00:0{index}+00:00"}
            for index, kind in enumerate(kinds)]
    (tmp_path / "operations.json").write_text(json.dumps(rows), encoding="utf-8")
    order: list[str] = []
    futures_started = threading.Event()
    financial_started = threading.Event()
    release_futures = threading.Event()
    release_financial = threading.Event()
    gui_done = threading.Event()
    import_done = threading.Event()

    def task(kind: str):
        def run() -> str:
            order.append(kind)
            if kind == "TDX_FUTURES_AUTO_DOWNLOAD":
                futures_started.set()
                assert release_futures.wait(3)
            elif kind == "TDX_FINANCIAL_AUTO_DOWNLOAD":
                financial_started.set()
                assert release_financial.wait(3)
                gui_done.set()
            elif kind in {"TDX_LOCAL_IMPORT", "BACKUP_CREATE"}:
                assert gui_done.is_set()
                if kind == "TDX_LOCAL_IMPORT":
                    import_done.set()
            return "PASS"
        return run

    handlers = {OperationKind(kind): task(kind) for kind in kinds}
    manager = OperationManager(tmp_path, handlers)
    manager.submit(OperationKind.TDX_FUTURES_AUTO_DOWNLOAD)

    assert futures_started.wait(3)
    assert financial_started.wait(3)
    assert "TDX_LOCAL_IMPORT" not in order
    assert "BACKUP_CREATE" not in order
    release_futures.set()
    assert "TDX_LOCAL_IMPORT" not in order
    assert "BACKUP_CREATE" not in order
    release_financial.set()
    assert import_done.wait(3)
    assert order.index("TDX_FUTURES_AUTO_DOWNLOAD") < order.index("BACKUP_CREATE")
    assert order.index("TDX_FINANCIAL_AUTO_DOWNLOAD") < order.index("BACKUP_CREATE")
    assert order.index("BACKUP_CREATE") < order.index("TDX_LOCAL_IMPORT")


def test_gui_download_runs_while_member_rank_is_running(tmp_path: Path) -> None:
    rank_started = threading.Event()
    release_rank = threading.Event()
    gui_done = threading.Event()

    def rank() -> str:
        rank_started.set()
        assert release_rank.wait(3)
        return "PASS"

    manager = OperationManager(tmp_path, {
        OperationKind.FUTURES_MEMBER_RANK_UPDATE: rank,
        OperationKind.TDX_FUTURES_AUTO_DOWNLOAD: lambda: gui_done.set() or "PASS",
    })
    manager.submit(OperationKind.FUTURES_MEMBER_RANK_UPDATE)
    assert rank_started.wait(3)
    manager.submit(OperationKind.TDX_FUTURES_AUTO_DOWNLOAD)
    assert gui_done.wait(3)
    assert any(item.kind == OperationKind.FUTURES_MEMBER_RANK_UPDATE
               and item.status == OperationStatus.RUNNING for item in manager.list())
    for _ in range(100):
        if any(item.kind == OperationKind.TDX_FUTURES_AUTO_DOWNLOAD
               and item.status == OperationStatus.PASS for item in manager.list()):
            break
        threading.Event().wait(0.01)
    assert any(item.kind == OperationKind.TDX_FUTURES_AUTO_DOWNLOAD
               and item.status == OperationStatus.PASS for item in manager.list())
    release_rank.set()


def test_gui_download_waits_for_running_import_or_backup(tmp_path: Path) -> None:
    for blocking_kind in (OperationKind.TDX_LOCAL_IMPORT, OperationKind.BACKUP_CREATE):
        root = tmp_path / blocking_kind.value
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        gui_done = threading.Event()

        def blocker() -> str:
            blocker_started.set()
            assert release_blocker.wait(3)
            return "PASS"

        manager = OperationManager(root, {
            blocking_kind: blocker,
            OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD: lambda: gui_done.set() or "PASS",
        })
        manager.submit(blocking_kind)
        assert blocker_started.wait(3)
        manager.submit(OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD)
        assert not gui_done.wait(0.05)
        release_blocker.set()
        assert gui_done.wait(3)


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


def test_running_operation_reports_checkpoint_and_cancels_at_safe_boundary(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler(context) -> str:
        context.progress("来源采集", 1, 3, checkpoint="source-1")
        entered.set()
        assert release.wait(3)
        context.check_cancelled()
        raise AssertionError("cancelled handler must not start the next phase")

    manager = OperationManager(tmp_path, {
        OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE: ContextOperationHandler(handler),
    })
    operation, _ = manager.submit(OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE)
    assert entered.wait(3)
    running = manager.cancel(operation.operation_id)
    assert running is not None and running.status == OperationStatus.RUNNING
    assert running.cancel_requested and running.phase == "来源采集"
    assert (running.completed_units, running.total_units, running.checkpoint) == (1, 3, "source-1")
    release.set()
    for _ in range(50):
        if manager.list()[0].status == OperationStatus.CANCELLED:
            break
        threading.Event().wait(.02)
    finished = manager.list()[0]
    assert finished.status == OperationStatus.CANCELLED
    assert finished.result == {"status": "CANCELLED", "summary": "已在安全检查点取消",
                               "cancelRequested": True, "cancellationApplied": True}
    assert OperationManager(tmp_path, {}).list()[0].checkpoint == "source-1"


def test_running_legacy_handler_does_not_claim_cancellation_support(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler() -> str:
        entered.set()
        assert release.wait(3)
        return "PASS"

    manager = OperationManager(tmp_path, {OperationKind.MARKET_UPDATE: handler})
    operation, _ = manager.submit(OperationKind.MARKET_UPDATE)
    assert entered.wait(3)
    current = manager.cancel(operation.operation_id)
    assert current is not None and not current.cancel_supported and not current.cancel_requested
    from market_monitor.web_app import create_web_app

    with TestClient(create_web_app(tmp_path, operation_manager=manager),
                    base_url="http://127.0.0.1:8765", client=("127.0.0.1", 42000)) as http:
        assert http.post(f"/api/operations/{operation.operation_id}/cancel").status_code == 409
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
