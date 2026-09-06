from __future__ import annotations

import pytest

from market_monitor.strategy_function_registry import (
    FunctionInput,
    StrategyFunctionDefinition,
    StrategyFunctionRegistry,
    StrategyFunctionRegistryError,
    build_builtin_strategy_function_registry,
)


def definition(function_id: str, version: int, *, deprecated: bool = False) -> StrategyFunctionDefinition:
    return StrategyFunctionDefinition(
        function_id=function_id,
        version=version,
        display_name=f"函数 {version}",
        category="trend",
        category_label="趋势",
        description="纯函数",
        inputs=(FunctionInput("close", "series<number>"),),
        output_type="number",
        supported_asset_types=("STOCK", "ETF"),
        runtime_name="ts_mean",
        deprecated=deprecated,
    )


def test_registry_resolves_exact_version_and_latest_active_version() -> None:
    registry = StrategyFunctionRegistry(
        [definition("technical.sma", 1, deprecated=True), definition("technical.sma", 2)]
    )

    assert registry.resolve("technical.sma").version == 2
    assert registry.resolve("technical.sma", 1).deprecated is True


def test_registry_rejects_duplicate_id_and_version() -> None:
    registry = StrategyFunctionRegistry([definition("technical.sma", 1)])

    with pytest.raises(StrategyFunctionRegistryError) as caught:
        registry.register(definition("technical.sma", 1))

    assert caught.value.code == "DUPLICATE_FUNCTION"


def test_registry_filters_search_category_asset_and_deprecation() -> None:
    registry = StrategyFunctionRegistry(
        [
            definition("technical.sma", 1),
            definition("technical.sma", 2, deprecated=True),
            definition("technical.ema", 1),
        ]
    )

    assert [item.function_id for item in registry.list(query="EMA")] == ["technical.ema"]
    assert len(registry.list(category="趋势", asset_type="stock")) == 2
    assert [item.version for item in registry.list(version=2)] == []
    assert [item.version for item in registry.list(version=2, include_deprecated=True)] == [2]


def test_builtin_registry_preserves_legacy_ids_and_pure_capability() -> None:
    registry = build_builtin_strategy_function_registry()
    item = registry.resolve("gann_rising_rate")
    public = item.to_public_dict({"indicators": ["gann"], "strategies": ["demo"]})

    assert public["versionedId"] == "gann_rising_rate@1"
    assert public["pure"] is True
    assert public["capabilities"] == ["market_data_input"]
    assert public["referencedBy"] == {"indicators": ["gann"], "strategies": ["demo"]}


def test_builtin_atr_versions_coexist_without_rewriting_v1() -> None:
    registry = build_builtin_strategy_function_registry()

    first = registry.resolve("technical.atr", 1)
    second = registry.resolve("technical.atr", 2)

    assert first.runtime_name == "atr"
    assert second.runtime_name == "atr_v2"
    assert first.definition_hash() != second.definition_hash()
    assert registry.resolve("technical.atr").version == 2


def test_registry_returns_stable_order_and_unknown_error() -> None:
    registry = build_builtin_strategy_function_registry()

    assert list(registry.list()) == sorted(
        registry.list(), key=lambda item: (item.category, item.function_id, item.version)
    )
    with pytest.raises(StrategyFunctionRegistryError) as caught:
        registry.resolve("missing")
    assert caught.value.code == "FUNCTION_NOT_FOUND"
