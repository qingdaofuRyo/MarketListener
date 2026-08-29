import math
from pathlib import Path

import pytest

from market_monitor.futures_heat import (
    compute_futures_long_short_heat,
    compute_product_daily_signals,
    exponential_decay_weights,
    load_long_short_heat_config,
    parse_long_short_heat_config,
)


def _bar(
    day: str,
    product: str,
    settlement: float | None,
    *,
    exchange: str = "SHFE",
    kind: str = "WEIGHTED",
    contract: str | None = None,
    oi: float = 10.0,
    multiplier: float | None = 10.0,
    margin: float | None = 0.1,
) -> dict:
    return {
        "trading_day": day,
        "exchange": exchange,
        "product_code": product,
        "series_kind": kind,
        "symbol": contract or f"{product}.WEIGHTED",
        "settlement": settlement,
        "open_interest": oi,
        "contract_multiplier": multiplier,
        "margin_rate": margin,
    }


def _two_day_product(product: str, first: float, second: float, **kwargs) -> list[dict]:
    return [
        _bar("2026-08-03", product, first, **kwargs),
        _bar("2026-08-04", product, second, **kwargs),
        _bar("2026-08-03", product, first, kind="CONTRACT", contract=f"{product}2610", **kwargs),
        _bar("2026-08-04", product, second, kind="CONTRACT", contract=f"{product}2610", **kwargs),
    ]


def test_config_and_exponential_decay_are_strict_and_normalized(tmp_path: Path) -> None:
    config = load_long_short_heat_config()
    assert config.lookback_trading_days == 10
    assert config.half_life_trading_days == 3
    assert config.min_fund_coverage == pytest.approx(0.8)
    assert (config.user_weight_min, config.user_weight_max, config.user_weight_step) == (0, 1, 0.05)
    assert config.state_bands[3] == (-20, 20, "多空均衡")
    weights = exponential_decay_weights(10, config.half_life_trading_days)
    assert sum(weights) == pytest.approx(1.0)
    assert all(left > right for left, right in zip(weights, weights[1:]))
    assert weights[3] / weights[0] == pytest.approx(0.5)

    payload = {
        "schemaVersion": 1,
        "formulaVersion": "test-v1",
        "lookbackTradingDays": 10,
        "timeWeight": {"method": "exponential_decay", "halfLifeTradingDays": 3},
        "defaultUserWeight": {"breadthWeight": 0.4, "fundWeight": 0.6},
        "userWeight": {"min": 0, "max": 1, "step": 0.05},
        "score": {"min": -100, "max": 100},
        "stateBands": [
            {"min": -100, "max": -20, "label": "偏空"},
            {"min": -20, "max": 20, "label": "均衡"},
            {"min": 20, "max": 100, "label": "偏多"},
        ],
        "fundUnit": "元",
        "neutralThreshold": 0,
        "divergenceThreshold": 10,
        "minFundCoverage": 0.8,
        "excludeExchanges": ["CFFEX"],
    }
    assert parse_long_short_heat_config(payload).formula_version == "test-v1"
    with pytest.raises(ValueError, match="unknown"):
        parse_long_short_heat_config({**payload, "typo": 1})
    with pytest.raises(ValueError, match="sum to 1"):
        parse_long_short_heat_config(
            {**payload, "defaultUserWeight": {"breadthWeight": 0.5, "fundWeight": 0.6}}
        )


def test_weighted_settlement_return_is_preferred_and_cffex_is_excluded() -> None:
    bars = _two_day_product("RB", 100, 110)
    bars.extend(_two_day_product("IF", 100, 120, exchange="CFFEX"))
    signals = compute_product_daily_signals(bars)
    latest = [signal for signal in signals if signal.trade_date == "2026-08-04"]
    assert len(latest) == 1
    assert latest[0].product_code == "RB"
    assert latest[0].product_return == pytest.approx(math.log(1.1))
    assert latest[0].return_method == "WEIGHTED_SETTLEMENT"
    # Fund uses the existing two-sided formula: 110 * 10 OI * 10 multiplier * 10% * 2.
    assert latest[0].deposited_fund == pytest.approx(2200)


def test_contract_settlement_oi_weighted_fallback() -> None:
    bars = [
        _bar("2026-08-03", "RB", None),
        _bar("2026-08-04", "RB", None),
        _bar("2026-08-03", "RB", 100, kind="CONTRACT", contract="RB2610", oi=3),
        _bar("2026-08-04", "RB", 110, kind="CONTRACT", contract="RB2610", oi=3),
        _bar("2026-08-03", "RB", 200, kind="CONTRACT", contract="RB2611", oi=1, multiplier=20),
        _bar("2026-08-04", "RB", 180, kind="CONTRACT", contract="RB2611", oi=1, multiplier=20),
    ]
    latest = compute_product_daily_signals(bars)[-1]
    expected = (math.log(1.1) * 330 * 10 + math.log(0.9) * 180 * 20) / (330 * 10 + 180 * 20)
    assert latest.product_return == pytest.approx(expected)
    assert latest.return_method == "CONTRACT_MARKET_VALUE_WEIGHTED"
    assert latest.return_contract_count == 2


def test_contract_return_does_not_treat_missing_multiplier_as_valid_weight() -> None:
    bars = [
        _bar("2026-08-03", "RB", None),
        _bar("2026-08-04", "RB", None),
        _bar("2026-08-03", "RB", 100, kind="CONTRACT", contract="RB2610", multiplier=None),
        _bar("2026-08-04", "RB", 110, kind="CONTRACT", contract="RB2610", multiplier=None),
    ]
    latest = compute_product_daily_signals(bars)[-1]
    assert latest.product_return is None
    assert latest.direction is None
    assert latest.return_contract_count == 0


def test_attachment_breadth_fund_and_total_component_examples() -> None:
    bars: list[dict] = []
    for index in range(70):
        bars.extend(_two_day_product(f"U{index}", 100, 101, multiplier=1, margin=0.1))
    for index in range(30):
        bars.extend(_two_day_product(f"D{index}", 100, 99, multiplier=1, margin=0.1))
    latest = compute_futures_long_short_heat(bars)[-1]
    assert latest.up_variety_count == 70
    assert latest.down_variety_count == 30
    assert latest.breadth_score_daily == pytest.approx(40)
    assert latest.breadth_score_10d == pytest.approx(40)

    fund_bars = _two_day_product("UP", 100, 150, oi=10, multiplier=1, margin=0.1)
    fund_bars += _two_day_product("DOWN", 100, 50, oi=10, multiplier=1, margin=0.1)
    fund_latest = compute_futures_long_short_heat(fund_bars)[-1]
    assert fund_latest.up_fund == pytest.approx(300)
    assert fund_latest.down_fund == pytest.approx(100)
    assert fund_latest.fund_score_daily == pytest.approx(50)


def test_ten_observation_days_skip_holiday_and_have_no_future_leakage() -> None:
    days = [
        "2026-09-25",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-09",
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
        "2026-10-19",
    ]
    bars = [_bar(days[0], "RB", 100), _bar(days[0], "RB", 100, kind="CONTRACT", contract="RB2610")]
    price = 100.0
    for day in days[1:]:
        price += 1
        bars.extend([_bar(day, "RB", price), _bar(day, "RB", price, kind="CONTRACT", contract="RB2610")])
    before_future = compute_futures_long_short_heat(bars, trading_days=days)
    assert before_future[-1].is_warmup is False
    assert before_future[-1].breadth_score_10d == pytest.approx(100)
    historical = before_future[4].to_dict()

    future_day = "2026-10-20"
    bars.extend([_bar(future_day, "RB", 1), _bar(future_day, "RB", 1, kind="CONTRACT", contract="RB2610")])
    after_future = compute_futures_long_short_heat(bars, trading_days=[*days, future_day])
    assert after_future[4].to_dict() == historical


def test_warmup_renormalizes_available_days_and_missing_is_not_zero_filled() -> None:
    bars = _two_day_product("RB", 100, 110)
    result = compute_futures_long_short_heat(bars, trading_days=["2026-08-03", "2026-08-04"])
    assert result[-1].is_warmup is True
    assert result[-1].breadth_score_daily == pytest.approx(100)
    assert result[-1].breadth_score_10d == pytest.approx(100)

    missing = compute_futures_long_short_heat(
        [_bar("2026-08-03", "RB", 100), _bar("2026-08-04", "RB", None)],
        trading_days=["2026-08-03", "2026-08-04"],
    )[-1]
    assert missing.breadth_score_daily is None
    assert missing.breadth_score_10d is None
    assert missing.data_quality_status == "UNAVAILABLE"


def test_warmup_counts_available_fund_observations_not_calendar_rows() -> None:
    days = [f"2026-08-{day:02d}" for day in range(1, 12)]
    bars: list[dict] = []
    for index, day in enumerate(days):
        bars.append(_bar(day, "RB", 100 + index))
        bars.append(
            _bar(
                day,
                "RB",
                100 + index,
                kind="CONTRACT",
                contract="RB2610",
                margin=None if index == 1 else 0.1,
            )
        )
    result = compute_futures_long_short_heat(bars, trading_days=days)
    assert result[-1].fund_score_10d == pytest.approx(100)
    assert result[-1].is_warmup is True


def test_fund_coverage_gate_blocks_fund_and_divergence() -> None:
    bars = _two_day_product("RB", 100, 110)
    bars += _two_day_product("CU", 100, 90, margin=None)
    latest = compute_futures_long_short_heat(bars)[-1]
    assert latest.breadth_score_daily == pytest.approx(0)
    assert latest.fund_coverage == pytest.approx(0.5)
    assert latest.fund_score_daily is None
    assert latest.fund_score_10d is None
    assert latest.divergence is None
    assert latest.data_quality_status == "PARTIAL"


def test_fund_coverage_boundary_is_inclusive_and_expected_universe_counts_missing() -> None:
    bars: list[dict] = []
    expected = []
    for index in range(5):
        product = f"P{index}"
        expected.append(f"SHFE.{product}")
        bars += _two_day_product(product, 100, 101, margin=None if index == 4 else 0.1)
    expected.append("SHFE.MISSING")

    latest = compute_futures_long_short_heat(
        bars,
        expected_products_by_day={"2026-08-04": expected},
    )[-1]
    assert latest.total_variety_count == 6
    assert latest.valid_variety_count == 5
    assert latest.missing_variety_count == 1
    assert latest.return_coverage == pytest.approx(5 / 6)
    assert latest.fund_coverage == pytest.approx(0.8)
    assert latest.fund_score_daily == pytest.approx(100)
    assert latest.data_quality_status == "PARTIAL"


def test_flat_and_zero_denominators_stay_in_bounds_and_unavailable() -> None:
    flat = compute_futures_long_short_heat(_two_day_product("RB", 100, 100))[-1]
    assert flat.flat_variety_count == 1
    assert flat.breadth_score_daily is None
    assert flat.fund_score_daily is None
    assert flat.data_quality_status == "UNAVAILABLE"

    all_up = compute_futures_long_short_heat(_two_day_product("RB", 100, 110))[-1]
    all_down = compute_futures_long_short_heat(_two_day_product("RB", 100, 90))[-1]
    assert all_up.breadth_score_daily == pytest.approx(100)
    assert all_down.breadth_score_daily == pytest.approx(-100)
    assert -100 <= all_up.fund_score_daily <= 100
    assert -100 <= all_down.fund_score_daily <= 100
