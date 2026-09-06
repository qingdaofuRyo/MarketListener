from __future__ import annotations

from time import perf_counter

import pytest

from market_monitor.strategy_function_registry import build_builtin_strategy_function_registry
from market_monitor.strategy_functions import (
    STRATEGY_FUNCTION_IMPLEMENTATIONS,
    StrategyFunctionError,
    atr,
    atr_percent,
    bollinger,
    chaikin_volatility,
    crossover,
    crossunder,
    donchian,
    ema,
    execute_strategy_function,
    highest,
    keltner,
    lowest,
    macd,
    market_cap,
    percentage_change,
    relative_volatility_index,
    rsi,
    sma,
    stddev,
    stochastic,
    true_range,
    up_down_count,
    volume_change,
    volume_profile,
)


def test_rolling_functions_are_deterministic_and_do_not_look_ahead() -> None:
    original = [1.0, 2.0, 3.0, 4.0]

    assert sma(original, 3) == [None, None, 2.0, 3.0]
    assert ema(original, 3) == [None, None, 2.0, 3.0]
    assert highest(original, 3) == [None, None, 3.0, 4.0]
    assert lowest(original, 3) == [None, None, 1.0, 2.0]
    assert stddev(original, 2)[1] == pytest.approx(0.5)
    assert sma([*original, 1000.0], 3)[: len(original)] == sma(original, 3)
    assert original == [1.0, 2.0, 3.0, 4.0]


def test_momentum_and_change_functions_use_only_explicit_series() -> None:
    close = [10.0, 11.0, 12.0, 11.0, 13.0]

    changes = percentage_change(close, 2)
    assert changes[:2] == [None, None]
    assert changes[2:] == pytest.approx([0.2, 0.0, 1 / 12])
    volumes = volume_change([100, 120, 90], 1)
    assert volumes[0] is None
    assert volumes[1:] == pytest.approx([0.2, -0.25])
    assert rsi([1, 2, 3, 4, 5], 3) == [None, None, None, 100.0, 100.0]


def test_volatility_and_channel_functions_return_aligned_components() -> None:
    high = [11, 12, 13, 14, 15, 16]
    low = [9, 10, 11, 12, 13, 14]
    close = [10, 11, 12, 13, 14, 15]

    assert true_range(high, low, close) == [2.0] * 6
    assert atr(high, low, close, 3) == [None, None, 2.0, 2.0, 2.0, 2.0]
    assert set(bollinger(close, 3)) == {"middle", "upper", "lower"}
    assert set(keltner(high, low, close, 3, 3, 2)) == {"middle", "upper", "lower"}
    assert donchian(high, low, 3)["middle"][-1] == 14.0
    oscillator = stochastic(high, low, close, 3, 2)
    assert len(oscillator["k"]) == len(close)
    assert len(oscillator["d"]) == len(close)


def test_advanced_volatility_functions_match_fixed_vectors_without_lookahead() -> None:
    high = [11, 12, 14, 13, 16, 17, 18, 17, 20, 22, 21, 23, 25, 24, 26, 28, 27, 30, 31, 29]
    low = [9, 10, 11, 10, 12, 13, 14, 12, 15, 17, 16, 18, 20, 19, 21, 23, 22, 24, 25, 23]
    close = [10, 11, 12, 11, 15, 16, 15, 13, 19, 21, 18, 22, 24, 21, 25, 27, 24, 29, 28, 25]

    chaikin = chaikin_volatility(high, low, 3, 2)
    twiggs_public_variant = atr_percent(high, low, close, 3)
    rvi = relative_volatility_index(close, 3, 4)

    assert chaikin[:4] == [None] * 4
    assert chaikin[4:] == pytest.approx(
        [42.8571428571, 37.5, 15.0, 20.4545454545, 22.8260869565, 9.9056603774, 4.6460176991, 2.2532188841, 1.1099365751, 0.5508919203, 0.2744380554, 0.136968432, 0.0684217386, 10.0387546408, 15.0205124866, 6.8266569987],
    )
    assert twiggs_public_variant[:2] == [None] * 2
    assert twiggs_public_variant[2:] == pytest.approx(
        [19.4444444444, 23.2323232323, 22.4691358025, 22.3765432099, 24.8010973937, 31.8982800464, 26.8307944071, 24.1201617059, 28.0193850305, 22.8590585015, 20.9138690842, 23.8708843816, 20.0343619204, 18.5397295805, 20.8492416298, 18.3995815889, 19.8473301447, 22.8193398414],
    )
    assert rvi[:5] == [None] * 5
    assert rvi[5:] == pytest.approx(
        [90.8426399246, 80.957838842, 58.5017244393, 76.1460425343, 86.5427056683, 71.3333089833, 78.2718197486, 85.2552869985, 70.2112842752, 77.4435481229, 84.7083401586, 69.7351914253, 78.1998994222, 56.1759187526, 43.3638736591],
    )
    assert all(0 <= value <= 100 for value in rvi if value is not None)
    assert chaikin_volatility([*high, 500], [*low, 0], 3, 2)[: len(high)] == chaikin


def test_volume_profile_allocates_only_explicit_volume_with_poc_and_value_area() -> None:
    high = [11, 20, 25, 30]
    low = [9, 15, 20, 22]
    close = [10, 18, 23, 25]
    volume = [100, 200, 300, 400]

    profile = volume_profile(high, low, close, volume, bins=4, value_area_percent=70)

    assert profile.algorithm_version == "volume_profile_typical_price_v1"
    assert profile.total_volume == 1_000
    assert profile.poc_bucket_index == 3
    assert profile.value_area_volume == 700
    assert [item.volume for item in profile.buckets] == [100, 200, 300, 400]
    assert [item.share for item in profile.buckets] == [10, 20, 30, 40]
    assert [item.is_poc for item in profile.buckets] == [False, False, False, True]
    assert [item.is_value_area for item in profile.buckets] == [False, False, True, True]
    assert [(item.bucket_low, item.bucket_high) for item in profile.buckets] == pytest.approx(
        [(9, 14.25), (14.25, 19.5), (19.5, 24.75), (24.75, 30)],
    )
    assert volume_profile(high, low, close, volume, bins=4, range_start=1, range_end=2).total_volume == 500
    assert volume_profile([*high, 100], [*low, 1], [*close, 2], [*volume, 999], bins=4, range_end=3) == profile


def test_volume_profile_rejects_missing_or_empty_volume_without_amount_fallback() -> None:
    with pytest.raises(StrategyFunctionError) as caught:
        volume_profile([2, 3], [1, 2], [1.5, 2.5], [100, None])
    assert caught.value.code == "MISSING_FIELD"
    with pytest.raises(StrategyFunctionError) as caught:
        volume_profile([2, 3], [1, 2], [1.5, 2.5], [0, 0])
    assert caught.value.code == "NO_VOLUME"
    with pytest.raises(StrategyFunctionError) as caught:
        volume_profile([2, 3], [1, 2], [1.5, 2.5], [1, 1], range_start=1, range_end=2)
    assert caught.value.code == "INVALID_PARAMETER"


def test_volume_profile_handles_a_large_visible_window_with_bounded_bins() -> None:
    count = 5_000
    low = [100.0 + index * 0.01 for index in range(count)]
    high = [value + 1.0 for value in low]
    close = [value + 0.5 for value in low]
    volume = [float(100 + index % 17) for index in range(count)]

    started = perf_counter()
    profile = volume_profile(high, low, close, volume, bins=200)
    elapsed = perf_counter() - started

    assert len(profile.buckets) == 200
    assert sum(item.volume for item in profile.buckets) == pytest.approx(sum(volume))
    assert elapsed < 2.0


def test_macd_and_cross_conditions_have_stable_structured_outputs() -> None:
    close = [float(value) for value in range(1, 50)]
    result = macd(close)

    assert set(result) == {"macd", "signal", "histogram"}
    assert all(len(values) == len(close) for values in result.values())
    assert crossover([1, 2, 4], [2, 2, 3]) == [None, False, True]
    assert crossunder([3, 2, 0], [2, 2, 1]) == [None, False, True]


def test_up_down_count_reports_unavailable_inputs_instead_of_silently_matching() -> None:
    result = up_down_count([10, 11, None, 10, 10, 12], 2)

    assert result[0].unavailable_reason == "INSUFFICIENT_DATA"
    assert result[2].unavailable_reason == "MISSING_OR_NAN_BAR"
    assert result[-1].to_dict() == {
        "up": 1,
        "down": 0,
        "flat": 1,
        "available": True,
        "unavailable_reason": None,
    }


def test_market_cap_has_explicit_units_and_missing_value_semantics() -> None:
    assert market_cap(20_000_000_000, "gte", 200, "hundred_million_yuan") is True
    assert market_cap(None, "gte", 200, "hundred_million_yuan") is None
    with pytest.raises(StrategyFunctionError) as caught:
        market_cap(1, "greater", 1)
    assert caught.value.code == "INVALID_PARAMETER"


def test_every_canonical_registry_function_has_an_executable_implementation() -> None:
    registry = build_builtin_strategy_function_registry()
    canonical_ids = {item.function_id for item in registry.list() if "." in item.function_id}

    assert canonical_ids == set(STRATEGY_FUNCTION_IMPLEMENTATIONS)
    assert execute_strategy_function("technical.sma", [1, 2, 3], 2) == [None, 1.5, 2.5]
    with pytest.raises(StrategyFunctionError) as caught:
        execute_strategy_function("unknown.function", [])
    assert caught.value.code == "FUNCTION_NOT_FOUND"


def test_invalid_windows_and_mismatched_series_return_stable_errors() -> None:
    with pytest.raises(StrategyFunctionError) as caught:
        sma([1, 2], 0)
    assert caught.value.code == "INVALID_PARAMETER"
    with pytest.raises(StrategyFunctionError) as caught:
        true_range([1], [0, 1], [0])
    assert caught.value.code == "SERIES_LENGTH"
    with pytest.raises(StrategyFunctionError) as caught:
        relative_volatility_index([1, 2, 3], 0, 14)
    assert caught.value.code == "INVALID_PARAMETER"
