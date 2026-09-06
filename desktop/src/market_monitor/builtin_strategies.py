"""Built-in versioned Strategy Definitions used by the R4 backtest slice."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_CREATED_AT = "2026-09-03T00:00:00+08:00"


def _series(field: str, *, offset: int = 0) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": "series", "field": field}
    if offset:
        result["offset"] = offset
    return result


def _literal(value: int | float | str | bool) -> dict[str, Any]:
    return {"kind": "literal", "value": value}


def _function(function_id: str, *arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "function",
        "call": {"function_id": function_id, "version": 1, "arguments": list(arguments)},
    }


def _condition(function_id: str, *arguments: dict[str, Any]) -> dict[str, Any]:
    return {"node_type": "condition", "left": {"function_id": function_id, "version": 1, "arguments": list(arguments)}}


def _base(strategy_id: str, display_name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": strategy_id,
        "version": 1,
        "display_name": display_name,
        "origin": "builtin",
        "supported_asset_types": ["STOCK", "ETF", "FUTURE"],
        "status": "active",
        "base_timeframe": "1d",
        "universe": {"market_types": ["a_share", "etf", "cn_future"]},
        "parameters": {},
        "direction": "long",
        "position_sizing": {"kind": "equity_percent", "value": 20},
        "stop_loss": {"enabled": True, "kind": "percent", "value": 5},
        "take_profit": {"enabled": True, "kind": "percent", "value": 10},
        "pyramiding": {"enabled": False, "max_entries": 1},
        "reentry": {"enabled": True, "cooldown_bars": 1},
        "risk": {"max_position_percent": 30, "max_drawdown_percent": 25},
        "execution": {"run_mode": "backtest", "signal_timing": "bar_close", "fill_price": "next_open"},
        "backtest": {"initial_cash": 100000, "commission_rate": 0.0003, "slippage_rate": 0.0001},
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
    }


def _ma_crossover() -> dict[str, Any]:
    definition = _base("strategy.ma_crossover", "MA 均线交叉")
    definition.update(
        {
            "description": "20 日 SMA 上穿 60 日 SMA 开多，下穿平仓。SMA 与 MA Indicator 共享 technical.sma@1。",
            "parameters": {
                "fast": {"type": "integer", "default": 20, "minimum": 2, "maximum": 500},
                "slow": {"type": "integer", "default": 60, "minimum": 3, "maximum": 500},
            },
            "entry_rules": _condition(
                "condition.crossover",
                _function("technical.sma", _series("close"), {"kind": "parameter", "name": "fast"}),
                _function("technical.sma", _series("close"), {"kind": "parameter", "name": "slow"}),
            ),
            "exit_rules": _condition(
                "condition.crossunder",
                _function("technical.sma", _series("close"), {"kind": "parameter", "name": "fast"}),
                _function("technical.sma", _series("close"), {"kind": "parameter", "name": "slow"}),
            ),
        }
    )
    return definition


def _donchian_atr() -> dict[str, Any]:
    definition = _base("strategy.donchian_atr", "Donchian + ATR 趋势")
    definition.update(
        {
            "description": "收盘价突破此前 20 根最高价开多，跌破此前 10 根最低价平仓，并使用 2 ATR 止损。",
            "parameters": {
                "entry_lookback": {"type": "integer", "default": 20, "minimum": 2, "maximum": 500},
                "exit_lookback": {"type": "integer", "default": 10, "minimum": 2, "maximum": 500},
                "atr_lookback": {"type": "integer", "default": 14, "minimum": 2, "maximum": 200},
            },
            "entry_rules": _condition(
                "condition.crossover",
                _series("close"),
                _function(
                    "technical.highest",
                    _series("high", offset=1),
                    {"kind": "parameter", "name": "entry_lookback"},
                ),
            ),
            "exit_rules": _condition(
                "condition.crossunder",
                _series("close"),
                _function(
                    "technical.lowest",
                    _series("low", offset=1),
                    {"kind": "parameter", "name": "exit_lookback"},
                ),
            ),
            "position_sizing": {"kind": "risk_percent", "value": 1},
            "stop_loss": {"enabled": True, "kind": "atr_multiple", "value": 2},
            "take_profit": {"enabled": False, "kind": "percent", "value": 0},
        }
    )
    return definition


_BUILTINS = (_ma_crossover(), _donchian_atr())


def build_builtin_strategy_definitions() -> tuple[dict[str, Any], ...]:
    """Return copies so callers cannot mutate the process-wide catalog."""

    return tuple(deepcopy(item) for item in _BUILTINS)


__all__ = ["build_builtin_strategy_definitions"]
