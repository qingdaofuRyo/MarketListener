from __future__ import annotations

import pytest

from market_monitor.external_market import ExternalMarketSeries, VIX_INSTRUMENT_ID
from market_monitor.indicator_calculation import (
    calculate_indicator,
    calculate_indicator_instance,
    calculate_legacy_indicator_series,
)
from market_monitor.indicator_registry import build_builtin_indicator_registry
from market_monitor.strategy_functions import atr_percent, chaikin_volatility, relative_volatility_index, sma


def bars(count: int = 40) -> list[dict[str, float]]:
    return [
        {
            "open": 99.5 + index,
            "high": 101.0 + index,
            "low": 98.0 + index,
            "close": 100.0 + index,
            "volume": 1_000.0 + index,
        }
        for index in range(count)
    ]


def test_ma_indicator_is_the_shared_strategy_function_result() -> None:
    registry = build_builtin_indicator_registry()
    values = bars(8)

    result = calculate_indicator(registry.resolve("indicator.ma", 1), values, {"lookback": 3})

    assert result["ma"] == sma([value["close"] for value in values], 3)


def test_indicator_instance_keeps_definition_version_style_and_alignment() -> None:
    registry = build_builtin_indicator_registry()
    result = calculate_indicator_instance(
        registry,
        bars(),
        {
            "instanceId": "ma-one",
            "definitionId": "indicator.ma",
            "version": 1,
            "parameters": {"lookback": 5},
            "placement": "overlay",
            "visible": True,
            "style": {"color": "#ff0000", "lineWidth": 2},
        },
        asset_type="STOCK",
    )

    assert result["status"] == "ready"
    assert result["instanceId"] == "ma-one"
    assert result["definitionId"] == "indicator.ma"
    assert result["version"] == 1
    assert result["placement"] == "overlay"
    assert result["style"] == {"color": "#ff0000", "lineWidth": 2}
    assert len(result["series"]["ma"]) == len(bars())
    assert result["series"]["ma"][:4] == [None] * 4


def test_unavailable_instance_is_isolated_from_ready_instance() -> None:
    registry = build_builtin_indicator_registry()
    requests = [
        {
            "instanceId": "ready",
            "definitionId": "indicator.rsi",
            "version": 1,
            "parameters": {"lookback": 5},
            "placement": "pane",
        },
        {
            "instanceId": "missing",
            "definitionId": "indicator.not_found",
            "version": 1,
            "parameters": {},
            "placement": "pane",
        },
        {
            "instanceId": "rvi",
            "definitionId": "indicator.rvi",
            "version": 1,
            "parameters": {"stddevLookback": 3, "smoothLookback": 4},
            "placement": "pane",
        },
    ]

    results = [
        calculate_indicator_instance(registry, bars(), request, asset_type="STOCK")
        for request in requests
    ]

    assert results[0]["status"] == "ready"
    assert len(results[0]["series"]["rsi"]) == len(bars())
    assert results[1]["status"] == "unavailable"
    assert results[1]["unavailableCode"] == "DEFINITION_NOT_FOUND"
    assert results[2]["status"] == "ready"
    assert len(results[2]["series"]["rvi"]) == len(bars())


def test_advanced_volatility_indicators_use_published_functions_and_field_contracts() -> None:
    registry = build_builtin_indicator_registry()
    values = bars(30)
    high = [item["high"] for item in values]
    low = [item["low"] for item in values]
    close = [item["close"] for item in values]
    requests = [
        ("indicator.chaikin_volatility", {"emaLookback": 3, "changeLookback": 2}, "value", chaikin_volatility(high, low, 3, 2)),
        ("indicator.twiggs_volatility", {"lookback": 3}, "value", atr_percent(high, low, close, 3)),
        ("indicator.rvi", {"stddevLookback": 3, "smoothLookback": 4}, "rvi", relative_volatility_index(close, 3, 4)),
    ]

    for definition_id, parameters, plot_id, expected in requests:
        result = calculate_indicator_instance(
            registry,
            values,
            {
                "instanceId": definition_id,
                "definitionId": definition_id,
                "version": 1,
                "parameters": parameters,
                "placement": "pane",
            },
            asset_type="STOCK",
        )
        assert result["status"] == "ready"
        assert result["series"][plot_id] == expected

    missing_high = [{"low": item["low"], "close": item["close"]} for item in values]
    missing = calculate_indicator_instance(
        registry,
        missing_high,
        {"instanceId": "missing-high", "definitionId": "indicator.chaikin_volatility", "version": 1, "parameters": {}, "placement": "pane"},
        asset_type="STOCK",
    )
    assert missing["unavailableCode"] == "MISSING_FIELD"
    invalid = calculate_indicator_instance(
        registry,
        values,
        {"instanceId": "bad-window", "definitionId": "indicator.chaikin_volatility", "version": 1, "parameters": {"emaLookback": 1}, "placement": "pane"},
        asset_type="STOCK",
    )
    assert invalid["unavailableCode"] == "INVALID_PARAMETER"
    unsupported = calculate_indicator_instance(
        registry,
        values,
        {"instanceId": "unsupported", "definitionId": "indicator.rvi", "version": 1, "parameters": {}, "placement": "pane"},
        asset_type="CRYPTO",
    )
    assert unsupported["unavailableCode"] == "UNSUPPORTED_ASSET"


def test_volume_profile_is_isolated_and_returns_traceable_visible_range_metadata() -> None:
    registry = build_builtin_indicator_registry()
    values = [
        {"high": 11, "low": 9, "close": 10, "volume": 100, "barOpenTime": "2026-01-01T09:30:00+08:00"},
        {"high": 20, "low": 15, "close": 18, "volume": 200, "barOpenTime": "2026-01-02T09:30:00+08:00"},
        {"high": 25, "low": 20, "close": 23, "volume": 300, "barOpenTime": "2026-01-03T09:30:00+08:00"},
        {"high": 30, "low": 22, "close": 25, "volume": 400, "barOpenTime": "2026-01-04T09:30:00+08:00"},
    ]
    request = {
        "instanceId": "profile-one",
        "definitionId": "indicator.volume_profile",
        "version": 1,
        "parameters": {"bins": 4, "valueAreaPercent": 70},
        "placement": "overlay",
        "rangeStart": 1,
        "rangeEnd": 2,
    }

    result = calculate_indicator_instance(registry, values, request, asset_type="STOCK")

    assert result["status"] == "ready"
    assert result["series"] == {}
    assert result["profile"]["range"] == {
        "startIndex": 1,
        "endIndex": 2,
        "inputBarCount": 2,
        "rangeSemantics": "inclusive_visible_indices_in_requested_window",
        "firstBarAt": "2026-01-02T09:30:00+08:00",
        "lastBarAt": "2026-01-03T09:30:00+08:00",
    }
    assert sum(item["volume"] for item in result["profile"]["buckets"]) == 500
    assert sum(item["share"] for item in result["profile"]["buckets"]) == pytest.approx(100)
    missing_volume = calculate_indicator_instance(
        registry,
        [{**item, "volume": None if index == 2 else item["volume"]} for index, item in enumerate(values)],
        request,
        asset_type="STOCK",
    )
    assert missing_volume["unavailableCode"] == "MISSING_FIELD"


def test_drawing_tool_cannot_be_requested_as_an_indicator_instance() -> None:
    result = calculate_indicator_instance(
        build_builtin_indicator_registry(),
        bars(),
        {
            "instanceId": "fib-as-indicator",
            "definitionId": "drawing.fibonacci_retracement",
            "version": 1,
            "parameters": {},
            "placement": "overlay",
        },
        asset_type="STOCK",
    )

    assert result["status"] == "unavailable"
    assert result["unavailableCode"] == "DRAWING_TOOL"


def test_vix_instance_only_uses_verified_external_series_and_exact_dates() -> None:
    registry = build_builtin_indicator_registry()
    request = {
        "instanceId": "vix-one",
        "definitionId": "indicator.vix",
        "version": 1,
        "parameters": {},
        "placement": "pane",
    }
    values = [
        {"tradingDate": "2026-08-06", "close": 100.0},
        {"tradingDate": "2026-08-07", "close": 99999.0},
        {"tradingDate": "2026-08-08", "close": 1.0},
    ]
    source = ExternalMarketSeries(
        VIX_INSTRUMENT_ID,
        "fixture: verified VIX standard series",
        "2026-08-08",
        (("2026-08-06", 19.25), ("2026-08-08", 17.5)),
        "ready",
    )

    missing = calculate_indicator_instance(registry, values, request, asset_type="STOCK")
    result = calculate_indicator_instance(
        registry,
        values,
        request,
        asset_type="FUTURE",
        external_series=source,
    )

    assert missing["status"] == "unavailable"
    assert missing["unavailableCode"] == "MISSING_DATASOURCE"
    assert result["status"] == "ready"
    assert result["series"]["vix"] == [19.25, None, 17.5]
    assert result["external"]["externalInstrumentId"] == VIX_INSTRUMENT_ID
    assert result["external"]["asOfDate"] == "2026-08-08"
    assert result["external"]["coverage"]["matchedBarCount"] == 2


def test_legacy_flat_adapter_uses_registered_definitions() -> None:
    registry = build_builtin_indicator_registry()
    result = calculate_legacy_indicator_series(
        registry,
        bars(),
        [
            {"id": "ma", "lookback": 3},
            {"id": "bollinger", "lookback": 5, "multiplier": 2},
            {"id": "atr", "atrLookback": 5, "centerLookback": 6, "multiplier": 2},
        ],
    )

    assert set(result) == {
        "ma",
        "bollingerUpper",
        "bollingerMiddle",
        "bollingerLower",
        "atrUpper",
        "atrMiddle",
        "atrLower",
    }
    assert result["ma"] == sma([value["close"] for value in bars()], 3)
