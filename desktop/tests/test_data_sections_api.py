from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import duckdb
import pytest

from market_monitor import collector
from market_monitor.a_share_calendar import is_known_a_share_closed_day, is_verified_a_share_trading_day
from market_monitor.storage import MarketStore
from market_monitor import storage
from market_monitor.web_api import data_sections as data_sections_api
from market_monitor.web_api.data_sections import _exchange_default_end
from market_monitor.web_app import create_web_app
from web_fixtures import silver_row, write_silver


def test_kpl_status_only_exposes_state_and_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from market_monitor import kpl_market_probe

    root = tmp_path / "data"
    calls: list[Path] = []

    def read_status(path: Path) -> dict[str, str]:
        calls.append(path)
        return {"status": "UNVERIFIED", "checkedAt": "2026-10-28T01:00:00+00:00",
                "observedDate": "2026-10-28", "private": "must-not-leak"}

    monkeypatch.setattr(kpl_market_probe, "read_kpl_status", read_status)
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    response = client.get("/api/data/kpl-market/status")
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "UNVERIFIED"
    assert value["checkedAt"] == "2026-10-28T01:00:00+00:00"
    assert value["observedDate"] == "2026-10-28"
    assert set(value) == {"status", "checkedAt", "observedDate", "calendarStatus", "checkAge"}
    assert calls == [root]


def test_kpl_status_calendar_and_age_do_not_change_source_state() -> None:
    project = data_sections_api._kpl_status_projection
    zone = ZoneInfo("Asia/Shanghai")
    status = {"status": "STALE", "checkedAt": "2026-10-28T01:00:00+00:00", "observedDate": "2026-10-27"}
    recent = project(status, datetime(2026, 10, 29, tzinfo=zone))
    assert recent["calendarStatus"] == "VERIFIED" and recent["checkAge"] == "RECENT"
    old = project(status, datetime(2027, 1, 1, tzinfo=zone))
    assert old["calendarStatus"] == "UNKNOWN" and old["checkAge"] == "OLD"
    assert old["status"] == "STALE" and old["observedDate"] == "2026-10-27"
    assert project(status, datetime(2026, 10, 28, 9, 20, tzinfo=zone))["checkAge"] == "RECENT"
    assert project(status, datetime(2026, 10, 28, 10, 0, tzinfo=zone))["checkAge"] == "OLD"
    assert project({"status": "EMPTY", "checkedAt": None}, datetime(2026, 10, 29, tzinfo=zone))["checkAge"] == "UNKNOWN"
    assert project({"checkedAt": "invalid"}, datetime(2026, 10, 29, tzinfo=zone))["checkAge"] == "UNKNOWN"


def test_exchange_default_end_waits_for_close_publication_window() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    assert _exchange_default_end(datetime(2026, 10, 26, 0, 48, tzinfo=zone)) == date(2026, 10, 25)
    assert _exchange_default_end(datetime(2026, 10, 26, 18, 59, tzinfo=zone)) == date(2026, 10, 25)
    assert _exchange_default_end(datetime(2026, 10, 26, 19, 0, tzinfo=zone)) == date(2026, 10, 26)


def test_exchange_explicit_open_day_is_not_a_missing_source(tmp_path: Path, monkeypatch) -> None:
    class BeforeClose(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 10, 26, 0, 48, tzinfo=ZoneInfo("Asia/Shanghai"))
            return current.astimezone(tz) if tz else current

    monkeypatch.setattr(data_sections_api, "datetime", BeforeClose)
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    overview = client.get("/api/data/exchanges/overview", params={
        "startDay": "2026-10-26", "endDay": "2026-10-26",
    }).json()
    stock = next(item for item in overview["coverage"] if item["asset"] == "STOCK_A")
    assert stock["status"] == "NOT_DUE"
    assert stock["missingVenues"] == []
    assert stock["failedVenues"] == []


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


def test_catalog_lock_reports_retryable_busy_without_false_macro_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "busy-catalog"
    store = MarketStore(root)
    store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    attempts = 0

    def locked(_path: str):
        nonlocal attempts
        attempts += 1
        raise duckdb.IOException("IO Error: Cannot open file: 另一个程序正在使用此文件")

    monkeypatch.setattr(storage.duckdb, "connect", locked)
    started = time.monotonic()
    response = client.get("/api/data/macro/catalog")
    elapsed = time.monotonic() - started
    assert attempts == 5
    assert elapsed < 3
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.headers["x-marketlistener-error-code"] == "CATALOG_BUSY"
    assert response.json() == {"detail": {
        "code": "CATALOG_BUSY", "message": "本机数据正在入库，目录暂被占用；请稍后重试。",
    }}

    # Other data-page readers use the same bounded gate. None may turn a
    # temporary lock into invented zero coverage or an empty history.
    monkeypatch.setattr(storage, "_CATALOG_CONNECT_DELAYS", (0, 0, 0, 0))
    for path in (
        "/api/data/sections", "/api/data/macro/series?seriesId=M2_MONEY_SUPPLY",
        "/api/data/equities/cn/overview", "/api/data/exchanges/overview",
        "/api/data/ths-cpi",
    ):
        blocked = client.get(path)
        assert blocked.status_code == 503
        assert blocked.headers["x-marketlistener-error-code"] == "CATALOG_BUSY"
        assert blocked.json()["detail"]["code"] == "CATALOG_BUSY"
        assert blocked.json()["detail"]["message"] == "本机数据正在入库，目录暂被占用；请稍后重试。"


def test_non_lock_catalog_error_is_not_mislabeled_as_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "invalid-catalog"
    root.mkdir()
    (root / "catalog.duckdb").touch()

    def broken(_root: Path):
        raise duckdb.IOException("database file is corrupted")

    monkeypatch.setattr(data_sections_api, "connect_catalog", broken)
    with pytest.raises(duckdb.IOException, match="corrupted"):
        data_sections_api._macro_availability(root)


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
        "timestamp": "2026-08-28T16:00:00+08:00",
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


def _limit_up_open_return(
    day: str, value: float, method: str, *, series_id: str = "CN_ZT_POOL",
) -> dict[str, object]:
    return {
        "metric_id": f"{series_id}:CN.A_SHARE.YESTERDAY_LIMIT_UP:{day}:1d:YESTERDAY_LIMIT_UP_OPEN_RETURN",
        "instrument_id": "CN.A_SHARE.YESTERDAY_LIMIT_UP",
        "trading_date": day,
        "period": "1d",
        "metric_name": "昨日涨停今日接盘收益率(%)",
        "value": value,
        "definition": "昨日涨停个股今日开盘价买入的当日平均收益率",
        "calculation_method": method,
        "timestamp": f"{day}T16:00:00+08:00",
    }


def _limit_height(day: str, side: str, value: float) -> dict[str, object]:
    normalized_day = day.replace("-", "")
    iso_day = f"{normalized_day[:4]}-{normalized_day[4:6]}-{normalized_day[6:8]}"
    return {
        "metric_id": f"CN_ZT_POOL:CN.A_SHARE.{side}:{normalized_day}:1d:{side}_MAX_LIANBAN",
        "instrument_id": f"CN.A_SHARE.{side}",
        "trading_date": day,
        "period": "1d",
        "metric_name": f"{side}最高连板高度",
        "value": value,
        "definition": "每日已核验涨跌停池的最高连续板数",
        "calculation_method": "source=fixture_eastmoney_pool",
        "timestamp": f"{iso_day}T16:00:00+08:00",
    }


def test_preclose_snapshot_and_legacy_empty_pool_are_not_final_values(tmp_path: Path) -> None:
    root = tmp_path / "legacy-early"
    store = MarketStore(root)
    try:
        store.upsert_gold_metrics([
            {**_a_share_metric("TOTAL_MARKET_CAP_YI", "总市值", 1_000_000),
             "metric_id": "A_SHARE_BREADTH:CN.A_SHARE.BREADTH:2026-10-13:1d:TOTAL_MARKET_CAP_YI",
             "trading_date": "2026-10-13", "timestamp": "2026-10-13T08:49:00+08:00"},
            collector._metric("CN_ZT_POOL", "CN.A_SHARE.涨停", "20261013", "1d",
                              "涨停家数", 0, "历史空池", "source=stock_zt_pool_em",
                              metric_key="涨停_COUNT"),
            collector._metric("CN_ZT_POOL", "CN.A_SHARE.跌停", "20261013", "1d",
                              "跌停家数", 0, "历史空池", "source=stock_zt_pool_dtgc_em",
                              metric_key="跌停_COUNT"),
            _limit_height("20261013", "涨停", 10),
        ])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    points = client.get("/api/data/equities/cn/overview").json()["points"]
    assert len(points) == 1
    assert points[0]["tradingDay"] == "2026-10-13"
    assert points[0]["totalMarketCapYi"] is None
    assert points[0]["limitUpCount"] is None
    assert points[0]["limitDownCount"] is None
    assert points[0]["limitUpHeight"] == 10
    panels = {item["id"]: item for item in client.get("/api/data/sections").json()["sections"][0]["panels"]}
    assert panels["cn-limit-pool"]["status"] == "UNAVAILABLE"


def test_historical_a_share_market_cap_replaces_the_whole_legacy_spot_series(tmp_path: Path) -> None:
    root = tmp_path / "historical-cap"
    method = ("source=eastmoney-RPT_VALUEMARKET+szse-official-daily; "
              "formula=em_main+em_star+em_bse+szse_main_a+szse_chinext_a; unit=亿元; "
              f"em_sha256={'a' * 64}; szse_sha256={'b' * 64}")
    history = [collector._metric(
        "A_SHARE_MARKET_CAP_HISTORY", "CN.A_SHARE.MARKET_CAP", day, "1d",
        "沪深京A股总市值(亿)", cap, "同日A股市值", method,
        metric_key="TOTAL_MARKET_CAP_YI", timestamp="2026-10-24T10:00:00+08:00",
    ) for day, cap in (("2026-10-20", 1_053_942.81), ("2026-10-21", 1_048_661.10))]
    old = [collector._metric(
        "A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", day, "1d",
        "旧腾讯总市值(亿)", 1_197_103.78, "旧快照", "source=stock_zh_a_spot_tx",
        metric_key="TOTAL_MARKET_CAP_YI", timestamp=f"{day}T16:00:00+08:00",
    ) for day in ("2026-10-21", "2026-10-22")]
    old.append(collector._metric(
        "A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", "2026-10-22", "1d",
        "当日成交额(亿)", 12_000, "可信当日成交额", "source=stock_zh_a_spot_tx",
        metric_key="TOTAL_AMOUNT_YI", timestamp="2026-10-22T16:00:00+08:00",
    ))
    old.append(collector._metric(
        "A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", "2026-10-20", "1d",
        "当日成交额(亿)", 14_715.3, "本机覆盖成交额",
        "source=tdx-cn-v2; historical_silver_recovery; coverage=5568; paired=5566; amount_unit=CNY",
        metric_key="TOTAL_AMOUNT_YI", timestamp="2026-10-24T10:00:00+08:00",
    ))
    malformed = collector._metric(
        "A_SHARE_MARKET_CAP_HISTORY", "CN.A_SHARE.MARKET_CAP", "2026-10-22", "1d",
        "未核验市值", 1_100_000, "缺原件摘要", "source=eastmoney-RPT_VALUEMARKET+szse-official-daily;",
        metric_key="TOTAL_MARKET_CAP_YI", timestamp="2026-10-24T10:00:00+08:00",
    )
    store = MarketStore(root)
    try:
        store.upsert_gold_metrics([*history, *old, malformed])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    points = {item["tradingDay"]: item for item in client.get("/api/data/equities/cn/overview").json()["points"]}
    assert points["2026-10-20"]["totalMarketCapYi"] == pytest.approx(1_053_942.81)
    assert points["2026-10-20"]["sources"]["turnoverYi"] == {
        "label": "通达信本机质检日线", "coverage": 5568, "pairedCoverage": 5566,
    }
    assert points["2026-10-21"]["totalMarketCapYi"] == pytest.approx(1_048_661.10)
    assert points["2026-10-22"]["totalMarketCapYi"] is None
    assert points["2026-10-22"]["sources"]["turnoverYi"]["label"] == "腾讯行情收盘快照"
    panels = {item["id"]: item for item in client.get("/api/data/sections").json()["sections"][0]["panels"]}
    cap_panel = panels["cn-market-cap"]
    assert cap_panel["values"][0]["tradingDate"] == "2026-10-21"
    assert cap_panel["values"][0]["source"] == "东方财富估值分析＋深交所市场总貌"


def _official_metric(venue: str, category: str, day: str, key: str, field: str,
                     value: float, *, trusted: bool = True) -> dict[str, object]:
    method = (f"source=official-exchanges-v1; venue={venue}; category={category}; field={field}; "
              f"raw_sha256=bse_stock:{'b' * 64},sse_market:{'a' * 64},"
              f"szse_categories:{'c' * 64}") if trusted else "source=unverified-fixture"
    return collector._metric(
        "OFFICIAL_EXCHANGE_DAILY", f"CN.{venue}.MARKET.{category}", day, "1d",
        f"{category} {field}", value, "同日官网核验", method, metric_key=key,
        timestamp=f"{day}T19:00:00+08:00",
    )


def test_official_exchange_series_replace_legacy_whole_range_and_expose_gaps(tmp_path: Path) -> None:
    root = tmp_path / "official"
    rows = []
    for venue, cap, float_cap, turnover in (
        ("SSE", 650_882.32, 580_000, 6_440.10),
        ("SZSE", 394_916.32, 340_000, 7_872.76),
        ("BSE", 8_144.17, 4_500, 128.96),
        ("ALL", 1_053_942.81, 924_500, 14_441.82),
    ):
        for key, field, value in (("TOTAL_MARKET_CAP_YI", "capYi", cap),
                                  ("FLOAT_MARKET_CAP_YI", "floatCapYi", float_cap),
                                  ("TOTAL_AMOUNT_YI", "turnoverYi", turnover)):
            rows.append(_official_metric(venue, "STOCK_A", "2026-10-20", key, field, value))
    for venue in ("SSE", "SZSE"):
        for key, field, value in (("TOTAL_MARKET_CAP_YI", "capYi", 500_000),
                                  ("FLOAT_MARKET_CAP_YI", "floatCapYi", 450_000),
                                  ("TOTAL_AMOUNT_YI", "turnoverYi", 6_000)):
            rows.append(_official_metric(venue, "STOCK_A", "2026-10-21", key, field, value))
    for venue in ("SSE", "SZSE"):
        for key, field, value in (("SECURITY_COUNT", "securityCount", 500),
                                  ("TOTAL_MARKET_CAP_YI", "capYi", 10_000),
                                  ("FLOAT_MARKET_CAP_YI", "floatCapYi", 9_000),
                                  ("TOTAL_AMOUNT_YI", "turnoverYi", 1_000),
                                  ("VOLUME_YI", "volumeYi", 200)):
            if venue == "SSE" and key == "VOLUME_YI":
                continue
            rows.append(_official_metric(venue, "FUND", "2026-10-20", key, field, value))
    rows.append(_official_metric("ALL", "STOCK_A", "2026-10-21", "TOTAL_MARKET_CAP_YI",
                                 "capYi", 999_999, trusted=False))
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        rows.extend([
            collector._metric("A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", day, "1d",
                              "旧快照市值", 1_197_000, "旧来源", "source=stock_zh_a_spot_tx",
                              metric_key="TOTAL_MARKET_CAP_YI", timestamp=f"{day}T16:00:00+08:00"),
            collector._metric("A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", day, "1d",
                              "旧快照成交额", 12_000, "旧来源", "source=stock_zh_a_spot_tx",
                              metric_key="TOTAL_AMOUNT_YI", timestamp=f"{day}T16:00:00+08:00"),
            collector._metric("A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", day, "1d",
                              "上涨家数", 3000, "旧来源", "source=stock_zh_a_spot_tx",
                              metric_key="ADVANCES", timestamp=f"{day}T16:00:00+08:00"),
        ])
    store = MarketStore(root)
    try:
        store.upsert_gold_metrics(rows)
    finally:
        store.close()
    state = root / "state"
    state.mkdir(parents=True)
    (state / "official_exchange_daily_backlog.json").write_text(json.dumps({
        "schema": "official-exchange-backlog-v1", "kind": "daily",
        "failures": {"2026-10-19": "sse_fund_daily:ValueError",
                     "2026-10-20": "sse_fund_daily:ValueError,bse_bond:Timeout",
                     "2026-10-21": "bse_stock:Timeout,sse_bond_custody_daily:ValueError"},
    }), encoding="utf-8")
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    payload = client.get("/api/data/equities/cn/overview", params={
        "startDay": "2026-10-19", "endDay": "2026-10-21",
    }).json()
    points = {point["tradingDay"]: point for point in payload["points"]}
    assert points["2026-10-19"]["totalMarketCapYi"] is None
    assert points["2026-10-19"]["turnoverYi"] is None
    assert points["2026-10-19"]["officialCoverage"]["failedVenues"] == []
    assert points["2026-10-20"]["totalMarketCapYi"] == pytest.approx(1_053_942.81)
    assert points["2026-10-20"]["officialCoverage"]["status"] == "COMPLETE"
    assert points["2026-10-20"]["officialCoverage"]["failedVenues"] == []
    assert points["2026-10-20"]["floatMarketCapYi"] == pytest.approx(924_500)
    assert points["2026-10-20"]["turnoverYi"] == pytest.approx(14_441.82)
    assert points["2026-10-20"]["sources"]["turnoverYi"]["label"] == "沪深京证券交易所官网日报"
    assert points["2026-10-21"]["totalMarketCapYi"] is None
    assert points["2026-10-21"]["turnoverYi"] is None
    assert points["2026-10-21"]["advances"] == 3000
    assert points["2026-10-21"]["officialCoverage"]["missingVenues"] == ["BSE"]
    assert points["2026-10-21"]["officialCoverage"]["failedVenues"] == ["BSE"]
    panels = {item["id"]: item for item in client.get("/api/data/sections").json()["sections"][0]["panels"]}
    assert panels["cn-market-cap"]["values"][0]["source"] == "沪深京证券交易所官网日报"
    assert panels["cn-float-market-cap"]["values"][0]["value"] == 924_500
    assert panels["cn-turnover"]["values"][0]["value"] == 14_441.82

    exchange = client.get("/api/data/exchanges/overview", params={
        "startDay": "2026-10-19", "endDay": "2026-10-21",
    })
    assert exchange.status_code == 200
    overview = exchange.json()
    assert overview["available"] is True
    total_cap = next(item for item in overview["series"] if
                     (item["venue"], item["category"], item["metric"]) ==
                     ("ALL", "STOCK_A", "TOTAL_MARKET_CAP_YI"))
    assert total_cap["points"] == [{"day": "2026-10-20", "value": 1_053_942.81}]
    assert total_cap["unit"] == "亿元"
    day_21 = next(item for item in overview["coverage"] if
                  item["day"] == "2026-10-21" and item["asset"] == "STOCK_A")
    assert day_21["status"] == "PARTIAL" and day_21["missingVenues"] == ["BSE"]
    assert day_21["failedVenues"] == ["BSE"]
    day_20 = next(item for item in overview["coverage"] if
                  item["day"] == "2026-10-20" and item["asset"] == "STOCK_A")
    assert day_20["status"] == "COMPLETE" and day_20["failedVenues"] == []
    fund_day_20 = next(item for item in overview["coverage"] if
                       item["day"] == "2026-10-20" and item["asset"] == "FUND")
    assert fund_day_20["status"] == "PARTIAL"
    assert fund_day_20["missingVenues"] == ["SSE"]
    assert fund_day_20["failedVenues"] == ["SSE"]
    bond_day_20 = next(item for item in overview["coverage"] if
                       item["day"] == "2026-10-20" and item["asset"] == "BOND_CASH")
    assert bond_day_20["failedVenues"] == ["BSE"]
    stock_day_19 = next(item for item in overview["coverage"] if
                        item["day"] == "2026-10-19" and item["asset"] == "STOCK_A")
    assert stock_day_19["status"] == "MISSING" and stock_day_19["failedVenues"] == []
    bond_day_21 = next(item for item in overview["coverage"] if
                       item["day"] == "2026-10-21" and item["asset"] == "BOND_CASH")
    assert bond_day_21["status"] == "MISSING" and bond_day_21["failedVenues"] == []
    assert client.get("/api/data/exchanges/overview", params={
        "startDay": "2025-01-01", "endDay": "2026-10-21",
    }).status_code == 422


def test_broken_board_and_official_hk_cap_keep_separate_dates(tmp_path: Path) -> None:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        store.upsert_gold_metrics([
            collector._metric("CN_ZT_POOL", "CN.A_SHARE.炸板", "20261020", "1d",
                              "炸板股票数", 29, "盘中触板且收盘未封板",
                              "source=stock_zt_pool_zbgc_em; unique=code; close<limit_up; bursts>=1",
                              metric_key="炸板_COUNT"),
            collector._metric("CN_ZT_POOL", "CN.A_SHARE.涨停", "20261020", "1d",
                              "涨停家数", 54, "收盘封板股票数", "source=stock_zt_pool_em",
                              metric_key="涨停_COUNT"),
            collector._metric("HKEX_MARKET_CAP", "HK.HKEX.EQUITY.ALL", "2026-09-30", "1d",
                              "港股主板及GEM总市值(亿港元)", 454_803.88552796,
                              "港交所主板及GEM同日档案",
                              "source=HKEX Securities Statistics Archive; scope=MAIN+GEM; currency=HKD; "
                              "main_sha256=synthetic; gem_sha256=synthetic",
                              metric_key="TOTAL_MARKET_CAP_YI"),
        ])
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    sections = client.get("/api/data/sections").json()["sections"]
    cn = {item["id"]: item for item in sections[0]["panels"]}
    assert cn["cn-broken-board"]["values"][0]["value"] == 29
    assert cn["cn-broken-board"]["values"][0]["tradingDate"] == "20261020"
    assert cn["cn-sealed-rate"]["values"][0]["value"] == pytest.approx(54 / 83 * 100)
    hk = {item["id"]: item for item in sections[1]["panels"]}
    assert hk["hk-market-cap"]["values"][0]["value"] == pytest.approx(454_803.88552796)
    assert hk["hk-market-cap"]["values"][0]["tradingDate"] == "2026-09-30"
    points = client.get("/api/data/equities/hk/overview").json()["points"]
    assert points[-1]["totalMarketCapYi"] == pytest.approx(454_803.88552796)
    assert points[-1]["turnoverYi"] is None


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
    assert list(a_share_panels) == [
        "cn-market-cap", "cn-float-market-cap", "cn-turnover", "cn-breadth", "cn-limit-pool", "cn-broken-board", "cn-sealed-rate", "cn-limit-height",
        "cn-limit-up-open-return",
    ]
    assert a_share_panels["cn-market-cap"]["status"] == "PASS"
    assert a_share_panels["cn-market-cap"]["values"][0]["value"] == 1_000_000
    assert a_share_panels["cn-breadth"]["status"] == "PARTIAL"
    assert len(a_share_panels["cn-breadth"]["values"]) == 3
    assert a_share_panels["cn-breadth"]["limitations"]
    assert a_share_panels["cn-limit-pool"]["title"] == "A股涨跌停家数"
    assert a_share_panels["cn-limit-up-open-return"]["status"] == "UNAVAILABLE"
    assert [item["id"] for item in sections["sections"][1]["panels"]] == [
        "hk-market-cap", "hk-turnover", "hk-breadth",
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
    electricity = next(item for item in catalog["items"] if item["seriesId"] == "CN_ELECTRICITY_CONSUMPTION")
    assert electricity["name"] == "中国全社会用电量（年内累计）"
    assert electricity["unit"] == "亿千瓦时"

    us_catalog = client.get("/api/data/macro/catalog", params={"country": "US"}).json()
    assert {item["country"] for item in us_catalog["items"]} == {"US"}
    assert next(item for item in us_catalog["items"] if item["seriesId"] == "US_CORE_PCE_QOQ_SAAR_FINAL")["topic"] == "物价"
    assert next(item for item in us_catalog["items"] if item["seriesId"] == "US_IMPORTS_SA")["topic"] == "贸易"

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
        "failedLimitUpCount": None,
        "sealedLimitUpRatePct": None,
        "limitUpHeight": None,
        "limitDownHeight": None,
        "yesterdayLimitUpOpenReturnPct": None,
        "updatedAt": "2026-08-28T16:00:00+08:00",
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


def test_limit_up_open_return_filters_legacy_close_to_close_fallback(tmp_path: Path) -> None:
    data_root = tmp_path / "open-return"
    store = MarketStore(data_root)
    try:
        store.upsert_gold_metrics([
            _limit_up_open_return(
                "2026-08-27", 2.5,
                "pytdx 昨日涨停池4只中3只有当日K线，按(收盘-开盘)/开盘取均值；单位%",
            ),
            _limit_up_open_return(
                "2026-08-28", 8.0,
                "K线覆盖不足(1/4)，回退用东财昨日涨停池涨跌幅(今收/昨收-1)均值；单位%",
            ),
            _limit_up_open_return(
                "2026-08-31", -1.25,
                "source=pytdx; metric=open_to_close_mean; pool_count=4; priced_count=3; "
                "formula=(close-open)/open*100; unit=%",
                series_id="CN_ZT_POOL_OPEN_RETURN_V2",
            ),
            _limit_up_open_return(
                "2026-08-31", 8.0,
                "K线覆盖不足(1/4)，回退用东财昨日涨停池涨跌幅(今收/昨收-1)均值；单位%",
            ),
            _a_share_metric("ADVANCES", "上涨家数", 3_000),
            {
                **_a_share_metric("ADVANCES", "上涨家数", 3_100),
                "metric_id": "A_SHARE_BREADTH:CN.A_SHARE.BREADTH:2026-09-01:1d:ADVANCES",
                "trading_date": "2026-09-01",
                "timestamp": "2026-09-01T16:00:00+08:00",
            },
        ])
    finally:
        store.close()

    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    panels = {item["id"]: item for item in client.get("/api/data/sections").json()["sections"][0]["panels"]}
    panel = panels["cn-limit-up-open-return"]
    assert panel["status"] == "PARTIAL"
    assert panel["values"][0]["tradingDate"] == "2026-08-31"
    assert panel["values"][0]["value"] == -1.25
    assert panel["values"][0]["coverage"] == {"poolCount": 4, "pricedCount": 3}
    assert panel["latestOtherObservationDay"] == "2026-09-01"
    assert panel["stale"] is True
    assert "2026-08-31" in panel["limitations"][-1]

    overview = client.get("/api/data/equities/cn/overview").json()
    by_day = {point["tradingDay"]: point for point in overview["points"]}
    assert by_day["2026-08-27"]["yesterdayLimitUpOpenReturnPct"] == 2.5
    assert by_day["2026-08-28"]["yesterdayLimitUpOpenReturnPct"] is None
    assert by_day["2026-08-31"]["yesterdayLimitUpOpenReturnPct"] == -1.25


def test_a_share_limit_height_exposes_both_daily_streak_maxima_without_false_zero(tmp_path: Path) -> None:
    root = tmp_path / "limit-height"
    store = MarketStore(root)
    try:
        store.upsert_gold_metrics([
            _limit_height("20261013", "涨停", 0),  # no all-null date point
            _limit_height("2026-10-15", "涨停", 4),
            _limit_height("2026-10-15", "跌停", 0),  # legacy missing streak column
            _limit_height("20261016", "涨停", 11),
            _limit_height("20261016", "跌停", 2),
            _limit_height("20261007", "涨停", 99),  # exchange holiday
        ])
    finally:
        store.close()

    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    section = client.get("/api/data/sections").json()["sections"][0]
    panel = next(item for item in section["panels"] if item["id"] == "cn-limit-height")
    assert panel["title"] == "A股涨跌停高度"
    assert panel["status"] == "PARTIAL"
    assert [(item["metricKey"], item["value"], item["unit"]) for item in panel["values"]] == [
        ("涨停_MAX_LIANBAN", 11.0, "板"),
        ("跌停_MAX_LIANBAN", 2.0, "板"),
    ]
    assert {item["tradingDate"] for item in panel["values"]} == {"20261016"}

    overview = client.get("/api/data/equities/cn/overview").json()
    assert overview["units"]["limitHeight"] == "板"
    by_day = {point["tradingDay"]: point for point in overview["points"]}
    assert sorted(by_day) == ["2026-10-15", "2026-10-16"]
    assert (by_day["2026-10-15"]["limitUpHeight"], by_day["2026-10-15"]["limitDownHeight"]) == (4.0, None)
    assert (by_day["2026-10-16"]["limitUpHeight"], by_day["2026-10-16"]["limitDownHeight"]) == (11.0, 2.0)
    selected = client.get(
        "/api/data/equities/cn/overview", params={"startDay": "2026-10-16", "endDay": "2026-10-16"},
    ).json()
    assert [point["tradingDay"] for point in selected["points"]] == ["2026-10-16"]


def test_limit_pool_missing_streak_field_does_not_emit_zero_height(monkeypatch: pytest.MonkeyPatch) -> None:
    def frame(records: list[dict[str, object]]) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda orient: records,
                               columns=["代码", "最新价", "涨停价", "炸板次数"])

    down = [{"连续跌停": 2}, {"名称": "缺少连续跌停字段"}]
    api = SimpleNamespace(
        stock_zt_pool_em=lambda date: frame([{"连板数": 3}, {"连板数": 5}]),
        stock_zt_pool_dtgc_em=lambda date: frame(down),
        stock_zt_pool_zbgc_em=lambda date: frame([]),
        stock_zt_pool_previous_em=lambda date: frame([]),
    )
    monkeypatch.setattr(collector, "_ak", lambda: api)
    monkeypatch.setattr(collector, "_a_share_pool_target_day", lambda: "20261016")

    partial = collector._collect_zt_pool()
    by_key = {str(item["metric_id"]).rsplit(":", 1)[-1]: item["value"] for item in partial.persist.gold_metrics}
    assert partial.status == "PARTIAL_FAILURE"
    assert by_key == {"涨停_COUNT": 2, "涨停_MAX_LIANBAN": 5.0, "跌停_COUNT": 2}
    assert "LIANBAN_UNVERIFIED" in (partial.error or "")

    down[1]["连续跌停"] = 1
    complete = collector._collect_zt_pool()
    by_key = {str(item["metric_id"]).rsplit(":", 1)[-1]: item["value"] for item in complete.persist.gold_metrics}
    assert "LIANBAN_UNVERIFIED" not in (complete.error or "")
    assert by_key["跌停_MAX_LIANBAN"] == 2.0


def test_broken_board_count_requires_unique_unsealed_stocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def frame(rows: list[dict[str, object]]) -> SimpleNamespace:
        return SimpleNamespace(columns=["代码", "最新价", "涨停价", "炸板次数"],
                               to_dict=lambda orient: rows)

    broken = [
        {"代码": "600001", "最新价": 10.0, "涨停价": 11.0, "炸板次数": 2},
        {"代码": "000002", "最新价": 8.8, "涨停价": 9.0, "炸板次数": 1},
    ]
    api = SimpleNamespace(stock_zt_pool_em=lambda date: frame([{"连板数": 1}]),
                          stock_zt_pool_dtgc_em=lambda date: frame([{"连续跌停": 1}]),
                          stock_zt_pool_zbgc_em=lambda date: frame(broken),
                          stock_zt_pool_previous_em=lambda date: frame([]))
    monkeypatch.setattr(collector, "_ak", lambda: api)
    monkeypatch.setattr(collector, "_a_share_pool_target_day", lambda: "20261020")
    valid = collector._collect_zt_pool()
    assert next(item["value"] for item in valid.persist.gold_metrics
                if item["metric_id"].endswith(":炸板_COUNT")) == 2
    broken.append({"代码": "000003", "最新价": 9.0, "涨停价": 9.0, "炸板次数": 1})
    resealed = collector._collect_zt_pool()
    assert next(item["value"] for item in resealed.persist.gold_metrics
                if item["metric_id"].endswith(":炸板_COUNT")) == 2
    broken.pop()
    broken.append(dict(broken[0]))
    invalid = collector._collect_zt_pool()
    assert not any(item["metric_id"].endswith(":炸板_COUNT") for item in invalid.persist.gold_metrics)
    assert "stock_zt_pool_zbgc_em:ValueError" in (invalid.error or "")


def test_open_return_task_does_not_replace_missing_open_return_with_daily_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def frame(records: list[dict[str, object]]) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda orient: records,
                               columns=["代码", "最新价", "涨停价", "炸板次数"])

    previous = [
        {"代码": f"00000{index}", "名称": f"测试{index}", "涨跌幅": 9.0, "最新价": 11.0}
        for index in range(1, 5)
    ]
    api = SimpleNamespace(
        stock_zt_pool_em=lambda date: frame([]),
        stock_zt_pool_dtgc_em=lambda date: frame([]),
        stock_zt_pool_zbgc_em=lambda date: frame([]),
        stock_zt_pool_previous_em=lambda date: frame(previous),
    )
    monkeypatch.setattr(collector, "_ak", lambda: api)
    monkeypatch.setattr(collector, "_a_share_pool_target_day", lambda: "20260828")

    priced = 2

    def dated_bar(symbol: str, day: str) -> tuple[float, float] | None:
        code = int(symbol[-1])
        assert day == "20260828"
        return (10.0, 11.0) if code <= priced else None

    monkeypatch.setattr(collector, "_tencent_dated_open_close", dated_bar)
    insufficient = collector._collect_zt_open_return()
    assert not any(
        item["metric_id"].endswith(":YESTERDAY_LIMIT_UP_OPEN_RETURN")
        for item in insufficient.persist.gold_metrics
    )

    priced = 4
    sufficient = collector._collect_zt_open_return()
    returns = [
        item for item in sufficient.persist.gold_metrics
        if item["metric_id"].endswith(":YESTERDAY_LIMIT_UP_OPEN_RETURN")
    ]
    assert len(returns) == 1
    assert returns[0]["value"] == pytest.approx(10.0)
    assert collector._collect_zt_open_return("20260828").status == "PASS"
    with pytest.raises(ValueError, match="historical open-return day"):
        collector._collect_zt_open_return("20261018")
    assert returns[0]["metric_id"].startswith("CN_ZT_POOL_OPEN_RETURN_V2:")
    assert "pool_count=4; priced_count=4" in returns[0]["calculation_method"]


def test_limit_pool_is_independent_of_slow_open_return_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    def frame(rows: list[dict[str, object]]) -> SimpleNamespace:
        return SimpleNamespace(columns=["代码", "最新价", "涨停价", "炸板次数"],
                               to_dict=lambda orient: rows)

    def forbidden_previous(**_kwargs: object) -> None:
        raise AssertionError("pool task must not fetch individual return source")

    monkeypatch.setattr(collector, "_a_share_pool_target_day", lambda: "20261020")
    monkeypatch.setattr(collector, "_ak", lambda: SimpleNamespace(
        stock_zt_pool_em=lambda date: frame([{"连板数": 1}]),
        stock_zt_pool_dtgc_em=lambda date: frame([{"连续跌停": 1}]),
        stock_zt_pool_zbgc_em=lambda date: frame([
            {"代码": "000001", "最新价": 10.0, "涨停价": 11.0, "炸板次数": 1}]),
        stock_zt_pool_previous_em=forbidden_previous,
    ))
    result = collector._collect_zt_pool()
    assert result.status == "PASS"
    assert len(result.persist.gold_metrics) == 5


def test_tencent_dated_bar_rejects_other_day_and_bounds_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def response(rows: list[list[str]]) -> SimpleNamespace:
        text = "kline_day2026=" + json.dumps({"code": 0, "data": {"sz000001": {"day": rows}}})
        return SimpleNamespace(text=text, content=text.encode(), raise_for_status=lambda: None)

    def get(_url: str, **kwargs: object) -> SimpleNamespace:
        seen.append(kwargs["timeout"])
        return response([["2026-10-19", "9", "10"]])

    monkeypatch.setattr(collector.requests, "get", get)
    assert collector._tencent_dated_open_close("sz000001", "20261020") is None
    monkeypatch.setattr(collector.requests, "get", lambda url, **kwargs: response(
        [["2026-10-20", "10", "11"]]))
    assert collector._tencent_dated_open_close("sz000001", "20261020") == (10.0, 11.0)
    assert seen == [(2, 4)]


def test_official_2026_stock_closures_and_final_day_gate() -> None:
    for day in ("2026-01-01", "2026-02-16", "2026-04-06", "2026-05-04",
                "2026-06-19", "2026-09-25", "2026-10-01", "2026-10-02",
                "2026-10-05", "2026-10-06", "2026-10-07"):
        assert is_known_a_share_closed_day(date.fromisoformat(day))
        assert not is_verified_a_share_trading_day(date.fromisoformat(day))
    assert is_verified_a_share_trading_day(date(2026, 9, 28))
    assert is_verified_a_share_trading_day(date(2026, 10, 8))
    shanghai = ZoneInfo("Asia/Shanghai")
    assert collector._a_share_collection_day(datetime(2026, 10, 7, 16, tzinfo=shanghai)) is None
    assert collector._a_share_collection_day(datetime(2026, 10, 8, 15, 29, tzinfo=shanghai)) is None
    assert collector._a_share_collection_day(datetime(2026, 10, 8, 15, 30, tzinfo=shanghai)) == "20261008"
    assert collector._a_share_collection_day(datetime(2027, 1, 4, 16, tzinfo=shanghai)) is None
    assert collector._a_share_pool_target_day(datetime(2026, 10, 21, 0, 30, tzinfo=shanghai)) == "20261020"


def test_a_share_collectors_never_write_closed_days_or_unverified_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def frame(records: list[dict[str, object]]) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda orient: records)

    def unavailable_api() -> None:
        raise AssertionError("closed day must not fetch network data")

    monkeypatch.setattr(collector, "_ak", unavailable_api)
    monkeypatch.setattr(collector, "_a_share_collection_day", lambda: None)
    monkeypatch.setattr(collector, "_a_share_pool_target_day", lambda: None)
    for fetch in (collector._collect_a_share_breadth, collector._collect_zt_pool,
                  collector._collect_zt_open_return):
        outcome = fetch()
        assert outcome.status == "SKIPPED"
        assert outcome.persist.gold_metrics == []

    snapshot = [
        {"code": "sh600519", "zxj": "1329.00", "zdf": "1.2", "zsz": "100", "turnover": "200"},
        {"code": "sz000001", "zxj": "11.50", "zdf": "-0.3", "zsz": "50", "turnover": "100"},
    ]
    stale_api = SimpleNamespace(
        stock_zh_a_spot_tx=lambda: frame(snapshot),
        stock_zh_a_hist_tx=lambda **kwargs: frame([{"date": "2026-10-16", "close": 1329.0}]),
    )
    monkeypatch.setattr(collector, "_ak", lambda: stale_api)
    monkeypatch.setattr(collector, "_a_share_collection_day", lambda: "20261019")
    failed = collector._collect_a_share_breadth()
    assert failed.status == "FAILED"
    assert failed.persist.gold_metrics == []
    assert failed.error == "SOURCE_DATE_UNVERIFIED"

    def dated_bar(**kwargs: object) -> SimpleNamespace:
        return frame([{"date": "2026-10-19", "close": 1329.0 if kwargs["symbol"] == "sh600519" else 11.5}])

    stale_api.stock_zh_a_hist_tx = dated_bar
    verified = collector._collect_a_share_breadth()
    assert verified.status == "PASS"
    assert {metric["trading_date"] for metric in verified.persist.gold_metrics} == {"2026-10-19"}

    empty_pool_api = SimpleNamespace(
        stock_zt_pool_em=lambda date: frame([]),
        stock_zt_pool_dtgc_em=lambda date: frame([]),
        stock_zt_pool_previous_em=lambda date: frame([]),
    )
    monkeypatch.setattr(collector, "_ak", lambda: empty_pool_api)
    monkeypatch.setattr(collector, "_a_share_pool_target_day", lambda: "20261019")
    pool = collector._collect_zt_pool()
    assert pool.persist.gold_metrics == []
    assert "EMPTY_UNVERIFIED_NOT_ZERO" in pool.detail


def test_expected_a_share_skip_does_not_downgrade_fetch_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        collector.CollectionTaskResult("MACRO_SERIES", "宏观", "fixture", status="PASS"),
        collector.CollectionTaskResult("A_SHARE_BREADTH", "A股", "fixture", status="SKIPPED"),
    ]
    monkeypatch.setattr(collector, "_run_tasks", lambda *args, **kwargs: results)
    monkeypatch.setattr(collector, "_persist_results", lambda *args, **kwargs: None)
    summary = collector.run_fetch_session(tmp_path, tasks=[])
    assert summary["status"] == "PASS"
    assert summary["passed"] == 1
    assert summary["skipped"] == 1


def test_legacy_a_share_closed_day_gold_is_hidden_without_deletion(tmp_path: Path) -> None:
    root = tmp_path / "legacy-closures"
    rows: list[dict[str, object]] = []
    for day, value in (("2026-09-25", 10.0), ("2026-10-01", 11.0),
                       ("2026-10-02", 12.0), ("2026-10-05", 13.0),
                       ("2026-10-06", 14.0), ("2026-10-07", 15.0),
                       ("2026-10-08", 16.0)):
        row = _a_share_metric("TOTAL_AMOUNT_YI", "当日成交额(亿)", value)
        row["metric_id"] = str(row["metric_id"]).replace("2026-08-28", day)
        row["trading_date"] = day
        row["timestamp"] = f"{day}T16:00:00+08:00"
        rows.append(row)
    store = MarketStore(root)
    try:
        store.upsert_gold_metrics(rows)
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    points = client.get("/api/data/equities/cn/overview").json()["points"]
    assert [point["tradingDay"] for point in points] == ["2026-10-08"]
    panels = client.get("/api/data/sections").json()["sections"][0]["panels"]
    turnover = next(panel for panel in panels if panel["id"] == "cn-turnover")
    assert turnover["values"][0]["tradingDate"] == "2026-10-08"
    assert len(rows) == 7  # The runtime Gold rows are intentionally left intact.


def test_hk_overview_aggregates_only_verified_local_tdx_daily_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert overview["source"] == "通达信金融终端（本地）港股股票日线样本"
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
    assert (data_root / "state" / "hk_tdx_daily_overview.json").is_file()
    data_sections_api._HK_OVERVIEW_CACHE.clear()
    with monkeypatch.context() as patches:
        patches.setattr(data_sections_api.duckdb, "connect",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("disk cache was ignored")))
        assert data_sections_api._hk_tdx_overview_rows(data_root)[-1]["turnoverYi"] == 3.5
    panels = {item["id"]: item for item in client.get("/api/data/sections").json()["sections"][1]["panels"]}
    assert panels["hk-market-cap"]["status"] == "UNAVAILABLE"
    assert panels["hk-turnover"]["status"] == "PARTIAL"
    assert panels["hk-turnover"]["values"][0]["coverage"] == 3
    assert panels["hk-breadth"]["status"] == "PARTIAL"
    cache = data_root / "state" / "hk_tdx_daily_overview.json"
    stale = json.loads(cache.read_text(encoding="utf-8"))
    stale["revision"] = "old-silver-revision"
    cache.write_text(json.dumps(stale), encoding="utf-8")
    data_sections_api._HK_OVERVIEW_CACHE.clear()
    assert data_sections_api._hk_tdx_overview_rows(data_root)[-1]["turnoverYi"] == 3.5
    assert json.loads(cache.read_text(encoding="utf-8"))["revision"] != "old-silver-revision"
    cache.write_text("{broken", encoding="utf-8")
    data_sections_api._HK_OVERVIEW_CACHE.clear()
    assert data_sections_api._hk_tdx_overview_rows(data_root)[-1]["turnoverYi"] == 3.5
