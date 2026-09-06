"""/api/strategy router: local Strategy DSL definitions, validation, scans and history.

Definitions live under ``data_control/strategies/definitions/*.json`` and run
records under ``data_control/strategies/runs/{run_id}.json``.  The router only
loads allow-listed local documents, never accepts inline DSL code for
execution, and reuses the shared ``strategy_dsl`` scanner/writer services.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
import csv
import base64
import hashlib
import io
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.builtin_strategies import build_builtin_strategy_definitions
from market_monitor.formula_engine import (
    FormulaError,
    validate_formula_document,
)
from market_monitor.formula_runtime import run_formula_strategy
from market_monitor.indicator_registry import (
    IndicatorResourceError,
    build_indicator_registry,
    build_builtin_indicator_registry,
    custom_indicator_definition,
)
from market_monitor.strategy_function_registry import (
    StrategyFunctionDefinition,
    StrategyFunctionRegistryError,
    build_builtin_strategy_function_registry,
)
from market_monitor.strategy_registry import StrategyRegistry, registration_from_document
from market_monitor.strategy_dsl import StrategyDslError, scan_strategy, validate_dsl, write_run_record
from market_monitor.strategy_definition import StrategyDefinitionError, validate_strategy_definition
from market_monitor.strategy_backtest import StrategyBacktestError, run_strategy_backtest
from market_monitor.strategy_execution import (
    FileExecutionAudit,
    FileExecutionStore,
    RiskContext,
    StrategyExecutionError,
    StrategyExecutionGateway,
)
from market_monitor.strategy_report import StrategyReportError, build_strategy_report
from market_monitor.strategy_transfer import StrategyTransferError, build_package, inspect_package
from market_monitor.strategy_templates import (
    StrategyTemplateError,
    build_strategy_templates,
    create_from_template,
)
from market_monitor.web_api.common import (
    bars_by_instrument,
    clean,
    load_inventory,
    load_json,
    now_iso,
    paginate,
    read_bars,
)
from market_monitor.market_data_version import market_data_version
from market_monitor.market_classification import (
    UNCLASSIFIED_CATEGORY,
    classify_market,
    matches_market_category,
)
from market_monitor.web_api.market import _logical_instruments

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

# 生产环境默认数据根目录；测试或其他宿主可通过 ``app.state.data_root`` 覆盖。
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_DATA_ROOT = _REPO_ROOT / "data_control"

_DEFINITION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DEFAULT_HISTORY_LIMIT = 50
_MAX_HISTORY_LIMIT = 200
_MAX_SIGNALS_PER_INSTRUMENT = 50


def _camel_key(key: str) -> str:
    head, *parts = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in parts)


def _camel_keys(value: Any) -> Any:
    """Recursively convert snake_case dict keys to camelCase for JSON responses."""
    if isinstance(value, dict):
        return {_camel_key(str(key)): _camel_keys(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_camel_keys(item) for item in value]
    return value


def _document_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _data_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_root", None)
    if configured:
        return Path(configured)
    return _DEFAULT_DATA_ROOT


def _definitions_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "definitions"


def _definition_resources_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "resources"


def _indicator_resources_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "indicator_resources"


def _resource_migrations_dir(data_root: Path) -> Path:
    return _definition_resources_dir(data_root) / "migrations"


def _custom_indicator_resource_documents(data_root: Path) -> list[dict[str, Any]]:
    """Return valid local Indicator resources without trusting malformed files."""

    directory = _indicator_resources_dir(data_root)
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*@*.json")):
        document = load_json(path, default=None)
        if not isinstance(document, dict):
            continue
        try:
            custom_indicator_definition(document)
        except IndicatorResourceError:
            continue
        result.append(document)
    return result


def _indicator_registry(data_root: Path):
    return build_indicator_registry(_custom_indicator_resource_documents(data_root))


def _indicator_resource_ids(data_root: Path) -> set[str]:
    return {item.indicator_id for item in _indicator_registry(data_root).list()}


def _safe_indicator_id(existing_ids: set[str]) -> str:
    candidate = f"indicator.user.{uuid.uuid4().hex[:12]}"
    while candidate in existing_ids:
        candidate = f"indicator.user.{uuid.uuid4().hex[:12]}"
    return candidate


def _indicator_document_from_template(
    source: Any,
    *,
    indicator_id: str,
    display_name: str,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    """Build the only custom Indicator shape accepted by the registry.

    The client may later change display metadata, supported assets and defaults,
    but the calculation template, function dependencies and plots are copied
    exactly from the selected published definition.
    """

    return {
        "schema_version": 1,
        "resource_kind": "indicator",
        "id": indicator_id,
        "version": 1,
        "display_name": display_name,
        "origin": "custom",
        "supported_asset_types": list(source.supported_asset_types),
        "status": "active",
        "created_at": created_at,
        "updated_at": updated_at,
        "dependencies": [
            {"resource_kind": "strategy_function", "id": function_id, "version": version}
            for function_id, version in source.dependencies
        ],
        "capabilities": ["market_data_input", "plot_create"],
        "definition": {
            "english_name": source.english_name,
            "category": source.category,
            "category_label": source.category_label,
            "description": source.description,
            "placement": source.placement,
            "parameters": [dict(item) for item in source.parameters],
            "plots": [dict(item) for item in source.plots],
            "calculation_id": source.calculation_id or source.indicator_id,
        },
    }


def _validate_custom_indicator(document: Mapping[str, Any]):
    try:
        return custom_indicator_definition(document)
    except IndicatorResourceError as error:
        raise HTTPException(status_code=422, detail={"code": "INVALID_INDICATOR_RESOURCE", "message": str(error)}) from error


def _next_indicator_version(data_root: Path, indicator_id: str) -> int:
    versions = [
        int(item["version"])
        for item in _custom_indicator_resource_documents(data_root)
        if item["id"] == indicator_id
    ]
    return max(versions, default=0) + 1


def _latest_custom_indicator_resource(data_root: Path, indicator_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in _custom_indicator_resource_documents(data_root)
        if item["id"] == indicator_id
    ]
    if not matches:
        raise HTTPException(status_code=404, detail={"code": "CUSTOM_INDICATOR_NOT_FOUND"})
    return max(matches, key=lambda item: int(item["version"]))


def _runs_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "runs"


def _backtests_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "backtests"


def _metadata_path(data_root: Path) -> Path:
    return data_root / "strategies" / "metadata.json"


def _read_metadata(data_root: Path) -> dict[str, dict[str, Any]]:
    payload = load_json(_metadata_path(data_root), default={})
    if not isinstance(payload, dict):
        return {}
    items = payload.get("items", payload)
    if not isinstance(items, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for strategy_id, value in items.items():
        normalized_id = str(strategy_id)
        if not isinstance(value, dict) or not _DEFINITION_NAME.fullmatch(normalized_id):
            continue
        result[normalized_id] = {
            "displayName": str(value.get("displayName") or normalized_id),
            "markColorId": str(value.get("markColorId") or ""),
            "origin": str(value.get("origin") or "custom"),
            "status": str(value.get("status") or "active"),
            "runMode": str(value.get("runMode") or "backtest"),
            "backtestStatus": str(value.get("backtestStatus") or "not_run"),
            "category": str(value.get("category") or "custom"),
            "createdAt": str(value.get("createdAt") or ""),
        }
    return result


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _save_metadata(data_root: Path, metadata: dict[str, dict[str, Any]]) -> None:
    _atomic_json(_metadata_path(data_root), {"schemaVersion": 1, "items": metadata})


def _safe_strategy_id(display_name: str, existing_ids: set[str]) -> str:
    del display_name
    candidate = f"user_{uuid.uuid4().hex[:12]}"
    while candidate in existing_ids:
        candidate = f"user_{uuid.uuid4().hex[:12]}"
    return candidate


def _definition_items(data_root: Path) -> list[dict[str, Any]]:
    directory = _definitions_dir(data_root)
    if not directory.is_dir():
        return []
    metadata = _read_metadata(data_root)
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        document = load_json(path, default=None)
        if not isinstance(document, dict) or not str(document.get("strategy_id") or ""):
            continue
        try:
            stat = path.stat()
            updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError:
            updated_at = now_iso()
            created_at = updated_at
        strategy_id = str(document.get("strategy_id") or "")
        ui = metadata.get(strategy_id, {})
        items.append(registration_from_document(document, ui, created_at=created_at, updated_at=updated_at).to_public_dict())
    return items


def _load_definition(data_root: Path, strategy_id: str) -> dict[str, Any]:
    strategy_id = strategy_id.strip()
    if not _DEFINITION_NAME.fullmatch(strategy_id):
        raise HTTPException(status_code=400, detail="invalid strategy id")
    path = _definitions_dir(data_root) / f"{strategy_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="strategy definition not found")
    document = load_json(path, default=None)
    if not isinstance(document, dict):
        raise HTTPException(status_code=404, detail="strategy definition is not valid JSON")
    return document


class StrategyRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyId: str
    parameters: dict[str, int | float | bool] = Field(default_factory=dict)
    period: str | None = None
    limitInstruments: int = Field(default=200, ge=1, le=1000)
    limitPerInstrument: int = Field(default=500, ge=1, le=5000)
    timeoutSeconds: float = Field(default=2.0, gt=0.0, le=30.0)
    maxOps: int = Field(default=500_000, ge=1000, le=5_000_000)


class StrategyMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=4000)
    scriptKind: Literal["dsl_v1", "formula_v1", "builder_v1", "python_safe_v1"] = "dsl_v1"
    script: dict[str, Any]


class StrategyMarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markColorId: str | None = Field(default=None, pattern=r"^strategy-mark-(0[1-9]|1[0-9]|20)$")


class StrategyStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled", "deprecated"]


class StrategyCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str | None = Field(default=None, min_length=1, max_length=64)


class IndicatorCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str | None = Field(default=None, min_length=1, max_length=128)


class StrategyDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmDisplayName: str = Field(min_length=1, max_length=64)


class StrategyPackageImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packageBase64: str = Field(min_length=1, max_length=8_000_000)
    conflict: Literal["cancel", "rename", "new_version"] = "cancel"
    newStrategyId: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class StrategyTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str | None = Field(default=None, min_length=1, max_length=128)


class StrategyMatchesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyIds: list[str] = Field(default_factory=list, max_length=40)
    allStrategies: bool = False
    categoryKeys: list[str] = Field(default_factory=list, max_length=20)
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=24, ge=1, le=100)


class StrategyOrderIntentRequest(BaseModel):
    """Client facts only; resource kind and capability are always server-derived."""

    model_config = ConfigDict(extra="forbid")
    strategyId: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    strategyVersion: int = Field(ge=1)
    intentId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    runMode: Literal["backtest", "paper", "live"]
    instrumentId: str = Field(min_length=1, max_length=160)
    side: Literal["buy", "sell"]
    positionEffect: Literal["open", "close"]
    orderType: Literal["market", "limit", "stop"] = "market"
    quantity: float = Field(gt=0)
    signalTime: str = Field(min_length=1, max_length=64)
    limitPrice: float | None = Field(default=None, gt=0)
    stopPrice: float | None = Field(default=None, gt=0)
    accountEquity: float = Field(gt=0)
    referencePrice: float = Field(gt=0)
    currentPositionQuantity: float = 0.0
    contractMultiplier: float = Field(default=1.0, gt=0)
    drawdownPercent: float = Field(default=0.0, ge=0)


class StrategyBacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyId: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$", max_length=128)
    strategyVersion: int = Field(ge=1)
    instrumentId: str = Field(min_length=1, max_length=160)
    parameters: dict[str, int | float | bool | str] = Field(default_factory=dict)
    limit: int = Field(default=5000, ge=1, le=100_000)
    contractMultiplier: float = Field(default=1.0, gt=0)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3,8}$")


_BUILDER_CALLS: dict[str, tuple[str, ...]] = {
    "period_return": ("close",), "no_limit_up": ("limit_up",), "no_limit_down": ("limit_down",),
    "limit_up_count": ("limit_up",), "limit_down_count": ("limit_down",),
    "close_new_high": ("close", "high"), "close_new_low": ("close", "low"),
    "up_count": ("open", "close"), "down_count": ("open", "close"),
    "up_down_ratio": ("open", "close"), "down_up_ratio": ("open", "close"),
    "range_high_low_ratio": ("high", "low"), "range_low_high_ratio": ("high", "low"),
    "volume_slope": ("volume",), "gann_rising_rate": ("low",), "gann_falling_rate": ("high",),
    "hsar_resistance": ("high",), "hsar_support": ("low",),
}
_BUILDER_BOOLEAN = {"no_limit_up", "no_limit_down", "close_new_high", "close_new_low"}
_BUILDER_OPERATORS = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "==", "ne": "!="}
_ALL_MARKET_TYPES = ["a_share", "hk_stock", "main_board", "chinext", "star", "etf", "bse", "cn_future", "cn_commodity_index", "global_future"]


def _function_references(data_root: Path, definitions: list[StrategyFunctionDefinition]) -> dict[str, dict[str, set[str]]]:
    """Resolve local indicator/strategy references without executing definitions."""
    references = {
        item.function_id: {"indicators": set(), "strategies": set()}
        for item in definitions
    }
    for indicator in _indicator_registry(data_root).list():
        for function_id, _version in indicator.dependencies:
            if function_id in references:
                references[function_id]["indicators"].add(indicator.indicator_id)

    def contains(value: Any, names: tuple[str, ...]) -> set[str]:
        if isinstance(value, dict):
            found = {str(value.get("functionId") or "")} & set(names)
            for nested in value.values():
                found.update(contains(nested, names))
            return found
        if isinstance(value, list):
            found: set[str] = set()
            for nested in value:
                found.update(contains(nested, names))
            return found
        if isinstance(value, str):
            return {name for name in names if re.search(rf"\b{re.escape(name)}\s*\(", value)}
        return set()

    runtime_names = tuple(item.runtime_name for item in definitions)
    by_runtime_name = {item.runtime_name: item.function_id for item in definitions}
    directory = _definitions_dir(data_root)
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            document = load_json(path, default=None)
            if not isinstance(document, dict):
                continue
            strategy_id = str(document.get("strategy_id") or path.stem)
            for runtime_name in contains(document, runtime_names):
                references[by_runtime_name[runtime_name]]["strategies"].add(strategy_id)
    return references


def _public_function_catalog(data_root: Path) -> list[dict[str, Any]]:
    registry = build_builtin_strategy_function_registry()
    definitions = list(registry.list())
    references = _function_references(data_root, definitions)
    return [item.to_public_dict(references[item.function_id]) for item in definitions]


def _builder_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise FormulaError("INVALID_PARAMETER", "函数参数必须为有限数值")
        return repr(value)
    raise FormulaError("INVALID_PARAMETER", "可视化函数参数仅支持数字或布尔值")


def _builder_condition(node: Any, universe: dict[str, Any]) -> str:
    if not isinstance(node, dict):
        raise FormulaError("INVALID_BUILDER", "策略条件节点必须是对象")
    if "children" in node:
        operator = str(node.get("operator") or "and").lower()
        if operator not in {"and", "or"}:
            raise FormulaError("INVALID_BUILDER", "条件组只支持 AND 或 OR")
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise FormulaError("INVALID_BUILDER", "条件组至少需要一个条件")
        expressions = [_builder_condition(item, universe) for item in children]
        return f"({' {} '.format(operator).join(f'({item})' for item in expressions)})"
    function_id = str(node.get("functionId") or "")
    if function_id == "market_scope":
        values = node.get("marketTypes") or node.get("args") or []
        if not isinstance(values, list) or not values or any(str(value) not in _ALL_MARKET_TYPES for value in values):
            raise FormulaError("INVALID_UNIVERSE", "市场范围必须选择有效市场类型")
        universe["market_types"] = [str(value) for value in values]
        return "True"
    if function_id == "market_cap":
        field = str(node.get("field") or "total_market_cap_yi")
        if field not in {"total_market_cap_yi", "float_market_cap_yi"}:
            raise FormulaError("INVALID_UNIVERSE", "市值字段无效")
        operator = str(node.get("operator") or "gt")
        if operator not in {"gt", "lt"}:
            raise FormulaError("INVALID_UNIVERSE", "市值比较只支持大于或小于")
        value = float(node.get("value"))
        if not math.isfinite(value) or value <= 0:
            raise FormulaError("INVALID_UNIVERSE", "市值阈值必须为正数")
        universe[field] = {"operator": operator, "value": value}
        return "True"
    bases = _BUILDER_CALLS.get(function_id)
    if bases is None:
        raise FormulaError("UNKNOWN_FUNCTION", f"未知策略函数：{function_id}")
    args = node.get("args", [])
    if isinstance(args, dict):
        args = list(args.values())
    if not isinstance(args, list):
        raise FormulaError("INVALID_PARAMETER", "函数参数必须是数组")
    call = f"{function_id}({', '.join([*bases, *(_builder_literal(value) for value in args)])})"
    if function_id in _BUILDER_BOOLEAN and node.get("operator") is None:
        return call
    operator = _BUILDER_OPERATORS.get(str(node.get("operator") or "gt"))
    if operator is None:
        raise FormulaError("INVALID_BUILDER", "比较符无效")
    return f"{call} {operator} {_builder_literal(node.get('value'))}"


def _builder_document(script: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    period = str(script.get("period") or "").strip()
    if not period:
        raise FormulaError("INVALID_PERIOD", "每个策略必须指定 K 线周期")
    universe = dict(script.get("universe") or {})
    tree = script.get("conditionTree")
    expression = _builder_condition(tree, universe)
    universe.setdefault("market_types", _ALL_MARKET_TYPES)
    document = {
        "strategy_id": strategy_id, "description": str(script.get("description") or ""), "script_kind": "formula_v1",
        "condition_kind": "builder_v1", "formula_version": 1, "period": period, "universe": universe,
        "parameters": {}, "expression": f"value = 1\nsignal = {expression}", "condition_tree": tree,
    }
    validate_formula_document(document)
    return document


def _safe_python_document(script: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    expression = str(script.get("expression") or script.get("source") or "").strip()
    if "signal" not in expression:
        raise FormulaError("MISSING_OUTPUT", "安全 Python 条件必须赋值 signal")
    if "value" not in expression:
        expression = f"value = 1\n{expression}"
    document = {
        "strategy_id": strategy_id, "description": str(script.get("description") or ""), "script_kind": "formula_v1",
        "condition_kind": "python_safe_v1", "formula_version": 1, "period": str(script.get("period") or ""),
        "universe": dict(script.get("universe") or {"market_types": _ALL_MARKET_TYPES}),
        "parameters": dict(script.get("parameters") or {}), "expression": expression, "source": expression,
    }
    validate_formula_document(document)
    return document


def _normalized_document(body: StrategyMutationRequest, strategy_id: str) -> dict[str, Any]:
    document = dict(body.script)
    document["strategy_id"] = strategy_id
    document["description"] = body.description.strip()
    if body.scriptKind == "builder_v1":
        try:
            document = _builder_document(document, strategy_id)
        except (FormulaError, ValueError, TypeError) as error:
            detail = error.to_dict() if isinstance(error, FormulaError) else str(error)
            raise HTTPException(status_code=400, detail=detail) from error
        document["description"] = body.description.strip()
        return document
    if body.scriptKind == "python_safe_v1":
        try:
            document = _safe_python_document(document, strategy_id)
        except FormulaError as error:
            raise HTTPException(status_code=400, detail=error.to_dict()) from error
        document["description"] = body.description.strip()
        return document
    if body.scriptKind == "formula_v1":
        document["script_kind"] = "formula_v1"
        try:
            validate_formula_document(document)
        except FormulaError as error:
            raise HTTPException(status_code=400, detail=error.to_dict()) from error
        return document
    document.pop("script_kind", None)
    try:
        validate_dsl(document)
    except StrategyDslError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error
    return document


def _definition_response(data_root: Path, strategy_id: str) -> dict[str, Any]:
    document = _load_definition(data_root, strategy_id)
    summary = next(item for item in _definition_items(data_root) if item["strategyId"] == strategy_id)
    return clean({**summary, "script": document})


@router.get("/definitions")
def strategy_definitions(
    request: Request,
    q: str | None = Query(default=None, max_length=128),
    category: str | None = Query(default=None, max_length=64),
    asset_type: str | None = Query(default=None, alias="assetType", max_length=32),
    origin: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    registrations = [
        registration_from_document(
            _load_definition(_data_root(request), item["strategyId"]),
            item,
            created_at=item["createdAt"],
            updated_at=item["updatedAt"],
        )
        for item in _definition_items(_data_root(request))
    ]
    items = [item.to_public_dict() for item in StrategyRegistry(registrations).list(query=q, category=category, asset_type=asset_type, origin=origin, status=status)]
    latest_resources: dict[str, dict[str, Any]] = {}
    for document in _strategy_resource_documents(_data_root(request)):
        current = latest_resources.get(str(document["id"]))
        if current is None or int(document["version"]) > int(current["version"]):
            latest_resources[str(document["id"])] = document
    for document in latest_resources.values():
        searchable = f"{document['id']} {document['display_name']} {document.get('description', '')}".casefold()
        if q and q.casefold().strip() not in searchable:
            continue
        if category and category != "structured":
            continue
        if asset_type and asset_type.upper().strip() not in document["supported_asset_types"]:
            continue
        if origin and document["origin"] != origin:
            continue
        if status and document["status"] != status:
            continue
        items.append(
            clean(
                {
                    "resourceKind": "strategy",
                    "strategyId": document["id"],
                    "id": document["id"],
                    "version": document["version"],
                    "versionedId": f"{document['id']}@{document['version']}",
                    "strategyVersion": str(document["version"]),
                    "displayName": document["display_name"],
                    "description": document.get("description", ""),
                    "category": "structured",
                    "origin": document["origin"],
                    "status": document["status"],
                    "enabled": document["status"] == "active",
                    "runMode": document["execution"]["run_mode"],
                    "availableRunModes": [
                        {"id": "backtest", "enabled": True},
                        {"id": "paper", "enabled": True},
                        {"id": "live", "enabled": False, "reason": "实盘交易接口尚未配置"},
                    ],
                    "backtestStatus": "not_run",
                    "scriptKind": "structured_v1",
                    "baseTimeframe": document["base_timeframe"],
                    "supportedAssetTypes": document["supported_asset_types"],
                    "inputs": [],
                    "parameters": document["parameters"],
                    "createdAt": document["created_at"],
                    "updatedAt": document["updated_at"],
                }
            )
        )
    items.sort(key=lambda item: str(item["updatedAt"]), reverse=True)
    return clean({"items": items, "total": len(items)})


@router.get("/indicators")
def strategy_indicators(
    request: Request,
    q: str | None = Query(default=None, max_length=128),
    category: str | None = Query(default=None, max_length=64),
    asset_type: str | None = Query(default=None, alias="assetType", max_length=32),
    origin: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    """Return versioned chart-only indicators with lazy catalog filters."""
    registry = _indicator_registry(_data_root(request))
    definitions = registry.list(query=q, category=category, asset_type=asset_type, origin=origin)
    items = [item.to_public_dict() for item in definitions]
    categories = [
        {"id": category_id, "name": category_name}
        for category_id, category_name in sorted({(item.category, item.category_label) for item in definitions})
    ]
    return clean({"items": items, "total": len(items), "categories": categories})


@router.get("/indicators/{indicator_id}")
def strategy_indicator_detail(
    indicator_id: str,
    request: Request,
    version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    try:
        definition = _indicator_registry(_data_root(request)).resolve(indicator_id, version)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={"code": "INDICATOR_NOT_FOUND"}) from error
    return clean(definition.to_public_dict())


@router.post("/indicators/{indicator_id}/copy", status_code=201)
def strategy_indicator_copy(
    indicator_id: str,
    request: Request,
    body: IndicatorCopyRequest,
) -> dict[str, Any]:
    """Create a safe custom copy of a published chart Indicator template."""

    data_root = _data_root(request)
    try:
        source = _indicator_registry(data_root).resolve(indicator_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={"code": "INDICATOR_NOT_FOUND"}) from error
    if source.resource_kind != "indicator" or source.status == "disabled":
        raise HTTPException(status_code=422, detail={"code": "INDICATOR_NOT_COPYABLE"})
    copied_id = _safe_indicator_id(_indicator_resource_ids(data_root))
    timestamp = now_iso()
    document = _indicator_document_from_template(
        source,
        indicator_id=copied_id,
        display_name=body.displayName or f"{source.name} 副本",
        created_at=timestamp,
        updated_at=timestamp,
    )
    definition = _validate_custom_indicator(document)
    _atomic_json(_indicator_resources_dir(data_root) / f"{copied_id}@1.json", document)
    return clean(definition.to_public_dict())


@router.post("/indicator-resources", status_code=201)
def strategy_indicator_resource_create(request: Request, document: dict[str, Any]) -> dict[str, Any]:
    """Persist a validated custom Indicator version; formulas remain template-bound."""

    definition = _validate_custom_indicator(document)
    data_root = _data_root(request)
    builtin_ids = {item.indicator_id for item in build_builtin_indicator_registry().list()}
    if definition.indicator_id in builtin_ids:
        raise HTTPException(status_code=409, detail={"code": "BUILTIN_ID_RESERVED"})
    target = _indicator_resources_dir(data_root) / f"{definition.indicator_id}@{definition.version}.json"
    if target.exists():
        raise HTTPException(status_code=409, detail={"code": "VERSION_EXISTS"})
    expected = _next_indicator_version(data_root, definition.indicator_id)
    if definition.version != expected:
        raise HTTPException(
            status_code=409,
            detail={"code": "VERSION_SEQUENCE", "message": f"next version must be {expected}"},
        )
    _atomic_json(target, document)
    return clean(definition.to_public_dict())


@router.put("/indicator-resources/{indicator_id}")
def strategy_indicator_resource_update(
    indicator_id: str,
    request: Request,
    document: dict[str, Any],
) -> dict[str, Any]:
    """Append the next immutable version of one custom Indicator."""

    definition = _validate_custom_indicator(document)
    if definition.indicator_id != indicator_id:
        raise HTTPException(status_code=400, detail={"code": "ID_MISMATCH"})
    data_root = _data_root(request)
    latest = _latest_custom_indicator_resource(data_root, indicator_id)
    if definition.created_at != latest["created_at"]:
        raise HTTPException(status_code=422, detail={"code": "CREATED_AT_IMMUTABLE"})
    expected = int(latest["version"]) + 1
    if definition.version != expected:
        raise HTTPException(
            status_code=409,
            detail={"code": "VERSION_SEQUENCE", "message": f"next version must be {expected}"},
        )
    target = _indicator_resources_dir(data_root) / f"{indicator_id}@{definition.version}.json"
    if target.exists():
        raise HTTPException(status_code=409, detail={"code": "VERSION_EXISTS"})
    _atomic_json(target, document)
    return clean(definition.to_public_dict())


@router.delete("/indicator-resources/{indicator_id}")
def strategy_indicator_resource_delete(indicator_id: str, request: Request) -> Response:
    """Remove all local versions of one custom Indicator, never built-ins."""

    data_root = _data_root(request)
    documents = _custom_indicator_resource_documents(data_root)
    matches = [item for item in documents if item["id"] == indicator_id]
    if not matches:
        raise HTTPException(status_code=404, detail={"code": "CUSTOM_INDICATOR_NOT_FOUND"})
    for document in matches:
        path = _indicator_resources_dir(data_root) / f"{document['id']}@{document['version']}.json"
        if path.is_file():
            path.unlink()
    return Response(status_code=204)


@router.get("/conditions")
def strategy_conditions(request: Request) -> dict[str, Any]:
    items = _public_function_catalog(_data_root(request))
    return clean({"items": items, "total": len(items)})


@router.get("/functions")
def strategy_functions(
    request: Request,
    q: str | None = Query(default=None, max_length=128),
    category: str | None = Query(default=None, max_length=64),
    asset_type: str | None = Query(default=None, alias="assetType", max_length=32),
    version: int | None = Query(default=None, ge=1),
    include_deprecated: bool = Query(default=False, alias="includeDeprecated"),
) -> dict[str, Any]:
    """Search the versioned pure-function registry used by strategy editors."""
    registry = build_builtin_strategy_function_registry()
    definitions = list(
        registry.list(
            query=q,
            category=category,
            asset_type=asset_type,
            version=version,
            include_deprecated=include_deprecated,
        )
    )
    references = _function_references(_data_root(request), definitions)
    items = [item.to_public_dict(references[item.function_id]) for item in definitions]
    categories = [
        {"id": category_id, "name": category_name}
        for category_id, category_name in sorted({(item.category, item.category_label) for item in definitions})
    ]
    return clean({"items": items, "total": len(items), "runtime": "python_safe", "categories": categories})


@router.get("/functions/{function_id}")
def strategy_function_detail(
    request: Request,
    function_id: str,
    version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Return one exact version, including indicators and strategies that reference it."""
    registry = build_builtin_strategy_function_registry()
    try:
        definition = registry.resolve(function_id, version)
    except StrategyFunctionRegistryError as error:
        raise HTTPException(status_code=404, detail=error.to_dict()) from error
    references = _function_references(_data_root(request), [definition])
    return clean(definition.to_public_dict(references[definition.function_id]))


@router.post("/formula/validate")
def strategy_formula_validate(document: dict[str, Any]) -> dict[str, Any]:
    try:
        program = validate_formula_document(document)
    except FormulaError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error
    return clean({"valid": True, "scriptKind": "formula_v1", "dependencies": sorted(program.dependencies)})


@router.post("/condition/validate")
def strategy_condition_validate(document: dict[str, Any]) -> dict[str, Any]:
    """Validate, but do not persist, a new visual or safe-Python condition."""
    kind = str(document.get("conditionKind") or document.get("scriptKind") or "")
    script = document.get("script") if isinstance(document.get("script"), dict) else document
    try:
        if kind == "builder_v1":
            normalized = _builder_document(dict(script), "preview")
        elif kind == "python_safe_v1":
            normalized = _safe_python_document(dict(script), "preview")
        else:
            raise FormulaError("INVALID_KIND", "策略条件必须为 builder_v1 或 python_safe_v1")
    except FormulaError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error
    return clean({"valid": True, "conditionKind": kind, "expression": normalized["expression"],
                  "dependencies": sorted(validate_formula_document(normalized).dependencies)})


@router.post("/definition/validate")
def strategy_definition_validate(document: dict[str, Any]) -> dict[str, Any]:
    """Validate the authoritative versioned Rule AST without executing it."""
    try:
        return clean(validate_strategy_definition(document))
    except StrategyDefinitionError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error


def _strategy_resource_documents(data_root: Path) -> list[dict[str, Any]]:
    directory = _definition_resources_dir(data_root)
    result = list(build_builtin_strategy_definitions())
    if directory.is_dir():
        for path in sorted(directory.glob("*@*.json")):
            document = load_json(path, default=None)
            if not isinstance(document, dict):
                continue
            try:
                validate_strategy_definition(document)
            except StrategyDefinitionError:
                continue
            result.append(document)
    return result


def _strategy_execution_gateway(data_root: Path) -> StrategyExecutionGateway:
    execution_root = data_root / "strategies" / "execution"
    return StrategyExecutionGateway(
        store=FileExecutionStore(execution_root / "intents"),
        audit=FileExecutionAudit(execution_root / "audit"),
    )


def _exact_strategy_resource(data_root: Path, strategy_id: str, version: int) -> dict[str, Any]:
    matches = [
        item
        for item in _strategy_resource_documents(data_root)
        if item["id"] == strategy_id and int(item["version"]) == version
    ]
    if not matches:
        raise HTTPException(status_code=404, detail={"code": "STRATEGY_VERSION_NOT_FOUND"})
    return matches[0]


def _assert_strategy_source_executable(definition: Mapping[str, Any]) -> None:
    """Community/plugin metadata is display-only until a future approved ADR."""

    if definition.get("origin") in {"community", "plugin"}:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "UNTRUSTED_ORIGIN",
                "message": "社区或插件策略当前仅可查看，尚未完成审核与执行授权",
            },
        )


@router.get("/execution/capabilities")
def strategy_execution_capabilities(request: Request) -> dict[str, Any]:
    """Expose read-only server authority; Live remains disabled without an accepted adapter."""

    return clean(_strategy_execution_gateway(_data_root(request)).capabilities())


@router.post("/order-intents")
def strategy_order_intent_dispatch(request: Request, body: StrategyOrderIntentRequest) -> dict[str, Any]:
    """Risk-check and submit one intent to an isolated non-Live adapter.

    The strategy resource kind, capability, exact definition and adapter are
    all selected by the server.  The request cannot name or bypass them.
    """

    if body.runMode == "live":
        raise HTTPException(
            status_code=409,
            detail={"code": "LIVE_DISABLED", "message": "实盘执行适配器尚未配置并验收"},
        )
    data_root = _data_root(request)
    definition = _exact_strategy_resource(data_root, body.strategyId, body.strategyVersion)
    _assert_strategy_source_executable(definition)
    gateway = _strategy_execution_gateway(data_root)
    try:
        outcome = gateway.dispatch(
            definition,
            RiskContext(
                account_equity=body.accountEquity,
                reference_price=body.referencePrice,
                current_position_quantity=body.currentPositionQuantity,
                contract_multiplier=body.contractMultiplier,
                drawdown_percent=body.drawdownPercent,
            ),
            instrument_id=body.instrumentId,
            side=body.side,
            position_effect=body.positionEffect,
            order_type=body.orderType,
            quantity=body.quantity,
            signal_time=body.signalTime,
            run_mode=body.runMode,
            intent_id=body.intentId,
            limit_price=body.limitPrice,
            stop_price=body.stopPrice,
        )
    except StrategyExecutionError as error:
        raise HTTPException(status_code=error.status_code, detail=error.to_dict()) from error
    return clean(_camel_keys(outcome.to_dict()))


@router.post("/backtests")
def strategy_backtest_run(request: Request, body: StrategyBacktestRequest) -> dict[str, Any]:
    """Run one exact Strategy Definition against one local Silver series."""

    data_root = _data_root(request)
    definition = _exact_strategy_resource(data_root, body.strategyId, body.strategyVersion)
    _assert_strategy_source_executable(definition)
    source_bars = read_bars(
        data_root,
        body.instrumentId,
        period=str(definition["base_timeframe"]),
        limit=body.limit,
    )
    try:
        result = run_strategy_backtest(
            definition,
            source_bars,
            instrument_id=body.instrumentId,
            data_version=market_data_version(data_root),
            parameters=body.parameters,
            contract_multiplier=body.contractMultiplier,
            currency=body.currency,
            data_query={"period": str(definition["base_timeframe"]), "limit": body.limit},
        )
    except StrategyBacktestError as error:
        raise HTTPException(status_code=422, detail=error.to_dict()) from error
    try:
        result["report"] = build_strategy_report(result)
    except StrategyReportError as error:
        raise HTTPException(
            status_code=500,
            detail={"code": "INVALID_STRATEGY_REPORT", "message": str(error)},
        ) from error
    _atomic_json(_backtests_dir(data_root) / f"{result['run_id']}.json", result)
    return clean(_camel_keys(result))


def _backtest_result_or_404(data_root: Path, run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"bt_[0-9a-f]{24}", run_id):
        raise HTTPException(status_code=400, detail={"code": "INVALID_BACKTEST_RUN_ID"})
    result = load_json(_backtests_dir(data_root) / f"{run_id}.json", default=None)
    if not isinstance(result, dict):
        raise HTTPException(status_code=404, detail={"code": "BACKTEST_NOT_FOUND"})
    return result


def _locked_definition_for_reproduction(data_root: Path, result: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve only the exact resources named in a persisted dependency lock."""

    lock = result.get("dependency_lock")
    if not isinstance(lock, dict):
        raise HTTPException(status_code=409, detail={"code": "LEGACY_BACKTEST_NO_DEPENDENCY_LOCK"})
    strategy_lock = lock.get("strategy")
    if not isinstance(strategy_lock, dict):
        raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
    strategy_id = str(strategy_lock.get("id") or "")
    version = strategy_lock.get("version")
    if not isinstance(version, int):
        raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
    try:
        definition = _exact_strategy_resource(data_root, strategy_id, version)
    except HTTPException as error:
        if error.status_code == 404:
            raise HTTPException(
                status_code=409,
                detail={"code": "MISSING_STRATEGY_VERSION", "id": strategy_id, "version": version},
            ) from error
        raise
    if _document_hash(definition) != strategy_lock.get("definition_hash"):
        raise HTTPException(status_code=409, detail={"code": "DEFINITION_HASH_MISMATCH"})

    registry = build_builtin_strategy_function_registry()
    functions = lock.get("strategy_functions")
    if not isinstance(functions, list):
        raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
    for function_lock in functions:
        if not isinstance(function_lock, dict):
            raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
        function_id = str(function_lock.get("id") or "")
        function_version = function_lock.get("version")
        if not isinstance(function_version, int):
            raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
        try:
            function = registry.resolve(function_id, function_version)
        except StrategyFunctionRegistryError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "MISSING_FUNCTION_VERSION", "id": function_id, "version": function_version},
            ) from error
        if function.definition_hash() != function_lock.get("definition_hash"):
            raise HTTPException(
                status_code=409,
                detail={"code": "FUNCTION_DEFINITION_HASH_MISMATCH", "id": function_id, "version": function_version},
            )
    # Current Strategy Rule AST intentionally cannot invoke chart Indicators.
    # A future nonempty list needs an exact resource resolver before replay.
    if lock.get("indicators") != []:
        raise HTTPException(status_code=409, detail={"code": "UNSUPPORTED_INDICATOR_DEPENDENCY_LOCK"})
    return definition, lock


@router.post("/backtests/{run_id}/reproduce")
def strategy_backtest_reproduce(request: Request, run_id: str) -> dict[str, Any]:
    """Re-run one historical record only when every locked dependency remains exact."""

    data_root = _data_root(request)
    stored = _backtest_result_or_404(data_root, run_id)
    definition, lock = _locked_definition_for_reproduction(data_root, stored)
    market_lock = lock.get("market_data")
    parameters_lock = lock.get("parameters")
    if not isinstance(market_lock, dict) or not isinstance(parameters_lock, dict):
        raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
    query = market_lock.get("query")
    query = query if isinstance(query, dict) else {}
    period = str(query.get("period") or definition["base_timeframe"])
    limit = query.get("limit")
    if not isinstance(limit, int) or limit < 1:
        raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
    instrument_id = str(market_lock.get("instrument_id") or "")
    if not instrument_id:
        raise HTTPException(status_code=409, detail={"code": "INVALID_DEPENDENCY_LOCK"})
    current_bars = read_bars(data_root, instrument_id, period=period, limit=limit)
    try:
        reproduced = run_strategy_backtest(
            definition,
            current_bars,
            instrument_id=instrument_id,
            data_version=market_data_version(data_root),
            parameters=dict(parameters_lock.get("values") or {}),
            contract_multiplier=float(stored["contract_multiplier"]),
            currency=str(stored["currency"]),
            data_query={"period": period, "limit": limit},
        )
    except StrategyBacktestError as error:
        raise HTTPException(status_code=422, detail=error.to_dict()) from error
    if reproduced["data_fingerprint"] != market_lock.get("data_fingerprint"):
        raise HTTPException(
            status_code=409,
            detail={"code": "DATA_VERSION_MISMATCH", "message": "历史行情版本已变化，拒绝伪造复现结果"},
        )
    if reproduced["version_fingerprint"] != stored.get("version_fingerprint"):
        raise HTTPException(status_code=409, detail={"code": "REPRODUCTION_HASH_MISMATCH"})
    try:
        reproduced["report"] = build_strategy_report(reproduced)
    except StrategyReportError as error:
        raise HTTPException(status_code=500, detail={"code": "INVALID_STRATEGY_REPORT"}) from error
    return clean({"reproduced": True, "result": _camel_keys(reproduced)})


@router.get("/backtests/{run_id}")
def strategy_backtest_result(request: Request, run_id: str) -> dict[str, Any]:
    return clean(_camel_keys(_backtest_result_or_404(_data_root(request), run_id)))


@router.get("/backtests/{run_id}/report")
def strategy_backtest_report(request: Request, run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"bt_[0-9a-f]{24}", run_id):
        raise HTTPException(status_code=400, detail={"code": "INVALID_BACKTEST_RUN_ID"})
    result = load_json(_backtests_dir(_data_root(request)) / f"{run_id}.json", default=None)
    if not isinstance(result, dict):
        raise HTTPException(status_code=404, detail={"code": "BACKTEST_NOT_FOUND"})
    report = result.get("report")
    if not isinstance(report, dict):
        try:
            report = build_strategy_report(result)
        except (KeyError, TypeError, ValueError, StrategyReportError) as error:
            raise HTTPException(status_code=422, detail={"code": "REPORT_UNAVAILABLE"}) from error
    return clean(_camel_keys(report))


@router.get("/backtests/{run_id}/trades.csv")
def strategy_backtest_trade_export(request: Request, run_id: str) -> Response:
    report = strategy_backtest_report(request, run_id)
    fields = [
        "tradeId",
        "instrumentId",
        "direction",
        "entryTime",
        "exitTime",
        "entryPrice",
        "exitPrice",
        "quantity",
        "contractMultiplier",
        "commission",
        "slippageCost",
        "pnl",
        "pnlPercent",
        "holdingBars",
        "entryReason",
        "exitReason",
        "currency",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(report["trades"])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-trades.csv"'},
    )


@router.get("/definition-resources")
def strategy_definition_resources(
    request: Request,
    q: str | None = Query(default=None, max_length=128),
    asset_type: str | None = Query(default=None, alias="assetType", max_length=32),
) -> dict[str, Any]:
    documents = _strategy_resource_documents(_data_root(request))
    if q:
        term = q.casefold().strip()
        documents = [item for item in documents if term in f"{item['id']} {item['display_name']} {item.get('description', '')}".casefold()]
    if asset_type:
        wanted = asset_type.upper().strip()
        documents = [item for item in documents if wanted in item["supported_asset_types"]]
    documents.sort(key=lambda item: (item["updated_at"], item["id"], item["version"]), reverse=True)
    return clean({"items": [_camel_keys(item) for item in documents], "total": len(documents)})


@router.post("/definition-resources/migrations/legacy", status_code=201)
def strategy_definition_resource_migrate_legacy(request: Request) -> dict[str, Any]:
    """Move pre-versioned local AST files into immutable resource files.

    The operation is all-or-nothing after preflight.  Each original file is
    retained under a migration-specific backup directory so rollback is an
    explicit, auditable operation rather than a best-effort rewrite.
    """

    data_root = _data_root(request)
    resources = _definition_resources_dir(data_root)
    legacy_paths = [path for path in sorted(resources.glob("*.json")) if "@" not in path.stem]
    if not legacy_paths:
        return clean({"migrationId": None, "status": "noop", "actions": []})
    existing = _strategy_resource_documents(data_root)
    plan: list[tuple[Path, dict[str, Any], Path]] = []
    for source in legacy_paths:
        document = load_json(source, default=None)
        if not isinstance(document, dict):
            raise HTTPException(status_code=409, detail={"code": "LEGACY_RESOURCE_INVALID", "path": source.name})
        try:
            validate_strategy_definition(document)
        except StrategyDefinitionError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "LEGACY_RESOURCE_INVALID", "path": source.name, "message": error.message},
            ) from error
        strategy_id = str(document["id"])
        version = int(document["version"])
        target = resources / f"{strategy_id}@{version}.json"
        expected = max(
            (int(item["version"]) for item in existing if item["id"] == strategy_id), default=0
        ) + 1
        if target.exists() or version != expected:
            raise HTTPException(
                status_code=409,
                detail={"code": "LEGACY_VERSION_CONFLICT", "id": strategy_id, "version": version, "expectedVersion": expected},
            )
        existing.append(document)
        plan.append((source, document, target))

    migration_id = f"migration_{uuid.uuid4().hex[:16]}"
    migration_root = _resource_migrations_dir(data_root) / migration_id
    backup_root = migration_root / "originals"
    actions: list[dict[str, Any]] = []
    completed: list[tuple[Path, Path, Path]] = []
    try:
        for source, document, target in plan:
            backup = backup_root / source.name
            _atomic_json(target, document)
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, backup)
            completed.append((source, backup, target))
            actions.append(
                {"id": document["id"], "version": document["version"], "source": source.name, "target": target.name}
            )
    except OSError as error:
        for source, backup, target in reversed(completed):
            if backup.exists():
                os.replace(backup, source)
        # Preflight proved these target paths did not exist, so this exact
        # cleanup cannot remove an unrelated published resource.
        for _source, _document, target in plan:
            if target.exists():
                target.unlink()
        raise HTTPException(status_code=500, detail={"code": "LEGACY_MIGRATION_FAILED"}) from error
    record = {
        "schema_version": 1,
        "migration_id": migration_id,
        "status": "applied",
        "created_at": now_iso(),
        "actions": actions,
    }
    _atomic_json(_resource_migrations_dir(data_root) / f"{migration_id}.json", record)
    return clean(_camel_keys(record))


@router.post("/definition-resources/migrations/{migration_id}/rollback")
def strategy_definition_resource_rollback_migration(request: Request, migration_id: str) -> dict[str, Any]:
    data_root = _data_root(request)
    if not re.fullmatch(r"migration_[0-9a-f]{16}", migration_id):
        raise HTTPException(status_code=400, detail={"code": "INVALID_MIGRATION_ID"})
    record_path = _resource_migrations_dir(data_root) / f"{migration_id}.json"
    record = load_json(record_path, default=None)
    if not isinstance(record, dict) or record.get("status") != "applied":
        raise HTTPException(status_code=409, detail={"code": "MIGRATION_NOT_ROLLBACKABLE"})
    actions = record.get("actions")
    if not isinstance(actions, list):
        raise HTTPException(status_code=409, detail={"code": "INVALID_MIGRATION_RECORD"})
    references = sorted(
        {
            run_id
            for action in actions
            if isinstance(action, dict)
            for run_id in _strategy_backtest_references(data_root, str(action.get("id") or ""))
        }
    )
    if references:
        raise HTTPException(
            status_code=409,
            detail={"code": "MIGRATION_REFERENCED", "backtestRunIds": references},
        )
    resources = _definition_resources_dir(data_root)
    backup_root = _resource_migrations_dir(data_root) / migration_id / "originals"
    rolled_back_root = _resource_migrations_dir(data_root) / migration_id / "rolled_back"
    prepared: list[tuple[Path, Path, Path]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise HTTPException(status_code=409, detail={"code": "INVALID_MIGRATION_RECORD"})
        source_name = str(action.get("source") or "")
        target_name = str(action.get("target") or "")
        if (
            not source_name
            or not target_name
            or Path(source_name).name != source_name
            or Path(target_name).name != target_name
        ):
            raise HTTPException(status_code=409, detail={"code": "INVALID_MIGRATION_RECORD"})
        source = resources / source_name
        target = resources / target_name
        backup = backup_root / source.name
        if not target.is_file() or not backup.is_file():
            raise HTTPException(status_code=409, detail={"code": "MIGRATION_FILES_CHANGED"})
        prepared.append((source, backup, target))
    for source, backup, target in prepared:
        rolled_back_root.mkdir(parents=True, exist_ok=True)
        os.replace(target, rolled_back_root / target.name)
        os.replace(backup, source)
    record["status"] = "rolled_back"
    record["rolled_back_at"] = now_iso()
    _atomic_json(record_path, record)
    return clean(_camel_keys(record))


@router.post("/definition-resources", status_code=201)
def strategy_definition_resource_create(request: Request, document: dict[str, Any]) -> dict[str, Any]:
    try:
        evidence = validate_strategy_definition(document)
    except StrategyDefinitionError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error
    data_root = _data_root(request)
    existing = _strategy_resource_documents(data_root)
    same_id = [item for item in existing if item["id"] == document["id"]]
    if any(item["version"] == document["version"] for item in same_id):
        raise HTTPException(status_code=409, detail={"code": "VERSION_EXISTS"})
    expected_version = max((int(item["version"]) for item in same_id), default=0) + 1
    if int(document["version"]) != expected_version:
        raise HTTPException(
            status_code=409,
            detail={"code": "VERSION_SEQUENCE", "message": f"next version must be {expected_version}"},
        )
    filename = f"{document['id']}@{document['version']}.json"
    _atomic_json(_definition_resources_dir(data_root) / filename, document)
    return clean({"definition": _camel_keys(document), "validation": _camel_keys(evidence)})


def _latest_strategy_resource(data_root: Path, strategy_id: str) -> dict[str, Any]:
    matches = [item for item in _strategy_resource_documents(data_root) if item["id"] == strategy_id]
    if not matches:
        raise HTTPException(status_code=404, detail={"code": "STRATEGY_VERSION_NOT_FOUND"})
    return max(matches, key=lambda item: int(item["version"]))


def _strategy_backtest_references(data_root: Path, strategy_id: str) -> list[str]:
    """Return persisted runs that lock any version of one Strategy resource."""

    directory = _backtests_dir(data_root)
    if not directory.is_dir():
        return []
    references: list[str] = []
    for path in sorted(directory.glob("bt_*.json")):
        result = load_json(path, default=None)
        lock = result.get("dependency_lock") if isinstance(result, dict) else None
        strategy = lock.get("strategy") if isinstance(lock, dict) else None
        if isinstance(strategy, dict) and strategy.get("id") == strategy_id:
            references.append(str(result.get("run_id") or path.stem))
    return references


def _next_resource_timestamp(document: dict[str, Any]) -> str:
    current = datetime.fromisoformat(str(document["updated_at"]).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return max(now, current + timedelta(seconds=1)).isoformat(timespec="seconds")


def _decode_strategy_package(value: str) -> bytes:
    """Decode one bounded browser payload without accepting permissive Base64."""

    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_PACKAGE_ENCODING", "message": "策略包必须为标准 Base64"},
        ) from error


def _inspect_strategy_package_or_422(value: str) -> dict[str, Any]:
    try:
        return inspect_package(_decode_strategy_package(value))
    except StrategyTransferError as error:
        raise HTTPException(status_code=422, detail=error.to_dict()) from error


@router.get("/templates")
def strategy_templates() -> dict[str, Any]:
    """Return immutable built-in template metadata, not editable definitions."""

    try:
        values = build_strategy_templates()
    except StrategyTemplateError as error:
        raise HTTPException(status_code=500, detail=error.to_dict()) from error
    return clean({"items": [_camel_keys(item) for item in values], "total": len(values)})


@router.post("/templates/{template_id}/create", status_code=201)
def strategy_template_create(
    request: Request,
    template_id: str,
    body: StrategyTemplateCreateRequest,
) -> dict[str, Any]:
    """Create an isolated custom v1 from one read-only template."""

    data_root = _data_root(request)
    existing = _strategy_resource_documents(data_root)
    requested_name = body.displayName.strip() if body.displayName else None
    if requested_name and any(str(item["display_name"]).casefold() == requested_name.casefold() for item in existing):
        raise HTTPException(status_code=409, detail={"code": "DISPLAY_NAME_EXISTS"})
    strategy_id = f"strategy.{_safe_strategy_id(requested_name or template_id, {str(item['id']) for item in existing})}"
    try:
        definition = create_from_template(template_id, display_name=requested_name, strategy_id=strategy_id)
        validation = validate_strategy_definition(definition)
    except StrategyTemplateError as error:
        raise HTTPException(status_code=422 if error.code != "TEMPLATE_NOT_FOUND" else 404, detail=error.to_dict()) from error
    _atomic_json(_definition_resources_dir(data_root) / f"{strategy_id}@1.json", definition)
    return clean({"definition": _camel_keys(definition), "validation": _camel_keys(validation)})


@router.post("/packages/preview")
def strategy_package_preview(request: Request, body: StrategyPackageImportRequest) -> dict[str, Any]:
    """Validate an untrusted package entirely in memory before any import."""

    package = _inspect_strategy_package_or_422(body.packageBase64)
    document = package["definition"]
    existing = [
        item for item in _strategy_resource_documents(_data_root(request)) if item["id"] == document["id"]
    ]
    package["conflict"] = {
        "exists": bool(existing),
        "id": document["id"],
        "versions": sorted(int(item["version"]) for item in existing),
    }
    return clean(_camel_keys(package))


@router.post("/packages/import", status_code=201)
def strategy_package_import(request: Request, body: StrategyPackageImportRequest) -> dict[str, Any]:
    """Import only a fully validated desktop Rule-AST package.

    Package JSON is never copied blindly: it is schema-validated, its exact
    Function lock is re-resolved, and a conflict requires a user-selected
    immutable identity transition.
    """

    package = _inspect_strategy_package_or_422(body.packageBase64)
    document = deepcopy(package["definition"])
    if document.get("origin") != "custom":
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_RESOURCE_ORIGIN"})
    data_root = _data_root(request)
    existing = _strategy_resource_documents(data_root)
    same_id = [item for item in existing if item["id"] == document["id"]]
    imported_unchanged = False
    mutation: str | None = None
    if same_id:
        if body.conflict == "cancel":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IMPORT_CONFLICT",
                    "id": document["id"],
                    "version": document["version"],
                    "allowed": ["rename", "new_version", "cancel"],
                },
            )
        timestamp = now_iso()
        if body.conflict == "rename":
            target_id = (body.newStrategyId or "").strip()
            if not target_id:
                raise HTTPException(status_code=400, detail={"code": "IMPORT_RENAME_REQUIRED"})
            if any(item["id"] == target_id for item in existing):
                raise HTTPException(status_code=409, detail={"code": "IMPORT_RENAME_EXISTS", "id": target_id})
            document.update({"id": target_id, "version": 1, "created_at": timestamp, "updated_at": timestamp})
            mutation = "rename"
        elif body.conflict == "new_version":
            next_version = max(int(item["version"]) for item in same_id) + 1
            document.update({"version": next_version, "created_at": timestamp, "updated_at": timestamp})
            mutation = "new_version"
        else:  # Literal keeps Pydantic exhaustive, preserving an explicit error code.
            raise HTTPException(status_code=400, detail={"code": "INVALID_IMPORT_CONFLICT_MODE"})
    else:
        if int(document["version"]) != 1:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_SEQUENCE", "message": "首次导入必须是 version 1"},
            )
        imported_unchanged = True
    try:
        validation = validate_strategy_definition(document)
        # Re-lock after a user-selected rename/new-version; the result makes
        # it explicit that the package hash is intentionally no longer exact.
        imported_lock = inspect_package(build_package(document))["dependency_lock"]
    except (StrategyDefinitionError, StrategyTransferError) as error:
        detail = error.to_dict() if hasattr(error, "to_dict") else {"code": error.code, "message": error.message}
        raise HTTPException(status_code=422, detail=detail) from error
    target = _definition_resources_dir(data_root) / f"{document['id']}@{document['version']}.json"
    if target.exists():
        raise HTTPException(status_code=409, detail={"code": "VERSION_EXISTS"})
    _atomic_json(target, document)
    return clean(
        _camel_keys(
            {
                "definition": document,
                "validation": validation,
                "dependency_lock": imported_lock,
                "imported_unchanged": imported_unchanged,
                "mutation": mutation,
                "source_package": package["manifest"],
            }
        )
    )


@router.post("/definition-resources/{strategy_id}/copy", status_code=201)
def strategy_definition_resource_copy(
    request: Request,
    strategy_id: str,
    body: StrategyCopyRequest,
) -> dict[str, Any]:
    data_root = _data_root(request)
    source = deepcopy(_latest_strategy_resource(data_root, strategy_id))
    if source["origin"] in {"community", "plugin"}:
        raise HTTPException(status_code=403, detail={"code": "UNTRUSTED_ORIGIN"})
    display_name = (body.displayName or f"{source['display_name']} 副本").strip()
    existing = _strategy_resource_documents(data_root)
    if any(str(item["display_name"]).casefold() == display_name.casefold() for item in existing):
        raise HTTPException(status_code=409, detail="strategy display name already exists")
    copied_id = f"strategy.{_safe_strategy_id(display_name, {str(item['id']) for item in existing})}"
    timestamp = now_iso()
    source.update(
        {
            "id": copied_id,
            "version": 1,
            "display_name": display_name,
            "origin": "custom",
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    validate_strategy_definition(source)
    _atomic_json(_definition_resources_dir(data_root) / f"{copied_id}@1.json", source)
    return clean(_camel_keys(source))


@router.patch("/definition-resources/{strategy_id}/status", status_code=201)
def strategy_definition_resource_status(
    request: Request,
    strategy_id: str,
    body: StrategyStatusRequest,
) -> dict[str, Any]:
    data_root = _data_root(request)
    source = deepcopy(_latest_strategy_resource(data_root, strategy_id))
    if source["origin"] == "builtin":
        raise HTTPException(status_code=403, detail={"code": "BUILTIN_READ_ONLY"})
    if source["origin"] in {"community", "plugin"}:
        raise HTTPException(status_code=403, detail={"code": "UNTRUSTED_ORIGIN"})
    source["version"] = int(source["version"]) + 1
    source["status"] = body.status
    source["updated_at"] = _next_resource_timestamp(source)
    validate_strategy_definition(source)
    _atomic_json(_definition_resources_dir(data_root) / f"{strategy_id}@{source['version']}.json", source)
    return clean(_camel_keys(source))


@router.delete("/definition-resources/{strategy_id}")
def strategy_definition_resource_delete(
    request: Request,
    strategy_id: str,
    body: StrategyDeleteRequest,
) -> dict[str, Any]:
    data_root = _data_root(request)
    latest = _latest_strategy_resource(data_root, strategy_id)
    if latest["origin"] == "builtin":
        raise HTTPException(status_code=403, detail={"code": "BUILTIN_READ_ONLY"})
    if latest["origin"] in {"community", "plugin"}:
        raise HTTPException(status_code=403, detail={"code": "UNTRUSTED_ORIGIN"})
    if body.confirmDisplayName != latest["display_name"]:
        raise HTTPException(status_code=400, detail="strategy display name confirmation does not match")
    # Published resources are immutable.  A delete request is an archival
    # state transition, especially important when historical runs cite v1.
    archived = deepcopy(latest)
    archived["version"] = int(latest["version"]) + 1
    archived["status"] = "deprecated"
    archived["updated_at"] = _next_resource_timestamp(latest)
    validate_strategy_definition(archived)
    _atomic_json(_definition_resources_dir(data_root) / f"{strategy_id}@{archived['version']}.json", archived)
    return clean(
        _camel_keys(
            {
                "deleted": False,
                "archived": True,
                "strategy_id": strategy_id,
                "version": archived["version"],
                "referenced_by_backtests": _strategy_backtest_references(data_root, strategy_id),
            }
        )
    )


@router.get("/definition-resources/{strategy_id}/versions")
def strategy_definition_resource_versions(request: Request, strategy_id: str) -> dict[str, Any]:
    """List all immutable versions so a user can select a historical replay."""

    versions = [
        item for item in _strategy_resource_documents(_data_root(request)) if item["id"] == strategy_id
    ]
    if not versions:
        raise HTTPException(status_code=404, detail={"code": "STRATEGY_VERSION_NOT_FOUND"})
    versions.sort(key=lambda item: int(item["version"]), reverse=True)
    return clean({"items": [_camel_keys(item) for item in versions], "total": len(versions)})


@router.get("/definition-resources/{strategy_id}/export")
def strategy_definition_resource_export(
    request: Request,
    strategy_id: str,
    version: int = Query(..., ge=1),
) -> Response:
    """Export one immutable custom Definition with only declarative assets."""

    definition = _exact_strategy_resource(_data_root(request), strategy_id, version)
    if definition["origin"] != "custom":
        raise HTTPException(status_code=403, detail={"code": "BUILTIN_NOT_EXPORTABLE"})
    try:
        package = build_package(definition)
    except StrategyTransferError as error:
        raise HTTPException(status_code=422, detail=error.to_dict()) from error
    return Response(
        content=package,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{strategy_id}@{version}.strategy.zip"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/definition-resources/{strategy_id}")
def strategy_definition_resource(
    request: Request,
    strategy_id: str,
    version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    matches = [item for item in _strategy_resource_documents(_data_root(request)) if item["id"] == strategy_id]
    if version is not None:
        matches = [item for item in matches if item["version"] == version]
    if not matches:
        raise HTTPException(status_code=404, detail={"code": "STRATEGY_VERSION_NOT_FOUND"})
    document = max(matches, key=lambda item: int(item["version"]))
    return clean(_camel_keys(document))


@router.get("/definitions/{strategy_id}")
def strategy_definition(request: Request, strategy_id: str) -> dict[str, Any]:
    return _definition_response(_data_root(request), strategy_id)


@router.post("/definitions", status_code=201)
def strategy_create(request: Request, body: StrategyMutationRequest) -> dict[str, Any]:
    """Persist a validated DSL with a stable ASCII ID and separate UI metadata."""
    data_root = _data_root(request)
    display_name = body.displayName.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="strategy display name is required")
    existing = {item["strategyId"] for item in _definition_items(data_root)}
    if any(str(item["displayName"]).casefold() == display_name.casefold() for item in _definition_items(data_root)):
        raise HTTPException(status_code=409, detail="strategy display name already exists")
    strategy_id = _safe_strategy_id(display_name, existing)
    document = _normalized_document(body, strategy_id)
    _atomic_json(_definitions_dir(data_root) / f"{strategy_id}.json", document)
    metadata = _read_metadata(data_root)
    metadata[strategy_id] = {
        "displayName": display_name,
        "markColorId": "",
        "origin": "custom",
        "status": "active",
        "runMode": "backtest",
        "backtestStatus": "not_run",
        "category": "custom",
        "createdAt": now_iso(),
    }
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, strategy_id)


@router.put("/definitions/{strategy_id}")
def strategy_update(request: Request, strategy_id: str, body: StrategyMutationRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    _load_definition(data_root, strategy_id)
    display_name = body.displayName.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="strategy display name is required")
    if any(
        str(item["displayName"]).casefold() == display_name.casefold()
        and item["strategyId"] != strategy_id
        for item in _definition_items(data_root)
    ):
        raise HTTPException(status_code=409, detail="strategy display name already exists")
    document = _normalized_document(body, strategy_id)
    _atomic_json(_definitions_dir(data_root) / f"{strategy_id}.json", document)
    metadata = _read_metadata(data_root)
    existing_metadata = metadata.get(strategy_id, {})
    metadata[strategy_id] = {**existing_metadata, "displayName": display_name}
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, strategy_id)


@router.patch("/definitions/{strategy_id}/mark")
def strategy_mark(request: Request, strategy_id: str, body: StrategyMarkRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    _load_definition(data_root, strategy_id)
    metadata = _read_metadata(data_root)
    existing_metadata = metadata.get(strategy_id, {})
    metadata[strategy_id] = {
        **existing_metadata,
        "displayName": existing_metadata.get("displayName") or strategy_id,
        "markColorId": body.markColorId or "",
    }
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, strategy_id)


@router.patch("/definitions/{strategy_id}/status")
def strategy_status(request: Request, strategy_id: str, body: StrategyStatusRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    _load_definition(data_root, strategy_id)
    metadata = _read_metadata(data_root)
    existing_metadata = metadata.get(strategy_id, {})
    if existing_metadata.get("origin") == "builtin":
        raise HTTPException(status_code=403, detail={"code": "BUILTIN_READ_ONLY"})
    metadata[strategy_id] = {**existing_metadata, "status": body.status}
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, strategy_id)


@router.post("/definitions/{strategy_id}/copy", status_code=201)
def strategy_copy(request: Request, strategy_id: str, body: StrategyCopyRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    source = dict(_load_definition(data_root, strategy_id))
    existing_items = _definition_items(data_root)
    existing_ids = {str(item["strategyId"]) for item in existing_items}
    source_name = next(str(item["displayName"]) for item in existing_items if item["strategyId"] == strategy_id)
    display_name = (body.displayName or f"{source_name} 副本").strip()
    if any(str(item["displayName"]).casefold() == display_name.casefold() for item in existing_items):
        raise HTTPException(status_code=409, detail="strategy display name already exists")
    copied_id = _safe_strategy_id(display_name, existing_ids)
    source["strategy_id"] = copied_id
    _atomic_json(_definitions_dir(data_root) / f"{copied_id}.json", source)
    metadata = _read_metadata(data_root)
    metadata[copied_id] = {
        "displayName": display_name,
        "markColorId": "",
        "origin": "custom",
        "status": "active",
        "runMode": "backtest",
        "backtestStatus": "not_run",
        "category": "custom",
        "createdAt": now_iso(),
    }
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, copied_id)


@router.delete("/definitions/{strategy_id}")
def strategy_delete(request: Request, strategy_id: str, body: StrategyDeleteRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    _load_definition(data_root, strategy_id)
    metadata = _read_metadata(data_root)
    display_name = metadata.get(strategy_id, {}).get("displayName") or strategy_id
    if body.confirmDisplayName != display_name:
        raise HTTPException(status_code=400, detail="strategy display name confirmation does not match")
    path = _definitions_dir(data_root) / f"{strategy_id}.json"
    path.unlink()
    metadata.pop(strategy_id, None)
    _save_metadata(data_root, metadata)
    return clean({"deleted": True, "strategyId": strategy_id})


@router.post("/matches")
def strategy_matches(request: Request, body: StrategyMatchesRequest) -> dict[str, Any]:
    """Return a bounded, de-duplicated OR-union of current strategy signals."""
    data_root = _data_root(request)
    definitions = {item["strategyId"]: item for item in _definition_items(data_root)}
    strategy_ids = list(definitions) if body.allStrategies else list(dict.fromkeys(body.strategyIds))
    if not strategy_ids:
        return clean({"items": [], "total": 0, "updatedAt": None, "page": body.page, "pageSize": body.pageSize})
    unknown = [strategy_id for strategy_id in strategy_ids if strategy_id not in definitions]
    if unknown:
        raise HTTPException(status_code=404, detail=f"strategy definition not found: {unknown[0]}")
    logical = _logical_instruments(data_root)
    storage_to_logical = {
        str(item.get("storageInstrumentId") or instrument_id): instrument_id
        for instrument_id, item in logical.items()
        if classify_market(item) != UNCLASSIFIED_CATEGORY
        and (not body.categoryKeys or any(matches_market_category(item, key) for key in body.categoryKeys))
    }
    source_bars = bars_by_instrument(data_root, period="1d", limit_per_instrument=500, max_instruments=500)
    instruments = {
        storage_id: bars
        for storage_id, bars in source_bars.items()
        if storage_id in storage_to_logical
    }
    matches: dict[str, dict[str, Any]] = {}
    for strategy_id in strategy_ids:
        document = _load_definition(data_root, strategy_id)
        if document.get("script_kind") == "formula_v1":
            try:
                formula_result = run_formula_strategy(
                    data_root, strategy_id, document, limit_instruments=500, limit_per_instrument=500,
                )
            except FormulaError as error:
                raise HTTPException(status_code=400, detail=error.to_dict()) from error
            for scan in formula_result["signals"]:
                if not scan["signals"] or scan["signals"][-1]["index"] != scan["barCount"] - 1:
                    continue
                instrument_id = str(scan["instrumentId"])
                item = logical.get(instrument_id)
                if not item:
                    continue
                if classify_market(item) == UNCLASSIFIED_CATEGORY:
                    continue
                if body.categoryKeys and not any(matches_market_category(item, key) for key in body.categoryKeys):
                    continue
                last_signal = scan["signals"][-1]
                result = matches.setdefault(
                    instrument_id,
                    {**item, "matchedStrategyIds": [], "latestSignalAt": last_signal["barOpenTime"]},
                )
                result["matchedStrategyIds"].append(strategy_id)
            continue
        try:
            validated = validate_dsl(document)
            report = scan_strategy(validated, instruments, timeout_seconds=2.0, max_ops=500_000)
        except StrategyDslError as error:
            raise HTTPException(status_code=400, detail=error.to_dict()) from error
        for scan in report.instruments:
            if not scan.signals or scan.signals[-1].index != scan.bar_count - 1:
                continue
            instrument_id = storage_to_logical.get(scan.instrument_id)
            item = logical.get(instrument_id or "")
            if not item:
                continue
            last_signal = scan.signals[-1]
            result = matches.setdefault(instrument_id, {**item, "matchedStrategyIds": [], "latestSignalAt": last_signal.bar_open_time})
            result["matchedStrategyIds"].append(strategy_id)
            if str(last_signal.bar_open_time) > str(result["latestSignalAt"]):
                result["latestSignalAt"] = last_signal.bar_open_time
    ordered = sorted(matches.values(), key=lambda item: (str(item["latestSignalAt"]), len(item["matchedStrategyIds"]), str(item.get("name") or "")), reverse=True)
    response = paginate(ordered, body.page, body.pageSize)
    response["updatedAt"] = max(
        (str(logical[instrument_id].get("lastBarAt") or "") for instrument_id in storage_to_logical.values()),
        default="",
    ) or None
    return clean(response)


@router.post("/validate")
def strategy_validate(request: Request, document: dict[str, Any]) -> dict[str, Any]:
    """Validate a full Strategy DSL document without persisting anything."""
    try:
        validated = validate_dsl(document)
    except StrategyDslError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error
    return clean(
        {
            "valid": True,
            "strategyId": validated.strategy_id,
            "inputs": list(validated.inputs),
            "parameters": validated.parameters,
        }
    )


@router.post("/run")
def strategy_run(request: Request, body: StrategyRunRequest) -> dict[str, Any]:
    """Scan local silver bars with a persisted strategy definition and write a run record."""
    data_root = _data_root(request)
    document = _load_definition(data_root, body.strategyId)
    if _read_metadata(data_root).get(body.strategyId, {}).get("status") == "disabled":
        raise HTTPException(status_code=409, detail={"code": "DISABLED", "message": "策略已禁用"})
    if document.get("script_kind") == "formula_v1":
        try:
            result = run_formula_strategy(
                data_root,
                body.strategyId,
                document,
                parameters=body.parameters,
                period=body.period,
                limit_instruments=body.limitInstruments,
                limit_per_instrument=body.limitPerInstrument,
            )
        except FormulaError as error:
            raise HTTPException(status_code=400, detail=error.to_dict()) from error
        _atomic_json(_runs_dir(data_root) / f"{result['report']['runId']}.json", result["report"])
        return clean(result)
    try:
        validated = validate_dsl(document)
    except StrategyDslError as error:
        raise HTTPException(status_code=400, detail=error.to_dict()) from error

    period = body.period
    if period is not None:
        inventory = load_inventory(data_root)
        if period not in inventory.periods:
            raise HTTPException(status_code=400, detail=f"unknown period: {period}")

    instruments = bars_by_instrument(
        data_root,
        period=period,
        limit_per_instrument=body.limitPerInstrument,
        max_instruments=body.limitInstruments,
    )
    if not instruments:
        raise HTTPException(status_code=404, detail="no local bars available for this strategy run")

    report = scan_strategy(
        validated,
        instruments,
        parameters=dict(body.parameters),
        timeout_seconds=body.timeoutSeconds,
        max_ops=body.maxOps,
    )
    write_run_record(_runs_dir(data_root) / f"{report.run_id}.json", report)
    signals = [
        {
            "instrumentId": scan.instrument_id,
            "barCount": scan.bar_count,
            "signalCount": scan.signal_count,
            "signals": [asdict(signal) for signal in scan.signals[:_MAX_SIGNALS_PER_INSTRUMENT]],
        }
        for scan in report.instruments
    ]
    return clean({"report": _camel_keys(asdict(report)), "signals": _camel_keys(signals)})


@router.get("/history")
def strategy_history(
    request: Request,
    limit: int = Query(default=_DEFAULT_HISTORY_LIMIT, ge=1, le=_MAX_HISTORY_LIMIT),
) -> dict[str, Any]:
    """Return run-record summaries, newest first."""
    runs_directory = _runs_dir(_data_root(request))
    summaries: list[dict[str, Any]] = []
    if runs_directory.is_dir():
        paths = sorted(runs_directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths:
            record = load_json(path, default=None)
            if not isinstance(record, dict):
                continue
            instruments = record.get("instruments") or []
            summaries.append(
                {
                    "runId": str(record.get("run_id") or record.get("runId") or ""),
                    "strategyId": str(record.get("strategy_id") or record.get("strategyId") or ""),
                    "strategyVersion": str(record.get("strategy_version") or record.get("strategyVersion") or ""),
                    "dataVersion": str(record.get("data_version") or ""),
                    "parameterVersion": str(record.get("parameter_version") or ""),
                    "startedAt": str(record.get("started_at") or record.get("startedAt") or ""),
                    "finishedAt": str(record.get("finished_at") or record.get("finishedAt") or ""),
                    "status": str(record.get("status") or "UNKNOWN"),
                    "error": record.get("error"),
                    "instrumentCount": int(record.get("instrumentCount") or len(instruments)),
                    "signalCount": sum(
                        int(item.get("signal_count") or item.get("signalCount") or 0)
                        for item in instruments if isinstance(item, dict)
                    ),
                }
            )
    return clean({"items": summaries[:limit], "total": len(summaries), "limit": limit})


__all__ = ("router",)
