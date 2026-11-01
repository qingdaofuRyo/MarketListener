"""Source-isolated TDX contract-day quotes with dated exchange supplements.

The exchange quote is comparison evidence and a source of published turnover,
margin and multiplier. It never supplies a missing TDX price or quantity.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import struct
from typing import Any, Mapping, Sequence
from uuid import uuid4

import duckdb

from .futures_calendar import load_futures_trading_calendar
from .futures_official_multipliers import load_official_multiplier_registry
from .market_list_metrics import RETURN_WINDOWS, finite_number, valid_daily_closes
from .market_official_futures_metrics import contract_identity, financial_metrics_version, official_metrics


SOURCE_POLICY_VERSION = "futures-tdx-official-supplements-v1"
FORMULA_VERSION = "tdx-futures-financial-metrics-v2-cffex-publication-asof"
_PRICES = ("open", "high", "low", "close", "settlement")
_QUANTITIES = ("volume", "openInterest")


@lru_cache(maxsize=4)
def _multiplier_registry(version: str):
    return load_official_multiplier_registry()


@lru_cache(maxsize=4)
def _cffex_rule_book(root: str, version: str):
    from .futures_rule_sync import load_rule_book

    return load_rule_book(Path(root))


def is_tdx_futures_id(identity: Any) -> bool:
    return str(identity or "").upper().endswith(".CONTRACT.TDX")


def official_multiplier(root: Path, day: str, exchange: str, product: str,
                        version: str | None = None) -> float | None:
    rule = _multiplier_registry(version or financial_metrics_version(root)).resolve(day, exchange, product)
    if rule:
        return finite_number(rule.contract_multiplier)
    if exchange == "CFFEX":
        return finite_number(_cffex_rule_book(str(root.resolve()), version or financial_metrics_version(root))
                             .resolve_multiplier(day, exchange, product))
    return None


def _field(row: Mapping[str, Any], key: str) -> float | None:
    aliases = {"openInterest": "open_interest"}
    value = row.get(key)
    return finite_number(value if value is not None else row.get(aliases.get(key, key)))


def _float32_equal(left: float, right: float) -> bool:
    try:
        rounded = struct.unpack("<f", struct.pack("<f", right))[0]
        source = struct.unpack("<f", struct.pack("<f", left))[0]
    except (OverflowError, struct.error):
        return False
    return source == rounded or math.isclose(source, rounded, rel_tol=0, abs_tol=1e-7)


def official_price_checks(root: Path, identities: set[tuple[str, str, str]]) -> dict[tuple[str, str, str], dict[str, float | None]]:
    """Batch only the official comparison prices needed by visible returns."""
    if not identities or not (root / "catalog.duckdb").is_file():
        return {}
    from .futures_official_daily_sources import PARSER_VERSION

    # MarketStore may hold a writer connection in this same process. DuckDB
    # requires identical configuration even for this SELECT-only reader.
    with duckdb.connect(str(root / "catalog.duckdb")) as connection:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if not {"futures_contract_daily_official", "futures_contract_daily_official_coverage"} <= tables:
            return {}
        connection.execute("CREATE TEMP TABLE requested(exchange VARCHAR,contract_code VARCHAR,trade_date VARCHAR)")
        connection.executemany("INSERT INTO requested VALUES (?,?,?)", sorted(identities))
        rows = connection.execute(
            """WITH totals AS (
                 SELECT f.trade_date,f.exchange,f.adapter,count(*) n,
                        count(DISTINCT f.payload_sha256) sha_n,min(f.payload_sha256) sha
                 FROM futures_contract_daily_official f
                 WHERE f.parser_version=? AND f.quality_status='PASS'
                   AND f.trade_date BETWEEN ? AND ? GROUP BY 1,2,3
               ), sources AS (
                 SELECT c.trade_date,c.exchange,c.adapter
                 FROM futures_contract_daily_official_coverage c JOIN totals t USING(trade_date,exchange,adapter)
                 WHERE c.status='PASS' AND c.parser_version=?
                   AND c.adapter LIKE 'official-%-daily-v2'
                   AND c.record_count=t.n AND c.usable_count=t.n AND t.sha_n=1
                   AND c.payload_sha256=t.sha AND length(c.payload_sha256)=64
                 QUALIFY row_number() OVER(PARTITION BY c.trade_date,c.exchange
                   ORDER BY c.retrieved_at DESC,c.adapter)=1
               )
               SELECT f.exchange,f.contract_code,f.trade_date,f.close,f.settlement
               FROM futures_contract_daily_official f
               JOIN sources s USING(trade_date,exchange,adapter)
               JOIN requested r USING(trade_date,exchange,contract_code)
               WHERE f.parser_version=? AND f.quality_status='PASS'""",
            [PARSER_VERSION, min(day for _, _, day in identities), max(day for _, _, day in identities),
             PARSER_VERSION, PARSER_VERSION],
        ).fetchall()
    return {(venue, code, day): {"close": finite_number(close), "settlement": finite_number(settlement)}
            for venue, code, day, close, settlement in rows}


def compare_return_prices(bar: Mapping[str, Any], official: Mapping[str, Any] | None) -> dict[str, str]:
    result = {}
    for key in ("close", "settlement"):
        left, right = _field(bar, key), _field(official or {}, key)
        result[key] = ("UNCOMPARED" if left is None or right is None else
                       "MATCHED" if _float32_equal(left, right) else "MISMATCH")
    return result


def _valid_tdx(bar: Mapping[str, Any], day: str) -> bool:
    identity = bar.get("sourceInstrumentId") or bar.get("instrument_id")
    if not is_tdx_futures_id(identity) or str(bar.get("trading_day") or bar.get("trading_date") or "")[:10] != day:
        return False
    if str(bar.get("quality_status") or "PASS").upper() != "PASS":
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    oi, volume = _field(bar, "openInterest"), _field(bar, "volume")
    if (oi is None or volume is None or oi < 0 or volume < 0
            or not oi.is_integer() or not volume.is_integer()):
        return False
    opened, high, low, close, settlement = (_field(bar, key) for key in _PRICES)
    if volume > 0 and (opened is None or high is None or low is None or close is None
                       or min(opened, high, low, close) <= 0):
        return False
    if high is not None and low is not None and high < low:
        return False
    if opened is not None and high is not None and low is not None and not low <= opened <= high:
        return False
    if close is not None and high is not None and low is not None and not low <= close <= high:
        return False
    return not (oi > 0 and (settlement is None or settlement <= 0))


def project_contract_day(
    bar: Mapping[str, Any], exchange_row: Mapping[str, Any] | None, *,
    day: str, exchange: str, contract_code: str, multiplier: float | None,
) -> dict[str, Any]:
    """Calculate one month without rewriting either provider's source row."""
    official = exchange_row if exchange_row and (
        str(exchange_row.get("officialMetricsDate") or "") == day
        and str(exchange_row.get("exchange") or "") == exchange
        and str(exchange_row.get("contractCode") or "") == contract_code
    ) else None
    valid = _valid_tdx(bar, day)
    field_checks: dict[str, str] = {}
    issues: dict[str, dict[str, float]] = {}
    for key in (*_PRICES, *_QUANTITIES):
        left, right = _field(bar, key), _field(official or {}, key)
        if not valid or left is None or right is None:
            field_checks[key] = "UNCOMPARED"
        elif (_float32_equal(left, right) if key in _PRICES else left == right):
            field_checks[key] = "MATCHED"
        else:
            field_checks[key] = "MISMATCH"
            issues[key] = {"tdx": left, "exchange": right}

    oi, settlement = _field(bar, "openInterest"), _field(bar, "settlement")
    dated_multiplier = finite_number(multiplier)
    if dated_multiplier is None or dated_multiplier <= 0:
        dated_multiplier = None
    blocked = any(field_checks[key] == "MISMATCH" for key in ("settlement", "openInterest"))
    notional = None
    if valid and not blocked and oi is not None and dated_multiplier is not None:
        if oi == 0:
            notional = 0.0
        elif settlement is not None and settlement > 0:
            notional = oi * settlement * dated_multiplier

    amount = _field(official or {}, "amount")
    if amount is not None and amount < 0:
        amount = None
    long = _field(official or {}, "specMarginRateLong")
    short = _field(official or {}, "specMarginRateShort")
    if str((official or {}).get("marginSourceKind") or "") != "OFFICIAL_EXCHANGE":
        long = short = None
    long = long if long is not None and 0 <= long <= 1 else None
    short = short if short is not None and 0 <= short <= 1 else None
    capital_long = notional * long if notional is not None and long is not None else None
    capital_short = notional * short if notional is not None and short is not None else None
    capital = (capital_long + capital_short if capital_long is not None and capital_short is not None
               else None)
    if not valid:
        reason = "通达信月份合约日线未通过字段校验"
    elif blocked:
        reason = "通达信与官网结算价或持仓量不同，派生指标待核验"
    elif dated_multiplier is None:
        reason = "缺少当日有效的官网合约乘数"
    elif long is None or short is None:
        reason = "缺少收盘结算时有效的官网投机保证金率"
    else:
        reason = None
    complete = valid and all(value is not None for value in (notional, amount, capital))
    return {
        "sourcePolicyVersion": SOURCE_POLICY_VERSION, "capitalDepositFormulaVersion": FORMULA_VERSION,
        "tradingDay": day,
        "financialMetricsDate": day if valid else None,
        "officialMetricsDate": day if official is not None else None,
        "officialMetricsSource": (official or {}).get("officialMetricsSource"),
        "officialPayloadSha256": (official or {}).get("officialPayloadSha256"),
        "marginPayloadSha256": (official or {}).get("marginPayloadSha256"),
        "marginSourceKind": (official or {}).get("marginSourceKind") if long is not None else None,
        "marginSourceUrl": (official or {}).get("marginSourceUrl") if long is not None else None,
        "marginPublishedOn": (official or {}).get("marginPublishedOn") if long is not None else None,
        "marginCarriedForward": (official or {}).get("marginCarriedForward") if long is not None else False,
        "contractMultiplier": dated_multiplier, "specMarginRateLong": long,
        "specMarginRateShort": short, "marginRateUsed": long + short if long is not None and short is not None else None,
        "marginRate": max(long, short) if long is not None and short is not None else None,
        "notionalValue": notional, "amount": amount, "capitalDeposit": capital,
        "capitalDepositLong": capital_long, "capitalDepositShort": capital_short,
        "capitalDepositBasis": "TDX_SETTLEMENT_BOTH_OFFICIAL_SPECULATIVE_SIDES",
        "capitalDepositReason": reason,
        "officialMetricsStatus": "PASS" if complete else "WAITING_VERIFICATION" if blocked else "PARTIAL",
        "comparisonStatus": ("MISMATCH" if issues else "MATCHED" if official and valid
                             and all(value == "MATCHED" for value in field_checks.values()) else "UNCOMPARED"),
        "comparisonFields": field_checks, "comparisonIssues": issues,
        "fieldSources": {**{key: "TDX_FUTURES" if valid and _field(bar, key) is not None else None
                             for key in (*_PRICES, *_QUANTITIES)},
                         "amount": "OFFICIAL_EXCHANGE" if amount is not None else None,
                         "contractMultiplier": "OFFICIAL_EXCHANGE" if dated_multiplier is not None else None,
                         "specMarginRates": "OFFICIAL_EXCHANGE" if long is not None and short is not None else None},
        "tdxSourceInstrumentId": bar.get("sourceInstrumentId") or bar.get("instrument_id"),
        "tdxQualityStatus": "PASS" if valid else "INVALID",
        "exchange": exchange, "contractCode": contract_code,
        "open": _field(bar, "open") if valid else None,
        "high": _field(bar, "high") if valid else None,
        "low": _field(bar, "low") if valid else None,
        "close": _field(bar, "close") if valid else None,
        "settlement": settlement if valid else None,
        "volume": _field(bar, "volume") if valid else None,
        "openInterest": oi if valid else None,
    }


def strict_tdx_returns(root: Path, bars: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    """Close-to-close windows cannot jump over a missing actual trading day."""
    calendar = load_futures_trading_calendar(root)
    closes = valid_daily_closes(
        bar for bar in bars if str(bar.get("sourceInstrumentId") or bar.get("instrument_id") or "")
        .upper().endswith(".TDX")
    )
    result: dict[str, float | None] = {}
    for window in RETURN_WINDOWS:
        selected = closes[-window - 1:]
        if len(selected) != window + 1 or not calendar:
            result[str(window)] = None
            continue
        days = calendar.between(selected[0][0], selected[-1][0])
        by_day = {str(bar.get("trading_day") or bar.get("trading_date") or "")[:10]: bar for bar in bars}
        if any((by_day.get(selected[index][0], {}).get("comparisonFields") or
                by_day.get(selected[index][0], {}).get("comparison_fields") or {}).get("close") == "MISMATCH"
               for index in (0, -1)):
            result[str(window)] = None
            continue
        result[str(window)] = (100 * (selected[-1][1] / selected[0][1] - 1)
                               if selected[0][1] > 0 and days == [day for day, _ in selected]
                               else None)
    return result


def apply_tdx_daily_changes(root: Path, bars: list[dict[str, Any]]) -> None:
    """Use yesterday's settlement only when yesterday's TDX bar is present."""
    calendar = load_futures_trading_calendar(root)
    ordinals = {day: index for index, day in enumerate(calendar.trading_days)} if calendar else {}
    previous: dict[str, Any] | None = None
    for bar in bars:
        day = str(bar.get("trading_day") or bar.get("trading_date") or "")[:10]
        close = _field(bar, "close")
        basis = _field(previous or {}, "settlement")
        previous_day = str((previous or {}).get("trading_day") or (previous or {}).get("trading_date") or "")[:10]
        checks = bar.get("comparisonFields") or bar.get("comparison_fields") or {}
        previous_checks = (previous or {}).get("comparisonFields") or (previous or {}).get("comparison_fields") or {}
        comparable = (previous is not None and day in ordinals and previous_day in ordinals
                      and ordinals[day] == ordinals[previous_day] + 1
                      and str(checks.get("close")) != "MISMATCH"
                      and str(previous_checks.get("settlement")) != "MISMATCH")
        change = close - basis if comparable and close is not None and basis is not None and basis > 0 else None
        bar["change"] = change
        bar["pct_change"] = 100 * change / basis if change is not None and basis is not None else None
        previous = bar


def enrich_tdx_contract_bars(root: Path, bars: list[dict[str, Any]],
                             item: Mapping[str, Any], period: str) -> list[dict[str, Any]]:
    """Enrich TDX month bars while preserving their OHLC, OI and volume."""
    if not bars or str(item.get("seriesKind") or "").upper() != "CONTRACT":
        return bars
    if period != "1d":
        for bar in bars:
            if bar.get("tdx_financial_daily_version") != FORMULA_VERSION:
                bar.update(amount=None, notional_value=None, capital_deposit=None,
                           capital_deposit_long=None, capital_deposit_short=None)
            bar["source_policy_version"] = SOURCE_POLICY_VERSION
        return bars
    venue, code = contract_identity(item)
    product_match = re.fullmatch(r"([A-Z]+(?:-F)?)\d{4}", code)
    product = product_match[1] if product_match else str(item.get("productCode") or "").upper()
    days = [str(bar.get("trading_day") or bar.get("trading_date") or "")[:10] for bar in bars]
    supplemental = official_metrics(root, exchange=venue, contract=code, start=min(days), end=max(days))
    version = financial_metrics_version(root)
    for bar, day in zip(bars, days, strict=True):
        original = {**bar, "sourceInstrumentId": bar.get("instrument_id") or item.get("storageInstrumentId")}
        source = supplemental.get(day)
        multiplier = official_multiplier(root, day, venue, product, version)
        if multiplier is None and source:
            multiplier = finite_number(source.get("contractMultiplier"))
        metrics = project_contract_day(original, source, day=day, exchange=venue,
                                       contract_code=code, multiplier=multiplier)
        for key, value in metrics.items():
            snake = "".join("_" + char.lower() if char.isupper() else char for char in key)
            if key not in {"open", "high", "low", "close", "settlement", "volume", "openInterest"}:
                bar[snake] = value
        bar["tdx_financial_daily_version"] = FORMULA_VERSION
    apply_tdx_daily_changes(root, bars)
    return bars


def refresh_financial_coverage(root: Path) -> dict[str, Any]:
    """Publish a small source status snapshot after the serial update window."""
    from datetime import datetime, timezone

    from .futures_official_daily_sources import PARSER_VERSION
    from .futures_settlement_parameters import PARSER_VERSION as MARGIN_VERSION
    from .market_continuous_futures_metrics import product_months
    from .market_query_cache import KLineCacheUnavailable, get_kline_query_store

    root = Path(root)
    tdx_day = None
    if (root / "silver").is_dir():
        store = get_kline_query_store(root)
        try:
            store.ensure_ready()
        except KLineCacheUnavailable:
            # Keep the last published coverage rather than replacing its
            # counts with fabricated zeros while a large import is still
            # waiting for its separate query-index publication.
            previous_path = root / "state" / "futures_financial_coverage.json"
            try:
                previous = json.loads(previous_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            return {**previous, "queryIndexStatus": "STALE"}
        with duckdb.connect(str(store.path), read_only=True) as connection:
            row = connection.execute(
                "SELECT max(substr(latest_bar_at,1,10)) FROM instrument_file WHERE period='1d' "
                "AND regexp_matches(instrument_id,'^CN\\.(CFFEX|SHFE|INE|DCE|CZCE|GFEX)\\.FUTURE\\..*\\.CONTRACT\\.TDX$')"
            ).fetchone()
            tdx_day = str(row[0]) if row and row[0] else None
    turnover_day = margin_day = None
    database = root / "catalog.duckdb"
    if database.is_file():
        with duckdb.connect(str(database)) as connection:
            tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
            if "futures_contract_daily_official_coverage" in tables:
                turnover_day = connection.execute(
                    "SELECT max(trade_date) FROM futures_contract_daily_official_coverage "
                    "WHERE status='PASS' AND parser_version=?", [PARSER_VERSION]
                ).fetchone()[0]
            if "futures_settlement_parameter_coverage" in tables:
                margin_day = connection.execute(
                    "SELECT max(trade_date) FROM futures_settlement_parameter_coverage "
                    "WHERE status='PASS' AND parser_version=?", [MARGIN_VERSION]
                ).fetchone()[0]
    months = product_months(root, start=tdx_day, end=tdx_day) if tdx_day else {}
    rows = [row for group in months.values() for row in group]
    summary = {
        "sourcePolicyVersion": SOURCE_POLICY_VERSION, "formulaVersion": FORMULA_VERSION,
        "queryIndexStatus": "CURRENT",
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tdxLatestDay": tdx_day, "officialTurnoverLatestDay": turnover_day,
        "officialMarginLatestDay": margin_day, "derivedLatestDay": tdx_day if rows else None,
        "contractCount": len(rows),
        "tdxValidCount": sum(row.get("tdxQualityStatus") == "PASS" for row in rows),
        "mismatchCount": sum(row.get("comparisonStatus") == "MISMATCH" for row in rows),
        "uncomparedCount": sum(row.get("comparisonStatus") == "UNCOMPARED" for row in rows),
        "notionalCount": sum(row.get("notionalValue") is not None for row in rows),
        "turnoverCount": sum(row.get("amount") is not None and row.get("tdxQualityStatus") == "PASS" for row in rows),
        "capitalCount": sum(row.get("capitalDeposit") is not None for row in rows),
    }
    target = root / "state" / "futures_financial_coverage.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return summary
