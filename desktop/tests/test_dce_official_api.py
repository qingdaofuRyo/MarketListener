import json
from pathlib import Path
import threading
from requests.exceptions import ConnectTimeout, SSLError

from market_monitor.dce_authorized_backfill import ApiFailure, HTTP_BASE
import market_monitor.dce_official_api as module
from market_monitor.storage import MarketStore
from market_monitor.market_data_version import market_data_version


ACCOUNTS = [{"api_key": "synthetic-A", "secret": "synthetic-secret-A"},
            {"api_key": "synthetic-B", "secret": "synthetic-secret-B"}]


class Client:
    def __init__(self, key, *, barrier=None, failed=False):
        self.key, self.barrier, self.failed = key, barrier, failed
        self.calls = 0

    def request(self, endpoint, body, **kwargs):
        self.calls += 1
        if self.failed:
            raise ApiFailure(501)
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if endpoint == "dayQuotes":
            rows = [{"variety": "豆一", "contractId": "a2611", "open": 100, "high": 110,
                     "low": 90, "close": 105, "clearPrice": 104, "lastClear": 102,
                     "volumn": 20, "openInterest": 10, "turnover": 2}]
        else:
            rows = [{"contractId": "a2611", "clearPrice": 104,
                     "specBuyRate": ".1", "specSellRate": ".12"}]
        payload = {"success": True, "code": 200, "data": rows}
        return payload, json.dumps(payload).encode()

    def close(self):
        pass


def test_two_accounts_capture_in_parallel_and_only_parent_imports(tmp_path, monkeypatch):
    silver_revision = market_data_version(tmp_path)
    barrier = threading.Barrier(2)

    def factory(root, key, secret, *, account_policy):
        assert account_policy == (1, 3)
        return Client(key, barrier=barrier)

    monkeypatch.setattr(module, "DceClient", factory)
    monkeypatch.setattr(module, "load_accounts", lambda: ACCOUNTS)
    writer_threads = []
    method = MarketStore.record_futures_official_daily

    def record(store, rows, coverage):
        writer_threads.append(threading.get_ident())
        return method(store, rows, coverage)

    monkeypatch.setattr(MarketStore, "record_futures_official_daily", record)
    report = module.update_official_dce_api(tmp_path, ["2026-09-30"])
    assert report["status"] == "PASS"
    assert report["importedRows"] == {"daily": 1, "settlement": 1}
    assert report["pendingDays"] == 0
    assert writer_threads == [threading.get_ident()]
    assert market_data_version(tmp_path) == silver_revision
    store = MarketStore(tmp_path)
    try:
        margin = store.connection.execute("SELECT source_date_basis,spec_long_rate,spec_short_rate "
                                          "FROM futures_settlement_parameter_official").fetchone()
        assert margin == ("AUTHORIZED_API_REQUEST_BODY", .1, .12)
    finally:
        store.close()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"synthetic-secret" not in path.read_bytes()


def test_approved_http_transport_is_explicit_in_capture_status(tmp_path):
    class HttpClient(Client):
        base_url = "http://www.dce.com.cn"

    report = module.capture_items(
        tmp_path, [("daily", "2026-09-30")], accounts=ACCOUNTS,
        client_factory=lambda root, key, secret, **kwargs: HttpClient(key),
    )
    assert report["transportStatus"] == "USER_APPROVED_HTTP"
    assert report["accounts"]["A"]["requestScheme"] == "http"
    assert report["accounts"]["B"]["requestScheme"] is None
    assert report["outcomes"][0]["source_url"].startswith("http://www.dce.com.cn/")


def test_http_api_provenance_survives_bronze_parser_and_catalog(tmp_path, monkeypatch):
    class HttpClient(Client):
        base_url = HTTP_BASE

    monkeypatch.setattr(module, "load_accounts", lambda: ACCOUNTS)
    monkeypatch.setattr(module, "DceClient", lambda root, key, secret, **kwargs: HttpClient(key))
    report = module.update_official_dce_api(tmp_path, ["2026-09-30"])
    assert report["status"] == "PASS"
    for kind in ("daily", "settlement"):
        meta = json.loads((module._directory(tmp_path, kind, "2026-09-30") / "result.json").read_text())
        assert meta["source_url"].startswith("http://www.dce.com.cn/dceapi/")
        assert meta["request_evidence"]["source_url"] == meta["source_url"]
    store = MarketStore(tmp_path)
    try:
        daily = store.connection.execute("SELECT DISTINCT source_url FROM futures_contract_daily_official WHERE exchange='DCE'").fetchall()
        margin = store.connection.execute("SELECT DISTINCT source_url FROM futures_settlement_parameter_official WHERE exchange='DCE'").fetchall()
    finally:
        store.close()
    assert len(daily) == len(margin) == 1
    assert daily[0][0].startswith("http://www.dce.com.cn/dceapi/")
    assert margin[0][0].startswith("http://www.dce.com.cn/dceapi/")


def test_501_account_does_not_stop_second_account(tmp_path):
    clients = {}

    def factory(root, key, secret, **kwargs):
        clients[key] = Client(key, failed=key == "synthetic-A")
        return clients[key]

    report = module.capture_items(tmp_path, [("daily", day) for day in
        ("2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28")],
        accounts=ACCOUNTS, client_factory=factory)
    assert report["status"] == "PARTIAL"
    assert clients["synthetic-A"].calls == 1
    assert clients["synthetic-B"].calls == 2
    assert report["accounts"]["A"]["status"] == "COOLDOWN_WAIT"
    assert report["accounts"]["B"]["successfulResponses"] == 2
    assert sum(item["status"] == "PASS" for item in report["outcomes"]) == 2


def test_tls_failure_is_visible_without_stopping_other_account(tmp_path):
    class TlsClient(Client):
        def request(self, endpoint, body, **kwargs):
            if self.key == "synthetic-A":
                raise SSLError("synthetic certificate verification failure")
            return super().request(endpoint, body, **kwargs)

    report = module.capture_items(tmp_path, [("daily", "2026-09-23"),
                                              ("daily", "2026-09-24")],
                                  accounts=ACCOUNTS,
                                  client_factory=lambda root, key, secret, **kwargs: TlsClient(key))
    assert report["status"] == "TLS_BLOCKED"
    assert report["transportStatus"] == "TLS_VERIFICATION_FAILED"
    assert report["accounts"]["A"]["status"] == "TLS_BLOCKED"
    assert report["accounts"]["B"]["successfulResponses"] == 1


def test_connect_timeout_is_reported_as_network_transport_failure(tmp_path):
    class TimedOutClient(Client):
        def request(self, endpoint, body, **kwargs):
            raise ConnectTimeout("synthetic connection timeout")

    report = module.capture_items(
        tmp_path,
        [("daily", day) for day in ("2026-10-12", "2026-10-13", "2026-10-14")],
        accounts=ACCOUNTS,
        client_factory=lambda root, key, secret, **kwargs: TimedOutClient(key),
    )
    assert report["status"] == "PARTIAL"
    assert report["transportStatus"] == "NETWORK_ERROR"
    assert all(account["transportStatus"] == "NETWORK_ERROR" for account in report["accounts"].values())
    assert all(account["successfulResponses"] == 0 for account in report["accounts"].values())
    assert all(item["status"] in {"FAILED", "DEFERRED"} for item in report["outcomes"])


def test_coverage_requires_facts_and_original_and_reports_margin_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "load_accounts", lambda: ACCOUNTS)
    monkeypatch.setattr(module, "DceClient", lambda root, key, secret, **kwargs: Client(key))
    days = ["2026-09-30"]
    assert module.update_official_dce_api(tmp_path, days)["pendingDays"] == 0
    # A stale PASS marker cannot hide a missing formal row. Repair from its raw.
    store = MarketStore(tmp_path)
    try:
        store.connection.execute("DELETE FROM futures_settlement_parameter_official")
    finally:
        store.close()
    repaired = module.update_official_dce_api(tmp_path, days)
    assert repaired["importedRows"] == {"daily": 0, "settlement": 1}
    assert repaired["pendingSettlementDays"] == 0
    # Preserved evidence is also required; an API refusal leaves a margin gap.
    directory = module._directory(tmp_path, "settlement", days[0])
    meta = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    (directory / meta["raw_file"]).write_bytes(b"corrupted-original")
    monkeypatch.setattr(module, "DceClient", lambda root, key, secret, **kwargs: Client(key, failed=True))
    partial = module.update_official_dce_api(tmp_path, days)
    assert partial["status"] == "PARTIAL"
    assert partial["pendingDailyDays"] == 0
    assert partial["pendingSettlementDays"] == partial["pendingDays"] == 1
