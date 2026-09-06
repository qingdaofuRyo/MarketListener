from __future__ import annotations

import pytest

from market_monitor.market_list_metrics import trailing_trading_returns


def test_returns_use_trading_bars_not_calendar_days() -> None:
    bars = [
        {"trading_day": "2026-09-03", "close": 100.0, "quality_status": "PASS"},
        {"trading_day": "2026-09-04", "close": 102.0, "quality_status": "PASS"},
        {"trading_day": "2026-09-07", "close": 105.0, "quality_status": "PASS"},
        {"trading_day": "2026-09-08", "close": 110.0, "quality_status": "PASS"},
    ]

    assert trailing_trading_returns(bars, (3, 5)) == {3: pytest.approx(10.0), 5: None}


def test_returns_deduplicate_a_trading_day_and_reject_partial_or_zero_base() -> None:
    bars = [
        {"trading_day": "2026-09-01", "close": 0.0, "quality_status": "PASS"},
        {"trading_day": "2026-09-02", "close": 4.0, "quality_status": "PASS"},
        {"trading_day": "2026-09-02", "close": 5.0, "quality_status": "PASS"},
        {"trading_day": "2026-09-03", "close": 6.0, "is_partial": True},
        {"trading_day": "2026-09-03", "close": 10.0, "quality_status": "PASS"},
    ]

    assert trailing_trading_returns(bars, (1, 2, 3)) == {1: 100.0, 2: None, 3: None}
