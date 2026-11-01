"""Contract-count and exchange-turnover heat; deposited funds stay undivided."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
import math
from typing import Any, Mapping, Sequence

from .futures_rule_sync import RuleBook
from .futures_official_daily_sources import PARSER_VERSION


FORMULA_VERSION = "2026-10-official-daily-heat-v3"
LOOKBACK_DAYS = 10
HALF_LIFE_DAYS = 3.0
MIN_TURNOVER_COVERAGE = 0.8
MIN_DEPOSIT_COVERAGE = 0.8
EXCLUDED_EXCHANGES = frozenset()
CALCULATION_METHOD = (
    "全部国内有成交期货合约含中金所和月均价期货，按官网今收盘减昨结算判断方向；"
    "数量热度=(上涨合约数-下跌合约数)/(上涨+下跌合约数)*100；"
    "成交额热度=(上涨合约成交额-下跌合约成交额)/(上涨+下跌成交额)*100；"
    "成交额使用交易所公布的单边人民币值；沉淀资金只保留整体，不按涨跌分桶；"
    "最近10个有观测交易日按半衰期3日指数衰减，缺失日不补零"
)


def _positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _non_negative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _smooth(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    if not rows or rows[-1][field] is None:
        return None
    weighted = [
        (float(row[field]), 2 ** (-lag / HALF_LIFE_DAYS))
        for lag, row in enumerate(reversed(rows[-LOOKBACK_DAYS:]))
        if row[field] is not None
    ]
    total = sum(weight for _, weight in weighted)
    return sum(value * weight for value, weight in weighted) / total if total else None


def calculate_contract_turnover_heat(
    bars: Sequence[Mapping[str, Any]],
    official_rows: Sequence[Mapping[str, Any]],
    coverage_rows: Sequence[Mapping[str, Any]],
    *,
    rule_book: RuleBook | None = None,
) -> list[dict[str, Any]]:
    """Return one factual point per observed contract day without using future prices."""

    official = {
        (str(row["trade_date"]), str(row["exchange"]), str(row["contract_code"])): row
        for row in official_rows if row.get("quality_status") == "PASS"
        and row.get("parser_version") == PARSER_VERSION
        and str(row.get("adapter") or "").endswith("-daily-v2")
    }
    coverage: dict[tuple[str, str], str] = {}
    for row in coverage_rows:
        if not str(row.get("adapter") or "").endswith("-daily-v2"):
            continue
        key = str(row["trade_date"]), str(row["exchange"])
        if coverage.get(key) != "PASS":
            coverage[key] = str(row["status"])
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for bar in official.values():
        if str(bar["exchange"]) not in EXCLUDED_EXCHANGES:
            by_day[str(bar["trade_date"])].append(bar)
    for day, _venue in coverage:
        by_day.setdefault(day, [])
    results: list[dict[str, Any]] = []
    for trade_day in sorted(by_day):
        contracts = [bar for bar in by_day[trade_day] if _positive(bar.get("volume")) is not None]
        directions: list[tuple[Mapping[str, Any], str | None]] = []
        for bar in sorted(contracts, key=lambda item: (str(item["exchange"]), str(item["contract_code"]))):
            close = _positive(bar.get("close"))
            prior = _positive(bar.get("previous_settlement"))
            direction = None if close is None or prior is None else (
                "UP" if close > prior else "DOWN" if close < prior else "FLAT"
            )
            directions.append((bar, direction))
        total_contracts = len(directions)
        valid = [(bar, direction) for bar, direction in directions if direction is not None]
        up = sum(direction == "UP" for _, direction in valid)
        down = sum(direction == "DOWN" for _, direction in valid)
        flat = sum(direction == "FLAT" for _, direction in valid)
        amounts = {"UP": 0.0, "DOWN": 0.0, "FLAT": 0.0}
        amount_count = 0
        deposit_values: list[float] = []
        for bar, direction in valid:
            key = (trade_day, str(bar["exchange"]), str(bar["contract_code"]))
            observation = official.get(key)
            turnover = _non_negative(observation.get("turnover_cny")) if observation else None
            if (
                turnover is not None and observation is not None
                and observation.get("turnover_sidedness") == "ONE_SIDED"
                and observation.get("turnover_quality_status") == "PASS"
            ):
                amounts[str(direction)] += turnover
                amount_count += 1
        if rule_book is not None:
            for bar, _direction in directions:
                rule = rule_book.resolve(
                    date.fromisoformat(trade_day), str(bar["exchange"]),
                    str(bar["product_code"]), str(bar["contract_code"]),
                )
                settlement = _positive(bar.get("settlement"))
                open_interest = _non_negative(bar.get("open_interest"))
                if rule is not None and settlement is not None and open_interest is not None:
                    deposit_values.append(
                        2 * settlement * rule.contract_multiplier * open_interest * rule.margin_rate
                    )
        directional_count = up + down
        count_score = (up - down) / directional_count * 100 if directional_count else None
        turnover_coverage = amount_count / len(valid) if valid else 0.0
        directional_turnover = amounts["UP"] + amounts["DOWN"]
        # Keep the verified official-source sample visible when coverage is
        # partial.  Coverage still controls PASS/PARTIAL quality status below;
        # missing exchange rows are never treated as zero turnover.
        turnover_score = (
            (amounts["UP"] - amounts["DOWN"]) / directional_turnover * 100
            if directional_turnover > 0 else None
        )
        deposit_coverage = len(deposit_values) / total_contracts if total_contracts else 0.0
        total_deposit = (
            sum(deposit_values) if deposit_coverage >= MIN_DEPOSIT_COVERAGE and deposit_values else None
        )
        expected_exchanges = {"SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"}
        missing_exchanges = sorted(
            exchange for exchange in expected_exchanges if coverage.get((trade_day, exchange)) != "PASS"
        )
        daily = {
            "formula_version": FORMULA_VERSION,
            "trade_date": trade_day,
            "total_contract_count": total_contracts,
            "valid_contract_count": len(valid),
            "missing_direction_count": total_contracts - len(valid),
            "up_contract_count": up,
            "down_contract_count": down,
            "flat_contract_count": flat,
            "turnover_valid_contract_count": amount_count,
            "turnover_missing_contract_count": len(valid) - amount_count,
            "up_turnover_cny": amounts["UP"],
            "down_turnover_cny": amounts["DOWN"],
            "flat_turnover_cny": amounts["FLAT"],
            "total_deposit_cny": total_deposit,
            "direction_coverage": len(valid) / total_contracts if total_contracts else 0.0,
            "turnover_coverage": turnover_coverage,
            "deposit_coverage": deposit_coverage,
            "count_score_daily": count_score,
            "turnover_score_daily": turnover_score,
            "count_score_10d": None,
            "turnover_score_10d": None,
            "divergence": None,
            "is_warmup": False,
            "data_quality_status": "UNAVAILABLE",
            "missing_exchanges_json": json.dumps(missing_exchanges, ensure_ascii=False),
            "source_cutoff": trade_day,
            "calculation_method": CALCULATION_METHOD,
            "calculated_at": "",
        }
        results.append(daily)
        window = results[-LOOKBACK_DAYS:]
        daily["count_score_10d"] = _smooth(window, "count_score_daily")
        daily["turnover_score_10d"] = _smooth(window, "turnover_score_daily")
        if daily["count_score_10d"] is not None and daily["turnover_score_10d"] is not None:
            daily["divergence"] = daily["count_score_10d"] - daily["turnover_score_10d"]
        daily["is_warmup"] = len(window) < LOOKBACK_DAYS
        daily["data_quality_status"] = (
            "UNAVAILABLE" if count_score is None else
            "PASS" if turnover_score is not None and not missing_exchanges
            and daily["missing_direction_count"] == 0
            and daily["turnover_missing_contract_count"] == 0
            and not daily["is_warmup"]
            else "PARTIAL"
        )
    return results


__all__ = (
    "FORMULA_VERSION", "LOOKBACK_DAYS", "HALF_LIFE_DAYS", "MIN_TURNOVER_COVERAGE",
    "EXCLUDED_EXCHANGES", "CALCULATION_METHOD", "calculate_contract_turnover_heat",
)
