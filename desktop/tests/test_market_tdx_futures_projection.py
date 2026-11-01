"""Dated TDX quantities and exchange supplements remain source isolated."""

import json
from pathlib import Path

import pytest

from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_SCHEMA
from market_monitor.market_tdx_futures_projection import (
    apply_tdx_daily_changes,
    project_contract_day,
    refresh_financial_coverage,
    strict_tdx_returns,
)


DAY = "2026-09-30"


def test_financial_coverage_keeps_last_verified_snapshot_while_index_is_stale(
    tmp_path: Path, monkeypatch,
) -> None:
    from market_monitor import market_query_cache

    root = tmp_path / "data"
    (root / "silver").mkdir(parents=True)
    path = root / "state" / "futures_financial_coverage.json"
    path.parent.mkdir(parents=True)
    previous = {"tdxLatestDay": "2026-10-09", "contractCount": 91}
    path.write_text(json.dumps(previous), encoding="utf-8")

    class StaleIndex:
        def ensure_ready(self):
            raise market_query_cache.KLineCacheUnavailable("synthetic import pending")

    monkeypatch.setattr(market_query_cache, "get_kline_query_store", lambda _root: StaleIndex())
    result = refresh_financial_coverage(root)
    assert result == {**previous, "queryIndexStatus": "STALE"}
    assert json.loads(path.read_text(encoding="utf-8")) == previous


def _tdx(**changes):
    return {"instrument_id": "CN.CFFEX.FUTURE.IF2612.CONTRACT.TDX", "trading_day": DAY,
            "quality_status": "PASS", "open": 4300., "high": 4310., "low": 4290., "close": 4300.8,
            "settlement": 4299.39990234375, "volume": 38676, "open_interest": 166992,
            **changes}


def _official(**changes):
    return {"officialMetricsDate": DAY, "exchange": "CFFEX", "contractCode": "IF2612",
            "open": 4300., "high": 4310., "low": 4290., "close": 4300.8,
            "settlement": 4299.4, "volume": 38676, "openInterest": 166992,
            "amount": 49_812_346_080., "specMarginRateLong": .12,
            "specMarginRateShort": .12, "marginSourceKind": "OFFICIAL_EXCHANGE",
            "officialMetricsSource": "http://www.cffex.com.cn/sj/", "officialPayloadSha256": "a" * 64,
            **changes}


def _project(bar=None, official=None, multiplier=300):
    return project_contract_day(bar if bar is not None else _tdx(),
                                official if official is not None else _official(),
                                day=DAY, exchange="CFFEX", contract_code="IF2612", multiplier=multiplier)


def test_float32_settlement_matches_and_two_sides_use_tdx_value():
    row = _project()
    assert row["comparisonStatus"] == "MATCHED"
    assert row["notionalValue"] == pytest.approx(166992 * 4299.39990234375 * 300)
    assert row["capitalDeposit"] == pytest.approx(row["notionalValue"] * .24)
    assert row["amount"] == 49_812_346_080
    assert row["fieldSources"]["settlement"] == "TDX_FUTURES"


def test_quantity_difference_quarantines_dependent_values_only():
    row = _project(official=_official(openInterest=166993))
    assert row["comparisonIssues"]["openInterest"] == {"tdx": 166992., "exchange": 166993.}
    assert row["notionalValue"] is row["capitalDeposit"] is None
    assert row["amount"] == 49_812_346_080
    assert row["settlement"] == pytest.approx(4299.39990234375)
    assert row["officialMetricsStatus"] == "WAITING_VERIFICATION"
    assert _project()["notionalValue"] is not None  # Updated evidence releases it automatically.


def test_missing_or_third_party_margin_never_patches_official_history():
    no_supplement = project_contract_day(_tdx(), None, day=DAY, exchange="CFFEX",
                                         contract_code="IF2612", multiplier=300)
    assert no_supplement["notionalValue"] is not None
    assert no_supplement["amount"] is no_supplement["capitalDeposit"] is None
    assert no_supplement["comparisonStatus"] == "UNCOMPARED"
    backup = _project(official=_official(marginSourceKind="THIRD_PARTY_CURRENT_EXCHANGE_STANDARD"))
    assert backup["notionalValue"] is not None and backup["amount"] is not None
    assert backup["capitalDeposit"] is None
    stale = _project(official=_official(officialMetricsDate="2026-09-29"))
    assert stale["amount"] is stale["capitalDeposit"] is None


def test_actual_trading_calendar_prevents_cross_gap_returns(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "cn_futures_trading_calendar.json").write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-09-30T16:00:00+08:00",
        "tradingDays": ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", DAY],
    }), encoding="utf-8")
    bars = [_tdx(trading_day=day, close=price, settlement=price - 1)
            for day, price in (("2026-09-24", 100), ("2026-09-25", 102),
                               ("2026-09-28", 104), ("2026-09-29", 106), (DAY, 108))]
    apply_tdx_daily_changes(tmp_path, bars)
    assert bars[2]["change"] == 104 - 101  # Weekend is not a trading-day gap.
    assert strict_tdx_returns(tmp_path, bars)["3"] == pytest.approx(100 * (108 / 102 - 1))
    missing = [bar for bar in bars if bar["trading_day"] != "2026-09-29"]
    apply_tdx_daily_changes(tmp_path, missing)
    assert missing[-1]["change"] is None
    assert strict_tdx_returns(tmp_path, missing)["3"] is None


def test_return_endpoint_price_difference_is_quarantined(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    days = ["2026-09-25", "2026-09-28", "2026-09-29", DAY]
    (state / "cn_futures_trading_calendar.json").write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-09-30T16:00:00+08:00",
        "tradingDays": days,
    }), encoding="utf-8")
    bars = [_tdx(trading_day=day, close=100 + index, settlement=100 + index,
                 comparisonFields={"close": "MATCHED", "settlement": "MATCHED"})
            for index, day in enumerate(days)]
    assert strict_tdx_returns(tmp_path, bars)["3"] == pytest.approx(3)
    bars[0]["comparisonFields"]["close"] = "MISMATCH"
    assert strict_tdx_returns(tmp_path, bars)["3"] is None
