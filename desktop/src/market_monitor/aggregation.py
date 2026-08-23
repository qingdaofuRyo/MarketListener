"""Versioned exchange-session aggregation without crossing session boundaries.

支持：
- 分钟级聚合（1/5/15/30/60/120/240 分钟），严格按交易时段分桶；
- 日线 -> 周线/月线/季线/年线聚合（架构调整任务第三节）；
- 期货夜盘归属下一交易日（可传入交易日历，避免把 21:00+ 的 bar 算进错误交易日）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Mapping, Sequence

SUPPORTED_PERIOD_MINUTES = (1, 5, 15, 30, 60, 120, 240)
NIGHT_SESSION_START = time(21)


@dataclass(frozen=True)
class SessionRule:
    version: int
    sessions: tuple[tuple[time, time], ...]


SESSION_RULES = {
    "CN_STOCK": SessionRule(1, ((time(9, 30), time(11, 30)), (time(13), time(15)))),
    "HK_STOCK": SessionRule(1, ((time(9, 30), time(12),), (time(13), time(16)))),
    "CN_FUTURE": SessionRule(1, ((time(9), time(11, 30)), (time(13, 30), time(15)), (time(21), time(23)))),
    "CN_FUTURE_0100": SessionRule(2, ((time(9), time(11, 30)), (time(13, 30), time(15)), (time(21), time(1)))),
    "CN_FUTURE_0230": SessionRule(2, ((time(9), time(11, 30)), (time(13, 30), time(15)), (time(21), time(2, 30)))),
}


def aggregate_bars(
    bars: Sequence[Mapping[str, Any]],
    period_minutes: int,
    session_rule: str,
    *,
    trading_calendar: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if session_rule not in SESSION_RULES:
        raise ValueError(f"Unknown session rule: {session_rule}")
    if period_minutes not in SUPPORTED_PERIOD_MINUTES:
        raise ValueError(f"period_minutes must be one of {SUPPORTED_PERIOD_MINUTES}")
    rule = SESSION_RULES[session_rule]
    buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    boundaries: dict[tuple[str, str], datetime] = {}
    for bar in sorted(bars, key=lambda item: str(item["bar_open_time"])):
        open_time = _parse(str(bar["bar_open_time"]))
        session = _session_for(open_time, rule)
        if session is None:
            continue
        session_start, session_end = session
        elapsed_minutes = int((open_time - session_start).total_seconds() // 60)
        bucket_start = session_start + timedelta(minutes=(elapsed_minutes // period_minutes) * period_minutes)
        trading_day = _trading_day_for_bar(bar, open_time, session_rule, trading_calendar)
        bucket_key = (trading_day, bucket_start.isoformat())
        buckets.setdefault(bucket_key, []).append(bar)
        boundaries[bucket_key] = min(bucket_start + timedelta(minutes=period_minutes), session_end)
    output: list[dict[str, Any]] = []
    for key, bucket in sorted(buckets.items()):
        combined = _combine(bucket, period_minutes, boundaries[key], rule.version)
        combined["trading_day"] = key[0]
        output.append(combined)
    return output


def aggregate_daily_bars(
    bars: Sequence[Mapping[str, Any]],
    output_period: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """把日线（1d）聚合为周线、月线、季线或年线。

    按每个标的独立聚合：open 取首日、high/low 取极值、close 取末日、
    volume/amount 求和、open_interest 取末日。不跨标的混合。
    """

    if output_period not in ("1w", "1mo", "1q", "3mo", "6mo", "1y"):
        raise ValueError("output_period must be one of '1w', '1mo', '1q', '3mo', '6mo', '1y'")
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for bar in sorted(bars, key=lambda item: (str(item.get("instrument_key", "")), str(item["bar_open_time"]))):
        trading_day = str(bar["trading_day"])
        bucket = _period_bucket(trading_day, output_period)
        instrument = _instrument_id(bar)
        groups.setdefault((instrument, bucket), []).append(bar)
    output: list[dict[str, Any]] = []
    for (instrument, bucket), group in sorted(groups.items()):
        combined = _combine_daily(group, output_period)
        combined["trading_day"] = str(group[-1]["trading_day"])
        combined["period"] = output_period
        if now is not None:
            combined["is_partial"] = _parse(str(combined["bar_close_time"])) < now
        output.append(combined)
    return output


def _combine(bars: Sequence[Mapping[str, Any]], period_minutes: int, expected_end: datetime, rule_version: int) -> dict[str, Any]:
    first, last = bars[0], bars[-1]
    output = dict(first)
    output.update(
        {
            "period": f"{period_minutes}m" if period_minutes < 60 else f"{period_minutes // 60}h",
            "source_period": first.get("period"),
            "bar_open_time": first["bar_open_time"],
            "bar_close_time": last["bar_close_time"],
            "open": first["open"],
            "high": _max_optional(bars, "high"),
            "low": _min_optional(bars, "low"),
            "close": last["close"],
            "volume": _sum_optional(bars, "volume"),
            "amount": _sum_optional(bars, "amount"),
            "open_interest": last.get("open_interest"),
            "settlement": last.get("settlement"),
            "session_rule_version": rule_version,
            "is_partial": _parse(str(last["bar_close_time"])) < expected_end,
        }
    )
    return output


def _combine_daily(bars: Sequence[Mapping[str, Any]], output_period: str) -> dict[str, Any]:
    first, last = bars[0], bars[-1]
    output = dict(first)
    output.update(
        {
            "period": output_period,
            "source_period": "1d",
            "bar_open_time": first["bar_open_time"],
            "bar_close_time": last["bar_close_time"],
            "open": first["open"],
            "high": _max_optional(bars, "high"),
            "low": _min_optional(bars, "low"),
            "close": last["close"],
            "volume": _sum_optional(bars, "volume"),
            "amount": _sum_optional(bars, "amount"),
            "open_interest": last.get("open_interest"),
            "settlement": last.get("settlement"),
            "aggregated_from": "1d",
            "aggregation_rule_version": 1,
            "is_partial": False,
        }
    )
    return output


def _numeric(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_optional(bars: Sequence[Mapping[str, Any]], field: str) -> float | None:
    """Do not turn an unavailable upstream metric into a synthetic zero."""
    values = [_numeric(bar.get(field)) for bar in bars]
    return sum(values) if values and all(value is not None for value in values) else None


def _max_optional(bars: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [_numeric(bar.get(field)) for bar in bars]
    return max(values) if values and all(value is not None for value in values) else None


def _min_optional(bars: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [_numeric(bar.get(field)) for bar in bars]
    return min(values) if values and all(value is not None for value in values) else None


def _trading_day_for_bar(
    bar: Mapping[str, Any],
    open_time: datetime,
    session_rule: str,
    trading_calendar: Sequence[str] | None,
) -> str:
    source_day = str(bar.get("trading_day", open_time.date().isoformat()))
    if not session_rule.startswith("CN_FUTURE") or open_time.time() < NIGHT_SESSION_START:
        return source_day
    # 期货夜盘（21:00+）归属下一交易日。优先使用调用方提供的交易日历，
    # 否则按自然日+1（周一至周五的夜盘落在下一自然日；周五夜盘需要日历才能正确落在周一）。
    if trading_calendar:
        ordered = sorted(trading_calendar)
        try:
            index = ordered.index(source_day)
        except ValueError:
            return source_day
        candidates = ordered[index + 1:]
        return candidates[0] if candidates else source_day
    next_day = open_time.date() + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day.isoformat()


def _period_bucket(trading_day: str, output_period: str) -> str:
    value = datetime.strptime(trading_day, "%Y-%m-%d")
    if output_period == "1w":
        iso_year, iso_week, _ = value.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if output_period == "1mo":
        return f"{value.year:04d}-{value.month:02d}"
    if output_period in {"1q", "3mo"}:
        return f"{value.year:04d}-Q{(value.month - 1) // 3 + 1}"
    if output_period == "6mo":
        return f"{value.year:04d}-H{1 if value.month <= 6 else 2}"
    return f"{value.year:04d}"


def _instrument_id(bar: Mapping[str, Any]) -> str:
    key = bar.get("instrument_key")
    if isinstance(key, Mapping):
        return ".".join(str(key.get(part, "")) for part in ("country_or_market", "exchange", "asset_type", "code"))
    return str(key if key is not None else bar.get("instrument_id", ""))


def _session_for(value: datetime, rule: SessionRule) -> tuple[datetime, datetime] | None:
    for start, end in rule.sessions:
        session_start = value.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        session_end = value.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if end <= start:
            session_end += timedelta(days=1)
            if value < session_start:
                session_start -= timedelta(days=1)
                session_end -= timedelta(days=1)
        if session_start <= value < session_end:
            return session_start, session_end
    return None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
