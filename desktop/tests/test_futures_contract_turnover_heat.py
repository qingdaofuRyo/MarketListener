from __future__ import annotations

import pytest

from market_monitor.futures_contract_turnover_heat import (
    FORMULA_VERSION,
    calculate_contract_turnover_heat,
)
from market_monitor.futures_rule_sync import RuleBook


def _bar(day: str, exchange: str, contract: str, close: float) -> dict[str, object]:
    return {
        "trade_date": day, "exchange": exchange, "contract_code": contract,
        "product_code": "".join(character for character in contract if character.isalpha()),
        "close": close, "settlement": close, "open_interest": 10,
    }


def _turnover(exchange: str, contract: str, amount: float, close: float) -> dict[str, object]:
    return {
        "trade_date": "2026-09-08", "exchange": exchange, "contract_code": contract,
        "close": close, "turnover_cny": amount,
        "turnover_unit": "CNY_10K", "turnover_sidedness": "ONE_SIDED",
        "quality_status": "PASS",
    }


def test_contract_heat_uses_prior_valid_close_and_labels_partial_turnover_sample() -> None:
    bars = [
        _bar("2026-09-07", "SHFE", "CU2609", 100),
        _bar("2026-09-07", "SHFE", "AU2609", 200),
        _bar("2026-09-07", "DCE", "M2609", 300),
        _bar("2026-09-08", "SHFE", "CU2609", 110),
        _bar("2026-09-08", "SHFE", "AU2609", 190),
        _bar("2026-09-08", "DCE", "M2609", 310),
    ]
    official = [
        _turnover("SHFE", "CU2609", 100, 110),
        _turnover("SHFE", "AU2609", 50, 190),
    ]
    coverage = [
        {"trade_date": "2026-09-08", "exchange": "SHFE", "status": "PASS"},
        {"trade_date": "2026-09-08", "exchange": "DCE", "status": "FAILED"},
    ]

    points = calculate_contract_turnover_heat(bars, official, coverage)
    first, second = points

    assert first["count_score_daily"] is None
    assert second["formula_version"] == FORMULA_VERSION
    assert (second["up_contract_count"], second["down_contract_count"]) == (2, 1)
    assert second["count_score_daily"] == pytest.approx(100 / 3)
    assert second["up_turnover_cny"] == 100
    assert second["down_turnover_cny"] == 50
    assert second["turnover_coverage"] == pytest.approx(2 / 3)
    assert second["turnover_score_daily"] == pytest.approx(100 / 3)
    assert second["missing_exchanges_json"] == '["DCE"]'
    assert second["data_quality_status"] == "PARTIAL"

    complete = calculate_contract_turnover_heat(
        bars, [*official, _turnover("DCE", "M2609", 200, 310)],
        [{**row, "status": "PASS"} for row in coverage],
    )[-1]
    assert complete["turnover_coverage"] == 1
    assert complete["turnover_score_daily"] == pytest.approx(100 * 250 / 350)
    assert complete["turnover_score_10d"] == complete["turnover_score_daily"]


def test_contract_heat_keeps_deposited_funds_as_one_total() -> None:
    rules = RuleBook({
        "2026-09-08": {
            "products": {
                "SHFE.CU": {"contractMultiplier": 5, "marginRate": 0.1},
                "DCE.M": {"contractMultiplier": 10, "marginRate": 0.2},
            },
            "contractOverrides": {},
        }
    })
    bars = [
        _bar("2026-09-08", "SHFE", "CU2609", 100),
        _bar("2026-09-08", "DCE", "M2609", 200),
    ]
    point = calculate_contract_turnover_heat(bars, [], [], rule_book=rules)[0]

    assert point["total_deposit_cny"] == pytest.approx(2 * 100 * 5 * 10 * 0.1 + 2 * 200 * 10 * 10 * 0.2)
    assert point["deposit_coverage"] == 1
    assert point["up_turnover_cny"] == point["down_turnover_cny"] == 0
