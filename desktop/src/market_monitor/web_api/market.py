"""/api/market router: local silver overview, instruments, and K-line bars.

This router is a thin adapter over the local silver parquet partitions.  It
only reads data already stored under ``data_control/silver`` and never
executes arbitrary SQL, shell commands or third-party requests.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.aggregation import aggregate_bars, aggregate_daily_bars
from market_monitor.market_data_version import market_data_version
from market_monitor.market_classification import (
    UNCLASSIFIED_CATEGORY,
    classify_market,
    market_category_options,
    matches_market_category,
    night_session,
)
from market_monitor.market_query_cache import get_kline_query_store
from market_monitor.futures import (
    compute_future_capital_deposit,
    futures_product_name,
    is_expired_futures_contract,
    resolve_futures_contract_spec,
)
from market_monitor.industry_graph.f10 import CompanyRepository
from market_monitor.tdx_local import _cn_classification
from market_monitor.unclassified_instruments import scan_unclassified_tdx

from .common import (
    DEFAULT_PAGE_SIZE,
    MAX_BARS,
    MAX_PAGE_SIZE,
    bar_bounds,
    beijing_text,
    clean,
    load_inventory,
    load_json,
    instrument_periods,
    paginate,
    read_bars,
    read_bars_before,
    read_bars_window,
    save_json,
)
from .sources import local_inventory

router = APIRouter(prefix="/api/market", tags=["market"])

# 生产环境默认数据根目录；测试或其他宿主可通过 ``app.state.data_root`` 覆盖。
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_DATA_ROOT = _REPO_ROOT / "data_control"


def _data_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_root", None)
    if configured:
        return Path(configured)
    return _DEFAULT_DATA_ROOT


def _camel_key(key: str) -> str:
    """Convert one snake_case JSON key to camelCase."""
    head, *parts = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in parts)


_RAW_PERIODS = ("1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo", "1q", "1y")
_MINUTE_DERIVATIVES = {"15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
_MINUTE_SOURCES = ("1m", "5m", "15m", "30m")
_DAILY_DERIVATIVES = ("1w", "1mo", "1q", "3mo", "6mo", "1y")
_PERIOD_ORDER = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "1q", "3mo", "6mo", "1y")
_SOURCE_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
_DAILY_WINDOW_FACTORS = {"1w": 6, "1mo": 24, "1q": 70, "3mo": 70, "6mo": 140, "1y": 270}
_MARKET_CAP_CACHE: dict[str, dict[str, tuple[float | None, float | None]]] = {}
_CONTINUOUS_LABELS = {"SECONDARY": "次连", "MAIN": "主连", "WEIGHTED": "加权"}
# The v3 revision changes the public market taxonomy and removes the
# unclassified review section from the market page.  Persisted v2 category
# payloads must not leak the old overlapping labels into the new UI.
_PRESENTATION_SCHEMA_VERSION = "market-categories-r4-v3"
_TDX_SECURITY_NAMES_PATH = Path(__file__).resolve().parents[1] / "config" / "tdx_security_names.json"
_TDX_COMMODITY_INDEX_NAMES_PATH = Path(__file__).resolve().parents[1] / "config" / "tdx_commodity_index_names.json"
_TDX_SECURITY_NAMES: dict[str, str] | None = None
_TDX_COMMODITY_INDEX_NAMES: dict[str, str] | None = None
_TDX_SECURITY_NAMES_STAMP: tuple[int, int] | None = None
_TDX_COMMODITY_INDEX_NAMES_STAMP: tuple[int, int] | None = None


def _tdx_security_names() -> dict[str, str]:
    global _TDX_SECURITY_NAMES, _TDX_SECURITY_NAMES_STAMP
    try:
        stat = _TDX_SECURITY_NAMES_PATH.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if _TDX_SECURITY_NAMES is None or stamp != _TDX_SECURITY_NAMES_STAMP:
        try:
            payload = json.loads(_TDX_SECURITY_NAMES_PATH.read_text(encoding="utf-8"))
            _TDX_SECURITY_NAMES = {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            _TDX_SECURITY_NAMES = {}
        _TDX_SECURITY_NAMES_STAMP = stamp
    return _TDX_SECURITY_NAMES


def _tdx_commodity_index_names() -> dict[str, str]:
    global _TDX_COMMODITY_INDEX_NAMES, _TDX_COMMODITY_INDEX_NAMES_STAMP
    try:
        stat = _TDX_COMMODITY_INDEX_NAMES_PATH.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if _TDX_COMMODITY_INDEX_NAMES is None or stamp != _TDX_COMMODITY_INDEX_NAMES_STAMP:
        try:
            payload = json.loads(_TDX_COMMODITY_INDEX_NAMES_PATH.read_text(encoding="utf-8"))
            _TDX_COMMODITY_INDEX_NAMES = {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            _TDX_COMMODITY_INDEX_NAMES = {}
        _TDX_COMMODITY_INDEX_NAMES_STAMP = stamp
    return _TDX_COMMODITY_INDEX_NAMES


def _response_data_version(data_root: Path) -> str:
    """Invalidate persisted UI payloads when display semantics change."""

    return f"{market_data_version(data_root)}:{_PRESENTATION_SCHEMA_VERSION}"


def _session_rule(bar: dict[str, Any]) -> str | None:
    market = str(bar.get("market") or "").upper()
    asset_type = str(bar.get("asset_type") or "").upper()
    if market == "HK":
        return "HK_STOCK"
    if market == "CN" and asset_type == "FUTURE":
        product = str(bar.get("product_code") or bar.get("productCode") or "").upper()
        if product in {"AU", "AG", "CU", "AL", "ZN", "PB", "NI", "SN", "SS", "SC", "BC", "LU", "NR"}:
            return "CN_FUTURE_0230"
        if product in {"A", "B", "C", "CS", "I", "J", "JM", "L", "M", "P", "PP", "V", "Y"}:
            return "CN_FUTURE_0100"
        return "CN_FUTURE"
    if market == "CN" and asset_type in {
        "STOCK", "ETF", "INDEX", "CONVERTIBLE_BOND", "EXCHANGEABLE_BOND", "PLEDGED_REPO", "REPO", "LOF", "REIT",
    }:
        return "CN_STOCK"
    return None


def _bar_with_close_time(bar: dict[str, Any]) -> dict[str, Any]:
    """Make older Silver bars usable by the aggregation contract without guessing OHLC."""
    if bar.get("bar_close_time"):
        return bar
    period = str(bar.get("period") or "")
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}.get(period)
    if minutes is None:
        return bar
    try:
        opened = datetime.fromisoformat(str(bar["bar_open_time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return bar
    result = dict(bar)
    result["bar_close_time"] = (opened + timedelta(minutes=minutes)).isoformat()
    return result


def _daily_bar_for_aggregate(bar: dict[str, Any]) -> dict[str, Any]:
    """Bridge legacy Silver names for a read-time weekly/monthly projection."""
    result = dict(bar)
    result.setdefault("trading_day", result.get("trading_date") or str(result.get("bar_open_time") or "")[:10])
    # Old daily rows did not retain a close timestamp.  Preserve their source
    # timestamp rather than inventing an exchange-close time at read time.
    result.setdefault("bar_close_time", result.get("bar_open_time"))
    return result


def _raw_periods_for_instrument(data_root: Path, instrument_id: str) -> list[str]:
    actual = set(instrument_periods(data_root, instrument_id))
    return [period for period in _RAW_PERIODS if period in actual]


def _source_rank(item: dict[str, Any]) -> int:
    """Prefer local TDX when it is not materially older than its fallback."""
    source = str(item.get("actualSource") or item.get("source") or "")
    return 0 if source.startswith("通达信") else 1


def _as_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _logical_instruments(data_root: Path) -> dict[str, dict[str, Any]]:
    """Collapse source-isolated Silver rows into one public canonical instrument."""
    physical = load_inventory(data_root).instruments
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in physical.values():
        canonical = str(item.get("canonicalInstrumentId") or item["instrumentId"])
        groups.setdefault(canonical, []).append(dict(item))
    result: dict[str, dict[str, Any]] = {}
    for canonical, candidates in groups.items():
        latest = max((_as_datetime(item.get("lastBarAt")) for item in candidates), default=None)
        fresh: list[dict[str, Any]] = []
        for item in candidates:
            timestamp = _as_datetime(item.get("lastBarAt"))
            if latest is None or timestamp is None or latest - timestamp <= timedelta(days=1):
                fresh.append(item)
        chosen = sorted(fresh or candidates, key=lambda item: (_source_rank(item), str(item.get("lastBarAt") or "")), reverse=False)[0]
        # ``sorted`` above selects TDX first.  A candidate older by more than a
        # day is excluded, so the AKShare fallback wins when the local client is stale.
        logical = dict(chosen)
        logical["instrumentId"] = canonical
        logical["storageInstrumentId"] = chosen["instrumentId"]
        logical["actualSource"] = chosen.get("actualSource") or chosen.get("source")
        logical["latestTimeText"] = beijing_text(chosen.get("lastBarAt"))
        logical["sourceCandidates"] = [str(chosen["instrumentId"]), *[
            str(item["instrumentId"]) for item in candidates if item["instrumentId"] != chosen["instrumentId"]
        ]]
        if latest and _as_datetime(chosen.get("lastBarAt")) and latest - _as_datetime(chosen.get("lastBarAt")) > timedelta(days=1):
            logical["availability"] = "数据滞后"
        result[canonical] = logical
    catalog = load_json(data_root / "state" / "futures_catalog.json", {})
    for item in catalog.get("unsupported", []) if isinstance(catalog, dict) else []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonicalInstrumentId") or "")
        if canonical and canonical not in result:
            result[canonical] = {"instrumentId": canonical, "storageInstrumentId": "", "symbol": str(item.get("symbol") or ""),
                                 "name": str(item.get("name") or ""), "market": str(item.get("market") or ""),
                                 "assetType": str(item.get("assetType") or ""), "period": "", "seriesKind": str(item.get("seriesKind") or ""),
                                 "availability": str(item.get("availability") or "暂不支持"), "unsupportedReason": item.get("unsupportedReason"),
                                 "actualSource": "", "latestTimeText": None, "sourceCandidates": []}
    return result


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _market_caps(data_root: Path) -> dict[str, tuple[float | None, float | None]]:
    """Return latest local F10 market caps in 亿元, keyed by logical ID."""
    paths = (data_root / "industry" / "f10" / "cn_f10.jsonl", data_root / "industry" / "f10" / "hk_f10.jsonl")
    fingerprint = "|".join(
        f"{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}" for path in paths if path.is_file()
    )
    if fingerprint in _MARKET_CAP_CACHE:
        return _MARKET_CAP_CACHE[fingerprint]
    values: dict[str, tuple[float | None, float | None]] = {}
    try:
        repository = CompanyRepository(data_root)
        page = 1
        while True:
            companies = repository.list_companies(page=page, page_size=500)
            for company in companies.items:
                values[company.instrument_key] = (
                    company.total_market_cap.value / 100_000_000 if company.total_market_cap else None,
                    company.float_market_cap.value / 100_000_000 if company.float_market_cap else None,
                )
            if page * companies.page_size >= companies.total:
                break
            page += 1
    except Exception:
        values = {}
    _MARKET_CAP_CACHE.clear()
    _MARKET_CAP_CACHE[fingerprint] = values
    return values


def _capital_deposit(
    bar: dict[str, Any],
    instrument: dict[str, Any] | None = None,
) -> tuple[float | None, str | None, dict[str, Any]]:
    """Calculate a futures capital deposit only when all real inputs exist."""
    oi = _number(bar.get("open_interest"))
    close = _number(bar.get("close"))
    multiplier = _number(bar.get("contract_multiplier") or bar.get("contractMultiplier"))
    margin = _number(bar.get("margin_rate") or bar.get("marginRate"))
    trace: dict[str, Any] = {}
    if instrument:
        multiplier = multiplier or _number(instrument.get("contractMultiplier"))
        margin = margin or _number(instrument.get("marginRate"))
    day_text = str(bar.get("trading_day") or bar.get("trading_date") or (instrument or {}).get("lastBarAt") or "")[:10]
    try:
        trading_day = date.fromisoformat(day_text)
    except ValueError:
        trading_day = None
    if (multiplier is None or margin is None) and trading_day:
        resolution = resolve_futures_contract_spec(
            str((instrument or {}).get("productCode") or bar.get("product_code") or bar.get("productCode") or ""),
            str((instrument or {}).get("exchange") or bar.get("exchange") or ""),
            trading_day,
        )
        if resolution.spec:
            multiplier = multiplier or resolution.spec.contract_multiplier
            margin = margin or resolution.spec.margin_rate
            trace = {
                "contract_multiplier": multiplier,
                "margin_rate": margin,
                "contract_spec_effective_from": (
                    resolution.spec.effective_from.isoformat() if resolution.spec.effective_from else None
                ),
                "contract_spec_source": resolution.spec.source,
                "capital_deposit_formula_version": "2026-01-v1",
            }
        elif resolution.reason:
            trace["contract_spec_reason"] = resolution.reason
    if oi is None:
        return None, "缺少持仓量", trace
    if close is None:
        return None, "缺少收盘价", trace
    if multiplier is None:
        return None, str(trace.get("contract_spec_reason") or "缺少合约乘数"), trace
    if margin is None:
        return None, str(trace.get("contract_spec_reason") or "缺少保证金率"), trace
    trace.setdefault("contract_multiplier", multiplier)
    trace.setdefault("margin_rate", margin)
    trace.setdefault("capital_deposit_formula_version", "2026-01-v1")
    try:
        return compute_future_capital_deposit(
            instrument_id=str((instrument or {}).get("instrumentId") or bar.get("instrument_id") or ""),
            trading_day=trading_day or date.fromisoformat(day_text),
            open_interest=oi,
            price=close,
            contract_multiplier=multiplier,
            margin_rate=margin,
        ).value, None, trace
    except (TypeError, ValueError) as error:
        return None, str(error), trace


def _enrich_bars(bars: list[dict[str, Any]], instrument: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expose raw bar fields together with deterministic local derivations."""
    previous_close: float | None = None
    result: list[dict[str, Any]] = []
    for source in bars:
        bar = dict(source)
        close = _number(bar.get("close"))
        high = _number(bar.get("high") or bar.get("highest"))
        low = _number(bar.get("low") or bar.get("lowest"))
        if "high" not in bar and high is not None:
            bar["high"] = high
        if "low" not in bar and low is not None:
            bar["low"] = low
        if previous_close is not None and close is not None:
            change = close - previous_close
            bar["change"] = change
            bar["pct_change"] = change / previous_close * 100 if previous_close else None
            bar["amplitude"] = (high - low) / previous_close * 100 if high is not None and low is not None and previous_close else None
        else:
            bar.setdefault("change", None)
            bar.setdefault("pct_change", None)
            bar.setdefault("amplitude", None)
        if bar.get("turnover_rate") is None and bar.get("turnoverRate") is not None:
            bar["turnover_rate"] = bar.get("turnoverRate")
        asset_type = str((instrument or {}).get("assetType") or bar.get("asset_type") or "").upper()
        series_kind = str((instrument or {}).get("seriesKind") or bar.get("series_kind") or "").upper()
        if asset_type == "FUTURE" or series_kind == "COMMODITY_INDEX":
            capital, reason, trace = _capital_deposit(bar, instrument)
            bar["capital_deposit"] = capital
            bar["capital_deposit_reason"] = reason
            bar.update(trace)
        if close is not None:
            previous_close = close
        result.append(bar)
    return result


def _normalize_future_name(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    series_kind = str(item.get("seriesKind") or "").upper()
    if str(item.get("assetType") or "").upper() != "FUTURE":
        return result
    product = str(item.get("productCode") or "").upper()
    configured_name = futures_product_name(product, str(item.get("exchange") or ""))
    if series_kind in _CONTINUOUS_LABELS:
        base = re.sub(
            r"(?:指数|主力连续|主力|主连|次连|加权)$",
            "",
            str(configured_name or item.get("name") or product or item.get("symbol") or "").strip(),
        )
        result["name"] = f"{base}{_CONTINUOUS_LABELS[series_kind]}"
    elif series_kind == "CONTRACT" and configured_name:
        symbol = str(item.get("symbol") or item.get("sourceSymbol") or "").upper()
        delivery = symbol[len(product) :] if product and symbol.startswith(product) else ""
        result["name"] = f"{configured_name}{delivery}"
    return result


_TDX_EXCHANGE_PREFIX = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}


def _normalize_tdx_instrument(item: dict[str, Any]) -> dict[str, Any]:
    """Repair legacy TongdaXin rows whose asset type or name predates R3 rules."""
    source = str(item.get("actualSource") or item.get("source") or "")
    result = dict(item)
    exchange = str(item.get("exchange") or "").upper()
    symbol = str(item.get("symbol") or "").upper()
    asset_type = str(item.get("assetType") or "").upper()
    if "通达信" in source:
        if asset_type != "FUTURE" and exchange in _TDX_EXCHANGE_PREFIX and symbol:
            inferred_type, series_kind, _exchange = _cn_classification(_TDX_EXCHANGE_PREFIX[exchange], symbol)
            if inferred_type and inferred_type != asset_type:
                result["assetType"] = inferred_type
            if series_kind and not item.get("seriesKind"):
                result["seriesKind"] = series_kind
        name = _tdx_security_names().get(f"{exchange}.{symbol}") if exchange and symbol else None
        if not name and str(item.get("seriesKind") or "").upper() == "COMMODITY_INDEX":
            name = _tdx_commodity_index_names().get(symbol)
        if name:
            result["name"] = name
    return result


def _quote_item(item: dict[str, Any], caps: dict[str, tuple[float | None, float | None]]) -> dict[str, Any]:
    result = _normalize_tdx_instrument(_normalize_future_name(item))
    total_cap, float_cap = caps.get(str(item.get("instrumentId") or ""), (None, None))
    result["latestPrice"] = item.get("lastClose")
    result["totalMarketCap"] = total_cap
    result["floatMarketCap"] = float_cap
    result["openInterest"] = item.get("lastOpenInterest")
    capital, reason, trace = _capital_deposit(
        {
            "open_interest": item.get("lastOpenInterest"),
            "close": item.get("lastClose"),
            "contract_multiplier": item.get("contractMultiplier"),
            "margin_rate": item.get("marginRate"),
        },
        item,
    )
    result["capitalDeposit"] = capital
    result["capitalDepositReason"] = reason
    result.update({_camel_key(key): value for key, value in trace.items()})
    result["nightSession"] = night_session(item)
    return result


def _beijing_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _last_trading_day(item: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(item.get("lastBarAt") or "")[:10])
    except ValueError:
        return None


def _is_expired_contract_item(item: dict[str, Any], *, reference_day: date | None = None) -> bool:
    if str(item.get("seriesKind") or "").upper() != "CONTRACT":
        return False
    return is_expired_futures_contract(
        str(item.get("symbol") or item.get("sourceSymbol") or ""),
        str(item.get("exchange") or ""),
        reference_day=reference_day or _beijing_today(),
        last_trading_day=_last_trading_day(item),
    )


def _available_periods(data_root: Path, instrument_id: str) -> list[str]:
    raw = _raw_periods_for_instrument(data_root, instrument_id)
    available = set(raw)
    if "1d" in raw:
        available.update(_DAILY_DERIVATIVES)
    minute_source = next((period for period in _MINUTE_SOURCES if period in raw), None)
    if minute_source:
        probe = read_bars(data_root, instrument_id, period=minute_source, limit=1)
        if probe and _session_rule(probe[0]):
            available.update(period for period, minutes in _MINUTE_DERIVATIVES.items() if minutes >= {"1m": 1, "5m": 5, "15m": 15, "30m": 30}[minute_source])
    return [period for period in _PERIOD_ORDER if period in available]


def _all_raw_bars(data_root: Path, instrument_id: str, period: str) -> list[dict[str, Any]]:
    """Load a complete local source period in chunks for derived periods."""
    start = 0
    output: list[dict[str, Any]] = []
    while True:
        chunk, total = read_bars_window(data_root, instrument_id, period=period, start=start, limit=5_000)
        output.extend(chunk)
        start += len(chunk)
        if not chunk or start >= total:
            return output


def _derived_bars(data_root: Path, instrument_id: str, period: str) -> list[dict[str, Any]]:
    cache = get_kline_query_store(data_root)
    cached = cache.get_derived(instrument_id, period)
    if cached is not None:
        return cached
    if period in _DAILY_DERIVATIVES:
        daily = _all_raw_bars(data_root, instrument_id, "1d")
        normalized = [_daily_bar_for_aggregate(bar) for bar in daily]
        result = aggregate_daily_bars(normalized, period) if normalized else []
        cache.put_derived(instrument_id, period, result)
        return result
    minutes = _MINUTE_DERIVATIVES.get(period)
    if minutes is None:
        return []
    source = next(
        (candidate for candidate in _MINUTE_SOURCES if read_bars(data_root, instrument_id, period=candidate, limit=1)),
        None,
    )
    if source is None:
        return []
    bars = [_bar_with_close_time(bar) for bar in _all_raw_bars(data_root, instrument_id, source)]
    if not bars:
        return []
    rule = _session_rule(bars[0])
    result = aggregate_bars(bars, minutes, rule) if rule else []
    cache.put_derived(instrument_id, period, result)
    return result


def _source_window_before(
    data_root: Path,
    instrument_id: str,
    period: str,
    *,
    before: str | None,
    target_rows: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Read a bounded raw window in keyset pages, newest rows retained."""

    output: list[dict[str, Any]] = []
    cursor = before
    total = 0
    has_more = True
    remaining = max(1, target_rows)
    while remaining > 0 and has_more:
        window = read_bars_before(
            data_root,
            instrument_id,
            period=period,
            before=cursor,
            limit=min(5_000, remaining),
        )
        total = window.total
        if not window.bars:
            has_more = False
            break
        output = [*window.bars, *output]
        remaining -= len(window.bars)
        has_more = window.has_more
        if not window.before or window.before == cursor:
            has_more = False
            break
        cursor = window.before
    return output, total, has_more


def _derived_before(
    data_root: Path,
    instrument_id: str,
    period: str,
    *,
    before: str | None,
    size: int,
) -> tuple[list[dict[str, Any]], int, str | None, bool, str | None, str | None]:
    """Project only the requested derived viewport instead of all history."""

    if period in _DAILY_DERIVATIVES:
        source = "1d"
        factor = _DAILY_WINDOW_FACTORS[period]
        raw, raw_total, source_has_more = _source_window_before(
            data_root,
            instrument_id,
            source,
            before=before,
            target_rows=max(64, (size + 2) * factor),
        )
        projected = aggregate_daily_bars(
            [_daily_bar_for_aggregate(bar) for bar in raw], period
        ) if raw else []
    else:
        target_minutes = _MINUTE_DERIVATIVES.get(period)
        raw_periods = _raw_periods_for_instrument(data_root, instrument_id)
        source = next((candidate for candidate in _MINUTE_SOURCES if candidate in raw_periods), "")
        if target_minutes is None or not source:
            return [], 0, before, False, None, None
        factor = max(1, target_minutes // _SOURCE_MINUTES[source])
        raw, raw_total, source_has_more = _source_window_before(
            data_root,
            instrument_id,
            source,
            before=before,
            target_rows=max(128, (size + 4) * factor),
        )
        normalized = [_bar_with_close_time(bar) for bar in raw]
        rule = _session_rule(normalized[0]) if normalized else None
        projected = aggregate_bars(normalized, target_minutes, rule) if rule else []

    eligible = [
        bar for bar in projected
        if before is None or str(bar.get("bar_open_time") or "") < before
    ]
    selected = eligible[-size:]
    has_more = source_has_more or len(eligible) > len(selected)
    estimated_total = max(len(selected), (raw_total + factor - 1) // factor)
    cursor = str(selected[0].get("bar_open_time") or before) if selected else before
    _source_total, lower, upper = bar_bounds(data_root, instrument_id, source)
    return selected, estimated_total, cursor, has_more, lower, upper


def _overview(data_root: Path) -> dict[str, Any]:
    """Build the market overview from the silver inventory index."""
    inventory = load_inventory(data_root)
    markets: dict[str, int] = {}
    asset_types: dict[str, int] = {}
    for item in inventory.instruments.values():
        market = str(item.get("market") or "")
        asset_type = str(item.get("assetType") or "")
        markets[market] = markets.get(market, 0) + 1
        asset_types[asset_type] = asset_types.get(asset_type, 0) + 1
    return {
        "generatedAt": inventory.generated_at,
        "instruments": len(inventory.instruments),
        "rows": inventory.rows,
        "markets": markets,
        "assetTypes": asset_types,
        "periods": list(inventory.periods),
        "latestBarAt": inventory.latest_bar_at,
    }


@router.get("/overview")
def market_overview(request: Request) -> dict[str, Any]:
    return clean(_overview(_data_root(request)))


@router.get("/cache-status")
def market_cache_status(request: Request) -> dict[str, Any]:
    """Expose local K-line cache health for the desktop client and diagnostics."""

    data_root = _data_root(request)
    payload = get_kline_query_store(data_root).status().to_dict()
    payload["dataVersion"] = _response_data_version(data_root)
    return clean(payload)


@router.get("/groups")
def market_groups(request: Request) -> dict[str, Any]:
    """Return actual Silver coverage grouped for the client market view."""
    items = local_inventory(_data_root(request))
    return clean({"items": items, "total": len(items)})


@router.get("/categories")
def market_categories() -> dict[str, Any]:
    """Return the ordered, configuration-backed market filter contract."""
    items = market_category_options()
    return {"items": items, "total": len(items)}


def _silver_unclassified_items(data_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_item in _logical_instruments(data_root).values():
        item = _normalize_tdx_instrument(_normalize_future_name(source_item))
        if classify_market(item) != UNCLASSIFIED_CATEGORY:
            continue
        market = str(item.get("market") or "")
        exchange = str(item.get("exchange") or "")
        asset_type = str(item.get("assetType") or "")
        symbol = str(item.get("symbol") or item.get("sourceSymbol") or "")
        period = str(item.get("period") or "")
        items.append({
            "reviewId": f"silver:{item.get('instrumentId') or symbol}",
            "instrumentId": item.get("instrumentId"),
            "name": item.get("name"),
            "code": symbol,
            "sourceCode": item.get("sourceSymbol") or symbol,
            "marketPrefix": exchange,
            "latestClose": item.get("lastClose"),
            "lastBarAt": item.get("lastBarAt"),
            "pricePeriod": period or None,
            "periods": [period] if period else [],
            "sourceTerminal": item.get("actualSource") or item.get("source") or "Silver 标准库",
            "origin": "SILVER_UNCLASSIFIED",
            "classificationStatus": "PENDING_REVIEW",
            "reason": (
                "标准标的字段未命中已登记分类规则"
                f"（market={market or '-'}, exchange={exchange or '-'}, assetType={asset_type or '-'}）"
            ),
        })
    return items


@router.get("/unclassified")
def market_unclassified(
    request: Request,
    q: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, alias="pageSize"),
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the single review queue for unclassified Silver rows and raw TDX files."""
    data_root = _data_root(request)
    financial_root = getattr(request.app.state, "tdx_root", None)
    futures_root = getattr(request.app.state, "tdx_futures_root", None)
    items = _silver_unclassified_items(data_root)
    items.extend(scan_unclassified_tdx(financial_root, futures_root, refresh=refresh))
    keyword = q.strip().casefold()
    if keyword:
        items = [
            item for item in items
            if any(
                keyword in str(item.get(field) or "").casefold()
                for field in ("name", "code", "sourceCode", "marketPrefix", "sourceTerminal", "reason")
            )
        ]
    items.sort(key=lambda item: (
        str(item.get("sourceTerminal") or ""),
        str(item.get("marketPrefix") or ""),
        str(item.get("code") or ""),
    ))
    payload = paginate(items, page, page_size)
    payload["dataVersion"] = _response_data_version(data_root)
    return clean(payload)


@router.get("/instruments")
def market_instruments(
    request: Request,
    market: str | None = None,
    asset_type: str | None = Query(default=None, alias="assetType"),
    series_kind: str | None = Query(default=None, alias="seriesKind"),
    category_key: str | None = Query(default=None, alias="categoryKey"),
    q: str = "",
    include_expired: bool = Query(default=False, alias="includeExpired"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, alias="pageSize"),
) -> dict[str, Any]:
    """Paginate the local instrument index, optionally filtered by market/q."""
    items = [
        _normalize_tdx_instrument(_normalize_future_name(item))
        for item in _logical_instruments(_data_root(request)).values()
    ]
    items = [item for item in items if classify_market(item) != UNCLASSIFIED_CATEGORY]
    if not include_expired:
        items = [item for item in items if not _is_expired_contract_item(item)]
    if category_key:
        items = [item for item in items if matches_market_category(item, category_key)]
    if market:
        wanted = market.strip().casefold()
        items = [item for item in items if str(item.get("market") or "").casefold() == wanted]
    if asset_type:
        wanted_type = asset_type.strip().casefold()
        items = [item for item in items if str(item.get("assetType") or "").casefold() == wanted_type]
    if series_kind:
        wanted_kinds = {value.strip().casefold() for value in series_kind.split(",") if value.strip()}
        items = [item for item in items if str(item.get("seriesKind") or "").casefold() in wanted_kinds]
    keyword = q.strip().casefold()
    if keyword:
        items = [
            item
            for item in items
            if any(
                keyword in str(item.get(field) or "").casefold()
                for field in ("instrumentId", "symbol", "name", "productCode", "sourceSymbol")
            )
        ]
    caps = _market_caps(_data_root(request))
    payload = paginate([_quote_item(item, caps) for item in items], page, page_size)
    payload["dataVersion"] = _response_data_version(_data_root(request))
    return clean(payload)


def _resolve_period(data_root: Path, instrument_id: str, period: str | None) -> tuple[dict[str, Any], str, list[str]]:
    instrument = _logical_instruments(data_root).get(instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="instrument not found")
    candidate_ids = [str(value) for value in instrument.get("sourceCandidates", []) if value]
    if not candidate_ids and instrument.get("storageInstrumentId"):
        candidate_ids = [str(instrument["storageInstrumentId"])]
    if not candidate_ids:
        raise HTTPException(status_code=400, detail=str(instrument.get("unsupportedReason") or "该标的暂不支持K线"))
    physical = load_inventory(data_root).instruments
    routes = [(candidate_id, _available_periods(data_root, candidate_id)) for candidate_id in candidate_ids]
    available_set = {value for _candidate_id, values in routes for value in values}
    available = [value for value in _PERIOD_ORDER if value in available_set]
    selected = period or str(instrument.get("period") or "1d")
    if selected not in available:
        raise HTTPException(status_code=400, detail=f"unknown period: {selected}")
    storage_id = next(candidate_id for candidate_id, values in routes if selected in values)
    routed = dict(instrument)
    routed["storageInstrumentId"] = storage_id
    selected_source = physical.get(storage_id, {})
    routed["actualSource"] = selected_source.get("actualSource") or selected_source.get("source") or instrument.get("actualSource")
    return routed, selected, available


def _history_window(
    data_root: Path,
    instrument: dict[str, Any],
    period: str,
    start: int,
    size: int,
) -> tuple[list[dict[str, Any]], int, str | None, str | None]:
    """Return one complete viewport plus its absolute total and range bounds."""
    storage_id = str(instrument["storageInstrumentId"])
    start = max(0, start)
    size = max(1, min(size, 5_000))
    total, lower, upper = bar_bounds(data_root, storage_id, period)
    raw, _raw_total = read_bars_window(data_root, storage_id, period=period, start=max(0, start - 1), limit=size + 1)
    # Some derived periods (for example 1h) are also valid raw storage
    # periods.  Prefer a real stored series, but derive it whenever no such
    # series exists; otherwise a 30m-only instrument advertises 1h yet returns
    # an empty chart.
    if total == 0:
        derived = _derived_bars(data_root, storage_id, period)
        total = len(derived)
        raw = derived[max(0, start - 1) : start + size]
        lower = derived[0].get("bar_open_time") if derived else None
        upper = derived[-1].get("bar_open_time") if derived else None
    enriched = _enrich_bars(raw, instrument)
    if start > 0 and enriched:
        enriched = enriched[1:]
    for bar in enriched:
        bar["instrument_id"] = instrument["instrumentId"]
        bar["actual_source"] = instrument.get("actualSource")
        bar["bar_time_text"] = beijing_text(bar.get("bar_open_time"))
    return enriched, total, lower, upper


def _history_before(
    data_root: Path,
    instrument: dict[str, Any],
    period: str,
    before: str | None,
    size: int,
) -> tuple[list[dict[str, Any]], int, str | None, bool, str | None, str | None]:
    """Read a raw history page by indexed time cursor and enrich it once."""

    storage_id = str(instrument["storageInstrumentId"])
    total, lower, upper = bar_bounds(data_root, storage_id, period)
    if total == 0:
        raw, total, cursor, has_more, lower, upper = _derived_before(
            data_root,
            storage_id,
            period,
            before=before,
            size=size,
        )
    else:
        window = read_bars_before(data_root, storage_id, period=period, before=before, limit=size)
        raw, total, cursor, has_more = window.bars, window.total, window.before, window.has_more
    enriched = _enrich_bars(raw, instrument)
    for bar in enriched:
        bar["instrument_id"] = instrument["instrumentId"]
        bar["actual_source"] = instrument.get("actualSource")
        bar["bar_time_text"] = beijing_text(bar.get("bar_open_time"))
    return enriched, total, cursor, has_more, lower, upper


@router.get("/instruments/{instrument_id}/bars/history")
def market_bars_history(
    instrument_id: str,
    request: Request,
    period: str | None = None,
    before: str | None = None,
    start: int = Query(default=0, ge=0),
    size: int = Query(default=60, ge=1, le=5_000),
) -> dict[str, Any]:
    """Fetch a logical history viewport; ``total`` covers all local bars."""
    data_root = _data_root(request)
    instrument, selected_period, available = _resolve_period(data_root, instrument_id, period)
    if before:
        bars, total, next_before, has_more, earliest, latest = _history_before(
            data_root, instrument, selected_period, before, size
        )
    else:
        bars, total, earliest, latest = _history_window(data_root, instrument, selected_period, start, size)
        next_before = str(bars[0].get("bar_open_time") or "") if bars else None
        has_more = start > 0
    return clean({
        "instrumentId": instrument_id,
        "period": selected_period,
        "availablePeriods": available,
        "start": start,
        "size": len(bars),
        "total": total,
        "before": next_before,
        "hasMore": has_more,
        "earliestBarAt": earliest,
        "latestBarAt": latest,
        "dataVersion": _response_data_version(data_root),
        "bars": [{_camel_key(key): value for key, value in bar.items()} for bar in bars],
    })


@router.get("/instruments/{instrument_id}/bars/meta")
def market_bars_meta(instrument_id: str, request: Request, period: str | None = None) -> dict[str, Any]:
    data_root = _data_root(request)
    instrument, selected_period, available = _resolve_period(data_root, instrument_id, period)
    _bars, total, _before, _has_more, earliest, latest = _history_before(
        data_root, instrument, selected_period, None, 1
    )
    return clean({"instrumentId": instrument_id, "period": selected_period, "availablePeriods": available,
                  "total": total, "earliestBarAt": earliest, "latestBarAt": latest,
                  "dataVersion": _response_data_version(data_root)})


def _chart_indicators(value: str) -> list[dict[str, str]]:
    names = [item.strip().lower() for item in value.split(",") if item.strip()]
    if len(names) > 12:
        raise HTTPException(status_code=422, detail="图形指标最多选择 12 项")
    return [{"id": name} for name in names]


@router.get("/instruments/{instrument_id}/chart")
def market_chart_bootstrap(
    instrument_id: str,
    request: Request,
    period: str | None = None,
    before: str | None = None,
    start: int | None = Query(default=None, ge=0),
    size: int = Query(default=60, ge=1, le=5_000),
    indicators: str = "volume",
) -> dict[str, Any]:
    """Return one chart viewport, its indicator series and drawings together."""

    data_root = _data_root(request)
    instrument, selected_period, available = _resolve_period(data_root, instrument_id, period)
    if start is None:
        bars, total, next_before, has_more, earliest, latest = _history_before(
            data_root, instrument, selected_period, before, size
        )
        range_start = max(0, total - len(bars)) if before is None else 0
    else:
        storage_id = str(instrument["storageInstrumentId"])
        total, earliest, latest = bar_bounds(data_root, storage_id, selected_period)
        if total == 0:
            total = len(_derived_bars(data_root, storage_id, selected_period))
        range_start = start
        bars, total, earliest, latest = _history_window(data_root, instrument, selected_period, range_start, size)
        next_before = str(bars[0].get("bar_open_time") or "") if bars else None
        has_more = range_start > 0
    camel_bars = [{_camel_key(key): value for key, value in bar.items()} for bar in bars]
    requested = _chart_indicators(indicators)
    series = _indicator_series(camel_bars, requested)
    drawings_payload = load_json(_drawings_path(data_root, instrument_id), {"items": []})
    drawing_items = drawings_payload.get("items", []) if isinstance(drawings_payload, dict) else []
    # The workbench needs the complete drawing document so editing one period
    # cannot accidentally erase drawings that belong only to another period.
    # Visibility is still determined client-side by ``crossPeriod``/``period``.
    drawings = [item for item in drawing_items if isinstance(item, dict)]
    return clean(
        {
            "instrumentId": instrument_id,
            "period": selected_period,
            "availablePeriods": available,
            "start": range_start,
            "size": len(camel_bars),
            "total": total,
            "before": next_before,
            "hasMore": has_more,
            "earliestBarAt": earliest,
            "latestBarAt": latest,
            "bars": camel_bars,
            "series": series,
            "drawings": drawings,
            "dataVersion": _response_data_version(data_root),
        }
    )


class IndicatorSeriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: str
    start: int = Field(default=0, ge=0)
    size: int = Field(default=60, ge=1, le=5_000)
    indicators: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


def _rolling_ma(values: list[float | None], lookback: int) -> list[float | None]:
    output: list[float | None] = []
    for index in range(len(values)):
        window = values[max(0, index - lookback + 1): index + 1]
        output.append(sum(window) / lookback if len(window) == lookback and all(value is not None for value in window) else None)
    return output


def _rolling_sd(values: list[float | None], lookback: int) -> list[float | None]:
    averages = _rolling_ma(values, lookback)
    output: list[float | None] = []
    for index, average in enumerate(averages):
        window = values[max(0, index - lookback + 1): index + 1]
        output.append(
            math.sqrt(sum((float(value) - average) ** 2 for value in window) / lookback)
            if average is not None and len(window) == lookback and all(value is not None for value in window) else None
        )
    return output


def _ema(values: list[float | None], lookback: int) -> list[float | None]:
    output: list[float | None] = []
    value: float | None = None
    ratio = 2 / (lookback + 1)
    for item in values:
        if item is None:
            output.append(None)
        else:
            value = float(item) if value is None else value + ratio * (float(item) - value)
            output.append(value)
    return output


def _indicator_series(bars: list[dict[str, Any]], requested: list[dict[str, Any]]) -> dict[str, list[float | None]]:
    close = [_number(bar.get("close")) for bar in bars]
    high = [_number(bar.get("high")) for bar in bars]
    low = [_number(bar.get("low")) for bar in bars]
    output: dict[str, list[float | None]] = {}
    for config in requested:
        indicator_id = str(config.get("id") or "").lower()
        lookback = int(config.get("lookback") or 20)
        if lookback < 1 or lookback > 2_000:
            raise HTTPException(status_code=422, detail="指标回看期必须在 1 至 2000 之间")
        if indicator_id == "ma":
            output["ma"] = _rolling_ma(close, lookback)
        elif indicator_id == "sd":
            output["sd"] = _rolling_sd(close, lookback)
        elif indicator_id == "bollinger":
            multiplier = float(config.get("multiplier") or 2)
            middle = _rolling_ma(close, lookback)
            deviation = _rolling_sd(close, lookback)
            output["bollingerMiddle"] = middle
            output["bollingerUpper"] = [value + multiplier * deviation[index] if value is not None and deviation[index] is not None else None for index, value in enumerate(middle)]
            output["bollingerLower"] = [value - multiplier * deviation[index] if value is not None and deviation[index] is not None else None for index, value in enumerate(middle)]
        elif indicator_id == "hsar":
            top_percent = float(config.get("topPercent") or 20)
            amount = max(1, math.ceil(lookback * top_percent / 100))
            resistance: list[float | None] = []
            support: list[float | None] = []
            for index in range(len(bars)):
                highs = [value for value in high[max(0, index - lookback + 1):index + 1] if value is not None]
                lows = [value for value in low[max(0, index - lookback + 1):index + 1] if value is not None]
                resistance.append(sum(sorted(highs, reverse=True)[:amount]) / amount if len(highs) == lookback else None)
                support.append(sum(sorted(lows, reverse=True)[:amount]) / amount if len(lows) == lookback else None)
            output["hsarResistance"] = resistance
            output["hsarSupport"] = support
        elif indicator_id == "atr":
            atr_lookback = int(config.get("atrLookback") or 14)
            center_lookback = int(config.get("centerLookback") or 20)
            multiplier = float(config.get("multiplier") or 2)
            true_range: list[float | None] = []
            previous: float | None = None
            for index in range(len(bars)):
                if high[index] is None or low[index] is None:
                    true_range.append(None)
                elif previous is None:
                    true_range.append(high[index] - low[index])
                else:
                    true_range.append(max(high[index] - low[index], abs(high[index] - previous), abs(low[index] - previous)))
                if close[index] is not None:
                    previous = close[index]
            center = _ema(close, center_lookback)
            atr = _rolling_ma(true_range, atr_lookback)
            output["atrMiddle"] = center
            output["atrUpper"] = [value + multiplier * atr[index] if value is not None and atr[index] is not None else None for index, value in enumerate(center)]
            output["atrLower"] = [value - multiplier * atr[index] if value is not None and atr[index] is not None else None for index, value in enumerate(center)]
        elif indicator_id == "volume":
            output["volume"] = [_number(bar.get("volume")) for bar in bars]
        elif indicator_id:
            raise HTTPException(status_code=422, detail=f"未知图形指标：{indicator_id}")
    return output


@router.post("/instruments/{instrument_id}/indicator-series")
def market_indicator_series(instrument_id: str, request: Request, body: IndicatorSeriesRequest) -> dict[str, Any]:
    data_root = _data_root(request)
    instrument, selected_period, _available = _resolve_period(data_root, instrument_id, body.period)
    warmup = max([int(item.get("lookback") or 20) for item in body.indicators] or [1]) + 2
    fetch_start = max(0, body.start - warmup)
    bars, _total, _earliest, _latest = _history_window(data_root, instrument, selected_period, fetch_start, body.size + warmup)
    series = _indicator_series([{_camel_key(key): value for key, value in bar.items()} for bar in bars], body.indicators)
    trim = body.start - fetch_start
    return clean({"instrumentId": instrument_id, "period": selected_period, "start": body.start,
                  "series": {key: values[trim:trim + body.size] for key, values in series.items()}})


class ChartDrawingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=2_000)


def _drawings_path(data_root: Path, instrument_id: str) -> Path:
    return data_root / "personal" / "chart_drawings" / f"{sha256(instrument_id.encode('utf-8')).hexdigest()}.json"


@router.get("/instruments/{instrument_id}/drawings")
def market_drawings(instrument_id: str, request: Request, period: str | None = None) -> dict[str, Any]:
    payload = load_json(_drawings_path(_data_root(request), instrument_id), {"items": []})
    items = payload.get("items", []) if isinstance(payload, dict) else []
    visible = [item for item in items if isinstance(item, dict) and (not period or item.get("crossPeriod") or item.get("period") == period)]
    return clean({"instrumentId": instrument_id, "items": visible, "updatedAt": payload.get("updatedAt") if isinstance(payload, dict) else None})


@router.get("/drawings/batch")
def market_drawings_batch(
    request: Request,
    instrument_ids: str = Query(alias="instrumentIds", min_length=1, max_length=20_000),
    period: str | None = None,
) -> dict[str, Any]:
    """Return saved drawings for a bounded card-page instrument batch."""

    identifiers = list(dict.fromkeys(value.strip() for value in instrument_ids.split(",") if value.strip()))
    if len(identifiers) > 100:
        raise HTTPException(status_code=400, detail="单次最多读取 100 个标的的画线")
    items: dict[str, list[dict[str, Any]]] = {}
    for instrument_id in identifiers:
        payload = load_json(_drawings_path(_data_root(request), instrument_id), {"items": []})
        drawings = payload.get("items", []) if isinstance(payload, dict) else []
        items[instrument_id] = [
            item
            for item in drawings
            if isinstance(item, dict) and (not period or item.get("crossPeriod") or item.get("period") == period)
        ]
    return clean({"items": items, "total": len(items)})


@router.put("/instruments/{instrument_id}/drawings")
def market_save_drawings(instrument_id: str, request: Request, body: ChartDrawingsRequest) -> dict[str, Any]:
    if len(json.dumps(body.items, ensure_ascii=False, separators=(",", ":"))) > 1_000_000:
        raise HTTPException(status_code=422, detail="画线文档超过 1 MB 限制")
    for item in body.items:
        if item.get("type") != "brush":
            continue
        points = item.get("points")
        if not isinstance(points, list) or not 2 <= len(points) <= 2_048:
            raise HTTPException(status_code=422, detail="笔刷必须包含 2 至 2048 个点")
        for point in points:
            if not isinstance(point, dict) or not isinstance(point.get("time"), str) or not point["time"].strip():
                raise HTTPException(status_code=422, detail="笔刷点必须包含有效时间")
            price = point.get("price")
            if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price):
                raise HTTPException(status_code=422, detail="笔刷点价格必须为有限数值")
    payload = {"schemaVersion": 1, "instrumentId": instrument_id, "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"), "items": body.items}
    save_json(_drawings_path(_data_root(request), instrument_id), payload)
    return clean(payload)


class DrawingsDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_ids: list[str] = Field(default_factory=list, alias="instrumentIds", max_length=2_000)


@router.get("/drawings/index")
def market_drawings_index(request: Request) -> dict[str, Any]:
    """List every instrument that currently owns saved chart drawings."""
    data_root = _data_root(request)
    directory = data_root / "personal" / "chart_drawings"
    instruments = _logical_instruments(data_root)
    items: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            payload = load_json(path, {})
            if not isinstance(payload, dict):
                continue
            instrument_id = str(payload.get("instrumentId") or "").strip()
            raw_items = payload.get("items", [])
            count = sum(1 for item in raw_items if isinstance(item, dict))
            if not instrument_id or count == 0:
                continue
            updated_at = payload.get("updatedAt")
            if not updated_at:
                try:
                    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
                except OSError:
                    updated_at = None
            logical = instruments.get(instrument_id, {})
            items.append({
                "instrumentId": instrument_id,
                "symbol": str(logical.get("symbol") or ""),
                "name": str(logical.get("name") or ""),
                "count": count,
                "updatedAt": updated_at,
            })
    items.sort(key=lambda item: (item["name"] or item["instrumentId"]).casefold())
    return clean({"items": items, "total": len(items)})


@router.delete("/drawings")
def market_delete_drawings(request: Request, body: DrawingsDeleteRequest) -> dict[str, Any]:
    """Delete all drawings for the selected instruments."""
    data_root = _data_root(request)
    deleted = 0
    for instrument_id in dict.fromkeys(value for value in body.instrument_ids if value.strip()):
        try:
            _drawings_path(data_root, instrument_id).unlink()
            deleted += 1
        except FileNotFoundError:
            continue
    return {"deleted": deleted}


@router.get("/instruments/bars/batch")
def market_bars_batch(
    request: Request,
    instrument_ids: str = Query(alias="instrumentIds"),
    period: str = "1d",
    limit: int = Query(default=60, ge=1, le=120),
) -> dict[str, Any]:
    """Load recent K-lines for the currently visible quote cards in one call."""

    requested = list(dict.fromkeys(item.strip() for item in instrument_ids.split(",") if item.strip()))
    if not requested:
        raise HTTPException(status_code=422, detail="至少需要一个标的")
    if len(requested) > 48:
        raise HTTPException(status_code=422, detail="一次最多加载 48 个标的缩略图")
    data_root = _data_root(request)
    logical = _logical_instruments(data_root)
    items: dict[str, list[dict[str, Any]]] = {}
    if period in _RAW_PERIODS:
        try:
            candidate_ids = [
                str(candidate_id)
                for instrument_id in requested
                for candidate_id in logical.get(instrument_id, {}).get("sourceCandidates", [])
                if candidate_id
            ]
            store = get_kline_query_store(data_root)
            period_map = store.periods_many(candidate_ids)
            physical_inventory = load_inventory(data_root).instruments
            routed: dict[str, dict[str, Any]] = {}
            for instrument_id in requested:
                instrument = logical.get(instrument_id)
                if instrument is None:
                    continue
                candidates = [str(value) for value in instrument.get("sourceCandidates", []) if value]
                storage_id = next((value for value in candidates if period in period_map.get(value, [])), "")
                if not storage_id:
                    continue
                selected = dict(instrument)
                selected["storageInstrumentId"] = storage_id
                source = physical_inventory.get(storage_id, {})
                selected["actualSource"] = source.get("actualSource") or source.get("source") or instrument.get("actualSource")
                routed[instrument_id] = selected
            tails = store.read_tails(
                [str(instrument["storageInstrumentId"]) for instrument in routed.values()], period, limit
            )
            for instrument_id in requested:
                instrument = routed.get(instrument_id)
                storage_id = str(instrument.get("storageInstrumentId") or "") if instrument else ""
                raw = tails.get(storage_id, [])
                enriched = _enrich_bars(raw, instrument) if instrument else []
                for bar in enriched:
                    bar["instrument_id"] = instrument_id
                    bar["actual_source"] = instrument.get("actualSource") if instrument else None
                    bar["bar_time_text"] = beijing_text(bar.get("bar_open_time"))
                items[instrument_id] = [
                    {_camel_key(key): value for key, value in bar.items()} for bar in enriched
                ]
            return clean({"period": period, "limit": limit, "items": items, "dataVersion": _response_data_version(data_root)})
        except Exception:
            # Keep the per-instrument path as a resilience fallback for a
            # missing/damaged query cache.
            items = {}
    for instrument_id in requested:
        instrument = logical.get(instrument_id)
        if instrument is None or not instrument.get("storageInstrumentId"):
            items[instrument_id] = []
            continue
        storage_id = str(instrument["storageInstrumentId"])
        selected_period = period if period in _available_periods(data_root, storage_id) else ""
        if not selected_period:
            items[instrument_id] = []
            continue
        bars, _total, _before, _has_more, _lower, _upper = _history_before(
            data_root, instrument, selected_period, None, limit
        )
        items[instrument_id] = [{_camel_key(key): value for key, value in bar.items()} for bar in bars]
    return clean({"period": period, "limit": limit, "items": items, "dataVersion": _response_data_version(data_root)})


@router.get("/instruments/{instrument_id}/bars")
def market_bars(
    instrument_id: str,
    request: Request,
    period: str | None = None,
    limit: int = Query(default=1000, ge=1, le=MAX_BARS),
) -> dict[str, Any]:
    """Return ascending K-line bars for one local instrument."""
    data_root = _data_root(request)
    instrument, selected_period, available_periods = _resolve_period(data_root, instrument_id, period)
    bars, total, before, has_more, _earliest, _latest = _history_before(
        data_root, instrument, selected_period, None, limit
    )
    start = max(0, total - len(bars))
    camel_bars = [{_camel_key(key): value for key, value in bar.items()} for bar in bars]
    return clean(
        {
            "instrumentId": instrument_id,
            "period": selected_period,
            "availablePeriods": available_periods,
            "bars": camel_bars,
            "total": len(camel_bars),
            "historyTotal": total,
            "start": start,
            "size": len(camel_bars),
            "before": before,
            "hasMore": has_more,
            "lastBarAt": camel_bars[-1].get("barOpenTime") if camel_bars else None,
            "actualSource": instrument.get("actualSource"),
            "availability": instrument.get("availability"),
            "dataVersion": _response_data_version(data_root),
        }
    )


__all__ = ("router",)
