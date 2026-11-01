"""Official contract-day financial metrics for market lists and chart bars.

Each exchange/day chooses one count- and SHA-verified daily source. Margin
parameters match the date, except that CFFEX uses its latest verified
publication on or before the trading day for contracts listed in that sheet.
TDX bars are chart prices only and never supply these three financial fields.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

import duckdb

from .atomic_files import replace_with_retry
from .cffex_margin_asof import (
    applicable_publication_on_or_before,
    known_publication_days,
    verified_publications,
)

from .futures_calendar import load_futures_trading_calendar
from .futures_official_daily_sources import PARSER_VERSION
from .futures_official_multipliers import load_official_multiplier_registry
from .futures_rule_sync import load_rule_book
from .futures_settlement_parameters import PARSER_VERSION as SETTLEMENT_PARSER_VERSION
from .market_list_metrics import RETURN_WINDOWS, finite_number, trailing_trading_returns
from .storage import connect_catalog

FORMULA_VERSION = "official-futures-financial-metrics-v5-cffex-publication-asof"
_LOCK = threading.RLock()
_CACHE: OrderedDict[tuple[str, str, str, str, str], dict[str, dict[str, Any]]] = OrderedDict()
_RETURN_CACHE: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}


def _financial_dependencies(root: Path) -> dict[str, Any]:
    """A bounded file-only freshness check; never bootstrap/scan Silver."""
    config = Path(__file__).with_name("config")
    paths = [config / "futures_official_multipliers.json", config / "cffex_contract_multipliers.json",
             root / "state" / "cn_futures_trading_calendar.json",
             root / "state" / "futures_contract_rule_snapshots.json",
             root / "state" / "market_data_version.json",
             Path(__file__).with_name("market_tdx_futures_projection.py"),
             Path(__file__).with_name("market_continuous_futures_metrics.py")]
    stamps = []
    for path in paths:
        try:
            stat = path.stat()
            stamps.append([str(path), stat.st_size, stat.st_mtime_ns])
        except OSError:
            stamps.append([str(path), None, None])
    return {"formulaVersion": FORMULA_VERSION, "dailyParserVersion": PARSER_VERSION,
            "settlementParserVersion": SETTLEMENT_PARSER_VERSION,
            "files": stamps}


def financial_metrics_version(root: Path) -> str:
    """Read the writer-published digest without Catalog queries or a TTL.

    Only logical dependencies invalidate it. Unrelated Catalog DDL/run records
    do not change the version. Official fact/coverage and effective evidence
    writes must refresh this digest at the single-writer commit boundary.
    """
    dependencies = _financial_dependencies(root)
    try:
        payload = json.loads((root / "state" / "market_financial_version.json").read_text(encoding="utf-8"))
        if (isinstance(payload, dict) and payload.get("schemaVersion") == 1
                and payload.get("dependencies") == dependencies
                and isinstance(payload.get("version"), str) and payload["version"]):
            return payload["version"]
    except (OSError, ValueError, TypeError):
        pass
    return "unpublished-" + sha256(json.dumps(dependencies, sort_keys=True).encode()).hexdigest()[:20]


def refresh_financial_metrics_version(root: Path, connection: duckdb.DuckDBPyConnection | None = None,
                                      *, versions: Mapping[str, Any] | None = None) -> str:
    """Compute source evidence once at the writer/publication boundary.

    Optional connection reuses the existing writer; this function never opens
    a second formal writer, rewrites source rows or uses a busy fallback hash.
    """
    coverage = []
    database = root / "catalog.duckdb"
    owned = None
    if database.is_file():
        try:
            if connection is None:
                # SELECT-only, using the same configuration as the existing
                # same-process writer connection required by DuckDB.
                owned = duckdb.connect(str(database))
                connection = owned
            tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
            for table in ("futures_contract_daily_official_coverage", "futures_settlement_parameter_coverage",
                          "futures_settlement_parameter_backup_observation"):
                if table in tables:
                    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()}
                    fields = [field for field in ("trade_date", "exchange", "adapter", "payload_sha256", "status", "quality_status", "parser_version", "usable_count", "record_count", "retrieved_at", "source_url") if field in columns]
                    expression = " || ':' || ".join(f"coalesce(cast({field} AS VARCHAR),'')" for field in fields)
                    if expression:
                        coverage.append([table, *connection.execute(f"SELECT count(*),coalesce(sum(hash({expression})),0) FROM {table}").fetchone()])
            if "futures_settlement_parameter_official" in tables:
                # Effective interval evidence can change without report SHA.
                coverage.append(["effectiveIntervals", *connection.execute(
                    "SELECT count(*),coalesce(sum(hash(contract_code || exchange || trade_date || "
                    "effective_from || effective_to || coalesce(interval_payload_sha256,''))),0) "
                    "FROM futures_settlement_parameter_official "
                    "WHERE effective_from<>trade_date OR effective_to<>trade_date"
                ).fetchone()])
        finally:
            if owned is not None:
                owned.close()
    dependencies = _financial_dependencies(root)
    # The writer may additionally bind actual query/import/quality revisions;
    # request readers do not query their databases to rediscover these facts.
    producer_versions = dict(versions or {})
    version = sha256(json.dumps([dependencies, coverage, producer_versions], sort_keys=True).encode()).hexdigest()[:20]
    payload = {"schemaVersion": 1, "version": version, "dependencies": dependencies,
               "sourceEvidence": coverage, "producerVersions": producer_versions,
               "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = root / "state" / "market_financial_version.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return version


def is_actual_future(item: Mapping[str, Any]) -> bool:
    return str(item.get("assetType") or "").upper() == "FUTURE" and str(
        item.get("seriesKind") or ""
    ).upper() == "CONTRACT"


def contract_identity(item: Mapping[str, Any]) -> tuple[str, str]:
    exchange = str(item.get("exchange") or "").upper()
    code = str(item.get("symbol") or item.get("sourceSymbol") or "").upper()
    monthly = re.fullmatch(r"([A-Z]+)(\d{4})F", code)
    if monthly:
        code = f"{monthly[1]}-F{monthly[2]}"
    # CZCE legacy three-digit source names are anchored to the instrument's
    # observed date, matching the official adapter's decade normalization.
    short = re.fullmatch(r"([A-Z]+)(\d)(\d{2})", code)
    if exchange == "CZCE" and short:
        day = str(item.get("lastBarAt") or "")[:10]
        try:
            year = date.fromisoformat(day).year
        except ValueError:
            return exchange, code
        candidates = [year // 10 * 10 + int(short[2]) + shift for shift in (-10, 0, 10)]
        contract_year = min(candidates, key=lambda value: abs(value - year))
        code = f"{short[1]}{contract_year % 100:02d}{short[3]}"
    return exchange, code


def completed_day(root: Path) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    upper = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    calendar = load_futures_trading_calendar(root)
    days = [day for day in calendar.trading_days if day <= upper.isoformat()] if calendar else []
    return days[-1] if days else upper.isoformat()


@lru_cache(maxsize=4)
def _registry(version: str):
    return load_official_multiplier_registry()


@lru_cache(maxsize=4)
def _rules(root: str, version: str):
    return load_rule_book(Path(root))


def metric_from_fact(row: Mapping[str, Any], margin: Mapping[str, Any] | None,
                     multiplier: float | None) -> dict[str, Any]:
    oi = finite_number(row.get("open_interest"))
    settlement = finite_number(row.get("settlement"))
    valid_oi = oi is not None and oi >= 0 and row.get("open_interest_sidedness") == "ONE_SIDED"
    notional = (0.0 if valid_oi and oi == 0 and multiplier is not None else
                oi * settlement * multiplier if (
                    valid_oi and settlement is not None and settlement > 0 and multiplier is not None
                ) else None)
    if valid_oi and oi == 0:
        notional = 0.0
    amount = finite_number(row.get("turnover_cny"))
    if (amount is None or amount < 0 or row.get("turnover_quality_status") != "PASS"
            or row.get("turnover_sidedness") != "ONE_SIDED"):
        amount = None
    long = finite_number((margin or {}).get("spec_long_rate"))
    short = finite_number((margin or {}).get("spec_short_rate"))
    long = long if long is not None and 0 <= long <= 1 else None
    short = short if short is not None and 0 <= short <= 1 else None
    used = long + short if long is not None and short is not None else None
    capital_long = notional * long if notional is not None and long is not None else None
    capital_short = notional * short if notional is not None and short is not None else None
    capital = capital_long + capital_short if capital_long is not None and capital_short is not None else None
    backup = (margin or {}).get("source_kind") == "THIRD_PARTY_CURRENT_EXCHANGE_STANDARD"
    complete = all(value is not None for value in (notional, amount, capital))
    return {
        "notionalValue": notional, "amount": amount, "capitalDeposit": capital,
        "capitalDepositLong": capital_long, "capitalDepositShort": capital_short,
        "capitalDepositBasis": "OFFICIAL_SETTLEMENT_BOTH_SPECULATIVE_SIDES",
        "specMarginRateLong": long, "specMarginRateShort": short, "marginRateUsed": used,
        "officialMetricsDate": str(row["trade_date"]), "officialMetricsSource": row.get("source_url"),
        "officialMetricsStatus": "BACKUP_VERIFIED" if complete and backup else "PASS" if complete else "PARTIAL",
        "capitalDepositReason": None if capital is not None else "缺少所选日官网行情、有效乘数或适用的投机保证金率",
        "capitalDepositFormulaVersion": FORMULA_VERSION, "contractMultiplier": multiplier,
        "marginRate": max(long, short) if long is not None and short is not None else None,
        "openInterest": oi if valid_oi else None,
        "settlement": settlement, "close": finite_number(row.get("close")),
        "open": finite_number(row.get("open")), "high": finite_number(row.get("high")),
        "low": finite_number(row.get("low")), "volume": finite_number(row.get("volume")),
        "officialPayloadSha256": row.get("payload_sha256"),
        "marginPayloadSha256": (margin or {}).get("payload_sha256"),
        "marginSourceKind": (margin or {}).get("source_kind") or ("OFFICIAL_EXCHANGE" if margin else None),
        "marginSourceUrl": (margin or {}).get("source_url"),
        "marginPublishedOn": (margin or {}).get("trade_date"),
        "marginCarriedForward": bool(margin and row.get("exchange") == "CFFEX"
                                      and isinstance(margin.get("trade_date"), str)
                                      and margin["trade_date"] < row["trade_date"]),
    }


def missing_metrics() -> dict[str, Any]:
    return {"notionalValue": None, "amount": None, "capitalDeposit": None,
            "capitalDepositLong": None, "capitalDepositShort": None,
            "capitalDepositBasis": "OFFICIAL_SETTLEMENT_BOTH_SPECULATIVE_SIDES",
            "specMarginRateLong": None, "specMarginRateShort": None, "marginRateUsed": None,
            "officialMetricsDate": None, "officialMetricsStatus": "MISSING",
            "capitalDepositReason": "缺少所选日官网行情或适用的结算参数",
            "capitalDepositFormulaVersion": FORMULA_VERSION}


def official_list_returns(root: Path, end: str) -> dict[str, dict[str, Any]]:
    """One bounded query for all futures list return columns, no TDX tails."""
    key = str(root.resolve()), financial_metrics_version(root), end
    with _LOCK:
        if key in _RETURN_CACHE:
            return _RETURN_CACHE[key]
    calendar = load_futures_trading_calendar(root)
    days = [day for day in calendar.trading_days if day <= end][-max(RETURN_WINDOWS) - 1:] if calendar else []
    if not days:
        return {}
    start = days[0]
    path = root / "catalog.duckdb"
    if not path.is_file():
        return {}
    with connect_catalog(root) as connection:
        rows = connection.execute(
            """WITH n AS (
                 SELECT trade_date,exchange,adapter,count(*) n,count(DISTINCT payload_sha256) sha_n,
                        min(payload_sha256) sha FROM futures_contract_daily_official
                 WHERE parser_version=? AND quality_status='PASS' AND trade_date BETWEEN ? AND ? GROUP BY 1,2,3
               ), sources AS (
                 SELECT c.trade_date,c.exchange,c.adapter FROM futures_contract_daily_official_coverage c
                 JOIN n USING(trade_date,exchange,adapter)
                 WHERE c.status='PASS' AND c.parser_version=? AND c.adapter LIKE 'official-%-daily-v2'
                 AND c.record_count=n.n AND c.usable_count=n.n AND n.sha_n=1 AND c.payload_sha256=n.sha AND length(c.payload_sha256)=64
                 QUALIFY row_number() OVER(PARTITION BY c.trade_date,c.exchange ORDER BY c.retrieved_at DESC,c.adapter)=1
               ) SELECT f.exchange,f.contract_code,f.trade_date,f.close
               FROM futures_contract_daily_official f JOIN sources s USING(trade_date,exchange,adapter)
               WHERE f.parser_version=? AND f.quality_status='PASS' ORDER BY f.trade_date""",
            [PARSER_VERSION, start, end, PARSER_VERSION, PARSER_VERSION],
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for exchange, code, day, close in rows:
        grouped[f"{exchange}.{code}"].append({"trading_day": day, "close": close})
    result = {identity: {str(window): value for window, value in trailing_trading_returns(bars).items()}
              for identity, bars in grouped.items()}
    with _LOCK:
        _RETURN_CACHE.clear()
        _RETURN_CACHE[key] = result
    return result


def official_metrics(root: Path, *, exchange: str = "", contract: str = "",
                     start: str = "", end: str = "", latest: bool = False,
                     product: str = "", by_contract: bool = False) -> dict[str, dict[str, Any]]:
    """Read a latest market snapshot or one contract's bounded official history."""
    version = financial_metrics_version(root)
    end = min(end or completed_day(root), completed_day(root))
    key = (str(root.resolve()), version, exchange, contract, f"{start}:{end}:{latest}:{product}:{by_contract}")
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
    path = root / "catalog.duckdb"
    if not path.is_file():
        return {}
    with connect_catalog(root) as connection:
        tables = {str(value[0]) for value in connection.execute("SHOW TABLES").fetchall()}
        if not {"futures_contract_daily_official", "futures_contract_daily_official_coverage"} <= tables:
            return {}
        if latest:
            day = connection.execute("SELECT max(trade_date) FROM futures_contract_daily_official_coverage WHERE trade_date<=?", [end]).fetchone()[0]
            if day is None:
                return {}
            start = end = str(day)
        parameters: list[Any] = [PARSER_VERSION, end, start or "2023-01-01"]
        # Validate the complete source before filtering one requested contract.
        sources = connection.execute(
            """WITH n AS (
                 SELECT trade_date,exchange,adapter,count(*) n,count(DISTINCT payload_sha256) sha_n,
                        min(payload_sha256) sha FROM futures_contract_daily_official
                 WHERE parser_version=? AND quality_status='PASS' AND trade_date<=? AND trade_date>=?
                 GROUP BY 1,2,3)
               SELECT c.trade_date,c.exchange,c.adapter FROM futures_contract_daily_official_coverage c
               JOIN n USING(trade_date,exchange,adapter)
               WHERE c.status='PASS' AND c.parser_version=? AND c.adapter LIKE 'official-%-daily-v2'
                 AND c.record_count=n.n AND c.usable_count=n.n AND n.sha_n=1 AND c.payload_sha256=n.sha AND length(c.payload_sha256)=64
               QUALIFY row_number() OVER(PARTITION BY c.trade_date,c.exchange ORDER BY c.retrieved_at DESC,c.adapter)=1""",
            [*parameters, PARSER_VERSION],
        ).fetchall()
        if not sources:
            return {}
        connection.execute("CREATE TEMP TABLE _financial_sources(trade_date VARCHAR,exchange VARCHAR,adapter VARCHAR)")
        connection.executemany("INSERT INTO _financial_sources VALUES (?,?,?)", sources)
        where = "f.parser_version=? AND f.quality_status='PASS'"
        args: list[Any] = [PARSER_VERSION]
        if exchange:
            where += " AND f.exchange=?"
            args.append(exchange)
        if contract:
            where += " AND f.contract_code=?"
            args.append(contract)
        if product:
            where += " AND f.product_code=?"
            args.append(product)
        cursor = connection.execute("SELECT f.* FROM futures_contract_daily_official f JOIN _financial_sources s USING(trade_date,exchange,adapter) WHERE " + where, args)
        names = [str(value[0]) for value in cursor.description]
        facts = [dict(zip(names, values)) for values in cursor.fetchall()]
        margins: dict[tuple[str, str, str], dict[str, Any]] = {}
        if "futures_settlement_parameter_official" in tables:
            publications = verified_publications(connection, SETTLEMENT_PARSER_VERSION)
            publication_days = sorted({day for day, _, _ in publications})
            known_days = known_publication_days(connection, SETTLEMENT_PARSER_VERSION)
            verified_sources = set(publications)
            cursor = connection.execute("SELECT * FROM futures_settlement_parameter_official WHERE quality_status='PASS' "
                "AND parser_version=? AND length(payload_sha256)=64 "
                "AND (trade_date BETWEEN ? AND ? OR (effective_from<=? AND effective_to>=?) "
                "OR (exchange='CFFEX' AND trade_date<=?))",
                [SETTLEMENT_PARSER_VERSION, start or "2023-01-01", end, end,
                 start or "2023-01-01", end])
            names = [str(value[0]) for value in cursor.description]
            margin_rows = [dict(zip(names, values)) for values in cursor.fetchall()]
            margins_by_contract: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in margin_rows:
                if row["exchange"] == "CFFEX" and (
                    row["trade_date"], row["source_url"], row["payload_sha256"]
                ) not in verified_sources:
                    continue
                margins_by_contract[(row["exchange"], row["contract_code"])].append(row)
            for fact in facts:
                candidates = []
                selected_cffex_day = (applicable_publication_on_or_before(
                    publication_days, known_days, str(fact["trade_date"]),
                )
                                       if fact["exchange"] == "CFFEX" else None)
                for row in margins_by_contract.get((fact["exchange"], fact["contract_code"]), []):
                    if fact["exchange"] == "CFFEX":
                        if row["trade_date"] != selected_cffex_day:
                            continue
                        if row.get("interval_source_url") and not (
                            str(row["effective_from"]) <= str(fact["trade_date"]) <= str(row["effective_to"])
                            and len(str(row.get("interval_payload_sha256") or "")) == 64
                        ):
                            continue
                    elif not (
                        row.get("effective_from") and row.get("effective_to")
                        and str(row["effective_from"]) <= str(fact["trade_date"]) <= str(row["effective_to"])
                        and (str(row["trade_date"]) == str(fact["trade_date"]) or (
                            row.get("interval_source_url")
                            and len(str(row.get("interval_payload_sha256") or "")) == 64
                        ))
                    ):
                        continue
                    candidates.append(row)
                if candidates:
                    margins[(str(fact["trade_date"]), fact["exchange"], fact["contract_code"])] = max(candidates, key=lambda row: (str(row["trade_date"]) == str(fact["trade_date"]), str(row.get("retrieved_at") or "")))
    registry = _registry(version)
    rules = _rules(str(root.resolve()), version)
    result = {}
    for fact in facts:
        day, venue, code, product = str(fact["trade_date"]), fact["exchange"], fact["contract_code"], fact["product_code"]
        official = registry.resolve(day, venue, product)
        multiplier = official.contract_multiplier if official else None
        if multiplier is None and venue == "CFFEX":
            multiplier = rules.resolve_multiplier(day, venue, product)
        metrics = metric_from_fact(fact, margins.get((day, venue, code)), multiplier)
        metrics.update(exchange=venue, productCode=product, contractCode=code)
        result[f"{venue}.{code}|{day}" if by_contract else f"{venue}.{code}" if latest else day] = metrics
    with _LOCK:
        _CACHE[key] = result
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return result


_BAR_KEYS = {"notionalValue": "notional_value", "capitalDeposit": "capital_deposit",
             "capitalDepositLong": "capital_deposit_long", "capitalDepositShort": "capital_deposit_short",
             "capitalDepositBasis": "capital_deposit_basis",
             "marginSourceKind": "margin_source_kind", "marginSourceUrl": "margin_source_url",
             "capitalDepositReason": "capital_deposit_reason", "capitalDepositFormulaVersion": "capital_deposit_formula_version",
             "specMarginRateLong": "spec_margin_rate_long", "specMarginRateShort": "spec_margin_rate_short",
             "marginRateUsed": "margin_rate_used", "officialMetricsDate": "official_metrics_date",
             "officialMetricsStatus": "official_metrics_status", "contractMultiplier": "contract_multiplier",
             "officialMetricsSource": "official_metrics_source", "officialPayloadSha256": "official_payload_sha256",
             "marginPayloadSha256": "margin_payload_sha256", "amountTradingDays": "amount_trading_days",
            "amountPayloadSha256": "amount_payload_sha256",
            "marginPublishedOn": "margin_published_on",
            "marginCarriedForward": "margin_carried_forward"}


def enrich_official_bars(root: Path, bars: list[dict[str, Any]], item: Mapping[str, Any], period: str) -> list[dict[str, Any]]:
    if not is_actual_future(item) or not bars:
        return bars
    if period not in {"1d", "1w", "1mo", "1q", "3mo", "6mo", "1y"}:
        for bar in bars:
            bar.update(amount=None, notional_value=None, capital_deposit=None)
        return bars
    exchange, contract = contract_identity(item)
    end = max(_bar_day(bar) for bar in bars)
    start = min(_bar_day(bar) for bar in bars)
    if period != "1d":
        start = _bucket_start(start, period)
    values = official_metrics(root, exchange=exchange, contract=contract, start=start, end=end)
    calendar = load_futures_trading_calendar(root)
    for bar in bars:
        day = _bar_day(bar)
        metric = dict(values.get(day) or missing_metrics())
        if period != "1d":
            lower = _bucket_start(day, period)
            dates = calendar.between(lower, day) if calendar else []
            amounts = [values.get(trading_day, {}).get("amount") for trading_day in dates]
            metric["amount"] = sum(amounts) if amounts and all(value is not None for value in amounts) else None
            metric["amountTradingDays"] = dates
            metric["amountPayloadSha256"] = [values.get(trading_day, {}).get("officialPayloadSha256") for trading_day in dates]
            if metric["amount"] is None:
                metric["officialMetricsStatus"] = "PARTIAL"
        for camel, value in metric.items():
            if camel in _BAR_KEYS or camel == "amount":
                bar[_BAR_KEYS.get(camel, camel)] = value
    return bars


def _bar_day(bar: Mapping[str, Any]) -> str:
    return str(bar.get("trading_day") or bar.get("trading_date") or bar.get("bar_open_time") or "")[:10]


def _bucket_start(day: str, period: str) -> str:
    value = date.fromisoformat(day)
    if period == "1w":
        return (value - timedelta(days=value.weekday())).isoformat()
    if period == "1y":
        return value.replace(month=1, day=1).isoformat()
    span = {"1mo": 1, "1q": 3, "3mo": 3, "6mo": 6}[period]
    return value.replace(month=(value.month - 1) // span * span + 1, day=1).isoformat()
