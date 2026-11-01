"""Source-gated, product-level official futures distribution matrices.

Only exchange daily contract facts enter this module.  The database reduces
contracts to product days; the browser receives bounded product matrices.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

import duckdb

from .futures import futures_product_name
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
from .futures_weekly_heat import (
    FORMULA_VERSION as WEEKLY_HEAT_FORMULA_VERSION,
    product_weekly_streaks_from_daily,
)
from .storage import MarketStore


FORMULA_VERSION = "futures-market-distributions-official-v15"
DIRECTION_FORMULA_VERSION = "futures-oi-weighted-daily-sum-60-v1"
SHORT_MOVE_FORMULA_VERSION = "futures-oi-weighted-daily-sum-3-v1"
SHORT_RANK_FORMULA_VERSION = "futures-short-move-cross-sectional-midrank-v1"
_CORE_CACHE_SCHEMA = 7
_DIRECTION_WINDOW = 60
_EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")
_METRICS = ("open_interest", "notional", "turnover", "volume", "return", "deposit")
_UNITS = {"open_interest": "contracts", "notional": "CNY", "turnover": "CNY", "volume": "contracts", "return": "percent", "deposit": "CNY"}
_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_SNAPSHOT_FILE_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_SNAPSHOT_META_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_LOCK = threading.RLock()
_SNAPSHOT_LOCK = threading.RLock()


def _calendar_fingerprint(calendar: Any) -> str | None:
    """Bind a cached direction calculation to the current verified date list."""

    if calendar is None or not calendar.trading_days:
        return None
    payload = "\n".join((calendar.provider, *calendar.trading_days))
    return sha256(payload.encode("utf-8")).hexdigest()


def _direction_calendar_verified(root: Path, core: dict[str, Any]) -> bool:
    try:
        calendar = load_futures_trading_calendar(root)
        fingerprint = _calendar_fingerprint(calendar)
        return bool(calendar and fingerprint and core.get("calendarVerified") is True
                    and core.get("calendarFingerprint") == fingerprint
                    and core.get("dates") == calendar.between("2023-01-01", str(core["latestDay"])))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _direction_points(
    days: list[str], records: dict[str, dict[str, Any]], *, calendar_verified: bool,
) -> list[dict[str, Any]]:
    """Sum the last 60 verified daily returns; missing days never bridge a gap."""

    first_index = next((index for index, day in enumerate(days) if day in records), None)
    result: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        empty: dict[str, Any] = {
            "sum": None, "up": None, "down": None, "flat": None,
            "count": None, "status": "MISSING", "reason": None,
        }
        if not calendar_verified:
            empty.update(status="CALENDAR_UNVERIFIED", reason="期货交易日历缺失、无效或与官网来源日期不一致")
            result.append(empty)
            continue
        if first_index is None or index < first_index:
            empty["reason"] = "尚无该品种可核验的交易日行情"
            result.append(empty)
            continue
        start = max(first_index, index - _DIRECTION_WINDOW + 1)
        returns: list[float] = []
        for sample_day in days[start:index + 1]:
            point = records.get(sample_day)
            if point is None:
                empty["reason"] = f"{sample_day}品种行情缺报"
                break
            oi = point.get("openInterest")
            if not isinstance(oi, (int, float)) or isinstance(oi, bool) or not math.isfinite(oi) or oi <= 0:
                empty["reason"] = f"{sample_day}缺少有效持仓"
                break
            value = point.get("weightedReturn")
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= -1):
                empty["reason"] = f"{sample_day}缺少可核验的合约加权日涨跌幅"
                break
            returns.append(float(value))
        else:
            count = len(returns)
            directions = [0 if math.isclose(value, 0.0, abs_tol=1e-12) else
                          1 if value > 0 else -1 for value in returns]
            result.append({"sum": math.fsum(returns) * 100,
                           "up": directions.count(1),
                           "down": directions.count(-1),
                           "flat": directions.count(0),
                           "count": count, "status": "FULL" if count == _DIRECTION_WINDOW else "SHORT",
                           "reason": None})
            continue
        result.append(empty)
    return result


def _short_move_3_points(
    days: list[str], records: dict[str, dict[str, Any]], *, calendar_verified: bool,
) -> list[float | None]:
    """Three actual trading-day changes, computed before any display slicing."""

    result: list[float | None] = []
    for index in range(len(days)):
        if not calendar_verified or index < 2:
            result.append(None)
            continue
        window: list[float] = []
        for day in days[index - 2:index + 1]:
            point = records.get(day, {})
            oi, value = point.get("openInterest"), point.get("weightedReturn")
            if (isinstance(oi, bool) or not isinstance(oi, (int, float))
                    or not math.isfinite(oi) or oi <= 0
                    or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= -1):
                break
            window.append(float(value))
        result.append(math.fsum(window) * 100 if len(window) == 3 else None)
    return result


def _short_move_rank_points(
    days: list[str], coverage: dict[str, Any], moves: dict[str, list[float | None]],
    *, calendar_verified: bool,
) -> tuple[dict[str, list[float | None]], list[str], list[int | None]]:
    """Compare only complete exchange windows; equal moves share a midrank."""

    ranks: dict[str, list[float | None]] = {key: [None] * len(days) for key in moves}
    statuses = ["INCOMPLETE"] * len(days)
    counts: list[int | None] = [None] * len(days)
    for index in range(2, len(days)):
        if not calendar_verified or any(
            not isinstance(coverage.get(day), dict)
            or coverage[day].get("missingExchanges") != []
            for day in days[index - 2:index + 1]
        ):
            continue
        samples = sorted((value, key) for key, series in moves.items()
                         if (value := series[index]) is not None and math.isfinite(value))
        counts[index] = len(samples)
        if len(samples) < 20:
            continue
        statuses[index] = "COMPLETE"
        begin = 0
        while begin < len(samples):
            end = begin + 1
            while end < len(samples) and samples[end][0] == samples[begin][0]:
                end += 1
            rank = (begin + (end - begin) / 2) / len(samples)
            for _, key in samples[begin:end]:
                ranks[key][index] = rank
            begin = end
    return ranks, statuses, counts


def market_distributions(
    data_root: Path, *, metric: str = "open_interest", range_name: str = "1y",
    window: int = 20, end_day: str | None = None, snapshot_only: bool = False,
) -> dict[str, Any]:
    """Return fixed-date product arrays for one of six market views.

    Daily product return is the open-interest-weighted settlement return.
    Return series also carry the change between the first and last daily
    product-weighted settlements in the latest ten actual trading dates.
    Deposit sums exact-day official speculative margin on both sides across
    every positive-position month, without replacing missing rates with zero.
    Legacy xValues/yValues remain available for existing consumers.
    """

    if metric not in _METRICS:
        raise ValueError("metric must be open_interest, notional, turnover, volume, return or deposit")
    if range_name not in {"1y", "3y", "5y", "all"}:
        raise ValueError("range must be 1y, 3y, 5y or all")
    if window not in {1, 5, 20, 60}:
        raise ValueError("window must be 1, 5, 20 or 60 trading days")
    if end_day is not None:
        date.fromisoformat(end_day)
    root = Path(data_root)
    empty = {
        "available": False, "metric": metric, "unit": _UNITS[metric], "dates": [],
        "totals": [], "series": [], "baselineDay": None, "coverage": [],
        "formulaVersion": FORMULA_VERSION, "directionFormulaVersion": DIRECTION_FORMULA_VERSION,
        "limitations": ["尚无已核验的官网逐合约日行情。"],
        "windowTradingDays": window, "snapshotStatus": "CURRENT",
    }
    if not (root / "catalog.duckdb").is_file():
        return empty

    snapshot_status = "CURRENT"
    if snapshot_only:
        snapshot = _read_latest_core_snapshot(root, require_ten_day=metric == "return",
                                              require_deposit=metric == "deposit",
                                              range_name=range_name, end_day=end_day)
        if snapshot is None:
            return {**empty, "limitations": ["尚无可复用的官网聚合快照。"]}
        core, snapshot_identity = snapshot
        snapshot_status = "CACHED_AWAITING_REFRESH"
        data_version_source = repr((FORMULA_VERSION, snapshot_identity))
    else:
        try:
            store = MarketStore(root)
        except duckdb.IOException as error:
            if "Cannot open file" not in str(error):
                raise
            snapshot = _read_latest_core_snapshot(root, require_ten_day=metric == "return",
                                                  require_deposit=metric == "deposit",
                                                  range_name=range_name, end_day=end_day)
            if snapshot is None:
                raise
            core, snapshot_identity = snapshot
            snapshot_status = "CACHED_DURING_UPDATE"
            data_version_source = repr((FORMULA_VERSION, snapshot_identity))
        else:
            try:
                connection = store.connection
                version = connection.execute(
                    "SELECT count(*),coalesce(sum(hash(coalesce(payload_sha256,'') || ':' || "
                    "status || ':' || coalesce(parser_version,'') || ':' || "
                    "cast(usable_count AS VARCHAR))),0),max(retrieved_at) "
                    "FROM futures_contract_daily_official_coverage "
                    "WHERE adapter LIKE 'official-%-daily-v2'"
                ).fetchone()
                latest = connection.execute(
                    "SELECT max(trade_date) FROM futures_contract_daily_official_coverage "
                    "WHERE adapter LIKE 'official-%-daily-v2'"
                ).fetchone()[0]
                if latest is None:
                    return empty
                registry_path = Path(__file__).with_name("config") / "futures_official_multipliers.json"
                cffex_path = Path(__file__).with_name("config") / "cffex_contract_multipliers.json"
                calendar_path = root / "state" / "cn_futures_trading_calendar.json"
                rule_path = root / "state" / "futures_contract_rule_snapshots.json"
                has_weekly_gold = connection.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='futures_weekly_heat_product_gold'"
                ).fetchone()[0]
                weekly_version = connection.execute(
                    "SELECT count(*),max(calculated_at) FROM futures_weekly_heat_product_gold "
                    "WHERE formula_version=?", [WEEKLY_HEAT_FORMULA_VERSION],
                ).fetchone() if has_weekly_gold else None
                has_margin = connection.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='futures_settlement_parameter_official'"
                ).fetchone()[0]
                margin_version = connection.execute(
                    "SELECT count(*),coalesce(sum(hash(trade_date || ':' || exchange || ':' || "
                    "contract_code || ':' || effective_from || ':' || effective_to || ':' || "
                    "coalesce(cast(spec_long_rate AS VARCHAR),'') || ':' || "
                    "coalesce(cast(spec_short_rate AS VARCHAR),'') || ':' || payload_sha256 || ':' || "
                    "source_url || ':' || retrieved_at || ':' || "
                    "coalesce(interval_source_url,'') || ':' || "
                    "coalesce(interval_payload_sha256,'') || ':' || parser_version || ':' || "
                    "quality_status)),0),max(retrieved_at) "
                    "FROM futures_settlement_parameter_official"
                ).fetchone() if has_margin else None
                has_margin_coverage = connection.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='futures_settlement_parameter_coverage'"
                ).fetchone()[0]
                margin_coverage_version = connection.execute(
                    "SELECT count(*),coalesce(sum(hash(trade_date || ':' || source_url || ':' || "
                    "status || ':' || cast(record_count AS VARCHAR) || ':' || "
                    "cast(usable_count AS VARCHAR) || ':' || coalesce(payload_sha256,'') || ':' || "
                    "parser_version)),0),max(retrieved_at) "
                    "FROM futures_settlement_parameter_coverage WHERE exchange='CFFEX'"
                ).fetchone() if has_margin_coverage else None
                listing_version = []
                for table, fields in (
                    ("futures_month_contract_listing_evidence", "product_key || contract_code || listed_on || payload_sha256"),
                    ("futures_product_listing_evidence", "product_key || coalesce(first_trading_day,'') || "
                     "date_precision || evidence_status || payload_sha256"),
                ):
                    present = connection.execute(
                        "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table],
                    ).fetchone()[0]
                    listing_version.append(connection.execute(
                        f"SELECT count(*),coalesce(sum(hash({fields})),0) FROM {table}"
                    ).fetchone() if present else None)
                key = (
                    str(root.resolve()), version, registry_path.stat().st_mtime_ns,
                    cffex_path.stat().st_mtime_ns if cffex_path.exists() else None,
                    calendar_path.stat().st_mtime_ns if calendar_path.exists() else None,
                    rule_path.stat().st_mtime_ns if rule_path.exists() else None,
                    weekly_version, tuple(listing_version), margin_version,
                    margin_coverage_version,
                )
                with _LOCK:
                    core = _CACHE.get(key)
                    if core is None:
                        core = _read_core_snapshot(root, key)
                        if core is None:
                            core = _read_product_days(connection, root, str(latest))
                            core["sourceVersionStamp"] = max(
                                str(version[2] or ""), str(margin_version[2] or "") if margin_version else "",
                            )
                            core["sourceBuiltAtNs"] = time.time_ns()
                            _write_core_snapshot(root, key, core)
                        if len(_CACHE) >= 3:
                            _CACHE.clear()
                        _CACHE[key] = core
                    if metric == "return" and core.get("selectedSources") and not core["tenDayReady"]:
                        _enrich_ten_day_returns(connection, core)
                        core["tenDayReady"] = True
                        _write_core_snapshot(root, key, core)
                    if metric == "deposit":
                        requested_start, requested_end = _selected_range(core, range_name, end_day)
                        cached_start = core.get("depositStart")
                        cached_end = core.get("depositEnd")
                        if (not core.get("depositReady") or not cached_start or not cached_end
                                or requested_start < cached_start or requested_end > cached_end):
                            deposit_start = min(requested_start, cached_start or requested_start)
                            deposit_end = max(requested_end, cached_end or requested_end)
                            _enrich_deposit(connection, core, root, deposit_start, deposit_end)
                            core["depositStart"] = deposit_start
                            core["depositEnd"] = deposit_end
                            core["depositReady"] = True
                            _write_core_snapshot(root, key, core)
                    if metric == "deposit":
                        # Another range can clear and recalculate the shared core
                        # as soon as this lock is released. Assemble from a fixed copy.
                        core = deepcopy(core)
                data_version_source = repr((FORMULA_VERSION, *key[1:]))
            finally:
                store.close()

    start, selected_end = _selected_range(core, range_name, end_day)
    dates = [day for day in core["dates"] if start <= day <= selected_end]
    if not dates:
        return {**empty, "limitations": ["所选日期范围没有已核验交易日。"]}
    all_dates: list[str] = core["dates"]
    day_index = {day: index for index, day in enumerate(all_dates)}
    selected_metric = {"return": "weightedReturn"}.get(metric, _camel(metric))
    products = core["products"]
    direction_calendar_verified = _direction_calendar_verified(root, core) if metric == "return" else False
    short_moves = ({product: _short_move_3_points(all_dates, records, calendar_verified=direction_calendar_verified)
                    for product, records in products.items()} if metric == "return" else {})
    short_ranks, short_rank_statuses, short_sample_counts = (_short_move_rank_points(
        all_dates, core["coverage"], short_moves, calendar_verified=direction_calendar_verified,
    ) if metric == "return" else ({}, [], []))
    metric_coverage = {}
    for day in dates:
        missing_products = sorted(
            product for product, records in products.items()
            if day in records and records[day].get(selected_metric) is None
            and (metric != "return" or records[day].get("openInterest") != 0)
        )
        source_coverage = core["coverage"].get(day, {"tradeDate": day, "missingExchanges": list(_EXCHANGES)})
        complete = core["dayQuality"].get(day) == "PASS" and not missing_products
        metric_coverage[day] = {
            **source_coverage, "status": "PASS" if complete else "PARTIAL",
            "dataQualityStatus": "PASS" if complete else "PARTIAL",
            "missingProducts": missing_products,
        }
    baseline = max(
        (day for day in dates if metric_coverage[day]["status"] == "PASS"),
        default=None,
    )
    ordered = sorted(
        products,
        key=lambda product: (
            -_rank_value(products[product].get(baseline or "", {}).get(selected_metric)), product,
        ),
    )
    result_series = []
    for product in ordered:
        records = products[product]
        values: list[float | None] = []
        xs: list[float | None] = []
        ys: list[float | None] = []
        sizes: list[float | None] = []
        streaks: list[int | None] = []
        ten_day_returns: list[float | None] = []
        ten_day_coverage: list[float | None] = []
        ten_day_window_min: list[int | None] = []
        ten_day_window_max: list[int | None] = []
        short_window_contracts: list[int | None] = []
        deposit_reasons: list[str | None] = []
        missing_margin_contracts: list[int | None] = []
        margin_publication_days: list[str | None] = []
        direction_sums: list[float | None] = []
        direction_up: list[int | None] = []
        direction_down: list[int | None] = []
        direction_flat: list[int | None] = []
        direction_windows: list[int | None] = []
        direction_statuses: list[str] = []
        direction_reasons: list[str | None] = []
        direction_points = (_direction_points(all_dates, records, calendar_verified=direction_calendar_verified)
                            if metric == "return" else [])
        horizontal_cache: dict[int, float | None] = {}

        def horizontal_at(index: int) -> float | None:
            if index < 0:
                return None
            if index not in horizontal_cache:
                horizontal_cache[index] = _horizontal_value(records, all_dates, index, window, metric, selected_metric)
            return horizontal_cache[index]

        for day in dates:
            index = day_index[day]
            point = records.get(day, {})
            daily = point.get(selected_metric)
            value = daily * 100 if metric == "return" and daily is not None else daily
            streak = core["weeklyStreak"].get((day, product))
            y = horizontal_at(index)
            previous = horizontal_at(index - 1)
            x = _daily_horizontal_change(y, previous)
            size = point.get("notional") if metric == "return" else daily
            # Preserve horizontal values, genuine zero areas and missing speed
            # independently.  The client only draws a bubble with all three.
            values.append(value)
            xs.append(x)
            ys.append(y)
            sizes.append(size)
            streaks.append(streak)
            if metric == "deposit":
                deposit_reasons.append(point.get("depositReason"))
                missing_margin_contracts.append(point.get("missingMarginContractCount"))
                margin_publication_days.append(point.get("marginPublicationDay"))
            if metric == "return":
                ten_day_return = point.get("tenDayReturn")
                ten_day_returns.append(ten_day_return * 100 if ten_day_return is not None else None)
                ten_day_coverage.append(point.get("tenDayCoverage"))
                ten_day_window_min.append(point.get("tenDayWindowMin"))
                ten_day_window_max.append(point.get("tenDayWindowMax"))
                short_window_contracts.append(point.get("shortWindowContracts"))
                direction = direction_points[index]
                direction_sums.append(direction["sum"])
                direction_up.append(direction["up"])
                direction_down.append(direction["down"])
                direction_flat.append(direction["flat"])
                direction_windows.append(direction["count"])
                direction_statuses.append(direction["status"])
                direction_reasons.append(direction["reason"])
        exchange, product_code = product.split(".", 1)
        name = futures_product_name(product_code, exchange) or product_code
        result_series.append({
            "key": product, "name": f"{name}（{product_code}）",
            "values": values, "xValues": xs, "yValues": ys,
            "sizeValues": sizes, "weeklyStreakValues": streaks,
            **({"tenDayReturnValues": ten_day_returns,
                "tenDayCoverageValues": ten_day_coverage,
                "tenDayWindowMinValues": ten_day_window_min,
                "tenDayWindowMaxValues": ten_day_window_max,
                "shortWindowContractCountValues": short_window_contracts,
                "directionSumValues": direction_sums,
                "directionUpCountValues": direction_up,
                "directionDownCountValues": direction_down,
                "directionFlatCountValues": direction_flat,
                "directionWindowCountValues": direction_windows,
                "directionStatusValues": direction_statuses,
                "directionReasonValues": direction_reasons,
                "shortMove3Values": [short_moves[product][day_index[day]] for day in dates],
                "shortRankValues": [short_ranks[product][day_index[day]] for day in dates],
                "shortCompareStatusValues": [short_rank_statuses[day_index[day]] for day in dates],
                "shortSampleCountValues": [short_sample_counts[day_index[day]] for day in dates]} if metric == "return" else {}),
            **({"depositReasonValues": deposit_reasons,
                "missingMarginContractCountValues": missing_margin_contracts,
                "marginPublicationDayValues": margin_publication_days} if metric == "deposit" else {}),
        })
    total_points = []
    for day in dates:
        values = [
            records[day].get(selected_metric) for records in products.values()
            if day in records and (metric != "return" or records[day].get("openInterest") != 0)
        ]
        complete = metric_coverage[day]["status"] == "PASS" and values
        if metric == "return":
            active = [value for value in values if value is not None]
            total = 100 * sum(active) / len(active) if complete and active else None
        else:
            total = sum(values) if complete else None
        total_points.append(total)
    coverage = [metric_coverage[day] for day in dates]
    limitations: list[str] = []
    if any(value is None for value in total_points):
        limitations.append("部分交易日来源或计算字段不完整，全部品种合计保留空值。")
    if snapshot_status != "CURRENT":
        limitations.append("官网新数据正在核对，暂显示上次已核验的交易日快照。")
    return {
        "available": bool(result_series), "metric": metric, "unit": _UNITS[metric],
        "dates": dates, "totals": total_points, "series": result_series,
        "baselineDay": baseline, "coverage": coverage,
        "formulaVersion": FORMULA_VERSION, "directionFormulaVersion": DIRECTION_FORMULA_VERSION,
        **({"shortMoveFormulaVersion": SHORT_MOVE_FORMULA_VERSION,
            "shortRankFormulaVersion": SHORT_RANK_FORMULA_VERSION} if metric == "return" else {}),
        "windowTradingDays": window,
        "dataVersion": sha256(data_version_source.encode("utf-8")).hexdigest(),
        "snapshotStatus": snapshot_status,
        "limitations": limitations,
    }


def _selected_range(core: dict[str, Any], range_name: str,
                    end_day: str | None) -> tuple[str, str]:
    selected_end = min(end_day or str(core["latestDay"]), str(core["latestDay"]))
    start = "2023-01-01" if range_name == "all" else (
        date.fromisoformat(selected_end) - timedelta(days=365 * {"1y": 1, "3y": 3, "5y": 5}[range_name])
    ).isoformat()
    return start, selected_end


def _core_snapshot_path(root: Path, key: tuple[Any, ...]) -> Path:
    fingerprint = sha256(repr((FORMULA_VERSION, _CORE_CACHE_SCHEMA, key)).encode("utf-8")).hexdigest()
    return root / "cache" / f"futures-distribution-core-v{_CORE_CACHE_SCHEMA}-{fingerprint}.json.gz"


def _read_core_snapshot(root: Path, key: tuple[Any, ...]) -> dict[str, Any] | None:
    snapshot = _read_snapshot_file(_core_snapshot_path(root, key))
    return deepcopy(snapshot) if snapshot is not None else None


def _read_snapshot_file(path: Path) -> dict[str, Any] | None:
    try:
        cache_key = str(path), path.stat().st_mtime_ns
        with _SNAPSHOT_LOCK:
            cached = _SNAPSHOT_FILE_CACHE.get(cache_key)
            if cached is not None:
                return cached
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                payload = json.load(stream)
            if payload.get("schema") != _CORE_CACHE_SCHEMA or payload.get("formulaVersion") != FORMULA_VERSION:
                return None
            core = payload["core"]
            core["weeklyStreak"] = {
                (day, product): value for day, product, value in core["weeklyStreak"]
            }
            if len(_SNAPSHOT_FILE_CACHE) >= 3:
                _SNAPSHOT_FILE_CACHE.clear()
            _SNAPSHOT_FILE_CACHE[cache_key] = core
            return core
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _snapshot_metadata(path: Path) -> dict[str, Any] | None:
    try:
        cache_key = str(path), path.stat().st_mtime_ns
    except OSError:
        return None
    with _SNAPSHOT_LOCK:
        cached = _SNAPSHOT_META_CACHE.get(cache_key)
        if cached is not None:
            return cached
    core = _read_snapshot_file(path)
    if core is None:
        return None
    dates = core.get("dates") or []
    try:
        metadata = {
            "latestDay": str(core["latestDay"]),
            "firstDay": min(dates) if dates else None,
            "lastDay": max(dates) if dates else None,
            "tenDayReady": bool(core.get("tenDayReady")),
            "depositReady": bool(core.get("depositReady")),
            "depositStart": core.get("depositStart"),
            "depositEnd": core.get("depositEnd"),
            "rank": (str(core["latestDay"]), str(core.get("sourceVersionStamp") or ""),
                     int(core.get("sourceBuiltAtNs") or 0), cache_key[1]),
        }
    except (TypeError, ValueError, KeyError):
        return None
    with _SNAPSHOT_LOCK:
        if len(_SNAPSHOT_META_CACHE) >= 32:
            _SNAPSHOT_META_CACHE.clear()
        _SNAPSHOT_META_CACHE[cache_key] = metadata
    return metadata


def _read_latest_core_snapshot(root: Path, *, require_ten_day: bool,
                               require_deposit: bool = False, range_name: str = "1y",
                               end_day: str | None = None) -> tuple[dict[str, Any], str] | None:
    directory = root / "cache"
    if not directory.is_dir():
        return None
    try:
        candidates = list(directory.glob(f"futures-distribution-core-v{_CORE_CACHE_SCHEMA}-*.json.gz"))
    except OSError:
        return None
    eligible: list[tuple[tuple[str, str, int, int], Path]] = []
    for path in candidates:
        metadata = _snapshot_metadata(path)
        if metadata is None:
            continue
        start, end = _selected_range(metadata, range_name, end_day)
        if ((not require_ten_day or metadata["tenDayReady"])
                and (not require_deposit or (metadata["depositReady"]
                     and (metadata["depositStart"] or metadata["firstDay"] or "9999") <= start
                     and (metadata["depositEnd"] or metadata["lastDay"] or "") >= end))):
            eligible.append((metadata["rank"], path))
    for _, path in sorted(eligible, reverse=True):
        core = _read_snapshot_file(path)
        if core is not None:
            return core, path.name
    return None


def _write_core_snapshot(root: Path, key: tuple[Any, ...], core: dict[str, Any]) -> None:
    path = _core_snapshot_path(root, key)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _CORE_CACHE_SCHEMA,
            "formulaVersion": FORMULA_VERSION,
            "core": {
                **core,
                "weeklyStreak": [
                    [day, product, value] for (day, product), value in core["weeklyStreak"].items()
                ],
            },
        }
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
        snapshots = sorted(path.parent.glob(f"futures-distribution-core-v{_CORE_CACHE_SCHEMA}-*.json.gz"),
                           key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for stale in snapshots[8:]:
            stale.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError):
        temporary.unlink(missing_ok=True)


def _read_product_days(connection: Any, root: Path, latest: str) -> dict[str, Any]:
    """Validate source-day counts, then let DuckDB reduce contract facts."""

    coverage_rows = connection.execute(
        """WITH counts AS (
             SELECT trade_date,exchange,adapter,count(*) AS n,
                    count(DISTINCT payload_sha256) sha_count,
                    min(payload_sha256) payload_sha256
             FROM futures_contract_daily_official
             WHERE parser_version=? AND quality_status='PASS'
             GROUP BY 1,2,3
           )
           SELECT c.trade_date,c.exchange,c.adapter,c.status,c.record_count,c.usable_count,
                  c.parser_version,c.payload_sha256,coalesce(n.n,0),
                  coalesce(n.sha_count,0),n.payload_sha256
           FROM futures_contract_daily_official_coverage c
           LEFT JOIN counts n USING(trade_date,exchange,adapter)
           WHERE c.trade_date>='2023-01-01' AND c.trade_date<=?
             AND c.adapter LIKE 'official-%-daily-v2'
           ORDER BY 1,2,c.retrieved_at DESC""",
        [PARSER_VERSION, latest],
    ).fetchall()
    source: dict[tuple[str, str], tuple[str, str]] = {}
    for day, exchange, adapter, status, count, usable, parser, coverage_sha, actual, sha_count, fact_sha in coverage_rows:
        key = day, exchange
        verified = (
            status == "PASS" and parser == PARSER_VERSION and actual > 0
            and actual == count == usable and sha_count == 1
            and coverage_sha == fact_sha
        )
        if key not in source or (verified and source[key][1] != "PASS"):
            source[key] = (adapter, "PASS" if verified else "FAILED")
    selected = [(day, exchange, adapter) for (day, exchange), (adapter, status) in source.items() if status == "PASS"]
    if not selected:
        return {"latestDay": latest, "dates": [], "dayQuality": {}, "coverage": {}, "products": {}, "weeklyStreak": {}}
    try:
        calendar = load_futures_trading_calendar(root)
    except (OSError, ValueError, TypeError):
        calendar = None
    source_days = {str(day) for day, _, _ in selected}
    calendar_days = set(calendar.trading_days) if calendar else set()
    # Observed dates cannot replace a complete calendar when checking a rolling
    # window. A source day outside it means the entire date basis needs review.
    calendar_verified = bool(calendar_days and source_days and latest in calendar_days
                             and source_days <= calendar_days)
    dates = (calendar.between("2023-01-01", latest) if calendar_verified and calendar
             else sorted({str(day) for day, _ in source}))
    # A temporary relation is connection-local; official source rows remain
    # immutable and each product/day aggregation executes inside DuckDB.
    connection.execute("CREATE OR REPLACE TEMP TABLE _market_distribution_sources (trade_date VARCHAR,exchange VARCHAR,adapter VARCHAR)")
    connection.executemany("INSERT INTO _market_distribution_sources VALUES (?,?,?)", selected)
    rows = connection.execute(
        """SELECT f.trade_date,f.exchange,f.product_code,
                  count(*) contract_count,
                  sum(CASE WHEN f.open_interest IS NULL OR f.open_interest<0 OR
                    f.open_interest_sidedness<>'ONE_SIDED' THEN 1 ELSE 0 END) bad_oi,
                  sum(f.open_interest) oi,
                  sum(CASE WHEN f.volume IS NULL OR f.volume<0 OR
                    f.volume_sidedness<>'ONE_SIDED' THEN 1 ELSE 0 END) bad_volume,
                  sum(f.volume) volume,
                  sum(CASE WHEN f.turnover_cny IS NULL OR f.turnover_cny<0 OR
                    f.turnover_sidedness<>'ONE_SIDED' OR f.turnover_quality_status<>'PASS'
                    THEN 1 ELSE 0 END) bad_turnover,
                  sum(f.turnover_cny) turnover,
                  sum(CASE WHEN f.open_interest>0 AND (f.settlement IS NULL OR
                    f.settlement<=0) THEN 1 ELSE 0 END) bad_notional,
                  sum(CASE WHEN f.open_interest>0 THEN f.open_interest*f.settlement ELSE 0 END) settlement_oi,
                  sum(CASE WHEN f.open_interest>0 AND (f.settlement IS NULL OR
                    f.settlement<=0 OR f.previous_settlement IS NULL OR
                    f.previous_settlement<=0) THEN 1 ELSE 0 END) bad_return,
                  sum(CASE WHEN f.open_interest>0 AND f.settlement>0 AND
                    f.previous_settlement>0 THEN f.open_interest*(f.settlement/f.previous_settlement-1)
                    ELSE 0 END) weighted_return_sum
           FROM futures_contract_daily_official f
           JOIN _market_distribution_sources s USING(trade_date,exchange,adapter)
           WHERE f.parser_version=? AND f.quality_status='PASS'
           GROUP BY 1,2,3 ORDER BY 1,2,3""",
        [PARSER_VERSION],
    ).fetchall()
    registry = load_official_multiplier_registry()
    rule_book = load_rule_book(root)
    products: dict[str, dict[str, dict[str, float | None]]] = {}
    for (day, exchange, product, _, bad_oi, oi, bad_volume, volume,
         bad_turnover, turnover, bad_notional, settlement_oi,
         bad_return, weighted_return_sum) in rows:
        key = f"{exchange}.{product}"
        official = registry.resolve(day, exchange, product)
        multiplier = official.contract_multiplier if official else None
        if exchange == "CFFEX" and multiplier is None:
            multiplier = rule_book.resolve_multiplier(day, exchange, product)
        valid_oi = float(oi) if bad_oi == 0 and _finite_nonnegative(oi) else None
        products.setdefault(key, {})[day] = {
            "openInterest": valid_oi,
            "volume": float(volume) if bad_volume == 0 and _finite_nonnegative(volume) else None,
            "turnover": float(turnover) if bad_turnover == 0 and _finite_nonnegative(turnover) else None,
            "weightedSettlement": (
                float(settlement_oi) / valid_oi
                if valid_oi is not None and valid_oi > 0 and bad_notional == 0 else None
            ),
            "notional": (
                0.0 if valid_oi == 0 else float(settlement_oi) * multiplier
                if valid_oi is not None and bad_notional == 0 and multiplier is not None else None
            ),
            "weightedReturn": (
                float(weighted_return_sum) / valid_oi
                if valid_oi is not None and valid_oi > 0 and bad_return == 0 else None
            ),
        }
    gold_streak = _gold_product_streaks(connection, dates, products)
    weekly_streak = gold_streak if gold_streak is not None else (
        product_weekly_streaks_from_daily(
            {
                product: {day: values.get("weightedReturn") for day, values in records.items()}
                for product, records in products.items()
            },
            [day for day in calendar.trading_days if day >= "2023-01-01"],
            latest,
        )
        if calendar else {}
    )
    day_quality = {}
    coverage = {}
    for day in dates:
        missing = [exchange for exchange in _EXCHANGES if source.get((day, exchange), (None, "FAILED"))[1] != "PASS"]
        status = "PASS" if not missing else "PARTIAL"
        day_quality[day] = status
        coverage[day] = {"tradeDate": day, "status": status, "missingExchanges": missing}
    return {
        "latestDay": latest, "dates": dates, "dayQuality": day_quality,
        "coverage": coverage, "products": products, "weeklyStreak": weekly_streak,
        "selectedSources": selected, "tenDayReady": False, "depositReady": False,
        "calendarVerified": calendar_verified,
        "calendarFingerprint": _calendar_fingerprint(calendar) if calendar_verified else None,
    }


def _enrich_deposit(connection: Any, core: dict[str, Any], root: Path,
                    start_day: str, end_day: str) -> None:
    """Sum each month's two-sided official speculative margin at its valid date."""
    products = core.get("products", {})
    if not products:
        return
    for product_key, records in products.items():
        for point in records.values():
            point["deposit"] = None
            point["depositReason"] = ("缺所选日适用的中金所最近公布投机保证金率"
                                      if product_key.startswith("CFFEX.") else "缺同日有效投机保证金率")
            point["missingMarginContractCount"] = None
            if point.get("openInterest") == 0:
                point["deposit"] = 0.0
                point["depositReason"] = None
                point["missingMarginContractCount"] = 0
    present = connection.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name='futures_settlement_parameter_official'"
    ).fetchone()[0]
    if not present or not core.get("selectedSources"):
        return
    connection.execute("CREATE OR REPLACE TEMP TABLE _market_distribution_sources "
                       "(trade_date VARCHAR,exchange VARCHAR,adapter VARCHAR)")
    connection.executemany("INSERT INTO _market_distribution_sources VALUES (?,?,?)",
                           core["selectedSources"])
    publications = verified_publications(connection, SETTLEMENT_PARSER_VERSION)
    publication_days = sorted({day for day, _, _ in publications})
    known_days = known_publication_days(connection, SETTLEMENT_PARSER_VERSION)
    selected_days = sorted({str(day) for day, _, _ in core["selectedSources"]
                            if start_day <= str(day) <= end_day})
    connection.execute("CREATE OR REPLACE TEMP TABLE _cffex_margin_asof_days "
                       "(trade_date VARCHAR,publication_day VARCHAR)")
    connection.executemany(
        "INSERT INTO _cffex_margin_asof_days VALUES (?,?)",
        [(day, applicable_publication_on_or_before(publication_days, known_days, day))
         for day in selected_days],
    )
    connection.execute("CREATE OR REPLACE TEMP TABLE _cffex_verified_publications "
                       "(trade_date VARCHAR,source_url VARCHAR,payload_sha256 VARCHAR)")
    if publications:
        connection.executemany("INSERT INTO _cffex_verified_publications VALUES (?,?,?)", publications)
    connection.execute(
        """CREATE OR REPLACE TEMP TABLE _cffex_margin_for_day AS
           SELECT applied.trade_date AS applied_day,'CFFEX' AS exchange,
                  publication.trade_date AS publication_day,
                  rates.contract_code,rates.spec_long_rate,rates.spec_short_rate
           FROM _cffex_margin_asof_days applied
           JOIN _cffex_verified_publications publication
             ON publication.trade_date=applied.publication_day
           JOIN futures_settlement_parameter_official rates
             ON rates.trade_date=publication.trade_date
            AND rates.exchange='CFFEX' AND rates.source_url=publication.source_url
            AND rates.payload_sha256=publication.payload_sha256
            AND rates.parser_version=? AND rates.quality_status='PASS'
            AND (rates.interval_source_url IS NULL OR
                 (rates.effective_from<=applied.trade_date
                  AND rates.effective_to>=applied.trade_date
                  AND length(rates.interval_payload_sha256)=64))
           QUALIFY row_number() OVER (
             PARTITION BY applied.trade_date,rates.contract_code
             ORDER BY rates.retrieved_at DESC,rates.source_url
           )=1""",
        [SETTLEMENT_PARSER_VERSION],
    )
    connection.execute(
        """CREATE OR REPLACE TEMP TABLE _noncffex_daily_margin AS
           SELECT trade_date,exchange,contract_code,spec_long_rate,spec_short_rate,
                  effective_from,effective_to,retrieved_at,source_url
           FROM futures_settlement_parameter_official
           WHERE exchange<>'CFFEX' AND quality_status='PASS' AND parser_version=?
             AND length(payload_sha256)=64 AND trade_date BETWEEN ? AND ?""",
        [SETTLEMENT_PARSER_VERSION, start_day, end_day],
    )
    rows = connection.execute(
        """WITH candidates AS (
             SELECT f.trade_date,f.exchange,f.product_code,f.contract_code,
                    f.open_interest,f.settlement,
                    coalesce(daily.spec_long_rate,cffex.spec_long_rate) spec_long_rate,
                    coalesce(daily.spec_short_rate,cffex.spec_short_rate) spec_short_rate,
                    publication_map.publication_day AS margin_publication_day,
                    row_number() OVER (
                      PARTITION BY f.trade_date,f.exchange,f.product_code,f.contract_code
                      ORDER BY daily.retrieved_at DESC,daily.source_url
                    ) AS chosen
             FROM futures_contract_daily_official f
             JOIN _market_distribution_sources s USING(trade_date,exchange,adapter)
             LEFT JOIN _noncffex_daily_margin daily
               ON daily.trade_date=f.trade_date AND daily.exchange=f.exchange
              AND daily.contract_code=f.contract_code
              AND daily.effective_from<=f.trade_date AND daily.effective_to>=f.trade_date
             LEFT JOIN _cffex_margin_asof_days publication_map
               ON publication_map.trade_date=f.trade_date
             LEFT JOIN _cffex_margin_for_day cffex
               ON cffex.applied_day=f.trade_date AND cffex.exchange=f.exchange
              AND cffex.contract_code=f.contract_code
             WHERE f.parser_version=? AND f.quality_status='PASS'
               AND f.trade_date BETWEEN ? AND ?
           ), grouped AS (
             SELECT trade_date,exchange,product_code,
                    max(margin_publication_day) AS margin_publication_day,
                    sum(CASE WHEN open_interest>0 AND
                      (spec_long_rate IS NULL OR spec_short_rate IS NULL OR
                       NOT isfinite(spec_long_rate) OR NOT isfinite(spec_short_rate) OR
                       spec_long_rate<0 OR spec_long_rate>1 OR
                       spec_short_rate<0 OR spec_short_rate>1)
                      THEN 1 ELSE 0 END) AS missing_margin_contracts,
                    sum(CASE WHEN open_interest>0 AND settlement>0 AND
                      isfinite(settlement) AND
                      spec_long_rate>=0 AND spec_long_rate<=1 AND
                      spec_short_rate>=0 AND spec_short_rate<=1 AND
                      isfinite(spec_long_rate) AND isfinite(spec_short_rate)
                      THEN open_interest*settlement*(spec_long_rate+spec_short_rate)
                      ELSE 0 END) AS deposit_factor
             FROM candidates WHERE chosen=1 GROUP BY 1,2,3
           ) SELECT trade_date,exchange,product_code,
                    missing_margin_contracts,deposit_factor,margin_publication_day FROM grouped""",
        [PARSER_VERSION, start_day, end_day],
    ).fetchall()
    registry = load_official_multiplier_registry()
    rule_book = load_rule_book(root)
    for day, exchange, product, missing_margin, factor, margin_publication_day in rows:
        point = products.get(f"{exchange}.{product}", {}).get(day)
        if point is None or point["deposit"] == 0:
            continue
        if exchange == "CFFEX":
            point["marginPublicationDay"] = margin_publication_day
        missing = int(missing_margin or 0)
        point["missingMarginContractCount"] = missing
        if point.get("notional") is None:
            point["depositReason"] = "缺同日有效结算价、持仓或合约乘数"
            continue
        if missing:
            point["depositReason"] = f"{missing}个有持仓月份合约缺所选日适用的中金所最近公布投机保证金率" if exchange == "CFFEX" else f"{missing}个有持仓月份合约缺同日有效投机保证金率"
            continue
        official = registry.resolve(day, exchange, product)
        multiplier = official.contract_multiplier if official else None
        if exchange == "CFFEX" and multiplier is None:
            multiplier = rule_book.resolve_multiplier(day, exchange, product)
        if multiplier is None or factor is None:
            point["depositReason"] = "缺同日有效合约乘数或保证金计算值"
            continue
        point["deposit"] = float(factor) * multiplier
        point["depositReason"] = None


def _enrich_ten_day_returns(connection: Any, core: dict[str, Any]) -> None:
    """Compare endpoints of the complete product's daily OI-weighted settlement.

    Every effective month contributes on its own trading day. A month rollover
    changes the weighted series composition, but never hides an otherwise
    complete product merely because the new month lacks nine older observations.
    """
    verified_listings = {
        (str(key), str(day)) for key, day in connection.execute(
            "SELECT product_key,first_trading_day FROM futures_product_listing_evidence "
            "WHERE evidence_status='PASS' AND date_precision='DAY' "
            "AND length(payload_sha256)=64 AND first_trading_day IS NOT NULL"
        ).fetchall()
    }
    dates: list[str] = core["dates"]
    for product_key, records in core["products"].items():
        first_index = next((index for index, day in enumerate(dates) if day in records), None)
        if first_index is None:
            continue
        listed = first_index > 0 and (product_key, dates[first_index]) in verified_listings
        for index, day in enumerate(dates):
            point = records.get(day)
            if point is None:
                continue
            start = max(first_index, index - 9)
            window_length = index - start + 1
            if window_length < 10 and not listed:
                continue
            prices = [records.get(window_day, {}).get("weightedSettlement")
                      for window_day in dates[start:index + 1]]
            if len(prices) != window_length or any(
                not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0
                for price in prices
            ):
                continue
            point["tenDayReturn"] = prices[-1] / prices[0] - 1
            point["tenDayCoverage"] = 1.0
            point["tenDayWindowMin"] = window_length
            point["tenDayWindowMax"] = window_length


def _gold_product_streaks(
    connection: Any, days: list[str], products: dict[str, Any],
) -> dict[tuple[str, str], int | None] | None:
    exists = connection.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name='futures_weekly_heat_product_gold'"
    ).fetchone()[0]
    if not exists:
        return None
    rows = connection.execute(
        "SELECT week_end_date,product_key,payload_json FROM futures_weekly_heat_product_gold "
        "WHERE formula_version=? ORDER BY week_end_date,product_key",
        [WEEKLY_HEAT_FORMULA_VERSION],
    ).fetchall()
    if not rows:
        return None
    events: dict[str, dict[str, int | None]] = {}
    for end, product, payload in rows:
        value = json.loads(payload).get("streakWeeks")
        if product in products:
            events.setdefault(str(end), {})[str(product)] = int(value) if value is not None else None
    active: dict[str, int | None] = {}
    result: dict[tuple[str, str], int | None] = {}
    for day in days:
        if day in events:
            active = events[day]
        for product in products:
            result[day, product] = active.get(product)
    return result


def _horizontal_value(
    records: dict[str, dict[str, float | None]], days: list[str], index: int,
    window: int, metric: str, field: str,
) -> float | None:
    if index < 0:
        return None
    if metric == "return":
        daily_return = records.get(days[index], {}).get(field)
        return daily_return * 100 if daily_return is not None else None
    if metric in {"turnover", "volume"}:
        return _activity_change(records, days, index, window, field)
    return _change(records, days, index, window, field)


def _daily_horizontal_change(current: float | None, previous: float | None) -> float | None:
    return current - previous if current is not None and previous is not None else None


def _change(records: dict[str, dict[str, float | None]], days: list[str], index: int, window: int, field: str) -> float | None:
    if index < window:
        return None
    current = records.get(days[index], {}).get(field)
    previous = records.get(days[index - window], {}).get(field)
    if current is None or previous is None or previous <= 0:
        return None
    return (current / previous - 1) * 100


def _activity_change(records: dict[str, dict[str, float | None]], days: list[str], index: int, window: int, field: str) -> float | None:
    if index < 2 * window - 1:
        return None
    current = [records.get(day, {}).get(field) for day in days[index - window + 1:index + 1]]
    previous = [records.get(day, {}).get(field) for day in days[index - 2 * window + 1:index - window + 1]]
    if any(value is None for value in current + previous):
        return None
    before = sum(previous) / window
    return (sum(current) / window / before - 1) * 100 if before > 0 else None


def _finite_nonnegative(value: Any) -> bool:
    return value is not None and math.isfinite(float(value)) and float(value) >= 0


def _rank_value(value: float | None) -> float:
    return float(value) if value is not None and math.isfinite(value) else float("-inf")


def _camel(metric: str) -> str:
    return "openInterest" if metric == "open_interest" else metric


__all__ = ("FORMULA_VERSION", "market_distributions")
