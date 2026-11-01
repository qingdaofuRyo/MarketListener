"""Pure official-futures weekly heat and descriptive product return calculations.

Only verified source days and completed calendar weeks enter the result. This
module owns no database connection and never changes the archived heat Gold.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
import re
from typing import Any, Mapping, Sequence

from .futures_official_daily_quality import validated_daily_inputs


FORMULA_VERSION = "2026-10-daily-three-component-heat-v8"
EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")
WEEK_RETURN_SCALE = 0.05
FIXED_WEIGHTS = {"breadth": 0.20, "streak": 0.60, "magnitude": 0.20}
_PRODUCT = re.compile(r"^(SHFE|INE|DCE|CZCE|GFEX|CFFEX)\.([A-Z]{1,3}(?:-F)?)$")
_CONTRACT = re.compile(r"^[A-Z]{1,3}(?:-F)?\d{3,4}$")
_CFFEX_PRODUCTS = {"IF", "IH", "IC", "IM", "TS", "TF", "T", "TL"}


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and (value > 0 if positive else value >= 0) else None


def _sign(value: float) -> int:
    if math.isclose(value, 0.0, abs_tol=1e-12):
        return 0
    return 1 if value > 0 else -1 if value < 0 else 0


def _next_streak(previous: int | None, direction: int) -> int:
    if not direction:
        return 0
    if previous is not None and previous * direction > 0:
        return previous + direction
    return direction


def trend_weight_log2(streak: int) -> int:
    """An exact unbounded exponent: consecutive weeks 1/2/3/4 mean 1/2/4/8."""

    return max(abs(streak) - 1, 0)


def normalize_log_weights(log_weights: Sequence[float]) -> list[float]:
    """Normalize without materializing exponentially growing multipliers."""

    if not log_weights:
        return []
    if any(not math.isfinite(value) for value in log_weights):
        raise ValueError("log weights must be finite")
    maximum = max(log_weights)
    scaled = [math.exp(value - maximum) for value in log_weights]
    total = math.fsum(scaled)
    return [value / total for value in scaled]


def _calendar_weeks(calendar_days: Sequence[str]) -> list[list[str]]:
    grouped: dict[tuple[int, int], list[str]] = defaultdict(list)
    for day in sorted(set(calendar_days)):
        parsed = date.fromisoformat(day)
        year, week, _ = parsed.isocalendar()
        grouped[(year, week)].append(day)
    return list(grouped.values())


def product_weekly_streaks_from_daily(
    product_daily_returns_by_day: Mapping[str, Mapping[str, float | None]],
    calendar_days: Sequence[str], as_of_day: str,
) -> dict[tuple[str, str], int | None]:
    """Map completed product price-chain weekly streaks onto each trading day.

    These are descriptive product-chain returns, with the caller's current-OI
    daily weighting. Missing daily returns break the week and subsequent streak;
    an unfinished week keeps the preceding completed week's state.
    """

    date.fromisoformat(as_of_day)
    all_weeks = _calendar_weeks(calendar_days)
    source_days = [day for values in product_daily_returns_by_day.values() for day in values]
    if not source_days:
        return {}
    first_day = min(source_days)
    all_weeks = [days for days in all_weeks if days[-1] >= first_day]
    visible = sorted(day for day in set(calendar_days) if first_day <= day <= as_of_day)
    result: dict[tuple[str, str], int | None] = {}
    for product, values in product_daily_returns_by_day.items():
        if _PRODUCT.fullmatch(product) is None:
            raise ValueError(f"invalid product key: {product}")
        ends: dict[str, int | None] = {}
        state: int | None = None
        for days in all_weeks:
            if days[-1] > as_of_day:
                break
            returns = [values.get(day) for day in days]
            if any(value is None or isinstance(value, bool) or not math.isfinite(value) or value <= -1 for value in returns):
                state = None
            else:
                # Only the sign of the compounded return is required here;
                # log growth preserves it even for an extreme price chain.
                log_growth = math.fsum(math.log1p(value) for value in returns)
                state = _next_streak(state, _sign(log_growth))
            ends[days[-1]] = state
        active: int | None = None
        for day in visible:
            if day in ends:
                active = ends[day]
            result[(day, product)] = active
    return result


def _prepare_inputs(
    facts: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]],
    calendar_days: Sequence[str], as_of_day: str,
) -> tuple[dict[str, dict[tuple[str, str], dict[str, Any]]], dict[tuple[str, str], bool]]:
    allowed_days = {day for day in calendar_days if day <= as_of_day}
    rows, source_days = validated_daily_inputs(facts, coverage)
    by_day: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        day = str(row["trade_date"])
        venue = str(row["exchange"]).upper()
        product = str(row["product_code"]).upper()
        contract = str(row["contract_code"]).upper()
        if day not in allowed_days or venue not in EXCHANGES:
            continue
        if row.get("adapter") != f"official-{venue.lower()}-daily-v2":
            continue
        if _PRODUCT.fullmatch(f"{venue}.{product}") is None or _CONTRACT.fullmatch(contract) is None:
            continue
        if venue == "CFFEX" and product not in _CFFEX_PRODUCTS:
            continue
        if contract[:-4] != product and contract[:-3] != product:
            continue
        identity = venue, contract
        if identity in by_day[day]:
            raise ValueError(f"multiple official daily sources for {venue}.{contract} on {day}")
        by_day[day][identity] = row
    sources = {
        (str(row["trade_date"]), str(row["exchange"]).upper()): row["status"] == "PASS"
        for row in source_days if str(row["trade_date"]) in allowed_days
        and row.get("adapter") == f"official-{str(row['exchange']).lower()}-daily-v2"
    }
    return dict(by_day), sources


def _weekly_context(
    facts: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]],
    calendar_days: Sequence[str], as_of_day: str,
    seed_prices: Mapping[tuple[str, str, str], float] | None = None,
) -> dict[str, Any]:
    date.fromisoformat(as_of_day)
    by_day, sources = _prepare_inputs(facts, coverage, calendar_days, as_of_day)
    all_weeks = _calendar_weeks(calendar_days)
    seed_prices = seed_prices or {}
    known_days = sorted(day for day, _venue in sources)
    if not known_days:
        return {"weeks": [], "lookup": {}, "byDay": by_day, "sources": sources}
    first_day = known_days[0]
    state: dict[tuple[str, str], int | None] = {}
    lookup: dict[tuple[str, str, str], int | None] = {}
    prepared = []
    for position, days in enumerate(all_weeks):
        end = days[-1]
        if end < first_day:
            continue
        if end > as_of_day:
            break
        prior_end = all_weeks[position - 1][-1] if position else None
        current = by_day.get(end, {})
        prior = by_day.get(prior_end, {}) if prior_end else {}
        missing = sorted({venue for venue in EXCHANGES if any(not sources.get((day, venue), False) for day in days)})
        previous_missing = sorted(venue for venue in EXCHANGES if prior_end is None or not sources.get((prior_end, venue), False))
        seeded_venues = {venue for seed_day, venue, _contract in seed_prices if seed_day == prior_end}
        previous_missing = [venue for venue in previous_missing if venue not in seeded_venues]
        contracts: dict[tuple[str, str], dict[str, Any]] = {}
        derived_seeds: list[str] = []
        for identity, row in current.items():
            venue, code = identity
            previous = prior.get(identity)
            if previous is None and prior_end is not None:
                derived_price = seed_prices.get((prior_end, venue, code))
                if derived_price is not None:
                    previous = {"settlement": derived_price}
                    derived_seeds.append(f"{venue}.{code}")
            value = None
            reason = None
            if venue in missing:
                reason = "SOURCE_GAP"
            elif venue in previous_missing:
                reason = "FIRST_WEEK_SEED" if prior_end is None or prior_end < first_day else "SOURCE_GAP"
            elif previous is None:
                reason = "NEW_CONTRACT_SEED"
            else:
                settlement = _number(row.get("settlement"), positive=True)
                previous_settlement = _number(previous.get("settlement"), positive=True)
                if settlement is None or previous_settlement is None:
                    reason = "MISSING_SETTLEMENT"
                else:
                    value = settlement / previous_settlement - 1
                    if not math.isfinite(value):
                        value, reason = None, "INVALID_RETURN"
            streak = _next_streak(state.get(identity), _sign(value)) if value is not None else None
            state[identity] = streak
            lookup[(end, venue, code)] = streak
            week_rows = [by_day.get(day, {}).get(identity) for day in days]
            # A newly listed contract has no row on earlier days of its first
            # week. Complete venue reports establish that absence as a seed.
            published = [item for item in week_rows if item is not None]
            volume_complete = all(item.get("volume_sidedness") == "ONE_SIDED" and _number(item.get("volume")) is not None for item in published)
            turnover_complete = all(
                item.get("turnover_sidedness") == "ONE_SIDED" and item.get("turnover_quality_status") == "PASS"
                and _number(item.get("turnover_cny")) is not None for item in published
            )
            contracts[identity] = {
                "productKey": f"{venue}.{row['product_code']}", "contractCode": code,
                "weeklyReturn": value, "streakWeeks": streak,
                "weightLog2": trend_weight_log2(streak) if streak is not None else None,
                "openInterest": _number(row.get("open_interest")) if row.get("open_interest_sidedness") == "ONE_SIDED" else None,
                "weeklyVolume": math.fsum(float(item["volume"]) for item in published) if volume_complete else None,
                "weeklyTurnover": math.fsum(float(item["turnover_cny"]) for item in published) if turnover_complete else None,
                "reason": reason, "existsAtWeekEnd": True,
            }
        # A contract can expire during this trading week. Its real turnover is
        # still part of the amount denominator, but its last earlier quotation
        # is not a substitute for the specified calendar week-end settlement.
        week_identities = {identity for day in days for identity in by_day.get(day, {})}
        for identity in sorted(week_identities - current.keys()):
            published = [by_day[day][identity] for day in days if identity in by_day.get(day, {})]
            row = published[-1]
            turnover_complete = all(
                item.get("turnover_sidedness") == "ONE_SIDED" and item.get("turnover_quality_status") == "PASS"
                and _number(item.get("turnover_cny")) is not None for item in published
            )
            volume_complete = all(item.get("volume_sidedness") == "ONE_SIDED" and _number(item.get("volume")) is not None for item in published)
            contracts[identity] = {
                "productKey": f"{identity[0]}.{row['product_code']}", "contractCode": identity[1],
                "weeklyReturn": None, "streakWeeks": None, "weightLog2": None, "openInterest": None,
                "weeklyVolume": math.fsum(float(item["volume"]) for item in published) if volume_complete else None,
                "weeklyTurnover": math.fsum(float(item["turnover_cny"]) for item in published) if turnover_complete else None,
                "reason": "SOURCE_GAP" if identity[0] in missing else "NO_WEEK_END_SETTLEMENT",
                "existsAtWeekEnd": False,
            }
            state[identity] = None
            lookup[(end, identity[0], identity[1])] = None
        for venue in missing:
            for identity in list(state):
                if identity[0] == venue:
                    state[identity] = None
                    lookup[(end, identity[0], identity[1])] = None
        prepared.append({"weekEndDate": end, "previousWeekEndDate": prior_end, "days": days,
                         "missingExchanges": missing, "previousMissingExchanges": previous_missing,
                         "contracts": contracts, "derivedSeedContracts": derived_seeds})
    return {"weeks": prepared, "lookup": lookup, "byDay": by_day, "sources": sources}


def build_weekly_streak_lookup(
    facts: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]],
    calendar_days: Sequence[str], *, as_of_day: str,
) -> dict[tuple[str, str, str], int | None]:
    """Return (completed week end, exchange, contract) -> signed streak."""

    return _weekly_context(facts, coverage, calendar_days, as_of_day)["lookup"]


def weekly_contract_streaks(
    facts: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]],
    calendar_days: Sequence[str], *, as_of_day: str,
) -> dict[str, Any]:
    context = _weekly_context(facts, coverage, calendar_days, as_of_day)
    return {"weekEndDates": [week["weekEndDate"] for week in context["weeks"]],
            "lookup": context["lookup"],
            "pendingWeeks": [week["weekEndDate"] for week in context["weeks"] if week["missingExchanges"]]}


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    if not values:
        return None
    return math.fsum(value * weight for value, weight in zip(values, weights, strict=True))


def _score_contracts(contracts: Sequence[Mapping[str, Any]], end: str) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for contract in contracts:
        if not contract.get("existsAtWeekEnd", True):
            continue
        groups[str(contract["productKey"])].append(contract)
    prepared = []
    bootstrap = []
    inactive = []
    seeded = []
    invalid = []
    missing_turnover = []
    missing_weight = []
    basis_numerator = basis_denominator = 0.0
    for product, rows in sorted(groups.items()):
        if any(row["openInterest"] is None for row in rows):
            invalid.append(product)
            missing_weight.append(product)
            continue
        total_oi = math.fsum(row["openInterest"] for row in rows)
        field = "openInterest"
        if total_oi == 0:
            field = "weeklyVolume"
            if any(row[field] is None for row in rows):
                invalid.append(product)
                continue
            bootstrap.append(product)
        basis_total = math.fsum(row[field] for row in rows)
        if basis_total == 0:
            inactive.append(product)
            continue
        eligible = []
        for row in rows:
            if row[field] <= 0:
                continue
            if row["weeklyReturn"] is None:
                if row["reason"] in {"NEW_CONTRACT_SEED", "FIRST_WEEK_SEED"}:
                    seeded.append(f"{product}:{row['contractCode']}")
                else:
                    invalid.append(f"{product}:{row['contractCode']}")
                continue
            if row["weeklyTurnover"] is None:
                missing_turnover.append(f"{product}:{row['contractCode']}")
            eligible.append(row)
        eligible_basis = math.fsum(row[field] for row in eligible)
        basis_numerator += eligible_basis / basis_total
        basis_denominator += 1
        if eligible_basis == 0:
            continue
        for row in eligible:
            base = row[field] / eligible_basis
            prepared.append({**row, "baseWeight": base,
                             "logWeight": math.log(base),
                             "weightMethod": "OPEN_INTEREST" if field == "openInterest" else "VOLUME_BOOTSTRAP"})
    weights = normalize_log_weights([row["logWeight"] for row in prepared])
    signs = [_sign(row["weeklyReturn"]) for row in prepared]
    magnitude = [math.tanh(row["weeklyReturn"] / WEEK_RETURN_SCALE) for row in prepared]
    breadth_score = _weighted_mean(signs, weights)
    magnitude_score = _weighted_mean(magnitude, weights)
    active_rows = [row for row in contracts if (row["openInterest"] or 0) > 0 or (row["weeklyVolume"] or 0) > 0]
    invalid.extend(f"{row['productKey']}:{row['contractCode']}:{row['reason']}" for row in active_rows
                   if row["weeklyReturn"] is None and row["reason"] not in {"NEW_CONTRACT_SEED", "FIRST_WEEK_SEED"})
    directional_rows = [row for row in active_rows if row["weeklyReturn"] is not None]
    missing_turnover = sorted({f"{row['productKey']}:{row['contractCode']}" for row in directional_rows if row["weeklyTurnover"] is None})
    verified_money = [row for row in directional_rows if row["weeklyTurnover"] is not None]
    # Every verified active contract gets one baseline vote. A second same-direction
    # week adds one vote, a third adds three, a fourth adds seven, and so on.
    # The baseline vote stays in the denominator but has no trend direction.
    # Softmax normalization avoids materializing unbounded powers of two.
    streak_logs = [trend_weight_log2(row["streakWeeks"] or 0) * math.log(2) for row in directional_rows]
    streak_weights = normalize_log_weights(streak_logs)
    streak_score = math.fsum(
        _sign(row["weeklyReturn"]) * -math.expm1(-log_weight) * weight
        for row, log_weight, weight in zip(directional_rows, streak_logs, streak_weights, strict=True)
    ) if directional_rows else None
    raw_components = {"breadth": breadth_score, "streak": streak_score,
                      "magnitude": magnitude_score}
    components = {key: min(100.0, max(-100.0, value * 100)) if value is not None else None
                  for key, value in raw_components.items()}
    total_score = math.fsum(components[key] * fixed for key, fixed in FIXED_WEIGHTS.items()) \
        if all(value is not None for value in components.values()) else None
    if total_score is not None:
        total_score = min(100.0, max(-100.0, total_score))
    total_turnover = math.fsum(row["weeklyTurnover"] or 0 for row in contracts)
    eligible_turnover = math.fsum(row["weeklyTurnover"] for row in verified_money)
    active_count = len(active_rows)
    count_signs = [_sign(row["weeklyReturn"]) for row in directional_rows]
    product_returns = {
        product: math.fsum(row["weeklyReturn"] * row["baseWeight"] for row in prepared if row["productKey"] == product)
        for product in {row["productKey"] for row in prepared}
    }
    return {
        "tradeDate": end, "weekEndDate": end, "totalScore": total_score,
        "score": total_score, "breadthScore": components["breadth"], "streakScore": components["streak"],
        "magnitudeScore": components["magnitude"],
        "upCount": count_signs.count(1), "downCount": count_signs.count(-1),
        "flatCount": count_signs.count(0), "validContractCount": len(directional_rows), "weightedContractCount": len(prepared),
        "totalContractCount": active_count,
        "weeklyUpCount": sum(row["streakWeeks"] > 0 for row in directional_rows),
        "weeklyDownCount": sum(row["streakWeeks"] < 0 for row in directional_rows),
        "streakUpCount": sum((row["streakWeeks"] or 0) >= 2 for row in directional_rows),
        "streakDownCount": sum((row["streakWeeks"] or 0) <= -2 for row in directional_rows),
        "streakEligibleCount": len(directional_rows),
        "maxStreakWeeks": max((abs(row["streakWeeks"]) for row in directional_rows), default=0),
        "eligibleProductCount": len(product_returns), "totalProductCount": len(groups),
        "upProductCount": sum(value > 0 for value in product_returns.values()),
        "downProductCount": sum(value < 0 for value in product_returns.values()),
        "flatProductCount": sum(value == 0 for value in product_returns.values()),
        "dataQualityStatus": "UNAVAILABLE" if total_score is None else "PARTIAL" if invalid else "WARMUP" if seeded else "PASS",
        "isWarmup": bool(seeded),
        "coverage": {"direction": len(directional_rows) / active_count if active_count else 0,
                     "turnover": len(verified_money) / active_count if active_count else 0,
                     "turnoverAmount": None if missing_turnover else eligible_turnover / total_turnover if total_turnover else 0,
                     "weightBasis": None if missing_weight else basis_numerator / basis_denominator if basis_denominator else 0,
                     "missingExchanges": [], "newContractSeeds": seeded,
                     "invalidContracts": sorted(set(invalid)), "missingTurnoverContracts": missing_turnover,
                     "volumeBootstrapProducts": bootstrap, "missingWeightProducts": missing_weight,
                     "excludedInactiveProducts": inactive},
        "contracts": [{**row, "normalizedBaseWeight": weight} for row, weight in zip(prepared, weights, strict=True)],
        "productReturns": product_returns,
    }


def _config() -> dict[str, Any]:
    return {
        "weights": dict(FIXED_WEIGHTS), "score": {"min": -100, "max": 100},
        "stateBands": [
            {"min": -100, "max": -80, "label": "极度偏空"},
            {"min": -80, "max": -60, "label": "明显偏空"},
            {"min": -60, "max": -20, "label": "偏空"},
            {"min": -20, "max": 20, "label": "多空均衡"},
            {"min": 20, "max": 60, "label": "偏多"},
            {"min": 60, "max": 80, "label": "明显偏多"},
            {"min": 80, "max": 100, "label": "极度偏多"},
        ],
        "priceBasis": "SETTLEMENT", "weeklyReturnScale": WEEK_RETURN_SCALE,
        "formula": "total = breadth*0.20 + streak*0.60 + magnitude*0.20",
        "streakRule": "连续周独立计分；不足2个同向周加持0；第2/3/4周总票数2/4/8，额外方向票1/3/7，无封顶；反向首周及平盘加持0，缺报断开",
        "weightMethod": "数量与幅度按品种等权、品种内持仓分配；全品种零持仓才用周成交量。连续周按所有有效合约逐个计票；成交额只展示原始事实，不参与热度分数",
        "frequency": "DAILY_WITH_COMPLETED_WEEK_STREAK", "smoothing": "NONE",
    }


def _daily_contracts(rows: Mapping[tuple[str, str], Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Adapt one verified official source day to the score calculator's fields."""

    contracts = []
    for (venue, code), row in rows.items():
        settlement = _number(row.get("settlement"), positive=True)
        previous = _number(row.get("previous_settlement"), positive=True)
        daily_return = settlement / previous - 1 if settlement is not None and previous is not None else None
        if daily_return is not None and not math.isfinite(daily_return):
            daily_return = None
        contracts.append({
            "productKey": f"{venue}.{row['product_code']}", "contractCode": code,
            "weeklyReturn": daily_return, "streakWeeks": 0, "weightLog2": 0,
            "openInterest": _number(row.get("open_interest")) if row.get("open_interest_sidedness") == "ONE_SIDED" else None,
            "weeklyVolume": _number(row.get("volume")) if row.get("volume_sidedness") == "ONE_SIDED" else None,
            "weeklyTurnover": _number(row.get("turnover_cny")) if row.get("turnover_sidedness") == "ONE_SIDED"
            and row.get("turnover_quality_status") == "PASS" else None,
            "reason": None if daily_return is not None else "MISSING_DAILY_SETTLEMENT",
            "existsAtWeekEnd": True,
        })
    return contracts


def _daily_score(
    contracts: Sequence[Mapping[str, Any]], day: str, *, streak: Mapping[str, Any] | None,
    pending_week: str | None, missing_exchanges: Sequence[str],
    streak_missing_exchanges: Sequence[str] = (), product_key: str | None = None,
) -> dict[str, Any]:
    """Join today's count and magnitude scores to the last closed-week streak."""

    result = _score_contracts(contracts, day)
    weighted = result.pop("contracts")
    result.pop("productReturns")
    active = [row for row in contracts if (row["openInterest"] or 0) > 0 or (row["weeklyVolume"] or 0) > 0]
    directional = [row for row in active if row["weeklyReturn"] is not None]
    result["upWeightedReturnPct"] = 100 * math.fsum(
        row["weeklyReturn"] * row["normalizedBaseWeight"] for row in weighted if row["weeklyReturn"] > 0
    ) if weighted else None
    result["downWeightedReturnPct"] = 100 * math.fsum(
        row["weeklyReturn"] * row["normalizedBaseWeight"] for row in weighted if row["weeklyReturn"] < 0
    ) if weighted else None
    money_complete = all(row["weeklyTurnover"] is not None for row in directional)
    result["upTurnoverCny"] = math.fsum(
        row["weeklyTurnover"] for row in directional if row["weeklyReturn"] > 0
    ) if directional and money_complete else None
    result["downTurnoverCny"] = math.fsum(
        row["weeklyTurnover"] for row in directional if row["weeklyReturn"] < 0
    ) if directional and money_complete else None
    result["flatTurnoverCny"] = math.fsum(
        row["weeklyTurnover"] for row in directional if _sign(row["weeklyReturn"]) == 0
    ) if directional and money_complete else None
    if pending_week is not None:
        streak_score = None
    elif streak is not None:
        streak_score = streak.get("streakScore")
    else:
        streak_score = 0.0
    result["streakScore"] = streak_score
    result["streakWeekEndDate"] = streak.get("weekEndDate") if streak is not None else None
    result["weekEndDate"] = result["streakWeekEndDate"]
    result["streakPendingWeek"] = pending_week
    result["pendingWeek"] = pending_week
    result["streakUpCount"] = streak.get("streakUpCount", 0) if streak is not None else 0
    result["streakDownCount"] = streak.get("streakDownCount", 0) if streak is not None else 0
    result["streakEligibleCount"] = streak.get("streakEligibleCount", 0) if streak is not None else 0
    result["coverage"]["missingExchanges"] = sorted(set(missing_exchanges) | set(streak_missing_exchanges) | set(
        streak.get("coverage", {}).get("missingExchanges", []) if streak is not None else []
    ))
    result["coverage"]["dailyMissingExchanges"] = list(missing_exchanges)
    result["coverage"]["derivedSeedContracts"] = (
        streak.get("coverage", {}).get("derivedSeedContracts", []) if streak is not None else []
    )
    if product_key is not None:
        result["productKey"] = product_key
    values = {"breadth": result["breadthScore"], "streak": streak_score,
              "magnitude": result["magnitudeScore"]}
    result["totalScore"] = result["score"] = math.fsum(
        values[key] * weight for key, weight in FIXED_WEIGHTS.items()
    ) if all(value is not None for value in values.values()) else None
    if missing_exchanges or pending_week is not None:
        result["totalScore"] = result["score"] = None
        result["dataQualityStatus"] = "PENDING"
    elif streak_score is None:
        result["dataQualityStatus"] = "UNAVAILABLE"
    elif result["totalScore"] is not None and streak is None:
        result["dataQualityStatus"] = "WARMUP"
    elif result["totalScore"] is not None and streak is not None and streak.get("dataQualityStatus") == "PARTIAL":
        result["dataQualityStatus"] = "PARTIAL"
    result["isWarmup"] = streak is None
    result["sourceComplete"] = not missing_exchanges and pending_week is None
    return result


def calculate_weekly_heat(
    facts: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]],
    calendar_days: Sequence[str], *, as_of_day: str, start_day: str | None = None,
    end_day: str | None = None, product_key: str | None = None,
    seed_prices: Mapping[tuple[str, str, str], float] | None = None,
) -> dict[str, Any]:
    """Calculate daily official heat with only completed-week streak snapshots."""

    if product_key is not None:
        product_key = product_key.upper()
        if _PRODUCT.fullmatch(product_key) is None:
            raise ValueError("invalid product_key")
    for boundary in (start_day, end_day):
        if boundary is not None:
            date.fromisoformat(boundary)
    if start_day is not None and end_day is not None and start_day > end_day:
        raise ValueError("start_day must not be after end_day")
    context = _weekly_context(facts, coverage, calendar_days, as_of_day, seed_prices)
    scope = {product_key.split(".")[0]} if product_key else set(EXCHANGES)
    weekly_points = []
    products = []
    product_states: dict[str, int | None] = {}
    events = {}
    last_snapshot = None
    for week in context["weeks"]:
        end = week["weekEndDate"]
        rows = [row for row in week["contracts"].values() if product_key is None or row["productKey"] == product_key]
        missing = sorted(scope & set(week["missingExchanges"]))
        previous_missing = sorted(scope & set(week["previousMissingExchanges"]))
        invalid_previous = previous_missing and week["previousWeekEndDate"] is not None and week["previousWeekEndDate"] >= min(context["byDay"], default=end)
        result = _score_contracts(rows, end)
        result["coverage"]["derivedSeedContracts"] = week["derivedSeedContracts"]
        if week["derivedSeedContracts"] and result["dataQualityStatus"] == "PASS":
            result["dataQualityStatus"] = "WARMUP"
        result["previousWeekEndDate"] = week["previousWeekEndDate"]
        pending = bool(missing or invalid_previous)
        if pending:
            result["dataQualityStatus"] = "PENDING"
            result["coverage"]["missingExchanges"] = sorted(set(missing) | set(previous_missing))
            result["totalScore"] = result["score"] = None
            for product in {row["productKey"] for row in rows}:
                product_states[product] = None
        else:
            for product, weekly_return in sorted(result["productReturns"].items()):
                streak = _next_streak(product_states.get(product), _sign(weekly_return))
                product_states[product] = streak
                subset = [row for row in rows if row["productKey"] == product]
                product_point = _score_contracts(subset, end)
                products.append({**product_point, "productKey": product, "weeklyReturn": weekly_return,
                                 "streakWeeks": streak, "weightLog2": trend_weight_log2(streak)})
        result["sourceComplete"] = not pending
        weekly_points.append(result)
        if result["streakScore"] is not None and not pending:
            last_snapshot = result
        events[end] = (last_snapshot, end if pending or result["streakScore"] is None else None,
                       result["coverage"]["missingExchanges"])
    visible = sorted(day for day in set(calendar_days)
                     if day <= as_of_day and (start_day is None or day >= start_day)
                     and (end_day is None or day <= end_day)
                     and day >= min(context["byDay"], default=as_of_day))
    points = []
    product_daily_points = []
    product_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in products:
        product_events[product["weekEndDate"]].append(product)
    product_streaks: dict[str, dict[str, Any]] = {}
    held = None
    pending_week = None
    pending_venues = []
    for day in sorted(day for day in set(calendar_days) if day <= as_of_day):
        if day in events:
            held, pending_week, pending_venues = events[day]
        for product in product_events.get(day, []):
            if product["streakScore"] is not None:
                product_streaks[product["productKey"]] = product
        if day not in visible:
            continue
        unresolved = pending_week if pending_week is not None and (start_day is None or pending_week >= start_day) else None
        day_contracts = [row for row in _daily_contracts(context["byDay"].get(day, {}))
                         if product_key is None or row["productKey"] == product_key]
        missing_daily = sorted(venue for venue in scope if not context["sources"].get((day, venue), False))
        points.append(_daily_score(day_contracts, day, streak=held, pending_week=unresolved,
                                   missing_exchanges=missing_daily,
                                   streak_missing_exchanges=pending_venues if unresolved else []))
        by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for contract in day_contracts:
            by_product[contract["productKey"]].append(contract)
        for key, subset in sorted(by_product.items()):
            if product_key is not None and key != product_key:
                continue
            venue = key.split(".")[0]
            local_missing = [venue] if venue in missing_daily else []
            local_pending = unresolved if venue in pending_venues else None
            product_daily_points.append(_daily_score(
                subset, day, streak=product_streaks.get(key), pending_week=local_pending,
                missing_exchanges=local_missing,
                streak_missing_exchanges=[venue] if local_pending else [], product_key=key,
            ))
    return {
        "available": any(point["totalScore"] is not None for point in points),
        "formulaVersion": FORMULA_VERSION, "config": _config(), "points": points,
        "weeklyPoints": weekly_points, "productPoints": products,
        "productDailyPoints": product_daily_points, "latest": points[-1] if points else None,
        "productKey": product_key,
        "limitations": ["数量和幅度每天按官网结算更新；连续周只使用当日已结束的最近完整交易周。",
                        "新合约无上一周价时只建立连续周种子；真实来源缺报保持断点。",
                        "连续周从第二个同向周开始独立加持；另外两项不乘连续周倍数。"],
    }


def product_weekly_streak_lookup(result: Mapping[str, Any]) -> dict[tuple[str, str], int]:
    return {(str(row["weekEndDate"]), str(row["productKey"])): int(row["streakWeeks"])
            for row in result.get("productPoints", [])}


__all__ = (
    "FORMULA_VERSION", "FIXED_WEIGHTS", "EXCHANGES", "calculate_weekly_heat",
    "build_weekly_streak_lookup", "weekly_contract_streaks", "product_weekly_streaks_from_daily",
    "product_weekly_streak_lookup", "trend_weight_log2", "normalize_log_weights",
)
