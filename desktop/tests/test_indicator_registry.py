from __future__ import annotations

from copy import deepcopy

import pytest

from market_monitor.indicator_registry import (
    IndicatorResourceError,
    build_indicator_registry,
    build_builtin_indicator_registry,
    custom_indicator_definition,
)
from market_monitor.strategy_function_registry import build_builtin_strategy_function_registry


def _custom_ma_document(*, indicator_id: str = "indicator.user.ma_demo", version: int = 1) -> dict[str, object]:
    source = build_builtin_indicator_registry().resolve("indicator.ma", 1)
    return {
        "schema_version": 1,
        "resource_kind": "indicator",
        "id": indicator_id,
        "version": version,
        "display_name": "自定义两日均线",
        "origin": "custom",
        "supported_asset_types": ["STOCK", "ETF"],
        "status": "active",
        "created_at": "2026-09-05T08:00:00+08:00",
        "updated_at": "2026-09-05T08:00:00+08:00",
        "dependencies": [
            {"resource_kind": "strategy_function", "id": function_id, "version": function_version}
            for function_id, function_version in source.dependencies
        ],
        "capabilities": ["market_data_input", "plot_create"],
        "definition": {
            "english_name": "Custom Moving Average",
            "category": source.category,
            "category_label": source.category_label,
            "description": "仅调整默认值的安全自定义均线。",
            "placement": source.placement,
            "parameters": [{**source.parameters[0], "default": 2}],
            "plots": [dict(item) for item in source.plots],
            "calculation_id": "indicator.ma",
        },
    }


def test_indicator_registry_contains_required_first_phase_catalog() -> None:
    registry = build_builtin_indicator_registry()
    ids = {item.indicator_id for item in registry.list()}

    assert {
        "indicator.ma",
        "indicator.ema",
        "indicator.bollinger",
        "indicator.keltner",
        "indicator.donchian",
        "indicator.rsi",
        "indicator.macd",
        "indicator.stochastic",
        "indicator.atr",
        "indicator.chaikin_volatility",
        "indicator.twiggs_volatility",
        "indicator.rvi",
        "indicator.volume_profile",
        "indicator.vix",
    }.issubset(ids)


def test_every_active_indicator_dependency_resolves_to_an_exact_function_version() -> None:
    indicators = build_builtin_indicator_registry()
    functions = build_builtin_strategy_function_registry()

    for indicator in indicators.list():
        if indicator.status == "disabled":
            continue
        for function_id, version in indicator.dependencies:
            assert functions.resolve(function_id, version).version == version


def test_indicator_permissions_and_placement_are_visual_only() -> None:
    public = build_builtin_indicator_registry().resolve("indicator.macd").to_public_dict()

    assert public["placement"] == "pane"
    assert public["capabilities"] == ["market_data_input", "plot_create"]
    assert "order_intent_create" not in public["capabilities"]
    assert all(item["resourceKind"] == "strategy_function" for item in public["dependencies"])


def test_indicator_filters_and_external_market_limits_are_explicit() -> None:
    registry = build_builtin_indicator_registry()

    assert {item.indicator_id for item in registry.list(category="动量", asset_type="stock")} == {
        "indicator.macd",
        "indicator.rsi",
        "indicator.stochastic",
    }
    assert [item.indicator_id for item in registry.list(query="VIX")] == ["indicator.vix"]
    vix = registry.resolve("indicator.vix")
    assert vix.status == "active"
    assert vix.required_fields == ()
    assert vix.warmup_bars == "0；不从当前标的 OHLC 推导"
    assert "null" in vix.limitations[1]


def test_advanced_volatility_catalog_publishes_formula_variant_and_warmup() -> None:
    registry = build_builtin_indicator_registry()
    chaikin = registry.resolve("indicator.chaikin_volatility").to_public_dict()
    twiggs = registry.resolve("indicator.twiggs_volatility").to_public_dict()
    rvi = registry.resolve("indicator.rvi").to_public_dict()

    assert chaikin["status"] == "active"
    assert chaikin["requiredFields"] == ["high", "low"]
    assert chaikin["warmupBars"] == "emaLookback + changeLookback"
    assert chaikin["formulaSource"]
    assert "专有实现" in twiggs["limitations"][0]
    assert rvi["requiredFields"] == ["close"]
    assert rvi["warmupBars"] == "stddevLookback + smoothLookback − 1"


def test_fibonacci_is_published_as_a_drawing_tool_not_an_indicator_calculation() -> None:
    definition = build_builtin_indicator_registry().resolve("drawing.fibonacci_retracement").to_public_dict()

    assert definition["resourceKind"] == "drawing_tool"
    assert definition["dependencies"] == []
    assert "双锚点" in definition["description"]
    assert "Strategy Function" in definition["limitations"][0]


def test_custom_indicator_is_a_safe_versioned_template_copy() -> None:
    document = _custom_ma_document()
    definition = build_indicator_registry([document]).resolve("indicator.user.ma_demo", 1)

    assert definition.calculation_id == "indicator.ma"
    assert definition.parameters[0]["default"] == 2
    assert definition.required_fields == ()
    assert definition.dependencies == (("technical.sma", 1),)
    assert definition.supported_asset_types == ("STOCK", "ETF")

    changed = deepcopy(document)
    changed["definition"]["parameters"][0]["maximum"] = 999  # type: ignore[index]
    with pytest.raises(IndicatorResourceError, match="parameter types and bounds"):
        custom_indicator_definition(changed)
