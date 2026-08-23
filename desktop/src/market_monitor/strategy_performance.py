"""Editable desktop strategy ledger and local mark-to-market performance."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from market_monitor.formula_engine import MetricValue, performance_metrics
from market_monitor.web_api.common import MAX_BARS, load_json, load_jsonl, read_bars
from market_monitor.web_api.market import _derived_bars, _logical_instruments

STORE_VERSION = 1


class PerformanceError(ValueError):
    pass


def _store_path(data_root: Path) -> Path:
    return data_root / "personal" / "strategy-performance.json"


def load_performance_store(data_root: Path) -> dict[str, Any]:
    payload = load_json(_store_path(data_root), default={})
    if not isinstance(payload, dict):
        payload = {}
    capitals = payload.get("capitalByStrategy")
    trades = payload.get("trades")
    return {
        "schemaVersion": STORE_VERSION,
        "capitalByStrategy": dict(capitals) if isinstance(capitals, dict) else {},
        "trades": [dict(item) for item in trades if isinstance(item, dict)] if isinstance(trades, list) else [],
    }


def save_performance_store(data_root: Path, store: Mapping[str, Any]) -> None:
    path = _store_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(store), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def set_strategy_capital(data_root: Path, strategy_id: str, capital: float) -> dict[str, Any]:
    if not strategy_id.strip() or not capital > 0:
        raise PerformanceError("策略和独立初始本金必须有效，且本金必须大于零")
    store = load_performance_store(data_root)
    store["capitalByStrategy"][strategy_id] = float(capital)
    save_performance_store(data_root, store)
    return store


def upsert_strategy_trade(data_root: Path, trade: Mapping[str, Any], trade_id: str | None = None) -> dict[str, Any]:
    store = load_performance_store(data_root)
    normalized = dict(trade)
    normalized["id"] = trade_id or str(normalized.get("id") or f"trade_{uuid.uuid4().hex[:16]}")
    normalized["source"] = "desktop"
    found = False
    for index, item in enumerate(store["trades"]):
        if item.get("id") == normalized["id"]:
            store["trades"][index] = normalized
            found = True
            break
    if trade_id and not found:
        raise PerformanceError("交易记录不存在")
    if not found:
        store["trades"].append(normalized)
    save_performance_store(data_root, store)
    return normalized


def delete_strategy_trade(data_root: Path, trade_id: str) -> None:
    store = load_performance_store(data_root)
    filtered = [item for item in store["trades"] if item.get("id") != trade_id]
    if len(filtered) == len(store["trades"]):
        raise PerformanceError("交易记录不存在")
    store["trades"] = filtered
    save_performance_store(data_root, store)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fees(row: Mapping[str, Any]) -> float:
    raw = row.get("fees")
    if not isinstance(raw, list):
        return 0.0
    return sum(float(item.get("amount") or 0) for item in raw if isinstance(item, dict))


def _legacy_trades(data_root: Path, strategy_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, row in enumerate(load_jsonl(data_root / "personal" / "ledger.jsonl")):
        if row.get("type") != "trade" or str(row.get("strategy_id") or "") != strategy_id:
            continue
        try:
            quantity = float(row["quantity"])
            price = float(row["price"])
            timestamp = _timestamp(row["executed_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        side = str(row.get("side") or "").upper()
        instrument_id = str(row.get("instrument_id") or "").strip()
        if side not in {"BUY", "SELL"} or not instrument_id or quantity <= 0 or price <= 0:
            continue
        result.append({"id": f"legacy:{index}", "instrumentId": instrument_id, "side": side,
                       "quantity": quantity, "price": price, "at": timestamp, "fees": _fees(row), "source": "legacy"})
    return sorted(result, key=lambda item: (item["at"], item["id"]))


def _bars_for(data_root: Path, instrument_id: str, period: str) -> list[dict[str, Any]]:
    logical = _logical_instruments(data_root).get(instrument_id, {})
    storage_id = str(logical.get("storageInstrumentId") or instrument_id)
    bars = read_bars(data_root, storage_id, period=period, limit=MAX_BARS)
    return bars or _derived_bars(data_root, storage_id, period)


def _close_series(data_root: Path, instruments: set[str], period: str) -> dict[str, list[tuple[datetime, float]]]:
    result: dict[str, list[tuple[datetime, float]]] = {}
    for instrument_id in instruments:
        points: list[tuple[datetime, float]] = []
        for bar in _bars_for(data_root, instrument_id, period):
            try:
                close = float(bar["close"])
                timestamp = _timestamp(bar.get("bar_close_time") or bar["bar_open_time"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if close > 0:
                points.append((timestamp, close))
        result[instrument_id] = sorted(set(points))
    return result


def _last_close(points: Sequence[tuple[datetime, float]], timestamp: datetime) -> tuple[datetime, float] | None:
    index = bisect_right([item[0] for item in points], timestamp) - 1
    return points[index] if index >= 0 else None


def _metric_payload(metric: MetricValue) -> dict[str, Any]:
    return {"value": round(metric.value, 10) if metric.value is not None else None, "reason": metric.reason}


def build_strategy_performance(
    data_root: Path, strategy_id: str, period: str, *, lookback: int | None = None,
    risk_free_rate: float = 0.02,
) -> dict[str, Any]:
    store = load_performance_store(data_root)
    raw_capital = store["capitalByStrategy"].get(strategy_id)
    if isinstance(raw_capital, bool) or not isinstance(raw_capital, (int, float)) or raw_capital <= 0:
        return {"available": False, "reason": "请先为该策略补录独立初始本金", "strategyId": strategy_id,
                "period": period, "curve": [], "valuationAt": None}
    capital = float(raw_capital)
    advanced = [dict(item) for item in store["trades"] if item.get("strategyId") == strategy_id]
    legacy = _legacy_trades(data_root, strategy_id)
    instruments = {str(item.get("instrumentId") or "") for item in advanced}
    instruments.update(str(item["instrumentId"]) for item in legacy)
    instruments.discard("")
    if not advanced and not legacy:
        return {"available": False, "reason": "该策略尚无交易记录", "strategyId": strategy_id,
                "period": period, "initialCapital": capital, "curve": [], "valuationAt": None}
    closes = _close_series(data_root, instruments, period)
    timeline = {timestamp for points in closes.values() for timestamp, _close in points}
    timeline.update(item["at"] for item in legacy)
    parsed_advanced: list[dict[str, Any]] = []
    for item in advanced:
        try:
            parsed = dict(item)
            parsed["entry"] = _timestamp(item["entryAt"])
            parsed["exit"] = _timestamp(item["exitAt"]) if item.get("exitAt") else None
            parsed["entryPrice"] = float(item["entryPrice"])
            parsed["quantity"] = float(item["quantity"])
            parsed["contractMultiplier"] = float(item["contractMultiplier"])
            parsed["entryFees"] = float(item.get("entryFees") or 0)
            parsed["exitFees"] = float(item.get("exitFees") or 0)
            if item.get("exitAt"):
                parsed["exitPrice"] = float(item["exitPrice"])
            if parsed.get("direction") not in {"LONG", "SHORT"}:
                continue
            if min(parsed["entryPrice"], parsed["quantity"], parsed["contractMultiplier"]) <= 0:
                continue
            if min(parsed["entryFees"], parsed["exitFees"]) < 0:
                continue
            parsed_advanced.append(parsed)
            timeline.add(parsed["entry"])
            if parsed["exit"] is not None:
                timeline.add(parsed["exit"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    activity_times = [item["at"] for item in legacy] + [item["entry"] for item in parsed_advanced]
    if activity_times:
        started_at = min(activity_times)
        timeline = {timestamp for timestamp in timeline if timestamp >= started_at}
        timeline.update(activity_times)
        timeline.update(item["exit"] for item in parsed_advanced if item["exit"] is not None)
    if not timeline:
        return {"available": False, "reason": "缺少所选周期的本地行情", "strategyId": strategy_id,
                "period": period, "initialCapital": capital, "curve": [], "valuationAt": None}

    legacy_positions: dict[str, dict[str, float]] = {}
    legacy_cash = capital
    legacy_index = 0
    curve: list[dict[str, Any]] = []
    latest_valuation: datetime | None = None
    missing_open_market = False
    for timestamp in sorted(timeline):
        while legacy_index < len(legacy) and legacy[legacy_index]["at"] <= timestamp:
            event = legacy[legacy_index]
            position = legacy_positions.setdefault(event["instrumentId"], {"quantity": 0.0, "cost": 0.0})
            if event["side"] == "BUY":
                position["quantity"] += event["quantity"]
                position["cost"] += event["price"] * event["quantity"] + event["fees"]
                legacy_cash -= event["price"] * event["quantity"] + event["fees"]
            elif event["quantity"] <= position["quantity"]:
                average_cost = position["cost"] / position["quantity"] if position["quantity"] else 0.0
                legacy_cash += event["price"] * event["quantity"] - event["fees"]
                position["quantity"] -= event["quantity"]
                position["cost"] = max(0.0, position["cost"] - average_cost * event["quantity"])
            legacy_index += 1
        legacy_value = 0.0
        marked_at: list[datetime] = []
        for instrument_id, position in legacy_positions.items():
            if position["quantity"] <= 0:
                continue
            marked = _last_close(closes.get(instrument_id, []), timestamp)
            if marked is None:
                legacy_value += position["cost"]
            else:
                marked_at.append(marked[0])
                legacy_value += marked[1] * position["quantity"]
        advanced_pnl = 0.0
        for trade in parsed_advanced:
            if timestamp < trade["entry"]:
                continue
            direction = 1.0 if trade.get("direction") == "LONG" else -1.0
            quantity = float(trade["quantity"])
            multiplier = float(trade["contractMultiplier"])
            entry_price = float(trade["entryPrice"])
            fees = float(trade.get("entryFees") or 0)
            if trade["exit"] is not None and timestamp >= trade["exit"]:
                mark_price = float(trade["exitPrice"])
                fees += float(trade.get("exitFees") or 0)
            else:
                marked = _last_close(closes.get(str(trade["instrumentId"]), []), timestamp)
                if marked is None:
                    missing_open_market = True
                    continue
                marked_at.append(marked[0])
                mark_price = marked[1]
            advanced_pnl += direction * (mark_price - entry_price) * quantity * multiplier - fees
        nav = legacy_cash + legacy_value + advanced_pnl
        if marked_at:
            latest_valuation = max(marked_at) if latest_valuation is None else max(latest_valuation, *marked_at)
        curve.append({"t": timestamp.isoformat(), "nav": round(nav, 8)})

    final_timestamp = max(timeline)
    missing_instruments = {
        instrument_id for instrument_id, position in legacy_positions.items()
        if position["quantity"] > 0 and _last_close(closes.get(instrument_id, []), final_timestamp) is None
    }
    missing_instruments.update(
        str(trade["instrumentId"]) for trade in parsed_advanced
        if trade["exit"] is None and _last_close(closes.get(str(trade["instrumentId"]), []), final_timestamp) is None
    )
    if missing_instruments or (missing_open_market and not any(closes.values())):
        return {"available": False, "reason": f"未平仓交易缺少所选周期的本地行情：{sorted(missing_instruments)[0] if missing_instruments else '未知标的'}", "strategyId": strategy_id,
                "period": period, "initialCapital": capital, "curve": curve, "valuationAt": None}
    measured_curve = curve[-lookback:] if lookback else curve
    metrics = performance_metrics([item["t"] for item in measured_curve], [item["nav"] for item in measured_curve], risk_free_rate)
    returns: list[float | None] = [None]
    for before, after in zip(curve, curve[1:]):
        returns.append(after["nav"] / before["nav"] - 1 if before["nav"] > 0 else None)
    return {
        "available": True, "reason": None, "strategyId": strategy_id, "period": period,
        "initialCapital": capital, "riskFreeRate": risk_free_rate, "lookback": lookback,
        "curve": curve, "returns": returns, "valuationAt": latest_valuation.isoformat() if latest_valuation else None,
        "legacyTradeCount": len(legacy), "editableTradeCount": len(advanced),
        "metrics": {name: _metric_payload(metric) for name, metric in metrics.items()},
    }


__all__ = (
    "PerformanceError", "build_strategy_performance", "delete_strategy_trade", "load_performance_store",
    "save_performance_store", "set_strategy_capital", "upsert_strategy_trade",
)
