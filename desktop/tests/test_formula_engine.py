"""Deterministic tests for the isolated formula_v1 indicator engine."""

from __future__ import annotations

import math

import pytest

from market_monitor.formula_engine import (
    FormulaError,
    bollinger_lower,
    bollinger_upper,
    close_new_high,
    close_new_low,
    cross_sectional_momentum,
    down_count,
    down_up_ratio,
    evaluate_formula,
    gann_falling_rate,
    gann_rising_rate,
    hsar_resistance,
    hsar_support,
    limit_down_count,
    limit_up_count,
    moving_average,
    no_limit_down,
    no_limit_up,
    parse_formula,
    performance_metrics,
    period_return,
    range_high_low_ratio,
    range_low_high_ratio,
    standard_deviation,
    time_series_momentum,
    up_count,
    up_down_ratio,
    volume_slope,
)


def test_time_series_formula_has_no_future_data_and_requires_history() -> None:
    program = parse_formula(
        "value = ts_momentum(close, lookback)\nsignal = value > threshold",
        ["lookback", "threshold"],
    )
    result = evaluate_formula(
        program,
        {"close": [100.0, 110.0, 99.0], "lookback": 1, "threshold": 0.0},
        {"ts_momentum": time_series_momentum},
    )
    assert result["value"] == [None, pytest.approx(0.1), pytest.approx(-0.1)]
    assert result["signal"] == [False, True, False]


def test_cross_section_uses_average_tie_rank_and_highest_bucket_for_best() -> None:
    ranked = cross_sectional_momentum(
        {"A": [100.0, 110.0], "B": [100.0, 110.0], "C": [100.0, 90.0], "D": [100.0, 80.0]},
        1,
        2,
    )
    assert ranked["A"][-1] == ranked["B"][-1] == 2
    assert ranked["C"][-1] == ranked["D"][-1] == 1


def test_performance_annualization_uses_actual_time_density() -> None:
    metrics = performance_metrics(
        ["2024-01-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"],
        [100.0, 90.0, 121.0],
        0.02,
    )
    assert metrics["annualReturn"].value == pytest.approx(0.21, rel=0.01)
    assert metrics["annualVolatility"].value is not None
    assert metrics["maxDrawdown"].value == pytest.approx(0.1)
    assert math.isfinite(metrics["sharpe"].value or math.nan)
    assert metrics["calmar"].value == pytest.approx((metrics["annualReturn"].value or 0) / 0.1)


@pytest.mark.parametrize(
    "source,code",
    [
        ("import os\nvalue = 1\nsignal = True", "UNSAFE_SYNTAX"),
        ("value = close.__class__\nsignal = True", "UNSAFE_SYNTAX"),
        ("value = open(1)\nsignal = True", "UNKNOWN_FUNCTION"),
        ("while True:\n  pass\nvalue = 1\nsignal = True", "UNSAFE_SYNTAX"),
    ],
)
def test_formula_rejects_unsafe_language(source: str, code: str) -> None:
    with pytest.raises(FormulaError) as captured:
        parse_formula(source)
    assert captured.value.code == code


def test_formula_is_lazy_and_division_by_zero_is_unavailable() -> None:
    program = parse_formula("unused = sharpe(strategy_returns, rf, n)\nvalue = 1 / zero\nsignal = value > 0", ["rf", "n", "zero"])
    result = evaluate_formula(
        program,
        {"rf": 0.02, "n": 20, "zero": 0},
        {"sharpe": lambda *_args: pytest.fail("unused indicator must not execute")},
    )
    assert result == {"value": None, "signal": False}


def test_zero_volatility_and_drawdown_return_reasons() -> None:
    metrics = performance_metrics(
        ["2025-01-01T00:00:00+00:00", "2025-02-01T00:00:00+00:00", "2025-03-01T00:00:00+00:00"],
        [100.0, 100.0, 100.0],
    )
    assert metrics["sharpe"].value is None
    assert metrics["sharpe"].reason == "年化波动率为零"
    assert metrics["calmar"].value is None
    assert metrics["calmar"].reason == "最大回撤率为零"


def test_formula_size_and_extreme_power_are_bounded() -> None:
    with pytest.raises(FormulaError) as captured:
        parse_formula("value = 1\nsignal = True\n" + " " * 10_000)
    assert captured.value.code == "FORMULA_TOO_LARGE"
    program = parse_formula("value = 9 ** exponent\nsignal = value > 0", ["exponent"])
    assert evaluate_formula(program, {"exponent": 1_000_000}, {}) == {"value": None, "signal": False}


def test_hsar_ma_sd_bollinger_and_gann_are_history_only() -> None:
    close = [1.0, 2.0, 3.0, 4.0]
    high = [10.0, 12.0, 13.0, 14.0]
    low = [6.0, 7.0, 8.0, 9.0]
    assert moving_average(close, 3) == [None, None, 2.0, 3.0]
    assert standard_deviation(close, 3) == [None, None, pytest.approx(math.sqrt(2 / 3)), pytest.approx(math.sqrt(2 / 3))]
    assert bollinger_upper(close, 3, 2)[2] == pytest.approx(2 + 2 * math.sqrt(2 / 3))
    assert bollinger_lower(close, 3, 2)[3] == pytest.approx(3 - 2 * math.sqrt(2 / 3))
    assert hsar_resistance(high, 3, 50) == [None, None, 12.5, 13.5]
    # 支撑位遵从需求中的“最高的若干根最低价”定义，而不是惯常的最低低点定义。
    assert hsar_support(low, 3, 50) == [None, None, 7.5, 8.5]
    assert gann_rising_rate(low, 3) == [None, None, pytest.approx(2 / 3), pytest.approx(2 / 3)]
    assert gann_falling_rate(high, 3) == [None, None, pytest.approx(-1.0), pytest.approx(-2 / 3)]


def test_new_ohlc_formula_functions_are_whitelisted() -> None:
    program = parse_formula(
        "value = bollinger_upper(close, lookback, multiplier)\nsignal = value > hsar_support(low, lookback, top_percent)",
        ["lookback", "multiplier", "top_percent"],
    )
    result = evaluate_formula(
        program,
        {"close": [1.0, 2.0, 3.0], "low": [0.5, 1.5, 2.5], "lookback": 3, "multiplier": 2.0, "top_percent": 50.0},
        {"bollinger_upper": bollinger_upper, "hsar_support": hsar_support},
    )
    assert result["value"][-1] is not None
    assert result["signal"] == [False, False, True]


def test_strategy_condition_series_cover_counts_ratios_breakouts_and_volume() -> None:
    opens = [10.0, 10.0, 10.0]
    closes = [11.0, 9.0, 12.0]
    highs = [11.0, 12.0, 13.0]
    lows = [9.0, 8.0, 10.0]
    assert period_return(closes, 1) == [None, pytest.approx(-2 / 11), pytest.approx(1 / 3)]
    assert up_count(opens, closes, 3) == [None, None, 2]
    assert down_count(opens, closes, 3) == [None, None, 1]
    assert up_down_ratio(opens, closes, 3) == [None, None, 2.0]
    assert down_up_ratio(opens, closes, 3) == [None, None, 0.5]
    assert range_high_low_ratio(highs, lows, 3) == [None, None, pytest.approx(13 / 8)]
    assert range_low_high_ratio(highs, lows, 3) == [None, None, pytest.approx(8 / 13)]
    assert close_new_high([10.0, 11.0, 15.0], [10.5, 11.5, 15.5], 2) == [False, False, True]
    assert close_new_low([10.0, 9.0, 5.0], [9.5, 8.5, 4.5], 2) == [False, False, True]
    assert volume_slope([100.0, 200.0, 500.0], 1, 3) == [None, None, pytest.approx(400 / 3)]


def test_limit_conditions_fail_closed_when_history_is_unknown() -> None:
    flags = [None, True, False, False]
    assert limit_up_count(flags, 2) == [None, None, 1, 0]
    assert limit_down_count(flags, 2) == [None, None, 1, 0]
    assert no_limit_up(flags, 2) == [False, False, False, True]
    assert no_limit_down(flags, 2) == [False, False, False, True]
