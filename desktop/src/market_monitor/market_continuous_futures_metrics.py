"""Project TDX continuous quotes through same-day TDX contract months."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import date
import math
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Sequence

from .futures_calendar import load_futures_trading_calendar
from .market_data_version import market_data_version
from .market_list_metrics import finite_number
from .market_official_futures_metrics import financial_metrics_version, missing_metrics, official_metrics
from .market_tdx_futures_projection import (
    FORMULA_VERSION, SOURCE_POLICY_VERSION, apply_tdx_daily_changes, official_multiplier, project_contract_day,
)

_CACHE: OrderedDict[tuple[str, str, str, str, str, str, str], dict[str, list[dict[str, Any]]]] = OrderedDict()
_LOCK = threading.RLock()
_MONEY_KEYS = ("notionalValue", "amount", "capitalDeposit")
_SIDE_MONEY_KEYS = ("capitalDepositLong", "capitalDepositShort")
_FACT_KEYS = (*_MONEY_KEYS, "specMarginRateLong", "specMarginRateShort", "marginRateUsed",
              *_SIDE_MONEY_KEYS, "capitalDepositBasis",
              "marginSourceKind", "marginSourceUrl",
              "marginPublishedOn", "marginCarriedForward",
              "marginRate",
              "officialMetricsDate", "officialMetricsStatus", "officialMetricsSource",
              "capitalDepositReason", "capitalDepositFormulaVersion", "contractMultiplier",
              "officialPayloadSha256", "marginPayloadSha256", "sourcePolicyVersion",
              "fieldSources", "comparisonStatus", "comparisonFields", "comparisonIssues",
              "tdxSourceInstrumentId", "tdxQualityStatus", "financialMetricsDate")


def is_continuous_future(item: Mapping[str, Any]) -> bool:
    return str(item.get("assetType") or "").upper() == "FUTURE" and str(
        item.get("seriesKind") or "").upper() in {"MAIN", "SECONDARY", "WEIGHTED"} and str(
        item.get("exchange") or "").upper() in {"SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"}


def continuous_product_code(item: Mapping[str, Any]) -> str:
    """Repair legacy metadata without merging monthly averages into ordinary futures."""
    product = str(item.get("productCode") or "").upper()
    if str(item.get("exchange") or "").upper() == "DCE":
        symbol = str(item.get("symbol") or item.get("sourceSymbol") or "").upper()
        match = re.fullmatch(r"(L|PP|V)-FL[789]", symbol)
        if match:
            return match[1] + "-F"
    return product


def trading_day(bar: Mapping[str, Any]) -> str:
    return str(bar.get("trading_day") or bar.get("trading_date") or bar.get("bar_open_time")
               or bar.get("dailyAsOf") or "")[:10]


def _field(row: Mapping[str, Any], field: str) -> float | None:
    aliases = {"open_interest": "openInterest"}
    return finite_number(row.get(field, row.get(aliases.get(field, field))))


def _same_fields(left: Mapping[str, Any], right: Mapping[str, Any], fields: Sequence[str]) -> bool:
    values = [(_field(left, field), _field(right, field)) for field in fields]
    return bool(_field(left, "close") and _field(left, "close") > 0 and all(
        a is not None and b is not None and math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-5)
        for a, b in values))


def match_tdx_month(bar: Mapping[str, Any], months: Sequence[Mapping[str, Any]],
                    tdx_months: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Price equality alone is insufficient, including inactive flat contracts."""
    matched = []
    for row in months:
        if row.get("financialMetricsDate", row.get("officialMetricsDate")) != trading_day(bar):
            continue
        if _same_fields(bar, row, ("open", "high", "low", "close", "volume", "open_interest")):
            matched.append(row)
    if len(matched) != 1:
        return {**missing_metrics(), "underlyingContractCode": None,
                "mappingStatus": "AMBIGUOUS" if matched else "UNMATCHED",
                "mappingCandidateContracts": [row["contractCode"] for row in matched],
                "capitalDepositReason": "通达信同日主连与月份合约未取得唯一行情匹配"}
    row = matched[0]
    return {**{key: row.get(key) for key in _FACT_KEYS},
            "underlyingContractCode": row["contractCode"], "mappingStatus": "MATCHED",
            "mappingSource": "TDX_SAME_DAY_MONTH_OHLC_VOLUME_OI",
            "mappingAsOfDate": trading_day(bar),
            "mappingSourceInstrumentId": row.get("tdxSourceInstrumentId"),
            "mappingRevisedFields": {},
            "mappingCandidateContracts": [row["contractCode"]]}


def weighted_totals(months: Sequence[Mapping[str, Any]], day: str) -> dict[str, Any]:
    """Sum contract-specific money; never multiply weighted price by one margin."""
    if not months:
        return missing_metrics()
    days = {row.get("tradingDay", row.get("financialMetricsDate", row.get("officialMetricsDate")))
            for row in months}
    identities = {(row.get("exchange"), row.get("productCode")) for row in months}
    codes = [row["contractCode"] for row in months]
    if days != {day} or len(identities) != 1 or len(set(codes)) != len(codes):
        raise ValueError("weighted futures requires unique same-product same-day months")
    result = {}
    missing = {}
    for key in (*_MONEY_KEYS, *_SIDE_MONEY_KEYS, "openInterest"):
        missing[key] = [row["contractCode"] for row in months if finite_number(row.get(key)) is None]
        result[key] = None if missing[key] else sum(float(row[key]) for row in months)
    complete = all(result[key] is not None for key in _MONEY_KEYS)
    if any(row.get("monthUniverseStatus") not in (None, "VERIFIED") for row in months):
        for key in (*_MONEY_KEYS, *_SIDE_MONEY_KEYS):
            result[key] = None
        complete = False
    result["monthlyOpenInterest"] = result.pop("openInterest")
    result.update(
        officialMetricsDate=day if all(row.get("officialMetricsDate") == day for row in months) else None,
        financialMetricsDate=day, sourcePolicyVersion=SOURCE_POLICY_VERSION,
        officialMetricsStatus="PASS" if complete else "PARTIAL",
        capitalDepositFormulaVersion=FORMULA_VERSION, specMarginRateLong=None,
        capitalDepositBasis="SUM_OF_TDX_MONTH_BOTH_OFFICIAL_SPECULATIVE_SIDES",
        specMarginRateShort=None, marginRateUsed=None, marginRate=None, contractMultiplier=None,
        capitalDepositReason="部分月份合约缺少同日官方投机保证金率" if result["capitalDeposit"] is None else None,
        aggregateContractCodes=sorted(codes), aggregateContractCount=len(codes),
        aggregateMissingContracts=missing, financialCalculation="SUM_OF_TDX_MONTH_CONTRACTS",
        officialPayloadSha256=sorted({row["officialPayloadSha256"] for row in months if row.get("officialPayloadSha256")}),
        marginPayloadSha256=sorted({row["marginPayloadSha256"] for row in months if row.get("marginPayloadSha256")}),
        marginSourceKind="OFFICIAL_EXCHANGE",
        marginSourceUrl=sorted({row["marginSourceUrl"] for row in months if row.get("marginSourceUrl")}),
        marginPublishedOn=sorted({row["marginPublishedOn"] for row in months if row.get("marginPublishedOn")}),
        marginCarriedForward=any(row.get("marginCarriedForward") is True for row in months),
    )
    return result


def product_months(root: Path, *, exchange: str = "", product: str = "", start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    version = financial_metrics_version(root)
    # TDX months are Silver facts: a new import can change them without an
    # official financial publication. Both revisions are required.
    cache_key = (str(root.resolve()), version, market_data_version(root), exchange, product, start, end)
    with _LOCK:
        if cache_key in _CACHE:
            _CACHE.move_to_end(cache_key)
            return _CACHE[cache_key]
    from .market_tdx_contract_bridge import tdx_contracts_by_day

    raw = official_metrics(root, exchange=exchange, product=product, start=start, end=end, by_contract=True)
    local = tdx_contracts_by_day(root, exchange=exchange, product=product, start=start, end=end)
    official_by_key = {(row["exchange"], row["productCode"], row["contractCode"], row["officialMetricsDate"]): row
                       for row in raw.values()}
    expected: dict[str, set[str]] = defaultdict(set)
    for venue, member_product, code, day in official_by_key:
        expected[f"{venue}.{member_product}|{day}"].add(code)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for day_key, rows in local.items():
        venue_product, day = day_key.split("|", 1)
        venue, member_product = venue_product.split(".", 1)
        seen: set[str] = set()
        for bar in rows:
            code = str(bar["contractCode"])
            if code in seen:
                continue
            seen.add(code)
            official_row = official_by_key.get((venue, member_product, code, day))
            metrics = project_contract_day(
                bar, official_row, day=day, exchange=venue, contract_code=code,
                multiplier=official_multiplier(root, day, venue, member_product, version),
            )
            metrics.update(productCode=member_product,
                           monthUniverseStatus="VERIFIED" if code in expected.get(day_key, set()) else "UNVERIFIED")
            grouped[day_key].append(metrics)
    for day_key, codes in expected.items():
        venue_product, day = day_key.split("|", 1)
        venue, member_product = venue_product.split(".", 1)
        present = {row["contractCode"] for row in grouped[day_key]}
        for code in codes - present:
            official_row = official_by_key[venue, member_product, code, day]
            metrics = project_contract_day({}, official_row, day=day, exchange=venue,
                                           contract_code=code, multiplier=official_multiplier(root, day, venue, member_product, version))
            metrics.update(productCode=member_product, monthUniverseStatus="MISSING_TDX_MONTH")
            grouped[day_key].append(metrics)
    with _LOCK:
        _CACHE[cache_key] = dict(grouped)
        while len(_CACHE) > 32:
            _CACHE.popitem(last=False)
    return dict(grouped)


def continuous_snapshot(root: Path, item: Mapping[str, Any], quote: Mapping[str, Any],
                        months_by_day: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
                        tdx_months_by_day: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
    day = trading_day(quote)
    exchange, product = str(item.get("exchange") or "").upper(), continuous_product_code(item)
    try:
        date.fromisoformat(day)
    except ValueError:
        return missing_metrics()
    grouped = months_by_day if months_by_day is not None else product_months(root, exchange=exchange, product=product, start=day, end=day)
    months = grouped.get(f"{exchange}.{product}|{day}", [])
    if str(item.get("seriesKind") or "").upper() == "WEIGHTED":
        result = weighted_totals(months, day)
    else:
        source = str(item.get("actualSource") or item.get("source") or "")
        if "通达信" not in source and "TDX" not in str(item.get("storageInstrumentId") or "").upper():
            result = {**missing_metrics(), "mappingStatus": "TDX_SOURCE_UNAVAILABLE",
                      "capitalDepositReason": "未取得通达信主连价格，不能核验月份映射"}
        else:
            result = match_tdx_month(quote, months)
            result["mappingAsOfDate"] = day
    result.setdefault("sourcePolicyVersion", SOURCE_POLICY_VERSION)
    result["quoteAsOf"] = day
    return result


def enrich_continuous_bars(root: Path, bars: list[dict[str, Any]], item: Mapping[str, Any], period: str) -> list[dict[str, Any]]:
    if not is_continuous_future(item) or not bars:
        return bars
    if period not in {"1d", "1w", "1mo", "1q", "3mo", "6mo", "1y"}:
        for bar in bars:
            bar.update(amount=None, notional_value=None, capital_deposit=None)
        return bars
    from .market_official_futures_metrics import _BAR_KEYS

    start, end = min(trading_day(bar) for bar in bars), max(trading_day(bar) for bar in bars)
    if period != "1d":
        for bar in bars:
            if bar.get("continuous_financial_version") != FORMULA_VERSION:
                bar.update(amount=None, notional_value=None, capital_deposit=None,
                           capital_deposit_reason="连续合约周期指标需逐日核验来源合约")
        return bars
    grouped = product_months(root, exchange=str(item.get("exchange") or ""), product=continuous_product_code(item), start=start, end=end)
    for bar in bars:
        result = continuous_snapshot(root, item, bar, grouped)
        for key, value in result.items():
            snake = _BAR_KEYS.get(key) or "".join("_" + char.lower() if char.isupper() else char for char in key)
            bar[snake] = value
        bar["continuous_financial_version"] = FORMULA_VERSION
    apply_tdx_daily_changes(root, bars)
    if str(item.get("seriesKind") or "").upper() in {"MAIN", "SECONDARY"}:
        previous = None
        for bar in bars:
            current = bar.get("underlying_contract_code")
            bar["is_roll_day"] = bool(current and previous and current != previous)
            if current:
                previous = current
            else:
                previous = None
    return bars


def aggregate_continuous_daily(root: Path, bars: list[dict[str, Any]], item: Mapping[str, Any], period: str) -> list[dict[str, Any]]:
    from .aggregation import aggregate_daily_bars
    from .market_official_futures_metrics import _bucket_start

    daily = enrich_continuous_bars(root, bars, item, "1d")
    result = aggregate_daily_bars(daily, period) if daily else []
    by_day = {trading_day(bar): bar for bar in daily}
    calendar = load_futures_trading_calendar(root)
    for bar in result:
        day = trading_day(bar)
        last = by_day[day]
        for key, value in last.items():
            if key.startswith(("official_", "mapping_", "aggregate_", "margin_", "spec_margin_", "underlying_", "financial_", "continuous_", "notional_", "capital_", "quote_as_of")):
                bar[key] = value
        days = calendar.between(_bucket_start(day, period), day) if calendar else []
        values = [by_day.get(value, {}).get("amount") for value in days]
        bar["amount"] = sum(values) if values and all(value is not None for value in values) else None
        bar["amount_trading_days"] = days
        bar["period_underlying_contract_codes"] = list(dict.fromkeys(
            by_day[value]["underlying_contract_code"] for value in days
            if value in by_day and by_day[value].get("underlying_contract_code")))
        if bar["amount"] is None:
            bar["official_metrics_status"] = "PARTIAL"
    return result


__all__ = ("is_continuous_future", "continuous_product_code", "match_tdx_month", "weighted_totals", "product_months",
           "continuous_snapshot", "enrich_continuous_bars", "aggregate_continuous_daily", "trading_day")
