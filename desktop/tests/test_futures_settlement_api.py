from hashlib import sha256
import json

from fastapi.testclient import TestClient

from market_monitor.futures_settlement_parameters import import_partition, parse_shfe
from market_monitor.storage import MarketStore
from market_monitor.web_app import create_web_app


def test_settlement_api_preserves_speculative_sides_and_exact_date(tmp_path):
    raw = json.dumps({"report_date": "20260929", "o_cursor": [{
        "INSTRUMENTID": "cu2610", "SPECLONGMARGINRATIO": 0.11,
        "SPECSHORTMARGINRATIO": 0.12, "HEDGLONGMARGINRATIO": 0.05,
        "HEDGSHORTMARGINRATIO": 0.05}]}).encode()
    url = "https://www.shfe.com.cn/data/tradedata/future/dailydata/js20260929.dat"
    stamp = "2026-10-07T00:00:00+00:00"
    rows = parse_shfe(raw, trading_day="2026-09-29", source_url=url, retrieved_at=stamp)
    store = MarketStore(tmp_path)
    import_partition(store.connection, rows, exchange="SHFE", trade_date="2026-09-29",
                     source_url=url, payload_sha256=sha256(raw).hexdigest(), retrieved_at=stamp)
    # Reading through the service must work with an existing catalog connection.
    client = TestClient(create_web_app(tmp_path))
    response = client.get("/api/futures/settlement-parameters", params={
        "trade_date": "2026-09-29", "exchange": "SHFE", "contract_code": "CU2610"})
    assert response.status_code == 200
    result = response.json()["rows"][0]
    assert result["spec_long_rate"] == 0.11
    assert result["spec_short_rate"] == result["standard_margin_rate"] == 0.12
    assert result["payload_sha256"] == sha256(raw).hexdigest()
    assert client.get("/api/futures/settlement-parameters", params={"trade_date": "2026-09-30"}).json()["rows"] == []
    assert client.get("/api/futures/settlement-parameters", params={"trade_date": "2026-09-29", "exchange": "BAD"}).status_code == 422
    store.close()


def test_startup_migrates_older_settlement_columns_before_readiness(tmp_path, monkeypatch):
    from market_monitor.web_api import market

    store = MarketStore(tmp_path)
    store.connection.execute("DROP INDEX settlement_parameter_day_idx")
    store.connection.execute("ALTER TABLE futures_settlement_parameter_official DROP COLUMN published_on")
    store.connection.execute("CREATE INDEX settlement_parameter_day_idx ON futures_settlement_parameter_official(trade_date,exchange,contract_code)")
    store.close()
    monkeypatch.setattr(market, "prime_market_list_snapshots", lambda root: None)
    with TestClient(create_web_app(tmp_path)) as client:
        assert client.get("/api/ready").status_code == 200
        result = client.get("/api/futures/settlement-parameters", params={"trade_date": "2026-09-30"})
        assert result.status_code == 200
        assert result.json()["rows"] == []
