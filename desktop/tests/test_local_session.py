from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from market_monitor.admin_boundary import ADMIN_TOKEN_HEADER, install_admin_boundary
from market_monitor.local_session import (
    CONTROL_HEADER, CONSOLE_CSRF_HEADER, PRODUCT_CSRF_HEADER,
    LocalSessionManager, install_local_sessions, load_control_token,
)


def application(audience="product", port=8765, *, clock=None):
    app = FastAPI()
    manager = LocalSessionManager(audience, port, **({"clock": clock} if clock else {}))
    install_local_sessions(app, manager, control_token="synthetic-local-control-only")

    @app.get("/api/private")
    def sensitive():
        return {"fixture": "synthetic personal value"}

    @app.post("/api/private")
    def mutate():
        return {"status": "SAVED"}

    return app


def client(app, port=8765, host="127.0.0.1"):
    return TestClient(app, base_url=f"http://127.0.0.1:{port}", client=(host, 49000))


def bootstrap(http, port=8765):
    issue = http.post("/api/local-session/issue", json={}, headers={CONTROL_HEADER: "synthetic-local-control-only"})
    assert issue.status_code == 200
    token = issue.json()["bootstrap"]
    response = http.post("/api/local-session/bootstrap", json={"bootstrap": token}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert response.status_code == 200
    return token, response


def test_sensitive_gets_require_session_and_bootstrap_is_one_use():
    with client(application()) as http:
        assert http.get("/api/private").status_code == 401
        token, response = bootstrap(http)
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert http.get("/api/private").json()["fixture"].startswith("synthetic")
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": token},
                         headers={"Origin": "http://127.0.0.1:8765"}).status_code == 401


def test_direct_local_open_cannot_be_enabled_for_product():
    app = application()
    assert not any(getattr(route, "path", None) == "/api/local-session/open" for route in app.routes)
    with pytest.raises(ValueError, match="admin audience"):
        install_local_sessions(FastAPI(), LocalSessionManager("product", 8765),
                               admin_local_access=True)


def test_csrf_origin_cross_site_and_logout():
    with client(application()) as http:
        _, result = bootstrap(http)
        headers = {"Origin": "http://127.0.0.1:8765", PRODUCT_CSRF_HEADER: result.json()["csrfToken"]}
        assert http.post("/api/private", json={}).status_code == 403
        assert http.post("/api/private", json={}, headers={**headers, PRODUCT_CSRF_HEADER: "synthetic-invalid"}).status_code == 403
        assert http.post("/api/private", json={}, headers={**headers, "Origin": "http://127.0.0.1:8766"}).status_code == 403
        assert http.get("/api/private", headers={"Origin": "https://untrusted.invalid"}).status_code == 403
        assert http.get("/api/private", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert http.post("/api/private", json={}, headers=headers).status_code == 200
        assert http.post("/api/local-session/logout", json={}, headers=headers).status_code == 200
        assert http.get("/api/private").status_code == 401


def test_host_peer_and_proxy_identity_are_rejected():
    app = application()
    with client(app, host="192.0.2.30") as http:
        assert http.get("/api/private").status_code == 403
    with client(app) as http:
        bootstrap(http)
        for headers in ({"Host": "untrusted.invalid:8765"}, {"Host": "127.0.0.1:8766"},
                        {"Forwarded": "for=127.0.0.1"}, {"X-Forwarded-For": "127.0.0.1"},
                        {"X-Forwarded-Host": "127.0.0.1:8765"}):
            assert http.get("/api/private", headers=headers).status_code == 403


def test_cookie_and_csrf_are_not_shared_between_realms_or_hosts():
    product, admin = application(), application("admin", 8766)
    with client(product) as p, client(admin, 8766) as a:
        _, p_session = bootstrap(p)
        for cookie in p.cookies.jar:
            a.cookies.set(cookie.name, cookie.value)
            a.cookies.set(admin.state.local_sessions.cookie_name, cookie.value)
        assert a.get("/api/private").status_code == 401
        _, a_session = bootstrap(a, 8766)
        assert p_session.json()["csrfToken"] != a_session.json()["csrfToken"]
        assert a.post("/api/private", json={}, headers={"Origin": "http://127.0.0.1:8766",
            CONSOLE_CSRF_HEADER: p_session.json()["csrfToken"]}).status_code == 403
        assert p.get("/api/private", headers={"Host": "localhost:8765"}).status_code == 401


def test_control_is_not_a_browser_permission_and_body_is_bounded():
    with client(application()) as http:
        assert http.post("/api/local-session/issue", json={}).status_code == 403
        assert http.post("/api/local-session/issue", json={}, headers={CONTROL_HEADER: "synthetic-local-control-only",
            "Origin": "http://127.0.0.1:8765"}).status_code == 403
        assert http.get("/api/private", headers={CONTROL_HEADER: "synthetic-local-control-only"}).status_code == 401
        assert http.post("/api/local-session/bootstrap?bootstrap=synthetic-never-in-query", json={},
            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 403
        assert http.post("/api/local-session/bootstrap", content=b"x" * 2048,
            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 400


def test_bootstrap_and_session_expiry_idle_and_absolute():
    now = [100.0]
    with client(application(clock=lambda: now[0])) as http:
        token = http.post("/api/local-session/issue", json={}, headers={CONTROL_HEADER: "synthetic-local-control-only"}).json()["bootstrap"]
        now[0] += 121
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": token},
            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 401
        bootstrap(http)
        now[0] += 1801
        assert http.get("/api/private").status_code == 401
        bootstrap(http)
        for _ in range(28):
            now[0] += 1000
            assert http.get("/api/private").status_code == 200
        now[0] += 1000
        assert http.get("/api/private").status_code == 401


def test_server_token_is_scoped_and_browser_cannot_adopt_it():
    app = FastAPI()
    install_admin_boundary(app, "synthetic-server-token", control_token="synthetic-control")

    @app.get("/api/internal/fixture")
    def internal():
        return {"fixture": True}

    with client(app) as http:
        headers = {ADMIN_TOKEN_HEADER: "synthetic-server-token"}
        assert http.get("/api/internal/fixture", headers=headers).status_code == 200
        assert http.get("/api/internal/fixture", headers={**headers, "Origin": "http://127.0.0.1:8765"}).status_code == 401
        token = app.state.local_sessions.issue_bootstrap("127.0.0.1:8765")
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": token},
            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert http.get("/api/internal/fixture").status_code == 403
        assert http.get("/api/internal/fixture", headers={**headers, "Sec-Fetch-Site": "same-origin"}).status_code == 403
        result = http.post("/api/internal/local-session/bootstrap", json={}, headers=headers)
        assert result.status_code == 200 and result.json()["audience"] == "product"


@pytest.mark.skipif(os.name != "nt", reason="current-user DPAPI")
def test_control_storage_is_encrypted_scope_bound_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    value = load_control_token("admin", 9876)
    assert value == load_control_token("admin", 9876, create=False)
    stored = next(tmp_path.rglob("*.dpapi")).read_bytes()
    assert value.encode() not in stored
    assert load_control_token("product", 9876) != value
