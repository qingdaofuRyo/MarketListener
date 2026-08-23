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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.formula_engine import (
    CHART_INDICATOR_CATALOG,
    STRATEGY_FUNCTION_CATALOG,
    FormulaError,
    validate_formula_document,
)
from market_monitor.formula_runtime import run_formula_strategy
from market_monitor.strategy_dsl import StrategyDslError, scan_strategy, validate_dsl, write_run_record
from market_monitor.web_api.common import (
    bars_by_instrument,
    clean,
    load_inventory,
    load_json,
    now_iso,
    paginate,
)
from market_monitor.web_api.market import _logical_instruments, matches_market_category

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


def _data_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_root", None)
    if configured:
        return Path(configured)
    return _DEFAULT_DATA_ROOT


def _definitions_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "definitions"


def _runs_dir(data_root: Path) -> Path:
    return data_root / "strategies" / "runs"


def _metadata_path(data_root: Path) -> Path:
    return data_root / "strategies" / "metadata.json"


def _read_metadata(data_root: Path) -> dict[str, dict[str, str]]:
    payload = load_json(_metadata_path(data_root), default={})
    if not isinstance(payload, dict):
        return {}
    items = payload.get("items", payload)
    if not isinstance(items, dict):
        return {}
    return {
        str(strategy_id): {
            "displayName": str(value.get("displayName") or strategy_id),
            "markColorId": str(value.get("markColorId") or ""),
        }
        for strategy_id, value in items.items()
        if isinstance(value, dict) and _DEFINITION_NAME.fullmatch(str(strategy_id))
    }


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


def _save_metadata(data_root: Path, metadata: dict[str, dict[str, str]]) -> None:
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
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError:
            updated_at = now_iso()
        strategy_id = str(document.get("strategy_id") or "")
        script_kind = str(document.get("condition_kind") or document.get("script_kind") or "dsl_v1")
        ui = metadata.get(strategy_id, {})
        items.append(
            {
                "strategyId": strategy_id,
                "displayName": ui.get("displayName") or strategy_id,
                "markColorId": ui.get("markColorId") or None,
                "scriptKind": script_kind,
                "strategyVersion": str(document.get("strategy_version") or ("1" if document.get("script_kind") == "formula_v1" else "")),
                "inputs": list(document.get("inputs") or (["open", "high", "low", "close", "volume"] if document.get("script_kind") == "formula_v1" else [])),
                "parameters": dict(document.get("parameters") or {}),
                "description": str(document.get("description") or ""),
                "updatedAt": updated_at,
            }
        )
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


class StrategyDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmDisplayName: str = Field(min_length=1, max_length=64)


class StrategyMatchesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyIds: list[str] = Field(default_factory=list, max_length=40)
    allStrategies: bool = False
    categoryKeys: list[str] = Field(default_factory=list, max_length=20)
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=24, ge=1, le=100)


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


def _public_function_catalog() -> list[dict[str, Any]]:
    """Attach typed parameter and applicability metadata to legacy entries."""
    integer_names = {"lookback", "short_lookback", "long_lookback", "count_threshold", "first_offset", "second_offset", "buckets"}
    result: list[dict[str, Any]] = []
    for source in STRATEGY_FUNCTION_CATALOG:
        item = dict(source)
        parameters = item.get("parameters") or []
        if parameters and all(isinstance(value, str) for value in parameters):
            item["parameters"] = [
                {
                    "name": value,
                    "type": "integer" if value in integer_names else "float",
                    "minimum": 1 if value in integer_names else None,
                }
                for value in parameters
            ]
        item.setdefault("returnType", "boolean_or_number")
        item.setdefault("requiredFields", [field.strip() for field in str(item.get("data") or "").replace("、", ",").split(",") if field.strip() in {"open", "high", "low", "close", "volume"}])
        item.setdefault("applicableMarkets", _ALL_MARKET_TYPES)
        result.append(item)
    return result


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
    runtime_kind = str(document.get("script_kind") or "dsl_v1")
    script_kind = str(document.get("condition_kind") or runtime_kind)
    ui = _read_metadata(data_root).get(strategy_id, {})
    return clean({
        "strategyId": strategy_id,
        "displayName": ui.get("displayName") or strategy_id,
        "markColorId": ui.get("markColorId") or None,
        "scriptKind": script_kind,
        "strategyVersion": str(document.get("strategy_version") or ("1" if runtime_kind == "formula_v1" else "")),
        "description": str(document.get("description") or ""),
        "inputs": list(document.get("inputs") or (["open", "high", "low", "close", "volume"] if runtime_kind == "formula_v1" else [])),
        "parameters": dict(document.get("parameters") or {}),
        "script": document,
    })


@router.get("/definitions")
def strategy_definitions(request: Request) -> dict[str, Any]:
    items = _definition_items(_data_root(request))
    items.sort(key=lambda item: item["updatedAt"], reverse=True)
    return clean({"items": items, "total": len(items)})


@router.get("/indicators")
def strategy_indicators() -> dict[str, Any]:
    """Return chart-only indicators; they are not strategy conditions."""
    return clean({"items": list(CHART_INDICATOR_CATALOG), "total": len(CHART_INDICATOR_CATALOG)})


@router.get("/conditions")
def strategy_conditions() -> dict[str, Any]:
    items = _public_function_catalog()
    return clean({"items": items, "total": len(items)})


@router.get("/functions")
def strategy_functions() -> dict[str, Any]:
    """Typed Python strategy-function catalog for the visual condition builder."""
    items = _public_function_catalog()
    return clean({"items": items, "total": len(items), "runtime": "python"})


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
    metadata[strategy_id] = {"displayName": display_name, "markColorId": ""}
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
    metadata[strategy_id] = {"displayName": display_name, "markColorId": metadata.get(strategy_id, {}).get("markColorId", "")}
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, strategy_id)


@router.patch("/definitions/{strategy_id}/mark")
def strategy_mark(request: Request, strategy_id: str, body: StrategyMarkRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    _load_definition(data_root, strategy_id)
    metadata = _read_metadata(data_root)
    metadata[strategy_id] = {
        "displayName": metadata.get(strategy_id, {}).get("displayName") or strategy_id,
        "markColorId": body.markColorId or "",
    }
    _save_metadata(data_root, metadata)
    return _definition_response(data_root, strategy_id)


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
        if not body.categoryKeys or any(matches_market_category(item, key) for key in body.categoryKeys)
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
