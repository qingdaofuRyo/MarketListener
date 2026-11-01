from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from urllib.request import ProxyHandler

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/start_marketlistener.py"
SPEC = importlib.util.spec_from_file_location("web_bootstrap", SCRIPT)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


def test_local_control_bootstrap_never_uses_system_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid:8999")
    ordinary = bootstrap.build_opener()
    isolated = bootstrap.build_opener(ProxyHandler({}))
    assert any(isinstance(item, ProxyHandler) for item in ordinary.handlers)
    assert not any(isinstance(item, ProxyHandler) for item in isolated.handlers)
    assert not any(isinstance(item, ProxyHandler) for item in bootstrap._opener.handlers)


def test_client_shortcut_prepares_services_and_opens_browser_once(tmp_path, monkeypatch):
    index = tmp_path / "desktop/src/market_monitor/web_dist/index.html"
    index.parent.mkdir(parents=True)
    index.touch()
    monkeypatch.setattr(bootstrap, "PROJECT", tmp_path)
    actions = []
    monkeypatch.setattr(bootstrap, "ensure_admin", lambda *_a, **_k: actions.append("admin"))
    monkeypatch.setattr(bootstrap, "establish_admin_session", lambda: actions.append("admin-session"))
    monkeypatch.setattr(bootstrap, "ensure_client", lambda: actions.append("client"))
    monkeypatch.setattr(bootstrap, "issue_bootstrap", lambda *_a: "synthetic-one-use-bootstrap")
    monkeypatch.setattr(bootstrap.webbrowser, "open_new", lambda url: actions.append(url))
    assert bootstrap.main(["--target", "client"]) == 0
    assert actions == ["admin", "admin-session", "client",
                       "http://127.0.0.1:8765/#ml-bootstrap=synthetic-one-use-bootstrap"]


def test_background_entry_has_no_browser_window(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "PROJECT", tmp_path)
    monkeypatch.setattr(bootstrap, "ensure_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(bootstrap, "establish_admin_session", lambda: None)
    monkeypatch.setattr(bootstrap.webbrowser, "open_new", lambda *_a: pytest.fail("background entry must not open browser"))
    assert bootstrap.main(["--target", "none"]) == 0


def test_login_watchdog_checks_client_and_records_bounded_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "PROJECT", tmp_path)
    (tmp_path / "desktop/src/market_monitor/web_dist").mkdir(parents=True)
    (tmp_path / "desktop/src/market_monitor/web_dist/index.html").touch()
    actions = []
    monkeypatch.setattr(bootstrap, "ensure_admin", lambda *_a, **_k: actions.append("admin"))
    monkeypatch.setattr(bootstrap, "establish_admin_session", lambda: actions.append("session"))
    monkeypatch.setattr(bootstrap, "ensure_client", lambda: actions.append("client"))
    monkeypatch.setattr(bootstrap, "read_json", lambda path, **_kwargs:
                        {"settings": {"auto_start": True}} if path.endswith("/api/admin/state")
                        else {"status": "READY"})
    monkeypatch.setattr(bootstrap.webbrowser, "open_new", lambda *_a: pytest.fail("no browser"))

    assert bootstrap.main(["--target", "none", "--startup-watchdog"]) == 0
    assert actions == ["admin", "session", "client"]
    records = [json.loads(line) for line in
               (tmp_path / bootstrap._WATCHDOG_LOG).read_text(encoding="utf-8").splitlines()]
    assert [(item["status"], item["phase"]) for item in records] == [
        ("ATTEMPT", "LOCK"), ("PASS", "READY"),
    ]


def test_login_watchdog_honors_disabled_auto_start_and_logs_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "PROJECT", tmp_path)
    monkeypatch.setattr(bootstrap, "ensure_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(bootstrap, "establish_admin_session", lambda: None)
    monkeypatch.setattr(bootstrap, "ensure_client", lambda: pytest.fail("client auto start disabled"))
    monkeypatch.setattr(bootstrap, "read_json", lambda path, **_kwargs:
                        {"settings": {"auto_start": False}} if path.endswith("/api/admin/state") else {})
    assert bootstrap.main(["--target", "none", "--startup-watchdog"]) == 0
    monkeypatch.setattr(bootstrap, "ensure_admin",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("synthetic secret-free failure")))
    assert bootstrap.main(["--target", "none", "--startup-watchdog"]) == 2
    records = [json.loads(line) for line in
               (tmp_path / bootstrap._WATCHDOG_LOG).read_text(encoding="utf-8").splitlines()]
    assert records[-1]["status"] == "FAILED" and records[-1]["phase"] == "ADMIN"
    assert records[-1]["errorType"] == "RuntimeError"
    assert "synthetic secret-free failure" not in json.dumps(records)


def test_existing_identified_management_service_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "read_json", lambda *_a, **_k: {"service": "marketlistener-internal-console", "status": "READY"})
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not duplicate console"))
    bootstrap.ensure_admin(tmp_path)


def test_unrelated_listener_is_never_used_as_management_service(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "read_json", lambda *_a, **_k: {"service": "synthetic-unrelated-service"})

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return 0

    monkeypatch.setattr(bootstrap.socket, "socket", Socket)
    with pytest.raises(RuntimeError, match="8766"):
        bootstrap.ensure_admin(tmp_path)
