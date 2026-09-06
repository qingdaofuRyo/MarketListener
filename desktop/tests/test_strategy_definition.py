from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from market_monitor.strategy_definition import MAX_RULE_DEPTH, StrategyDefinitionError, validate_strategy_definition
from market_monitor.web_app import create_web_app


NOW = "2026-09-03T00:00:00+00:00"


def call(function_id: str, arguments: list[dict], *, comparator: str | None = None, right: dict | None = None) -> dict:
    result = {"node_type": "condition", "left": {"function_id": function_id, "version": 1, "arguments": arguments}}
    if comparator:
        result.update({"comparator": comparator, "right": right or {"kind": "literal", "value": 0}})
    return result


def document() -> dict:
    ma = call(
        "technical.sma",
        [{"kind": "series", "field": "close"}, {"kind": "parameter", "name": "lookback"}],
        comparator="gt",
        right={"kind": "literal", "value": 10},
    )
    cross = call(
        "condition.crossover",
        [{"kind": "series", "field": "close"}, {"kind": "series", "field": "open"}],
    )
    return {
        "schema_version": 1,
        "id": "strategy.ma_demo",
        "version": 1,
        "display_name": "均线演示",
        "description": "结构化策略",
        "origin": "custom",
        "supported_asset_types": ["STOCK"],
        "status": "active",
        "base_timeframe": "1d",
        "universe": {"market_types": ["a_share"], "exclude_st": True},
        "parameters": {"lookback": {"type": "integer", "default": 20, "minimum": 2, "maximum": 500}},
        "entry_rules": {"node_type": "group", "operator": "AND", "children": [ma, cross]},
        "exit_rules": {"node_type": "group", "operator": "NOT", "children": [cross]},
        "position_sizing": {"kind": "equity_percent", "value": 20},
        "stop_loss": {"enabled": True, "kind": "percent", "value": 5},
        "take_profit": {"enabled": True, "kind": "percent", "value": 10},
        "pyramiding": {"enabled": False, "max_entries": 1},
        "reentry": {"enabled": True, "cooldown_bars": 2},
        "risk": {"max_position_percent": 30, "max_drawdown_percent": 20},
        "execution": {"run_mode": "backtest", "signal_timing": "bar_close", "fill_price": "next_open"},
        "backtest": {"initial_cash": 100000, "commission_rate": 0.0003, "slippage_rate": 0.0001},
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_versioned_rule_ast_validates_and_returns_exact_function_references() -> None:
    result = validate_strategy_definition(document())

    assert result["valid"] is True
    assert result["rule_nodes"] == 5
    assert result["function_references"] == [
        {"resource_kind": "strategy_function", "id": "condition.crossover", "version": 1},
        {"resource_kind": "strategy_function", "id": "technical.sma", "version": 1},
    ]


def test_strategy_definition_accepts_short_direction_and_4h_timeframe() -> None:
    value = document()
    value["direction"] = "short"
    value["base_timeframe"] = "4h"

    result = validate_strategy_definition(value)

    assert result["valid"] is True


def test_scale_out_requires_enabled_take_profit_and_a_partial_ratio() -> None:
    missing_take_profit = document()
    missing_take_profit["take_profit"]["enabled"] = False
    missing_take_profit["scale_out"] = {"enabled": True, "ratio_percent": 40}
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(missing_take_profit)
    assert caught.value.code == "INVALID_DEFINITION"

    full_exit_ratio = document()
    full_exit_ratio["scale_out"] = {"enabled": True, "ratio_percent": 100}
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(full_exit_ratio)
    assert caught.value.code == "INVALID_DEFINITION"


def test_rule_ast_supports_nested_function_results_for_ma_crossover() -> None:
    value = document()
    nested_sma = {
        "kind": "function",
        "call": {
            "function_id": "technical.sma",
            "version": 1,
            "arguments": [{"kind": "series", "field": "close"}, {"kind": "literal", "value": 60}],
        },
    }
    value["entry_rules"]["children"][0]["right"] = nested_sma

    result = validate_strategy_definition(value)

    assert result["valid"] is True
    assert result["function_references"][-1]["id"] == "technical.sma"


def test_rule_ast_rejects_missing_function_version_type_mismatch_and_live_mode() -> None:
    missing = document()
    missing["entry_rules"]["children"][0]["left"]["version"] = 99
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(missing)
    assert caught.value.code == "MISSING_FUNCTION_VERSION"

    wrong_type = document()
    wrong_type["parameters"]["lookback"] = {"type": "string", "default": "20"}
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(wrong_type)
    assert caught.value.code == "TYPE_MISMATCH"

    live = document()
    live["execution"]["run_mode"] = "live"
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(live)
    assert caught.value.code == "LIVE_DISABLED"


def test_rule_ast_rejects_wrong_asset_empty_not_and_excessive_depth() -> None:
    unsupported = document()
    unsupported["supported_asset_types"] = ["FUTURE"]
    unsupported["entry_rules"]["children"][0] = call(
        "market.market_cap",
        [
            {"kind": "literal", "value": 1},
            {"kind": "literal", "value": "gte"},
            {"kind": "literal", "value": 1},
            {"kind": "literal", "value": "yuan"},
        ],
        comparator="eq",
        right={"kind": "literal", "value": 1},
    )
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(unsupported)
    assert caught.value.code == "UNSUPPORTED_ASSET"

    invalid_not = document()
    invalid_not["exit_rules"]["children"].append(deepcopy(invalid_not["exit_rules"]["children"][0]))
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(invalid_not)
    assert caught.value.code == "INVALID_DEFINITION"

    deep = document()
    node = deep["entry_rules"]
    for _ in range(MAX_RULE_DEPTH):
        child = {"node_type": "group", "operator": "AND", "children": [node]}
        node = child
    deep["entry_rules"] = node
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(deep)
    assert caught.value.code == "RULE_TOO_DEEP"


def test_strategy_definition_resource_api_persists_immutable_sequential_versions(tmp_path) -> None:
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    first = document()

    created = client.post("/api/strategy/definition-resources", json=first)
    assert created.status_code == 201, created.text
    assert created.json()["definition"]["id"] == "strategy.ma_demo"
    assert created.json()["validation"]["functionReferences"]
    assert client.post("/api/strategy/definition-resources", json=first).status_code == 409

    skipped = deepcopy(first)
    skipped["version"] = 3
    assert client.post("/api/strategy/definition-resources", json=skipped).status_code == 409

    second = deepcopy(first)
    second["version"] = 2
    second["updated_at"] = "2026-09-03T01:00:00+00:00"
    assert client.post("/api/strategy/definition-resources", json=second).status_code == 201
    latest = client.get("/api/strategy/definition-resources/strategy.ma_demo")
    assert latest.status_code == 200
    assert latest.json()["version"] == 2
    listing = client.get("/api/strategy/definition-resources", params={"assetType": "STOCK"}).json()
    assert listing["total"] == 4
    assert {item["id"] for item in listing["items"]} >= {"strategy.ma_crossover", "strategy.donchian_atr"}

    disabled = client.patch(
        "/api/strategy/definition-resources/strategy.ma_demo/status",
        json={"status": "disabled"},
    )
    assert disabled.status_code == 201
    assert disabled.json()["version"] == 3
    assert disabled.json()["status"] == "disabled"

    copied = client.post(
        "/api/strategy/definition-resources/strategy.ma_demo/copy",
        json={"displayName": "结构化副本"},
    )
    assert copied.status_code == 201
    copied_id = copied.json()["id"]
    assert copied_id.startswith("strategy.user_")
    deleted = client.request(
        "DELETE",
        f"/api/strategy/definition-resources/{copied_id}",
        json={"confirmDisplayName": "结构化副本"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is False
    assert deleted.json()["archived"] is True
    assert deleted.json()["version"] == 2
    versions = client.get(f"/api/strategy/definition-resources/{copied_id}/versions")
    assert [item["version"] for item in versions.json()["items"]] == [2, 1]


def test_legacy_resource_migration_is_explicit_and_can_roll_back(tmp_path) -> None:
    data_root = tmp_path / "data"
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    legacy = document()
    legacy["id"] = "strategy.legacy_migration"
    legacy["display_name"] = "旧版迁移策略"
    resource_dir = data_root / "strategies" / "resources"
    resource_dir.mkdir(parents=True)
    original = resource_dir / "legacy_resource.json"
    original.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = client.post("/api/strategy/definition-resources/migrations/legacy")
    assert migrated.status_code == 201, migrated.text
    migration_id = migrated.json()["migrationId"]
    assert not original.exists()
    assert (resource_dir / "strategy.legacy_migration@1.json").is_file()
    assert client.get("/api/strategy/definition-resources/strategy.legacy_migration/versions").json()["total"] == 1

    rolled_back = client.post(
        f"/api/strategy/definition-resources/migrations/{migration_id}/rollback"
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["status"] == "rolled_back"
    assert original.is_file()
    assert not (resource_dir / "strategy.legacy_migration@1.json").exists()
