from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.storage import MarketStore
from market_monitor.web_app import create_web_app
from web_fixtures import silver_row, write_silver


def _metric(series_id: str, period: str, value: float) -> dict[str, object]:
    return {
        "metric_id": f"{series_id}:{series_id}:{period}:MONTHLY:value",
        "instrument_id": series_id,
        "trading_date": period,
        "period": "MONTHLY",
        "metric_name": series_id,
        "value": value,
        "definition": "fixture definition",
        "calculation_method": "fixture method",
        "timestamp": f"{period}-28T08:00:00+08:00",
    }


def _a_share_metric(metric_key: str, metric_name: str, value: float) -> dict[str, object]:
    return {
        "metric_id": f"A_SHARE_BREADTH:CN.A_SHARE.BREADTH:2026-08-28:1d:{metric_key}",
        "instrument_id": "CN.A_SHARE.BREADTH",
        "trading_date": "2026-08-28",
        "period": "1d",
        "metric_name": metric_name,
        "value": value,
        "definition": "fixture A-share definition",
        "calculation_method": "fixture A-share method",
        "timestamp": "2026-08-28T08:00:00+08:00",
    }


def _northbound_metric(metric_key: str, metric_name: str, value: float) -> dict[str, object]:
    return {
        "metric_id": f"HSGT_FLOW:CN.HSGT.北向:2026-08-28:1d:{metric_key}",
        "instrument_id": "CN.HSGT.北向",
        "trading_date": "2026-08-28",
        "period": "1d",
        "metric_name": metric_name,
        "value": value,
        "definition": "fixture northbound definition",
        "calculation_method": "source=fixture_hsgt",
        "timestamp": "2026-08-28T08:00:00+08:00",
    }


def test_macro_catalog_and_series_expose_only_local_gold_observations(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = MarketStore(data_root)
    try:
        store.upsert_gold_metrics([
            _metric("M2_MONEY_SUPPLY", "2026-01", 7.0),
            _metric("M2_MONEY_SUPPLY", "2026-02", 7.1),
            _metric("FED_FUNDS_RATE", "2026-02", 4.5),
            _a_share_metric("TOTAL_MARKET_CAP_YI", "沪深京总市值(亿)", 1_000_000),
            _a_share_metric("TOTAL_AMOUNT_YI", "当日成交额(亿)", 12_000),
            _a_share_metric("ADVANCES", "上涨家数", 3_000),
            _a_share_metric("UNCHANGED", "平盘家数", 100),
            _a_share_metric("DECLINES", "下跌家数", 2_000),
            _northbound_metric("北向_当日成交净买额", "北向当日成交净买额(亿)", 12.5),
            _northbound_metric("北向_历史累计净买额", "北向历史累计净买额(亿)", 20_000),
            _northbound_metric("北向_持股市值", "北向持股市值(亿)", 30_000),
        ])
    finally:
        store.close()

    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    sections = client.get("/api/data/sections").json()
    assert [item["id"] for item in sections["sections"]] == ["cn-equities", "hk-equities", "macro"]
    a_share_panels = {item["id"]: item for item in sections["sections"][0]["panels"]}
    assert a_share_panels["cn-market-cap"]["status"] == "PASS"
    assert a_share_panels["cn-market-cap"]["values"][0]["value"] == 1_000_000
    assert a_share_panels["cn-breadth"]["status"] == "PARTIAL"
    assert len(a_share_panels["cn-breadth"]["values"]) == 3
    assert a_share_panels["cn-breadth"]["limitations"]
    assert a_share_panels["cn-high-amplitude-low-return"]["status"] == "UNAVAILABLE"
    assert a_share_panels["cn-risk-lists"]["limitations"]
    assert a_share_panels["cn-hsgt-flow"]["status"] == "PARTIAL"
    assert [item["metricKey"] for item in a_share_panels["cn-hsgt-flow"]["values"]] == [
        "北向_历史累计净买额", "北向_当日成交净买额", "北向_持股市值",
    ]
    assert sections["sections"][-1]["panels"][0]["availableSeries"] == 1

    catalog = client.get("/api/data/macro/catalog", params={"country": "CN"}).json()
    m2 = next(item for item in catalog["items"] if item["seriesId"] == "M2_MONEY_SUPPLY")
    assert m2["available"] is True
    assert m2["latestObservationPeriod"] == "2026-02"
    assert m2["latestFetchedAt"] == "2026-02-28T08:00:00+08:00"
    assert next(item for item in catalog["items"] if item["seriesId"] == "M0_MONEY_SUPPLY")["available"] is False
    assert next(item for item in catalog["items"] if item["seriesId"] == "M2_MONEY_SUPPLY")["timeBasis"] == "OBSERVATION_PERIOD"
    assert next(item for item in catalog["items"] if item["seriesId"] == "CN_IMPORT_USD_YOY")["timeBasis"] == "SOURCE_DATE"

    timeline = client.get("/api/data/macro/series", params={"seriesId": "M2_MONEY_SUPPLY"}).json()
    assert timeline["available"] is True
    assert [point["observationPeriod"] for point in timeline["observations"]] == ["2026-01", "2026-02"]
    assert timeline["observations"][0]["releasedAt"] is None
    assert timeline["observations"][0]["fetchedAt"] == "2026-01-28T08:00:00+08:00"
    seasonal = client.get(
        "/api/data/macro/series", params={"seriesId": "M2_MONEY_SUPPLY", "view": "seasonal"}
    ).json()
    assert seasonal["observations"] == [{"year": "2026", "months": [7.0, 7.1, None, None, None, None, None, None, None, None, None, None]}]

    unavailable = client.get("/api/data/macro/series", params={"seriesId": "M0_MONEY_SUPPLY"}).json()
    assert unavailable["available"] is False
    assert unavailable["observations"] == []
    assert client.get("/api/data/macro/catalog", params={"country": "bad"}).status_code == 422
    assert client.get("/api/data/macro/series", params={"seriesId": "bad"}).status_code == 422
    assert client.get("/api/data/macro/series", params={"seriesId": "M2_MONEY_SUPPLY", "startPeriod": "bad"}).status_code == 422

    overview = client.get("/api/data/equities/cn/overview").json()
    assert overview["available"] is True
    assert overview["market"] == "CN"
    assert overview["points"] == [{
        "tradingDay": "2026-08-28",
        "totalMarketCapYi": 1_000_000.0,
        "floatMarketCapYi": None,
        "turnoverYi": 12_000.0,
        "advances": 3_000.0,
        "unchanged": 100.0,
        "declines": 2_000.0,
        "limitUpCount": None,
        "limitDownCount": None,
        "updatedAt": "2026-08-28T08:00:00+08:00",
    }]
    assert client.get("/api/data/equities/cn/overview", params={"segment": "GEM"}).json()["available"] is False
    hk = client.get("/api/data/equities/hk/overview").json()
    assert hk["available"] is False and hk["currency"] == "HKD"

    status_list = client.get(
        "/api/data/equities/cn/lists",
        params={"type": "st_warning", "asOfDay": "2026-08-28", "page": 2, "pageSize": 20},
    ).json()
    assert status_list == {
        "available": False,
        "market": "CN",
        "segment": "ALL",
        "listType": "st_warning",
        "listTitle": "ST 风险警示",
        "asOfDay": "2026-08-28",
        "page": 2,
        "pageSize": 20,
        "total": None,
        "items": [],
        "limitations": [
            "A 股 ST 风险警示尚无带生效日期、来源公告和抓取时间的本地权威观测；不会以当前名称、历史最后状态或空表代替事实。",
        ],
    }
    assert client.get("/api/data/equities/cn/lists", params={"type": "unknown"}).status_code == 422
    assert client.get("/api/data/equities/cn/lists", params={"type": "st_warning", "asOfDay": "bad"}).status_code == 422
    assert client.get("/api/data/equities/cn/lists", params={"type": "st_warning", "pageSize": 201}).status_code == 422
    assert client.get("/api/data/equities/us/overview").status_code == 422
    assert client.get("/api/data/equities/cn/overview", params={"startDay": "bad"}).status_code == 422


def test_hk_overview_aggregates_only_verified_local_tdx_daily_bars(tmp_path: Path) -> None:
    data_root = tmp_path / "hk-local"

    def bar(code: str, day: str, close: float, amount: float) -> dict[str, object]:
        physical = f"HK.HKEX.STOCK.{code}.TDX_LOCAL"
        row = silver_row(
            physical,
            day,
            market="HK",
            asset_type="STOCK",
            close=close,
            amount=amount,
        )
        row["canonical_instrument_id"] = f"HK.HKEX.STOCK.{code}"
        row["source"] = "通达信金融终端（本地）"
        row["actual_source"] = "通达信金融终端（本地）"
        row["normalization_status"] = "PASS"
        row["normalization_version"] = "tdx-cn-v2"
        return row

    write_silver(
        data_root,
        [
            bar("00001", "2026-08-26", 10.0, 100_000_000),
            bar("00002", "2026-08-26", 5.0, 100_000_000),
            bar("00003", "2026-08-26", 4.0, 100_000_000),
            bar("00001", "2026-08-27", 11.0, 200_000_000),
            bar("00002", "2026-08-27", 5.0, 100_000_000),
            bar("00003", "2026-08-27", 3.0, 50_000_000),
        ],
    )
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    overview = client.get(
        "/api/data/equities/hk/overview",
        params={"startDay": "2026-08-27"},
    ).json()

    assert overview["available"] is True
    assert overview["source"] == "通达信金融终端（本地）港股日线"
    assert overview["points"] == [{
        "tradingDay": "2026-08-27",
        "totalMarketCapYi": None,
        "floatMarketCapYi": None,
        "turnoverYi": 3.5,
        "advances": 1.0,
        "unchanged": 1.0,
        "declines": 1.0,
        "limitUpCount": None,
        "limitDownCount": None,
        "coverage": 3,
        "turnoverCoverage": 3,
        "breadthCoverage": 3,
        "updatedAt": "2026-08-27T15:00:00+08:00",
    }]
    panels = {item["id"]: item for item in client.get("/api/data/sections").json()["sections"][1]["panels"]}
    assert panels["hk-market-cap"]["status"] == "UNAVAILABLE"
    assert panels["hk-turnover"]["status"] == "PARTIAL"
    assert panels["hk-turnover"]["values"][0]["coverage"] == 3
    assert panels["hk-breadth"]["status"] == "PARTIAL"
