from __future__ import annotations

import pytest

from market_monitor.strategy_registry import StrategyRegistry, registration_from_document


NOW = "2026-09-03T00:00:00+00:00"


def registration(strategy_id: str = "demo", **metadata: str):
    document = {
        "strategy_id": strategy_id,
        "strategy_version": 2,
        "description": "趋势策略",
        "script_kind": "formula_v1",
        "period": "1d",
        "universe": {"market": "CN", "asset_type": "STOCK"},
        "parameters": {"lookback": {"default": 20}},
    }
    return registration_from_document(document, metadata, created_at=NOW, updated_at=NOW)


def test_registration_exposes_version_origin_status_and_live_gate() -> None:
    public = registration(displayName="演示策略", origin="builtin", status="active").to_public_dict()

    assert public["versionedId"] == "demo@2"
    assert public["origin"] == "builtin"
    assert public["supportedAssetTypes"] == ["STOCK"]
    assert public["availableRunModes"][-1] == {
        "id": "live",
        "enabled": False,
        "reason": "实盘交易接口尚未配置",
    }


def test_strategy_registry_filters_and_rejects_duplicate_versions() -> None:
    first = registration("first", displayName="趋势一", category="trend")
    second = registration("second", displayName="动量二", category="momentum", status="disabled")
    registry = StrategyRegistry([first, second])

    assert [item.strategy_id for item in registry.list(query="趋势一", asset_type="stock")] == ["first"]
    assert [item.strategy_id for item in registry.list(status="disabled")] == ["second"]
    with pytest.raises(ValueError, match="duplicate strategy"):
        StrategyRegistry([first, first])
