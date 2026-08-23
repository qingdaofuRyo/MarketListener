"""Local K-line runtime for persisted formula_v1 strategies."""

from __future__ import annotations

import math
import threading
import uuid
from bisect import bisect_right
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from market_monitor.formula_engine import (
    FormulaError,
    bollinger_lower,
    bollinger_upper,
    close_new_high,
    close_new_low,
    cross_sectional_momentum,
    down_count,
    down_up_ratio,
    evaluate_formula,
    gann_falling_rate,
    gann_rising_rate,
    hsar_resistance,
    hsar_support,
    limit_down_count,
    limit_up_count,
    moving_average,
    no_limit_down,
    no_limit_up,
    period_return,
    range_high_low_ratio,
    range_low_high_ratio,
    standard_deviation,
    time_series_momentum,
    up_count,
    up_down_ratio,
    validate_formula_document,
    volume_slope,
)
from market_monitor.industry_graph.f10 import CompanyRepository
from market_monitor.strategy_performance import build_strategy_performance
from market_monitor.web_api.common import read_bars
from market_monitor.web_api.market import _derived_bars, _logical_instruments

_PERIOD_ALIASES = {
    "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m", "60min": "1h",
    "1hour": "1h", "1day": "1d",
}
_F10_CAP_CACHE: dict[tuple[tuple[str, int, int], ...], dict[str, tuple[float | None, float | None]]] = {}
_F10_CAP_LOCK = threading.Lock()


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical_period(value: str) -> str:
    period = value.strip().lower()
    return _PERIOD_ALIASES.get(period, period)


def _market_type_matches(item: Mapping[str, Any], market_type: str) -> bool:
    wanted = market_type.strip().lower()
    market = str(item.get("market") or "").upper()
    asset_type = str(item.get("assetType") or "").upper()
    series_kind = str(item.get("seriesKind") or "").upper()
    instrument_id = str(item.get("instrumentId") or "").upper()
    code = instrument_id.rsplit(".", 1)[-1]
    if wanted == "a_share":
        return market == "CN" and asset_type == "STOCK"
    if wanted == "hk_stock":
        return market == "HK" and asset_type == "STOCK"
    if wanted == "main_board":
        return market == "CN" and asset_type == "STOCK" and (
            (".SSE." in instrument_id and code.startswith(("600", "601", "603", "605", "900")))
            or (".SZSE." in instrument_id and code.startswith(("000", "001", "002", "003")))
        )
    if wanted == "chinext":
        return market == "CN" and asset_type == "STOCK" and code.startswith(("300", "301"))
    if wanted == "star":
        return market == "CN" and asset_type == "STOCK" and code.startswith(("688", "689"))
    if wanted == "etf":
        return asset_type == "ETF"
    if wanted == "bse":
        return market == "CN" and asset_type == "STOCK" and (".BSE." in instrument_id or code.startswith(("4", "8")))
    if wanted == "cn_future":
        return market == "CN" and asset_type == "FUTURE"
    if wanted == "cn_commodity_index":
        return market == "CN" and asset_type == "INDEX" and series_kind == "COMMODITY_INDEX"
    if wanted == "global_future":
        return market == "GLOBAL" and asset_type == "FUTURE"
    return False


def _is_st_name(value: Any) -> bool:
    name = str(value or "").strip().upper().replace(" ", "")
    return name.startswith(("ST", "*ST", "ＳＴ", "＊ＳＴ"))


def _f10_market_caps(data_root: Path) -> dict[str, tuple[float | None, float | None]]:
    paths = (data_root / "industry" / "f10" / "cn_f10.jsonl", data_root / "industry" / "f10" / "hk_f10.jsonl")
    fingerprint = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths if path.is_file()
    )
    cached = _F10_CAP_CACHE.get(fingerprint)
    if cached is not None:
        return cached
    with _F10_CAP_LOCK:
        cached = _F10_CAP_CACHE.get(fingerprint)
        if cached is not None:
            return cached
        repository = CompanyRepository(data_root)
        result: dict[str, tuple[float | None, float | None]] = {}
        page = 1
        while True:
            companies = repository.list_companies(page=page, page_size=500)
            for item in companies.items:
                result[item.instrument_key] = (
                    item.total_market_cap.value / 100_000_000 if item.total_market_cap else None,
                    item.float_market_cap.value / 100_000_000 if item.float_market_cap else None,
                )
            if page * companies.page_size >= companies.total:
                break
            page += 1
        _F10_CAP_CACHE.clear()
        _F10_CAP_CACHE[fingerprint] = result
        return result


def _cap_matches(value: float | None, specification: Any) -> bool:
    if not isinstance(specification, Mapping):
        return True
    try:
        threshold = float(specification["value"])
    except (KeyError, TypeError, ValueError):
        return False
    if value is None:
        return False
    operator = str(specification.get("operator") or "gt").lower()
    return value > threshold if operator == "gt" else value < threshold if operator == "lt" else False


def _limit_rate(item: Mapping[str, Any], name: str, day: str) -> Decimal:
    code = str(item.get("instrumentId") or "").rsplit(".", 1)[-1]
    if _is_st_name(name):
        return Decimal("0.05")
    if ".BSE." in str(item.get("instrumentId") or "").upper() or code.startswith(("4", "8")):
        return Decimal("0.30")
    if code.startswith(("688", "689")):
        return Decimal("0.20")
    if code.startswith(("300", "301")) and day >= "2020-08-24":
        return Decimal("0.20")
    return Decimal("0.10")


def _daily_limit_by_day(
    data_root: Path, item: Mapping[str, Any], name: str, bars: list[dict[str, Any]] | None = None,
) -> dict[str, tuple[bool | None, bool | None]]:
    if str(item.get("market") or "").upper() != "CN" or str(item.get("assetType") or "").upper() != "STOCK":
        return {}
    storage_id = str(item.get("storageInstrumentId") or "")
    bars = bars if bars is not None else (_bars(data_root, storage_id, "1d", 100_000) if storage_id else [])
    result: dict[str, tuple[bool | None, bool | None]] = {}
    previous_close: Decimal | None = None
    for bar in bars:
        day = str(bar.get("trading_date") or str(bar.get("bar_open_time") or "")[:10])
        try:
            close = Decimal(str(bar["close"]))
        except (KeyError, ValueError):
            continue
        if previous_close is None or not day:
            result[day] = (None, None)
        else:
            rate = _limit_rate(item, name, day)
            upper = (previous_close * (Decimal("1") + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            lower = (previous_close * (Decimal("1") - rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tolerance = Decimal("0.005")
            result[day] = (close >= upper - tolerance, close <= lower + tolerance)
        previous_close = close
    return result


def _bars(data_root: Path, storage_id: str, period: str, limit: int) -> list[dict[str, Any]]:
    raw = read_bars(data_root, storage_id, period=period, limit=limit)
    derived = raw or _derived_bars(data_root, storage_id, period)
    return derived[-limit:]


def _parameter_values(document: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, definition in dict(document.get("parameters") or {}).items():
        if name in overrides:
            result[name] = overrides[name]
        elif isinstance(definition, Mapping) and "default" in definition:
            result[name] = definition["default"]
        elif isinstance(definition, (int, float, bool)) and not isinstance(definition, str):
            result[name] = definition
        else:
            raise FormulaError("MISSING_PARAMETER", f"缺少参数：{name}")
    unknown = set(overrides) - set(result)
    if unknown:
        raise FormulaError("UNKNOWN_PARAMETER", f"未知参数：{sorted(unknown)[0]}")
    return result


def _bar_number(bar: Mapping[str, Any], field: str) -> float | None:
    try:
        value = float(bar[field])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _builder_function_value(
    data_root: Path,
    item: Mapping[str, Any],
    node: Mapping[str, Any],
    fallback_period: str,
    limit: int,
) -> tuple[float | bool | None, str | None, int, str | None]:
    """Evaluate one visual-builder function against its own K-line period.

    Builder conditions are screening conditions, therefore only their latest
    value is required.  Loading each condition independently is deliberate:
    a daily resistance check can be combined with a 30-minute volume check
    without upsampling one of the two series or leaking future bars.
    """
    function_id = str(node.get("functionId") or "")
    period = _canonical_period(str(node.get("period") or fallback_period))
    storage_id = str(item.get("storageInstrumentId") or "")
    bars = _bars(data_root, storage_id, period, limit)
    if not bars:
        return None, None, 0, f"{period} 周期缺少本地 K 线"
    latest_at = str(bars[-1].get("bar_close_time") or bars[-1].get("bar_open_time") or "")
    opens = [_bar_number(bar, "open") for bar in bars]
    highs = [_bar_number(bar, "high") for bar in bars]
    lows = [_bar_number(bar, "low") for bar in bars]
    closes = [_bar_number(bar, "close") for bar in bars]
    volumes = [_bar_number(bar, "volume") for bar in bars]
    raw_args = node.get("args") or []
    if not isinstance(raw_args, list) or any(isinstance(value, bool) for value in raw_args):
        return None, latest_at, len(bars), "策略函数参数无效"
    try:
        args = [float(value) for value in raw_args]
        if any(not math.isfinite(value) for value in args):
            raise ValueError
        values: list[Any]
        if function_id == "period_return" and len(args) == 1:
            values = period_return(closes, args[0])
        elif function_id == "no_limit_up" and len(args) == 1:
            flags = _daily_limit_by_day(data_root, item, str(item.get("name") or ""), bars if period == "1d" else None)
            values = no_limit_up([flags.get(str(bar.get("trading_date") or str(bar.get("bar_open_time") or "")[:10]), (None, None))[0] for bar in bars], args[0])
        elif function_id == "no_limit_down" and len(args) == 1:
            flags = _daily_limit_by_day(data_root, item, str(item.get("name") or ""), bars if period == "1d" else None)
            values = no_limit_down([flags.get(str(bar.get("trading_date") or str(bar.get("bar_open_time") or "")[:10]), (None, None))[1] for bar in bars], args[0])
        elif function_id in {"limit_up_count", "limit_down_count"} and len(args) == 1:
            flags = _daily_limit_by_day(data_root, item, str(item.get("name") or ""), bars if period == "1d" else None)
            index = 0 if function_id == "limit_up_count" else 1
            series = [flags.get(str(bar.get("trading_date") or str(bar.get("bar_open_time") or "")[:10]), (None, None))[index] for bar in bars]
            values = (limit_up_count if function_id == "limit_up_count" else limit_down_count)(series, args[0])
        elif function_id == "close_new_high" and len(args) == 1:
            values = close_new_high(closes, highs, args[0])
        elif function_id == "close_new_low" and len(args) == 1:
            values = close_new_low(closes, lows, args[0])
        elif function_id == "up_count" and len(args) == 1:
            values = up_count(opens, closes, args[0])
        elif function_id == "down_count" and len(args) == 1:
            values = down_count(opens, closes, args[0])
        elif function_id == "up_down_ratio" and len(args) == 1:
            values = up_down_ratio(opens, closes, args[0])
        elif function_id == "down_up_ratio" and len(args) == 1:
            values = down_up_ratio(opens, closes, args[0])
        elif function_id == "range_high_low_ratio" and len(args) == 1:
            values = range_high_low_ratio(highs, lows, args[0])
        elif function_id == "range_low_high_ratio" and len(args) == 1:
            values = range_low_high_ratio(highs, lows, args[0])
        elif function_id == "volume_slope" and len(args) == 2:
            values = volume_slope(volumes, args[0], args[1])
        elif function_id == "gann_rising_rate" and len(args) == 1:
            values = gann_rising_rate(lows, args[0])
        elif function_id == "gann_falling_rate" and len(args) == 1:
            values = gann_falling_rate(highs, args[0])
        elif function_id == "hsar_resistance" and len(args) == 2:
            values = hsar_resistance(highs, args[0], args[1])
        elif function_id == "hsar_support" and len(args) == 2:
            values = hsar_support(lows, args[0], args[1])
        else:
            return None, latest_at, len(bars), f"未知或参数不匹配的策略函数：{function_id}"
    except (FormulaError, TypeError, ValueError, ZeroDivisionError):
        return None, latest_at, len(bars), "历史不足或函数参数无效"
    value = values[-1] if values else None
    return value if isinstance(value, (float, int, bool)) else None, latest_at, len(bars), None


def _builder_node(
    data_root: Path,
    item: Mapping[str, Any],
    node: Any,
    fallback_period: str,
    limit: int,
) -> tuple[bool | None, str | None, int, str | None]:
    if not isinstance(node, Mapping):
        return None, None, 0, "策略条件节点无效"
    children = node.get("children")
    if isinstance(children, list):
        results = [_builder_node(data_root, item, child, fallback_period, limit) for child in children]
        if not results:
            return None, None, 0, "条件组为空"
        operator = str(node.get("operator") or "and").lower()
        if operator not in {"and", "or"}:
            return None, None, 0, "条件组运算符无效"
        known = [result[0] for result in results]
        value = all(known) if operator == "and" and all(item is not None for item in known) else any(known) if operator == "or" and all(item is not None for item in known) else None
        timestamp = max((result[1] or "" for result in results), default="") or None
        bar_count = max((result[2] for result in results), default=0)
        reason = next((result[3] for result in results if result[3]), None)
        return value, timestamp, bar_count, reason
    function_id = str(node.get("functionId") or "")
    if function_id in {"market_scope", "market_cap"}:
        return True, None, 0, None
    value, timestamp, bar_count, reason = _builder_function_value(data_root, item, node, fallback_period, limit)
    if value is None:
        return None, timestamp, bar_count, reason or "函数不可计算"
    if isinstance(value, bool) and node.get("operator") is None:
        return value, timestamp, bar_count, None
    operator = str(node.get("operator") or "gt")
    try:
        threshold = float(node.get("value"))
    except (TypeError, ValueError):
        return None, timestamp, bar_count, "比较阈值无效"
    comparisons = {"gt": value > threshold, "ge": value >= threshold, "lt": value < threshold, "le": value <= threshold, "eq": value == threshold, "ne": value != threshold}
    return comparisons.get(operator), timestamp, bar_count, None if operator in comparisons else "比较符无效"


def _run_builder_strategy(
    data_root: Path,
    strategy_id: str,
    document: Mapping[str, Any],
    *,
    period: str | None,
    limit_instruments: int,
    limit_per_instrument: int,
) -> dict[str, Any]:
    universe = dict(document.get("universe") or {})
    fallback_period = _canonical_period(period or str(document.get("period") or "1d"))
    market_types = [str(value) for value in universe.get("market_types", [])]
    selected_ids = {str(value) for value in universe.get("instrument_ids", [])}
    logical = _logical_instruments(data_root)
    candidates = [
        item for item in logical.values()
        if (not market_types or any(_market_type_matches(item, value) for value in market_types))
        and (not selected_ids or str(item.get("instrumentId")) in selected_ids)
        and item.get("storageInstrumentId")
    ]
    if bool(universe.get("exclude_st")):
        candidates = [item for item in candidates if not _is_st_name(item.get("name"))]
    total_cap_filter, float_cap_filter = universe.get("total_market_cap_yi"), universe.get("float_market_cap_yi")
    if isinstance(total_cap_filter, Mapping) or isinstance(float_cap_filter, Mapping):
        caps = _f10_market_caps(data_root)
        candidates = [item for item in candidates if _cap_matches(caps.get(str(item.get("instrumentId")), (None, None))[0], total_cap_filter) and _cap_matches(caps.get(str(item.get("instrumentId")), (None, None))[1], float_cap_filter)]
    tree = document.get("condition_tree")
    if not isinstance(tree, Mapping):
        raise FormulaError("INVALID_BUILDER", "缺少规范化策略条件树")
    scans: list[dict[str, Any]] = []
    for item in candidates[:limit_instruments]:
        signal, timestamp, bar_count, reason = _builder_node(data_root, item, tree, fallback_period, limit_per_instrument)
        instrument_id = str(item["instrumentId"])
        signals = [{"index": max(0, bar_count - 1), "instrumentId": instrument_id, "barOpenTime": timestamp, "value": 1.0}] if signal else []
        scans.append({"instrumentId": instrument_id, "name": item.get("name"), "barCount": bar_count,
                      "signalCount": len(signals), "latestValue": 1.0 if signal else 0.0 if signal is False else None,
                      "unavailableReason": reason if signal is None else None, "signals": signals})
    finished_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    return {"report": {"runId": f"builder_{uuid.uuid4().hex[:16]}", "strategyId": strategy_id,
                          "strategyVersion": "builder_v1", "scriptKind": "builder_v1", "period": fallback_period,
                          "status": "PASS", "startedAt": finished_at, "finishedAt": finished_at,
                          "instrumentCount": len(scans), "totalSignals": sum(item["signalCount"] for item in scans),
                          "universe": {"marketTypes": market_types}, "instruments": scans}, "signals": scans}


def _aligned_closes(
    source: Mapping[str, list[tuple[datetime, float]]], timeline: list[datetime],
) -> dict[str, list[float | None]]:
    result: dict[str, list[float | None]] = {}
    for instrument_id, points in source.items():
        times = [point[0] for point in points]
        values: list[float | None] = []
        for timestamp in timeline:
            index = bisect_right(times, timestamp) - 1
            values.append(points[index][1] if index >= 0 else None)
        result[instrument_id] = values
    return result


def run_formula_strategy(
    data_root: Path,
    strategy_id: str,
    document: Mapping[str, Any],
    *,
    parameters: Mapping[str, Any] | None = None,
    period: str | None = None,
    limit_instruments: int = 200,
    limit_per_instrument: int = 500,
) -> dict[str, Any]:
    if document.get("condition_kind") == "builder_v1":
        return _run_builder_strategy(
            data_root,
            strategy_id,
            document,
            period=period,
            limit_instruments=limit_instruments,
            limit_per_instrument=limit_per_instrument,
        )
    program = validate_formula_document(document)
    formula_period = _canonical_period(period or str(document["period"]))
    universe = dict(document["universe"])
    market_types = [str(value) for value in universe.get("market_types", [])]
    market = str(universe.get("market") or "MIXED").upper()
    asset_type = str(universe.get("asset_type") or "MIXED").upper()
    selected_ids = {str(value) for value in universe.get("instrument_ids", [])}
    logical = _logical_instruments(data_root)
    candidates = [
        item for item in logical.values()
        if (any(_market_type_matches(item, value) for value in market_types) if market_types else (
            str(item.get("market") or "").upper() == market
            and str(item.get("assetType") or "").upper() == asset_type
        ))
        and (not selected_ids or str(item.get("instrumentId")) in selected_ids)
        and item.get("storageInstrumentId")
    ]
    if bool(universe.get("exclude_st")):
        candidates = [item for item in candidates if not _is_st_name(item.get("name"))]
    total_cap_filter = universe.get("total_market_cap_yi")
    float_cap_filter = universe.get("float_market_cap_yi")
    if isinstance(total_cap_filter, Mapping) or isinstance(float_cap_filter, Mapping):
        caps = _f10_market_caps(data_root)
        candidates = [
            item for item in candidates
            if _cap_matches(caps.get(str(item.get("instrumentId")), (None, None))[0], total_cap_filter)
            and _cap_matches(caps.get(str(item.get("instrumentId")), (None, None))[1], float_cap_filter)
        ]
    candidates = candidates[:limit_instruments]
    source: dict[str, list[tuple[datetime, float]]] = {}
    open_source: dict[str, list[tuple[datetime, float]]] = {}
    high_source: dict[str, list[tuple[datetime, float]]] = {}
    low_source: dict[str, list[tuple[datetime, float]]] = {}
    volume_source: dict[str, list[tuple[datetime, float]]] = {}
    limit_days: dict[str, dict[str, tuple[bool | None, bool | None]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for item in candidates:
        instrument_id = str(item["instrumentId"])
        points: list[tuple[datetime, float]] = []
        open_points: list[tuple[datetime, float]] = []
        high_points: list[tuple[datetime, float]] = []
        low_points: list[tuple[datetime, float]] = []
        volume_points: list[tuple[datetime, float]] = []
        formula_bars = _bars(data_root, str(item["storageInstrumentId"]), formula_period, limit_per_instrument)
        for bar in formula_bars:
            try:
                close = float(bar["close"])
                timestamp = _time(bar.get("bar_close_time") or bar["bar_open_time"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(close):
                points.append((timestamp, close))
            for field, collection in (
                ("open", open_points), ("high", high_points), ("low", low_points), ("volume", volume_points),
            ):
                try:
                    value = float(bar[field])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(value):
                    collection.append((timestamp, value))
        if points:
            source[instrument_id] = sorted(set(points))
            open_source[instrument_id] = sorted(set(open_points))
            high_source[instrument_id] = sorted(set(high_points))
            low_source[instrument_id] = sorted(set(low_points))
            volume_source[instrument_id] = sorted(set(volume_points))
            metadata[instrument_id] = item
            limit_days[instrument_id] = _daily_limit_by_day(
                data_root, item, str(item.get("name") or ""), formula_bars if formula_period == "1d" else None,
            )
    if not source:
        raise FormulaError("NO_LOCAL_BARS", "所选范围和周期没有本地 K 线")
    timeline = sorted({timestamp for points in source.values() for timestamp, _close in points})
    closes = _aligned_closes(source, timeline)
    opens = _aligned_closes(open_source, timeline)
    highs = _aligned_closes(high_source, timeline)
    lows = _aligned_closes(low_source, timeline)
    volumes = _aligned_closes(volume_source, timeline)
    values = _parameter_values(document, parameters or {})
    cs_cache: dict[tuple[int, int], dict[str, list[int | None]]] = {}
    performance_cache: dict[tuple[int, float], dict[str, Any]] = {}

    def performance(lookback: Any, risk_free_rate: Any = 0.02) -> dict[str, Any]:
        if isinstance(lookback, bool) or int(lookback) != lookback or lookback < 2:
            raise FormulaError("INVALID_LOOKBACK", "绩效指标 lookback 必须是大于等于 2 的整数")
        key = (int(lookback), float(risk_free_rate))
        if key not in performance_cache:
            performance_cache[key] = build_strategy_performance(
                data_root, strategy_id, formula_period, lookback=int(lookback), risk_free_rate=float(risk_free_rate),
            )
        return performance_cache[key]

    scans: list[dict[str, Any]] = []
    for instrument_id, close_values in closes.items():
        unavailable_reason: str | None = None
        daily_flags = limit_days[instrument_id]
        limit_up_values = [daily_flags.get(timestamp.date().isoformat(), (None, None))[0] for timestamp in timeline]
        limit_down_values = [daily_flags.get(timestamp.date().isoformat(), (None, None))[1] for timestamp in timeline]

        def cs_callback(_close: Any, lookback: Any, buckets: Any) -> list[int | None]:
            key = (int(lookback), int(buckets))
            if key not in cs_cache:
                cs_cache[key] = cross_sectional_momentum(closes, lookback, buckets)
            return cs_cache[key][instrument_id]

        def sharpe_callback(_returns: Any, risk_free_rate: Any, lookback: Any) -> float | None:
            nonlocal unavailable_reason
            result = performance(lookback, risk_free_rate)
            if not result.get("available"):
                unavailable_reason = str(result.get("reason") or "策略绩效不可计算")
                return None
            metric = result.get("metrics", {}).get("sharpe", {})
            unavailable_reason = str(metric.get("reason")) if metric.get("value") is None else None
            return metric.get("value")

        def calmar_callback(_nav: Any, lookback: Any) -> float | None:
            nonlocal unavailable_reason
            result = performance(lookback)
            if not result.get("available"):
                unavailable_reason = str(result.get("reason") or "策略绩效不可计算")
                return None
            metric = result.get("metrics", {}).get("calmar", {})
            unavailable_reason = str(metric.get("reason")) if metric.get("value") is None else None
            return metric.get("value")

        evaluated = evaluate_formula(
            program,
            {"open": opens[instrument_id], "high": highs[instrument_id], "low": lows[instrument_id],
             "close": close_values, "volume": volumes[instrument_id],
             "limit_up": limit_up_values, "limit_down": limit_down_values,
             "strategy_returns": [], "strategy_nav": [], **values},
            {"ts_momentum": time_series_momentum, "cs_momentum": cs_callback,
             "sharpe": sharpe_callback, "calmar": calmar_callback,
             "hsar_resistance": hsar_resistance, "hsar_support": hsar_support,
             "ma": moving_average, "sd": standard_deviation,
             "bollinger_upper": bollinger_upper, "bollinger_lower": bollinger_lower,
             "gann_rising_rate": gann_rising_rate, "gann_falling_rate": gann_falling_rate,
             "period_return": period_return, "no_limit_up": no_limit_up, "no_limit_down": no_limit_down,
             "limit_up_count": limit_up_count, "limit_down_count": limit_down_count,
             "close_new_high": close_new_high, "close_new_low": close_new_low,
             "up_count": up_count, "down_count": down_count,
             "up_down_ratio": up_down_ratio, "down_up_ratio": down_up_ratio,
             "range_high_low_ratio": range_high_low_ratio, "range_low_high_ratio": range_low_high_ratio,
             "volume_slope": volume_slope},
        )
        output_values = evaluated["value"] if isinstance(evaluated["value"], list) else [evaluated["value"]] * len(timeline)
        output_signals = evaluated["signal"] if isinstance(evaluated["signal"], list) else [evaluated["signal"]] * len(timeline)
        signals = [
            {"index": index, "instrumentId": instrument_id, "barOpenTime": timeline[index].isoformat(),
             "value": output_values[index]}
            for index, signal in enumerate(output_signals) if signal and output_values[index] is not None
        ]
        latest_value = output_values[-1] if output_values else None
        scans.append({
            "instrumentId": instrument_id, "name": metadata[instrument_id].get("name"), "barCount": len(timeline),
            "signalCount": len(signals), "latestValue": latest_value,
            "unavailableReason": (unavailable_reason or "历史不足或指标分母无效") if latest_value is None else None,
            "signals": signals[-50:],
        })
    run_id = f"formula_{uuid.uuid4().hex[:16]}"
    finished_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    return {
        "report": {"runId": run_id, "strategyId": strategy_id, "strategyVersion": "formula_v1",
                   "scriptKind": "formula_v1", "period": formula_period, "status": "PASS",
                   "startedAt": finished_at, "finishedAt": finished_at,
                   "instrumentCount": len(scans), "totalSignals": sum(item["signalCount"] for item in scans),
                   "universe": {"market": market, "assetType": asset_type, "marketTypes": market_types},
                   "instruments": scans},
        "signals": scans,
    }


__all__ = ("run_formula_strategy",)
