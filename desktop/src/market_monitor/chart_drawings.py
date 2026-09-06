"""Versioned, non-indicator chart-drawing rules shared by the local API.

Drawing tools deliberately live outside the Strategy Function registry: their
anchors are user intent in chart coordinates, not market-data calculations nor
strategy inputs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


FIBONACCI_RETRACEMENT_VERSION = 1
MAX_FIBONACCI_LEVELS = 16
DEFAULT_FIBONACCI_LEVELS = (
    {"ratio": 0.0, "label": "0.0%"},
    {"ratio": 0.236, "label": "23.6%"},
    {"ratio": 0.382, "label": "38.2%"},
    {"ratio": 0.5, "label": "50.0%"},
    {"ratio": 0.618, "label": "61.8%"},
    {"ratio": 0.786, "label": "78.6%"},
    {"ratio": 1.0, "label": "100.0%"},
)


class ChartDrawingValidationError(ValueError):
    """A stable local drawing-document validation error."""


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def fibonacci_retracement_price(
    anchor_start_price: float,
    anchor_end_price: float,
    ratio: float,
) -> float:
    """Return one retracement price, retaining the user's anchor direction.

    Ratio 0 is the second anchor and ratio 1 is the first anchor. Reversing
    the anchors therefore reverses every level without hiding the user's swing.
    """

    start = _finite_number(anchor_start_price)
    end = _finite_number(anchor_end_price)
    level = _finite_number(ratio)
    if start is None or end is None or level is None:
        raise ChartDrawingValidationError("Fibonacci 锚点价格和比例必须为有限数值")
    return end + (start - end) * level


def validate_fibonacci_retracement(item: Mapping[str, Any]) -> None:
    """Validate only new Fibonacci documents without migrating legacy items."""

    if item.get("version") != FIBONACCI_RETRACEMENT_VERSION:
        raise ChartDrawingValidationError("Fibonacci 画线版本必须为 1")
    points = item.get("points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)) or len(points) != 2:
        raise ChartDrawingValidationError("Fibonacci 必须包含恰好两个锚点")
    for point in points:
        if not isinstance(point, Mapping) or not isinstance(point.get("time"), str) or not point["time"].strip():
            raise ChartDrawingValidationError("Fibonacci 锚点必须包含有效时间")
        if _finite_number(point.get("price")) is None:
            raise ChartDrawingValidationError("Fibonacci 锚点价格必须为有限数值")

    levels = item.get("levels")
    if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes)) or not 2 <= len(levels) <= MAX_FIBONACCI_LEVELS:
        raise ChartDrawingValidationError(f"Fibonacci 比例数量必须为 2 至 {MAX_FIBONACCI_LEVELS}")
    seen: set[float] = set()
    for level in levels:
        if not isinstance(level, Mapping):
            raise ChartDrawingValidationError("Fibonacci 比例必须包含 ratio 与 label")
        ratio = _finite_number(level.get("ratio"))
        label = level.get("label")
        if ratio is None or not -10 <= ratio <= 10:
            raise ChartDrawingValidationError("Fibonacci 比例必须是 -10 至 10 的有限数值")
        if ratio in seen:
            raise ChartDrawingValidationError("Fibonacci 比例不能重复")
        seen.add(ratio)
        if not isinstance(label, str) or not label.strip() or len(label) > 64:
            raise ChartDrawingValidationError("Fibonacci 比例标签必须为 1 至 64 个字符")


__all__ = [
    "ChartDrawingValidationError",
    "DEFAULT_FIBONACCI_LEVELS",
    "FIBONACCI_RETRACEMENT_VERSION",
    "MAX_FIBONACCI_LEVELS",
    "fibonacci_retracement_price",
    "validate_fibonacci_retracement",
]
