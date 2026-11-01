"""Regression tests for the FastAPI host and fixed terminal routes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from market_monitor import web_app
from market_monitor.operations import Operation, OperationKind, OperationManager, OperationStatus
from market_monitor.web_app import _operation_handlers, create_web_app


def _f10_fixture(data_root: Path) -> None:
    directory = data_root / "industry" / "f10"
    directory.mkdir(parents=True)
    row = {
        "code": "600519",
        "market": "CN",
        "name": "贵州茅台",
        "profile": "本地 F10 样本",
        "source": "eastmoney_f10",
        "fetched_at": "2026-08-09T10:00:00+08:00",
    }
    (directory / "cn_f10.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    (directory / "hk_f10.jsonl").write_text("", encoding="utf-8")


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<main id=app>MarketListener</main>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    return dist


def test_fastapi_shell_has_exact_routes_and_industry_v2_redirect(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data", web_dist=_dist(tmp_path)))

    for route in (
        "/",
        "/data/",
        "/f10/",
        "/industry/",
        "/logs/",
        "/market/all/",
        "/market/targets/",
        "/market/instrument/CN.CFFEX.FUTURE.IFMAIN",
        "/f10/company/CN.SSE.STOCK.600519",
    ):
        response = client.get(route)
        assert response.status_code == 200
        assert "MarketListener" in response.text

    redirect = client.get("/industry-v2/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/industry/"


def test_readiness_and_favicon_routes(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data", web_dist=_dist(tmp_path)))

    assert client.get("/api/ready").json() == {"status": "READY"}
    icon = client.get("/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    legacy = client.get("/favicon.ico", follow_redirects=False)
    assert legacy.status_code == 307
    assert legacy.headers["location"] == "/favicon.svg"


def test_startup_auto_update_queues_bounded_f10_and_market_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_app, "due_local_market_updates", lambda *_args: [
        OperationKind.TDX_LOCAL_IMPORT, OperationKind.MARKET_UPDATE,
    ])
    monkeypatch.setattr(web_app, "due_financial_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_futures_download", lambda *_args: False)
    class RecordingOperations:
        def __init__(self) -> None:
            self.submitted: list[OperationKind] = []

        def submit(self, kind: OperationKind):
            self.submitted.append(kind)
            return None, True

        def list(self):
            return []

    operations = RecordingOperations()
    app = create_web_app(
        tmp_path / "data",
        web_dist=_dist(tmp_path),
        operation_manager=operations,  # type: ignore[arg-type]
        auto_update_on_startup=True,
    )
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 200

    assert operations.submitted == [
        OperationKind.TDX_LOCAL_IMPORT,
        OperationKind.EQUITY_DASHBOARD_UPDATE,
        OperationKind.DISCLOSURES_UPDATE,
        OperationKind.A_SHARE_DELISTED_UPDATE,
        OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationKind.F10_UPDATE_CN,
        OperationKind.F10_UPDATE_HK,
        OperationKind.REVENUE_UPDATE,
        OperationKind.MARKET_UPDATE,
        OperationKind.FUTURES_CONTRACT_INFO_UPDATE,
    ]


def test_startup_pending_revenue_publication_queues_recovery_before_fetch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "due_financial_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_futures_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_local_market_updates", lambda *_args: [])
    class RecordingOperations:
        def __init__(self): self.submitted = []
        def list(self): return []
        def submit(self, kind):
            self.submitted.append(kind)
            return None, True
    root = tmp_path / "data"
    marker = root / "f10" / "cn" / "revenue_publication_pending.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    operations = RecordingOperations()
    app = create_web_app(root, web_dist=_dist(tmp_path), operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=True)
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 200
    assert OperationKind.F10_REVENUE_REPUBLISH in operations.submitted
    assert OperationKind.REVENUE_UPDATE not in operations.submitted


def test_startup_download_defers_tdx_import_until_gui_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_app, "due_financial_download", lambda *_args: True)
    monkeypatch.setattr(web_app, "due_futures_download", lambda *_args: True)
    monkeypatch.setattr(web_app, "due_local_market_updates", lambda *_args: [OperationKind.TDX_LOCAL_IMPORT])

    class RecordingOperations:
        submitted: list[OperationKind] = []

        def list(self) -> list[Operation]:
            return []

        def submit(self, kind: OperationKind) -> tuple[None, bool]:
            self.submitted.append(kind)
            return None, True

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=True)
    with TestClient(app):
        pass
    assert operations.submitted[:2] == [OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
                                       OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD]
    assert OperationKind.TDX_LOCAL_IMPORT not in operations.submitted
    assert OperationKind.TDX_FINANCIAL_LAUNCH not in operations.submitted
    assert OperationKind.TDX_FUTURES_LAUNCH not in operations.submitted


def test_due_gui_download_is_queued_even_while_silver_recovery_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import market_data_version

    entered = threading.Event()
    release = threading.Event()

    def slow_recovery(_root):
        entered.set()
        assert release.wait(3)
        return {"status": "RECOVERED"}

    monkeypatch.setattr(market_data_version, "pending_market_writes",
                        lambda _root: {"status": "PENDING"})
    monkeypatch.setattr(market_data_version, "recover_abandoned_pending_write", slow_recovery)
    monkeypatch.setattr(web_app, "due_financial_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_futures_download", lambda *_args: True)

    class RecordingOperations:
        def __init__(self) -> None:
            self.submitted: list[OperationKind] = []

        def list(self) -> list[Operation]:
            return []

        def submit(self, kind: OperationKind) -> tuple[None, bool]:
            self.submitted.append(kind)
            return None, True

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=True)
    try:
        with TestClient(app):
            assert entered.wait(3)
            assert operations.submitted[0] == OperationKind.TDX_FUTURES_AUTO_DOWNLOAD
    finally:
        release.set()


def test_startup_import_waits_for_existing_gui_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import local_backup

    monkeypatch.setattr(web_app, "due_financial_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_futures_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_local_market_updates", lambda *_args: [OperationKind.TDX_LOCAL_IMPORT])
    monkeypatch.setattr(local_backup, "profile_backup_due", lambda *_args: True)

    class RecordingOperations:
        def __init__(self) -> None:
            self.submitted: list[OperationKind] = []

        def list(self) -> list[Operation]:
            return [Operation("active-gui", OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
                              OperationStatus.RUNNING, "2026-10-22T23:00:00+08:00")]

        def submit(self, kind: OperationKind) -> tuple[None, bool]:
            self.submitted.append(kind)
            return None, True

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=True, auto_backup_on_startup=True)
    with TestClient(app):
        pass
    assert OperationKind.TDX_LOCAL_IMPORT not in operations.submitted
    assert OperationKind.BACKUP_CREATE not in operations.submitted


def test_rank_continuation_watcher_respects_writes_and_stops_with_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import market_data_version

    pending = {"status": "PENDING"}
    local_scan = threading.Event()
    rank_checked = threading.Event()
    rank_queued = threading.Event()
    calls = []
    local_calls = []
    monkeypatch.setattr(web_app, "RANK_CONTINUATION_CHECK_INTERVAL_SECONDS", .02)
    monkeypatch.setattr(market_data_version, "pending_market_writes", lambda _root: pending)
    monkeypatch.setattr(market_data_version, "recover_abandoned_pending_write",
                        lambda _root: {"status": "RETRY_LATER"})
    monkeypatch.setattr(web_app, "due_financial_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "due_futures_download", lambda *_args: False)
    monkeypatch.setattr(web_app, "reconcile_due_unknown_downloads", lambda *_args: None)
    monkeypatch.setattr(web_app, "due_data_updates", lambda *_args: [])
    monkeypatch.setattr(web_app, "due_operations", lambda *_args: [])
    monkeypatch.setattr(web_app, "load_futures_trading_calendar", lambda *_args: None)

    def scan(*_args):
        local_calls.append(1)
        local_scan.set()
        return []

    def rank_due(_calendar, jobs, _now):
        calls.append(1)
        rank_checked.set()
        return not any(item.kind == OperationKind.FUTURES_MEMBER_RANK_UPDATE for item in jobs)

    monkeypatch.setattr(web_app, "due_local_market_updates", scan)
    monkeypatch.setattr(web_app, "due_rank_continuation", rank_due)

    class RecordingOperations:
        def __init__(self) -> None:
            self.jobs: list[Operation] = []

        def list(self) -> list[Operation]:
            return list(self.jobs)

        def submit(self, kind: OperationKind) -> tuple[None, bool]:
            if kind == OperationKind.FUTURES_MEMBER_RANK_UPDATE:
                self.jobs.append(Operation("rank", kind, OperationStatus.RUNNING,
                                           "2026-10-27T20:00:00+08:00"))
                rank_queued.set()
            return None, True

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=True)
    with TestClient(app):
        assert local_scan.wait(2)
        assert not rank_checked.wait(.1)
        assert len(local_calls) == 1  # The fast watcher does not scan local files.
        pending["status"] = "CLEAR"
        assert rank_queued.wait(2)
        assert len(operations.jobs) == 1
    calls_at_shutdown = len(calls)
    threading.Event().wait(.08)
    assert len(calls) == calls_at_shutdown


def test_data_source_job_checks_hk_and_cpi_on_mainland_holiday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import (
        a_share_calendar, a_share_pool_recovery, a_share_silver_recovery,
        collector, official_exchange_daily, ths_indexflash,
    )

    monkeypatch.setattr(a_share_silver_recovery, "recover_recent_a_share_breadth",
                        lambda *_args, **_kwargs: {"recoveredDays": [], "metricCount": 0})
    monkeypatch.setattr(ths_indexflash, "probe_indexflash", lambda *_args: {"status": "NOT_DUE"})
    for name in ("recover_recent_a_share_pools", "recover_recent_a_share_open_returns"):
        monkeypatch.setattr(a_share_pool_recovery, name,
                            lambda *_args, **_kwargs: {"recoveredDays": [], "failedDays": []})

    available = [collector.CollectionTask(name, name, "fixture", lambda: None) for name in (
        "A_SHARE_BREADTH", "CN_ZT_POOL", "CN_ZT_OPEN_RETURN", "HKEX_MARKET_CAP", "THS_CPI",
    )]
    selected: list[set[str]] = []
    monkeypatch.setattr(collector, "build_collection_tasks", lambda **_kwargs: available)

    def run(_root: Path, *, tasks: list[collector.CollectionTask], **_kwargs: object) -> dict:
        selected.append({task.dataset_id for task in tasks})
        return {"status": "PASS", "tasks": [{"dataset_id": task.dataset_id, "status": "PASS"}
                                             for task in tasks]}

    monkeypatch.setattr(collector, "run_fetch_session", run)
    cap_calls: list[tuple[str, str]] = []
    cap_result = {"writtenDays": [], "failedDays": []}

    def sync_cap(_root: Path, *, start_day: str, end_day: str) -> dict:
        cap_calls.append((start_day, end_day))
        return cap_result

    monkeypatch.setattr(official_exchange_daily, "sync_official_exchange_due",
                        sync_cap)
    monkeypatch.setattr(official_exchange_daily, "sync_official_monthly_due",
                        lambda *_args, **_kwargs: {"writtenMonths": [], "failedMonths": []})
    handler = _operation_handlers(tmp_path)[OperationKind.EQUITY_DASHBOARD_UPDATE]
    monkeypatch.setattr(a_share_calendar, "is_verified_a_share_trading_day", lambda _day: False)
    assert "PASS" in handler()
    assert selected[-1] == {"THS_CPI"}
    monkeypatch.setattr(a_share_calendar, "is_verified_a_share_trading_day", lambda _day: True)
    assert "PASS" in handler()
    assert selected[-1] == {task.dataset_id for task in available} - {"HKEX_MARKET_CAP"}
    assert len(cap_calls) == 2
    assert all(start <= end for start, end in cap_calls)
    cap_result["failedDays"] = [{"day": "2027-01-04", "error": "CALENDAR_UNVERIFIED"}]
    detail = handler()
    assert detail.startswith("PARTIAL_FAILURE:")
    assert "待补 1 日" in detail


def test_auto_financial_download_launches_only_financial_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import tdx_download_ui, tdx_gui_auto_refresh

    class ReadyDriver:
        def __init__(self, exe: Path) -> None:
            assert exe == Path(r"C:\tongdaxin\tdxw.exe")
            self.exe = exe

        def start(self) -> str:
            return "ALREADY_RUNNING"

    called: list[tuple[Path, int | None]] = []

    def fake_worker(root: Path, *, owned_pid: int | None = None,
                    owned_token: str | None = None) -> tuple[int, None]:
        called.append((root, owned_pid))
        assert owned_token is None
        state = tmp_path / "state" / "tdx_download_ui"
        state.mkdir(parents=True)
        (state / "latest.json").write_text(json.dumps({
            "status": "UI_DONE_UNVERIFIED", "terminals": {"financial": "UI_DONE_UNVERIFIED"},
        }), encoding="utf-8")
        return 0, None

    monkeypatch.setattr(tdx_download_ui, "DialogDriver", ReadyDriver)
    monkeypatch.setattr(tdx_gui_auto_refresh, "run_financial_download_worker", fake_worker)
    handlers = _operation_handlers(tmp_path)
    assert handlers[OperationKind.TDX_FINANCIAL_LAUNCH]().startswith("PASS:")
    assert handlers[OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD]().startswith("PASS:")
    assert called == [(tmp_path, None)]


@pytest.mark.parametrize(("terminal", "kind"), [
    ("financial", OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD),
    ("futures", OperationKind.TDX_FUTURES_AUTO_DOWNLOAD),
])
def test_completed_download_does_not_launch_and_retries_only_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str, kind: OperationKind,
) -> None:
    from datetime import datetime, timezone
    from market_monitor import tdx_afterhours, tdx_download_ui, tdx_gui_auto_refresh, tdx_process_ownership

    slot = tdx_afterhours.latest_slot(datetime.now(timezone.utc))
    entries = {b.key: {"slot": slot, "status": "UI_DONE_UNVERIFIED"}
               for b in tdx_download_ui.TERMINALS[terminal][1]}
    state = tmp_path / "state" / "tdx_download_ui"
    state.mkdir(parents=True)
    (state / "checkpoints.json").write_text(json.dumps({terminal: entries}), encoding="utf-8")
    monkeypatch.setattr(tdx_download_ui, "DialogDriver", lambda *_args: pytest.fail("must not launch"))
    monkeypatch.setattr(tdx_process_ownership, "load_auto_owned", lambda *_args: None)
    handler = _operation_handlers(tmp_path)[kind]
    assert handler().startswith("PASS:")

    calls = []
    monkeypatch.setattr(tdx_process_ownership, "load_auto_owned", lambda *_args: (42, "windows-filetime:123"))
    monkeypatch.setattr(tdx_process_ownership, "clear_exited_auto_owned", lambda *_args: None)
    def worker(root, *, owned_pid, owned_token):
        calls.append((owned_pid, owned_token))
        (state / "latest.json").write_text(json.dumps({"terminals": {terminal: "UI_DONE_UNVERIFIED"}}))
        return 0, None
    monkeypatch.setattr(tdx_gui_auto_refresh, f"run_{terminal}_download_worker", worker)
    assert handler().startswith("PASS:")
    assert calls == [(42, "windows-filetime:123")]
    (state / "checkpoints.json").write_text(json.dumps({terminal: {next(iter(entries)): "invalid"}}))
    assert handler().startswith("PARTIAL_FAILURE:")
    assert len(calls) == 1


def test_member_rank_batch_limit_reports_authorized_api_continuation(tmp_path, monkeypatch):
    from market_monitor import futures_member_rank_update

    report = {
        "status": "PARTIAL", "tradingDay": "2026-10-26", "missingVenues": ["DCE"],
        "sources": {"DCE": {"status": "PARTIAL", "captureStatus": "AUTHORIZED_API_READY",
                                "importStatus": "BATCH_LIMIT", "detail": "已核验25/250个合约"}},
        "continuation": {"reason": "DCE_AUTHORIZED_BATCH_LIMIT", "source": "DCE_AUTHORIZED_API",
                         "tradingDay": "2026-10-26", "processedContracts": 25,
                         "completedContracts": 25, "totalContracts": 250, "remainingContracts": 225},
    }
    monkeypatch.setattr(futures_member_rank_update, "run_latest_member_rank_update",
                        lambda *_args, **_kwargs: report)
    result = _operation_handlers(tmp_path)[OperationKind.FUTURES_MEMBER_RANK_UPDATE]()
    assert "DCE_AUTHORIZED_BATCH_CONTINUATION:2026-10-26:25:25:250" in result
    assert "15分钟后检查断点续跑" in result
    assert "雷电" not in result


def test_auto_futures_download_uses_its_own_terminal_and_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import tdx_download_ui, tdx_gui_auto_refresh

    class ReadyDriver:
        def __init__(self, exe: Path) -> None:
            assert exe == Path(r"C:\new_tdxqh\tdxw.exe")
            self.exe = exe

        def start(self) -> str:
            return "ALREADY_RUNNING"

    def fake_worker(root: Path, *, owned_pid: int | None = None,
                    owned_token: str | None = None) -> tuple[int, None]:
        assert root == tmp_path
        assert owned_pid is None
        assert owned_token is None
        state = tmp_path / "state" / "tdx_download_ui"
        state.mkdir(parents=True)
        (state / "latest.json").write_text(json.dumps({
            "status": "UI_DONE_UNVERIFIED", "terminals": {"futures": "UI_DONE_UNVERIFIED"},
        }), encoding="utf-8")
        return 0, None

    monkeypatch.setattr(tdx_download_ui, "DialogDriver", ReadyDriver)
    monkeypatch.setattr(tdx_gui_auto_refresh, "run_futures_download_worker", fake_worker)
    handlers = _operation_handlers(tmp_path)
    assert handlers[OperationKind.TDX_FUTURES_LAUNCH]().startswith("PASS:")
    assert handlers[OperationKind.TDX_FUTURES_AUTO_DOWNLOAD]().startswith("PASS:")


def test_auto_download_passes_only_newly_launched_pid_to_gui_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import tdx_download_ui, tdx_gui_auto_refresh, tdx_process_ownership

    class NewDriver:
        def __init__(self, exe: Path) -> None:
            self.launched_pid = 4242
            self.exe = exe

        def start(self) -> str:
            return "LAUNCHED"

    received: list[int | None] = []

    def fake_worker(_root: Path, *, owned_pid: int | None = None,
                    owned_token: str | None = None) -> tuple[int, None]:
        received.append(owned_pid)
        assert owned_token == "windows-filetime:synthetic"
        path = tmp_path / "state" / "tdx_download_ui" / "latest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"terminals": {"futures": "UI_DONE_UNVERIFIED"}}),
                        encoding="utf-8")
        return 0, None

    monkeypatch.setattr(tdx_download_ui, "DialogDriver", NewDriver)
    monkeypatch.setattr(tdx_process_ownership, "remember_auto_owned",
                        lambda *_args: (4242, "windows-filetime:synthetic"))
    monkeypatch.setattr(tdx_process_ownership, "clear_exited_auto_owned",
                        lambda *_args: None)
    monkeypatch.setattr(tdx_gui_auto_refresh, "run_futures_download_worker", fake_worker)
    assert _operation_handlers(tmp_path)[OperationKind.TDX_FUTURES_AUTO_DOWNLOAD]().startswith("PASS:")
    assert received == [4242]


def test_auto_download_retry_reuses_only_recorded_terminal_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor import tdx_download_ui, tdx_gui_auto_refresh, tdx_process_ownership

    class ExistingDriver:
        def __init__(self, exe: Path) -> None:
            self.exe = exe

        def start(self) -> str:
            return "ALREADY_RUNNING"

    received: list[tuple[int | None, str | None]] = []

    def fake_worker(_root: Path, *, owned_pid: int | None = None,
                    owned_token: str | None = None) -> tuple[int, None]:
        received.append((owned_pid, owned_token))
        path = tmp_path / "state/tdx_download_ui/latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"terminals": {"futures": "UI_DONE_UNVERIFIED"}}),
                        encoding="utf-8")
        return 0, None

    monkeypatch.setattr(tdx_download_ui, "DialogDriver", ExistingDriver)
    monkeypatch.setattr(tdx_process_ownership, "load_auto_owned",
                        lambda *_args: (4242, "windows-filetime:synthetic"))
    monkeypatch.setattr(tdx_process_ownership, "clear_exited_auto_owned",
                        lambda *_args: None)
    monkeypatch.setattr(tdx_gui_auto_refresh, "run_futures_download_worker", fake_worker)
    assert _operation_handlers(tmp_path)[OperationKind.TDX_FUTURES_AUTO_DOWNLOAD]().startswith("PASS:")
    assert received == [(4242, "windows-filetime:synthetic")]


def test_f10_status_reports_pending_publication_without_exposing_marker_contents(tmp_path: Path) -> None:
    class EmptyOperations:
        items: list[Operation] = []
        def list(self) -> list[Operation]:
            return self.items

    root = tmp_path / "data"
    marker = root / "f10" / "cn" / "revenue_publication_pending.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"private":"synthetic-private"}', encoding="utf-8")
    operations = EmptyOperations()
    app = create_web_app(root, web_dist=_dist(tmp_path), operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/f10/update-status")
        operations.items.append(Operation("recovered", OperationKind.F10_REVENUE_REPUBLISH,
            OperationStatus.PASS, "2026-10-31T01:00:00+00:00", completed_at="2026-10-31T01:01:00+00:00"))
        after = client.get("/api/f10/update-status").json()
    assert response.status_code == 200 and response.json()["revenuePublicationPending"] is True
    assert "synthetic-private" not in response.text
    assert "F10_REVENUE_REPUBLISH:recovered:PASS" in after["completedRevision"]
    assert marker.exists()


def test_revenue_publication_operation_rebuilds_local_snapshot_only(tmp_path: Path, monkeypatch) -> None:
    from market_monitor import f10

    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    (folder / "details_20261031.jsonl").write_text(
        json.dumps({"code": "000001", "name": "合成企业", "market": "CN"}) + "\n", encoding="utf-8")
    (folder / "revenue_20261031.jsonl").write_text(json.dumps({"code": "000001", "fetched_at": "2026-10-29T03:00:00+00:00",
        "revenue_breakdown": [{"item": "合成产品", "period": "2026-06-30", "income": 42}]}) + "\n", encoding="utf-8")
    marker = folder / "revenue_publication_pending.json"
    marker.write_text("{}", encoding="utf-8")
    def unexpected_fetch(_code):
        raise AssertionError("local recovery must not request provider")
    monkeypatch.setattr(f10, "fetch_business_analysis", unexpected_fetch)
    handler = _operation_handlers(tmp_path)[OperationKind.F10_REVENUE_REPUBLISH]
    assert handler().startswith("PASS:")
    snapshot = tmp_path / "industry" / "f10" / "cn_f10.jsonl"
    row = json.loads(snapshot.read_text(encoding="utf-8").strip())
    assert row["code"] == "000001" and row["revenue_breakdown"][0]["item"] == "合成产品"
    assert row["revenue_breakdown"][0]["fetched_at"] == "2026-10-29T03:00:00+00:00"
    from market_monitor.industry_graph.f10.repository import company_detail_from_record
    detail = company_detail_from_record(row)
    assert detail is not None
    segment = detail.to_dict()["revenueSegments"][0]
    assert segment["fetchedAt"] == "2026-10-29T03:00:00+00:00"
    assert segment["amount"]["asOf"] == segment["fetchedAt"]
    assert not marker.exists()


def test_f10_batch_matches_only_its_latest_completed_operation(tmp_path: Path) -> None:
    class Operations:
        def __init__(self):
            self.items = [Operation("latest", OperationKind.REVENUE_UPDATE, OperationStatus.PARTIAL_FAILURE,
                "2026-10-31T02:00:00+00:00", started_at="2026-10-31T02:00:01+00:00",
                completed_at="2026-10-31T02:01:01+00:00")]
        def list(self): return self.items
    root = tmp_path / "data"
    summary = root / "f10" / "cn" / "revenue_summary.json"
    summary.parent.mkdir(parents=True)
    def write_summary(completed):
        summary.write_text(json.dumps({"status": "PARTIAL", "completed_at": completed,
            "new_revenue": 25, "remaining_details": 17, "failed_codes": 0}), encoding="utf-8")
    operations = Operations()
    app = create_web_app(root, web_dist=_dist(tmp_path), operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        def revenue(): return next(x for x in client.get("/api/f10/update-status").json()["jobs"]
                                   if x["kind"] == "REVENUE_UPDATE")
        write_summary("2026-10-31T02:01:01+00:00")
        assert revenue()["batchMatchesLatest"] is True
        write_summary("2026-10-31T01:01:01+00:00")
        assert revenue()["batchMatchesLatest"] is False
        operations.items[0].status = OperationStatus.RUNNING
        write_summary("2026-10-31T02:01:01+00:00")
        assert revenue()["batchMatchesLatest"] is False


def test_futures_status_read_is_side_effect_free_and_manual_check_queues_bounded_kinds(tmp_path: Path) -> None:
    class RecordingOperations:
        submitted: list[OperationKind] = []

        def list(self) -> list[Operation]:
            return []

        def submit(self, kind: OperationKind) -> tuple[Operation, bool]:
            self.submitted.append(kind)
            return Operation(f"synthetic-{kind.value}", kind, OperationStatus.QUEUED,
                             "2026-10-20T10:00:00+00:00"), True

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        status = client.get("/api/futures/update-status")
        assert status.status_code == 200
        assert status.json()["autoEnabled"] is False
        assert status.json()["completedRevision"] == ""
        assert operations.submitted == []
        queued = client.post("/api/futures/check-updates")
        assert queued.status_code == 202
        assert len(queued.json()["items"]) == 4
    assert operations.submitted == [
        OperationKind.TDX_LOCAL_IMPORT, OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE, OperationKind.FUTURES_MEMBER_RANK_UPDATE,
    ]


def test_data_status_read_is_side_effect_free_and_manual_check_queues_source_jobs(tmp_path: Path) -> None:
    class RecordingOperations:
        def __init__(self) -> None:
            self.submitted: list[OperationKind] = []

        def list(self) -> list[Operation]:
            return []

        def submit(self, kind: OperationKind) -> tuple[Operation, bool]:
            self.submitted.append(kind)
            return Operation(f"synthetic-{kind.value}", kind, OperationStatus.QUEUED,
                             "2026-10-21T10:00:00+00:00"), True

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations,  # type: ignore[arg-type]
                         auto_update_on_startup=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        status = client.get("/api/data/update-status")
        assert status.status_code == 200
        assert status.json()["autoEnabled"] is False
        assert operations.submitted == []
        queued = client.post("/api/data/check-updates")
        assert queued.status_code == 202
        assert len(queued.json()["items"]) == 2
    assert operations.submitted == [OperationKind.EQUITY_DASHBOARD_UPDATE,
                                    OperationKind.DISCLOSURES_UPDATE]


def test_data_update_revision_changes_after_local_import(tmp_path: Path) -> None:
    class RecordingOperations:
        def __init__(self) -> None:
            self.items: list[Operation] = []

        def list(self) -> list[Operation]:
            return list(reversed(self.items))

    operations = RecordingOperations()
    app = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path),
                         operation_manager=operations)  # type: ignore[arg-type]
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        before = client.get("/api/data/update-status").json()
        operations.items.append(Operation(
            "tdx-complete", OperationKind.TDX_LOCAL_IMPORT, OperationStatus.PASS,
            "2026-10-25T00:00:00+00:00", completed_at="2026-10-25T00:01:00+00:00",
        ))
        after = client.get("/api/data/update-status").json()
    assert before["completedRevision"] != after["completedRevision"]
    assert "TDX_LOCAL_IMPORT:tdx-complete:PASS" in after["completedRevision"]
    assert len(after["jobs"]) == 2  # Only data-page jobs are shown as buttons.


def test_startup_local_import_accepts_manual_download_without_ui_confirmation(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    state = data_root / "state" / "tdx_download_ui"
    state.mkdir(parents=True)
    (state / "latest.json").write_text(
        json.dumps({"status": "ATTENTION_REQUIRED", "slot": "2026-01-01T23:00:00+08:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "market_monitor.tdx_import_workflow.run_local_terminal_imports",
        lambda _root: {"status": "PASS", "terminals": {
            "financial": {"status": "PASS", "rows": 10}, "futures": {"status": "NO_CHANGE", "rows": 0},
        }},
    )
    monkeypatch.setattr(
        "market_monitor.web_api.market.prime_market_list_snapshots",
        lambda _root: (_ for _ in ()).throw(AssertionError("publication must be separate")),
    )
    handlers = _operation_handlers(data_root)

    result = handlers[OperationKind.TDX_LOCAL_IMPORT]()  # type: ignore[operator]

    assert str(result).startswith("PASS:")
    assert "金融终端=PASS" in str(result)
    assert "期货通=NO_CHANGE" in str(result)


def test_local_import_handler_reports_unknown_futures_without_hiding_financial_result(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state" / "tdx_download_ui" / "checkpoints.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"futures": {"future-5m": {
        "status": "RUNNING_UNKNOWN", "slot": "2026-10-23T23:00:00+08:00",
    }}}), encoding="utf-8")
    monkeypatch.setattr("market_monitor.tdx_import_workflow.run_local_terminal_imports",
                        lambda _root: {"status": "PARTIAL_FAILURE", "selectedTerminals": ["financial"],
                                       "blockedTerminals": {"futures": "RUNNING_UNKNOWN"},
                                       "terminals": {"financial": {"status": "PASS", "rows": 10}}})
    result = _operation_handlers(tmp_path)[OperationKind.TDX_LOCAL_IMPORT]()
    assert str(result).startswith("PARTIAL_FAILURE:")
    assert "futures" in str(result) and "RUNNING_UNKNOWN" in str(result)
    assert "金融终端=PASS，新增 10 根" in str(result)


def test_market_fetch_reports_source_status_without_publishing_inline(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    selected = []
    monkeypatch.setattr(
        "market_monitor.collector.build_collection_tasks",
        lambda **_kwargs: [SimpleNamespace(dataset_id="CN_STOCK"),
                           SimpleNamespace(dataset_id="FUTURES_OI_LEADERBOARD")],
    )

    def fetch(_root, *, tasks, isolate_akshare):
        selected.extend(task.dataset_id for task in tasks)
        assert isolate_akshare is True
        return {"status": "BLOCKED", "task_count": 1, "passed": 0,
                "failed": 0, "blocked": 1, "total_rows": 0}

    monkeypatch.setattr("market_monitor.collector.run_fetch_session", fetch)
    monkeypatch.setattr(
        "market_monitor.web_api.market.prime_market_list_snapshots",
        lambda _root: (_ for _ in ()).throw(AssertionError("publication must be separate")),
    )
    result = _operation_handlers(tmp_path / "data")[OperationKind.MARKET_UPDATE]()

    assert selected == ["CN_STOCK"]
    assert str(result).startswith("PARTIAL_FAILURE:")
    assert "阻断 1" in str(result)
    assert "独立发布任务" in str(result)


def test_manual_margin_update_uses_authorized_api_before_public_sync(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    calls = []
    days = ["2026-09-30"]
    monkeypatch.setattr("market_monitor.futures_calendar.load_futures_trading_calendar",
                        lambda root: SimpleNamespace(between=lambda start, end: days))
    monkeypatch.setattr("market_monitor.futures_official_backfill.default_backfill_range",
                        lambda root: ("2023-01-01", days[-1]))

    def api(root, requested):
        calls.append("api")
        assert requested == days
        return {"status": "PASS", "pendingSettlementDays": 0}

    def sync(root, **kwargs):
        calls.append("public")
        assert kwargs["dce_fetcher"] is None
        return {"status": "PASS"}

    def browser():
        raise AssertionError("Verified API coverage must not require an emulator")

    monkeypatch.setattr("market_monitor.dce_official_api.update_official_dce_api", api)
    monkeypatch.setattr("market_monitor.futures_settlement_parameters.sync_settlement_parameters", sync)
    monkeypatch.setattr("market_monitor.futures_dce_settlement_browser.DceSettlementBrowser", browser)
    monkeypatch.setattr("market_monitor.market_official_futures_metrics.financial_metrics_version", lambda root: "same")
    monkeypatch.setattr("market_monitor.futures_settlement_backup.sync_current_backup", lambda *args: {"status": "UNAVAILABLE"})
    monkeypatch.setattr("market_monitor.market_tdx_futures_projection.refresh_financial_coverage", lambda root: {})
    handlers = _operation_handlers(tmp_path / "data")
    result = handlers[OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE]()
    assert calls == ["api", "public"]
    assert str(result).startswith("PASS:")
    assert "大商所授权API：PASS" in str(result)


def test_fastapi_f10_api_is_local_paginated_and_never_returns_placeholders(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _f10_fixture(data_root)
    (data_root / "industry").mkdir(exist_ok=True)
    (data_root / "industry" / "industry-atlas.json").write_text(
        json.dumps({"chains": [{"name": "白酒", "stages": [{"name": "消费", "cards": [{"name": "白酒", "companyRefs": ["CN.SSE.STOCK.600519"]}]}]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    listing = client.get("/api/f10/companies", params={"q": "茅台", "page_size": 1})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["instrumentKey"] == "CN.SSE.STOCK.600519"

    detail = client.get("/api/f10/companies/CN.SSE.STOCK.600519")
    assert detail.status_code == 200
    encoded = detail.text
    assert "undefined" not in encoded
    assert "Invalid Date" not in encoded
    assert "null" not in encoded
    assert detail.json().get("chainLocations", []) == []  # old Atlas has no reviewed customer publication


def test_fastapi_rejects_unbounded_f10_pages_and_has_no_mutation_surface(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data", web_dist=_dist(tmp_path)), client=("127.0.0.1", 50000))

    assert client.get("/api/f10/companies", params={"page_size": 501}).status_code == 422
    assert client.post("/api/f10/companies").status_code == 405


def test_operations_only_accept_allowlisted_loopback_requests(tmp_path: Path) -> None:
    operations = OperationManager(tmp_path / "data", {OperationKind.STATUS_REFRESH: lambda: "PASS"})
    application = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path), operation_manager=operations)

    remote = TestClient(application)
    assert remote.post("/api/operations", json={"kind": "STATUS_REFRESH"}).status_code == 403

    loopback = TestClient(application, client=("127.0.0.1", 50000))
    created = loopback.post("/api/operations", json={"kind": "STATUS_REFRESH"})
    assert created.status_code == 202
    assert created.json()["operation"]["kind"] == "STATUS_REFRESH"
    assert loopback.post("/api/operations", json={"kind": "__import__('os').system('x')"}).status_code == 422
    assert loopback.post("/api/operations", json={"kind": "STATUS_REFRESH", "sql": "delete from runs"}).status_code == 422
    assert loopback.get("/api/operations").status_code == 200


def test_tdx_operations_are_allowlisted_for_loopback_custom_queue(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def safe_coverage_handler() -> str:
        entered.set()
        assert release.wait(3)
        return "PASS: synthetic coverage check"

    data_root = tmp_path / "data"
    operations = OperationManager(
        data_root,
        {
            OperationKind.TDX_COVERAGE_CHECK: safe_coverage_handler,
            OperationKind.TDX_AFTERHOURS_DOWNLOAD: lambda: "PASS: synthetic download",
        },
    )
    application = create_web_app(data_root, web_dist=_dist(tmp_path), operation_manager=operations)
    remote = TestClient(application)
    loopback = TestClient(application, client=("127.0.0.1", 50000))

    assert remote.post("/api/operations", json={"kind": "TDX_COVERAGE_CHECK"}).status_code == 403
    try:
        coverage = loopback.post("/api/operations", json={"kind": "TDX_COVERAGE_CHECK"})
        assert coverage.status_code == 202
        assert coverage.json()["operation"]["kind"] == "TDX_COVERAGE_CHECK"
        assert entered.wait(3)

        download = loopback.post("/api/operations", json={"kind": "TDX_AFTERHOURS_DOWNLOAD"})
        assert download.status_code == 202
        assert download.json()["operation"]["kind"] == "TDX_AFTERHOURS_DOWNLOAD"
        assert download.json()["operation"]["status"] == "QUEUED"
    finally:
        release.set()


def test_tdx_afterhours_coverage_api_returns_safe_aggregates(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    missing = client.get("/api/tdx-afterhours/coverage")
    assert missing.status_code == 200
    assert missing.json() == {"available": False}

    absolute_raw_path = tmp_path / "terminals" / "vipdoc" / "sh" / "lday" / "sh600519.day"
    report = {
        "schema": "tdx-raw-coverage-v1",
        "generatedAt": "2026-09-20T23:05:00+08:00",
        "status": "PARTIAL_FAILURE",
        "summary": {"files": 4, "gapFiles": 1, "unreadableFiles": 0, "writingFiles": 0, "unknownCalendarFiles": 1},
        "groups": [
            {
                "terminal": "FINANCIAL",
                "period": "1d",
                "files": 2,
                "readableFiles": 2,
                "unreadableFiles": 0,
                "writingFiles": 0,
                "earliestAt": "2026-09-01",
                "latestAt": "2026-09-19",
                "records": 30,
                "continuousFiles": 1,
                "gapFiles": 1,
                "unknownCalendarFiles": 0,
                "gapCount": 1,
                "sampleGaps": [{"instrument": "CN.SSE.STOCK.600519", "date": "2026-09-12", "reason": "missing trading day"}],
                "absolutePath": str(absolute_raw_path),
                "fileDetails": [{"path": str(absolute_raw_path), "records": 15}],
            }
        ],
        "files": [{"path": str(absolute_raw_path), "period": "1d", "records": 15}],
    }
    report_path = data_root / "reports" / "tdx-coverage" / "latest.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    response = client.get("/api/tdx-afterhours/coverage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["generatedAt"] == report["generatedAt"]
    assert payload["status"] == report["status"]
    assert payload["summary"] == report["summary"]
    assert payload["groups"] == [
        {
            "terminal": "FINANCIAL",
            "period": "1d",
            "files": 2,
            "readableFiles": 2,
            "unreadableFiles": 0,
            "writingFiles": 0,
            "earliestAt": "2026-09-01",
            "latestAt": "2026-09-19",
            "records": 30,
            "continuousFiles": 1,
            "gapFiles": 1,
            "unknownCalendarFiles": 0,
            "gapCount": 1,
            "sampleGaps": [{"instrument": "CN.SSE.STOCK.600519", "date": "2026-09-12", "reason": "missing trading day"}],
        }
    ]
    assert str(absolute_raw_path) not in response.text


def test_data_views_are_allowlisted_paginated_and_read_only(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _f10_fixture(data_root)
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    response = client.get("/api/data/f10", params={"page_size": 500})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert client.get("/api/data/not_sql").status_code == 404
    assert client.post("/api/data/f10").status_code == 403


def test_logs_api_is_bounded_read_only_jsonl_view(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    logs = data_root / "logs"
    logs.mkdir(parents=True)
    (logs / "events-2026-08-09.jsonl").write_text('{"timestamp":"2026-08-09T00:00:00+00:00","category":"Operation","status":"PASS"}\n', encoding="utf-8")
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    assert client.get("/api/logs", params={"category": "Operation", "page_size": 1}).json()["total"] == 1
    assert client.post("/api/logs").status_code == 403
