"""Python-shaped expressions compiled to a bounded, side-effect-free program.

Never exec/eval user text. Calls resolve exact registered Strategy Functions.
"""
from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from market_monitor.strategy_function_registry import build_builtin_strategy_function_registry
from market_monitor.strategy_functions import VERSIONED_STRATEGY_FUNCTION_IMPLEMENTATIONS, execute_strategy_function

FIELDS = ("open", "high", "low", "close", "volume", "amount", "open_interest", "settlement")
CONTEXT = ("direction", "observed_win_rate", "margin_rate")
OUTPUTS = {
    "attention": {"long", "short", "cancel", "lookback"},
    "position": {"rewardRisk", "winRate", "allocation", "capitalUsage", "leverage"},
    "timing": {"open", "add", "reduce", "close"},
}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.Gt: operator.gt, ast.Lt: operator.lt,
       ast.GtE: operator.ge, ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne}

TEMPLATE = '''# 每个组合策略包含三个部分，fn调用已登记函数的精确版本。
# 示例仅为编辑起点。allocation为建议名义敞口比例；没有保证金率时资金使用率不可用。
def attention():
    change = fn("price.percentage_change", 1, close, 10)
    fast = fn("technical.sma", 1, close, 5)
    slow = fn("technical.sma", 1, close, 14)
    return {"long": change > 0.08 and fast > slow,
            "short": change < -0.08 and fast < slow,
            "cancel": (direction == 1 and fast < slow) or (direction == -1 and fast > slow),
            "lookback": 10}

def position():
    atr = fn("technical.atr", 1, high, low, close, 14)
    allocation = 0.02 / (atr / close + 0.02)
    return {"rewardRisk": (2 * atr) / atr, "winRate": observed_win_rate,
            "allocation": allocation, "capitalUsage": allocation * margin_rate,
            "leverage": allocation}

def timing():
    fast = fn("technical.sma", 1, close, 5)
    up = fn("condition.crossover", 1, close, fast)
    down = fn("condition.crossunder", 1, close, fast)
    return {"open": (direction == 1 and up) or (direction == -1 and down),
            "add": False, "reduce": False,
            "close": (direction == 1 and down) or (direction == -1 and up)}
'''


@dataclass
class Program:
    functions: dict[str, ast.FunctionDef]
    dependencies: list[dict[str, Any]]


@lru_cache(maxsize=64)
def compile_program(source: str) -> Program:
    if not source.strip() or len(source) > 20000:
        raise ValueError("Python策略不能为空，且最多20000字符")
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as error:
        raise ValueError("Python语法错误，请检查缩进和括号") from error
    if sum(1 for _ in ast.walk(tree)) > 2000:
        raise ValueError("策略表达式过于复杂")
    functions: dict[str, ast.FunctionDef] = {}
    registry = build_builtin_strategy_function_registry()
    dependencies: dict[str, dict[str, Any]] = {}

    def expression(node: ast.AST, names: set[str], depth: int = 0) -> None:
        if depth > 40:
            raise ValueError("表达式嵌套过深")
        def descend(item):
            expression(item, names, depth + 1)
        if isinstance(node, ast.Constant):
            if node.value is not None and not isinstance(node.value, (bool, int, float)):
                raise ValueError("表达式只接受数字、布尔值或None")
            if isinstance(node.value, (int, float)) and (abs(node.value) > 1e100 or not math.isfinite(node.value)):
                raise ValueError("数值超出范围")
        elif isinstance(node, ast.Name) and node.id in names:
            return
        elif isinstance(node, ast.BinOp) and type(node.op) in OPS:
            descend(node.left)
            descend(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            descend(node.operand)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            for value in node.values:
                descend(value)
        elif isinstance(node, ast.Compare) and all(type(op) in OPS for op in node.ops):
            descend(node.left)
            for item in node.comparators:
                descend(item)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fn":
            if node.keywords or len(node.args) < 2 or not all(isinstance(arg, ast.Constant) for arg in node.args[:2]):
                raise ValueError("fn必须以固定函数ID和版本开头，不接受关键字参数")
            identifier, version = node.args[0].value, node.args[1].value
            if not isinstance(identifier, str) or type(version) is not int or version < 1:
                raise ValueError("fn函数ID必须为字符串、版本必须为正整数")
            definition = registry.resolve(identifier, version)
            if (identifier, version) not in VERSIONED_STRATEGY_FUNCTION_IMPLEMENTATIONS:
                raise ValueError("该函数没有可运行的精确版本实现")
            if len(node.args) - 2 != len(definition.inputs):
                raise ValueError(f"{identifier}@{version}需要{len(definition.inputs)}个输入")
            dependencies[f"{identifier}@{version}"] = {
                "id": identifier, "version": version, "hash": definition.definition_hash(),
            }
            for argument in node.args[2:]:
                descend(argument)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            # Select named output of e.g. MACD; numeric indexing cannot read future bars.
            descend(node.value)
        else:
            raise ValueError(f"不支持的Python语法：{type(node).__name__}；只允许表达式和已登记函数")

    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name not in OUTPUTS or function.name in functions:
            raise ValueError("只能定义attention、position、timing三个函数各一次")
        args = function.args
        if args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or function.decorator_list or function.returns:
            raise ValueError("策略函数不接受参数、装饰器或返回类型标注")
        if not function.body or not isinstance(function.body[-1], ast.Return):
            raise ValueError(f"{function.name}必须以return字典结束")
        names = set(FIELDS) | set(CONTEXT)
        for statement in function.body[:-1]:
            if (not isinstance(statement, ast.Assign) or len(statement.targets) != 1
                    or not isinstance(statement.targets[0], ast.Name)):
                raise ValueError("函数内部只允许简单变量赋值和最终return")
            name = statement.targets[0].id
            if name in names or name == "fn" or name.startswith("_"):
                raise ValueError(f"变量{name}重复或属于保留名称")
            expression(statement.value, names)
            names.add(name)
        result = function.body[-1].value
        if not isinstance(result, ast.Dict) or any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in result.keys):
            raise ValueError("return必须使用固定字段名的字典")
        keys = [key.value for key in result.keys]
        if len(keys) != len(set(keys)) or set(keys) != OUTPUTS[function.name]:
            raise ValueError(f"{function.name}必须返回：{', '.join(sorted(OUTPUTS[function.name]))}")
        for value in result.values:
            expression(value, names)
        functions[function.name] = function
    if set(functions) != set(OUTPUTS):
        raise ValueError("组合策略必须同时包含关注attention、仓位position、择时timing")
    return Program(functions, list(dependencies.values()))


def finite(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def evaluate_part(program: Program, part: str, bars: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a part only when its lifecycle gate allows it; last value is observable."""
    names = {field: [bar.get(field) for bar in bars] for field in FIELDS}
    names.update(context)
    size = len(bars)

    def mapped(values: list[Any], operation) -> Any:
        if any(isinstance(value, list) for value in values):
            sequences = [value if isinstance(value, list) else [value] * size for value in values]
            if any(len(value) != size for value in sequences):
                raise ValueError("函数结果长度与行情不一致")
            return [operation(*row) for row in zip(*sequences, strict=True)]
        return operation(*values)

    def binary(left, right, op):
        if left is None or right is None:
            return None
        try:
            result = OPS[type(op)](left, right)
            return result if isinstance(result, bool) or finite(result) else None
        except (ZeroDivisionError, OverflowError, TypeError):
            return None

    def boolean(values, conjunction):
        if any(value is not None and type(value) is not bool for value in values):
            raise ValueError("逻辑运算必须使用布尔条件")
        if conjunction:
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.BinOp):
            return mapped([visit(node.left), visit(node.right)], lambda a, b: binary(a, b, node.op))
        if isinstance(node, ast.BoolOp):
            return mapped([visit(item) for item in node.values], lambda *values: boolean(values, isinstance(node.op, ast.And)))
        if isinstance(node, ast.UnaryOp):
            def unary(value):
                if value is None:
                    return None
                if isinstance(node.op, ast.Not):
                    if type(value) is not bool:
                        raise ValueError("not必须用于布尔条件")
                    return not value
                return -value if isinstance(node.op, ast.USub) else value
            return mapped([visit(node.operand)], unary)
        if isinstance(node, ast.Compare):
            operands = [visit(node.left), *[visit(item) for item in node.comparators]]
            comparisons = [mapped([a, b], lambda a, b, op=op: binary(a, b, op))
                           for a, b, op in zip(operands, operands[1:], node.ops)]
            return mapped(comparisons, lambda *values: boolean(values, True))
        if isinstance(node, ast.Subscript):
            value = visit(node.value)
            if not isinstance(value, dict) or node.slice.value not in value:
                raise ValueError("函数不存在该命名输出")
            return value[node.slice.value]
        identifier, version = node.args[0].value, node.args[1].value
        definition = build_builtin_strategy_function_registry().resolve(identifier, version)
        arguments = [visit(arg) for arg in node.args[2:]]
        for value, specification in zip(arguments, definition.inputs, strict=True):
            if specification.value_type == "integer" and (not finite(value) or int(value) != value or not 0 <= value <= 500):
                raise ValueError("窗口和索引参数必须为0至500的整数")
        return execute_strategy_function(identifier, *arguments, version=version)

    function = program.functions[part]
    for statement in function.body[:-1]:
        names[statement.targets[0].id] = visit(statement.value)
    output = {key.value: visit(value) for key, value in zip(function.body[-1].value.keys, function.body[-1].value.values, strict=True)}
    return {key: (value[-1] if value else None) if isinstance(value, list) else value for key, value in output.items()}
