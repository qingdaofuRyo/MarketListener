from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler

from fastapi.testclient import TestClient
import pytest

from market_monitor.admin_boundary import ADMIN_TOKEN_HEADER
from market_monitor.web_admin import (
    CSRF_HEADER, ConsoleSettings, ServiceSupervisor, backend_command,
    create_admin_app as build_admin_app, load_console_settings,
)
from market_monitor.web_app import create_customer_web_app
from market_monitor.storage import MarketStore
from market_monitor.local_backup import create_profile_backup
import market_monitor.web_admin as web_admin_module


def create_admin_app(value: ServiceSupervisor):
    assets = Path(__file__).resolve().parents[2] / "desktop/admin-web"
    return build_admin_app(value, assets_dir=assets)


def test_internal_backend_control_requests_ignore_future_system_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid:8999")
    ordinary = web_admin_module.build_opener()
    isolated = web_admin_module.build_opener(ProxyHandler({}))
    assert any(isinstance(item, ProxyHandler) for item in ordinary.handlers)
    assert not any(isinstance(item, ProxyHandler) for item in isolated.handlers)
    assert not any(isinstance(item, ProxyHandler) for item in web_admin_module._backend_opener.handlers)


def test_delisted_review_page_checks_version_and_bounds(tmp_path, monkeypatch):
    from market_monitor import exchange_delisted

    value = supervisor(tmp_path, monkeypatch)
    calls = []

    def fake(root, exchange, **kwargs):
        calls.append((root, exchange, kwargs))
        return {"version": kwargs["version"], "changes": [], "matchedCount": 0}

    monkeypatch.setattr(exchange_delisted, "review_page", fake)
    app = create_admin_app(value)
    with client(app) as http:
        url = "/api/admin/delisted-review/SZSE"
        valid = {"version": "a" * 64, "offset": 40, "limit": 40, "start": "2025-01-01"}
        assert http.get(url, params=valid).status_code == 200
        assert calls[0][1] == "SZSE" and calls[0][2]["offset"] == 40
        assert http.get(url, params={**valid, "offset": -1}).status_code == 422
        assert http.get(url, params={**valid, "version": "bad"}).status_code == 422
        assert http.get("/api/admin/delisted-review/BSE", params=valid).status_code == 422

        def changed(*_args, **_kwargs):
            raise exchange_delisted.DelistedReviewChanged("synthetic")

        monkeypatch.setattr(exchange_delisted, "review_page", changed)
        assert http.get(url, params=valid).status_code == 409
    with client(app, host="192.0.2.10") as remote:
        assert remote.get("/api/admin/delisted-review/SZSE", params={"version": "a" * 64}).status_code == 403


def supervisor(tmp_path: Path, monkeypatch) -> ServiceSupervisor:
    monkeypatch.setattr("market_monitor.tdx_local_status.terminal_roots", lambda: [
        {"terminal": "financial", "name": "测试金融终端", "path": str(tmp_path / "terminal")},
        {"terminal": "futures", "name": "测试期货通", "path": str(tmp_path / "futures")},
    ])
    value = ServiceSupervisor(tmp_path / "project", tmp_path / "data")
    monkeypatch.setattr(value, "ready", lambda: False)
    return value


def test_tdx_status_is_local_and_available_with_customer_service_stopped(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    status = value.data_root / "state" / "futures_financial_coverage.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"sourcePolicyVersion": "synthetic-v1", "tdxLatestDay": "2026-09-30",
                                  "mismatchCount": 2}), encoding="utf-8")
    with client(create_admin_app(value)) as http:
        response = http.get("/api/admin/tdx-status?refresh=1")
        assert response.status_code == 200
        payload = response.json()
        assert {item["terminal"] for item in payload["roots"]} == {"financial", "futures"}
        assert all(item["lastDownload"]["status"] == "NOT_DOWNLOADED"
                   for item in payload["roots"])
        assert all(item["downloadSchedule"]["currentSlotComplete"] is False
                   for item in payload["roots"])
        assert payload["startupImport"]["enabled"] is True
        assert payload["financialCoverage"]["mismatchCount"] == 2
        assert "不证明中间交易日连续" in payload.get("scope", "") or payload["scanning"]


def client(app, *, host="127.0.0.1") -> TestClient:
    http = TestClient(app, base_url="http://127.0.0.1:8766", client=(host, 42000))
    if host == "127.0.0.1":
        bootstrap = app.state.local_sessions.issue_bootstrap("127.0.0.1:8766")
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": bootstrap},
                         headers={"Origin": "http://127.0.0.1:8766"}).status_code == 200
    return http


def login_product(http: TestClient, app) -> None:
    bootstrap = app.state.local_sessions.issue_bootstrap("127.0.0.1:8765")
    assert http.post("/api/local-session/bootstrap", json={"bootstrap": bootstrap},
                     headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def headers(http: TestClient) -> dict:
    value = http.get("/api/admin/state").json()
    return {"Origin": "http://127.0.0.1:8766", CSRF_HEADER: value["csrfToken"]}


def test_management_mutations_require_local_origin_and_console_credential(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(value, "schedule", lambda action: calls.append(action))
    with client(create_admin_app(value)) as http:
        assert http.get("/api/admin/ready").json()["service"] == "marketlistener-internal-console"
        assert http.post("/api/admin/service/start", json={}).status_code == 403
        good = headers(http)
        assert http.post("/api/admin/service/start", json={}, headers={**good, "Origin": "https://untrusted.example"}).status_code == 403
        assert http.post("/api/admin/service/start", json={}, headers=good).status_code == 202
        assert calls == ["start"]


def test_direct_browser_admin_access_preserves_session_and_csrf(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(value, "schedule", lambda action: calls.append(action))
    app = create_admin_app(value)
    origin = {"Origin": "http://127.0.0.1:8766"}
    with TestClient(app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 42001)) as http:
        assert http.get("/").status_code == 200
        assert http.get("/api/admin/state").status_code == 401
        assert http.post("/api/local-session/open", json={}).status_code == 403
        assert http.post("/api/local-session/open", json={},
                         headers={"Origin": "https://untrusted.example"}).status_code == 403
        opened = http.post("/api/local-session/open", json={}, headers=origin)
        assert opened.status_code == 200
        assert opened.json()["audience"] == "admin"
        assert "httponly" in opened.headers["set-cookie"].lower()
        reused = http.post("/api/local-session/open", json={}, headers=origin)
        assert reused.json()["csrfToken"] == opened.json()["csrfToken"]
        assert "set-cookie" not in reused.headers
        assert http.get("/api/admin/state").status_code == 200
        assert http.post("/api/admin/service/start", json={}).status_code == 403
        assert http.post("/api/admin/service/start", json={}, headers=headers(http)).status_code == 202
        assert calls == ["start"]
        assert http.post("/api/local-session/logout", headers=headers(http)).status_code == 200
        assert http.get("/api/admin/state").status_code == 401
        assert http.post("/api/local-session/open", json={}, headers=origin).status_code == 200


def test_direct_browser_local_open_rejects_payload_and_host(tmp_path, monkeypatch):
    app = create_admin_app(supervisor(tmp_path, monkeypatch))
    origin = {"Origin": "http://127.0.0.1:8766"}
    with TestClient(app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 42001)) as http:
        assert http.post("/api/local-session/open", json={"password": "unused"}, headers=origin).status_code == 400
        assert http.post("/api/local-session/open", content="x" * 65,
                         headers={**origin, "Content-Type": "application/json"}).status_code == 400
        assert http.post("/api/local-session/open", json={},
                         headers={**origin, "Host": "untrusted.example:8766"}).status_code == 403
    with TestClient(app, base_url="http://127.0.0.1:8766", client=("192.0.2.10", 42000)) as external:
        assert external.post("/api/local-session/open", json={}, headers=origin).status_code == 403


def test_retired_login_code_command_does_not_require_dpapi(capsys):
    assert web_admin_module.main(["--show-login-code"]) == 0
    output = capsys.readouterr().out
    assert "已取消浏览器口令" in output
    assert "http://127.0.0.1:8766/" in output


def test_private_research_status_and_pause_require_admin_session(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    app = create_admin_app(value)
    anonymous = TestClient(app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 42001))
    assert anonymous.get("/api/admin/research/status").status_code == 401
    with client(app) as http:
        status = http.get("/api/admin/research/status")
        assert status.status_code == 200
        assert status.json()["corpus"]["documents"] == 0
        assert status.json()["models"]["text"]["status"] in {"STOPPED", "FOREIGN_PORT"}
        assert http.get("/api/admin/research/errors").json()["items"] == []
        assert http.post("/api/admin/research/background/pause", headers=headers(http)).status_code == 200
        assert (value.data_root / "research" / "private" / "pause.flag").is_file()


def test_research_model_controls_fail_closed_without_pinned_local_files(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    with client(create_admin_app(value)) as http:
        good = headers(http)
        response = http.post("/api/admin/research/models/text/start", headers=good)
        assert response.status_code == 409
        assert "缺失" in response.json()["detail"] or "端口" in response.json()["detail"]


def test_research_status_detects_interrupted_page_worker(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    checkpoint = value.data_root / "research" / "private" / "full-extraction.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"phase": "RUNNING", "pid": 99999999,
                                      "sessionProcessed": 100}), encoding="utf-8")
    with client(create_admin_app(value)) as http:
        state = http.get("/api/admin/research/status").json()
        assert state["background"]["phase"] == "INTERRUPTED"
        assert state["background"]["sessionProcessed"] == 100


def test_offline_restore_requires_preview_and_a_stopped_product_service(tmp_path, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        backend_port = listener.getsockname()[1]
    monkeypatch.setattr("market_monitor.tdx_local_status.terminal_roots", lambda: [])
    value = ServiceSupervisor(tmp_path / "project", tmp_path / "data", backend_port=backend_port)
    profile = value.data_root / "personal" / "watchlist.json"
    profile.parent.mkdir(parents=True)
    profile.write_text('{"fixture":"before"}', encoding="utf-8")
    passphrase = "synthetic portable backup passphrase"
    archive = create_profile_backup(value.data_root, passphrase=passphrase)
    profile.write_text('{"fixture":"after"}', encoding="utf-8")
    with client(create_admin_app(value)) as http:
        good = headers(http)
        request = {"name": archive.name, "passphrase": passphrase}
        assert http.get("/api/admin/backups/profile").json()["total"] == 1
        invalid = http.post("/api/admin/backups/profile/restore", json={**request, "planDigest": "0" * 64},
                            headers=good)
        assert invalid.status_code == 202
        # The job rejects a digest that does not match the exact current files.
        # Wait for the isolated worker to report the failure before a new preview.
        for _ in range(40):
            rejected = http.get(f"/api/admin/backups/profile/restore/{invalid.json()['id']}").json()
            if rejected["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(.025)
        assert rejected["status"] == "FAILED"
        assert profile.read_text(encoding="utf-8") == '{"fixture":"after"}'
        preview = http.post("/api/admin/backups/profile/restore-preview", json=request, headers=good)
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        assert plan["replaceCount"] == 1
        result = http.post("/api/admin/backups/profile/restore",
                           json={**request, "planDigest": plan["planDigest"]}, headers=good)
        assert result.status_code == 202, result.text
        identifier = result.json()["id"]
        for _ in range(80):
            job = http.get(f"/api/admin/backups/profile/restore/{identifier}").json()
            if job["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(.025)
        assert job["status"] == "PASS", job
        assert profile.read_text(encoding="utf-8") == '{"fixture":"before"}'


def test_management_service_rejects_nonlocal_and_rebinding_hosts(tmp_path, monkeypatch):
    app = create_admin_app(supervisor(tmp_path, monkeypatch))
    with client(app, host="192.0.2.10") as http:
        assert http.get("/").status_code == 403
    with client(app) as http:
        assert http.get("/", headers={"Host": "untrusted.example:8766"}).status_code == 403
        assert http.get("/").status_code == 200


def test_admin_preferences_are_bounded_and_do_not_change_live_service(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    with client(create_admin_app(value)) as http:
        good = headers(http)
        settings = {"auto_start": False, "auto_update": False, "background_priority": True, "theme": "light"}
        assert http.put("/api/admin/settings", json={**settings, "command": "synthetic forbidden command"}, headers=good).status_code == 422
        assert http.put("/api/admin/settings", json=settings, headers=good).status_code == 200
        assert load_console_settings(value.settings_path) == ConsoleSettings(**settings)
        assert value.process is None
        assert http.post("/api/admin/tasks", json={"kind": "UNKNOWN_COMMAND"}, headers=good).status_code == 422
        assert http.get("/api/admin/logs?source=../private").status_code == 422


def test_existing_external_backend_can_be_reused_but_not_stopped(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    monkeypatch.setattr(value, "ready", lambda: True)
    monkeypatch.setattr("market_monitor.web_admin.subprocess.Popen", lambda *_a, **_k: pytest.fail("must reuse existing service"))
    # Reuse first discovers an occupied port, then verifies its service
    # identity. A ready stub alone must not represent a closed port.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        value.backend_port = listener.getsockname()[1]
        value.start()
    assert value.snapshot()["owned"] is False
    with client(create_admin_app(value)) as http:
        assert http.post("/api/admin/service/stop", json={}, headers=headers(http)).status_code == 409


def test_backend_credential_is_server_only_and_log_tail_redacts_it(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    value.log_dir.mkdir(parents=True)
    (value.log_dir / "web-backend.log").write_text("synthetic test output " + value.backend_token, encoding="utf-8")
    with client(create_admin_app(value)) as http:
        assert value.backend_token not in http.get("/api/admin/state").text
        response = http.get("/api/admin/logs").json()
        assert value.backend_token not in json.dumps(response)
        assert "[redacted]" in response["lines"][0]


def test_internal_tasks_are_forwarded_to_finite_backend_api(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    requests = []

    def backend(path, **options):
        requests.append((path, options))
        return {"created": True, "operation": {"kind": "MARKET_UPDATE"}}

    monkeypatch.setattr(value, "backend_request", backend)
    with client(create_admin_app(value)) as http:
        assert http.post("/api/admin/tasks", json={"kind": "MARKET_UPDATE"}, headers=headers(http)).status_code == 202
        assert requests[-1][0] == "/api/operations"
        assert requests[-1][1]["payload"] == {"kind": "MARKET_UPDATE"}
        assert http.post("/api/admin/tasks/../cancel", headers=headers(http)).status_code in {404, 405}


def test_customer_runtime_denies_internal_apis_but_keeps_product_writes(tmp_path):
    app = create_customer_web_app(tmp_path / "data", admin_token="synthetic-test-admin-credential")
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 42000)) as http:
        assert http.get("/api/ready").status_code == 200
        login_product(http, app)
        for path in ("/api/operations", "/api/logs", "/api/health", "/api/data-sources/providers", "/api/tdx-afterhours/coverage", "/api/internal/runtime", "/api/internal/tdx-import-inventory", "/api/internal/market-publication", "/api/data/silver", "/api/data/quarantine", "/api/dashboard/runs", "/api/metrics/heatmap?category=storage"):
            assert http.get(path).status_code == 403
        assert http.post("/api/operations", json={"kind": "STATUS_REFRESH"}).status_code == 403
        assert http.get("/api/operations", headers={ADMIN_TOKEN_HEADER: "synthetic-test-admin-credential"}).status_code == 200
        publication = http.get("/api/internal/market-publication", headers={
            ADMIN_TOKEN_HEADER: "synthetic-test-admin-credential",
        })
        assert publication.status_code == 200
        assert publication.json()["models"]["all"]["snapshotStatus"] == "MISSING_OR_INVALID"
        assert publication.json()["silverWrites"] == {"status": "CLEAR", "count": 0}
        ids = {item["id"] for item in http.get("/api/dashboard/definitions").json()["items"]}
        assert not ids.intersection({"storage", "quality", "freshness", "runs", "partitions"})
        # Product POST endpoints remain product capabilities, even though this
        # intentionally incomplete payload is rejected by their input schema.
        csrf = http.get("/api/local-session").json()["csrfToken"]
        assert http.post("/api/strategy/formula/validate", json={}, headers={
            "Origin": "http://127.0.0.1:8765", "X-MarketListener-CSRF": csrf,
        }).status_code != 403


def test_customer_startup_can_skip_data_updates(tmp_path):
    project = tmp_path / "project"
    executable = project / ("desktop/.venv/Scripts/python.exe" if os.name == "nt" else "desktop/.venv/bin/python")
    executable.parent.mkdir(parents=True)
    executable.touch()
    default = backend_command(project, tmp_path / "data", 9875, ConsoleSettings())
    paused = backend_command(project, tmp_path / "data", 9875, ConsoleSettings(auto_update=False))
    assert "--no-auto-update-on-startup" not in default
    assert "--no-auto-update-on-startup" in paused


def test_product_metadata_reads_work_while_task_store_connection_is_open(tmp_path):
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        app = create_customer_web_app(root)
        with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 42000)) as http:
            login_product(http, app)
            for path in ("/api/data/sections", "/api/data/equities/cn/overview", "/api/data/macro/catalog"):
                assert http.get(path).status_code == 200
        assert store.connection.execute("SELECT count(*) FROM gold_metrics").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="terminal driver is Windows-only")
def test_download_completion_is_preserved_when_followup_import_fails(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    commands = []

    def spawn(*args, **options):
        commands.append(args[0])
        report = {"status": "UI_DONE_UNVERIFIED", "terminals": {"financial": "UI_DONE_UNVERIFIED", "futures": "UI_DONE_UNVERIFIED"}}
        options["stdout"].write((json.dumps(report) + "\n").encode())
        options["stdout"].flush()
        return SimpleNamespace(wait=lambda: 0)

    def failed_import(*_args, **_kwargs):
        raise RuntimeError("synthetic import unavailable")

    monkeypatch.setattr("market_monitor.web_admin.subprocess.Popen", spawn)
    monkeypatch.setattr("market_monitor.tdx_gui_auto_refresh._is_x86_python", lambda _path: True)
    monkeypatch.setattr("market_monitor.tdx_gui_auto_refresh.financial_minute_start",
                        lambda *_args: date(2026, 10, 21))
    monkeypatch.setattr(value, "backend_request", failed_import)
    value.run_terminal("financial", "download")
    deadline = time.monotonic() + 3
    while value.terminal_busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert value.terminal_jobs["financial"]["status"] == "PASS"
    assert value.terminal_jobs["financial"]["detail"] == "UI_DONE_UNVERIFIED"
    assert value.terminal_jobs["financial"]["followUpError"] == "synthetic import unavailable"
    assert commands[0][0].endswith("python311-x86\\python.exe")
    assert "--auto-login-saved" in commands[0]


@pytest.mark.skipif(sys.platform != "win32", reason="verifies Windows venv redirector process ownership")
def test_real_owned_backend_stops_its_redirector_and_python_child(tmp_path):
    project = Path(__file__).resolve().parents[2]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    value = ServiceSupervisor(project, tmp_path / "data", backend_port=port)
    value.settings = ConsoleSettings(auto_start=False, auto_update=False)
    try:
        value.start()
        assert value.ready()
        assert {item["kind"] for item in value.backend_request("/api/operations")["items"]} <= {"BACKUP_CREATE"}
        runtime = value.backend_request("/api/internal/runtime")
        assert runtime["cpuSeconds"] >= 0
        assert runtime["pid"] > 0
        value.stop()
        assert not value.ready()
        assert value.process is None
    finally:
        if value.process is not None:
            value.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership guard")
def test_stopping_owned_backend_preserves_terminal_descendant(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    alive = {"backend": True, "redirector": True, "terminal": True}
    commands = []

    class Redirector:
        pid = 101

        def poll(self):
            return None if alive["redirector"] else 0

        def wait(self, *, timeout):
            assert timeout == 8
            assert not alive["redirector"]
            return 0

    value.process = Redirector()
    value.backend_runtime_pid = 202
    value.backend_runtime_token = "synthetic-lifecycle"
    monkeypatch.setattr(web_admin_module, "process_start_token", lambda pid:
                        "synthetic-lifecycle" if pid == 202 and alive["backend"] else None)

    def taskkill(command, **_kwargs):
        commands.append(command)
        assert command[:2] == ["taskkill", "/PID"]
        assert "/T" not in command
        if command[2] == "202":
            alive["backend"] = False
        elif command[2] == "101":
            alive["redirector"] = False
        else:
            pytest.fail("unowned process was targeted")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_admin_module.subprocess, "run", taskkill)
    value.stop()
    assert commands == [["taskkill", "/PID", "202", "/F"],
                        ["taskkill", "/PID", "101", "/F"]]
    assert alive["terminal"]
    assert value.process is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership guard")
def test_stopping_owned_backend_rejects_reused_runtime_pid(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    value.process = SimpleNamespace(pid=101, poll=lambda: None)
    value.backend_runtime_pid = 202
    value.backend_runtime_token = "synthetic-original-lifecycle"
    monkeypatch.setattr(web_admin_module, "process_start_token", lambda pid: "different-lifecycle")
    monkeypatch.setattr(web_admin_module.subprocess, "run", lambda *_args, **_kwargs:
                        pytest.fail("must not kill a reused PID"))
    with pytest.raises(RuntimeError, match="身份已变化"):
        value.stop()
    assert value.process is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership guard")
def test_stopping_owned_backend_rejects_unavailable_runtime_identity(tmp_path, monkeypatch):
    value = supervisor(tmp_path, monkeypatch)
    value.process = SimpleNamespace(pid=101, poll=lambda: None)
    value.backend_runtime_pid = 202
    value.backend_runtime_token = "synthetic-original-lifecycle"
    monkeypatch.setattr(web_admin_module, "process_start_token", lambda pid: None)
    monkeypatch.setattr(web_admin_module.subprocess, "run", lambda *_args, **_kwargs:
                        pytest.fail("must not kill a process with unverifiable identity"))
    with pytest.raises(RuntimeError, match="暂不可核验"):
        value.stop()
    assert value.process is not None
