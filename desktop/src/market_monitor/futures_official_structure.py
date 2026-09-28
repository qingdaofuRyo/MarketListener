"""Build separate, source-isolated product structures from official daily facts."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .futures import futures_product_name
from .futures_calendar import load_futures_trading_calendar
from .futures_official_multipliers import load_official_multiplier_registry
from .futures_rule_sync import load_rule_book
from .futures_structure import (
    PRODUCT_NOTIONAL_CHART_ID,
    PRODUCT_OPEN_INTEREST_CHART_ID,
    PRODUCT_VOLUME_CHART_ID,
    STRUCTURE_DIRECTION_GROSS,
    _build_baseline,
)
from .storage import MarketStore


OFFICIAL_PRODUCT_OI_FORMULA_VERSION = "futures-structure-oi-official-v1"
OFFICIAL_PRODUCT_NOTIONAL_FORMULA_VERSION = "futures-structure-notional-official-v5"
OFFICIAL_PRODUCT_VOLUME_FORMULA_VERSION = "futures-structure-volume-official-v1"
OFFICIAL_PRODUCT_SOURCE = "SIX_DOMESTIC_EXCHANGES_OFFICIAL_DAILY"
_EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")


def run_official_product_structure_pipeline(
    data_root: Path,
    *,
    metric: str,
    start_day: str | None = None,
    end_day: str | None = None,
    calculated_at: str | None = None,
    rebuild_baseline: bool = False,
) -> dict[str, Any]:
    """Aggregate official daily contract facts without falling back to TDX Silver."""

    if metric not in {"open_interest", "notional", "volume"}:
        raise ValueError("metric must be open_interest, notional or volume")
    start = date.fromisoformat(start_day) if start_day else None
    end = date.fromisoformat(end_day) if end_day else None
    if start and end and start > end:
        raise ValueError("start_day must not be after end_day")
    chart_id, formula_version = _metric_identity(metric)
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    root = Path(data_root)
    store = MarketStore(root)
    try:
        store.register_default_datasets()
        facts = [
            row for row in store.list_futures_official_daily(
                start_day=start.isoformat() if start else None,
                end_day=end.isoformat() if end else None,
            )
            if str(row.get("parser_version") or "").startswith("official-")
        ]
        all_coverage = [
            row for row in store.list_futures_official_daily_coverage(
                start_day=start.isoformat() if start else None,
                end_day=end.isoformat() if end else None,
            )
            if str(row.get("adapter") or "").endswith("-daily-v2")
        ]
    finally:
        store.close()
    if not facts and not all_coverage:
        return {
            "status": "NO_DATA", "chartId": chart_id, "formulaVersion": formula_version,
            "writtenRows": 0, "coverageDays": 0, "baselineDay": None,
        }

    coverage_by_day: dict[str, dict[str, str]] = defaultdict(dict)
    for item in all_coverage:
        coverage_by_day[str(item["trade_date"])][str(item["exchange"]).upper()] = str(item["status"]).upper()
    facts_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in facts:
        facts_by_day[str(item["trade_date"])].append(item)
    source_days = sorted(set(coverage_by_day) | set(facts_by_day))
    first_day = start.isoformat() if start else source_days[0]
    last_day = end.isoformat() if end else source_days[-1]
    calendar = load_futures_trading_calendar(root)
    days = calendar.between(first_day, last_day) if calendar else []
    all_days = sorted(set(days) | {day for day in source_days if first_day <= day <= last_day})
    rule_book = load_rule_book(root) if metric == "notional" else None
    registry = load_official_multiplier_registry() if metric == "notional" else None

    values_by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names_by_day: dict[str, dict[str, str]] = defaultdict(dict)
    coverage_rows: list[dict[str, Any]] = []
    quality_by_day: dict[str, dict[str, Any]] = {}
    for day in all_days:
        exchange_status = coverage_by_day.get(day, {})
        missing_exchanges = [exchange for exchange in _EXCHANGES if exchange_status.get(exchange) != "PASS"]
        day_facts = facts_by_day.get(day, [])
        details: dict[str, Any] = {
            "input": len(day_facts), "missing_settlement": 0, "missing_open_interest": 0,
            "missing_multiplier": 0, "missing_volume": 0, "unverified_volume": 0,
            "unverified_open_interest": 0, "products": set(), "contracts": set(),
        }
        missing_metric = len(missing_exchanges)
        for row in day_facts:
            exchange = str(row["exchange"]).upper()
            product = str(row["product_code"]).upper()
            key = f"{exchange}.{product}"
            value: float | None = None
            if metric == "open_interest":
                oi = _finite_nonnegative(row.get("open_interest"))
                if oi is None:
                    details["missing_open_interest"] += 1
                elif row.get("open_interest_sidedness") != "ONE_SIDED":
                    details["unverified_open_interest"] += 1
                else:
                    value = oi
            elif metric == "volume":
                volume = _finite_nonnegative(row.get("volume"))
                if volume is None:
                    details["missing_volume"] += 1
                elif row.get("volume_sidedness") != "ONE_SIDED":
                    details["unverified_volume"] += 1
                else:
                    value = volume
            else:
                oi = _finite_nonnegative(row.get("open_interest"))
                settlement = _finite_positive(row.get("settlement"))
                if oi is None or row.get("open_interest_sidedness") != "ONE_SIDED":
                    details["missing_open_interest"] += 1
                    if oi is not None:
                        details["unverified_open_interest"] += 1
                if settlement is None:
                    details["missing_settlement"] += 1
                multiplier = None
                official = registry.resolve(day, exchange, product) if registry else None
                multiplier = official.contract_multiplier if official else None
                if exchange == "CFFEX" and multiplier is None and rule_book is not None:
                    # CFFEX daily official contract specifications are retained in RuleBook.
                    multiplier = rule_book.resolve_multiplier(date.fromisoformat(day), exchange, product)
                if multiplier is None:
                    details["missing_multiplier"] += 1
                if (
                    oi is not None and settlement is not None and multiplier is not None
                    and row.get("open_interest_sidedness") == "ONE_SIDED"
                ):
                    value = oi * settlement * multiplier
            if value is None:
                missing_metric += 1
                details["products"].add(key)
                details["contracts"].add(f"{exchange}.{row['contract_code']}")
                continue
            values_by_day[day][key] += value
            product_name = futures_product_name(product, exchange) or product
            names_by_day[day][key] = f"{product_name}（{product}）"
        quality = "PASS" if not missing_exchanges and missing_metric == 0 else "PARTIAL"
        quality_by_day[day] = {
            "input": len(day_facts) + len(missing_exchanges),
            "missing": min(len(day_facts) + len(missing_exchanges), missing_metric),
            "quality": quality,
            "missingExchanges": missing_exchanges,
            **details,
        }
        coverage_rows.append({
            "chart_id": chart_id, "direction": STRUCTURE_DIRECTION_GROSS,
            "formula_version": formula_version, "trade_date": day,
            "input_contract_count": len(day_facts),
            "missing_settlement_count": details["missing_settlement"],
            "missing_open_interest_count": details["missing_open_interest"],
            "missing_multiplier_count": details["missing_multiplier"],
            "missing_products_json": json.dumps(sorted(details["products"]), ensure_ascii=False),
            "missing_contracts_json": json.dumps(sorted(details["contracts"]), ensure_ascii=False),
            "source": OFFICIAL_PRODUCT_SOURCE, "calculated_at": timestamp,
            "missing_volume_count": details["missing_volume"],
            "missing_exchange_count": len(missing_exchanges),
            "missing_exchanges_json": json.dumps(missing_exchanges, ensure_ascii=False),
            "unverified_volume_count": details["unverified_volume"],
            "unverified_open_interest_count": details["unverified_open_interest"],
            "quality_status": quality,
        })

    all_gold_rows: list[dict[str, Any]] = []
    for day, members in values_by_day.items():
        quality = quality_by_day[day]
        for key, value in sorted(members.items()):
            all_gold_rows.append({
                "chart_id": chart_id, "direction": STRUCTURE_DIRECTION_GROSS,
                "trade_date": day, "member_key": key,
                "member_name": names_by_day[day][key], "value": value,
                "input_row_count": quality["input"],
                "missing_row_count": quality["missing"],
                "data_quality_status": quality["quality"],
                "formula_version": formula_version, "price_basis": "SETTLEMENT" if metric == "notional" else None,
                "source": OFFICIAL_PRODUCT_SOURCE, "calculated_at": timestamp,
            })

    selected_gold_rows = [
        row for row in all_gold_rows
        if (start is None or row["trade_date"] >= start.isoformat())
        and (end is None or row["trade_date"] <= end.isoformat())
    ]
    store = MarketStore(root)
    try:
        baseline = store.get_futures_structure_baseline(
            chart_id=chart_id, direction=STRUCTURE_DIRECTION_GROSS, formula_version=formula_version,
        )
        if rebuild_baseline and baseline is not None:
            store.delete_futures_structure_baseline(
                chart_id=chart_id, direction=STRUCTURE_DIRECTION_GROSS, formula_version=formula_version,
            )
            baseline = None
        baseline_created = False
        latest_complete = next(
            (day for day in reversed(all_days)
             if quality_by_day[day]["quality"] == "PASS" and values_by_day.get(day)),
            None,
        )
        baseline_day = latest_complete or next(
            (day for day in reversed(all_days) if values_by_day.get(day)), None,
        )
        if baseline is None and baseline_day is not None:
            baseline = _build_baseline(
                values_by_day[baseline_day], names_by_day[baseline_day], baseline_day,
                timestamp, chart_id=chart_id, direction=STRUCTURE_DIRECTION_GROSS,
                formula_version=formula_version,
                price_basis="SETTLEMENT" if metric == "notional" else None,
                source=OFFICIAL_PRODUCT_SOURCE,
            )
            store.upsert_futures_structure_baseline(baseline)
            baseline_created = True
            baseline = store.get_futures_structure_baseline(
                chart_id=chart_id, direction=STRUCTURE_DIRECTION_GROSS, formula_version=formula_version,
            )
        written = store.replace_futures_structure_daily(
            selected_gold_rows, chart_id=chart_id, direction=STRUCTURE_DIRECTION_GROSS,
            formula_version=formula_version,
            start_day=start.isoformat() if start else None,
            end_day=end.isoformat() if end else None,
        )
        store.replace_futures_structure_input_coverage(
            coverage_rows, chart_id=chart_id, formula_version=formula_version,
        )
    finally:
        store.close()
    latest_quality = quality_by_day.get(max(quality_by_day), {})
    return {
        "status": "PASS" if latest_complete is not None and baseline is not None else "PARTIAL",
        "chartId": chart_id, "formulaVersion": formula_version,
        "sourceRows": len(facts), "writtenRows": written,
        "coverageDays": len(coverage_rows),
        "completeDays": sum(item["quality"] == "PASS" for item in quality_by_day.values()),
        "partialDays": sum(item["quality"] == "PARTIAL" for item in quality_by_day.values()),
        "baselineDay": baseline["baseline_day"] if baseline else None,
        "baselineCreated": baseline_created,
        "baselineQuality": quality_by_day.get(str(baseline["baseline_day"]), {}).get("quality") if baseline else None,
        "latestDay": max(quality_by_day),
        "latestMissingExchanges": latest_quality.get("missingExchanges", []),
        "latestMissingProducts": sorted(latest_quality.get("products", set())),
    }


def _metric_identity(metric: str) -> tuple[str, str]:
    if metric == "open_interest":
        return PRODUCT_OPEN_INTEREST_CHART_ID, OFFICIAL_PRODUCT_OI_FORMULA_VERSION
    if metric == "notional":
        return PRODUCT_NOTIONAL_CHART_ID, OFFICIAL_PRODUCT_NOTIONAL_FORMULA_VERSION
    return PRODUCT_VOLUME_CHART_ID, OFFICIAL_PRODUCT_VOLUME_FORMULA_VERSION


def _finite_nonnegative(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _finite_positive(value: Any) -> float | None:
    number = _finite_nonnegative(value)
    return number if number is not None and number > 0 else None


__all__ = (
    "OFFICIAL_PRODUCT_NOTIONAL_FORMULA_VERSION", "OFFICIAL_PRODUCT_OI_FORMULA_VERSION",
    "OFFICIAL_PRODUCT_SOURCE", "OFFICIAL_PRODUCT_VOLUME_FORMULA_VERSION",
    "run_official_product_structure_pipeline",
)
