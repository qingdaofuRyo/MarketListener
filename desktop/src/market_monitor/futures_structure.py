"""Offline builders for replayable Chinese commodity-futures market structures.

The first supported structure is product open interest.  It deliberately does
not depend on a price field, so it can be published without choosing the
separate ``priceBasis`` required by notional-value structures.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import math
from pathlib import Path
from typing import Any, Mapping

import duckdb

from .futures import is_expired_futures_contract
from .futures_heat_pipeline import _optional_day
from .storage import MarketStore


PRODUCT_OPEN_INTEREST_CHART_ID = "product-open-interest"
MEMBER_OPEN_INTEREST_CHART_ID = "member-open-interest"
STRUCTURE_DIRECTION_GROSS = "gross"
STRUCTURE_FORMULA_VERSION = "futures-structure-oi-v1"
MEMBER_STRUCTURE_FORMULA_VERSION = "futures-member-position-v1"
STRUCTURE_THRESHOLD = 0.015
STRUCTURE_SOURCE = "CN_FUTURE_SILVER"
MEMBER_STRUCTURE_SOURCE = "FUTURES_MEMBER_POSITION_DAILY"
# A local TDX futures directory can also contain overseas contracts or
# provider-specific pseudo exchanges.  Structure charts must use explicit
# domestic exchange keys, not a permissive "not CFFEX" predicate.
COMMODITY_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "GFEX"})


def run_product_open_interest_structure_pipeline(
    data_root: Path,
    *,
    start_day: str | None = None,
    end_day: str | None = None,
    calculated_at: str | None = None,
    rebuild_baseline: bool = False,
) -> dict[str, Any]:
    """Build the product open-interest structure from authoritative Silver.

    This is intentionally a controlled, offline materialisation.  The web
    API reads Gold only and therefore never reinterprets local K-line files.
    Existing baseline metadata is retained so new products are exposed as
    ``unclassified`` instead of silently changing the stack order.
    """

    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise ValueError("start_day must not be after end_day")
    root = Path(data_root)
    files = sorted(
        (root / "silver" / "market=CN" / "asset_type=FUTURE" / "period=1d").rglob("*.parquet")
    )
    bars, source_rows, rejected_rows = _read_contract_aggregates(
        files,
        end_day=end.isoformat() if end else None,
    )
    daily_values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    daily_names: dict[str, dict[str, str]] = defaultdict(dict)
    daily_sources: dict[str, set[str]] = defaultdict(set)
    daily_input_rows: dict[str, int] = defaultdict(int)
    daily_missing_rows: dict[str, int] = defaultdict(int)

    for bar in bars:
        trade_date = str(bar["trade_date"])
        member_key = f"{bar['exchange']}.{bar['product_code']}"
        daily_input_rows[trade_date] += int(bar["input_row_count"])
        daily_missing_rows[trade_date] += int(bar["missing_row_count"])
        open_interest = _positive_or_zero(bar.get("open_interest"))
        if open_interest is None:
            continue
        daily_values[trade_date][member_key] += open_interest
        daily_names[trade_date][member_key] = str(bar["product_code"])
        source = str(bar.get("actual_source") or "").strip()
        if source:
            daily_sources[trade_date].add(source)

    timestamp = calculated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    selected_days = [
        day for day in sorted(daily_values)
        if (start is None or day >= start.isoformat()) and (end is None or day <= end.isoformat())
    ]
    gold_rows: list[dict[str, Any]] = []
    for trade_date in selected_days:
        quality = "PASS" if daily_missing_rows[trade_date] == 0 else "PARTIAL"
        for member_key, value in sorted(daily_values[trade_date].items()):
            gold_rows.append(
                {
                    "chart_id": PRODUCT_OPEN_INTEREST_CHART_ID,
                    "direction": STRUCTURE_DIRECTION_GROSS,
                    "trade_date": trade_date,
                    "member_key": member_key,
                    "member_name": daily_names[trade_date][member_key],
                    "value": value,
                    "input_row_count": daily_input_rows[trade_date],
                    "missing_row_count": daily_missing_rows[trade_date],
                    "data_quality_status": quality,
                    "formula_version": STRUCTURE_FORMULA_VERSION,
                    "price_basis": None,
                    "source": ",".join(sorted(daily_sources[trade_date])) or STRUCTURE_SOURCE,
                    "calculated_at": timestamp,
                }
            )

    store = MarketStore(root)
    try:
        store.register_default_datasets()
        existing_baseline = store.get_futures_structure_baseline(
            chart_id=PRODUCT_OPEN_INTEREST_CHART_ID,
            direction=STRUCTURE_DIRECTION_GROSS,
            formula_version=STRUCTURE_FORMULA_VERSION,
        )
        if rebuild_baseline and existing_baseline is not None:
            store.delete_futures_structure_baseline(
                chart_id=PRODUCT_OPEN_INTEREST_CHART_ID,
                direction=STRUCTURE_DIRECTION_GROSS,
                formula_version=STRUCTURE_FORMULA_VERSION,
            )
            existing_baseline = None
        baseline_created = False
        if existing_baseline is None:
            baseline_day = _latest_complete_day(daily_values, daily_missing_rows)
            if baseline_day is not None:
                baseline = _build_baseline(daily_values[baseline_day], daily_names[baseline_day], baseline_day, timestamp)
                store.upsert_futures_structure_baseline(baseline)
                existing_baseline = baseline
                baseline_created = True
        written = store.replace_futures_structure_daily(
            gold_rows,
            chart_id=PRODUCT_OPEN_INTEREST_CHART_ID,
            direction=STRUCTURE_DIRECTION_GROSS,
            formula_version=STRUCTURE_FORMULA_VERSION,
            start_day=start.isoformat() if start else None,
            end_day=end.isoformat() if end else None,
        )
    finally:
        store.close()
    return {
        "status": "PASS" if written and existing_baseline is not None else "NO_DATA",
        "chartId": PRODUCT_OPEN_INTEREST_CHART_ID,
        "silverFiles": len(files),
        "sourceRows": source_rows,
        "preparedRows": len(bars),
        "rejectedRows": rejected_rows,
        "writtenRows": written,
        "startDay": selected_days[0] if selected_days else None,
        "endDay": selected_days[-1] if selected_days else None,
        "baselineDay": existing_baseline["baseline_day"] if existing_baseline else None,
        "baselineVersion": existing_baseline["baseline_version"] if existing_baseline else None,
        "baselineCreated": baseline_created,
        "baselineRebuilt": rebuild_baseline and baseline_created,
        "formulaVersion": STRUCTURE_FORMULA_VERSION,
        "priceBasis": None,
        "calculatedAt": timestamp,
}


def run_member_open_interest_structure_pipeline(
    data_root: Path,
    *,
    start_day: str | None = None,
    end_day: str | None = None,
    calculated_at: str | None = None,
    rebuild_baseline: bool = False,
) -> dict[str, Any]:
    """Materialise disclosed commodity-member position structures from rank rows.

    This is a ranking-coverage chart, not a claim about every broker's complete
    book.  Long/short and gross values can be summed from their published
    direction lists.  Net directions require the *same exchange, contract and
    member* to be published on both lists; otherwise the value remains absent.
    """

    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise ValueError("start_day must not be after end_day")
    timestamp = calculated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    root = Path(data_root)
    store = MarketStore(root)
    try:
        store.register_default_datasets()
        ranks = store.list_futures_member_position_ranks(commodity_only=True)
        result: dict[str, Any] = {
            "chartId": MEMBER_OPEN_INTEREST_CHART_ID,
            "formulaVersion": MEMBER_STRUCTURE_FORMULA_VERSION,
            "sourceRows": len(ranks),
            "writtenRows": {},
            "directions": {},
            "calculatedAt": timestamp,
        }
        for direction in ("long", "short", STRUCTURE_DIRECTION_GROSS, "net-long", "net-short"):
            values, names, sources, input_rows, missing_rows = _member_structure_values(ranks, direction)
            selected_days = [
                day for day in sorted(values)
                if (start is None or day >= start.isoformat()) and (end is None or day <= end.isoformat())
            ]
            gold_rows = [
                {
                    "chart_id": MEMBER_OPEN_INTEREST_CHART_ID,
                    "direction": direction,
                    "trade_date": day,
                    "member_key": member_key,
                    "member_name": names[day][member_key],
                    "value": value,
                    "input_row_count": input_rows[day],
                    "missing_row_count": missing_rows[day],
                    "data_quality_status": "PASS" if missing_rows[day] == 0 else "PARTIAL",
                    "formula_version": MEMBER_STRUCTURE_FORMULA_VERSION,
                    "price_basis": None,
                    "source": ",".join(sorted(sources[day])) or MEMBER_STRUCTURE_SOURCE,
                    "calculated_at": timestamp,
                }
                for day in selected_days
                for member_key, value in sorted(values[day].items())
            ]
            existing = store.get_futures_structure_baseline(
                chart_id=MEMBER_OPEN_INTEREST_CHART_ID,
                direction=direction,
                formula_version=MEMBER_STRUCTURE_FORMULA_VERSION,
            )
            if rebuild_baseline and existing is not None:
                store.delete_futures_structure_baseline(
                    chart_id=MEMBER_OPEN_INTEREST_CHART_ID,
                    direction=direction,
                    formula_version=MEMBER_STRUCTURE_FORMULA_VERSION,
                )
                existing = None
            baseline_created = False
            if existing is None:
                baseline_day = _latest_complete_day(values, missing_rows)
                if baseline_day is not None:
                    baseline = _build_baseline(
                        values[baseline_day], names[baseline_day], baseline_day, timestamp,
                        chart_id=MEMBER_OPEN_INTEREST_CHART_ID,
                        direction=direction,
                        formula_version=MEMBER_STRUCTURE_FORMULA_VERSION,
                        source=MEMBER_STRUCTURE_SOURCE,
                    )
                    store.upsert_futures_structure_baseline(baseline)
                    existing = baseline
                    baseline_created = True
            written = store.replace_futures_structure_daily(
                gold_rows,
                chart_id=MEMBER_OPEN_INTEREST_CHART_ID,
                direction=direction,
                formula_version=MEMBER_STRUCTURE_FORMULA_VERSION,
                start_day=start.isoformat() if start else None,
                end_day=end.isoformat() if end else None,
            )
            result["writtenRows"][direction] = written
            result["directions"][direction] = {
                "status": "PASS" if written and existing is not None else "NO_COMPLETE_COVERAGE",
                "startDay": selected_days[0] if selected_days else None,
                "endDay": selected_days[-1] if selected_days else None,
                "baselineDay": existing["baseline_day"] if existing else None,
                "baselineVersion": existing["baseline_version"] if existing else None,
                "baselineCreated": baseline_created,
            }
        return result
    finally:
        store.close()


def _member_structure_values(
    ranks: list[Mapping[str, Any]], direction: str
) -> tuple[
    dict[str, dict[str, float]],
    dict[str, dict[str, str]],
    dict[str, set[str]],
    dict[str, int],
    dict[str, int],
]:
    """Aggregate disclosed rank rows while retaining incomplete-coverage evidence."""

    if direction not in {"long", "short", STRUCTURE_DIRECTION_GROSS, "net-long", "net-short"}:
        raise ValueError(f"unsupported member structure direction: {direction}")
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ranks:
        day = str(row["trading_day"])
        by_day[day].append(row)
    values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names: dict[str, dict[str, str]] = defaultdict(dict)
    sources: dict[str, set[str]] = defaultdict(set)
    input_rows: dict[str, int] = defaultdict(int)
    missing_rows: dict[str, int] = defaultdict(int)
    for day, day_rows in by_day.items():
        exchanges = {str(row["exchange"]) for row in day_rows}
        missing_exchange_count = len(COMMODITY_EXCHANGES - exchanges)
        for row in day_rows:
            sources[day].add(str(row["source"]))
        if direction in {"long", "short", STRUCTURE_DIRECTION_GROSS}:
            expected_side = direction.upper()
            selected = [
                row for row in day_rows
                if direction == STRUCTURE_DIRECTION_GROSS or row["side"] == expected_side
            ]
            input_rows[day] = len(selected) + missing_exchange_count
            missing_rows[day] = missing_exchange_count
            for row in selected:
                key = _member_structure_key(row)
                values[day][key] += float(row["position"])
                names[day][key] = _member_structure_name(row)
            continue
        contracts: dict[tuple[str, str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
        for row in day_rows:
            contracts[(str(row["exchange"]), str(row["contract_code"]), str(row["member_key"]))][str(row["side"])] = row
        incomplete_contract_members = 0
        input_rows[day] = len(contracts) + missing_exchange_count
        missing_rows[day] = missing_exchange_count
        for sides in contracts.values():
            long_row = sides.get("LONG")
            short_row = sides.get("SHORT")
            if long_row is None or short_row is None:
                incomplete_contract_members += 1
                continue
            net = float(long_row["position"]) - float(short_row["position"])
            value = max(net, 0.0) if direction == "net-long" else max(-net, 0.0)
            key = _member_structure_key(long_row)
            values[day][key] += value
            names[day][key] = _member_structure_name(long_row)
        missing_rows[day] += incomplete_contract_members
        input_rows[day] += incomplete_contract_members
    return values, names, sources, input_rows, missing_rows


def _member_structure_key(row: Mapping[str, Any]) -> str:
    return f"{str(row['exchange'])}.{str(row['member_key'])}"


def _member_structure_name(row: Mapping[str, Any]) -> str:
    return f"{str(row['exchange'])} · {str(row['member_name'])}"


def _read_contract_aggregates(
    files: list[Path], *, end_day: str | None
) -> tuple[list[dict[str, Any]], int, int]:
    """Aggregate Parquet in DuckDB before Python sees any individual bar.

    Local daily partitions can have duplicate source rows and are large enough
    that materialising their JSON documents in Python is unacceptable.  The
    database therefore reduces them to one candidate per date/exchange/product
    /contract before expiry semantics are evaluated in Python.
    """

    if not files:
        return [], 0, 0
    paths = [str(path) for path in files]
    exchange_sql = ", ".join(f"'{exchange}'" for exchange in sorted(COMMODITY_EXCHANGES))
    end_clause = " AND trade_date <= ?" if end_day is not None else ""
    parameters: list[Any] = [paths]
    if end_day is not None:
        parameters.append(end_day)
    query = f"""
        WITH decoded AS (
            SELECT
                substr(coalesce(json_extract_string(bar_json, '$.trading_date'), json_extract_string(bar_json, '$.trading_day'), ''), 1, 10) AS trade_date,
                upper(coalesce(json_extract_string(bar_json, '$.exchange'), '')) AS exchange,
                upper(coalesce(json_extract_string(bar_json, '$.product_code'), json_extract_string(bar_json, '$.productCode'), '')) AS product_code,
                upper(coalesce(json_extract_string(bar_json, '$.symbol'), json_extract_string(bar_json, '$.contract_code'), '')) AS contract_code,
                try_cast(json_extract_string(bar_json, '$.open_interest') AS DOUBLE) AS open_interest,
                coalesce(json_extract_string(bar_json, '$.fetched_at'), '') AS fetched_at,
                coalesce(json_extract_string(bar_json, '$.actual_source'), json_extract_string(bar_json, '$.source'), '') AS source
            FROM read_parquet(?, union_by_name=true)
            WHERE bar_json IS NOT NULL
                AND coalesce(json_extract_string(bar_json, '$.market'), 'CN') = 'CN'
                AND coalesce(json_extract_string(bar_json, '$.asset_type'), 'FUTURE') = 'FUTURE'
                AND coalesce(json_extract_string(bar_json, '$.period'), '1d') = '1d'
                AND upper(coalesce(json_extract_string(bar_json, '$.series_kind'), json_extract_string(bar_json, '$.futures_series_kind'), '')) = 'CONTRACT'
                AND upper(coalesce(json_extract_string(bar_json, '$.exchange'), '')) IN ({exchange_sql})
                AND upper(coalesce(json_extract_string(bar_json, '$.quality_status'), 'UNKNOWN')) = 'PASS'
                AND lower(coalesce(json_extract_string(bar_json, '$.is_active'), 'true')) NOT IN ('false', '0')
                AND lower(coalesce(json_extract_string(bar_json, '$.is_expired'), 'false')) NOT IN ('true', '1')
        )
        SELECT trade_date, exchange, product_code, contract_code,
            arg_max(open_interest, fetched_at) AS open_interest,
            count(*) AS input_row_count,
            count_if(open_interest IS NULL) AS missing_row_count,
            max(source) AS actual_source
        FROM decoded
        WHERE trade_date <> '' AND product_code <> '' AND contract_code <> ''{end_clause}
        GROUP BY trade_date, exchange, product_code, contract_code
        ORDER BY trade_date, exchange, product_code, contract_code
    """
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    output: list[dict[str, Any]] = []
    rejected = 0
    source_rows = 0
    for trade_date, exchange, product_code, contract_code, open_interest, input_count, missing_count, source in rows:
        input_row_count = int(input_count)
        source_rows += input_row_count
        try:
            trade_day = date.fromisoformat(str(trade_date))
        except ValueError:
            rejected += input_row_count
            continue
        if trade_day.weekday() >= 5 or is_expired_futures_contract(
            str(contract_code), str(exchange), reference_day=trade_day, last_trading_day=trade_day
        ):
            rejected += input_row_count
            continue
        output.append(
            {
                "trade_date": str(trade_date),
                "exchange": str(exchange),
                "product_code": str(product_code),
                "open_interest": open_interest,
                "input_row_count": input_row_count,
                "missing_row_count": int(missing_count),
                "actual_source": str(source or ""),
            }
        )
    return output, source_rows, rejected


def _latest_complete_day(
    daily_values: Mapping[str, Mapping[str, float]], daily_missing_rows: Mapping[str, int]
) -> str | None:
    return next(
        (
            day
            for day in sorted(daily_values, reverse=True)
            if daily_values[day] and daily_missing_rows.get(day, 0) == 0
        ),
        None,
    )


def _build_baseline(
    values: Mapping[str, float],
    names: Mapping[str, str],
    baseline_day: str,
    created_at: str,
    *,
    chart_id: str = PRODUCT_OPEN_INTEREST_CHART_ID,
    direction: str = STRUCTURE_DIRECTION_GROSS,
    formula_version: str = STRUCTURE_FORMULA_VERSION,
    source: str = STRUCTURE_SOURCE,
) -> dict[str, Any]:
    total = sum(values.values())
    if total <= 0:
        raise ValueError("structure baseline requires a positive total")
    ordered_keys = sorted(values, key=lambda key: (-values[key], key))
    primary_keys = [key for key in ordered_keys if values[key] / total >= STRUCTURE_THRESHOLD]
    other_keys = [key for key in ordered_keys if key not in primary_keys]
    members = [
        {"memberKey": key, "memberName": names.get(key, key)}
        for key in ordered_keys
    ]
    return {
        "chart_id": chart_id,
        "direction": direction,
        "baseline_version": f"{formula_version}:{baseline_day}",
        "baseline_day": baseline_day,
        "threshold": STRUCTURE_THRESHOLD,
        "stack_order": [*primary_keys, *( ["OTHER"] if other_keys else [])],
        "primary_members": [member for member in members if member["memberKey"] in primary_keys],
        "other_members": [member for member in members if member["memberKey"] in other_keys],
        "formula_version": formula_version,
        "price_basis": None,
        "source": source,
        "created_at": created_at,
    }


def _positive_or_zero(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


__all__ = (
    "MEMBER_OPEN_INTEREST_CHART_ID",
    "MEMBER_STRUCTURE_FORMULA_VERSION",
    "PRODUCT_OPEN_INTEREST_CHART_ID",
    "STRUCTURE_DIRECTION_GROSS",
    "STRUCTURE_FORMULA_VERSION",
    "STRUCTURE_THRESHOLD",
    "run_member_open_interest_structure_pipeline",
    "run_product_open_interest_structure_pipeline",
)
