from __future__ import annotations

from copy import deepcopy

import pytest

from market_monitor.strategy_resources import (
    ResourceKind,
    RuntimeCapability,
    StrategyResourceError,
    adapt_legacy_strategy_definition,
    require_capability,
    validate_dependency_graph,
    validate_resource_definition,
)


NOW = "2026-09-02T10:00:00+08:00"


def resource(kind: str, resource_id: str, *, dependencies: list[dict] | None = None) -> dict:
    capabilities = {
        "strategy_function": ["market_data_input"],
        "indicator": ["market_data_input", "plot_create"],
        "strategy": ["market_data_input", "account_read", "position_read", "order_intent_create"],
    }[kind]
    return {
        "schema_version": 1,
        "resource_kind": kind,
        "id": resource_id,
        "version": 1,
        "display_name": resource_id,
        "origin": "builtin",
        "supported_asset_types": ["STOCK", "ETF", "FUTURE", "INDEX"],
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "dependencies": dependencies or [],
        "capabilities": capabilities,
        "definition": {},
    }


def function_ref(resource_id: str) -> dict:
    return {"resource_kind": "strategy_function", "id": resource_id, "version": 1}


def test_three_layer_resources_share_functions_without_strategy_to_indicator_dependency() -> None:
    sma = resource("strategy_function", "technical.sma")
    indicator = resource("indicator", "indicator.ma", dependencies=[function_ref("technical.sma")])
    strategy = resource("strategy", "strategy.ma_crossover", dependencies=[function_ref("technical.sma")])

    resources = validate_dependency_graph([sma, indicator, strategy])

    assert [item.resource_kind for item in resources] == [
        ResourceKind.STRATEGY_FUNCTION,
        ResourceKind.INDICATOR,
        ResourceKind.STRATEGY,
    ]


def test_indicator_and_strategy_cannot_depend_on_visual_or_strategy_resources() -> None:
    invalid = resource(
        "strategy",
        "strategy.invalid",
        dependencies=[{"resource_kind": "indicator", "id": "indicator.ma", "version": 1}],
    )

    with pytest.raises(StrategyResourceError, match="strategy_function"):
        validate_resource_definition(invalid)


def test_permission_matrix_allows_only_strategy_to_create_order_intent() -> None:
    function = validate_resource_definition(resource("strategy_function", "technical.sma"))
    indicator = validate_resource_definition(resource("indicator", "indicator.ma"))
    strategy = validate_resource_definition(resource("strategy", "strategy.ma"))

    for denied in (function, indicator):
        with pytest.raises(StrategyResourceError) as caught:
            require_capability(denied, RuntimeCapability.ORDER_INTENT_CREATE)
        assert caught.value.code == "PERMISSION_DENIED"
    require_capability(strategy, RuntimeCapability.ORDER_INTENT_CREATE)


def test_direct_order_api_capability_is_not_part_of_any_resource_contract() -> None:
    invalid = resource("strategy", "strategy.invalid")
    invalid["capabilities"].append("order_api_call")

    with pytest.raises(StrategyResourceError) as caught:
        validate_resource_definition(invalid)
    assert caught.value.code == "INVALID_RESOURCE"


def test_dependency_graph_requires_exact_versions_and_unique_keys() -> None:
    indicator = resource("indicator", "indicator.ma", dependencies=[function_ref("technical.sma")])
    with pytest.raises(StrategyResourceError) as caught:
        validate_dependency_graph([indicator])
    assert caught.value.code == "MISSING_DEPENDENCY"

    sma = resource("strategy_function", "technical.sma")
    with pytest.raises(StrategyResourceError) as caught:
        validate_dependency_graph([sma, deepcopy(sma)])
    assert caught.value.code == "DUPLICATE_RESOURCE"


def test_untrusted_origins_default_to_disabled() -> None:
    document = resource("indicator", "indicator.community")
    document["origin"] = "community"
    with pytest.raises(StrategyResourceError) as caught:
        validate_resource_definition(document)
    assert caught.value.code == "UNTRUSTED_RESOURCE"
    document["status"] = "disabled"
    validate_resource_definition(document)


def test_legacy_strategy_adapter_preserves_the_original_document() -> None:
    legacy = {"strategy_id": "legacy_ma", "strategy_version": "2", "script_kind": "dsl_v1", "inputs": ["close"]}

    adapted = adapt_legacy_strategy_definition(legacy, display_name="旧 MA", created_at=NOW, updated_at=NOW)
    result = validate_resource_definition(adapted)

    assert result.resource_id == "legacy_ma"
    assert result.version == 2
    assert result.definition["legacy_runtime"] == "dsl_v1"
    assert result.definition["document"] == legacy
