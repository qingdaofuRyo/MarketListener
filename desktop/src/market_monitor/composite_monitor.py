"""Attention-gated composite observations, isolated from account/order execution."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from market_monitor.composite_program import compile_program, evaluate_part, finite
from market_monitor.signal_monitor import normalized_time


def completed_bars(bars: list[dict[str, Any]], period: str = "1d") -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    result = {}
    for bar in bars:
        if bar.get("is_partial") is True:
            continue
        opened = str(bar.get("bar_open_time") or "")
        closed = bar.get("bar_close_time")
        if not closed or normalized_time(closed) <= normalized_time(opened):
            start = datetime.fromisoformat(normalized_time(opened)).astimezone(timezone(timedelta(hours=8)))
            minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120}
            if period in minutes:
                end = start + timedelta(minutes=minutes[period])
            elif period == "1d":
                end = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            elif period == "1w":
                end = (start + timedelta(days=7 - start.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                width = {"1mo": 1, "3mo": 3, "1y": 12}[period]
                month_index = start.year * 12 + ((start.month - 1) // width + 1) * width
                year, month = divmod(month_index, 12)
                end = start.replace(year=year, month=month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            # Without a session-close timestamp, Beijing midnight is not proof
            # that an overseas session has ended. Delay calendar bars by one
            # extra day rather than consume potentially unfinished prices.
            if period not in minutes and (end + timedelta(days=1)).astimezone(timezone.utc).isoformat() > now:
                continue
            closed = end.isoformat()
        stamp = normalized_time(closed)
        if stamp <= now:
            result[stamp] = {**bar, "_at": stamp}
    return [result[key] for key in sorted(result)]


def boolean_outputs(output: dict[str, Any], keys: tuple[str, ...]) -> None:
    if any(value is not None and type(value) is not bool for key in keys if (value := output[key]) is not None):
        raise ValueError("关注和择时条件必须返回布尔值或None")


def position_values(output: dict[str, Any], observed_count: int) -> dict[str, Any]:
    bounds = {"rewardRisk": (0, None), "winRate": (0, 1), "allocation": (0, 1),
              "capitalUsage": (0, 1), "leverage": (0, None)}
    missing = []
    for key, (minimum, maximum) in bounds.items():
        value = output[key]
        if value is None:
            missing.append(key)
        elif not finite(value) or value < minimum or maximum is not None and value > maximum:
            raise ValueError(f"仓位输出{key}超出合法范围")
    return {**output, "missing": missing, "observedRounds": observed_count}


def advance_composite(state: dict[str, Any], definition: dict[str, Any], instrument: dict[str, Any],
                      bars: list[dict[str, Any]], *, allow_attention: bool = True) -> None:
    program = compile_program(definition["source"])
    if definition.get("dependencies") != program.dependencies:
        raise ValueError("策略函数版本或定义已变化，请重新保存策略")
    fetched_count = len(bars)
    bars = completed_bars(bars, definition["period"])
    if not bars:
        raise ValueError("没有已结束的有效行情")
    identifier = instrument["instrumentId"]
    key = f"{definition['id']}@{definition['version']}|{identifier}"
    previous = state.get("observations", {}).get(key, {})
    cursor = previous.get("cursor")
    if cursor and fetched_count >= 500 and cursor < bars[0]["_at"]:
        raise ValueError("监控缺口超过500根窗口，未跳过信号，请补充历史")
    indices = [index for index, bar in enumerate(bars) if not cursor or bar["_at"] > cursor]
    if not cursor:
        indices = indices[-1:]
    # Commit per instrument/version only after the full batch validates.
    item = deepcopy(previous) or {"instrumentId": identifier, "strategyId": definition["id"],
                                 "strategyVersion": definition["version"], "strategyName": definition["displayName"],
                                 "period": definition["period"], "watched": False, "positionOpen": False,
                                 "closedRounds": 0, "winningRounds": 0}
    events = []
    for index in indices:
        bar = bars[index]
        stamp, close = bar["_at"], bar.get("close")
        if not finite(close):
            raise ValueError("最新收盘价缺失或非有限数值")
        history = bars[:index + 1]
        rounds = item["closedRounds"]
        # Only explicit metadata/record margin, never inferred from a market name.
        margin = bar.get("margin_rate", instrument.get("marginRate"))
        margin = margin if finite(margin) and 0 < margin <= 1 else None
        context = {"direction": (1 if item.get("direction") == "long" else -1) if item["watched"] else 0,
                   "observed_win_rate": item["winningRounds"] / rounds if rounds else None, "margin_rate": margin}
        attention = evaluate_part(program, "attention", history, context)
        boolean_outputs(attention, ("long", "short", "cancel"))
        lookback = attention["lookback"]
        if not finite(lookback) or int(lookback) != lookback or not 1 <= lookback <= 499:
            raise ValueError("关注lookback必须为1至499根有效K线")
        if any(attention[key] is None for key in ("long", "short", "cancel")):
            raise ValueError("关注条件缺少字段或历史暖机不足")

        def event(action: str, reason: str) -> dict[str, Any]:
            value = {"instrumentId": identifier, "strategyId": definition["id"], "strategyVersion": definition["version"],
                     "strategyName": definition["displayName"], "period": definition["period"],
                     "direction": item.get("direction"), "action": action, "at": stamp,
                     "barAt": bar.get("bar_open_time"), "price": close, "reason": reason,
                     "position": deepcopy(item.get("position"))}
            events.append(value)
            item["latestSignal"] = value
            return value

        item.update(cursor=stamp, asOf=stamp, latestPrice=close)
        if item["watched"] and attention["cancel"]:
            event("unwatch", "关注取消条件成立，结束本轮观察")
            item.update(watched=False, positionOpen=False, position=None, unwatchAt=stamp)
            continue
        if not item["watched"]:
            if not allow_attention or attention["cancel"]:
                continue
            if attention["long"] and attention["short"]:
                raise ValueError("关注条件同时给出多空方向，本根不建立关注")
            direction = "long" if attention["long"] else "short" if attention["short"] else None
            if not direction:
                continue
            start = index - int(lookback)
            if start < 0 or not finite(bars[start].get("close")) or bars[start]["close"] == 0:
                raise ValueError("关注涨跌幅基准历史不足或为零")
            item.update(watched=True, direction=direction, watchAt=stamp, positionOpen=False,
                        referenceAt=bars[start]["_at"], referencePrice=bars[start]["close"])
            context["direction"] = 1 if direction == "long" else -1
            event("watch", "关注方向条件成立")
        item["changePct"] = (close / item["referencePrice"] - 1) * 100
        if not finite(item["changePct"]):
            raise ValueError("关注涨跌幅超出有限数值范围")
        # These parts are never invoked for an unwatched instrument.
        item["position"] = position_values(evaluate_part(program, "position", history, context), rounds)
        timing = evaluate_part(program, "timing", history, context)
        boolean_outputs(timing, ("open", "add", "reduce", "close"))
        if any(value is None for value in timing.values()):
            raise ValueError("择时条件缺少字段或历史暖机不足")
        if item["positionOpen"]:
            if stamp <= item["openedAt"]:
                continue
            if timing["close"]:
                entry = item["entryPrice"]
                if entry != 0:
                    move = (close - entry) * (1 if item["direction"] == "long" else -1)
                    item["closedRounds"] += 1
                    item["winningRounds"] += int(move > 0)
                item.update(positionOpen=False, closedAt=stamp)
                event("close", "平仓条件成立，仍关注时可再次开仓")
            elif timing["reduce"]:
                event("reduce", "已开仓且减仓条件成立")
            elif timing["add"]:
                event("add", "已开仓且加仓条件成立")
        elif timing["open"] and (item["position"].get("allocation") or 0) > 0:
            item.update(positionOpen=True, openedAt=stamp, entryPrice=close)
            event("open", "已关注、仓位配置可用且开仓条件成立")
    state.setdefault("observations", {})[key] = item
    state["events"] = [*state.get("events", []), *events][-2000:]


def current_items(state: dict[str, Any], definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = {(item["id"], item["version"]) for item in definitions if item["enabled"] and not item.get("deleted")}
    return [item for item in state.get("observations", {}).values()
            if item.get("watched") and (item["strategyId"], item["strategyVersion"]) in valid]


def peer_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Comparable timestamps and direction are evidence of synchrony, not causality."""
    groups = {}
    for item in items:
        key = (item["strategyId"], item["strategyVersion"], item["direction"], item["referenceAt"], item["asOf"])
        groups.setdefault(key, []).append(item)
    return [{**item, "peers": [{"instrumentId": other["instrumentId"], "changePct": other["changePct"]}
                              for other in group if other["instrumentId"] != item["instrumentId"]][:30],
             "peerCount": len(group) - 1} for group in groups.values() for item in group]
