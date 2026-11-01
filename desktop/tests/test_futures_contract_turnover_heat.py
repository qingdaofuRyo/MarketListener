from __future__ import annotations

import pytest

from market_monitor.futures_contract_turnover_heat import FORMULA_VERSION, calculate_contract_turnover_heat
from market_monitor.futures_rule_sync import RuleBook


def _quote(exchange, contract, close, previous, amount=100):
    return {
        "trade_date": "2026-09-08", "exchange": exchange, "contract_code": contract,
        "product_code": contract[:-4], "close": close, "previous_settlement": previous,
        "settlement": close, "open_interest": 10, "volume": 10, "turnover_cny": amount,
        "turnover_sidedness": "ONE_SIDED", "turnover_quality_status": "PASS",
        "quality_status": "PASS", "parser_version": "official-futures-daily-v4", "adapter": f"official-{exchange.lower()}-daily-v2",
    }


def _coverage(failed=None):
    return [{"trade_date": "2026-09-08", "exchange": venue, "status": "FAILED" if venue == failed else "PASS", "adapter": f"official-{venue.lower()}-daily-v2"}
            for venue in ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")]


def test_heat_uses_close_minus_previous_settlement_and_includes_financial_and_month_average():
    rows = [_quote("SHFE", "CU2610", 110, 100, 100), _quote("CFFEX", "IF2610", 100, 110, 50), _quote("DCE", "L-F2610", 30, 20, 200), _quote("GFEX", "SI2610", 20, 20, 0)]
    tdx = [{"trade_date": "2026-09-08", "exchange": "SHFE", "contract_code": "CU2610", "close": 1}]
    point = calculate_contract_turnover_heat(tdx, rows, _coverage())[0]
    assert point["formula_version"] == FORMULA_VERSION
    assert (point["up_contract_count"], point["down_contract_count"], point["flat_contract_count"]) == (2, 1, 1)
    assert point["count_score_daily"] == pytest.approx(100 / 3)
    assert point["turnover_score_daily"] == pytest.approx(100 * 250 / 350)
    assert point["turnover_coverage"] == 1
    assert point["missing_exchanges_json"] == "[]"
    assert calculate_contract_turnover_heat(tdx, [], []) == []


def test_missing_direction_and_missing_amount_are_not_zero_filled():
    rows = [_quote("SHFE", "CU2610", 110, 100), _quote("DCE", "M2610", 30, None, 0), _quote("CFFEX", "IF2610", 20, 30, None)]
    point = calculate_contract_turnover_heat([], rows, _coverage("GFEX"))[0]
    assert point["missing_direction_count"] == 1
    assert point["turnover_missing_contract_count"] == 1
    assert point["turnover_coverage"] == .5
    assert point["missing_exchanges_json"] == '["GFEX"]'
    assert point["data_quality_status"] == "PARTIAL"


def test_contract_heat_keeps_deposited_funds_as_one_total():
    rules = RuleBook({"2026-09-08": {"products": {"SHFE.CU": {"contractMultiplier": 5, "marginRate": .1}, "DCE.M": {"contractMultiplier": 10, "marginRate": .2}}, "contractOverrides": {}}})
    rows = [_quote("SHFE", "CU2610", 100, 100), _quote("DCE", "M2610", 200, 200)]
    point = calculate_contract_turnover_heat([], rows, _coverage(), rule_book=rules)[0]
    assert point["total_deposit_cny"] == pytest.approx(2 * 100 * 5 * 10 * .1 + 2 * 200 * 10 * 10 * .2)
    assert point["deposit_coverage"] == 1


def test_smoothing_keeps_missing_trading_day_as_gap():
    rows = [{**_quote("SHFE", "CU2610", 110, 100), "trade_date": "2026-09-07"}]
    coverage = [{**row, "trade_date": "2026-09-07"} for row in _coverage()] + _coverage("SHFE")
    first, missing = calculate_contract_turnover_heat([], rows, coverage)
    assert first["count_score_daily"] == 100
    assert missing["count_score_daily"] is None
    assert missing["total_contract_count"] == 0


def test_published_zero_close_without_trade_is_not_a_down_contract():
    rows = [_quote("CZCE", "CY2302", 0, 21955, 0), _quote("DCE", "A2302", 100, 90, 100)]
    rows[0]["volume"] = 0
    point = calculate_contract_turnover_heat([], rows, _coverage())[0]
    assert point["up_contract_count"] == 1
    assert point["down_contract_count"] == 0
    assert point["missing_direction_count"] == 0
    assert point["total_contract_count"] == 1
