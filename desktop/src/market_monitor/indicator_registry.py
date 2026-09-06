"""Versioned visual-resource catalog for market and external-market indicators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from market_monitor.strategy_resources import (
    ResourceKind,
    ResourceOrigin,
    StrategyResourceError,
    validate_resource_definition,
)


_BUILTIN_TIMESTAMP = "2026-09-02T00:00:00+08:00"


class IndicatorResourceError(ValueError):
    """Raised when a persisted custom Indicator breaks its safe template contract."""


@dataclass(frozen=True)
class IndicatorDefinition:
    indicator_id: str
    version: int
    name: str
    english_name: str
    category: str
    category_label: str
    description: str
    placement: str
    supported_asset_types: tuple[str, ...]
    dependencies: tuple[tuple[str, int], ...]
    parameters: tuple[dict[str, Any], ...]
    plots: tuple[dict[str, Any], ...]
    origin: str = "builtin"
    status: str = "active"
    unavailable_code: str | None = None
    unavailable_reason: str | None = None
    math_formula: str = ""
    formula_source: str | None = None
    required_fields: tuple[str, ...] = ()
    warmup_bars: str = ""
    limitations: tuple[str, ...] = ()
    resource_kind: str = "indicator"
    calculation_id: str | None = None
    created_at: str = _BUILTIN_TIMESTAMP
    updated_at: str = _BUILTIN_TIMESTAMP

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "resourceKind": self.resource_kind,
            "id": self.indicator_id,
            "version": self.version,
            "versionedId": f"{self.indicator_id}@{self.version}",
            "name": self.name,
            "englishName": self.english_name,
            "displayName": self.name,
            "category": self.category,
            "categoryLabel": self.category_label,
            "description": self.description,
            "definition": self.description,
            "placement": self.placement,
            "supportedAssetTypes": list(self.supported_asset_types),
            "dependencies": [
                {"resourceKind": "strategy_function", "id": function_id, "version": version}
                for function_id, version in self.dependencies
            ],
            "parameters": [dict(value) for value in self.parameters],
            "plots": [dict(value) for value in self.plots],
            "origin": self.origin,
            "status": self.status,
            "capabilities": ["market_data_input", "plot_create"],
            "unavailableCode": self.unavailable_code,
            "unavailableReason": self.unavailable_reason,
            "mathFormula": self.math_formula,
            "formulaSource": self.formula_source,
            "requiredFields": list(self.required_fields),
            "warmupBars": self.warmup_bars,
            "limitations": list(self.limitations),
            "calculationId": self.calculation_id or self.indicator_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


class IndicatorRegistry:
    def __init__(self, definitions: Iterable[IndicatorDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], IndicatorDefinition] = {}
        for definition in definitions:
            key = (definition.indicator_id, definition.version)
            if key in self._definitions:
                raise ValueError(f"duplicate indicator {definition.indicator_id}@{definition.version}")
            self._definitions[key] = definition

    def register(self, definition: IndicatorDefinition) -> None:
        key = (definition.indicator_id, definition.version)
        if key in self._definitions:
            raise ValueError(f"duplicate indicator {definition.indicator_id}@{definition.version}")
        self._definitions[key] = definition

    def list(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        asset_type: str | None = None,
        origin: str | None = None,
    ) -> tuple[IndicatorDefinition, ...]:
        values = list(self._definitions.values())
        if query:
            term = query.casefold().strip()
            values = [value for value in values if term in " ".join((value.name, value.english_name, value.description)).casefold()]
        if category:
            term = category.casefold().strip()
            values = [value for value in values if term in {value.category.casefold(), value.category_label.casefold()}]
        if asset_type:
            term = asset_type.upper().strip()
            values = [value for value in values if term in value.supported_asset_types]
        if origin:
            values = [value for value in values if value.origin == origin]
        return tuple(sorted(values, key=lambda value: (value.category, value.indicator_id, value.version)))

    def resolve(self, indicator_id: str, version: int | None = None) -> IndicatorDefinition:
        values = [value for key, value in self._definitions.items() if key[0] == indicator_id]
        if version is not None:
            values = [value for value in values if value.version == version]
        if not values:
            raise KeyError(indicator_id)
        return max(values, key=lambda value: value.version)


_ASSETS = ("STOCK", "B_SHARE", "INDEX", "FUTURE", "ETF", "LOF", "REIT", "FUND")


def _parameter(name: str, default: int | float, minimum: int | float, maximum: int | float) -> dict[str, Any]:
    return {"name": name, "type": "integer" if isinstance(default, int) else "number", "default": default, "minimum": minimum, "maximum": maximum}


def _indicator(
    indicator_id: str,
    name: str,
    english_name: str,
    category: str,
    category_label: str,
    description: str,
    placement: str,
    dependencies: tuple[str, ...],
    parameters: tuple[dict[str, Any], ...],
    plots: tuple[tuple[str, str], ...],
    *,
    status: str = "active",
    unavailable_code: str | None = None,
    unavailable_reason: str | None = None,
    math_formula: str = "",
    formula_source: str | None = None,
    required_fields: tuple[str, ...] = (),
    warmup_bars: str = "",
    limitations: tuple[str, ...] = (),
    resource_kind: str = "indicator",
) -> IndicatorDefinition:
    return IndicatorDefinition(
        indicator_id,
        1,
        name,
        english_name,
        category,
        category_label,
        description,
        placement,
        _ASSETS,
        tuple((value, 1) for value in dependencies),
        parameters,
        tuple({"id": plot_id, "type": plot_type} for plot_id, plot_type in plots),
        status=status,
        unavailable_code=unavailable_code,
        unavailable_reason=unavailable_reason,
        math_formula=math_formula,
        formula_source=formula_source,
        required_fields=required_fields,
        warmup_bars=warmup_bars,
        limitations=limitations,
        resource_kind=resource_kind,
    )


def build_builtin_indicator_registry() -> IndicatorRegistry:
    p = _parameter
    definitions = (
        _indicator("indicator.ma", "移动平均线", "Moving Average", "trend", "趋势", "收盘价的简单移动平均线。", "overlay", ("technical.sma",), (p("lookback", 20, 1, 500),), (("ma", "line"),)),
        _indicator("indicator.ema", "指数移动平均线", "Exponential Moving Average", "trend", "趋势", "对近期价格赋予更高权重的移动平均线。", "overlay", ("technical.ema",), (p("lookback", 20, 1, 500),), (("ema", "line"),)),
        _indicator("indicator.bollinger", "布林带", "Bollinger Bands", "volatility", "波动率", "移动平均线加减标准差倍数形成的通道。", "overlay", ("technical.bollinger",), (p("lookback", 20, 2, 500), p("multiplier", 2.0, 0.1, 20.0)), (("upper", "line"), ("middle", "line"), ("lower", "line"))),
        _indicator("indicator.keltner", "肯特纳通道", "Keltner Channel", "price_channel", "价格通道", "EMA 中轨与 ATR 宽度构成的价格通道。", "overlay", ("technical.keltner",), (p("emaLookback", 20, 2, 500), p("atrLookback", 10, 2, 500), p("multiplier", 2.0, 0.1, 20.0)), (("upper", "line"), ("middle", "line"), ("lower", "line"))),
        _indicator("indicator.donchian", "唐奇安通道", "Donchian Channel", "price_channel", "价格通道", "滚动最高价和最低价形成的突破通道。", "overlay", ("technical.donchian",), (p("lookback", 20, 2, 500),), (("upper", "line"), ("middle", "line"), ("lower", "line"))),
        _indicator("indicator.rsi", "相对强弱指数", "RSI", "momentum", "动量", "以平滑涨跌幅衡量价格动量。", "pane", ("technical.rsi",), (p("lookback", 14, 2, 200),), (("rsi", "line"),)),
        _indicator("indicator.macd", "指数平滑异同移动平均线", "MACD", "momentum", "动量", "快慢 EMA 差、信号线及柱状值。", "pane", ("technical.macd",), (p("fast", 12, 2, 200), p("slow", 26, 3, 500), p("signal", 9, 2, 200)), (("macd", "line"), ("signal", "line"), ("histogram", "histogram"))),
        _indicator("indicator.stochastic", "随机振荡器", "Stochastic Oscillator", "momentum", "动量", "收盘价在近期高低区间中的相对位置。", "pane", ("technical.stochastic",), (p("lookback", 14, 2, 200), p("smooth", 3, 1, 50)), (("k", "line"), ("d", "line"))),
        _indicator("indicator.atr", "平均真实波幅", "ATR", "volatility", "波动率", "Wilder 平滑后的真实波幅。", "pane", ("technical.atr",), (p("lookback", 14, 2, 200),), (("atr", "line"),)),
        _indicator(
            "indicator.chaikin_volatility", "蔡金波动率", "Chaikin Volatility", "volatility", "波动率",
            "EMA(H-L) 相对指定历史位置的百分比变化。", "pane", ("volatility.chaikin_volatility",),
            (p("emaLookback", 10, 2, 200), p("changeLookback", 10, 1, 200)), (("value", "line"),),
            math_formula="100 × (EMA(H-L, emaLookback)[t] / EMA(H-L, emaLookback)[t-changeLookback] − 1)",
            formula_source="https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/volatility",
            required_fields=("high", "low"), warmup_bars="emaLookback + changeLookback",
            limitations=("历史 EMA(H-L) 为 0 时该点为 null", "不包含跳空，不使用成交量"),
        ),
        _indicator(
            "indicator.twiggs_volatility", "Twiggs 波动率（ATR% 公开变体）", "Twiggs Volatility (ATR% public variant)", "volatility", "波动率",
            "以公开可复算 ATR 百分比变体展示波动率；并非声称复现 Twiggs® 专有公式。", "pane", ("volatility.atr_percent",),
            (p("lookback", 21, 2, 200),), (("value", "line"),),
            math_formula="100 × Wilder ATR(high, low, close, lookback) / close",
            formula_source="https://www.incrediblecharts.com/indicators/twiggs-proprietary-indicators.php",
            required_fields=("high", "low", "close"), warmup_bars="lookback",
            limitations=("Twiggs® 原始算法是专有实现，当前为明确标注的公开 ATR% 变体", "close 为 0 时该点为 null"),
        ),
        _indicator(
            "indicator.rvi", "相对波动指数", "Relative Volatility Index", "volatility", "波动率",
            "Dorsey 1993 收盘价版本：按涨跌日拆分标准差并进行 Wilder 平滑。", "pane", ("volatility.relative_volatility_index",),
            (p("stddevLookback", 10, 2, 200), p("smoothLookback", 14, 2, 200)), (("rvi", "line"),),
            math_formula="100 × WilderAvg(up stddev) / (WilderAvg(up stddev) + WilderAvg(down stddev))",
            formula_source="https://user42.tuxfamily.org/chart/manual/Relative-Volatility-Index.html",
            required_fields=("close",), warmup_bars="stddevLookback + smoothLookback − 1",
            limitations=("采用 Dorsey 1993 close-based 版本，不是同名 Relative Vigor Index", "结果范围 0 至 100；双侧均为 0 时返回 50"),
        ),
        _indicator(
            "indicator.volume_profile", "成交量分布", "Volume Profile", "advanced_chart", "高级图表",
            "按当前可见 K 线区间的 HLC3 代表价格分桶；每根 bar 的完整成交量只进入一个桶。", "overlay",
            ("market.volume_profile",), (p("bins", 24, 4, 200), p("valueAreaPercent", 70.0, 1.0, 100.0)),
            (("profile", "profile"),),
            math_formula="每根 bar 的 volume 全量分配至 HLC3=(high+low+close)/3 所在等宽价格桶；POC 为最大桶，价值区从 POC 向相邻较大桶扩展至目标比例。",
            required_fields=("high", "low", "close", "volume"),
            warmup_bars="0；仅计算当前可见区间",
            limitations=("缺少或无正成交量时不可用，不以 amount 替代 volume", "价格范围恒定时退化为单桶", "每根 bar 不在其高低区间内拆分成交量"),
        ),
        _indicator(
            "drawing.fibonacci_retracement", "斐波那契回撤", "Fibonacci Retracement", "drawing_tool", "绘图工具",
            "双锚点画线；比例、价格标签和样式均以逻辑时间/价格坐标保存。", "overlay", (), (), (),
            math_formula="price(ratio) = anchorEnd + (anchorStart − anchorEnd) × ratio；0% 为第二锚点，100% 为第一锚点。",
            warmup_bars="不适用（用户双锚点）",
            limitations=("这是 drawing_tool，不是技术指标或 Strategy Function，不能被策略规则引用", "至少两个锚点；比例有限且单条画线最多 16 个", "不保存屏幕像素；历史画线文档无需迁移"),
            resource_kind="drawing_tool",
        ),
        _indicator(
            "indicator.vix", "VIX 恐慌指数", "CBOE Volatility Index", "external_market", "外部市场",
            "仅使用带 PASS 来源证据的本地 VIX 标准序列，并按交易日与当前图表精确对齐。", "pane",
            (), (), (("vix", "line"),),
            math_formula="VIX[t] = local_verified_vix[tradingDate(t)]；没有同日点时为 null。",
            required_fields=(), warmup_bars="0；不从当前标的 OHLC 推导",
            limitations=(
                "页面不直连第三方；仅接受 external_market/vix/series.json 中 sourceStatus=PASS 的本地标准序列",
                "美中交易日差异和缺失日返回 null，不前值填充",
                "当前没有经过独立来源探针写入的序列时，实例返回 MISSING_DATASOURCE",
            ),
        ),
        # Read-only aliases retained for clients that consumed the R3 catalog IDs.
        _indicator("ma", "MA 移动平均线（兼容）", "Moving Average", "legacy", "兼容", "旧行情页指标 ID；新实例应使用 indicator.ma。", "overlay", ("technical.sma",), (p("lookback", 20, 1, 500),), (("ma", "line"),), status="deprecated"),
        _indicator("sd", "SD 标准差（兼容）", "Standard Deviation", "legacy", "兼容", "旧行情页指标 ID；新实例应使用版本化指标定义。", "pane", ("technical.stddev",), (p("lookback", 20, 2, 500),), (("sd", "line"),), status="deprecated"),
        _indicator("bollinger", "布林带（兼容）", "Bollinger Bands", "legacy", "兼容", "旧行情页指标 ID；新实例应使用 indicator.bollinger。", "overlay", ("technical.bollinger",), (p("lookback", 20, 2, 500), p("multiplier", 2.0, 0.1, 20.0)), (("upper", "line"), ("middle", "line"), ("lower", "line")), status="deprecated"),
        _indicator("atr", "ATR（兼容）", "ATR", "legacy", "兼容", "旧行情页指标 ID；新实例应使用 indicator.atr。", "pane", ("technical.atr",), (p("lookback", 14, 2, 200),), (("atr", "line"),), status="deprecated"),
        _indicator("hsar", "HSAR（兼容）", "Horizontal Support and Resistance", "legacy", "兼容", "旧横向支撑阻力指标。", "overlay", ("hsar_resistance", "hsar_support"), (p("lookback", 20, 2, 500), p("topPercent", 20.0, 0.1, 100.0)), (("resistance", "line"), ("support", "line")), status="deprecated"),
        _indicator("volume", "成交量（兼容）", "Volume", "legacy", "兼容", "旧行情页原始成交量副图。", "pane", (), (), (("volume", "histogram"),), status="deprecated"),
    )
    return IndicatorRegistry(definitions)


def build_indicator_registry(custom_documents: Iterable[Mapping[str, Any]] = ()) -> IndicatorRegistry:
    """Combine published definitions with valid local custom Indicator copies.

    Bad local files are deliberately ignored at read time.  They cannot enter
    through the write API, but this makes a manually damaged data directory a
    localized catalog problem instead of an outage for every market chart.
    """

    registry = build_builtin_indicator_registry()
    for document in custom_documents:
        try:
            registry.register(custom_indicator_definition(document))
        except (IndicatorResourceError, ValueError):
            continue
    return registry


def custom_indicator_definition(document: Mapping[str, Any]) -> IndicatorDefinition:
    """Turn a validated custom Indicator resource into a safe catalog definition.

    R4 deliberately permits configurable copies of published Indicator
    definitions, not arbitrary formula or browser code.  The copied function
    dependencies, parameter schema and Plot topology must stay identical to
    the selected published calculation template.
    """

    try:
        resource = validate_resource_definition(document)
    except StrategyResourceError as error:
        raise IndicatorResourceError(error.message) from error
    if resource.resource_kind != ResourceKind.INDICATOR or resource.origin != ResourceOrigin.CUSTOM:
        raise IndicatorResourceError("custom indicator resources must use indicator/custom")
    definition = resource.definition
    required = {
        "english_name", "category", "category_label", "description", "placement",
        "parameters", "plots", "calculation_id",
    }
    if not required.issubset(definition) or not isinstance(definition["calculation_id"], str):
        raise IndicatorResourceError("custom indicator definition is missing required template fields")
    base_registry = build_builtin_indicator_registry()
    try:
        base = base_registry.resolve(str(definition["calculation_id"]), 1)
    except KeyError as error:
        raise IndicatorResourceError("custom indicator calculation template was not found") from error
    if base.resource_kind != "indicator" or base.status != "active":
        raise IndicatorResourceError("custom indicator calculation template must be an active chart indicator")
    dependency_keys = tuple((item.resource_id, item.version) for item in resource.dependencies)
    if dependency_keys != base.dependencies:
        raise IndicatorResourceError("custom indicator must preserve exact published function dependencies")
    if not set(resource.supported_asset_types).issubset(base.supported_asset_types):
        raise IndicatorResourceError("custom indicator assets must be a subset of its calculation template")
    if str(definition["placement"]) != base.placement:
        raise IndicatorResourceError("custom indicator must preserve calculation template placement")
    if tuple(definition["plots"]) != base.plots:
        raise IndicatorResourceError("custom indicator must preserve calculation template plots")
    if not isinstance(definition["parameters"], list):
        raise IndicatorResourceError("custom indicator parameters must be an array")
    parameters = tuple(dict(item) for item in definition["parameters"] if isinstance(item, Mapping))
    if len(parameters) != len(definition["parameters"]) or [item.get("name") for item in parameters] != [item["name"] for item in base.parameters]:
        raise IndicatorResourceError("custom indicator must preserve calculation template parameter names")
    for supplied, expected in zip(parameters, base.parameters, strict=True):
        if any(supplied.get(key) != expected.get(key) for key in ("name", "type", "minimum", "maximum")):
            raise IndicatorResourceError("custom indicator must preserve parameter types and bounds")
        value = supplied.get("default")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise IndicatorResourceError("custom indicator parameter default must be numeric")
        if value < expected["minimum"] or value > expected["maximum"]:
            raise IndicatorResourceError("custom indicator parameter default is outside published bounds")
    return IndicatorDefinition(
        indicator_id=resource.resource_id,
        version=resource.version,
        name=resource.display_name,
        english_name=str(definition["english_name"]),
        category=str(definition["category"]),
        category_label=str(definition["category_label"]),
        description=str(definition["description"]),
        placement=base.placement,
        supported_asset_types=resource.supported_asset_types,
        dependencies=base.dependencies,
        parameters=parameters,
        plots=base.plots,
        origin=resource.origin.value,
        status=resource.status.value,
        unavailable_code=base.unavailable_code,
        unavailable_reason=base.unavailable_reason,
        math_formula=base.math_formula,
        formula_source=base.formula_source,
        required_fields=base.required_fields,
        warmup_bars=base.warmup_bars,
        limitations=base.limitations,
        resource_kind="indicator",
        calculation_id=base.indicator_id,
        created_at=resource.created_at,
        updated_at=resource.updated_at,
    )


__all__ = [
    "IndicatorDefinition", "IndicatorRegistry", "IndicatorResourceError",
    "build_builtin_indicator_registry", "build_indicator_registry",
    "custom_indicator_definition",
]
