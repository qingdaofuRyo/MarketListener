"""Deterministic event-driven backtesting for versioned Strategy Definitions.

Rules are evaluated only from the current and earlier bars.  A bar-close
signal becomes an OrderIntent that may fill at the next bar open or close,
then passes through the shared risk/execution boundary before portfolio state
changes.  This module is independent from the legacy observation scanner.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any, Mapping, Sequence

from market_monitor.contracts import ContractValidationError, validate_contract
from market_monitor.strategy_definition import StrategyDefinitionError, validate_strategy_definition
from market_monitor.strategy_function_registry import (
    StrategyFunctionRegistryError,
    build_builtin_strategy_function_registry,
)
from market_monitor.strategy_execution import (
    InMemoryExecutionAudit,
    InMemoryExecutionStore,
    RiskContext,
    StrategyExecutionError,
    StrategyExecutionGateway,
)
from market_monitor.strategy_functions import StrategyFunctionError, execute_strategy_function


class StrategyBacktestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


BACKTEST_RESULT_SCHEMA = "strategy-backtest-result.schema.json"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyBacktestError("INVALID_BAR", f"{name} 必须为数值")
    parsed = float(value)
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        raise StrategyBacktestError("INVALID_BAR", f"{name} 必须为{'正' if positive else '有限'}数值")
    return parsed


def _normalize_bars(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((dict(bar) for bar in bars), key=lambda bar: str(bar.get("bar_open_time") or ""))
    seen: set[str] = set()
    for bar in ordered:
        opened = str(bar.get("bar_open_time") or "")
        if not opened or "T" not in opened:
            raise StrategyBacktestError("INVALID_BAR", "bar_open_time 必须为带时区的 ISO 时间")
        if opened in seen:
            raise StrategyBacktestError("DUPLICATE_BAR", f"重复 K 线时间：{opened}")
        seen.add(opened)
        open_price = _number(bar.get("open"), "open", positive=True)
        high = _number(bar.get("high"), "high", positive=True)
        low = _number(bar.get("low"), "low", positive=True)
        close = _number(bar.get("close"), "close", positive=True)
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise StrategyBacktestError("INVALID_BAR", "low/high 必须包住 open/close")
        bar.update({"open": open_price, "high": high, "low": low, "close": close})
    return ordered


def _parameters(definition: Mapping[str, Any], overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    specifications = definition["parameters"]
    supplied = dict(overrides or {})
    unknown = sorted(set(supplied) - set(specifications))
    if unknown:
        raise StrategyBacktestError("UNKNOWN_PARAMETER", f"未知参数：{', '.join(unknown)}")
    result: dict[str, Any] = {}
    for name, specification in specifications.items():
        value = supplied.get(name, specification["default"])
        value_type = specification["type"]
        if value_type == "boolean" and not isinstance(value, bool):
            raise StrategyBacktestError("TYPE_MISMATCH", f"参数 {name} 必须为 boolean")
        if value_type == "string" and not isinstance(value, str):
            raise StrategyBacktestError("TYPE_MISMATCH", f"参数 {name} 必须为 string")
        if value_type in {"integer", "number"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise StrategyBacktestError("TYPE_MISMATCH", f"参数 {name} 必须为有限数值")
            if value_type == "integer" and int(value) != value:
                raise StrategyBacktestError("TYPE_MISMATCH", f"参数 {name} 必须为整数")
            if "minimum" in specification and value < specification["minimum"]:
                raise StrategyBacktestError("INVALID_PARAMETER", f"参数 {name} 低于下限")
            if "maximum" in specification and value > specification["maximum"]:
                raise StrategyBacktestError("INVALID_PARAMETER", f"参数 {name} 高于上限")
        result[name] = value
    return result


def evaluate_rule_series(
    definition: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
) -> tuple[list[bool | None], list[bool | None]]:
    """Evaluate entry and exit Rule ASTs as aligned three-state series."""

    size = len(bars)
    fields = {
        name: [bar.get(name) for bar in bars]
        for name in ("open", "high", "low", "close", "volume", "amount", "open_interest", "settlement")
    }

    def operand(value: Mapping[str, Any]) -> Any:
        kind = value["kind"]
        if kind == "series":
            offset = int(value.get("offset") or 0)
            source = list(fields[str(value["field"])])
            return [None] * offset + source[: size - offset] if offset else source
        if kind == "literal":
            return value["value"]
        if kind == "parameter":
            return parameters[str(value["name"])]
        return function_call(value["call"])

    def function_call(call: Mapping[str, Any]) -> Any:
        arguments = [operand(item) for item in call["arguments"]]
        try:
            return execute_strategy_function(
                str(call["function_id"]),
                *arguments,
                version=int(call["version"]),
            )
        except StrategyFunctionError as error:
            raise StrategyBacktestError(error.code, error.message) from error

    def aligned(value: Any) -> list[Any]:
        if isinstance(value, list):
            if len(value) != size:
                raise StrategyBacktestError("SERIES_LENGTH", "策略函数返回长度与 K 线不一致")
            return value
        if isinstance(value, dict):
            raise StrategyBacktestError("TYPE_MISMATCH", "记录型函数结果必须先选择具体输出序列")
        return [value] * size

    comparisons = {
        "gte": lambda left, right: left >= right,
        "lte": lambda left, right: left <= right,
        "gt": lambda left, right: left > right,
        "eq": lambda left, right: left == right,
        "lt": lambda left, right: left < right,
        "ne": lambda left, right: left != right,
    }

    def visit(node: Mapping[str, Any]) -> list[bool | None]:
        if node["node_type"] == "condition":
            left = aligned(function_call(node["left"]))
            if "comparator" not in node:
                return [None if value is None else bool(value) for value in left]
            right = aligned(operand(node["right"]))
            compare = comparisons[str(node["comparator"])]
            return [
                None if left_value is None or right_value is None else bool(compare(left_value, right_value))
                for left_value, right_value in zip(left, right, strict=True)
            ]
        children = [visit(child) for child in node["children"]]
        operator = node["operator"]
        result: list[bool | None] = []
        for values in zip(*children, strict=True):
            if operator == "NOT":
                result.append(None if values[0] is None else not values[0])
            elif operator == "AND":
                result.append(False if False in values else (None if None in values else True))
            else:
                result.append(True if True in values else (None if None in values else False))
        return result

    return visit(definition["entry_rules"]), visit(definition["exit_rules"])


def _equity(state: Mapping[str, Any], price: float, multiplier: float) -> float:
    return float(state["cash"]) + float(state["position_quantity"]) * price * multiplier


def _drawdown(peak: float, equity: float) -> float:
    return 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak * 100.0)


def _fill_price(base: float, side: str, slippage_rate: float) -> tuple[float, float]:
    actual = base * (1 + slippage_rate if side == "buy" else 1 - slippage_rate)
    return actual, abs(actual - base)


def _threshold(
    control: Mapping[str, Any],
    *,
    entry_price: float,
    entry_atr: float | None,
    direction: str,
    stop: bool,
) -> float | None:
    if not control["enabled"]:
        return None
    value = float(control["value"])
    kind = control["kind"]
    sign = 1 if direction == "long" else -1
    if not stop:
        sign *= -1
    if kind == "percent":
        return entry_price * (1 - sign * value / 100.0)
    if kind == "atr_multiple":
        return None if entry_atr is None else entry_price - sign * value * entry_atr
    return value


def _position_size(
    definition: Mapping[str, Any],
    *,
    equity: float,
    price: float,
    multiplier: float,
    entry_atr: float | None,
) -> tuple[float, str | None]:
    sizing = definition["position_sizing"]
    value = float(sizing["value"])
    if sizing["kind"] == "fixed_quantity":
        return value, None
    if sizing["kind"] == "equity_percent":
        return equity * value / 100.0 / (price * multiplier), None
    stop_price = _threshold(
        definition["stop_loss"],
        entry_price=price,
        entry_atr=entry_atr,
        direction=str(definition.get("direction") or "long"),
        stop=True,
    )
    if stop_price is None or stop_price == price:
        return 0.0, "风险百分比仓位需要当前可计算的非零止损距离"
    return equity * value / 100.0 / (abs(price - stop_price) * multiplier), None


def _scale_out_configuration(definition: Mapping[str, Any]) -> tuple[bool, float]:
    """Return the optional one-time take-profit position-reduction rule.

    The field was added after the first published structured definitions.  An
    absent field must therefore preserve their original full-exit take-profit
    semantics exactly.
    """

    value = definition.get("scale_out")
    if not isinstance(value, Mapping) or not value.get("enabled"):
        return False, 0.0
    return True, float(value["ratio_percent"])


def _empty_history() -> dict[str, list[dict[str, Any]]]:
    return {"equity_points": [], "order_intents": [], "fills": [], "trades": [], "markers": [], "errors": []}


def _dependency_lock(
    definition: Mapping[str, Any],
    *,
    definition_hash: str,
    function_references: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
    parameter_fingerprint: str,
    instrument_id: str,
    data_version: str,
    data_fingerprint: str,
    bars: Sequence[Mapping[str, Any]],
    data_query: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the exact, persisted dependency lock for one backtest run."""

    registry = build_builtin_strategy_function_registry()
    function_locks: list[dict[str, Any]] = []
    for reference in function_references:
        function_id = str(reference["id"])
        version = int(reference["version"])
        try:
            function = registry.resolve(function_id, version)
        except StrategyFunctionRegistryError as error:
            raise StrategyBacktestError("MISSING_FUNCTION_VERSION", error.message) from error
        function_locks.append(
            {
                "resource_kind": "strategy_function",
                "id": function.function_id,
                "version": function.version,
                "definition_hash": function.definition_hash(),
            }
        )
    query = {
        key: value
        for key, value in dict(data_query or {}).items()
        if key in {"period", "limit"} and value is not None
    }
    return {
        "strategy": {
            "resource_kind": "strategy_definition",
            "id": str(definition["id"]),
            "version": int(definition["version"]),
            "definition_hash": definition_hash,
        },
        "strategy_functions": function_locks,
        # Indicators are chart-only resources; a Rule AST cannot execute one.
        # Preserve that fact explicitly instead of inventing a "latest" lock.
        "indicators": [],
        "parameters": {"values": dict(parameters), "fingerprint": parameter_fingerprint},
        "market_data": {
            "resource_kind": "market_bars",
            "instrument_id": instrument_id,
            "data_version": data_version,
            "data_fingerprint": data_fingerprint,
            "bars_count": len(bars),
            "first_bar_at": str(bars[0]["bar_open_time"]) if bars else None,
            "last_bar_at": str(bars[-1]["bar_open_time"]) if bars else None,
            "query": query,
        },
        "engine": {"id": "event_backtest", "version": 1},
    }


def run_strategy_backtest(
    definition: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    instrument_id: str,
    data_version: str,
    parameters: Mapping[str, Any] | None = None,
    contract_multiplier: float = 1.0,
    currency: str = "CNY",
    resume_state: Mapping[str, Any] | None = None,
    stop_after_index: int | None = None,
    data_query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run or resume a deterministic one-instrument event backtest."""

    if str((definition.get("execution") or {}).get("run_mode") or "") != "backtest":
        raise StrategyBacktestError("RUN_MODE_MISMATCH", "回测引擎只接受 run_mode=backtest 的策略")
    try:
        validation = validate_strategy_definition(definition)
    except StrategyDefinitionError as error:
        raise StrategyBacktestError(error.code, error.message) from error
    multiplier = _number(contract_multiplier, "contract_multiplier", positive=True)
    ordered = _normalize_bars(bars)
    values = _parameters(definition, parameters)
    definition_fingerprint = _hash(definition)
    data_fingerprint = _hash(
        {
            "instrument_id": instrument_id,
            "data_version": data_version,
            "bars": ordered,
        }
    )
    parameter_fingerprint = _hash(values)
    function_references = list(validation["function_references"])
    if definition["stop_loss"]["enabled"] and definition["stop_loss"]["kind"] == "atr_multiple":
        reference = {"resource_kind": "strategy_function", "id": "technical.atr", "version": 1}
        if reference not in function_references:
            function_references.append(reference)
            function_references.sort(key=lambda item: (item["id"], item["version"]))
    dependency_lock = _dependency_lock(
        definition,
        definition_hash=definition_fingerprint,
        function_references=function_references,
        parameters=values,
        parameter_fingerprint=parameter_fingerprint,
        instrument_id=instrument_id,
        data_version=data_version,
        data_fingerprint=data_fingerprint,
        bars=ordered,
        data_query=data_query,
    )
    version_fingerprint = _hash(
        {
            "dependency_lock": dependency_lock,
            "contract_multiplier": multiplier,
            "currency": currency,
        }
    )
    run_id = f"bt_{version_fingerprint[:24]}"

    base_result = {
        "schema_version": 1,
        "run_id": run_id,
        "strategy_id": definition["id"],
        "strategy_version": definition["version"],
        "definition_hash": definition_fingerprint,
        "version_fingerprint": version_fingerprint,
        "function_references": function_references,
        "dependency_lock": dependency_lock,
        "parameters": values,
        "parameter_fingerprint": parameter_fingerprint,
        "instrument_id": instrument_id,
        "data_version": data_version,
        "data_fingerprint": data_fingerprint,
        "run_mode": "backtest",
        "currency": currency,
        "contract_multiplier": multiplier,
        "bars_count": len(ordered),
        "first_bar_at": str(ordered[0]["bar_open_time"]) if ordered else None,
        "last_bar_at": str(ordered[-1]["bar_open_time"]) if ordered else None,
    }
    if not ordered:
        result = {
            **base_result,
            "status": "unavailable",
            "unavailable_reason": "NO_BARS",
            "initial_cash": float(definition["backtest"]["initial_cash"]),
            "final_equity": float(definition["backtest"]["initial_cash"]),
            **_empty_history(),
            "final_state": None,
            "resume_state": None,
        }
        _validate_result(result)
        return result

    entry_series, exit_series = evaluate_rule_series(definition, ordered, values)
    atr_lookback = int(values.get("atr_lookback", 14))
    atr_series = execute_strategy_function(
        "technical.atr",
        [bar["high"] for bar in ordered],
        [bar["low"] for bar in ordered],
        [bar["close"] for bar in ordered],
        atr_lookback,
        version=1,
    )
    initial_cash = float(definition["backtest"]["initial_cash"])
    state: dict[str, Any] = {
        "next_index": 0,
        "cash": initial_cash,
        "position_quantity": 0.0,
        "average_entry_price": 0.0,
        "entry_time": None,
        "entry_index": None,
        "entry_fees": 0.0,
        "entry_slippage_cost": 0.0,
        "entry_atr": None,
        "entry_count": 0,
        "scale_out_done": False,
        "peak_equity": initial_cash,
        "last_exit_index": None,
        "pending": None,
        "sequence": 0,
        "history": _empty_history(),
        "definition_fingerprint": definition_fingerprint,
        "data_fingerprint": data_fingerprint,
        "dependency_lock_fingerprint": _hash(dependency_lock),
    }
    if resume_state is not None:
        candidate = deepcopy(dict(resume_state))
        if (
            candidate.get("definition_fingerprint") != definition_fingerprint
            or candidate.get("data_fingerprint") != data_fingerprint
            or candidate.get("dependency_lock_fingerprint") != _hash(dependency_lock)
        ):
            raise StrategyBacktestError("RESUME_MISMATCH", "恢复状态与策略或行情版本不一致")
        state.update(candidate)
    state.setdefault("scale_out_done", False)
    history = state["history"]
    gateway = StrategyExecutionGateway(store=InMemoryExecutionStore(), audit=InMemoryExecutionAudit())
    commission_rate = float(definition["backtest"]["commission_rate"])
    slippage_rate = float(definition["backtest"]["slippage_rate"])
    direction = str(definition.get("direction") or "long")

    def submit(
        *,
        effect: str,
        signal_index: int,
        fill_index: int,
        reason: str,
        signal_time: str,
        filled_at: str,
        base_price: float,
        close_quantity: float | None = None,
    ) -> bool:
        current_quantity = float(state["position_quantity"])
        side = ("buy" if direction == "long" else "sell") if effect == "open" else ("sell" if current_quantity > 0 else "buy")
        actual_price, price_slippage = _fill_price(base_price, side, slippage_rate)
        current_equity = _equity(state, actual_price, multiplier)
        if effect == "open":
            quantity, sizing_error = _position_size(
                definition,
                equity=current_equity,
                price=actual_price,
                multiplier=multiplier,
                entry_atr=atr_series[fill_index],
            )
            if sizing_error:
                history["errors"].append({"barIndex": fill_index, "code": "POSITION_SIZE_UNAVAILABLE", "reason": sizing_error})
                return False
        else:
            quantity = abs(current_quantity) if close_quantity is None else float(close_quantity)
            if quantity <= 0:
                return False
            if quantity > abs(current_quantity) + 1e-9:
                raise StrategyBacktestError("INVALID_CLOSE_QUANTITY", "减仓数量不能超过当前持仓")
            quantity = min(quantity, abs(current_quantity))
        sequence = int(state["sequence"])
        intent_id = f"{run_id}:{signal_index}:{fill_index}:{effect}:{sequence}"
        state["sequence"] = sequence + 1
        try:
            outcome = gateway.dispatch(
                definition,
                RiskContext(
                    account_equity=max(current_equity, 1e-9),
                    reference_price=actual_price,
                    current_position_quantity=current_quantity,
                    contract_multiplier=multiplier,
                    drawdown_percent=_drawdown(float(state["peak_equity"]), current_equity),
                ),
                instrument_id=instrument_id,
                side=side,
                position_effect=effect,
                order_type="market",
                quantity=quantity,
                signal_time=signal_time,
                run_mode="backtest",
                intent_id=intent_id,
                created_at=signal_time,
            )
        except StrategyExecutionError as error:
            raise StrategyBacktestError(error.code, error.message) from error
        history["order_intents"].append(
            {
                "intent": dict(outcome.intent),
                "signal_index": signal_index,
                "fill_index": fill_index,
                "capability": outcome.capability,
                "run_mode": outcome.run_mode,
                "risk_decision": outcome.risk_decision,
                "execution_status": outcome.execution_status,
                "reason": outcome.reason,
                "adapter_id": outcome.adapter_id,
            }
        )
        if outcome.execution_status != "accepted":
            return False

        fee = quantity * actual_price * multiplier * commission_rate
        slippage_cost = quantity * price_slippage * multiplier
        fill = {
            "fill_id": f"fill:{intent_id}",
            "intent_id": intent_id,
            "instrument_id": instrument_id,
            "side": side,
            "position_effect": effect,
            "filled_at": filled_at,
            "bar_index": fill_index,
            "base_price": base_price,
            "price": actual_price,
            "quantity": quantity,
            "contract_multiplier": multiplier,
            "notional": quantity * actual_price * multiplier,
            "commission": fee,
            "slippage_cost": slippage_cost,
            "currency": currency,
            "reason": reason,
            "adapter_id": outcome.adapter_id,
        }
        history["fills"].append(fill)
        signed = quantity if side == "buy" else -quantity
        if side == "buy":
            state["cash"] = float(state["cash"]) - fill["notional"] - fee
        else:
            state["cash"] = float(state["cash"]) + fill["notional"] - fee

        if effect == "open":
            old_absolute = abs(current_quantity)
            new_quantity = current_quantity + signed
            state["average_entry_price"] = (
                (float(state["average_entry_price"]) * old_absolute + actual_price * quantity)
                / abs(new_quantity)
            )
            state["position_quantity"] = new_quantity
            if old_absolute == 0:
                state["entry_time"] = filled_at
                state["entry_index"] = fill_index
                state["entry_fees"] = 0.0
                state["entry_slippage_cost"] = 0.0
                state["entry_count"] = 0
                state["scale_out_done"] = False
            state["entry_fees"] = float(state["entry_fees"]) + fee
            state["entry_slippage_cost"] = float(state["entry_slippage_cost"]) + slippage_cost
            state["entry_atr"] = atr_series[fill_index]
            state["entry_count"] = int(state["entry_count"]) + 1
            history["markers"].append(
                {"kind": "entry", "time": filled_at, "price": actual_price, "bar_index": fill_index, "reason": reason}
            )
            return True

        entry_price = float(state["average_entry_price"])
        gross_pnl = (
            (actual_price - entry_price) * quantity * multiplier
            if current_quantity > 0
            else (entry_price - actual_price) * quantity * multiplier
        )
        closing_share = quantity / abs(current_quantity)
        allocated_entry_fees = float(state["entry_fees"]) * closing_share
        allocated_entry_slippage = float(state["entry_slippage_cost"]) * closing_share
        total_fees = allocated_entry_fees + fee
        pnl = gross_pnl - total_fees
        invested = entry_price * quantity * multiplier
        trade_id = f"trade:{intent_id}"
        history["trades"].append(
            {
                "trade_id": trade_id,
                "instrument_id": instrument_id,
                "direction": "long" if current_quantity > 0 else "short",
                "entry_time": state["entry_time"],
                "exit_time": filled_at,
                "entry_price": entry_price,
                "exit_price": actual_price,
                "quantity": quantity,
                "contract_multiplier": multiplier,
                "gross_pnl": gross_pnl,
                "commission": total_fees,
                "slippage_cost": allocated_entry_slippage + slippage_cost,
                "pnl": pnl,
                "pnl_percent": None if invested == 0 else pnl / invested * 100.0,
                "holding_bars": fill_index - int(state["entry_index"]),
                "entry_reason": "strategy_entry",
                "exit_reason": reason,
                "currency": currency,
            }
        )
        history["markers"].append(
            {"kind": "exit", "time": filled_at, "price": actual_price, "bar_index": fill_index, "reason": reason, "trade_id": trade_id}
        )
        remaining_quantity = abs(current_quantity) - quantity
        if remaining_quantity > 1e-9:
            state["position_quantity"] = current_quantity + signed
            state["entry_fees"] = float(state["entry_fees"]) - allocated_entry_fees
            state["entry_slippage_cost"] = float(state["entry_slippage_cost"]) - allocated_entry_slippage
            return True
        state.update(
            {
                "position_quantity": 0.0,
                "average_entry_price": 0.0,
                "entry_time": None,
                "entry_index": None,
                "entry_fees": 0.0,
                "entry_slippage_cost": 0.0,
                "entry_atr": None,
                "entry_count": 0,
                "last_exit_index": fill_index,
            }
        )
        return True

    start_index = int(state["next_index"])
    end_index = len(ordered) - 1 if stop_after_index is None else min(len(ordered) - 1, int(stop_after_index))
    if start_index < 0 or start_index > len(ordered) or end_index < start_index - 1:
        raise StrategyBacktestError("INVALID_RESUME_STATE", "恢复索引超出行情范围")
    fill_setting = definition["execution"]["fill_price"]

    for index in range(start_index, end_index + 1):
        bar = ordered[index]
        opened_at = str(bar["bar_open_time"])
        closed_at = str(bar.get("bar_close_time") or opened_at)
        pending = state.get("pending")
        if pending and fill_setting == "next_open":
            submit(
                effect=pending["effect"],
                signal_index=pending["signal_index"],
                fill_index=index,
                reason=pending["reason"],
                signal_time=pending["signal_time"],
                filled_at=opened_at,
                base_price=float(bar["open"]),
            )
            state["pending"] = None

        if float(state["position_quantity"]) != 0:
            position_direction = "long" if float(state["position_quantity"]) > 0 else "short"
            stop_price = _threshold(
                definition["stop_loss"],
                entry_price=float(state["average_entry_price"]),
                entry_atr=state["entry_atr"],
                direction=position_direction,
                stop=True,
            )
            take_price = _threshold(
                definition["take_profit"],
                entry_price=float(state["average_entry_price"]),
                entry_atr=state["entry_atr"],
                direction=position_direction,
                stop=False,
            )
            stop_hit = stop_price is not None and (
                float(bar["low"]) <= stop_price if position_direction == "long" else float(bar["high"]) >= stop_price
            )
            take_hit = take_price is not None and (
                float(bar["high"]) >= take_price if position_direction == "long" else float(bar["low"]) <= take_price
            )
            scale_out_enabled, scale_out_ratio = _scale_out_configuration(definition)
            should_scale_out = (
                not stop_hit
                and take_hit
                and scale_out_enabled
                and not bool(state.get("scale_out_done"))
            )
            if stop_hit or (take_hit and not scale_out_enabled):
                is_stop = bool(stop_hit)
                threshold = float(stop_price if is_stop else take_price)
                if position_direction == "long":
                    base = min(float(bar["open"]), threshold) if is_stop else max(float(bar["open"]), threshold)
                else:
                    base = max(float(bar["open"]), threshold) if is_stop else min(float(bar["open"]), threshold)
                submit(
                    effect="close",
                    signal_index=index,
                    fill_index=index,
                    reason="stop_loss" if is_stop else "take_profit",
                    signal_time=opened_at,
                    filled_at=opened_at,
                    base_price=base,
                )
                state["pending"] = None
            elif should_scale_out:
                threshold = float(take_price)
                base = (
                    max(float(bar["open"]), threshold)
                    if position_direction == "long"
                    else min(float(bar["open"]), threshold)
                )
                quantity = abs(float(state["position_quantity"])) * scale_out_ratio / 100.0
                if submit(
                    effect="close",
                    signal_index=index,
                    fill_index=index,
                    reason="take_profit_scale_out",
                    signal_time=opened_at,
                    filled_at=opened_at,
                    base_price=base,
                    close_quantity=quantity,
                ):
                    state["scale_out_done"] = True
                state["pending"] = None

        pending = state.get("pending")
        if pending and fill_setting == "next_close":
            submit(
                effect=pending["effect"],
                signal_index=pending["signal_index"],
                fill_index=index,
                reason=pending["reason"],
                signal_time=pending["signal_time"],
                filled_at=closed_at,
                base_price=float(bar["close"]),
            )
            state["pending"] = None

        has_position = float(state["position_quantity"]) != 0
        if index < len(ordered) - 1 and state.get("pending") is None:
            if has_position and exit_series[index] is True:
                state["pending"] = {
                    "effect": "close",
                    "signal_index": index,
                    "signal_time": closed_at,
                    "reason": "strategy_exit",
                }
            elif entry_series[index] is True:
                can_add = not has_position or (
                    definition["pyramiding"]["enabled"]
                    and int(state["entry_count"]) < int(definition["pyramiding"]["max_entries"])
                )
                last_exit = state["last_exit_index"]
                cooldown_ok = (
                    not has_position
                    and (
                        last_exit is None
                        or definition["reentry"]["enabled"]
                        and index - int(last_exit) > int(definition["reentry"]["cooldown_bars"])
                    )
                ) or has_position
                if can_add and cooldown_ok:
                    state["pending"] = {
                        "effect": "open",
                        "signal_index": index,
                        "signal_time": closed_at,
                        "reason": "strategy_entry" if not has_position else "pyramiding_entry",
                    }

        close_equity = _equity(state, float(bar["close"]), multiplier)
        state["peak_equity"] = max(float(state["peak_equity"]), close_equity)
        history["equity_points"].append(
            {
                "time": closed_at,
                "bar_index": index,
                "cash": float(state["cash"]),
                "position_quantity": float(state["position_quantity"]),
                "close": float(bar["close"]),
                "equity": close_equity,
                "drawdown_percent": _drawdown(float(state["peak_equity"]), close_equity),
                "currency": currency,
            }
        )
        state["next_index"] = index + 1

    partial = int(state["next_index"]) < len(ordered)
    final_price = float(ordered[max(0, int(state["next_index"]) - 1)]["close"])
    final_equity = _equity(state, final_price, multiplier)
    compact_state = {key: deepcopy(value) for key, value in state.items() if key != "history"}
    result = {
        **base_result,
        "status": "partial" if partial else "ready",
        "unavailable_reason": None,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        **deepcopy(history),
        "final_state": compact_state,
        "resume_state": deepcopy(state) if partial else None,
    }
    _validate_result(result)
    return result


def _validate_result(result: Mapping[str, Any]) -> None:
    try:
        validate_contract(BACKTEST_RESULT_SCHEMA, dict(result))
    except ContractValidationError as error:
        raise StrategyBacktestError("INVALID_BACKTEST_RESULT", str(error)) from error


__all__ = ["StrategyBacktestError", "evaluate_rule_series", "run_strategy_backtest"]
