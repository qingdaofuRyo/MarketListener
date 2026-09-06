from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import market_monitor.strategy_execution as execution
from market_monitor.strategy_execution import (
    ExecutionPlatform,
    InMemoryExecutionAudit,
    InMemoryExecutionStore,
    RiskContext,
    StrategyExecutionError,
    StrategyExecutionGateway,
    capability_context_for_resource,
    capability_context_for_strategy,
    create_order_intent,
)
from market_monitor.strategy_resources import validate_resource_definition
from market_monitor.web_app import create_web_app


NOW = "2026-09-03T00:00:00+00:00"


def _call(function_id: str, arguments: list[dict], *, comparator: str | None = None) -> dict:
    result = {"node_type": "condition", "left": {"function_id": function_id, "version": 1, "arguments": arguments}}
    if comparator:
        result.update({"comparator": comparator, "right": {"kind": "literal", "value": 10}})
    return result


def strategy_definition(*, run_mode: str = "backtest", status: str = "active") -> dict:
    ma = _call(
        "technical.sma",
        [{"kind": "series", "field": "close"}, {"kind": "literal", "value": 20}],
        comparator="gt",
    )
    cross = _call(
        "condition.crossover",
        [{"kind": "series", "field": "close"}, {"kind": "series", "field": "open"}],
    )
    return {
        "schema_version": 1,
        "id": "strategy.execution_demo",
        "version": 1,
        "display_name": "执行边界演示",
        "origin": "custom",
        "supported_asset_types": ["STOCK"],
        "status": status,
        "base_timeframe": "1d",
        "universe": {"market_types": ["a_share"]},
        "parameters": {},
        "entry_rules": ma,
        "exit_rules": cross,
        "position_sizing": {"kind": "equity_percent", "value": 20},
        "stop_loss": {"enabled": True, "kind": "percent", "value": 5},
        "take_profit": {"enabled": True, "kind": "percent", "value": 10},
        "pyramiding": {"enabled": False, "max_entries": 1},
        "reentry": {"enabled": False, "cooldown_bars": 0},
        "risk": {"max_position_percent": 30, "max_drawdown_percent": 20},
        "execution": {"run_mode": run_mode, "signal_timing": "bar_close", "fill_price": "next_open"},
        "backtest": {"initial_cash": 100000, "commission_rate": 0.0003, "slippage_rate": 0.0001},
        "created_at": NOW,
        "updated_at": NOW,
    }


def resource(kind: str) -> dict:
    capabilities = {
        "strategy_function": ["market_data_input"],
        "indicator": ["market_data_input", "plot_create"],
    }[kind]
    return {
        "schema_version": 1,
        "resource_kind": kind,
        "id": f"{kind}.demo",
        "version": 1,
        "display_name": kind,
        "origin": "builtin",
        "supported_asset_types": ["STOCK"],
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "dependencies": [],
        "capabilities": capabilities,
        "definition": {},
    }


def intent_values(**overrides) -> dict:
    values = {
        "instrument_id": "CN.SSE.STOCK.600000",
        "side": "buy",
        "position_effect": "open",
        "order_type": "market",
        "quantity": 100,
        "signal_time": "2026-09-03T01:00:00+00:00",
        "run_mode": "backtest",
        "intent_id": "intent-001",
    }
    values.update(overrides)
    return values


def risk_context(**overrides) -> RiskContext:
    values = {"account_equity": 100000.0, "reference_price": 10.0}
    values.update(overrides)
    return RiskContext(**values)


def test_only_trusted_desktop_strategy_context_can_create_order_intent() -> None:
    definition = strategy_definition()
    for kind in ("strategy_function", "indicator"):
        context = capability_context_for_resource(validate_resource_definition(resource(kind)))
        with pytest.raises(StrategyExecutionError) as caught:
            create_order_intent(context, definition, **intent_values())
        assert caught.value.code == "PERMISSION_DENIED"

    android = capability_context_for_strategy(definition, platform=ExecutionPlatform.ANDROID)
    with pytest.raises(StrategyExecutionError) as caught:
        create_order_intent(android, definition, **intent_values())
    assert caught.value.code == "PLATFORM_DENIED"

    desktop = capability_context_for_strategy(definition)
    assert create_order_intent(desktop, definition, **intent_values()).strategy_id == definition["id"]


def test_context_version_definition_mutation_and_disabled_strategy_are_rejected() -> None:
    definition = strategy_definition()
    context = capability_context_for_strategy(definition)
    changed = deepcopy(definition)
    changed["risk"]["max_position_percent"] = 40
    with pytest.raises(StrategyExecutionError) as caught:
        create_order_intent(context, changed, **intent_values())
    assert caught.value.code == "CONTEXT_MISMATCH"

    disabled = strategy_definition(status="disabled")
    disabled_context = capability_context_for_strategy(disabled)
    with pytest.raises(StrategyExecutionError) as caught:
        create_order_intent(disabled_context, disabled, **intent_values())
    assert caught.value.code == "STRATEGY_DISABLED"


def test_risk_port_cannot_be_bypassed_and_rejection_never_reaches_adapter() -> None:
    definition = strategy_definition()
    context = capability_context_for_strategy(definition)
    intent = create_order_intent(context, definition, **intent_values())
    adapter = execution._IsolatedSimulationAdapter(execution.StrategyRunMode.BACKTEST, seal=object())
    with pytest.raises(StrategyExecutionError) as caught:
        adapter.submit(intent, None)  # type: ignore[arg-type]
    assert caught.value.code == "RISK_BYPASS_DENIED"

    gateway = StrategyExecutionGateway()
    outcome = gateway.dispatch(definition, risk_context(), **intent_values(quantity=4000))
    assert outcome.risk_decision == "rejected"
    assert outcome.execution_status == "rejected"
    assert outcome.adapter_id is None

    close_outcome = gateway.dispatch(
        definition,
        risk_context(current_position_quantity=100, drawdown_percent=25),
        **intent_values(
            intent_id="risk-close",
            side="sell",
            position_effect="close",
            quantity=100,
        ),
    )
    assert close_outcome.risk_decision == "accepted"


def test_run_modes_are_isolated_and_live_is_always_disabled() -> None:
    backtest = strategy_definition()
    gateway = StrategyExecutionGateway()
    with pytest.raises(StrategyExecutionError) as caught:
        gateway.dispatch(backtest, risk_context(), **intent_values(run_mode="paper"))
    assert caught.value.code == "RUN_MODE_MISMATCH"

    paper = strategy_definition(run_mode="paper")
    paper_outcome = gateway.dispatch(paper, risk_context(), **intent_values(run_mode="paper", intent_id="paper-1"))
    assert paper_outcome.run_mode == "paper"
    assert paper_outcome.adapter_id == "paper-simulation-v1"
    assert "Order API" in paper_outcome.reason

    live = strategy_definition(run_mode="live")
    with pytest.raises(StrategyExecutionError) as caught:
        gateway.dispatch(live, risk_context(), **intent_values(run_mode="live", intent_id="live-1"))
    assert caught.value.code == "LIVE_DISABLED"
    assert caught.value.status_code == 409

    capabilities = gateway.capabilities()
    live_capability = next(item for item in capabilities["modes"] if item["runMode"] == "live")
    assert live_capability == {
        "runMode": "live",
        "enabled": False,
        "executionStatus": "disabled",
        "adapterId": None,
        "reason": "实盘执行适配器尚未配置并验收",
    }


def test_intent_id_is_idempotent_and_conflicting_payload_is_rejected() -> None:
    store = InMemoryExecutionStore()
    audit = InMemoryExecutionAudit()
    gateway = StrategyExecutionGateway(store=store, audit=audit)
    definition = strategy_definition()

    first = gateway.dispatch(definition, risk_context(), **intent_values())
    replay = gateway.dispatch(definition, risk_context(), **intent_values())
    assert first.execution_status == "accepted"
    assert replay.idempotent is True
    assert len(audit.events) == 1

    with pytest.raises(StrategyExecutionError) as caught:
        gateway.dispatch(definition, risk_context(), **intent_values(quantity=101))
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


def test_audit_event_is_allowlisted_and_contains_no_credentials() -> None:
    audit = InMemoryExecutionAudit()
    gateway = StrategyExecutionGateway(audit=audit)
    gateway.dispatch(strategy_definition(), risk_context(), **intent_values())

    assert set(audit.events[0]) == {
        "schemaVersion",
        "event",
        "auditedAt",
        "intentId",
        "strategyId",
        "strategyVersion",
        "instrumentId",
        "capability",
        "runMode",
        "riskDecision",
        "executionStatus",
        "adapterId",
        "reason",
    }
    serialized = json.dumps(audit.events[0]).casefold()
    assert all(term not in serialized for term in ("password", "token", "credential", "secret", "api_key"))


def test_order_intent_api_uses_saved_exact_version_and_rejects_forged_authority(tmp_path) -> None:
    data_root = tmp_path / "data"
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    definition = strategy_definition()
    assert client.post("/api/strategy/definition-resources", json=definition).status_code == 201

    capability = client.get("/api/strategy/execution/capabilities")
    assert capability.status_code == 200
    assert capability.json()["resourceKinds"] == {"strategy_function": False, "indicator": False, "strategy": True}

    body = {
        "strategyId": definition["id"],
        "strategyVersion": 1,
        "intentId": "api-intent-1",
        "runMode": "backtest",
        "instrumentId": "CN.SSE.STOCK.600000",
        "side": "buy",
        "positionEffect": "open",
        "orderType": "market",
        "quantity": 100,
        "signalTime": "2026-09-03T01:00:00+00:00",
        "accountEquity": 100000,
        "referencePrice": 10,
    }
    accepted = client.post("/api/strategy/order-intents", json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["riskDecision"] == "accepted"
    assert accepted.json()["executionStatus"] == "accepted"
    assert accepted.json()["adapterId"] == "backtest-simulation-v1"
    assert client.post("/api/strategy/order-intents", json=body).json()["idempotent"] is True

    forged = client.post(
        "/api/strategy/order-intents",
        json={**body, "intentId": "forged-1", "resourceType": "strategy"},
    )
    assert forged.status_code == 422
    assert client.post(
        "/api/strategy/order-intents",
        json={**body, "intentId": "live-1", "runMode": "live"},
    ).status_code == 409
    assert client.post(
        "/api/strategy/order-intents",
        json={**body, "strategyVersion": 2, "intentId": "missing-version"},
    ).status_code == 404

    audit_files = list((data_root / "strategies" / "execution" / "audit").glob("*.jsonl"))
    assert len(audit_files) == 1
    audit_text = audit_files[0].read_text(encoding="utf-8")
    assert "api-intent-1" in audit_text
    assert "accountEquity" not in audit_text


def test_order_intent_api_returns_explicit_risk_rejection(tmp_path) -> None:
    data_root = tmp_path / "data"
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    definition = strategy_definition(run_mode="paper")
    assert client.post("/api/strategy/definition-resources", json=definition).status_code == 201
    rejected = client.post(
        "/api/strategy/order-intents",
        json={
            "strategyId": definition["id"],
            "strategyVersion": 1,
            "intentId": "risk-rejected",
            "runMode": "paper",
            "instrumentId": "CN.SSE.STOCK.600000",
            "side": "buy",
            "positionEffect": "open",
            "quantity": 4000,
            "signalTime": "2026-09-03T01:00:00+00:00",
            "accountEquity": 100000,
            "referencePrice": 10,
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["runMode"] == "paper"
    assert rejected.json()["riskDecision"] == "rejected"
    assert rejected.json()["executionStatus"] == "rejected"
    assert rejected.json()["adapterId"] is None
