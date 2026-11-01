"""Production market list reads only a verified immutable publication."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from market_monitor.market_data_version import advance_market_data_version
from market_monitor.market_query_cache import get_kline_query_store
from market_monitor.market_query_cache import QUERY_INDEX_SCHEMA_VERSION
from market_monitor.market_readmodel import publish_market_readmodel
from market_monitor.web_api import market
from market_monitor.web_app import create_customer_web_app
from web_fixtures import silver_row, write_silver


def test_product_list_filters_and_pages_published_rows_without_source_scans(tmp_path, monkeypatch):
    root = tmp_path / "data"
    app = create_customer_web_app(root, admin_token="synthetic-admin-token")
    with TestClient(app, base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 42000)) as http:
        bootstrap = app.state.local_sessions.issue_bootstrap("127.0.0.1:8765")
        http.post("/api/local-session/bootstrap", json={"bootstrap": bootstrap},
                  headers={"Origin": "http://127.0.0.1:8765"})
        path = "/api/market/instruments?includeExpired=true&pageSize=1&sortField=latestPrice"
        assert http.get(path).json()["snapshotStatus"] == "NO_DATA"
        (root / "silver").mkdir()
        assert http.get(path).status_code == 503
        version = market._response_data_version(root)
        assert f":query-index-{QUERY_INDEX_SCHEMA_VERSION}:" in version
        rows = [
            {"instrumentId": "CN.DCE.FUTURE.A2611", "name": "豆一2611", "symbol": "A2611",
             "assetType": "FUTURE", "seriesKind": "CONTRACT", "market": "CN", "exchange": "DCE",
             "latestPrice": 4500.0},
            {"instrumentId": "CN.DCE.FUTURE.M2701", "name": "豆粕2701", "symbol": "M2701",
             "assetType": "FUTURE", "seriesKind": "CONTRACT", "market": "CN", "exchange": "DCE",
             "latestPrice": 2800.0},
        ]
        publish_market_readmodel(root, version=version, items=rows,
                                 source_policy_version="synthetic-policy")
        publish_market_readmodel(root, version=version, items=rows,
                                 identity="category:cn-future-dce",
                                 source_policy_version="synthetic-policy")
        monkeypatch.setattr(market, "load_inventory", lambda *_a: (_ for _ in ()).throw(
            AssertionError("published GET must not scan source inventory")))
        first_response = http.get(path)
        assert "snapshot;dur=" in first_response.headers["Server-Timing"]
        first = first_response.json()
        assert first["items"][0]["symbol"] == "A2611"
        assert first["total"] == 2
        assert first["queryVersion"] == version
        second = http.get(path + f"&page=2&version={version}").json()
        assert second["items"][0]["symbol"] == "M2701"
        assert http.get(path + "&q=豆粕").json()["total"] == 1
        assert http.get(path + "&categoryKey=cn-future-dce").json()["total"] == 2
        f10 = root / "industry" / "f10" / "cn_f10.jsonl"
        f10.parent.mkdir(parents=True)
        f10.write_text("{}\n", encoding="utf-8")
        assert market._response_data_version(root) != version
        stale = http.get(path + f"&version={version}").json()
        assert stale["snapshotStatus"] == "STALE"
        assert stale["dataVersion"] == version


def test_writer_can_publish_a_complete_small_fixture_then_reader_uses_it(tmp_path):
    root = tmp_path / "data"
    write_silver(root, [silver_row("CN.SSE.STOCK.600519", "2026-10-09", close=1500)])
    market.prime_market_list_snapshots(root)
    app = create_customer_web_app(root)
    with TestClient(app, base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 42000)) as http:
        bootstrap = app.state.local_sessions.issue_bootstrap("127.0.0.1:8765")
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": bootstrap},
                         headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
        response = http.get("/api/market/instruments?categoryKey=a-sh&pageSize=200")
        assert response.status_code == 200, response.text
        assert response.json()["snapshotStatus"] == "CURRENT"
        assert response.json()["total"] == 1
        for path in (
            "/api/market/instruments/CN.SSE.STOCK.600519/bars?period=1d&limit=1",
            "/api/market/instruments/CN.SSE.STOCK.600519/bars/history?period=1d&size=1",
        ):
            chart = http.get(path)
            assert chart.status_code == 200, chart.text
            assert chart.json()["bars"]
            assert all(f"{stage};dur=" in chart.headers["Server-Timing"]
                       for stage in ("resolve", "history", "encode", "app"))


def test_stale_chart_index_returns_retryable_503_while_list_snapshot_remains_readable(tmp_path, monkeypatch):
    root = tmp_path / "data"
    write_silver(root, [silver_row("CN.SSE.STOCK.600519", "2026-10-09", close=1500)])
    market.prime_market_list_snapshots(root)
    from market_monitor.market_query_cache import get_kline_query_store

    store = get_kline_query_store(root)
    monkeypatch.setattr(store, "_start_background_rebuild", lambda _revision: None)
    advance_market_data_version(root)
    app = create_customer_web_app(root)
    with TestClient(app, base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 42000)) as http:
        bootstrap = app.state.local_sessions.issue_bootstrap("127.0.0.1:8765")
        assert http.post("/api/local-session/bootstrap", json={"bootstrap": bootstrap},
                         headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
        listing = http.get("/api/market/instruments?categoryKey=a-sh&pageSize=200")
        assert listing.status_code == 200
        assert listing.json()["snapshotStatus"] == "STALE"
        chart = http.get("/api/market/instruments/CN.SSE.STOCK.600519/bars/history?period=1d")
        assert chart.status_code == 503
        assert chart.json()["code"] == "KLINE_INDEX_REBUILDING"
        assert chart.headers["Retry-After"] == "5"


def test_product_startup_migrates_exact_legacy_revision_before_index_audit(tmp_path):
    root = tmp_path / "data"
    write_silver(root, [silver_row("CN.SSE.STOCK.600519", "2026-10-09", close=1500)])
    state_file = root / "state" / "market_data_version.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"schemaVersion": 1, "revision": "legacy"}), encoding="utf-8")

    app = create_customer_web_app(root, auto_publish_on_startup=False)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 42000)):
        migrated = json.loads(state_file.read_text(encoding="utf-8"))
        assert migrated["revision"].startswith("migrated-")
        assert migrated["journal"][-1]["kind"] == "UNKNOWN"
        index = get_kline_query_store(root).rebuild()
        assert index.ready is True
        assert index.revision == migrated["revision"]
