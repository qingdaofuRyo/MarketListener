from __future__ import annotations

import pytest

from market_monitor.chart_drawings import (
    ChartDrawingValidationError,
    DEFAULT_FIBONACCI_LEVELS,
    fibonacci_retracement_price,
    validate_fibonacci_retracement,
)


def _fibonacci(**overrides: object) -> dict[str, object]:
    return {
        "id": "fib-1",
        "type": "fibonacci_retracement",
        "version": 1,
        "points": [
            {"time": "2026-09-01T09:30:00+08:00", "price": 100.0},
            {"time": "2026-09-05T09:30:00+08:00", "price": 200.0},
        ],
        "levels": [dict(level) for level in DEFAULT_FIBONACCI_LEVELS],
        **overrides,
    }


def test_fibonacci_prices_preserve_anchor_direction_and_standard_levels() -> None:
    assert fibonacci_retracement_price(100, 200, 0) == 200
    assert fibonacci_retracement_price(100, 200, 0.618) == pytest.approx(138.2)
    assert fibonacci_retracement_price(100, 200, 1) == 100
    assert fibonacci_retracement_price(200, 100, 0) == 100
    assert fibonacci_retracement_price(200, 100, 0.618) == pytest.approx(161.8)
    assert fibonacci_retracement_price(200, 100, 1) == 200


def test_fibonacci_document_requires_two_finite_anchors_and_bounded_labeled_levels() -> None:
    validate_fibonacci_retracement(_fibonacci())
    for payload in (
        _fibonacci(points=[]),
        _fibonacci(points=[{"time": "", "price": 100}, {"time": "2026-09-05", "price": 200}]),
        _fibonacci(points=[{"time": "2026-09-01", "price": float("nan")}, {"time": "2026-09-05", "price": 200}]),
        _fibonacci(version=2),
        _fibonacci(levels=[{"ratio": 0, "label": "0"}]),
        _fibonacci(levels=[{"ratio": 0, "label": "0"}, {"ratio": 0, "label": "also zero"}]),
        _fibonacci(levels=[{"ratio": 0, "label": "0"}, {"ratio": float("inf"), "label": "∞"}]),
    ):
        with pytest.raises(ChartDrawingValidationError):
            validate_fibonacci_retracement(payload)
