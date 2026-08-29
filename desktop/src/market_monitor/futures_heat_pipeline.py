"""Offline Silver-to-Gold builder for Chinese commodity-futures heat."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import duckdb

from .futures import is_expired_futures_contract, resolve_futures_contract_spec
from .futures_calendar import load_futures_trading_calendar
from .futures_heat import (
    LongShortHeatConfig,
    compute_futures_long_short_heat,
    load_long_short_heat_config,
)
from .futures_rule_sync import RuleBook, load_rule_book
from .storage import MarketStore


CALCULATION_METHOD = (
    "品种方向优先使用加权合约结算价对数收益，否则按各有效月份合约市值权重聚合；"
    "日宽度=(上涨品种数-下跌品种数)/(上涨品种数+下跌品种数)*100；"
    "日资金=(上涨沉淀资金-下跌沉淀资金)/(上涨沉淀资金+下跌沉淀资金)*100"
)


def run_futures_heat_pipeline(
    data_root: Path,
    *,
    start_day: str | None = None,
    end_day: str | None = None,
    calculated_at: str | None = None,
) -> dict[str, Any]:
    """Build replayable Gold rows from local CN/FUTURE/1d Silver partitions."""

    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise ValueError("start_day must not be after end_day")
    root = Path(data_root)
    files = sorted(
        (root / "silver" / "market=CN" / "asset_type=FUTURE" / "period=1d").rglob("*.parquet")
    )
    config = load_long_short_heat_config()
    raw_rows = _read_relevant_silver(files, end_day=end.isoformat() if end else None)
    rule_book = load_rule_book(root)
    bars, rejected_rows = _prepare_pipeline_bars(raw_rows, rule_book=rule_book)
    observed_days = sorted({str(bar["trade_date"]) for bar in bars})
    calendar = load_futures_trading_calendar(root)
    calendar_excluded_days: list[str] = []
    calendar_excluded_rows = 0
    if calendar is not None and observed_days:
        if observed_days[-1] > calendar.trading_days[-1]:
            raise ValueError("persisted CN futures calendar is stale; run futures-calendar-sync")
        trading_days = calendar.between(observed_days[0], observed_days[-1])
        allowed_days = set(trading_days)
        calendar_excluded_days = sorted(set(observed_days) - allowed_days)
        before_calendar_filter = len(bars)
        bars = [bar for bar in bars if str(bar["trade_date"]) in allowed_days]
        calendar_excluded_rows = before_calendar_filter - len(bars)
        calendar_source = calendar.provider
    else:
        trading_days = observed_days
        calendar_source = "OBSERVED_CN_FUTURE_SILVER_DAYS"
    expected_products = _expected_products_by_day(
        bars,
        config.exclude_exchanges,
        trading_days=trading_days,
    )
    calculated = compute_futures_long_short_heat(
        bars,
        config=config,
        trading_days=trading_days,
        expected_products_by_day=expected_products,
    )
    selected = [
        snapshot
        for snapshot in calculated
        if (start is None or snapshot.trade_date >= start.isoformat())
        and (end is None or snapshot.trade_date <= end.isoformat())
    ]
    cutoff = _source_cutoff(bars)
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    calculation_method = _calculation_method(config)
    gold_rows = [
        {
            **snapshot.to_dict(),
            "source_cutoff": cutoff,
            "calculation_method": calculation_method,
            "calculated_at": timestamp,
        }
        for snapshot in selected
    ]
    store = MarketStore(root)
    try:
        store.register_default_datasets()
        written = store.replace_futures_long_short_heat(
            gold_rows,
            formula_version=config.formula_version,
            start_day=start.isoformat() if start else None,
            end_day=end.isoformat() if end else None,
        )
    finally:
        store.close()
    return {
        "status": "PASS" if written else "NO_DATA",
        "silverFiles": len(files),
        "sourceRows": len(raw_rows),
        "preparedRows": len(bars),
        "rejectedRows": rejected_rows,
        "tradingDays": len(trading_days),
        "writtenRows": written,
        "startDay": selected[0].trade_date if selected else None,
        "endDay": selected[-1].trade_date if selected else None,
        "sourceCutoff": cutoff,
        "calculatedAt": timestamp,
        "formulaVersion": config.formula_version,
        "ruleSnapshotDays": len(rule_book.trading_days),
        "calendarSource": calendar_source,
        "calendarExcludedDays": calendar_excluded_days,
        "calendarExcludedRows": calendar_excluded_rows,
        "returnCoverage": selected[-1].return_coverage if selected else None,
        "fundCoverage": selected[-1].fund_coverage if selected else None,
        "dataQualityStatus": selected[-1].data_quality_status if selected else "UNAVAILABLE",
    }


def _read_relevant_silver(files: Iterable[Path], *, end_day: str | None) -> list[dict[str, Any]]:
    paths = [str(path) for path in files]
    if not paths:
        return []
    clauses = [
        "bar_json IS NOT NULL",
        "coalesce(json_extract_string(bar_json, '$.market'), 'CN') = 'CN'",
        "coalesce(json_extract_string(bar_json, '$.asset_type'), 'FUTURE') = 'FUTURE'",
        "coalesce(json_extract_string(bar_json, '$.period'), '1d') = '1d'",
        "upper(coalesce(json_extract_string(bar_json, '$.series_kind'), "
        "json_extract_string(bar_json, '$.futures_series_kind'), '')) IN ('CONTRACT', 'WEIGHTED')",
    ]
    parameters: list[Any] = [paths]
    if end_day is not None:
        clauses.append(
            "substr(coalesce(json_extract_string(bar_json, '$.trading_date'), "
            "json_extract_string(bar_json, '$.trading_day'), ''), 1, 10) <= ?"
        )
        parameters.append(end_day)
    query = (
        "SELECT bar_json FROM read_parquet(?, union_by_name=true) WHERE "
        + " AND ".join(clauses)
        + " ORDER BY bar_open_time, instrument_id"
    )
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    output: list[dict[str, Any]] = []
    for (bar_json,) in rows:
        try:
            payload = json.loads(str(bar_json))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            output.append(payload)
    return output


def _prepare_pipeline_bars(
    rows: Iterable[Mapping[str, Any]], *, rule_book: RuleBook | None = None
) -> tuple[list[dict[str, Any]], int]:
    selected: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    rejected = 0
    for source in rows:
        prepared = _prepare_pipeline_bar(source, rule_book=rule_book)
        if prepared is None:
            rejected += 1
            continue
        identity = (
            prepared["trade_date"],
            prepared["exchange"],
            prepared["product_code"],
            prepared["series_kind"],
            prepared["contract_code"] if prepared["series_kind"] == "CONTRACT" else "WEIGHTED",
        )
        current = selected.get(identity)
        if current is None or _row_priority(prepared) > _row_priority(current):
            selected[identity] = prepared
    return sorted(
        selected.values(),
        key=lambda row: (
            row["trade_date"], row["exchange"], row["product_code"],
            row["series_kind"], row["contract_code"],
        ),
    ), rejected


def _prepare_pipeline_bar(
    source: Mapping[str, Any], *, rule_book: RuleBook | None = None
) -> dict[str, Any] | None:
    trade_text = str(source.get("trading_date") or source.get("trading_day") or "")[:10]
    try:
        trade_day = date.fromisoformat(trade_text)
    except ValueError:
        return None
    if trade_day.weekday() >= 5:
        return None
    exchange = str(source.get("exchange") or "").strip().upper()
    product = str(source.get("product_code") or source.get("productCode") or "").strip().upper()
    kind = str(source.get("series_kind") or source.get("futures_series_kind") or "").strip().upper()
    symbol = str(source.get("symbol") or source.get("contract_code") or "").strip().upper()
    if not exchange or not product or kind not in {"CONTRACT", "WEIGHTED"}:
        return None
    contract_code = symbol or str(source.get("instrument_id") or "").strip().upper()
    expired = source.get("is_expired") is True or (
        kind == "CONTRACT"
        and is_expired_futures_contract(
            contract_code,
            exchange,
            reference_day=trade_day,
            last_trading_day=trade_day,
        )
    )
    active = source.get("is_active", True) is not False and not expired
    quality_status = str(source.get("quality_status") or "UNKNOWN").upper()
    usable = quality_status == "PASS"
    multiplier = (
        source.get("contract_multiplier") or source.get("contractMultiplier")
    ) if usable else None
    margin_rate = (source.get("margin_rate") or source.get("marginRate")) if usable else None
    if usable and kind == "CONTRACT":
        exact_rule = (
            rule_book.resolve(trade_day, exchange, product, contract_code)
            if rule_book is not None
            else None
        )
        if exact_rule is not None:
            multiplier = exact_rule.contract_multiplier
            margin_rate = exact_rule.margin_rate
        elif multiplier is None or margin_rate is None:
            resolution = resolve_futures_contract_spec(product, exchange, trade_day)
            if resolution.spec is not None:
                multiplier = multiplier or resolution.spec.contract_multiplier
                margin_rate = margin_rate or resolution.spec.margin_rate
    return {
        "trade_date": trade_text,
        "exchange": exchange,
        "product_code": product,
        "series_kind": kind,
        "contract_code": contract_code or f"{exchange}.{product}.{kind}",
        "settlement": source.get("settlement") if usable else None,
        "open_interest": source.get("open_interest") if usable else None,
        "contract_multiplier": multiplier,
        "margin_rate": margin_rate,
        "is_active": active,
        "is_expired": expired,
        "quality_status": quality_status,
        "fetched_at": str(source.get("fetched_at") or ""),
        "actual_source": str(source.get("actual_source") or source.get("source") or ""),
    }


def _row_priority(row: Mapping[str, Any]) -> tuple[int, int, int, int, str, str]:
    source = str(row.get("actual_source") or "").upper()
    return (
        int(str(row.get("quality_status")) == "PASS"),
        int(row.get("settlement") is not None),
        int(row.get("open_interest") is not None),
        int("TDX" in source or "通达信" in source),
        str(row.get("fetched_at") or ""),
        source,
    )


def _expected_products_by_day(
    bars: Iterable[Mapping[str, Any]],
    exclude_exchanges: Iterable[str],
    *,
    trading_days: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    excluded = {str(exchange).upper() for exchange in exclude_exchanges}
    result: dict[str, set[str]] = {}
    for bar in bars:
        exchange = str(bar["exchange"])
        if exchange in excluded or (bar["series_kind"] == "CONTRACT" and not bar["is_active"]):
            continue
        result.setdefault(str(bar["trade_date"]), set()).add(
            f"{exchange}.{bar['product_code']}"
        )
    output: dict[str, list[str]] = {}
    previous: list[str] = []
    for day in sorted(set(trading_days or result)):
        products = sorted(result.get(day, ()))
        if products:
            previous = products
        output[day] = products or list(previous)
    return output


def _source_cutoff(bars: Iterable[Mapping[str, Any]]) -> str:
    values = [str(bar.get("fetched_at") or bar.get("trade_date") or "") for bar in bars]
    return max((value for value in values if value), default="")


def _optional_day(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error


def _calculation_method(config: LongShortHeatConfig) -> str:
    return (
        f"{CALCULATION_METHOD}；最近{config.lookback_trading_days}个有效交易日按半衰期"
        f"{config.half_life_trading_days:g}日指数衰减并对可用样本重新归一化；"
        f"资金必要字段覆盖率不低于{config.min_fund_coverage:g}才计算资金热度；"
        f"方向中性阈值={config.neutral_threshold:g}；日期精确规则快照优先于包内静态合约规格"
    )


__all__ = ("CALCULATION_METHOD", "run_futures_heat_pipeline")
