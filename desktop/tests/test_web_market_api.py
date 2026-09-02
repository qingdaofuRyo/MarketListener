"""API tests for /api/market and /api/personal/watchlist."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_monitor.market_query_cache import get_kline_query_store
from market_monitor.storage import MarketStore, PartitionKey
from market_monitor.web_app import create_web_app
from market_monitor.web_api.market import _normalize_future_name, _normalize_tdx_instrument
from web_fixtures import silver_row, write_silver


def _data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    write_silver(
        data_root,
        [
            silver_row("CN.SSE.STOCK.600519", "2026-08-06", close=1510.0),
            silver_row("CN.SSE.STOCK.600519", "2026-08-07", close=1520.0),
            silver_row("HK.HKEX.STOCK.00700", "2026-08-07", market="HK", close=480.0),
            silver_row(
                "CN.SHFE.FUTURE.AU0",
                "2026-08-07",
                market="CN",
                asset_type="FUTURE",
                period="30m",
                close=780.0,
            ),
        ],
    )
    return data_root


def _app(tmp_path: Path) -> tuple[FastAPI, TestClient]:
    application = create_web_app(_data_root(tmp_path))
    return application, TestClient(application, client=("127.0.0.1", 50000))


def test_market_overview_is_local_and_compact(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    response = client.get("/api/market/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["instruments"] == 3
    assert payload["rows"] == 4
    assert payload["markets"] == {"CN": 2, "HK": 1}
    assert payload["assetTypes"] == {"STOCK": 2, "FUTURE": 1}
    assert payload["periods"] == ["1d", "30m"]
    assert "NaN" not in response.text
    assert "undefined" not in response.text


def test_market_groups_and_data_source_inventory_are_local_and_traceable(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    groups = client.get("/api/market/groups")
    assert groups.status_code == 200
    assert groups.json()["total"] == 3
    stock = next(item for item in groups.json()["items"] if item["assetType"] == "STOCK")
    assert stock["instruments"] == 1
    assert stock["fieldCompleteness"]["close"] == 1
    assert stock["sourceDetails"] == [
        {
            "providerId": "fixture",
            "name": "fixture",
            "endpoint": None,
            "status": "UNREGISTERED_SOURCE",
            "periods": [],
            "fields": [],
            "fieldNotes": None,
        }
    ]

    response = client.get("/api/data-sources")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["categories"] == 3
    assert payload["summary"]["instruments"] == 3
    assert any(item["providerId"] == "pytdx" for item in payload["providers"])
    pytdx = next(item for item in payload["providers"] if item["providerId"] == "pytdx")
    jqdata = next(item for item in payload["providers"] if item["providerId"] == "joinquant")
    assert pytdx["enabled"] is True and pytdx["priority"] == 10
    assert pytdx["fields"] == ["open", "high", "low", "close", "volume", "amount"]
    assert jqdata["enabled"] is False and jqdata["configured"] is False
    assert any(item["categoryKey"] == "CN:STOCK:1d" for item in payload["inventory"])
    stock_inventory = next(item for item in payload["inventory"] if item["categoryKey"] == "CN:STOCK:1d")
    assert stock_inventory["fieldCompleteness"]["settlement"] == 0


def test_data_source_preferences_persist_and_mutations_remain_loopback_only(tmp_path: Path) -> None:
    application, loopback = _app(tmp_path)
    body = {"preferences": {"CN:STOCK:1d": {"primary": "pytdx", "fallback1": "baostock", "fallback2": "custom-feed"}}}
    saved = loopback.put("/api/data-sources", json=body)
    assert saved.status_code == 200
    assert saved.json()["preferences"] == body["preferences"]
    assert loopback.get("/api/data-sources").json()["preferences"] == body["preferences"]
    assert TestClient(application).put("/api/data-sources", json=body).status_code == 403


def test_data_source_inventory_traces_registered_provider_to_endpoint(tmp_path: Path) -> None:
    data_root = tmp_path / "registered-source"
    row = silver_row("CN.SSE.STOCK.600519", "2026-08-07")
    row["source"] = "pytdx"
    write_silver(data_root, [row])
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    inventory = client.get("/api/data-sources").json()["inventory"]
    detail = inventory[0]["sourceDetails"]
    assert detail[0]["providerId"] == "pytdx"
    assert detail[0]["status"] == "IMPLEMENTED_UNVERIFIED"
    assert "TCP/7709" in detail[0]["endpoint"]
    assert detail[0]["fieldNotes"]


def test_tdx_normalization_diagnostics_read_latest_audit_report(tmp_path: Path) -> None:
    data_root = tmp_path / "tdx-diagnostics"
    report = data_root / "reports" / "tdx-local" / "latest-audit.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps({
            "状态": "完成",
            "标准化版本": "tdx-cn-v2",
            "生成时间": "2026-08-29 12:00:00",
            "扫描文件": 24,
            "导入文件": 23,
            "写入K线": 1000,
            "隔离文件": 1,
            "隔离K线": 2,
            "资产文件统计": {"STOCK": 20},
            "成交量倍率统计": {"STOCK:1d:1.0": 20},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    payload = client.get("/api/data-sources/tdx-local-normalization").json()

    assert payload["available"] is True
    assert payload["normalizationVersion"] == "tdx-cn-v2"
    assert payload["quarantinedFiles"] == 1


def test_data_source_browser_uses_manifest_metadata_without_reopening_silver_rows(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    get_kline_query_store(data_root).rebuild()
    # Once the producer-maintained manifest exists, inventory must remain
    # available even when a Parquet body cannot be opened.  This guards
    # against accidentally restoring a full Silver scan to page bootstrap.
    (data_root / "silver" / "fixture.parquet").write_bytes(b"not-a-parquet-body")
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    response = client.get("/api/data-sources/inventory")
    provider_response = client.get("/api/data-sources/providers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["mode"] == "LIGHTWEIGHT_MANIFEST"
    assert payload["metadata"]["scansSilverRows"] is False
    assert payload["summary"]["rows"] == 4
    silver_table = next(item for item in payload["tables"] if item["tableId"] == "silver.CN:STOCK:1d")
    assert silver_table["rowCountMode"] == "MANIFEST_EXACT"
    assert silver_table["partitions"] == 1
    assert {field["name"]: field["type"] for field in silver_table["fields"]}["close"] == "DOUBLE"
    assert provider_response.status_code == 200
    assert any(item["providerId"] == "tdx_local" for item in provider_response.json()["items"])


def test_market_instruments_paginate_search_and_filter(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    first = client.get("/api/market/instruments", params={"pageSize": 2, "page": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert all(item["actualSource"] == "fixture" for item in body["items"])

    second = client.get("/api/market/instruments", params={"pageSize": 2, "page": 2})
    assert len(second.json()["items"]) == 1

    search = client.get("/api/market/instruments", params={"q": "600519"})
    assert search.json()["total"] == 1
    assert search.json()["items"][0]["instrumentId"] == "CN.SSE.STOCK.600519"

    hk = client.get("/api/market/instruments", params={"market": "HK"})
    assert hk.json()["total"] == 1
    assert hk.json()["items"][0]["instrumentId"] == "HK.HKEX.STOCK.00700"

    futures = client.get("/api/market/instruments", params={"assetType": "FUTURE"})
    assert futures.json()["total"] == 1
    assert futures.json()["items"][0]["instrumentId"] == "CN.SHFE.FUTURE.AU0"

    sh = client.get("/api/market/instruments", params={"categoryKey": "a-sh"})
    assert sh.json()["total"] == 1
    night = client.get("/api/market/instruments", params={"categoryKey": "cn-future-night"})
    assert night.json()["total"] == 1
    assert night.json()["items"][0]["nightSession"] == "21:00-02:30"


def test_market_categories_expose_ordered_r3_filter_contract(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    response = client.get("/api/market/categories")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0] == {"id": "all", "label": "全部市场"}
    assert {item["id"] for item in items} >= {
        "exchange-index", "csi-index", "cni-index", "huazheng-index", "tdx-index", "a-sh", "a-sz", "a-bse",
        "a-chinext", "a-star", "a-etf", "a-convertible", "a-exchangeable", "a-pledged-repo",
        "a-lof", "a-reit",
        "hk-index", "hk-stock", "global-index", "future-comex", "future-nymex", "future-cbot",
        "cn-future-index", "cn-future-shfe", "cn-future-ine", "cn-future-dce", "cn-future-czce",
        "cn-future-cffex", "cn-future-gfex", "cn-future-night",
    }
    public_ids = {item["id"] for item in items}
    assert not {"other", "a-index", "tdx-board-index", "tdx-industry-index", "global-future", "cn-future-commodity", "a-repo", "a-other-repo"} & public_ids


def test_unclassified_endpoint_combines_silver_and_raw_review_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_root = tmp_path / "unclassified"
    unresolved = silver_row(
        "CN.TDX.FUTURE.CES013.CONTRACT",
        "2026-08-27",
        market="CN",
        asset_type="FUTURE",
        close=2801.5,
    )
    unresolved["symbol"] = "CES013"
    write_silver(
        data_root,
        [unresolved],
    )
    raw_item = {
        "reviewId": "raw:fixture:62#000300",
        "name": None,
        "code": "000300",
        "sourceCode": "62#000300",
        "marketPrefix": "62",
        "latestClose": 4492.25,
        "lastBarAt": "2026-08-27T00:00:00+08:00",
        "pricePeriod": "1d",
        "periods": ["1d", "5m"],
        "sourceTerminal": "通达信金融终端",
        "origin": "RAW_UNRECOGNIZED",
        "classificationStatus": "PENDING_REVIEW",
        "reason": "待确认",
    }
    monkeypatch.setattr("market_monitor.web_api.market.scan_unclassified_tdx", lambda *args, **kwargs: [raw_item])
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    normal = client.get("/api/market/instruments")
    assert normal.status_code == 200
    assert normal.json()["total"] == 0

    response = client.get("/api/market/unclassified", params={"pageSize": 10})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    origins = {item["origin"] for item in payload["items"]}
    assert origins == {"RAW_UNRECOGNIZED", "SILVER_UNCLASSIFIED"}
    silver = next(item for item in payload["items"] if item["origin"] == "SILVER_UNCLASSIFIED")
    assert silver["code"] == "CES013"
    assert silver["latestClose"] == 2801.5
    assert "assetType=FUTURE" in silver["reason"]


def test_tdx_index_names_resolve_from_bundled_name_map() -> None:
    item = {
        "symbol": "899050",
        "exchange": "BSE",
        "assetType": "INDEX",
        "actualSource": "通达信金融终端（本地）",
        "name": "CN指数 899050",
    }
    assert _normalize_tdx_instrument(item)["name"] == "北证50"

    untouched = dict(item, actualSource="其它来源")
    assert _normalize_tdx_instrument(untouched)["name"] == "CN指数 899050"

    commodity = {
        "symbol": "T003",
        "exchange": "TDX",
        "assetType": "INDEX",
        "seriesKind": "COMMODITY_INDEX",
        "actualSource": "通达信期货通",
        "name": "通达信商品指数 T003",
    }
    assert _normalize_tdx_instrument(commodity)["name"] == "工业品"

    bond = {
        "symbol": "110075",
        "exchange": "SSE",
        "assetType": "STOCK",
        "actualSource": "通达信金融终端（本地）",
        "name": "A股 110075",
    }
    repaired_bond = _normalize_tdx_instrument(bond)
    assert repaired_bond["assetType"] == "CONVERTIBLE_BOND"
    assert repaired_bond["name"] == "南航转债"

    sector = {
        "symbol": "881048",
        "exchange": "SSE",
        "assetType": "STOCK",
        "actualSource": "通达信金融终端（本地）",
        "name": "A股 881048",
    }
    repaired_sector = _normalize_tdx_instrument(sector)
    assert repaired_sector["assetType"] == "INDEX"
    assert repaired_sector["seriesKind"] == "TDX_INDUSTRY_INDEX"
    assert repaired_sector["name"] == "合成革"

    assert _normalize_tdx_instrument({
        "symbol": "000300", "exchange": "CSI", "assetType": "INDEX",
        "actualSource": "通达信金融终端（本地）", "name": "000300",
    })["name"] == "沪深300"
    assert _normalize_tdx_instrument({
        "symbol": "CN6002", "exchange": "CNI", "assetType": "INDEX",
        "actualSource": "通达信金融终端（本地）", "name": "CN6002",
    })["name"] == "北京指数"
    assert _normalize_tdx_instrument({
        "symbol": "T99001", "exchange": "HUAZHENG", "assetType": "INDEX",
        "actualSource": "通达信金融终端（本地）", "name": "T99001",
    })["name"] == "华证A指大盘全收"
    assert _normalize_tdx_instrument({
        "symbol": "EHR00W", "exchange": "COMEX", "assetType": "FUTURE",
        "actualSource": "通达信金融终端（本地）", "name": "EHR00W",
    })["name"] == "COMEX钢卷主连"


def test_future_contract_names_use_the_product_name_map() -> None:
    contract = {
        "symbol": "ZN2607",
        "sourceSymbol": "ZN2607",
        "productCode": "ZN",
        "exchange": "SHFE",
        "assetType": "FUTURE",
        "seriesKind": "CONTRACT",
        "name": "ZN2607",
    }
    assert _normalize_future_name(contract)["name"] == "沪锌2607"
    assert _normalize_future_name({
        "symbol": "PL2609", "sourceSymbol": "PL2609", "productCode": "PL",
        "exchange": "CZCE", "assetType": "FUTURE", "seriesKind": "CONTRACT", "name": "PL2609",
    })["name"] == "丙烯2609"
    assert _normalize_future_name({
        "symbol": "OP2701", "sourceSymbol": "OP2701", "productCode": "OP",
        "exchange": "SHFE", "assetType": "FUTURE", "seriesKind": "CONTRACT", "name": "OP2701",
    })["name"] == "胶版印刷纸2701"


def test_future_search_and_continuous_names_are_unambiguous(tmp_path: Path) -> None:
    data_root = tmp_path / "future-search"
    rows = []
    for suffix, kind in (("L7", "SECONDARY"), ("L8", "MAIN"), ("L9", "WEIGHTED")):
        physical_id = f"CN.GFEX.FUTURE.SI.{kind}.TDX"
        row = silver_row(physical_id, "2026-08-14", asset_type="FUTURE", period="5m")
        row.update({
            "canonical_instrument_id": f"CN.GFEX.FUTURE.SI.{kind}",
            "symbol": f"SI{suffix}",
            "source_symbol": f"SI{suffix}",
            "name": "工业硅指数",
            "series_kind": kind,
            "product_code": "SI",
            "exchange": "GFEX",
            "source": "通达信期货通",
            "actual_source": "通达信期货通",
        })
        rows.append(row)
    write_silver(data_root, rows)
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    response = client.get(
        "/api/market/instruments",
        params={"categoryKey": "cn-future-commodity", "q": "工业硅", "pageSize": 30},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert {item["name"] for item in response.json()["items"]} == {
        "工业硅次连", "工业硅主连", "工业硅加权",
    }
    assert {item["actualSource"] for item in response.json()["items"]} == {"通达信期货通"}


def test_month_contract_list_hides_expired_but_keeps_direct_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "contract-expiry"
    rows = []
    for symbol, day in (("JM2505", "2025-05-15"), ("JM2608", "2026-08-14"), ("AP705", "2026-08-14")):
        exchange = "CZCE" if symbol.startswith("AP") else "DCE"
        physical_id = f"CN.{exchange}.FUTURE.{symbol}.CONTRACT.TDX"
        row = silver_row(physical_id, day, asset_type="FUTURE", period="1d")
        row.update(
            {
                "canonical_instrument_id": f"CN.{exchange}.FUTURE.{symbol}.CONTRACT",
                "symbol": symbol,
                "name": symbol,
                "series_kind": "CONTRACT",
                "product_code": "AP" if symbol.startswith("AP") else "JM",
                "exchange": exchange,
            }
        )
        rows.append(row)
    write_silver(data_root, rows)
    monkeypatch.setattr("market_monitor.web_api.market._beijing_today", lambda: date(2026, 8, 22))
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    visible = client.get("/api/market/instruments", params={"seriesKind": "CONTRACT", "pageSize": 30})
    all_contracts = client.get(
        "/api/market/instruments",
        params={"seriesKind": "CONTRACT", "pageSize": 30, "includeExpired": True},
    )

    assert {item["symbol"] for item in visible.json()["items"]} == {"JM2608", "AP705"}
    assert {item["symbol"] for item in all_contracts.json()["items"]} == {"JM2505", "JM2608", "AP705"}
    direct = client.get("/api/market/instruments/CN.DCE.FUTURE.JM2505.CONTRACT/bars", params={"period": "1d"})
    assert direct.status_code == 200
    assert direct.json()["bars"][0]["close"] is not None


def test_period_routes_to_the_physical_source_that_has_local_data(tmp_path: Path) -> None:
    data_root = tmp_path / "period-routing"
    canonical = "CN.SSE.STOCK.600519"
    daily = silver_row(f"{canonical}.DAILY", "2026-08-13", period="1d", close=1500)
    daily.update({"canonical_instrument_id": canonical, "source": "daily-local"})
    minute = silver_row(f"{canonical}.MINUTE", "2026-08-14", period="5m", close=1510)
    minute.update({"canonical_instrument_id": canonical, "source": "minute-local"})
    write_silver(data_root, [daily, minute])
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    daily_response = client.get(f"/api/market/instruments/{canonical}/bars", params={"period": "1d"})
    minute_response = client.get(f"/api/market/instruments/{canonical}/bars", params={"period": "5m"})

    assert daily_response.status_code == 200
    assert daily_response.json()["actualSource"] == "daily-local"
    assert daily_response.json()["bars"][0]["close"] == 1500
    assert minute_response.status_code == 200
    assert minute_response.json()["actualSource"] == "minute-local"
    assert minute_response.json()["bars"][0]["close"] == 1510


def test_market_bars_are_ascending_bounded_and_camel_cased(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    response = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/bars",
        params={"period": "1d", "limit": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["instrumentId"] == "CN.SSE.STOCK.600519"
    assert body["total"] == 1
    assert body["historyTotal"] == 2
    assert body["start"] == 1
    assert body["size"] == 1
    assert body["bars"][0]["barOpenTime"] == "2026-08-07T09:30:00"
    assert body["bars"][0]["tradingDate"] == "2026-08-07"
    assert body["bars"][0]["qualityStatus"] == "OK"
    assert body["bars"][0]["instrumentId"] == "CN.SSE.STOCK.600519"
    assert body["lastBarAt"] == "2026-08-07T09:30:00"

    all_bars = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/bars",
        params={"period": "1d"},
    ).json()["bars"]
    times = [bar["barOpenTime"] for bar in all_bars]
    assert times == sorted(times)
    assert len(times) == 2


def test_chart_cursor_returns_latest_then_seeks_earlier_without_offset(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)

    latest = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/chart",
        params={"period": "1d", "size": 1},
    ).json()
    earlier = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/chart",
        params={"period": "1d", "size": 1, "before": latest["before"]},
    ).json()

    assert latest["bars"][0]["tradingDate"] == "2026-08-07"
    assert latest["hasMore"] is True
    assert latest["before"] == latest["bars"][0]["barOpenTime"]
    assert earlier["bars"][0]["tradingDate"] == "2026-08-06"
    assert earlier["hasMore"] is False


def test_market_bars_reject_unknown_instrument_and_period(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    assert (
        client.get("/api/market/instruments/NOPE/bars", params={"period": "1d"}).status_code
        == 404
    )
    bad_period = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/bars",
        params={"period": "5m"},
    )
    assert bad_period.status_code == 400
    assert client.get("/api/market/instruments", params={"pageSize": 501}).status_code == 422


def test_market_bars_derives_session_aware_hourly_and_weekly_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "derived"
    rows = []
    for index, opened in enumerate(("09:30", "10:00", "10:30", "11:00")):
        row = silver_row("CN.SSE.STOCK.600519", "2026-08-03", period="30m", close=11.0 + index)
        row["bar_open_time"] = f"2026-08-03T{opened}:00+08:00"
        row["bar_close_time"] = f"2026-08-03T{(10 + index // 2):02d}:{('00' if index % 2 == 0 else '30')}:00+08:00"
        rows.append(row)
    for day in ("2026-08-03", "2026-08-04", "2026-08-05"):
        rows.append(silver_row("CN.SSE.STOCK.600519", day, period="1d", close=20.0))
    write_silver(data_root, rows)
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    monkeypatch.setattr(
        "market_monitor.web_api.market._all_raw_bars",
        lambda *_args, **_kwargs: pytest.fail("游标接口不应扫描并聚合完整历史"),
    )

    hourly = client.get("/api/market/instruments/CN.SSE.STOCK.600519/bars", params={"period": "1h"})
    assert hourly.status_code == 200
    assert hourly.json()["availablePeriods"] == [
        "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "1q", "3mo", "6mo", "1y"
    ]
    assert hourly.json()["total"] == 2
    assert hourly.json()["bars"][0]["sourcePeriod"] == "30m"
    assert hourly.json()["bars"][0]["period"] == "1h"

    weekly = client.get("/api/market/instruments/CN.SSE.STOCK.600519/bars", params={"period": "1w"})
    assert weekly.status_code == 200
    assert weekly.json()["total"] == 1
    assert weekly.json()["bars"][0]["aggregatedFrom"] == "1d"

    quarterly = client.get("/api/market/instruments/CN.SSE.STOCK.600519/bars", params={"period": "1q"})
    assert quarterly.status_code == 200
    assert quarterly.json()["bars"][0]["period"] == "1q"

    seasonal = client.get("/api/market/instruments/CN.SSE.STOCK.600519/bars", params={"period": "3mo"})
    assert seasonal.status_code == 200
    assert seasonal.json()["bars"][0]["period"] == "3mo"
    assert seasonal.json()["bars"][0]["aggregatedFrom"] == "1d"


def test_derived_period_history_uses_stable_cursor_pages(tmp_path: Path) -> None:
    data_root = tmp_path / "derived-cursor"
    days = (
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
        "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
    )
    write_silver(
        data_root,
        [silver_row("CN.SSE.STOCK.600519", day, period="1d", close=20.0 + index) for index, day in enumerate(days)],
    )
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    latest = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/chart",
        params={"period": "1w", "size": 1},
    ).json()
    earlier = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/chart",
        params={"period": "1w", "size": 1, "before": latest["before"]},
    ).json()

    assert latest["bars"][0]["tradingDate"] == "2026-08-10"
    assert latest["hasMore"] is True
    assert earlier["bars"][0]["tradingDate"] == "2026-08-03"
    assert earlier["hasMore"] is False


def test_market_chart_bootstrap_and_visible_card_batch_share_local_cache(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)

    chart = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600519/chart",
        params={"period": "1d", "size": 60, "indicators": "volume,ma"},
    )
    assert chart.status_code == 200
    payload = chart.json()
    assert payload["total"] == 2
    assert len(payload["bars"]) == 2
    assert set(payload["series"]) == {"volume", "ma"}
    assert payload["dataVersion"]

    batch = client.get(
        "/api/market/instruments/bars/batch",
        params={"instrumentIds": "CN.SSE.STOCK.600519,HK.HKEX.STOCK.00700", "period": "1d", "limit": 60},
    )
    assert batch.status_code == 200
    assert len(batch.json()["items"]["CN.SSE.STOCK.600519"]) == 2
    assert len(batch.json()["items"]["HK.HKEX.STOCK.00700"]) == 1

    status = client.get("/api/market/cache-status")
    assert status.status_code == 200
    assert status.json()["ready"] is True
    assert status.json()["rows"] == 4


def test_chart_bootstrap_returns_complete_drawing_document(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    items = [
        {"id": "day", "type": "horizontal", "period": "1d", "crossPeriod": False, "points": [{"time": "2026-08-07T09:30:00", "price": 1500}]},
        {"id": "week", "type": "text", "period": "1w", "crossPeriod": False, "text": "周线", "points": [{"time": "2026-08-07T09:30:00", "price": 1500}]},
    ]
    saved = client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": items})
    assert saved.status_code == 200
    chart = client.get("/api/market/instruments/CN.SSE.STOCK.600519/chart", params={"period": "1d"})
    assert [item["id"] for item in chart.json()["drawings"]] == ["day", "week"]


def test_brush_drawing_validates_points_without_migrating_legacy_items(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    legacy = {"id": "line", "type": "horizontal", "points": [{"time": "2026-08-07T09:30:00", "price": 1500}]}
    brush = {"id": "brush", "type": "brush", "points": [{"time": "2026-08-07T09:30:00", "price": 1500}, {"time": "2026-08-07T09:35:00", "price": 1501}]}
    response = client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": [legacy, brush]})
    assert response.status_code == 200
    assert response.json()["items"] == [legacy, brush]
    assert client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": [{**brush, "points": brush["points"][:1]}]}).status_code == 422
    assert client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": [{**brush, "points": [{"time": "", "price": 1}, brush["points"][1]]}]}).status_code == 422
    assert client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": [{**brush, "points": [{"time": "2026-08-07", "price": "NaN"}, brush["points"][1]]}]}).status_code == 422
    too_many = [{"time": "2026-08-07T09:30:00", "price": 1500} for _ in range(2_049)]
    assert client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": [{**brush, "points": too_many}]}).status_code == 422


def test_drawings_index_and_batch_delete(tmp_path: Path) -> None:
    _application, client = _app(tmp_path)
    first = {"id": "one", "type": "horizontal", "period": "1d", "crossPeriod": False, "points": [{"time": "2026-08-07T09:30:00", "price": 1500}]}
    second = {"id": "two", "type": "vertical", "period": "1d", "crossPeriod": False, "points": [{"time": "2026-08-08T09:30:00", "price": 1510}]}
    assert client.put("/api/market/instruments/CN.SSE.STOCK.600519/drawings", json={"items": [first]}).status_code == 200
    assert client.put("/api/market/instruments/HK.HKEX.STOCK.00700/drawings", json={"items": [second]}).status_code == 200

    batch = client.get(
        "/api/market/drawings/batch",
        params={"instrumentIds": "CN.SSE.STOCK.600519,HK.HKEX.STOCK.00700", "period": "1d"},
    ).json()
    assert batch["items"] == {
        "CN.SSE.STOCK.600519": [first],
        "HK.HKEX.STOCK.00700": [second],
    }

    index = client.get("/api/market/drawings/index").json()
    assert index["total"] == 2
    assert {item["instrumentId"]: item["count"] for item in index["items"]} == {
        "CN.SSE.STOCK.600519": 1,
        "HK.HKEX.STOCK.00700": 1,
    }

    deleted = client.request("DELETE", "/api/market/drawings", json={"instrumentIds": ["CN.SSE.STOCK.600519"]})
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": 1}
    remaining = client.get("/api/market/drawings/index").json()
    assert [item["instrumentId"] for item in remaining["items"]] == ["HK.HKEX.STOCK.00700"]


def test_silver_write_updates_ready_query_cache_incrementally(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    cache = get_kline_query_store(data_root)
    cache.rebuild()
    before = cache.status().revision
    store = MarketStore(data_root)
    try:
        store.write_silver_bars(
            PartitionKey("CN", "STOCK", "1d", 2026, "incremental-cache"),
            [silver_row("CN.SSE.STOCK.600519", "2026-08-08", close=1530.0)],
            "2026-08-08T15:00:00+08:00",
            "run-cache",
        )
    finally:
        store.close()

    bars = cache.read_bars("CN.SSE.STOCK.600519", "1d", 10)
    after = cache.status()
    assert bars[-1]["close"] == 1530.0
    assert after.ready is True
    assert after.revision != before


def test_watchlist_add_list_duplicate_delete(tmp_path: Path) -> None:
    application, loopback = _app(tmp_path)
    assert loopback.get("/api/personal/watchlist").json() == {"items": []}

    created = loopback.post(
        "/api/personal/watchlist",
        json={"instrumentId": "CN.SSE.STOCK.600519", "note": "core"},
    )
    assert created.status_code == 200
    item = created.json()["item"]
    assert item["instrumentId"] == "CN.SSE.STOCK.600519"
    assert item["note"] == "core"

    duplicate = loopback.post(
        "/api/personal/watchlist",
        json={"instrumentId": "CN.SSE.STOCK.600519"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["item"]["addedAt"] == item["addedAt"]

    assert loopback.get("/api/personal/watchlist").json()["items"][0]["instrumentId"] == "CN.SSE.STOCK.600519"

    deleted = loopback.delete("/api/personal/watchlist/CN.SSE.STOCK.600519")
    assert deleted.status_code == 200
    assert loopback.get("/api/personal/watchlist").json()["items"] == []
    assert loopback.delete("/api/personal/watchlist/CN.SSE.STOCK.600519").status_code == 404

    remote = TestClient(application)
    assert remote.get("/api/personal/watchlist").status_code == 200
    assert (
        remote.post("/api/personal/watchlist", json={"instrumentId": "CN.SSE.STOCK.600519"}).status_code
        == 403
    )


def test_watchlist_rejects_unknown_instrument_and_extra_fields(tmp_path: Path) -> None:
    _application, loopback = _app(tmp_path)
    assert (
        loopback.post("/api/personal/watchlist", json={"instrumentId": "UNKNOWN"}).status_code == 400
    )
    assert (
        loopback.post(
            "/api/personal/watchlist",
            json={"instrumentId": "CN.SSE.STOCK.600519", "sql": "delete"},
        ).status_code
        == 422
    )
