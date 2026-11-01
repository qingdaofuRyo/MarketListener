"""The account trash is changed only by an explicit scoped maintenance request."""

from __future__ import annotations

from fastapi.testclient import TestClient

from market_monitor.account_analysis import _connect, create_account, get_account, soft_delete_account
from market_monitor.admin_boundary import ADMIN_TOKEN_HEADER
from market_monitor.web_app import create_customer_web_app


def test_account_read_does_not_purge_and_maintenance_requires_exact_preview(tmp_path):
    root = tmp_path / "data"
    account = create_account(root, {"name": "synthetic-account", "startDate": "2026-01-01",
                                    "initialEquity": 10_000})
    identifier = account["id"]
    soft_delete_account(root, identifier)
    with _connect(root) as connection:
        connection.execute("UPDATE accounts SET deleted_at=? WHERE id=?",
                           ["2026-01-02T00:00:00+00:00", identifier])

    app = create_customer_web_app(root, admin_token="synthetic-maintenance-token")
    with TestClient(app, base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 42000)) as http:
        bootstrap = app.state.local_sessions.issue_bootstrap("127.0.0.1:8765")
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": bootstrap},
                         headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert http.get("/api/stats/accounts").json()["total"] == 0
        assert get_account(root, identifier, include_deleted=True)["id"] == identifier

        preview_path = "/api/internal/accounts/trash/purge-preview"
        purge_path = "/api/internal/accounts/trash/purge"
        assert http.get(preview_path).status_code == 403
        headers = {ADMIN_TOKEN_HEADER: "synthetic-maintenance-token"}
        preview = http.get(preview_path, headers=headers).json()
        assert preview["accountIds"] == [identifier]
        assert http.post(purge_path, json={"confirmationDigest": "0" * 64},
                         headers=headers).status_code == 409
        assert get_account(root, identifier, include_deleted=True)["id"] == identifier
        response = http.post(purge_path, json={"confirmationDigest": preview["confirmationDigest"]},
                             headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["purgedAccounts"] == 1
        with _connect(root) as connection:
            assert connection.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM account_snapshots").fetchone()[0] == 0
