"""Summary metrics and traceable detail for one event-driven backtest."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

from market_monitor.contracts import ContractValidationError, validate_contract


REPORT_SCHEMA = "strategy-report.schema.json"


class StrategyReportError(ValueError):
    pass


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _streaks(trades: list[Mapping[str, Any]]) -> tuple[int, int]:
    best_win = best_loss = current_win = current_loss = 0
    for trade in trades:
        pnl = float(trade["pnl"])
        if pnl > 0:
            current_win += 1
            current_loss = 0
            best_win = max(best_win, current_win)
        elif pnl < 0:
            current_loss += 1
            current_win = 0
            best_loss = max(best_loss, current_loss)
        else:
            current_win = current_loss = 0
    return best_win, best_loss


def build_strategy_report(backtest: Mapping[str, Any]) -> dict[str, Any]:
    """Compute metrics exclusively from persisted equity, fills and trades."""

    initial = float(backtest["initial_cash"])
    final = float(backtest["final_equity"])
    trades = [dict(item) for item in backtest.get("trades", [])]
    fills = [dict(item) for item in backtest.get("fills", [])]
    equity_points = [dict(item) for item in backtest.get("equity_points", [])]
    markers = [dict(item) for item in backtest.get("markers", [])]
    wins = [float(item["pnl"]) for item in trades if float(item["pnl"]) > 0]
    losses = [float(item["pnl"]) for item in trades if float(item["pnl"]) < 0]
    flat = len(trades) - len(wins) - len(losses)
    unavailable: dict[str, str] = {}

    first = _timestamp(backtest.get("first_bar_at"))
    last = _timestamp(backtest.get("last_bar_at"))
    elapsed_days = (last - first).total_seconds() / 86400 if first and last and last > first else 0.0
    total_return = (final / initial - 1) * 100.0
    if elapsed_days > 0 and final > 0:
        try:
            annualized_return: float | None = ((final / initial) ** (365.25 / elapsed_days) - 1) * 100.0
        except OverflowError:
            annualized_return = None
            unavailable["annualized_return_percent"] = "NUMERIC_OVERFLOW"
        else:
            if not math.isfinite(annualized_return):
                annualized_return = None
                unavailable["annualized_return_percent"] = "NUMERIC_OVERFLOW"
    else:
        annualized_return = None
        unavailable["annualized_return_percent"] = "NON_POSITIVE_PERIOD_OR_EQUITY"

    if trades:
        win_rate: float | None = len(wins) / len(trades) * 100.0
        average_holding: float | None = sum(int(item["holding_bars"]) for item in trades) / len(trades)
    else:
        win_rate = None
        average_holding = None
        unavailable["win_rate_percent"] = "NO_CLOSED_TRADES"
        unavailable["average_holding_bars"] = "NO_CLOSED_TRADES"
    if wins and losses:
        average_win_loss: float | None = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
    else:
        average_win_loss = None
        unavailable["average_win_loss_ratio"] = "NEEDS_WIN_AND_LOSS"
    if losses:
        profit_factor: float | None = sum(wins) / abs(sum(losses))
    else:
        profit_factor = None
        unavailable["profit_factor"] = "NO_LOSING_TRADES"
    maximum_drawdown = max((float(item["drawdown_percent"]) for item in equity_points), default=0.0)
    best_win_streak, best_loss_streak = _streaks(trades)
    total_commission = sum(float(item["commission"]) for item in fills)
    total_slippage = sum(float(item["slippage_cost"]) for item in fills)
    total_gross_pnl = sum(float(item["gross_pnl"]) for item in trades)
    total_net_pnl = final - initial
    final_state = backtest.get("final_state") or {}
    quantity = float(final_state.get("position_quantity") or 0.0)
    open_position = None
    if quantity:
        last_close = float(equity_points[-1]["close"]) if equity_points else 0.0
        entry_price = float(final_state["average_entry_price"])
        multiplier = float(backtest["contract_multiplier"])
        unrealized = (
            (last_close - entry_price) * quantity * multiplier
            if quantity > 0
            else (entry_price - last_close) * abs(quantity) * multiplier
        )
        open_position = {
            "direction": "long" if quantity > 0 else "short",
            "quantity": abs(quantity),
            "entry_time": final_state.get("entry_time"),
            "average_entry_price": entry_price,
            "last_price": last_close,
            "unrealized_pnl_before_exit_cost": unrealized,
            "currency": backtest["currency"],
        }

    metrics = {
        "total_return_percent": total_return,
        "annualized_return_percent": annualized_return,
        "trade_count": len(trades),
        "winning_trade_count": len(wins),
        "losing_trade_count": len(losses),
        "flat_trade_count": flat,
        "win_rate_percent": win_rate,
        "average_win_loss_ratio": average_win_loss,
        "profit_factor": profit_factor,
        "max_drawdown_percent": maximum_drawdown,
        "average_holding_bars": average_holding,
        "max_consecutive_wins": best_win_streak,
        "max_consecutive_losses": best_loss_streak,
        "total_commission": total_commission,
        "total_slippage_cost": total_slippage,
        "gross_closed_pnl": total_gross_pnl,
        "net_equity_change": total_net_pnl,
        "initial_cash": initial,
        "final_equity": final,
    }
    if not all(value is None or not isinstance(value, float) or math.isfinite(value) for value in metrics.values()):
        raise StrategyReportError("report contains a non-finite metric")
    report = {
        "schema_version": 1,
        "report_id": f"report_{backtest['run_id']}",
        "run_id": backtest["run_id"],
        "strategy_id": backtest["strategy_id"],
        "strategy_version": backtest["strategy_version"],
        # Historical pre-R4-T028 records remain reportable, but do not claim
        # a dependency lock they never persisted and cannot reproduce from.
        "definition_hash": backtest.get("definition_hash"),
        "dependency_lock": backtest.get("dependency_lock"),
        "version_fingerprint": backtest["version_fingerprint"],
        "instrument_id": backtest["instrument_id"],
        "data_version": backtest["data_version"],
        "run_mode": "backtest",
        "status": backtest["status"],
        "currency": backtest["currency"],
        "contract_multiplier": backtest["contract_multiplier"],
        "period": {
            "first_bar_at": backtest.get("first_bar_at"),
            "last_bar_at": backtest.get("last_bar_at"),
            "bars_count": backtest["bars_count"],
        },
        "metrics": metrics,
        "metric_unavailable_reasons": unavailable,
        "open_position": open_position,
        "equity_points": equity_points,
        "markers": markers,
        "trades": trades,
    }
    try:
        validate_contract(REPORT_SCHEMA, report)
    except ContractValidationError as error:
        raise StrategyReportError(str(error)) from error
    return report


__all__ = ["StrategyReportError", "build_strategy_report"]
