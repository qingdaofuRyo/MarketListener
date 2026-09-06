"""Semantic validation for the versioned Strategy Rule AST."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

from market_monitor.contracts import ContractValidationError, validate_contract
from market_monitor.strategy_function_registry import (
    FunctionInput,
    StrategyFunctionRegistry,
    StrategyFunctionRegistryError,
    build_builtin_strategy_function_registry,
)


SCHEMA = "strategy-definition.schema.json"
MAX_RULE_DEPTH = 12
MAX_RULE_NODES = 200


class StrategyDefinitionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _timestamp(value: Any, name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise StrategyDefinitionError("INVALID_TIMESTAMP", f"{name} must be ISO-8601") from error


def _operand_type(operand: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    kind = str(operand.get("kind") or "")
    if kind == "series":
        return "series<number>"
    if kind == "parameter":
        name = str(operand.get("name") or "")
        if name not in parameters:
            raise StrategyDefinitionError("UNKNOWN_PARAMETER", f"unknown parameter: {name}")
        return str(parameters[name].get("type") or "")
    value = operand.get("value")
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _input_matches(expected: FunctionInput, actual: str) -> bool:
    if expected.value_type == actual:
        return True
    if expected.value_type in {"number", "integer"} and actual in {"number", "integer"}:
        return True
    return expected.value_type in {"comparison_operator", "market_cap_unit"} and actual == "string"


def validate_strategy_definition(
    document: Mapping[str, Any],
    registry: StrategyFunctionRegistry | None = None,
    *,
    validate_assets: bool = True,
) -> dict[str, Any]:
    """Validate schema, tree bounds, types, versions, assets and live-mode gate."""
    try:
        validate_contract(SCHEMA, dict(document))
    except ContractValidationError as error:
        raise StrategyDefinitionError("INVALID_DEFINITION", str(error)) from error
    if _timestamp(document["created_at"], "created_at") > _timestamp(document["updated_at"], "updated_at"):
        raise StrategyDefinitionError("INVALID_TIMESTAMP", "updated_at must not precede created_at")
    if document["execution"]["run_mode"] == "live":
        raise StrategyDefinitionError("LIVE_DISABLED", "实盘交易接口尚未配置")
    scale_out = document.get("scale_out")
    if isinstance(scale_out, Mapping) and scale_out.get("enabled") and not document["take_profit"]["enabled"]:
        raise StrategyDefinitionError("INVALID_DEFINITION", "止盈减仓需要先启用止盈")

    functions = registry or build_builtin_strategy_function_registry()
    parameters = document["parameters"]
    for name, specification in parameters.items():
        value_type = specification["type"]
        default = specification["default"]
        if value_type == "boolean" and not isinstance(default, bool):
            raise StrategyDefinitionError("TYPE_MISMATCH", f"parameter {name} default must be boolean")
        if value_type == "string" and not isinstance(default, str):
            raise StrategyDefinitionError("TYPE_MISMATCH", f"parameter {name} default must be string")
        if value_type in {"integer", "number"}:
            if isinstance(default, bool) or not isinstance(default, (int, float)) or not math.isfinite(float(default)):
                raise StrategyDefinitionError("TYPE_MISMATCH", f"parameter {name} default must be finite numeric")
            if value_type == "integer" and int(default) != default:
                raise StrategyDefinitionError("TYPE_MISMATCH", f"parameter {name} default must be integer")
            if "minimum" in specification and default < specification["minimum"]:
                raise StrategyDefinitionError("INVALID_PARAMETER", f"parameter {name} default is below minimum")
            if "maximum" in specification and default > specification["maximum"]:
                raise StrategyDefinitionError("INVALID_PARAMETER", f"parameter {name} default is above maximum")
    strategy_assets = set(document["supported_asset_types"])
    references: set[tuple[str, int]] = set()
    node_count = 0

    def validate_call(call: Mapping[str, Any]) -> str:
        try:
            definition = functions.resolve(str(call["function_id"]), int(call["version"]))
        except StrategyFunctionRegistryError as error:
            raise StrategyDefinitionError("MISSING_FUNCTION_VERSION", error.message) from error
        references.add(definition.key)
        if validate_assets and not strategy_assets.issubset(definition.supported_asset_types):
            raise StrategyDefinitionError(
                "UNSUPPORTED_ASSET",
                f"{definition.function_id}@{definition.version} does not support every strategy asset",
            )
        arguments = call["arguments"]
        if len(arguments) != len(definition.inputs):
            raise StrategyDefinitionError(
                "INVALID_ARGUMENTS",
                f"{definition.function_id}@{definition.version} expects {len(definition.inputs)} arguments",
            )
        for expected, operand in zip(definition.inputs, arguments, strict=True):
            actual = operand_type(operand)
            if not _input_matches(expected, actual):
                raise StrategyDefinitionError(
                    "TYPE_MISMATCH",
                    f"input {expected.name} expects {expected.value_type}, got {actual}",
                )
        return definition.output_type

    def operand_type(operand: Mapping[str, Any]) -> str:
        if operand.get("kind") == "function":
            return validate_call(operand["call"])
        return _operand_type(operand, parameters)

    def visit(node: Mapping[str, Any], depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if depth > MAX_RULE_DEPTH:
            raise StrategyDefinitionError("RULE_TOO_DEEP", f"rule depth exceeds {MAX_RULE_DEPTH}")
        if node_count > MAX_RULE_NODES:
            raise StrategyDefinitionError("TOO_MANY_RULES", f"rule nodes exceed {MAX_RULE_NODES}")
        if node["node_type"] == "group":
            for child in node["children"]:
                visit(child, depth + 1)
            return
        output_type = validate_call(node["left"])
        boolean_output = output_type in {"boolean", "series<boolean>"}
        has_comparator = "comparator" in node
        if boolean_output and has_comparator:
            raise StrategyDefinitionError("TYPE_MISMATCH", "boolean functions must not have a numeric comparator")
        if not boolean_output and not has_comparator:
            raise StrategyDefinitionError("TYPE_MISMATCH", "numeric functions require a comparator and right operand")
        if has_comparator:
            right_type = operand_type(node["right"])
            if right_type not in {"number", "integer", "series<number>"}:
                raise StrategyDefinitionError("TYPE_MISMATCH", "numeric comparison requires a numeric right operand")

    visit(document["entry_rules"], 1)
    visit(document["exit_rules"], 1)
    return {
        "valid": True,
        "strategy_id": document["id"],
        "version": document["version"],
        "function_references": [
            {"resource_kind": "strategy_function", "id": function_id, "version": version}
            for function_id, version in sorted(references)
        ],
        "rule_nodes": node_count,
    }


__all__ = ["MAX_RULE_DEPTH", "MAX_RULE_NODES", "StrategyDefinitionError", "validate_strategy_definition"]
