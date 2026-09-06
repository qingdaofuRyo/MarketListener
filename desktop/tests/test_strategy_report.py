from __future__ import annotations

import pytest

from market_monitor.strategy_report import build_strategy_report


def result() -> dict:
    return {
        "run_id": "bt_0123456789abcdef01234567",
        "strategy_id": "strategy.report_demo",
        "strategy_version": 1,
        "version_fingerprint": "a" * 64,
        "instrument_id": "CN.SSE.STOCK.600000",
        "data_version": "fixture-v1",
        "status": "ready",
        "currency": "CNY",
        "contract_multiplier": 1.0,
        "bars_count": 366,
        "first_bar_at": "2025-01-01T00:00:00+00:00",
        "last_bar_at": "2026-01-01T00:00:00+00:00",
        "initial_cash": 100.0,
        "final_equity": 110.0,
        "equity_points": [
            {"drawdown_percent": 0, "close": 10},
            {"drawdown_percent": 10, "close": 9},
            {"drawdown_percent": 2, "close": 11},
        ],
        "fills": [
            {"commission": 0.75, "slippage_cost": 1.0},
            {"commission": 1.25, "slippage_cost": 2.0},
        ],
        "trades": [
            {"pnl": 20.0, "gross_pnl": 21.0, "holding_bars": 2},
            {"pnl": -10.0, "gross_pnl": -9.0, "holding_bars": 4},
        ],
        "markers": [],
        "final_state": {"position_quantity": 0},
    }


def test_report_metrics_are_recomputable_from_equity_fills_and_trades() -> None:
    report = build_strategy_report(result())

    assert report["definition_hash"] is None
    assert report["dependency_lock"] is None
    metrics = report["metrics"]
    assert report["report_id"] == "report_bt_0123456789abcdef01234567"
    assert metrics["total_return_percent"] == pytest.approx(10)
    assert metrics["annualized_return_percent"] == pytest.approx(10, abs=0.1)
    assert metrics["trade_count"] == 2
    assert metrics["win_rate_percent"] == 50
    assert metrics["average_win_loss_ratio"] == 2
    assert metrics["profit_factor"] == 2
    assert metrics["max_drawdown_percent"] == 10
    assert metrics["average_holding_bars"] == 3
    assert metrics["max_consecutive_wins"] == 1
    assert metrics["max_consecutive_losses"] == 1
    assert metrics["total_commission"] == 2
    assert metrics["total_slippage_cost"] == 3
    assert metrics["final_equity"] == 110


def test_no_trade_and_zero_denominator_metrics_are_null_with_reasons() -> None:
    value = result()
    value.update({"trades": [], "fills": [], "final_equity": 100.0})
    report = build_strategy_report(value)
    metrics = report["metrics"]
    assert metrics["trade_count"] == 0
    assert metrics["win_rate_percent"] is None
    assert metrics["profit_factor"] is None
    assert report["metric_unavailable_reasons"] == {
        "win_rate_percent": "NO_CLOSED_TRADES",
        "average_holding_bars": "NO_CLOSED_TRADES",
        "average_win_loss_ratio": "NEEDS_WIN_AND_LOSS",
        "profit_factor": "NO_LOSING_TRADES",
    }


def test_open_position_reports_unrealized_pnl_without_faking_a_closed_trade() -> None:
    value = result()
    value.update(
        {
            "trades": [],
            "fills": [],
            "final_state": {
                "position_quantity": -2,
                "average_entry_price": 12,
                "entry_time": "2025-12-20T00:00:00+00:00",
            },
        }
    )
    value["equity_points"][-1]["close"] = 10
    report = build_strategy_report(value)
    assert report["metrics"]["trade_count"] == 0
    assert report["open_position"] == {
        "direction": "short",
        "quantity": 2.0,
        "entry_time": "2025-12-20T00:00:00+00:00",
        "average_entry_price": 12.0,
        "last_price": 10.0,
        "unrealized_pnl_before_exit_cost": 4.0,
        "currency": "CNY",
    }


def test_annualized_return_overflow_is_explicitly_unavailable() -> None:
    value = result()
    value.update(
        {
            "first_bar_at": "2025-01-01T00:00:00+00:00",
            "last_bar_at": "2025-01-01T00:00:01+00:00",
            "final_equity": 1e100,
        }
    )

    report = build_strategy_report(value)

    assert report["metrics"]["annualized_return_percent"] is None
    assert report["metric_unavailable_reasons"]["annualized_return_percent"] == "NUMERIC_OVERFLOW"
