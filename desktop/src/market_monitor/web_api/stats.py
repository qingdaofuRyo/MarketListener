"""/api/stats router: Android-compatible local trading ledger statistics.

The ledger is ``data_control/personal/ledger.jsonl`` (header + strategy/trade/cash
lines, same format as the Android import).  Positions use the Android
average-cost rules: BUY adds price*quantity + fees to cost basis, SELL realizes
proceeds - fees - averageCost*quantity and never goes short.  When no ledger
exists the summary reports ``available: false`` with null numeric fields.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.account_analysis import (
    AccountError,
    analyze_account,
    create_account,
    delete_record,
    list_accounts,
    list_records,
    purge_deleted,
    restore_account,
    soft_delete_account,
    update_account,
    upsert_cashflow,
    upsert_fill,
    upsert_snapshot,
    upsert_strategy_use,
)
from market_monitor.strategy_performance import (
    PerformanceError,
    build_strategy_performance,
    delete_strategy_trade,
    load_performance_store,
    set_strategy_capital,
    upsert_strategy_trade,
)
from market_monitor.web_api.common import (
    append_jsonl,
    bars_by_instrument,
    clean,
    load_inventory,
    load_jsonl,
    now_iso,
    paginate,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])

# 生产环境默认数据根目录；测试或其他宿主可通过 ``app.state.data_root`` 覆盖。
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_DATA_ROOT = _REPO_ROOT / "data_control"

MAX_IMPORT_LINES = 10_000
_CASH_KINDS = frozenset({"DEPOSIT", "WITHDRAWAL", "DIVIDEND", "TAX_REFUND", "OTHER"})
_MILLIS_PER_DAY = 86_400_000


def _account_error(error: AccountError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


def _data_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_root", None)
    if configured:
        return Path(configured)
    return _DEFAULT_DATA_ROOT


class AccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    startDate: str
    initialEquity: float = Field(gt=0)
    riskFreeRate: float = Field(default=0.02, gt=-1, lt=1)
    benchmarkInstrumentId: str | None = Field(default=None, max_length=256)


class AccountUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    startDate: str | None = None
    initialEquity: float | None = Field(default=None, gt=0)
    riskFreeRate: float | None = Field(default=None, gt=-1, lt=1)
    benchmarkInstrumentId: str | None = Field(default=None, max_length=256)


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=64)
    day: str
    equity: float = Field(gt=0)
    cash: float | None = None
    marketValue: float | None = None
    marginUsed: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=1000)


class CashflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=64)
    occurredAt: str
    kind: Literal["DEPOSIT", "WITHDRAWAL"]
    amount: float = Field(gt=0)
    note: str | None = Field(default=None, max_length=1000)


class FillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=64)
    instrumentId: str = Field(min_length=1, max_length=256)
    instrumentName: str | None = Field(default=None, max_length=160)
    direction: Literal["LONG", "SHORT"]
    positionEffect: Literal["OPEN", "CLOSE"]
    occurredAt: str
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    contractMultiplier: float = Field(default=1, gt=0)
    fee: float = Field(default=0, ge=0)
    strategyId: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class StrategyUseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=64)
    strategyId: str = Field(min_length=1, max_length=128)
    strategyName: str | None = Field(default=None, max_length=160)
    startDate: str
    endDate: str | None = None


class AccountCsvImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["snapshots", "cashflows", "fills", "strategyUses"]
    csvText: str = Field(min_length=1, max_length=2_000_000)


@router.get("/accounts")
def accounts(request: Request) -> dict[str, Any]:
    data_root = _data_root(request)
    purge_deleted(data_root)
    return clean({"items": list_accounts(data_root), "total": len(list_accounts(data_root))})


@router.get("/accounts/trash")
def account_trash(request: Request) -> dict[str, Any]:
    rows = [item for item in list_accounts(_data_root(request), include_deleted=True) if item.get("deletedAt")]
    return clean({"items": rows, "total": len(rows)})


@router.post("/accounts", status_code=201)
def account_create(request: Request, body: AccountCreateRequest) -> dict[str, Any]:
    try:
        return create_account(_data_root(request), body.model_dump())
    except AccountError as error:
        raise _account_error(error) from error


@router.get("/accounts/{account_id}")
def account_get(request: Request, account_id: str) -> dict[str, Any]:
    try:
        from market_monitor.account_analysis import get_account
        return get_account(_data_root(request), account_id)
    except AccountError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/accounts/{account_id}")
def account_update(request: Request, account_id: str, body: AccountUpdateRequest) -> dict[str, Any]:
    try:
        return update_account(_data_root(request), account_id, body.model_dump(exclude_unset=True))
    except AccountError as error:
        raise _account_error(error) from error


@router.delete("/accounts/{account_id}")
def account_delete(request: Request, account_id: str) -> dict[str, Any]:
    try:
        soft_delete_account(_data_root(request), account_id)
    except AccountError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"deleted": True, "accountId": account_id, "recoverableDays": 30}


@router.post("/accounts/{account_id}/restore")
def account_restore(request: Request, account_id: str) -> dict[str, Any]:
    try:
        return restore_account(_data_root(request), account_id)
    except AccountError as error:
        raise _account_error(error) from error


@router.get("/accounts/{account_id}/analysis")
def account_analysis(
    request: Request, account_id: str, start: str | None = None, end: str | None = None,
) -> dict[str, Any]:
    try:
        return analyze_account(_data_root(request), account_id, start=start, end=end)
    except AccountError as error:
        raise _account_error(error) from error


_ACCOUNT_RECORDS: dict[str, tuple[str, Any]] = {
    "snapshots": ("account_snapshots", upsert_snapshot),
    "cashflows": ("account_cashflows", upsert_cashflow),
    "fills": ("account_fills", upsert_fill),
    "strategy-uses": ("account_strategy_uses", upsert_strategy_use),
}


@router.get("/accounts/{account_id}/{kind}")
def account_records(request: Request, account_id: str, kind: str) -> dict[str, Any]:
    selected = _ACCOUNT_RECORDS.get(kind)
    if selected is None:
        raise HTTPException(status_code=404, detail="未知账户记录类型")
    try:
        rows = list_records(_data_root(request), account_id, selected[0])
        return clean({"items": rows, "total": len(rows)})
    except AccountError as error:
        raise _account_error(error) from error


def _save_account_record(request: Request, account_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    selected = _ACCOUNT_RECORDS.get(kind)
    if selected is None:
        raise HTTPException(status_code=404, detail="未知账户记录类型")
    try:
        return selected[1](_data_root(request), account_id, payload)
    except AccountError as error:
        raise _account_error(error) from error


@router.post("/accounts/{account_id}/snapshots")
def account_snapshot_save(request: Request, account_id: str, body: SnapshotRequest) -> dict[str, Any]:
    return _save_account_record(request, account_id, "snapshots", body.model_dump())


@router.post("/accounts/{account_id}/cashflows")
def account_cashflow_save(request: Request, account_id: str, body: CashflowRequest) -> dict[str, Any]:
    return _save_account_record(request, account_id, "cashflows", body.model_dump())


@router.post("/accounts/{account_id}/fills")
def account_fill_save(request: Request, account_id: str, body: FillRequest) -> dict[str, Any]:
    return _save_account_record(request, account_id, "fills", body.model_dump())


@router.post("/accounts/{account_id}/strategy-uses")
def account_strategy_use_save(request: Request, account_id: str, body: StrategyUseRequest) -> dict[str, Any]:
    return _save_account_record(request, account_id, "strategy-uses", body.model_dump())


@router.delete("/accounts/{account_id}/{kind}/{record_id}")
def account_record_delete(request: Request, account_id: str, kind: str, record_id: str) -> dict[str, Any]:
    selected = _ACCOUNT_RECORDS.get(kind)
    if selected is None:
        raise HTTPException(status_code=404, detail="未知账户记录类型")
    try:
        delete_record(_data_root(request), account_id, selected[0], record_id)
    except AccountError as error:
        raise _account_error(error) from error
    return {"deleted": True, "id": record_id}


@router.post("/accounts/{account_id}/csv-import")
def account_csv_import(request: Request, account_id: str, body: AccountCsvImportRequest) -> dict[str, Any]:
    """Import one account record kind from a UTF-8 CSV header row atomically per row."""
    kind_map = {"snapshots": "snapshots", "cashflows": "cashflows", "fills": "fills", "strategyUses": "strategy-uses"}
    kind = kind_map[body.kind]
    reader = csv.DictReader(io.StringIO(body.csvText))
    imported = 0
    errors: list[dict[str, Any]] = []
    for line, row in enumerate(reader, start=2):
        if line > 10_001:
            raise HTTPException(status_code=422, detail="单次 CSV 最多导入 10000 行")
        try:
            # CSV leaves everything as text.  Let Pydantic coerce numeric values
            # and surface the same validation messages as the manual form.
            if kind == "snapshots":
                payload = SnapshotRequest.model_validate(row).model_dump()
            elif kind == "cashflows":
                payload = CashflowRequest.model_validate(row).model_dump()
            elif kind == "fills":
                payload = FillRequest.model_validate(row).model_dump()
            else:
                payload = StrategyUseRequest.model_validate(row).model_dump()
            _save_account_record(request, account_id, kind, payload)
            imported += 1
        except (AccountError, ValueError) as error:
            errors.append({"line": line, "message": str(error)})
    return clean({"imported": imported, "failed": len(errors), "errors": errors[:100]})


def _ledger_path(data_root: Path) -> Path:
    return data_root / "personal" / "ledger.jsonl"


def _epoch_millis(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _epoch_day(epoch_millis: int) -> int:
    return math.floor(epoch_millis / _MILLIS_PER_DAY)


def _day_iso(epoch_day: int) -> str:
    return datetime.fromtimestamp(epoch_day * 86400, tz=timezone.utc).date().isoformat()


def _valid_trade(row: dict[str, Any], line_index: int) -> dict[str, Any] | None:
    instrument_id = str(row.get("instrument_id") or "").strip()
    side = str(row.get("side") or "").upper()
    quantity = row.get("quantity")
    price = row.get("price")
    executed_at = _epoch_millis(row.get("executed_at"))
    if not instrument_id or side not in {"BUY", "SELL"} or executed_at is None:
        return None
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
        return None
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        return None
    fees = 0.0
    raw_fees = row.get("fees")
    if raw_fees is not None:
        if not isinstance(raw_fees, list):
            return None
        for fee in raw_fees:
            if not isinstance(fee, dict):
                return None
            amount = fee.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
                return None
            fees += float(amount)
    strategy_id = row.get("strategy_id")
    return {
        "instrument_id": instrument_id,
        "side": side,
        "quantity": int(quantity),
        "price": float(price),
        "executed_at": executed_at,
        "executed_at_text": str(row.get("executed_at") or ""),
        "fees": fees,
        "fees_raw": raw_fees if raw_fees is not None else [],
        "strategy_id": str(strategy_id) if strategy_id else None,
        "order_group_id": str(row.get("order_group_id")) if row.get("order_group_id") else None,
        "note": str(row.get("note")) if row.get("note") else None,
        "line_index": line_index,
    }


def _valid_cash(row: dict[str, Any], line_index: int) -> dict[str, Any] | None:
    kind = str(row.get("kind") or "").upper()
    amount = row.get("amount")
    occurred_at = _epoch_millis(row.get("occurred_at") or row.get("executed_at"))
    if kind not in _CASH_KINDS or occurred_at is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount == 0:
        return None
    return {
        "kind": kind,
        "amount": float(amount),
        "occurred_at": occurred_at,
        "occurred_at_text": str(row.get("occurred_at") or ""),
        "note": str(row.get("note")) if row.get("note") else None,
        "line_index": line_index,
    }


def _ledger_events(data_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    cash_events: list[dict[str, Any]] = []
    for line_index, row in enumerate(load_jsonl(_ledger_path(data_root))):
        row_type = str(row.get("type") or "")
        if row_type == "trade":
            trade = _valid_trade(row, line_index)
            if trade is not None:
                trades.append(trade)
        elif row_type == "cash":
            cash = _valid_cash(row, line_index)
            if cash is not None:
                cash_events.append(cash)
    trades.sort(key=lambda trade: (trade["executed_at"], trade["line_index"]))
    cash_events.sort(key=lambda cash: (cash["occurred_at"], cash["line_index"]))
    return trades, cash_events


def _apply_trade(
    positions: dict[str, dict[str, Any]],
    trade: dict[str, Any],
) -> float | None:
    position = positions.setdefault(trade["instrument_id"], {"quantity": 0, "cost_basis": 0.0})
    if trade["side"] == "BUY":
        position["quantity"] += trade["quantity"]
        position["cost_basis"] += trade["price"] * trade["quantity"] + trade["fees"]
        return None
    if trade["quantity"] > position["quantity"]:
        return None  # 非法卖单：跳过，避免出现负持仓
    average_cost = position["cost_basis"] / position["quantity"] if position["quantity"] else 0.0
    pnl = trade["price"] * trade["quantity"] - trade["fees"] - average_cost * trade["quantity"]
    position["quantity"] -= trade["quantity"]
    position["cost_basis"] = (
        0.0 if position["quantity"] == 0 else position["cost_basis"] - average_cost * trade["quantity"]
    )
    return pnl


def _compute_state(
    trades: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[dict[str, Any], float]], float]:
    positions: dict[str, dict[str, Any]] = {}
    closed: list[tuple[dict[str, Any], float]] = []
    fees_total = 0.0
    for trade in trades:
        fees_total += trade["fees"]
        pnl = _apply_trade(positions, trade)
        if pnl is not None:
            closed.append((trade, pnl))
    return positions, closed, fees_total


def _daily_closes(data_root: Path, instrument_ids: set[str]) -> dict[str, dict[str, float]]:
    if not instrument_ids:
        return {}
    all_bars = bars_by_instrument(
        data_root,
        period="1d",
        limit_per_instrument=2000,
        max_instruments=500,
    )
    result: dict[str, dict[str, float]] = {}
    for instrument_id in instrument_ids:
        closes: dict[str, float] = {}
        for bar in all_bars.get(instrument_id, []):
            day = str(bar.get("trading_date") or str(bar.get("bar_open_time") or "")[:10])
            close = bar.get("close")
            if day and isinstance(close, (int, float)) and not isinstance(close, bool):
                closes[day] = float(close)
        result[instrument_id] = closes
    return result


def _nav_curve(
    trades: list[dict[str, Any]],
    cash_events: list[dict[str, Any]],
    closes: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    """Build the NAV curve with the same semantics as the Android
    ``TradingStatsCalculator``: one snapshot per event day, plus every day that
    has a close price; missing closes fall back to average cost and are flagged.
    """
    trade_by_day: dict[int, list[dict[str, Any]]] = {}
    cash_by_day: dict[int, list[dict[str, Any]]] = {}
    for trade in trades:
        trade_by_day.setdefault(_epoch_day(trade["executed_at"]), []).append(trade)
    for cash in cash_events:
        cash_by_day.setdefault(_epoch_day(cash["occurred_at"]), []).append(cash)
    days: set[int] = set(trade_by_day) | set(cash_by_day)
    for close_days in closes.values():
        for day_text in close_days:
            try:
                parsed = datetime.strptime(day_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            days.add(_epoch_day(int(parsed.timestamp() * 1000)))

    positions: dict[str, dict[str, Any]] = {}
    cash_total = 0.0
    snapshots: list[dict[str, Any]] = []
    for day in sorted(days):
        for cash in sorted(cash_by_day.get(day, []), key=lambda item: (item["occurred_at"], item["line_index"])):
            cash_total += cash["amount"]
        for trade in sorted(trade_by_day.get(day, []), key=lambda item: (item["executed_at"], item["line_index"])):
            position = positions.get(trade["instrument_id"])
            if trade["side"] == "SELL" and (position is None or trade["quantity"] > position["quantity"]):
                continue
            _apply_trade(positions, trade)
            if trade["side"] == "BUY":
                cash_total -= trade["price"] * trade["quantity"] + trade["fees"]
            else:
                cash_total += trade["price"] * trade["quantity"] - trade["fees"]
        snapshots.append(
            {
                "day": day,
                "cash": cash_total,
                "positions": {key: dict(value) for key, value in positions.items()},
            }
        )

    last_close: dict[str, float] = {}
    points: list[dict[str, Any]] = []
    snapshot_index = 0
    for day in sorted(days):
        while snapshot_index < len(snapshots) and snapshots[snapshot_index]["day"] <= day:
            snapshot_index += 1
        if snapshot_index == 0:
            cash = 0.0
            snapshot_positions: dict[str, dict[str, Any]] = {}
        else:
            snapshot = snapshots[snapshot_index - 1]
            cash = float(snapshot["cash"])
            snapshot_positions = snapshot["positions"]
        day_text = _day_iso(day)
        for instrument_id, close_days in closes.items():
            if day_text in close_days:
                last_close[instrument_id] = close_days[day_text]
        position_value = 0.0
        marked_with_fallback = False
        for instrument_id, position in snapshot_positions.items():
            close = last_close.get(instrument_id)
            if close is not None:
                position_value += close * position["quantity"]
            else:
                marked_with_fallback = True
                position_value += position["cost_basis"]
        nav = cash + position_value
        points.append(
            {
                "t": day_text,
                "nav": round(nav, 6),
                "cash": round(cash, 6),
                "positionValue": round(position_value, 6),
                "exposurePct": round(position_value / nav * 100.0, 6) if nav != 0.0 else 0.0,
                "markedWithFallback": marked_with_fallback,
            }
        )
    return points


def _summary_payload(data_root: Path) -> dict[str, Any]:
    trades, cash_events = _ledger_events(data_root)
    if not trades and not cash_events:
        return {
            "available": False,
            "navCurve": [],
            "totalReturnPct": None,
            "maxDrawdownPct": None,
            "winRatePct": None,
            "profitFactor": None,
            "grossProfit": None,
            "grossLoss": None,
            "feesTotal": None,
            "realizedTotal": None,
            "averageExposurePct": None,
            "maxExposurePct": None,
            "realizedByStrategy": {},
            "realizedByInstrument": {},
            "unrealizedByStrategy": {},
            "unrealizedByInstrument": {},
            "unrealizedTotal": None,
            "generatedAt": now_iso(),
        }

    positions, closed, fees_total = _compute_state(trades)
    closes = _daily_closes(data_root, {trade["instrument_id"] for trade in trades})
    curve = _nav_curve(trades, cash_events, closes)

    total_return = 0.0
    if curve and curve[0]["nav"] != 0.0:
        total_return = (curve[-1]["nav"] - curve[0]["nav"]) / curve[0]["nav"] * 100.0
    peak = float("-inf")
    max_drawdown = 0.0
    for point in curve:
        nav = float(point["nav"])
        peak = max(peak, nav)
        if peak > 0.0:
            max_drawdown = max(max_drawdown, (peak - nav) / peak * 100.0)

    pnls = [pnl for _trade, pnl in closed]
    wins = sum(1 for pnl in pnls if pnl > 0.0)
    win_rate = (wins / len(pnls) * 100.0) if pnls else 0.0
    gross_profit = sum(pnl for pnl in pnls if pnl > 0.0)
    gross_loss = sum(-pnl for pnl in pnls if pnl < 0.0)
    profit_factor = gross_profit / gross_loss if gross_loss != 0.0 else None
    realized_total = sum(pnls)

    exposure_points = [float(point["exposurePct"]) for point in curve if float(point["nav"]) != 0.0]
    average_exposure = sum(exposure_points) / len(exposure_points) if exposure_points else 0.0
    max_exposure = max(exposure_points) if exposure_points else 0.0

    realized_by_strategy: dict[str, float] = {}
    realized_by_instrument: dict[str, float] = {}
    for trade, pnl in closed:
        strategy = trade["strategy_id"] or "UNASSIGNED"
        realized_by_strategy[strategy] = realized_by_strategy.get(strategy, 0.0) + pnl
        realized_by_instrument[trade["instrument_id"]] = (
            realized_by_instrument.get(trade["instrument_id"], 0.0) + pnl
        )

    latest_strategy: dict[str, str] = {}
    for trade in reversed(trades):
        latest_strategy.setdefault(trade["instrument_id"], trade["strategy_id"] or "UNASSIGNED")
    unrealized_by_strategy: dict[str, float] = {}
    unrealized_by_instrument: dict[str, float] = {}
    unrealized_total = 0.0
    inventory = load_inventory(data_root)
    for instrument_id, position in positions.items():
        if position["quantity"] <= 0:
            continue
        last_close = inventory.instruments.get(instrument_id, {}).get("lastClose")
        if isinstance(last_close, (int, float)) and not isinstance(last_close, bool):
            market_value = float(last_close) * position["quantity"]
        else:
            market_value = position["cost_basis"]
        unrealized = market_value - position["cost_basis"]
        unrealized_total += unrealized
        strategy = latest_strategy.get(instrument_id, "UNASSIGNED")
        unrealized_by_strategy[strategy] = unrealized_by_strategy.get(strategy, 0.0) + unrealized
        unrealized_by_instrument[instrument_id] = unrealized_by_instrument.get(instrument_id, 0.0) + unrealized

    return {
        "available": True,
        "navCurve": curve,
        "totalReturnPct": round(total_return, 6),
        "maxDrawdownPct": round(max_drawdown, 6),
        "winRatePct": round(win_rate, 6),
        "profitFactor": round(profit_factor, 6) if profit_factor is not None else None,
        "grossProfit": round(gross_profit, 6),
        "grossLoss": round(gross_loss, 6),
        "feesTotal": round(fees_total, 6),
        "realizedTotal": round(realized_total, 6),
        "averageExposurePct": round(average_exposure, 6),
        "maxExposurePct": round(max_exposure, 6),
        "realizedByStrategy": realized_by_strategy,
        "realizedByInstrument": realized_by_instrument,
        "unrealizedByStrategy": unrealized_by_strategy,
        "unrealizedByInstrument": unrealized_by_instrument,
        "unrealizedTotal": round(unrealized_total, 6),
        "generatedAt": now_iso(),
    }


@router.get("/summary")
def stats_summary(request: Request) -> dict[str, Any]:
    return clean(_summary_payload(_data_root(request)))


class StrategyCapitalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    initialCapital: float = Field(gt=0)


class StrategyTradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyId: str = Field(min_length=1, max_length=128)
    instrumentId: str = Field(min_length=1, max_length=256)
    direction: Literal["LONG", "SHORT"]
    entryAt: datetime
    entryPrice: float = Field(gt=0)
    exitAt: datetime | None = None
    exitPrice: float | None = Field(default=None, gt=0)
    quantity: float = Field(gt=0)
    contractMultiplier: float = Field(default=1, gt=0)
    entryFees: float = Field(default=0, ge=0)
    exitFees: float = Field(default=0, ge=0)
    note: str | None = Field(default=None, max_length=1000)

    def as_record(self) -> dict[str, Any]:
        if (self.exitAt is None) != (self.exitPrice is None):
            raise ValueError("平仓时间和价格必须同时填写")
        entry_at = self.entryAt if self.entryAt.tzinfo else self.entryAt.replace(tzinfo=timezone.utc)
        exit_at = self.exitAt if self.exitAt is None or self.exitAt.tzinfo else self.exitAt.replace(tzinfo=timezone.utc)
        if exit_at is not None and exit_at < entry_at:
            raise ValueError("平仓时间不能早于开仓时间")
        return {
            "strategyId": self.strategyId, "instrumentId": self.instrumentId, "direction": self.direction,
            "entryAt": entry_at.isoformat(), "entryPrice": self.entryPrice,
            "exitAt": exit_at.isoformat() if exit_at else None, "exitPrice": self.exitPrice,
            "quantity": self.quantity, "contractMultiplier": self.contractMultiplier,
            "entryFees": self.entryFees, "exitFees": self.exitFees, "note": self.note,
        }


class StrategyPerformanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyId: str = Field(min_length=1, max_length=128)
    period: str = Field(min_length=1, max_length=16)
    lookback: int | None = Field(default=None, ge=3, le=100_000)
    riskFreeRate: float = Field(default=0.02, ge=-1, le=1)


@router.get("/strategy-ledger")
def stats_strategy_ledger(request: Request) -> dict[str, Any]:
    data_root = _data_root(request)
    store = load_performance_store(data_root)
    legacy, _cash = _ledger_events(data_root)
    legacy_items = [
        {
            "id": f"legacy:{trade['line_index']}", "strategyId": trade["strategy_id"],
            "instrumentId": trade["instrument_id"], "direction": "LONG", "side": trade["side"],
            "executedAt": trade["executed_at_text"], "price": trade["price"], "quantity": trade["quantity"],
            "contractMultiplier": 1, "fees": trade["fees"], "editable": False, "source": "legacy",
        }
        for trade in legacy if trade["strategy_id"]
    ]
    return clean({"capitalByStrategy": store["capitalByStrategy"], "items": store["trades"],
                  "legacyItems": legacy_items, "total": len(store["trades"]) + len(legacy_items)})


@router.put("/strategy-capital/{strategy_id}")
def stats_strategy_capital(request: Request, strategy_id: str, body: StrategyCapitalRequest) -> dict[str, Any]:
    try:
        store = set_strategy_capital(_data_root(request), strategy_id, body.initialCapital)
    except PerformanceError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return clean({"strategyId": strategy_id, "initialCapital": store["capitalByStrategy"][strategy_id]})


def _trade_record(body: StrategyTradeRequest) -> dict[str, Any]:
    try:
        return body.as_record()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/strategy-trades", status_code=201)
def stats_strategy_trade_create(request: Request, body: StrategyTradeRequest) -> dict[str, Any]:
    return clean(upsert_strategy_trade(_data_root(request), _trade_record(body)))


@router.put("/strategy-trades/{trade_id}")
def stats_strategy_trade_update(request: Request, trade_id: str, body: StrategyTradeRequest) -> dict[str, Any]:
    try:
        return clean(upsert_strategy_trade(_data_root(request), _trade_record(body), trade_id))
    except PerformanceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/strategy-trades/{trade_id}")
def stats_strategy_trade_delete(request: Request, trade_id: str) -> dict[str, Any]:
    try:
        delete_strategy_trade(_data_root(request), trade_id)
    except PerformanceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return clean({"deleted": True, "id": trade_id})


@router.post("/strategy-performance")
def stats_strategy_performance(request: Request, body: StrategyPerformanceRequest) -> dict[str, Any]:
    try:
        result = build_strategy_performance(
            _data_root(request), body.strategyId, body.period,
            lookback=body.lookback, risk_free_rate=body.riskFreeRate,
        )
    except (PerformanceError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return clean(result)


@router.get("/trades")
def stats_trades(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500, alias="pageSize"),
) -> dict[str, Any]:
    trades, _cash = _ledger_events(_data_root(request))
    items = [
        {
            "instrumentId": trade["instrument_id"],
            "side": trade["side"],
            "quantity": trade["quantity"],
            "price": trade["price"],
            "executedAt": trade["executed_at_text"],
            "fees": [
                {"kind": fee.get("kind"), "amount": fee.get("amount")}
                for fee in (trade.get("fees_raw") or [])
            ],
            "strategyId": trade["strategy_id"],
            "orderGroupId": trade["order_group_id"],
            "note": trade["note"],
        }
        for trade in reversed(trades)
    ]
    return clean(paginate(items, page, page_size))


@router.get("/positions")
def stats_positions(request: Request) -> dict[str, Any]:
    data_root = _data_root(request)
    trades, _cash = _ledger_events(data_root)
    positions, _closed, _fees = _compute_state(trades)
    inventory = load_inventory(data_root)
    items: list[dict[str, Any]] = []
    for instrument_id, position in sorted(positions.items()):
        if position["quantity"] <= 0:
            continue
        average_cost = position["cost_basis"] / position["quantity"]
        info = inventory.instruments.get(instrument_id, {})
        last_close = info.get("lastClose")
        if isinstance(last_close, (int, float)) and not isinstance(last_close, bool):
            market_value = float(last_close) * position["quantity"]
        else:
            market_value = position["cost_basis"]
        items.append(
            {
                "instrumentId": instrument_id,
                "quantity": position["quantity"],
                "averageCost": round(average_cost, 6),
                "marketValue": round(market_value, 6),
                "unrealizedPnl": round(market_value - position["cost_basis"], 6),
                "updatedAt": str(info.get("updatedAt") or now_iso()),
            }
        )
    return clean({"items": items, "total": len(items)})


class StatsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_IMPORT_LINES)


def _valid_import_line(line: dict[str, Any]) -> bool:
    row_type = str(line.get("type") or "")
    if row_type == "trade":
        return _valid_trade(line, 0) is not None
    if row_type == "cash":
        return _valid_cash(line, 0) is not None
    if row_type == "strategy":
        return bool(str(line.get("id") or "").strip()) and bool(str(line.get("name") or "").strip())
    return False


@router.post("/import")
def stats_import(request: Request, body: StatsImportRequest) -> dict[str, Any]:
    """Append validated ledger lines; invalid rows are skipped and counted."""
    data_root = _data_root(request)
    imported = 0
    skipped = 0
    ledger_path = _ledger_path(data_root)
    if not ledger_path.is_file() or ledger_path.stat().st_size == 0:
        append_jsonl(ledger_path, {"type": "header", "source_label": "desktop-import"})
    for line in body.lines:
        if not isinstance(line, dict) or not _valid_import_line(line):
            skipped += 1
            continue
        normalized = dict(line)
        if str(normalized.get("type")) == "trade":
            normalized["side"] = str(normalized["side"]).upper()
        elif str(normalized.get("type")) == "cash":
            normalized["kind"] = str(normalized["kind"]).upper()
        append_jsonl(ledger_path, clean(normalized))
        imported += 1
    return clean({"imported": imported, "skipped": skipped, "total": imported + skipped})


@router.get("/export")
def stats_export(request: Request) -> PlainTextResponse:
    path = _ledger_path(_data_root(request))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no ledger available")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


__all__ = ("router",)
