"""Versioned metadata registry for reusable strategy functions.

The registry describes pure calculation functions.  It does not execute user
code and intentionally exposes no chart, account, position or order capability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from market_monitor.formula_engine import STRATEGY_FUNCTION_CATALOG


_ALL_ASSET_TYPES = ("STOCK", "B_SHARE", "INDEX", "FUTURE", "ETF", "LOF", "REIT", "FUND")
_INTEGER_PARAMETERS = {
    "lookback",
    "short_lookback",
    "long_lookback",
    "count_threshold",
    "first_offset",
    "second_offset",
    "buckets",
}
_CATEGORY_BY_ID = {
    "market_scope": ("universe", "标的范围"),
    "market_cap": ("fundamental", "基本面"),
    "period_return": ("return", "收益"),
    "no_limit": ("limit", "涨跌停"),
    "close_breakout": ("price", "价格"),
    "limit_count": ("limit", "涨跌停"),
    "return_difference": ("return", "收益"),
    "direction_count": ("statistics", "统计"),
    "direction_ratio": ("statistics", "统计"),
    "range_ratio": ("statistics", "统计"),
    "volume_slope": ("volume", "成交量"),
    "gann_rising_rate": ("trend", "趋势"),
    "gann_falling_rate": ("trend", "趋势"),
    "hsar_resistance": ("support_resistance", "支撑阻力"),
    "hsar_support": ("support_resistance", "支撑阻力"),
}
_BUILTIN_TIMESTAMP = "2026-09-02T00:00:00+08:00"
_CANONICAL_CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "technical.sma", "name": "简单移动平均", "category": "trend", "label": "趋势", "inputs": (("values", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "sma", "description": "指定窗口的算术移动平均。"},
    {"id": "technical.ema", "name": "指数移动平均", "category": "trend", "label": "趋势", "inputs": (("values", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "ema", "description": "以简单移动平均为种子的标准指数移动平均。"},
    {"id": "technical.highest", "name": "滚动最高值", "category": "price", "label": "价格", "inputs": (("values", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "highest", "description": "仅使用当前及此前窗口数据的最高值。"},
    {"id": "technical.lowest", "name": "滚动最低值", "category": "price", "label": "价格", "inputs": (("values", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "lowest", "description": "仅使用当前及此前窗口数据的最低值。"},
    {"id": "technical.stddev", "name": "总体标准差", "category": "statistics", "label": "统计", "inputs": (("values", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "stddev", "description": "指定窗口的总体标准差。"},
    {"id": "technical.rsi", "name": "RSI", "category": "momentum", "label": "动量", "inputs": (("close", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "rsi", "description": "使用平滑平均涨跌幅计算相对强弱指数。"},
    {"id": "technical.atr", "name": "ATR", "category": "volatility", "label": "波动率", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "atr", "description": "Wilder 平滑的真实波幅均值。"},
    {"id": "technical.macd", "name": "MACD", "category": "momentum", "label": "动量", "inputs": (("close", "series<number>"), ("fast", "integer"), ("slow", "integer"), ("signal", "integer")), "output": "record<series<number>>", "runtime": "macd", "description": "返回 MACD 线、信号线和柱状值。"},
    {"id": "technical.bollinger", "name": "布林带", "category": "volatility", "label": "波动率", "inputs": (("close", "series<number>"), ("lookback", "integer"), ("multiplier", "number")), "output": "record<series<number>>", "runtime": "bollinger", "description": "返回中轨及标准差倍数上下轨。"},
    {"id": "technical.keltner", "name": "肯特纳通道", "category": "volatility", "label": "波动率", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>"), ("ema_lookback", "integer"), ("atr_lookback", "integer"), ("multiplier", "number")), "output": "record<series<number>>", "runtime": "keltner", "description": "以 EMA 为中轨、ATR 为宽度的价格通道。"},
    {"id": "technical.donchian", "name": "唐奇安通道", "category": "price_channel", "label": "价格通道", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("lookback", "integer")), "output": "record<series<number>>", "runtime": "donchian", "description": "返回滚动最高、最低及中轨。"},
    {"id": "technical.stochastic", "name": "随机振荡器", "category": "momentum", "label": "动量", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>"), ("lookback", "integer"), ("smooth", "integer")), "output": "record<series<number>>", "runtime": "stochastic", "description": "返回 %K 与平滑后的 %D。"},
    {"id": "condition.crossover", "name": "向上交叉", "category": "condition", "label": "条件判断", "inputs": (("left", "series<number>"), ("right", "series<number>")), "output": "series<boolean>", "runtime": "crossover", "description": "左序列从不高于右序列变为高于右序列。"},
    {"id": "condition.crossunder", "name": "向下交叉", "category": "condition", "label": "条件判断", "inputs": (("left", "series<number>"), ("right", "series<number>")), "output": "series<boolean>", "runtime": "crossunder", "description": "左序列从不低于右序列变为低于右序列。"},
    {"id": "price.percentage_change", "name": "价格涨跌幅", "category": "return", "label": "收益", "inputs": (("values", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "percentage_change", "description": "当前值相对指定历史位置的变化率。"},
    {"id": "volume.change", "name": "成交量变化率", "category": "volume", "label": "成交量", "inputs": (("volume", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "volume_change", "description": "当前成交量相对指定历史位置的变化率。"},
    {"id": "volatility.true_range", "name": "真实波幅", "category": "volatility", "label": "波动率", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>")), "output": "series<number>", "runtime": "true_range", "description": "综合当根高低差和相对前收的跳空幅度。"},
    {"id": "volatility.chaikin_volatility", "name": "Chaikin 波动率", "category": "volatility", "label": "波动率", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("ema_lookback", "integer"), ("change_lookback", "integer")), "output": "series<number>", "runtime": "chaikin_volatility", "description": "EMA(H-L) 相对指定历史位置的百分比变化，单位为百分比。"},
    {"id": "volatility.atr_percent", "name": "ATR 百分比", "category": "volatility", "label": "波动率", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>"), ("lookback", "integer")), "output": "series<number>", "runtime": "atr_percent", "description": "公开可复算的 100 × Wilder ATR / close；不声称等同 Twiggs® 专有实现。"},
    {"id": "volatility.relative_volatility_index", "name": "相对波动指数", "category": "volatility", "label": "波动率", "inputs": (("close", "series<number>"), ("stddev_lookback", "integer"), ("smooth_lookback", "integer")), "output": "series<number>", "runtime": "relative_volatility_index", "description": "Dorsey 1993 收盘价版本：按涨跌日拆分滚动标准差并进行 Wilder 平滑，范围 0 至 100。"},
    {"id": "market.up_down_count", "name": "周期上涨下跌数量", "category": "market", "label": "行情数据", "inputs": (("close", "series<number>"), ("lookback", "integer")), "output": "series<up_down_count>", "runtime": "up_down_count", "description": "按相邻收盘价统计窗口内上涨、下跌和平盘数量，并返回可用状态。"},
    {"id": "market.volume_profile", "name": "成交量分布", "category": "volume", "label": "成交量", "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>"), ("volume", "series<number>"), ("bins", "integer"), ("value_area_percent", "number"), ("range_start", "integer"), ("range_end", "integer")), "output": "record<volume_profile>", "runtime": "volume_profile", "description": "将每根 bar 的完整 volume 分配到 HLC3 所在等宽价格桶，返回 POC、价值区和可追溯范围元数据。"},
    {"id": "market.market_cap", "name": "市值比较", "category": "fundamental", "label": "基本面", "inputs": (("value_yuan", "number"), ("operator", "comparison_operator"), ("threshold", "number"), ("unit", "market_cap_unit")), "output": "boolean", "runtime": "market_cap", "description": "按元、万元或亿元阈值比较显式传入的公司市值。", "assets": ("STOCK", "B_SHARE")},
)

# A versioned function is a published executable resource.  Keep ATR v1 for
# every existing strategy and expose v2 as a distinct runtime identity rather
# than silently changing the meaning of ``technical.atr@1``.
_CANONICAL_VERSION_OVERRIDES: tuple[dict[str, Any], ...] = (
    {
        "id": "technical.atr",
        "version": 2,
        "name": "ATR（显式版本 2）",
        "category": "volatility",
        "label": "波动率",
        "inputs": (("high", "series<number>"), ("low", "series<number>"), ("close", "series<number>"), ("lookback", "integer")),
        "output": "series<number>",
        "runtime": "atr_v2",
        "description": "Wilder 平滑真实波幅均值的独立发布版本；历史策略仍精确锁定 v1。",
    },
)


class StrategyFunctionRegistryError(ValueError):
    """Stable registry error suitable for API translation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class FunctionInput:
    name: str
    value_type: str
    required: bool = True
    default: int | float | bool | str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    description: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.value_type,
            "required": self.required,
        }
        for key, value in (
            ("default", self.default),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
        ):
            if value is not None:
                result[key] = value
        if self.description:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class StrategyFunctionDefinition:
    function_id: str
    version: int
    display_name: str
    category: str
    category_label: str
    description: str
    inputs: tuple[FunctionInput, ...]
    output_type: str
    supported_asset_types: tuple[str, ...]
    runtime_name: str
    example: str = ""
    required_fields: tuple[str, ...] = ()
    deprecated: bool = False
    origin: str = "builtin"
    created_at: str = _BUILTIN_TIMESTAMP
    updated_at: str = _BUILTIN_TIMESTAMP

    @property
    def key(self) -> tuple[str, int]:
        return self.function_id, self.version

    def to_public_dict(self, references: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
        referenced_by = references or {}
        return {
            "resourceKind": "strategy_function",
            "id": self.function_id,
            "version": self.version,
            "versionedId": f"{self.function_id}@{self.version}",
            "name": self.display_name,
            "displayName": self.display_name,
            "category": self.category,
            "categoryLabel": self.category_label,
            "definition": self.description,
            "description": self.description,
            "inputs": [item.to_public_dict() for item in self.inputs],
            # Kept for the current visual builder while clients migrate to inputs.
            "parameters": [item.to_public_dict() for item in self.inputs if item.name not in self.required_fields],
            "output": {"type": self.output_type},
            "returnType": self.output_type,
            "supportedAssetTypes": list(self.supported_asset_types),
            "applicableMarkets": [
                "a_share", "hk_stock", "main_board", "chinext", "star", "etf", "bse",
                "cn_future", "cn_commodity_index", "global_future",
            ],
            "requiredFields": list(self.required_fields),
            "runtimeName": self.runtime_name,
            "pure": True,
            "capabilities": ["market_data_input"],
            "deprecated": self.deprecated,
            "origin": self.origin,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "example": self.example,
            "referencedBy": {
                "indicators": sorted(set(referenced_by.get("indicators", ()))),
                "strategies": sorted(set(referenced_by.get("strategies", ()))),
            },
        }

    def definition_hash(self) -> str:
        """Return a stable digest for this immutable published definition."""

        payload = self.to_public_dict()
        # Reference lists are derived, not part of the executable definition.
        payload.pop("referencedBy", None)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StrategyFunctionRegistry:
    """In-memory registry with exact-version resolution and stable ordering."""

    def __init__(self, definitions: Iterable[StrategyFunctionDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], StrategyFunctionDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: StrategyFunctionDefinition) -> None:
        if not definition.function_id or definition.version < 1:
            raise StrategyFunctionRegistryError("INVALID_FUNCTION", "function id and positive version are required")
        if definition.key in self._definitions:
            raise StrategyFunctionRegistryError(
                "DUPLICATE_FUNCTION",
                f"strategy function {definition.function_id}@{definition.version} already exists",
            )
        if not definition.inputs:
            raise StrategyFunctionRegistryError("INVALID_FUNCTION", "at least one function input is required")
        self._definitions[definition.key] = definition

    def resolve(self, function_id: str, version: int | None = None) -> StrategyFunctionDefinition:
        candidates = [value for key, value in self._definitions.items() if key[0] == function_id]
        if version is not None:
            candidates = [value for value in candidates if value.version == version]
        elif any(not value.deprecated for value in candidates):
            candidates = [value for value in candidates if not value.deprecated]
        if not candidates:
            suffix = f"@{version}" if version is not None else ""
            raise StrategyFunctionRegistryError("FUNCTION_NOT_FOUND", f"strategy function {function_id}{suffix} was not found")
        return max(candidates, key=lambda value: value.version)

    def list(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        asset_type: str | None = None,
        version: int | None = None,
        include_deprecated: bool = False,
    ) -> tuple[StrategyFunctionDefinition, ...]:
        values = list(self._definitions.values())
        if not include_deprecated:
            values = [value for value in values if not value.deprecated]
        if query:
            normalized = query.casefold().strip()
            values = [
                value
                for value in values
                if normalized in " ".join((value.function_id, value.display_name, value.description)).casefold()
            ]
        if category:
            normalized_category = category.casefold().strip()
            values = [
                value
                for value in values
                if normalized_category in {value.category.casefold(), value.category_label.casefold()}
            ]
        if asset_type:
            normalized_asset_type = asset_type.upper().strip()
            values = [value for value in values if normalized_asset_type in value.supported_asset_types]
        if version is not None:
            values = [value for value in values if value.version == version]
        return tuple(sorted(values, key=lambda value: (value.category, value.function_id, value.version)))


def _required_fields(item: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = item.get("requiredFields")
    if isinstance(explicit, list):
        return tuple(str(value) for value in explicit)
    data = str(item.get("data") or "").replace("、", ",")
    allowed = {"open", "high", "low", "close", "volume", "limit_up", "limit_down"}
    return tuple(value.strip() for value in data.split(",") if value.strip() in allowed)


def _inputs(item: Mapping[str, Any], required_fields: tuple[str, ...]) -> tuple[FunctionInput, ...]:
    result = [FunctionInput(name=value, value_type="series<number>") for value in required_fields]
    for raw in item.get("parameters") or ():
        if isinstance(raw, str):
            value_type = "integer" if raw in _INTEGER_PARAMETERS else "number"
            result.append(FunctionInput(raw, value_type, minimum=1 if value_type == "integer" else None))
            continue
        if isinstance(raw, Mapping):
            result.append(
                FunctionInput(
                    name=str(raw["name"]),
                    value_type="number" if str(raw.get("type")) == "float" else str(raw.get("type") or "number"),
                    required=bool(raw.get("required", True)),
                    default=raw.get("default"),
                    minimum=raw.get("minimum"),
                    maximum=raw.get("maximum"),
                    description=str(raw.get("description") or ""),
                )
            )
    # Universe-only functions have structured inputs rather than bar fields.
    if not result and item.get("id") == "market_scope":
        result.append(FunctionInput("market_types", "array<string>"))
    return tuple(result)


def _legacy_definition(item: Mapping[str, Any]) -> StrategyFunctionDefinition:
    function_id = str(item["id"])
    category, category_label = _CATEGORY_BY_ID.get(function_id, ("other", "其他"))
    fields = _required_fields(item)
    return StrategyFunctionDefinition(
        function_id=function_id,
        version=1,
        display_name=str(item["name"]),
        category=category,
        category_label=category_label,
        description=str(item.get("definition") or ""),
        inputs=_inputs(item, fields),
        output_type=str(item.get("returnType") or "boolean_or_number"),
        supported_asset_types=_ALL_ASSET_TYPES,
        runtime_name=function_id,
        example=str(item.get("example") or ""),
        required_fields=fields,
    )


def _canonical_definition(item: Mapping[str, Any]) -> StrategyFunctionDefinition:
    inputs = tuple(
        FunctionInput(
            str(name),
            str(value_type),
            minimum=1 if value_type == "integer" else None,
        )
        for name, value_type in item["inputs"]
    )
    required_fields = tuple(
        input_item.name
        for input_item in inputs
        if input_item.name in {"open", "high", "low", "close", "volume"}
    )
    return StrategyFunctionDefinition(
        function_id=str(item["id"]),
        version=int(item.get("version") or 1),
        display_name=str(item["name"]),
        category=str(item["category"]),
        category_label=str(item["label"]),
        description=str(item["description"]),
        inputs=inputs,
        output_type=str(item["output"]),
        supported_asset_types=tuple(item.get("assets") or _ALL_ASSET_TYPES),
        runtime_name=str(item["runtime"]),
        required_fields=required_fields,
    )


def build_builtin_strategy_function_registry() -> StrategyFunctionRegistry:
    """Create a fresh registry so tests and API callers cannot mutate a global singleton."""

    definitions = [_legacy_definition(item) for item in STRATEGY_FUNCTION_CATALOG]
    definitions.extend(_canonical_definition(item) for item in _CANONICAL_CATALOG)
    definitions.extend(_canonical_definition(item) for item in _CANONICAL_VERSION_OVERRIDES)
    return StrategyFunctionRegistry(definitions)


__all__ = [
    "FunctionInput",
    "StrategyFunctionDefinition",
    "StrategyFunctionRegistry",
    "StrategyFunctionRegistryError",
    "build_builtin_strategy_function_registry",
]
