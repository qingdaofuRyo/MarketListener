"""Immutable built-in Strategy templates and safe custom-copy creation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from market_monitor.builtin_strategies import build_builtin_strategy_definitions
from market_monitor.contracts import ContractValidationError, validate_contract
from market_monitor.strategy_definition import StrategyDefinitionError, validate_strategy_definition


TEMPLATE_SCHEMA = "strategy-template.schema.json"
_DISCLAIMER = "仅供策略研究与参数演示，不代表收益承诺、投资建议或可直接实盘执行。"


class StrategyTemplateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _blank_definition() -> dict[str, Any]:
    """A syntactically valid non-triggering start point for the visual editor."""

    timestamp = "2026-09-05T00:00:00+00:00"
    close = {"kind": "series", "field": "close"}
    return {
        "schema_version": 1,
        "id": "strategy.blank_placeholder",
        "version": 1,
        "display_name": "空白策略",
        "description": "空白起点：请先配置开平仓规则、标的与风险。默认同序列交叉永不触发。",
        "origin": "builtin",
        "supported_asset_types": ["STOCK", "ETF", "FUTURE"],
        "status": "active",
        "base_timeframe": "1d",
        "universe": {"market_types": ["a_share"]},
        "parameters": {},
        "entry_rules": {
            "node_type": "condition",
            "left": {"function_id": "condition.crossover", "version": 1, "arguments": [close, deepcopy(close)]},
        },
        "exit_rules": {
            "node_type": "condition",
            "left": {"function_id": "condition.crossunder", "version": 1, "arguments": [deepcopy(close), deepcopy(close)]},
        },
        "direction": "long",
        "position_sizing": {"kind": "equity_percent", "value": 10},
        "stop_loss": {"enabled": False, "kind": "percent", "value": 0},
        "take_profit": {"enabled": False, "kind": "percent", "value": 0},
        "pyramiding": {"enabled": False, "max_entries": 1},
        "reentry": {"enabled": False, "cooldown_bars": 0},
        "risk": {"max_position_percent": 10, "max_drawdown_percent": 10},
        "execution": {"run_mode": "backtest", "signal_timing": "bar_close", "fill_price": "next_open"},
        "backtest": {"initial_cash": 100000, "commission_rate": 0.0003, "slippage_rate": 0.0001},
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _sources() -> dict[tuple[str, int], dict[str, Any]]:
    values = {(item["id"], int(item["version"])): item for item in build_builtin_strategy_definitions()}
    values[("strategy.blank_placeholder", 1)] = _blank_definition()
    return values


_TEMPLATE_SPECS = (
    {
        "template_id": "template.ma_crossover",
        "version": 1,
        "display_name": "MA Crossover",
        "description": "快慢均线交叉的趋势跟随起点。",
        "source_strategy_id": "strategy.ma_crossover",
        "source_strategy_version": 1,
        "default_overrides": {"parameters": {"fast": 20, "slow": 60}},
    },
    {
        "template_id": "template.donchian_atr",
        "version": 1,
        "display_name": "Donchian + ATR",
        "description": "通道突破与 ATR 风险控制的趋势跟随起点。",
        "source_strategy_id": "strategy.donchian_atr",
        "source_strategy_version": 1,
        "default_overrides": {"parameters": {"entry_lookback": 20, "exit_lookback": 10, "atr_lookback": 14}},
    },
    {
        "template_id": "template.blank",
        "version": 1,
        "display_name": "空白模板",
        "description": "结构化策略的受控空白起点；默认规则不产生交易。",
        "source_strategy_id": None,
        "source_strategy_version": None,
        "default_overrides": {},
    },
)


def _function_dependencies(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        evidence = validate_strategy_definition(definition)
    except StrategyDefinitionError as error:
        raise StrategyTemplateError(error.code, error.message) from error
    return [{"id": value["id"], "version": value["version"]} for value in evidence["function_references"]]


def build_strategy_templates() -> tuple[dict[str, Any], ...]:
    sources = _sources()
    templates: list[dict[str, Any]] = []
    for spec in _TEMPLATE_SPECS:
        source_id = spec["source_strategy_id"] or "strategy.blank_placeholder"
        source_version = spec["source_strategy_version"] or 1
        source = sources.get((source_id, source_version))
        if source is None:
            raise StrategyTemplateError("MISSING_TEMPLATE_SOURCE", f"template source missing: {source_id}@{source_version}")
        document = {
            "schema_version": 1,
            **spec,
            "disclaimer": _DISCLAIMER,
            "supported_asset_types": source["supported_asset_types"],
            "dependencies": _function_dependencies(source),
            "preview": {
                "parameters": source["parameters"],
                "risk": source["risk"],
                "position_sizing": source["position_sizing"],
                "stop_loss": source["stop_loss"],
            },
        }
        try:
            validate_contract(TEMPLATE_SCHEMA, document)
        except ContractValidationError as error:
            raise StrategyTemplateError("INVALID_TEMPLATE", str(error)) from error
        templates.append(document)
    return tuple(deepcopy(templates))


def create_from_template(template_id: str, *, display_name: str | None = None, strategy_id: str) -> dict[str, Any]:
    templates = {item["template_id"]: item for item in build_strategy_templates()}
    template = templates.get(template_id)
    if template is None:
        raise StrategyTemplateError("TEMPLATE_NOT_FOUND", f"unknown strategy template: {template_id}")
    source_id = template["source_strategy_id"] or "strategy.blank_placeholder"
    source_version = template["source_strategy_version"] or 1
    source = _sources().get((source_id, source_version))
    if source is None:
        raise StrategyTemplateError("MISSING_TEMPLATE_SOURCE", f"template source missing: {source_id}@{source_version}")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    definition = deepcopy(source)
    definition.update(
        {
            "id": strategy_id,
            "version": 1,
            "display_name": (display_name or f"{template['display_name']} 副本").strip(),
            "origin": "custom",
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
            "template_source": {
                "template_id": template["template_id"],
                "template_version": template["version"],
                "source_strategy_id": template["source_strategy_id"],
                "source_strategy_version": template["source_strategy_version"],
                "default_overrides": template["default_overrides"],
            },
        }
    )
    try:
        validate_strategy_definition(definition)
    except StrategyDefinitionError as error:
        raise StrategyTemplateError(error.code, error.message) from error
    return definition


__all__ = ["StrategyTemplateError", "build_strategy_templates", "create_from_template"]
