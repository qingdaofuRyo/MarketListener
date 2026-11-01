import json
from hashlib import sha256

import pytest

from market_monitor import dce_authorized_backfill
from market_monitor.dce_authorized_backfill import ApiFailure, DceClient, backfill, contract_manifest, parse_ranking
from market_monitor.futures_calendar import CALENDAR_SCHEMA, CALENDAR_PROVIDER
from market_monitor.storage import MarketStore


def _http_consent(root, *keys):
    path = root / dce_authorized_backfill.HTTP_CONSENT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": dce_authorized_backfill.HTTP_CONSENT_SCHEMA,
        "scheme": "http", "host": "www.dce.com.cn",
        "account_key_sha256": [sha256(key.encode()).hexdigest() for key in keys],
    }), encoding="utf-8")
    return path


def test_http_transport_requires_exact_local_two_account_consent(tmp_path):
    key_a, key_b = "synthetic-A", "synthetic-B"
    assert dce_authorized_backfill.authorized_base(tmp_path, key_a) == dce_authorized_backfill.BASE
    path = _http_consent(tmp_path, key_a, key_b)
    assert dce_authorized_backfill.authorized_base(tmp_path, key_a) == dce_authorized_backfill.HTTP_BASE
    assert dce_authorized_backfill.authorized_base(tmp_path, key_b) == dce_authorized_backfill.HTTP_BASE
    assert dce_authorized_backfill.authorized_base(tmp_path, "synthetic-C") == dce_authorized_backfill.BASE
    consent = json.loads(path.read_text(encoding="utf-8"))
    for change in ({"host": "other.example"}, {"scheme": "https"}, {"account_key_sha256": consent["account_key_sha256"][:1]},
                   {"unexpected": True}):
        path.write_text(json.dumps({**consent, **change}), encoding="utf-8")
        assert dce_authorized_backfill.authorized_base(tmp_path, key_a) == dce_authorized_backfill.BASE
    assert "synthetic-A" not in path.read_text(encoding="utf-8")


def test_client_sends_to_exact_opted_in_http_host_without_redirect_and_rechecks_consent(tmp_path, monkeypatch):
    path = _http_consent(tmp_path, "synthetic-A", "synthetic-B")
    client = DceClient(tmp_path, "synthetic-A", "synthetic-secret-A", interval=0)
    calls = []

    class Response:
        status_code = 200
        headers = {}
        content = b'{"success":true,"code":200,"data":{}}'

        def json(self):
            return {"success": True, "code": 200, "data": {}}

    monkeypatch.setattr(client.http.session, "post", lambda *args, **kwargs: (calls.append((args, kwargs)), Response())[1])
    try:
        assert client._post("/dceapi/cms/auth/accessToken", {"secret": "synthetic-secret-A"}, auth=True)[0]["code"] == 200
        assert calls[0][0][0] == "http://www.dce.com.cn/dceapi/cms/auth/accessToken"
        assert calls[0][1]["allow_redirects"] is False
        with pytest.raises(RuntimeError, match="仅限"):
            client._post("//other.example/dceapi/cms/auth/accessToken", {}, auth=True)
        path.unlink()
        with pytest.raises(RuntimeError, match="仅限"):
            client._post("/dceapi/cms/auth/accessToken", {}, auth=True)
        assert len(calls) == 1
    finally:
        client.close()


def test_manifest_excludes_summary_options_and_keeps_monthly_average():
    assert contract_manifest({"data": [{"contractId": code} for code in
        ("a2611", "A2611", "pp2610F", "a2611-C-5000", None, "m2701")]}) == ["a2611", "m2701", "pp2610F"]


def test_monthly_average_ranking_identity_stays_separate():
    payload = {"data": {"contractId": "pp2611F", "qtyFutureList": [], "sellFutureList": [],
                        "buyFutureList": [{"rank": "1", "buyAbbr": "合成会员", "todayBuyQty": 10, "buySub": 0}]}}
    positions, _ = parse_ranking(payload, "2026-10-08", "pp2611F", "a" * 64, "2026-10-09T00:00:00+08:00")
    assert positions[0]["contract_code"] == "PP-F2611"
    assert positions[0]["product_code"] == "PP-F"
    assert dce_authorized_backfill.ranking_variety("pp2611F") == "ppf"
    payload["data"]["contractId"] = "期货公司会员"
    assert len(parse_ranking(payload, "2026-10-08", "pp2611F", "a" * 64,
                             "2026-10-09T00:00:00+08:00")[0]) == 1


def test_three_directions_are_independent_and_summaries_excluded():
    payload = {"data": {
        "qtyFutureList": [{"rank": "1", "qtyAbbr": "甲", "todayQty": 12, "qtySub": -2}],
        "buyFutureList": [{"rank": "1", "buyAbbr": "乙", "todayBuyQty": 0, "buySub": None},
                          {"rank": "合计", "todayBuyQty": 0}],
        "sellFutureList": [],
    }}
    positions, trades = parse_ranking(payload, "2026-09-29", "a2611", "a" * 64, "2026-10-02T00:00:00+08:00")
    assert positions[0]["side"] == "LONG"
    assert positions[0]["position"] == 0
    assert positions[0]["position_change"] is None
    assert len(positions) == len(trades) == 1
    assert trades[0]["volume_change"] == -2
    assert trades[0]["publisher"] == "DCE"
    assert json.loads(json.dumps(positions))[0]["contract_code"] == "A2611"


def test_missing_direction_and_duplicate_rank_are_rejected():
    with pytest.raises(ValueError, match="Missing ranking direction"):
        parse_ranking({"data": {}}, "2026-09-29", "a2611", "a" * 64, "2026-10-02T00:00:00+08:00")
    row = {"rank": "1", "qtyAbbr": "甲", "todayQty": 12, "qtySub": 0}
    with pytest.raises(ValueError, match="Duplicate"):
        parse_ranking({"data": {"qtyFutureList": [row, row], "buyFutureList": [], "sellFutureList": []}},
                      "2026-09-29", "a2611", "a" * 64, "2026-10-02T00:00:00+08:00")


def test_backfill_resumes_each_contract_and_does_not_replace_other_sources(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "cn_futures_trading_calendar.json").write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER, "market": "CN_FUTURE",
        "retrievedAt": "2026-10-02T00:00:00+08:00", "tradingDays": ["2025-01-02"],
    }), encoding="utf-8")

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.fail = True

        def request(self, endpoint, body):
            self.calls.append((endpoint, body.get("contractId")))
            if endpoint == "dayQuotes":
                payload = {"data": [{"contractId": "a2505"}, {"contractId": "m2505"}]}
            else:
                if body["contractId"] == "m2505" and self.fail:
                    raise ApiFailure(501)
                payload = {"data": {"qtyFutureList": [], "buyFutureList": [
                    {"rank": "1", "buyAbbr": "合成会员", "todayBuyQty": 10, "buySub": 2}],
                    "sellFutureList": []}}
            return payload, json.dumps(payload).encode()

    client = FakeClient()
    first = backfill(tmp_path, client, "2025-01-01", "2025-01-02")
    assert first["status"] == "FAILED"
    assert first["apiCode"] == 501
    assert first["processedContracts"] == 1
    client.fail = False
    client.calls.clear()
    second = backfill(tmp_path, client, "2025-01-01", "2025-01-02")
    assert second["status"] == "PASS"
    assert client.calls == [("memberDealPosi", "m2505")]
    store = MarketStore(tmp_path)
    try:
        ranks = store.list_futures_member_position_ranks(trading_day="2025-01-02", exchange="DCE")
        coverage = store.list_futures_member_position_coverage(trading_day="2025-01-02")
    finally:
        store.close()
    assert len(ranks) == 2
    assert coverage[0]["status"] == "PASS"
    client.calls.clear()
    assert backfill(tmp_path, client, "2025-01-01", "2025-01-02")["completedDays"] == 1
    assert client.calls == []


def test_capped_backfill_reports_committed_day_counts_for_automatic_continuation(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "cn_futures_trading_calendar.json").write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER, "market": "CN_FUTURE",
        "retrievedAt": "2026-10-02T00:00:00+08:00", "tradingDays": ["2025-01-02"],
    }), encoding="utf-8")

    class Client:
        def request(self, endpoint, body):
            payload = ({"data": [{"contractId": "a2505"}, {"contractId": "m2505"}]}
                       if endpoint == "dayQuotes" else {"data": {
                           "qtyFutureList": [], "sellFutureList": [], "buyFutureList": [
                               {"rank": "1", "buyAbbr": "合成会员", "todayBuyQty": 10, "buySub": 0}],
                       }})
            return payload, json.dumps(payload).encode()

    first = backfill(tmp_path, Client(), "2025-01-02", "2025-01-02", max_contracts=1)
    assert first["status"] == "BATCH_LIMIT"
    assert first["currentDay"] == "2025-01-02"
    assert first["processedContracts"] == first["dayCompletedContracts"] == first["dayPendingContracts"] == 1
    assert first["dayTotalContracts"] == 2
    second = backfill(tmp_path, Client(), "2025-01-02", "2025-01-02", max_contracts=1)
    assert second["status"] == "PASS"
    assert second["processedContracts"] == 1
    assert second["dayCompletedContracts"] == second["dayTotalContracts"] == 2
    assert second["dayPendingContracts"] == 0


@pytest.mark.parametrize("throttle_code", [429, 501])
@pytest.mark.parametrize("retry_code", [200, 501])
def test_adaptive_speed_honors_cooldown_and_one_downgrade(tmp_path, monkeypatch, retry_code, throttle_code):
    client = DceClient(tmp_path, "synthetic-test-key", "synthetic-test-secret", 0.2, adaptive_rate=True)
    client.token, client.expires_at, client.cooldown_until = "synthetic-test-token", 1000, 220
    codes, waits, calls = iter([throttle_code, retry_code]), [], []
    monkeypatch.setattr(dce_authorized_backfill.time, "monotonic", lambda: 100)
    monkeypatch.setattr(dce_authorized_backfill.time, "sleep", waits.append)

    def respond(path, body):
        calls.append(path)
        code = next(codes)
        if code == 429:
            raise ApiFailure(429)
        return {"success": code == 200, "code": code, "data": {}}, b"{}"

    monkeypatch.setattr(client, "_post", respond)
    try:
        if retry_code == 200:
            assert client.request("memberDealPosi", {})[0]["code"] == 200
        else:
            with pytest.raises(ApiFailure):
                client.request("memberDealPosi", {})
        assert waits == [120]
        assert client.interval == 2
        assert client.throttle_events == 1
        assert len(calls) == 2
    finally:
        client.close()


def test_malformed_retry_after_preserves_minimum_cooldown(tmp_path, monkeypatch):
    client = DceClient(tmp_path, "synthetic-test-key", "synthetic-test-secret")
    completed = []

    class Response:
        status_code = 429
        headers = {"Retry-After": "synthetic-invalid"}

    monkeypatch.setattr(client.http.session, "post", lambda *a, **kw: Response())
    monkeypatch.setattr(client.http.limiter, "acquire", lambda _: None)
    monkeypatch.setattr(client.http.limiter, "complete", lambda host, **kw: completed.append(kw))
    monkeypatch.setattr(dce_authorized_backfill.time, "monotonic", lambda: 100)
    try:
        with pytest.raises(ApiFailure):
            client._post("/dceapi/cms/auth/accessToken", {}, auth=True)
        assert completed == [{"retry_after_seconds": 120}]
        assert client.cooldown_until == 220
    finally:
        client.close()
