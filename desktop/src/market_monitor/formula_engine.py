"""Safe, on-demand formula programs for strategy indicators.

The module intentionally does not extend Strategy DSL v1.  Formula text is
parsed with :mod:`ast` and interpreted node by node; it is never passed to
``eval``/``exec`` or compiled as Python code.
"""

from __future__ import annotations

import ast
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

SECONDS_PER_YEAR = 365.2425 * 24 * 60 * 60
MAX_FORMULA_CHARS = 10_000
MAX_FORMULA_STATEMENTS = 100
ALLOWED_FUNCTIONS = frozenset({
    "ts_momentum", "cs_momentum", "sharpe", "calmar",
    "hsar_resistance", "hsar_support", "ma", "sd", "bollinger_upper", "bollinger_lower",
    "gann_rising_rate", "gann_falling_rate",
    "period_return", "no_limit_up", "no_limit_down", "limit_up_count", "limit_down_count",
    "close_new_high", "close_new_low", "up_count", "down_count", "up_down_ratio", "down_up_ratio",
    "range_high_low_ratio", "range_low_high_ratio", "volume_slope",
})
BUILTIN_SERIES = frozenset({
    "open", "high", "low", "close", "volume", "limit_up", "limit_down", "strategy_returns", "strategy_nav",
})
_FUNCTION_ARITY = {
    "ts_momentum": 2, "cs_momentum": 3, "sharpe": 3, "calmar": 2,
    "hsar_resistance": 3, "hsar_support": 3, "ma": 2, "sd": 2,
    "bollinger_upper": 3, "bollinger_lower": 3, "gann_rising_rate": 2, "gann_falling_rate": 2,
    "period_return": 2, "no_limit_up": 2, "no_limit_down": 2,
    "limit_up_count": 2, "limit_down_count": 2, "close_new_high": 3, "close_new_low": 3,
    "up_count": 3, "down_count": 3, "up_down_ratio": 3, "down_up_ratio": 3,
    "range_high_low_ratio": 3, "range_low_high_ratio": 3, "volume_slope": 3,
}

INDICATOR_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "ts_momentum",
        "name": "时序动量",
        "definition": "单一标的当前收盘价相对回看期收盘价的累计收益。",
        "mathFormula": "close[t] / close[t-lookback] - 1",
        "parameters": ["lookback"],
        "data": "同一标的、同一周期的收盘价",
        "example": "value = ts_momentum(close, lookback)\nsignal = value > threshold",
    },
    {
        "id": "cs_momentum",
        "name": "截面动量",
        "definition": "同市场、同资产类型、同周期标的累计收益的平均名次分档。",
        "mathFormula": "rank(close[t] / close[t-lookback] - 1) -> bucket",
        "parameters": ["lookback", "buckets"],
        "data": "单一市场、单一资产类型、同一周期的标的集合",
        "example": "value = cs_momentum(close, lookback, buckets)\nsignal = value == buckets",
    },
    {
        "id": "sharpe",
        "name": "夏普比率",
        "definition": "策略每承担一单位年化波动率获得的年化超额收益。",
        "mathFormula": "(CAGR - risk_free_rate) / annualized_volatility",
        "parameters": ["risk_free_rate", "lookback"],
        "data": "当前策略逐期盯市收益率",
        "example": "value = sharpe(strategy_returns, risk_free_rate, lookback)\nsignal = value > threshold",
    },
    {
        "id": "calmar",
        "name": "卡玛比率",
        "definition": "策略年化收益率与最大回撤率之比。",
        "mathFormula": "CAGR / maximum_drawdown",
        "parameters": ["lookback"],
        "data": "当前策略逐期盯市净值",
        "example": "value = calmar(strategy_nav, lookback)\nsignal = value > threshold",
    },
    {
        "id": "hsar",
        "name": "HSAR 横向阻力支撑",
        "definition": "在策略设置的 K 线周期内，对近窗口高点或低点中最高的一部分取均值，得到当前箱体阻力或支撑（hsar_resistance / hsar_support）。",
        "mathFormula": "avg(top ceil(lookback × top_percent / 100) of high/low[t-lookback+1:t])",
        "parameters": ["lookback", "top_percent"],
        "data": "策略配置周期（<arg1>）内的 high、low；lookback 为整数（<arg2>），top_percent 为浮点百分比（<arg3>）",
        "example": "value = hsar_resistance(high, lookback, top_percent)\nsignal = close > value",
    },
    {
        "id": "ma",
        "name": "MA 移动平均线",
        "definition": "过去 N 个周期收盘价的算术平均值。",
        "mathFormula": "sum(close[t-lookback+1:t]) / lookback",
        "parameters": ["lookback"],
        "data": "策略配置周期（<arg1>）内的 close；lookback 为整数（<arg2>）",
        "example": "value = ma(close, lookback)\nsignal = close > value",
    },
    {
        "id": "sd",
        "name": "SD 标准差",
        "definition": "过去 N 个周期收盘价相对其移动平均值的总体标准差。",
        "mathFormula": "sqrt(sum((close - MA)^2) / lookback)",
        "parameters": ["lookback"],
        "data": "策略配置周期（<arg1>）内的 close；lookback 为整数（<arg2>）",
        "example": "value = sd(close, lookback)\nsignal = value > threshold",
    },
    {
        "id": "bollinger",
        "name": "布林带通道",
        "definition": "上轨为 MA 加 K 倍 SD，下轨为 MA 减 K 倍 SD（bollinger_upper / bollinger_lower）。",
        "mathFormula": "UB = MA + multiplier × SD; LB = MA - multiplier × SD",
        "parameters": ["lookback", "multiplier"],
        "data": "策略配置周期（<arg1>）内的 close；lookback 为整数（<arg2>），multiplier 为浮点数（<arg3>）",
        "example": "value = bollinger_upper(close, lookback, multiplier)\nsignal = close > value",
    },
    {
        "id": "gann_volatility",
        "name": "江恩理论波动率",
        "definition": "窗口首根与当前根的低点差（gann_rising_rate）或高点差（gann_falling_rate）除以窗口根数；仅使用当根及历史 K 线。",
        "mathFormula": "rising = (low[t] - low[t-lookback+1]) / lookback; falling = (high[t-lookback+1] - high[t]) / lookback",
        "parameters": ["lookback"],
        "data": "策略配置周期（<arg1>）内的 high、low；lookback 为整数（<arg2>）",
        "example": "value = gann_rising_rate(low, lookback)\nsignal = value > threshold",
    },
)

CONDITION_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "market_scope", "name": "市场类型与 ST 排除",
        "definition": "限定 A股、港股、沪深主板、创业板、科创板、ETF、北证、国内期货、国内商品指数或国外期货；可排除 ST/*ST。",
        "parameters": [], "data": "本地品种目录与名称", "example": "在公式策略的市场类型区域勾选，可多选（OR）",
    },
    {
        "id": "market_cap", "name": "总市值 / 流通市值",
        "definition": "按亿元比较本地 F10 总市值或流通市值，缺少市值时不匹配。",
        "parameters": ["market_cap_yi"], "data": "本地 F10", "example": "在公式策略的市值筛选区域选择 > 或 < 并填写亿元数值",
    },
    {
        "id": "period_return", "name": "区间涨幅",
        "definition": "当前收盘价相对近第 lookback 根之前收盘价的累计涨幅。",
        "parameters": ["lookback", "return_threshold"], "data": "close",
        "example": "value = period_return(close, lookback)\nsignal = value > return_threshold / 100",
    },
    {
        "id": "no_limit", "name": "近 N 根没有涨停 / 跌停",
        "definition": "近 lookback 根 K 线内没有可确认的涨停或跌停；未知标记不会被当作没有涨跌停。",
        "parameters": ["lookback"], "data": "A股日线涨跌停标记",
        "example": "value = limit_up_count(limit_up, lookback)\nsignal = no_limit_up(limit_up, lookback)",
    },
    {
        "id": "close_breakout", "name": "收盘价创新高 / 新低",
        "definition": "当前收盘价突破此前 lookback 根 K 线最高价，或跌破此前 lookback 根最低价。",
        "parameters": ["lookback"], "data": "close、high、low",
        "example": "value = period_return(close, lookback)\nsignal = close_new_high(close, high, lookback)",
    },
    {
        "id": "limit_count", "name": "涨停 / 跌停数量",
        "definition": "统计近 lookback 根 K 线中已确认的涨停或跌停数量，可与整数阈值比较。",
        "parameters": ["lookback", "count_threshold"], "data": "A股日线涨跌停标记",
        "example": "value = limit_up_count(limit_up, lookback)\nsignal = value > count_threshold",
    },
    {
        "id": "return_difference", "name": "两段区间涨幅差",
        "definition": "短回看期累计涨幅减去长回看期累计涨幅。",
        "parameters": ["short_lookback", "long_lookback", "difference_threshold"], "data": "close",
        "example": "value = period_return(close, short_lookback) - period_return(close, long_lookback)\nsignal = value > difference_threshold / 100",
    },
    {
        "id": "direction_count", "name": "上涨 / 下跌 K 线数量",
        "definition": "按每根 K 线 close>open 或 close<open 统计近 lookback 根上涨或下跌数量。",
        "parameters": ["lookback", "count_threshold"], "data": "open、close",
        "example": "value = up_count(open, close, lookback)\nsignal = value > count_threshold",
    },
    {
        "id": "direction_ratio", "name": "上涨 / 下跌数量比",
        "definition": "近 lookback 根上涨 K 线数与下跌 K 线数之比，也支持反向比值；分母为零时不可计算。",
        "parameters": ["lookback", "ratio_threshold"], "data": "open、close",
        "example": "value = up_down_ratio(open, close, lookback)\nsignal = value > ratio_threshold",
    },
    {
        "id": "range_ratio", "name": "区间最高 / 最低价比",
        "definition": "近 lookback 根区间最高价与区间最低价之比，也支持反向比值。",
        "parameters": ["lookback", "ratio_threshold"], "data": "high、low",
        "example": "value = range_high_low_ratio(high, low, lookback)\nsignal = value > ratio_threshold",
    },
    {
        "id": "volume_slope", "name": "两根 K 线成交量变化速率",
        "definition": "两根指定倒数位置 K 线的成交量之差，除以两者间隔数量加一。倒数位置从 1 开始。",
        "parameters": ["first_offset", "second_offset"], "data": "volume",
        "example": "value = volume_slope(volume, first_offset, second_offset)\nsignal = value > threshold",
    },
    {
        "id": "example_strategy_a", "name": "组合示例：策略 A",
        "definition": "A股个股、排除 ST、总市值大于 200 亿元，并同时满足最新价突破 HSAR 阻力和近 10 根上涨/下跌数量比大于 1.5。",
        "parameters": ["box_lookback", "top_percent", "ratio_lookback", "ratio_threshold"],
        "data": "本地品种目录、F10、日线 OHLC",
        "example": "resistance = hsar_resistance(high, box_lookback, top_percent)\nvalue = up_down_ratio(open, close, ratio_lookback)\nsignal = (close > resistance) and (value > ratio_threshold)",
    },
)

# 图形指标和策略函数的目录有意分开。旧 ``INDICATOR_CATALOG`` 保留给
# formula_v1 兼容文档，桌面新页面只消费以下两个目录。
CHART_INDICATOR_CATALOG: tuple[dict[str, Any], ...] = tuple(
    item for item in INDICATOR_CATALOG if item["id"] in {"hsar", "ma", "sd", "bollinger"}
) + (
    {
        "id": "atr", "name": "ATR 通道", "definition": "EMA 中线加减 ATR 倍数的趋势通道。",
        "mathFormula": "EMA(close, center) ± multiplier × ATR(atr)",
        "parameters": ["centerLookback", "atrLookback", "multiplier"], "data": "high、low、close",
        "example": "中心 EMA(20)，ATR(14)，倍数 2",
    },
    {
        "id": "volume", "name": "成交量", "definition": "在副图显示 K 线原始成交量。",
        "mathFormula": "volume", "parameters": [], "data": "volume", "example": "显示成交量副图",
    },
)

STRATEGY_FUNCTION_CATALOG: tuple[dict[str, Any], ...] = tuple(
    item for item in CONDITION_CATALOG if item["id"] != "example_strategy_a"
) + (
    {
        "id": "gann_rising_rate", "name": "江恩上升波动率", "returnType": "number",
        "parameters": [{"name": "lookback", "type": "integer", "minimum": 2}],
        "requiredFields": ["low"], "definition": "当前低点与窗口首根低点的变化速率。",
    },
    {
        "id": "gann_falling_rate", "name": "江恩下降波动率", "returnType": "number",
        "parameters": [{"name": "lookback", "type": "integer", "minimum": 2}],
        "requiredFields": ["high"], "definition": "窗口首根高点与当前高点的变化速率。",
    },
    {
        "id": "hsar_resistance", "name": "HSAR 阻力价", "returnType": "number",
        "parameters": [{"name": "lookback", "type": "integer", "minimum": 2}, {"name": "topPercent", "type": "float", "minimum": 0.1, "maximum": 100}],
        "requiredFields": ["high"], "definition": "窗口最高价中最高百分位均值。",
    },
    {
        "id": "hsar_support", "name": "HSAR 支撑价", "returnType": "number",
        "parameters": [{"name": "lookback", "type": "integer", "minimum": 2}, {"name": "topPercent", "type": "float", "minimum": 0.1, "maximum": 100}],
        "requiredFields": ["low"], "definition": "窗口最低价中最高百分位均值。",
    },
)


class FormulaError(ValueError):
    """A stable validation/runtime error suitable for an API response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class FormulaProgram:
    expression: str
    assignments: Mapping[str, ast.AST]
    dependencies: frozenset[str]


@dataclass(frozen=True)
class MetricValue:
    value: float | None
    reason: str | None = None


def _formula_error(code: str, message: str) -> FormulaError:
    return FormulaError(code, message)


def _validate_node(node: ast.AST, allowed_names: set[str]) -> None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
            return
        raise _formula_error("UNSAFE_SYNTAX", "公式只允许数值和布尔常量")
    if isinstance(node, ast.Name):
        if node.id not in allowed_names:
            raise _formula_error("UNKNOWN_NAME", f"未知变量：{node.id}")
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)):
            raise _formula_error("UNSAFE_SYNTAX", "公式包含不允许的运算符")
        _validate_node(node.left, allowed_names)
        _validate_node(node.right, allowed_names)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            raise _formula_error("UNSAFE_SYNTAX", "公式包含不允许的一元运算")
        _validate_node(node.operand, allowed_names)
        return
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise _formula_error("UNSAFE_SYNTAX", "公式包含不允许的布尔运算")
        for value in node.values:
            _validate_node(value, allowed_names)
        return
    if isinstance(node, ast.Compare):
        if not all(isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops):
            raise _formula_error("UNSAFE_SYNTAX", "公式包含不允许的比较运算")
        _validate_node(node.left, allowed_names)
        for comparator in node.comparators:
            _validate_node(comparator, allowed_names)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
            name = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise _formula_error("UNKNOWN_FUNCTION", f"不允许调用函数：{name}")
        if node.keywords:
            raise _formula_error("UNSAFE_SYNTAX", "指标函数不接受关键字参数")
        expected = _FUNCTION_ARITY[node.func.id]
        if len(node.args) != expected:
            raise _formula_error("INVALID_ARGUMENTS", f"{node.func.id} 需要 {expected} 个参数")
        for argument in node.args:
            _validate_node(argument, allowed_names)
        return
    raise _formula_error("UNSAFE_SYNTAX", f"公式不允许使用 {type(node).__name__}")


def parse_formula(expression: str, parameter_names: Sequence[str] = ()) -> FormulaProgram:
    if not isinstance(expression, str) or not expression.strip():
        raise _formula_error("EMPTY_FORMULA", "公式不能为空")
    if len(expression) > MAX_FORMULA_CHARS:
        raise _formula_error("FORMULA_TOO_LARGE", f"公式不能超过 {MAX_FORMULA_CHARS} 个字符")
    try:
        tree = ast.parse(expression, mode="exec")
    except SyntaxError as error:
        raise _formula_error("INVALID_SYNTAX", f"公式语法错误（第 {error.lineno or 1} 行）") from error
    if len(tree.body) > MAX_FORMULA_STATEMENTS:
        raise _formula_error("FORMULA_TOO_LARGE", f"公式不能超过 {MAX_FORMULA_STATEMENTS} 条语句")

    assignments: dict[str, ast.AST] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            raise _formula_error("UNSAFE_SYNTAX", "公式只允许简单变量赋值")
        name = statement.targets[0].id
        if name.startswith("_") or name in ALLOWED_FUNCTIONS or name in BUILTIN_SERIES:
            raise _formula_error("RESERVED_NAME", f"不能为保留名称赋值：{name}")
        if name in assignments:
            raise _formula_error("DUPLICATE_ASSIGNMENT", f"变量重复赋值：{name}")
        assignments[name] = statement.value
    if "value" not in assignments or "signal" not in assignments:
        raise _formula_error("MISSING_OUTPUT", "公式必须同时赋值数值输出 value 和布尔输出 signal")

    parameters = set(parameter_names)
    if any(not name.isidentifier() or name.startswith("_") for name in parameters):
        raise _formula_error("INVALID_PARAMETER", "参数名称必须是安全标识符")
    allowed_names = set(BUILTIN_SERIES) | parameters | set(assignments)
    for node in assignments.values():
        _validate_node(node, allowed_names)

    dependencies: set[str] = set()
    visiting: set[str] = set()

    def visit_assignment(name: str) -> None:
        if name in dependencies:
            return
        if name in visiting:
            raise _formula_error("CYCLIC_DEPENDENCY", f"变量存在循环依赖：{name}")
        visiting.add(name)
        for child in ast.walk(assignments[name]):
            if isinstance(child, ast.Name) and child.id in assignments:
                visit_assignment(child.id)
        visiting.remove(name)
        dependencies.add(name)

    visit_assignment("value")
    visit_assignment("signal")
    return FormulaProgram(expression, assignments, frozenset(dependencies))


def validate_formula_document(document: Mapping[str, Any]) -> FormulaProgram:
    if document.get("formula_version", 1) != 1:
        raise _formula_error("UNSUPPORTED_VERSION", "仅支持 formula_version=1")
    universe = document.get("universe")
    if not isinstance(universe, Mapping):
        raise _formula_error("INVALID_UNIVERSE", "公式策略必须配置标的范围")
    market_types = universe.get("market_types")
    has_market_types = isinstance(market_types, list) and bool(market_types) and all(
        isinstance(value, str) and value.strip() for value in market_types
    )
    if not has_market_types and (
        not str(universe.get("market") or "").strip() or not str(universe.get("asset_type") or "").strip()
    ):
        raise _formula_error("INVALID_UNIVERSE", "标的范围必须指定单一市场和单一资产类型")
    for field in ("total_market_cap_yi", "float_market_cap_yi"):
        specification = universe.get(field)
        if specification is None:
            continue
        if not isinstance(specification, Mapping) or specification.get("operator") not in {"gt", "lt"}:
            raise _formula_error("INVALID_UNIVERSE", f"{field} 必须包含 gt/lt 运算符和正数阈值")
        value = specification.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
            raise _formula_error("INVALID_UNIVERSE", f"{field} 阈值必须是正数")
    if not str(document.get("period") or "").strip():
        raise _formula_error("INVALID_PERIOD", "公式策略必须配置 K 线周期")
    parameters = document.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise _formula_error("INVALID_PARAMETER", "parameters 必须是对象")
    reserved = set(parameters) & (set(BUILTIN_SERIES) | set(ALLOWED_FUNCTIONS) | {"value", "signal"})
    if reserved:
        raise _formula_error("INVALID_PARAMETER", f"参数名称与保留名称冲突：{sorted(reserved)[0]}")
    return parse_formula(str(document.get("expression") or ""), list(parameters))


def _vectorize_binary(left: Any, right: Any, operation: Callable[[Any, Any], Any]) -> Any:
    if isinstance(left, list) or isinstance(right, list):
        size = len(left) if isinstance(left, list) else len(right)
        if isinstance(left, list) and isinstance(right, list) and len(left) != len(right):
            raise _formula_error("SERIES_LENGTH", "参与运算的序列长度不同")
        left_values = left if isinstance(left, list) else [left] * size
        right_values = right if isinstance(right, list) else [right] * size
        return [_safe_operation(operation, a, b) for a, b in zip(left_values, right_values, strict=True)]
    return _safe_operation(operation, left, right)


def _safe_operation(operation: Callable[[Any, Any], Any], left: Any, right: Any) -> Any:
    if left is None or right is None:
        return None
    try:
        value = operation(left, right)
    except (ArithmeticError, OverflowError, TypeError, ValueError):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def evaluate_formula(
    program: FormulaProgram,
    variables: Mapping[str, Any],
    functions: Mapping[str, Callable[..., Any]],
) -> dict[str, Any]:
    cache: dict[str, Any] = dict(variables)
    active: set[str] = set()

    def evaluate_name(name: str) -> Any:
        if name in cache:
            return cache[name]
        if name not in program.assignments:
            raise _formula_error("UNKNOWN_NAME", f"未知变量：{name}")
        if name in active:
            raise _formula_error("CYCLIC_DEPENDENCY", f"变量存在循环依赖：{name}")
        active.add(name)
        value = evaluate_node(program.assignments[name])
        active.remove(name)
        cache[name] = value
        return value

    def evaluate_node(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return evaluate_name(node.id)
        if isinstance(node, ast.Call):
            callback = functions.get(node.func.id)  # type: ignore[union-attr]
            if callback is None:
                raise _formula_error("MISSING_CONTEXT", f"缺少指标运行环境：{node.func.id}")
            return callback(*(evaluate_node(argument) for argument in node.args))
        if isinstance(node, ast.BinOp):
            def safe_power(left: Any, right: Any) -> Any:
                if isinstance(right, (int, float)) and abs(right) > 1000:
                    raise OverflowError
                return left**right

            operators: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
                ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
                ast.Mod: lambda a, b: a % b, ast.Pow: safe_power,
            }
            return _vectorize_binary(evaluate_node(node.left), evaluate_node(node.right), operators[type(node.op)])
        if isinstance(node, ast.UnaryOp):
            value = evaluate_node(node.operand)
            callback = (lambda item: not bool(item)) if isinstance(node.op, ast.Not) else (
                (lambda item: +item) if isinstance(node.op, ast.UAdd) else (lambda item: -item)
            )
            return [None if item is None else callback(item) for item in value] if isinstance(value, list) else callback(value)
        if isinstance(node, ast.BoolOp):
            values = [evaluate_node(item) for item in node.values]
            callback = all if isinstance(node.op, ast.And) else any
            if any(isinstance(value, list) for value in values):
                size = len(next(value for value in values if isinstance(value, list)))
                return [callback(bool(value[index] if isinstance(value, list) else value) for value in values) for index in range(size)]
            return callback(bool(value) for value in values)
        if isinstance(node, ast.Compare):
            operands = [evaluate_node(node.left), *(evaluate_node(item) for item in node.comparators)]
            callbacks: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
                ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
                ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
                ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
            }
            result: Any = True
            for index, operation in enumerate(node.ops):
                compared = _vectorize_binary(operands[index], operands[index + 1], callbacks[type(operation)])
                compared = [False if item is None else item for item in compared] if isinstance(compared, list) else bool(compared)
                result = compared if result is True else _vectorize_binary(result, compared, lambda a, b: bool(a) and bool(b))
            return result
        raise _formula_error("UNSAFE_SYNTAX", f"无法执行 {type(node).__name__}")

    value = evaluate_name("value")
    signal = evaluate_name("signal")
    value_items = value if isinstance(value, list) else [value]
    signal_items = signal if isinstance(signal, list) else [signal]
    if any(item is not None and (isinstance(item, bool) or not isinstance(item, (int, float))) for item in value_items):
        raise _formula_error("INVALID_OUTPUT", "value 必须是数值或不可计算值")
    if any(not isinstance(item, bool) for item in signal_items):
        raise _formula_error("INVALID_OUTPUT", "signal 必须是布尔值")
    return {"value": value, "signal": signal}


def time_series_momentum(values: Sequence[float | None], lookback: Any) -> list[float | None]:
    if isinstance(lookback, bool) or not isinstance(lookback, (int, float)) or int(lookback) != lookback or lookback < 1:
        raise _formula_error("INVALID_LOOKBACK", "lookback 必须是正整数")
    window = int(lookback)
    result: list[float | None] = [None] * len(values)
    for index in range(window, len(values)):
        current, previous = values[index], values[index - window]
        if current is not None and previous not in (None, 0):
            result[index] = current / previous - 1
    return result


def _positive_window(lookback: Any, *, minimum: int = 1) -> int:
    if isinstance(lookback, bool) or not isinstance(lookback, (int, float)) or int(lookback) != lookback or lookback < minimum:
        qualifier = "正整数" if minimum == 1 else f"大于等于 {minimum} 的整数"
        raise _formula_error("INVALID_LOOKBACK", f"lookback 必须是{qualifier}")
    return int(lookback)


def _rolling_windows(values: Sequence[float | None], lookback: Any) -> tuple[int, list[list[float] | None]]:
    window = _positive_window(lookback)
    result: list[list[float] | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        raw = values[index - window + 1:index + 1]
        if all(value is not None and math.isfinite(float(value)) for value in raw):
            result[index] = [float(value) for value in raw if value is not None]
    return window, result


def moving_average(values: Sequence[float | None], lookback: Any) -> list[float | None]:
    window, windows = _rolling_windows(values, lookback)
    return [sum(items) / window if items is not None else None for items in windows]


def standard_deviation(values: Sequence[float | None], lookback: Any) -> list[float | None]:
    window, windows = _rolling_windows(values, lookback)
    result: list[float | None] = []
    for items in windows:
        if items is None:
            result.append(None)
            continue
        average = sum(items) / window
        result.append(math.sqrt(sum((item - average) ** 2 for item in items) / window))
    return result


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _formula_error("INVALID_ARGUMENTS", f"{name} 必须是有限浮点数")
    return float(value)


def bollinger_upper(values: Sequence[float | None], lookback: Any, multiplier: Any) -> list[float | None]:
    factor = _finite_float(multiplier, "multiplier")
    averages = moving_average(values, lookback)
    deviations = standard_deviation(values, lookback)
    return [average + factor * deviation if average is not None and deviation is not None else None
            for average, deviation in zip(averages, deviations, strict=True)]


def bollinger_lower(values: Sequence[float | None], lookback: Any, multiplier: Any) -> list[float | None]:
    factor = _finite_float(multiplier, "multiplier")
    averages = moving_average(values, lookback)
    deviations = standard_deviation(values, lookback)
    return [average - factor * deviation if average is not None and deviation is not None else None
            for average, deviation in zip(averages, deviations, strict=True)]


def _hsar(values: Sequence[float | None], lookback: Any, top_percent: Any) -> list[float | None]:
    _window, windows = _rolling_windows(values, lookback)
    percentage = _finite_float(top_percent, "top_percent")
    if not 0 < percentage <= 100:
        raise _formula_error("INVALID_PERCENT", "top_percent 必须大于 0 且不超过 100")
    result: list[float | None] = []
    for items in windows:
        if items is None:
            result.append(None)
            continue
        selected = sorted(items, reverse=True)[:max(1, math.ceil(len(items) * percentage / 100))]
        result.append(sum(selected) / len(selected))
    return result


def hsar_resistance(high: Sequence[float | None], lookback: Any, top_percent: Any) -> list[float | None]:
    """Current resistance: mean of the highest selected high prices."""
    return _hsar(high, lookback, top_percent)


def hsar_support(low: Sequence[float | None], lookback: Any, top_percent: Any) -> list[float | None]:
    """Current support follows the requested highest-selected-low definition."""
    return _hsar(low, lookback, top_percent)


def _gann_rate(values: Sequence[float | None], lookback: Any, *, rising: bool) -> list[float | None]:
    window = _positive_window(lookback, minimum=2)
    result: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        first, current = values[index - window + 1], values[index]
        if first is not None and current is not None:
            difference = float(current) - float(first) if rising else float(first) - float(current)
            result[index] = difference / window
    return result


def gann_rising_rate(low: Sequence[float | None], lookback: Any) -> list[float | None]:
    return _gann_rate(low, lookback, rising=True)


def gann_falling_rate(high: Sequence[float | None], lookback: Any) -> list[float | None]:
    return _gann_rate(high, lookback, rising=False)


def period_return(close: Sequence[float | None], lookback: Any) -> list[float | None]:
    return time_series_momentum(close, lookback)


def _rolling_flag_count(flags: Sequence[bool | None], lookback: Any) -> list[int | None]:
    window = _positive_window(lookback)
    result: list[int | None] = [None] * len(flags)
    for index in range(window - 1, len(flags)):
        items = flags[index - window + 1:index + 1]
        if all(isinstance(item, bool) for item in items):
            result[index] = sum(bool(item) for item in items)
    return result


def limit_up_count(flags: Sequence[bool | None], lookback: Any) -> list[int | None]:
    return _rolling_flag_count(flags, lookback)


def limit_down_count(flags: Sequence[bool | None], lookback: Any) -> list[int | None]:
    return _rolling_flag_count(flags, lookback)


def no_limit_up(flags: Sequence[bool | None], lookback: Any) -> list[bool]:
    return [count == 0 if count is not None else False for count in limit_up_count(flags, lookback)]


def no_limit_down(flags: Sequence[bool | None], lookback: Any) -> list[bool]:
    return [count == 0 if count is not None else False for count in limit_down_count(flags, lookback)]


def close_new_high(
    close: Sequence[float | None], high: Sequence[float | None], lookback: Any,
) -> list[bool]:
    window = _positive_window(lookback)
    if len(close) != len(high):
        raise _formula_error("SERIES_LENGTH", "close 与 high 序列长度不同")
    result = [False] * len(close)
    for index in range(window, len(close)):
        history = high[index - window:index]
        if close[index] is not None and all(item is not None for item in history):
            result[index] = float(close[index]) > max(float(item) for item in history if item is not None)
    return result


def close_new_low(
    close: Sequence[float | None], low: Sequence[float | None], lookback: Any,
) -> list[bool]:
    window = _positive_window(lookback)
    if len(close) != len(low):
        raise _formula_error("SERIES_LENGTH", "close 与 low 序列长度不同")
    result = [False] * len(close)
    for index in range(window, len(close)):
        history = low[index - window:index]
        if close[index] is not None and all(item is not None for item in history):
            result[index] = float(close[index]) < min(float(item) for item in history if item is not None)
    return result


def _direction_count(
    opens: Sequence[float | None], closes: Sequence[float | None], lookback: Any, *, rising: bool,
) -> list[int | None]:
    window = _positive_window(lookback)
    if len(opens) != len(closes):
        raise _formula_error("SERIES_LENGTH", "open 与 close 序列长度不同")
    result: list[int | None] = [None] * len(opens)
    for index in range(window - 1, len(opens)):
        pairs = list(zip(opens[index - window + 1:index + 1], closes[index - window + 1:index + 1], strict=True))
        if all(open_price is not None and close_price is not None for open_price, close_price in pairs):
            result[index] = sum(
                float(close_price) > float(open_price) if rising else float(close_price) < float(open_price)
                for open_price, close_price in pairs if open_price is not None and close_price is not None
            )
    return result


def up_count(opens: Sequence[float | None], closes: Sequence[float | None], lookback: Any) -> list[int | None]:
    return _direction_count(opens, closes, lookback, rising=True)


def down_count(opens: Sequence[float | None], closes: Sequence[float | None], lookback: Any) -> list[int | None]:
    return _direction_count(opens, closes, lookback, rising=False)


def _count_ratio(numerator: Sequence[int | None], denominator: Sequence[int | None]) -> list[float | None]:
    return [float(left) / float(right) if left is not None and right not in (None, 0) else None
            for left, right in zip(numerator, denominator, strict=True)]


def up_down_ratio(
    opens: Sequence[float | None], closes: Sequence[float | None], lookback: Any,
) -> list[float | None]:
    return _count_ratio(up_count(opens, closes, lookback), down_count(opens, closes, lookback))


def down_up_ratio(
    opens: Sequence[float | None], closes: Sequence[float | None], lookback: Any,
) -> list[float | None]:
    return _count_ratio(down_count(opens, closes, lookback), up_count(opens, closes, lookback))


def _range_ratio(
    highs: Sequence[float | None], lows: Sequence[float | None], lookback: Any, *, inverted: bool,
) -> list[float | None]:
    window = _positive_window(lookback)
    if len(highs) != len(lows):
        raise _formula_error("SERIES_LENGTH", "high 与 low 序列长度不同")
    result: list[float | None] = [None] * len(highs)
    for index in range(window - 1, len(highs)):
        high_items = highs[index - window + 1:index + 1]
        low_items = lows[index - window + 1:index + 1]
        if all(item is not None for item in high_items) and all(item is not None for item in low_items):
            highest = max(float(item) for item in high_items if item is not None)
            lowest = min(float(item) for item in low_items if item is not None)
            numerator, denominator = (lowest, highest) if inverted else (highest, lowest)
            result[index] = numerator / denominator if denominator != 0 else None
    return result


def range_high_low_ratio(
    highs: Sequence[float | None], lows: Sequence[float | None], lookback: Any,
) -> list[float | None]:
    return _range_ratio(highs, lows, lookback, inverted=False)


def range_low_high_ratio(
    highs: Sequence[float | None], lows: Sequence[float | None], lookback: Any,
) -> list[float | None]:
    return _range_ratio(highs, lows, lookback, inverted=True)


def volume_slope(values: Sequence[float | None], first_offset: Any, second_offset: Any) -> list[float | None]:
    first = _positive_window(first_offset)
    second = _positive_window(second_offset)
    needed = max(first, second)
    denominator = abs(first - second) + 1
    result: list[float | None] = [None] * len(values)
    for index in range(needed - 1, len(values)):
        first_value = values[index - first + 1]
        second_value = values[index - second + 1]
        if first_value is not None and second_value is not None:
            result[index] = (float(first_value) - float(second_value)) / denominator
    return result


def cross_sectional_momentum(
    closes: Mapping[str, Sequence[float | None]], lookback: Any, buckets: Any,
) -> dict[str, list[int | None]]:
    if isinstance(buckets, bool) or not isinstance(buckets, (int, float)) or int(buckets) != buckets or buckets < 2:
        raise _formula_error("INVALID_BUCKETS", "buckets 必须是大于等于 2 的整数")
    bucket_count = int(buckets)
    momentums = {name: time_series_momentum(values, lookback) for name, values in closes.items()}
    if len(momentums) < bucket_count:
        raise _formula_error("INSUFFICIENT_UNIVERSE", "截面标的数量不能少于分档数量")
    size = min((len(values) for values in momentums.values()), default=0)
    result = {name: [None] * len(values) for name, values in momentums.items()}
    for index in range(size):
        valid = sorted((float(values[index]), name) for name, values in momentums.items() if values[index] is not None)
        if len(valid) < bucket_count:
            continue
        cursor = 0
        while cursor < len(valid):
            end = cursor + 1
            while end < len(valid) and valid[end][0] == valid[cursor][0]:
                end += 1
            average_rank = ((cursor + 1) + end) / 2
            bucket = min(bucket_count, max(1, math.ceil(average_rank * bucket_count / len(valid))))
            for _value, name in valid[cursor:end]:
                result[name][index] = bucket
            cursor = end
    return result


def _as_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def performance_metrics(
    timestamps: Sequence[str | datetime], nav: Sequence[float | None], risk_free_rate: float = 0.02,
) -> dict[str, MetricValue]:
    pairs = [(_as_time(timestamp), float(value)) for timestamp, value in zip(timestamps, nav, strict=True) if value is not None]
    if len(pairs) < 3:
        reason = "样本不足：至少需要 3 个有效净值点"
        return {"annualReturn": MetricValue(None, reason), "annualVolatility": MetricValue(None, reason),
                "maxDrawdown": MetricValue(None, reason), "sharpe": MetricValue(None, reason), "calmar": MetricValue(None, reason)}
    if any(value <= 0 for _timestamp, value in pairs):
        reason = "净值非正，无法计算复合年化收益"
        return {"annualReturn": MetricValue(None, reason), "annualVolatility": MetricValue(None, reason),
                "maxDrawdown": MetricValue(None, reason), "sharpe": MetricValue(None, reason), "calmar": MetricValue(None, reason)}
    elapsed = (pairs[-1][0] - pairs[0][0]).total_seconds()
    if elapsed <= 0:
        reason = "有效净值时间跨度不足"
        return {"annualReturn": MetricValue(None, reason), "annualVolatility": MetricValue(None, reason),
                "maxDrawdown": MetricValue(None, reason), "sharpe": MetricValue(None, reason), "calmar": MetricValue(None, reason)}
    try:
        annual_return = (pairs[-1][1] / pairs[0][1]) ** (SECONDS_PER_YEAR / elapsed) - 1
    except OverflowError:
        reason = "时间跨度过短导致年化收益超出有效范围"
        return {"annualReturn": MetricValue(None, reason), "annualVolatility": MetricValue(None, reason),
                "maxDrawdown": MetricValue(None, reason), "sharpe": MetricValue(None, reason), "calmar": MetricValue(None, reason)}
    if not math.isfinite(annual_return):
        reason = "年化收益超出有效范围"
        return {"annualReturn": MetricValue(None, reason), "annualVolatility": MetricValue(None, reason),
                "maxDrawdown": MetricValue(None, reason), "sharpe": MetricValue(None, reason), "calmar": MetricValue(None, reason)}
    returns = [pairs[index][1] / pairs[index - 1][1] - 1 for index in range(1, len(pairs))]
    observations_per_year = len(returns) / (elapsed / SECONDS_PER_YEAR)
    volatility = statistics.stdev(returns) * math.sqrt(observations_per_year)
    peak = pairs[0][1]
    maximum_drawdown = 0.0
    for _timestamp, value in pairs:
        peak = max(peak, value)
        maximum_drawdown = max(maximum_drawdown, 1 - value / peak)
    sharpe = MetricValue(None, "年化波动率为零") if volatility == 0 else MetricValue((annual_return - risk_free_rate) / volatility)
    calmar = MetricValue(None, "最大回撤率为零") if maximum_drawdown == 0 else MetricValue(annual_return / maximum_drawdown)
    return {"annualReturn": MetricValue(annual_return), "annualVolatility": MetricValue(volatility),
            "maxDrawdown": MetricValue(maximum_drawdown), "sharpe": sharpe, "calmar": calmar}


def rolling_metric(
    timestamps: Sequence[str | datetime], nav: Sequence[float | None], lookback: Any, metric: str,
    risk_free_rate: float = 0.02,
) -> list[float | None]:
    if isinstance(lookback, bool) or not isinstance(lookback, (int, float)) or int(lookback) != lookback or lookback < 2:
        raise _formula_error("INVALID_LOOKBACK", "绩效指标 lookback 必须是大于等于 2 的整数")
    window = int(lookback)
    result: list[float | None] = [None] * len(nav)
    for index in range(window - 1, len(nav)):
        measured = performance_metrics(timestamps[index - window + 1:index + 1], nav[index - window + 1:index + 1], risk_free_rate)
        result[index] = measured[metric].value
    return result


__all__ = (
    "ALLOWED_FUNCTIONS", "BUILTIN_SERIES", "CHART_INDICATOR_CATALOG", "CONDITION_CATALOG", "STRATEGY_FUNCTION_CATALOG", "FormulaError", "FormulaProgram", "INDICATOR_CATALOG",
    "MetricValue", "bollinger_lower", "bollinger_upper", "cross_sectional_momentum", "evaluate_formula",
    "close_new_high", "close_new_low", "down_count", "down_up_ratio", "gann_falling_rate", "gann_rising_rate",
    "hsar_resistance", "hsar_support", "limit_down_count", "limit_up_count", "moving_average", "no_limit_down",
    "no_limit_up", "parse_formula", "performance_metrics", "period_return", "range_high_low_ratio",
    "range_low_high_ratio", "rolling_metric", "standard_deviation", "time_series_momentum", "up_count",
    "up_down_ratio", "validate_formula_document", "volume_slope",
)
