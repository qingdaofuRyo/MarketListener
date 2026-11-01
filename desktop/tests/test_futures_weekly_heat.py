from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
import math

import pytest

from market_monitor.futures_official_daily_sources import PARSER_VERSION
from market_monitor.futures_weekly_heat import (
    build_weekly_streak_lookup,
    calculate_weekly_heat,
    normalize_log_weights,
    product_weekly_streaks_from_daily,
    trend_weight_log2,
)


_PRODUCTS = {"SHFE": "CU", "INE": "SC", "DCE": "M", "CZCE": "AP", "GFEX": "SI", "CFFEX": "IF"}


def _weeks(count: int = 7) -> list[list[str]]:
    monday = date(2026, 1, 5)
    return [[(monday + timedelta(days=week * 7 + day)).isoformat() for day in range(5)] for week in range(count)]


def _coverage(facts: list[dict]) -> list[dict]:
    counts = Counter((row["trade_date"], row["exchange"]) for row in facts if row["adapter"].endswith("-daily-v2"))
    return [{"trade_date": day, "exchange": venue, "adapter": f"official-{venue.lower()}-daily-v2",
             "status": "PASS", "parser_version": PARSER_VERSION,
             "record_count": count, "usable_count": count, "payload_sha256": "a" * 64}
            for (day, venue), count in sorted(counts.items())]


def _inputs(weeks: list[list[str]], prices: list[float] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    prices = prices or [100 + index * 10 for index in range(len(weeks))]
    facts = []
    for index, days in enumerate(weeks):
        for day in days:
            for venue, product in _PRODUCTS.items():
                facts.append({"trade_date": day, "exchange": venue, "product_code": product,
                              "contract_code": product + "2612", "settlement": prices[index],
                              "previous_settlement": prices[index - 1] if index else prices[0],
                              "open_interest": 100.0, "volume": 10.0, "turnover_cny": 1000.0,
                              "volume_sidedness": "ONE_SIDED", "open_interest_sidedness": "ONE_SIDED",
                              "turnover_sidedness": "ONE_SIDED", "turnover_quality_status": "PASS",
                              "quality_status": "PASS", "parser_version": PARSER_VERSION,
                              "adapter": f"official-{venue.lower()}-daily-v2",
                              "payload_sha256": "a" * 64})
    return facts, _coverage(facts), [day for days in weeks for day in days]


def test_next_trading_day_prior_settlement_unlocks_first_week_without_faking_raw_day():
    weeks = [["2022-12-30"], ["2023-01-03", "2023-01-04", "2023-01-05", "2023-01-06"]]
    facts, _coverage_rows, days = _inputs(weeks, [100, 110])
    facts = [row for row in facts if not (row["trade_date"] == "2022-12-30" and row["exchange"] == "DCE")]
    coverage = _coverage(facts)
    blocked = calculate_weekly_heat(facts, coverage, days, as_of_day="2023-01-06", start_day="2023-01-01")
    assert blocked["latest"]["totalScore"] is None
    seeded = calculate_weekly_heat(
        facts, coverage, days, as_of_day="2023-01-06", start_day="2023-01-01",
        seed_prices={("2022-12-30", "DCE", "M2612"): 100},
    )
    assert seeded["latest"]["totalScore"] is not None
    assert "DCE.M2612" in seeded["latest"]["coverage"]["derivedSeedContracts"]
    assert seeded["points"][0]["pendingWeek"] is None
    assert seeded["points"][0]["isWarmup"] is True


def test_weekly_multipliers_double_each_week_without_a_cap_and_reverse() -> None:
    weeks = _weeks()
    facts, coverage, calendar = _inputs(weeks, [100, 105, 110, 115, 120, 110, 110])
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    contract = "CU2612"
    lookup = build_weekly_streak_lookup(facts, coverage, calendar, as_of_day=calendar[-1])
    assert [lookup[(weeks[index][-1], "SHFE", contract)] for index in range(1, 7)] == [1, 2, 3, 4, -1, 0]
    assert [2 ** trend_weight_log2(value) for value in [1, 2, 3, 4, -1, 0]] == [1, 2, 4, 8, 1, 1]
    assert result["config"]["weights"] == {"breadth": .20, "streak": .60, "magnitude": .20}
    assert "turnover" not in result["config"]["weights"]
    assert result["config"]["smoothing"] == "NONE"
    assert all(-100 <= point["totalScore"] <= 100 for point in result["points"] if point["totalScore"] is not None)
    assert result["weeklyPoints"][1]["magnitudeScore"] == pytest.approx(100 * math.tanh(.05 / .05))
    assert [result["weeklyPoints"][index]["streakScore"] for index in range(1, 7)] == pytest.approx(
        [0, 50, 75, 87.5, 0, 0]
    )
    assert result["weeklyPoints"][4]["streakUpCount"] == 6
    assert result["weeklyPoints"][5]["streakDownCount"] == 0
    fourth = result["weeklyPoints"][4]
    assert fourth["totalScore"] == pytest.approx(
        fourth["breadthScore"] * .20 + fourth["streakScore"] * .60
        + fourth["magnitudeScore"] * .20
    )


def test_unfinished_week_changes_daily_heat_without_using_future_week_streak() -> None:
    weeks = _weeks(4)
    facts, coverage, calendar = _inputs(weeks)
    as_of = weeks[3][2]
    before = calculate_weekly_heat(facts, coverage, calendar, as_of_day=as_of)
    for row in facts:
        if row["trade_date"] >= weeks[3][0]:
            row["settlement"] = 999999
    after = calculate_weekly_heat(facts, coverage, calendar, as_of_day=as_of)
    assert before["weeklyPoints"] == after["weeklyPoints"]
    assert before["latest"]["totalScore"] != after["latest"]["totalScore"]
    assert before["latest"]["streakScore"] == after["latest"]["streakScore"]
    assert after["latest"]["weekEndDate"] == weeks[2][-1]
    assert len(after["weeklyPoints"]) == 3


def test_daily_history_uses_daily_official_direction_amount_and_completed_streak() -> None:
    weeks = _weeks(3)
    facts, _, calendar = _inputs(weeks, [100, 110, 120])
    for row in facts:
        if row["trade_date"] in weeks[-1]:
            position = weeks[-1].index(row["trade_date"])
            row["settlement"] = 121 + position
            row["previous_settlement"] = 120 + position
    result = calculate_weekly_heat(facts, _coverage(facts), calendar, as_of_day=calendar[-1])
    by_day = {point["tradeDate"]: point for point in result["points"]}
    monday, tuesday, _, _, friday = (by_day[day] for day in weeks[-1])
    assert monday["streakScore"] == 0
    assert friday["streakScore"] == pytest.approx(50)
    assert monday["totalScore"] != tuesday["totalScore"]
    assert monday["totalScore"] == pytest.approx(
        .60 * monday["streakScore"] + .20 * monday["breadthScore"]
        + .20 * monday["magnitudeScore"]
    )
    assert monday["upCount"] == 6 and monday["downCount"] == 0
    assert monday["upWeightedReturnPct"] == pytest.approx(100 / 120)
    assert monday["downWeightedReturnPct"] == 0
    assert monday["upTurnoverCny"] == 6000
    assert monday["downTurnoverCny"] == 0
    assert monday["streakWeekEndDate"] == weeks[-2][-1]
    assert friday["streakWeekEndDate"] == weeks[-1][-1]
    assert result["productDailyPoints"][-1]["tradeDate"] == friday["tradeDate"]


def test_daily_source_gap_is_null_even_when_previous_streak_is_available() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    missing_day = weeks[-1][1]
    coverage = [row for row in coverage if not (row["trade_date"] == missing_day and row["exchange"] == "DCE")]
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    by_day = {point["tradeDate"]: point for point in result["points"]}
    assert by_day[missing_day]["totalScore"] is None
    assert by_day[missing_day]["dataQualityStatus"] == "PENDING"
    assert by_day[missing_day]["coverage"]["dailyMissingExchanges"] == ["DCE"]
    assert by_day[missing_day]["upCount"] == 5
    assert by_day[weeks[-1][-1]]["pendingWeek"] == weeks[-1][-1]


def test_missing_source_day_freezes_previous_snapshot_and_breaks_streak() -> None:
    weeks = _weeks(6)
    facts, coverage, calendar = _inputs(weeks)
    missing_day = weeks[3][2]
    coverage = [row for row in coverage if not (row["trade_date"] == missing_day and row["exchange"] == "DCE")]
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    points = {row["tradeDate"]: row for row in result["points"]}
    assert points[weeks[3][-1]]["weekEndDate"] == weeks[2][-1]
    assert points[weeks[3][-1]]["pendingWeek"] == weeks[3][-1]
    assert points[weeks[3][-1]]["dataQualityStatus"] == "PENDING"
    assert points[weeks[3][-1]]["coverage"]["missingExchanges"] == ["DCE"]
    assert result["weeklyPoints"][3]["dataQualityStatus"] == "PENDING"
    lookup = build_weekly_streak_lookup(facts, coverage, calendar, as_of_day=calendar[-1])
    assert lookup[(weeks[3][-1], "DCE", "M2612")] is None
    assert lookup[(weeks[4][-1], "DCE", "M2612")] == 1


def test_first_week_missing_source_is_a_real_gap_not_a_seed() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    coverage = [row for row in coverage if not (row["trade_date"] == weeks[0][2] and row["exchange"] == "SHFE")]
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    first = result["weeklyPoints"][0]
    assert first["dataQualityStatus"] == "PENDING"
    assert "SHFE" in first["coverage"]["missingExchanges"]
    assert any(":CU2612" in item for item in first["coverage"]["invalidContracts"])


def test_missing_week_end_keeps_lookup_unknown_until_a_new_pair_of_endpoints() -> None:
    weeks = _weeks(6)
    facts, coverage, calendar = _inputs(weeks)
    coverage = [row for row in coverage if not (row["trade_date"] == weeks[3][-1] and row["exchange"] == "DCE")]
    lookup = build_weekly_streak_lookup(facts, coverage, calendar, as_of_day=calendar[-1])
    assert lookup[(weeks[3][-1], "DCE", "M2612")] is None
    assert lookup[(weeks[4][-1], "DCE", "M2612")] is None
    assert lookup[(weeks[5][-1], "DCE", "M2612")] == 1


def test_new_contract_seeds_without_freezing_verified_existing_returns() -> None:
    weeks = _weeks(4)
    facts, coverage, calendar = _inputs(weeks)
    for row in list(facts):
        if row["exchange"] == "SHFE" and row["trade_date"] >= weeks[2][0]:
            facts.append({**row, "contract_code": "CU2611", "open_interest": 25})
    result = calculate_weekly_heat(facts, _coverage(facts), calendar, as_of_day=calendar[-1])
    seed = result["weeklyPoints"][2]
    assert seed["totalScore"] is not None and seed["dataQualityStatus"] == "WARMUP"
    assert seed["coverage"]["newContractSeeds"] == ["SHFE.CU:CU2611"]
    lookup = build_weekly_streak_lookup(facts, _coverage(facts), calendar, as_of_day=calendar[-1])
    assert lookup[(weeks[2][-1], "SHFE", "CU2611")] is None
    assert lookup[(weeks[3][-1], "SHFE", "CU2611")] == 1


def test_duplicate_month_count_does_not_increase_product_basic_weight() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in facts:
        if row["exchange"] == "SHFE":
            index = next(i for i, days in enumerate(weeks) if row["trade_date"] in days)
            row["settlement"] = 100 - index * 10
    before = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    split = []
    for row in facts:
        if row["exchange"] != "SHFE":
            split.append(row)
            continue
        for month in range(1, 13):
            split.append({**row, "contract_code": f"CU27{month:02}",
                          "open_interest": row["open_interest"] / 12, "turnover_cny": row["turnover_cny"] / 12})
    after = calculate_weekly_heat(split, _coverage(split), calendar, as_of_day=calendar[-1])
    assert before["latest"]["breadthScore"] == pytest.approx(after["latest"]["breadthScore"])
    assert before["latest"]["magnitudeScore"] == pytest.approx(after["latest"]["magnitudeScore"])
    assert "turnoverScore" not in after["latest"]


def test_contract_streak_is_its_own_component_and_counts_all_contracts() -> None:
    weeks = _weeks(4)
    facts, _, calendar = _inputs(weeks, [100, 110, 120, 130])
    versions = []
    for silver_prices in ([100, 90, 80, 70], [100, 70, 80, 70]):
        rows = list(facts)
        for index, days in enumerate(weeks):
            for row in facts:
                if row["exchange"] == "SHFE" and row["trade_date"] in days:
                    rows.append({**row, "product_code": "AG", "contract_code": "AG2612",
                                 "settlement": silver_prices[index]})
        versions.append((rows, calculate_weekly_heat(rows, _coverage(rows), calendar, as_of_day=calendar[-1])))
    sustained, reversed_direction = (version["latest"] for _, version in versions)
    assert sustained["streakDownCount"] == 1
    assert reversed_direction["streakDownCount"] == 0
    assert sustained["streakScore"] < reversed_direction["streakScore"]
    for component in ("breadthScore", "magnitudeScore"):
        assert sustained[component] == pytest.approx(reversed_direction[component])
    silver_rows = versions[0][0]
    silver = calculate_weekly_heat(silver_rows, _coverage(silver_rows), calendar,
                                   as_of_day=calendar[-1], product_key="SHFE.AG")
    assert silver["weeklyPoints"][1]["streakScore"] == 0
    assert silver["weeklyPoints"][2]["streakScore"] == pytest.approx(-50)
    assert silver["latest"]["streakScore"] == pytest.approx(-75)
    reversed_silver_rows = versions[1][0]
    reversed_silver = calculate_weekly_heat(
        reversed_silver_rows, _coverage(reversed_silver_rows), calendar,
        as_of_day=calendar[-1], product_key="SHFE.AG",
    )
    assert reversed_silver["latest"]["streakScore"] == 0
    local = calculate_weekly_heat(
        [row for row in facts if row["exchange"] == "SHFE"],
        _coverage([row for row in facts if row["exchange"] == "SHFE"]), calendar,
        as_of_day=calendar[-1], product_key="SHFE.CU",
    )
    assert local["latest"]["streakScore"] == pytest.approx(75)


def test_zero_product_oi_uses_one_uniform_volume_bootstrap() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in facts:
        if row["exchange"] == "SHFE":
            row["open_interest"] = 0
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    assert result["latest"]["coverage"]["volumeBootstrapProducts"] == ["SHFE.CU"]
    assert result["latest"]["totalScore"] is not None
    assert result["latest"]["dataQualityStatus"] == "PASS"


def test_missing_turnover_does_not_remove_return_from_breadth_or_magnitude() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    baseline = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    for row in facts:
        if row["exchange"] == "SHFE" and row["trade_date"] == weeks[-1][2]:
            row["turnover_cny"] = None
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    latest = result["latest"]
    assert latest["weekEndDate"] == weeks[-1][-1]
    assert latest["breadthScore"] == baseline["latest"]["breadthScore"]
    assert latest["magnitudeScore"] == baseline["latest"]["magnitudeScore"]
    assert latest["totalScore"] == pytest.approx(baseline["latest"]["totalScore"])
    assert latest["validContractCount"] == 6
    assert latest["dataQualityStatus"] == "PASS"
    assert latest["coverage"]["turnover"] == 1
    assert result["weeklyPoints"][-1]["coverage"]["turnover"] == pytest.approx(5 / 6)
    assert result["weeklyPoints"][-1]["coverage"]["missingTurnoverContracts"] == ["SHFE.CU:CU2612"]
    assert result["weeklyPoints"][-1]["dataQualityStatus"] == "PASS"
    missing_day = next(point for point in result["points"] if point["tradeDate"] == weeks[-1][2])
    assert missing_day["dataQualityStatus"] == "PASS"
    assert missing_day["totalScore"] is not None
    assert missing_day["upTurnoverCny"] is None
    assert missing_day["coverage"]["missingTurnoverContracts"] == ["SHFE.CU:CU2612"]
    assert latest["pendingWeek"] is None


def test_missing_local_settlement_publishes_partial_sample_instead_of_freezing() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in facts:
        if row["exchange"] == "SHFE" and row["trade_date"] == weeks[-1][-1]:
            row["settlement"] = None
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    latest = result["latest"]
    assert latest["weekEndDate"] == weeks[-1][-1] and latest["totalScore"] is not None
    assert latest["dataQualityStatus"] == "PARTIAL"
    assert latest["coverage"]["direction"] == pytest.approx(5 / 6)
    assert latest["pendingWeek"] is None


def test_large_streak_weights_are_finite_and_not_artificially_capped() -> None:
    logs = [trend_weight_log2(streak) * math.log(2) for streak in [10000, 9999, 1]]
    weights = normalize_log_weights(logs)
    assert all(math.isfinite(weight) for weight in weights)
    assert weights[0] == pytest.approx(2 / 3)
    assert weights[1] == pytest.approx(1 / 3)
    assert weights[2] == 0  # Harmless floating-point underflow, not a policy cap.


def test_product_chain_streak_freezes_and_compounds_daily_returns() -> None:
    weeks = _weeks(4)
    calendar = [day for days in weeks for day in days]
    daily = {day: .01 for day in calendar}
    daily[weeks[2][0]] = None
    result = product_weekly_streaks_from_daily({"SHFE.CU": daily}, calendar, weeks[-1][2])
    assert result[(weeks[1][2], "SHFE.CU")] == 1
    assert result[(weeks[1][-1], "SHFE.CU")] == 2
    assert result[(weeks[2][-1], "SHFE.CU")] is None
    assert result[(weeks[3][2], "SHFE.CU")] is None
    recovery = product_weekly_streaks_from_daily({"SHFE.CU": daily}, calendar, calendar[-1])
    assert recovery[(calendar[-1], "SHFE.CU")] == 1


def test_whole_holiday_week_is_skipped_and_short_week_closes_on_last_trading_day() -> None:
    weeks = _weeks(4)
    calendar_weeks = [weeks[0], weeks[2], weeks[3][:3]]
    facts, coverage, calendar = _inputs(calendar_weeks)
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    lookup = build_weekly_streak_lookup(facts, coverage, calendar, as_of_day=calendar[-1])
    assert lookup[(calendar_weeks[1][-1], "SHFE", "CU2612")] == 1
    assert lookup[(calendar_weeks[2][-1], "SHFE", "CU2612")] == 2
    assert result["latest"]["weekEndDate"] == calendar[-1]


def test_product_filter_keeps_other_venues_gap_outside_requested_scope() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    coverage = [row for row in coverage if row["exchange"] != "DCE"]
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1], product_key="SHFE.CU")
    assert result["latest"]["totalScore"] is not None
    assert result["latest"]["coverage"]["missingExchanges"] == []
    assert result["latest"]["eligibleProductCount"] == 1


def test_traded_zero_oi_contract_does_not_mix_turnover_into_heat_weights() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in list(facts):
        if row["exchange"] == "SHFE":
            index = next(index for index, days in enumerate(weeks) if row["trade_date"] in days)
            facts.append({**row, "contract_code": "CU2611", "settlement": 100 - index * 10,
                          "open_interest": 0, "turnover_cny": 1_000_000})
    result = calculate_weekly_heat(facts, _coverage(facts), calendar, as_of_day=calendar[-1])
    latest = result["latest"]
    assert latest["breadthScore"] == pytest.approx(100)
    assert "turnoverScore" not in latest
    assert latest["validContractCount"] == 7 and latest["weightedContractCount"] == 6
    assert latest["streakEligibleCount"] == 7 and latest["streakDownCount"] == 1
    assert latest["coverage"]["volumeBootstrapProducts"] == []


def test_missing_oi_publishes_partial_weights_without_hiding_direction_or_amount() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in facts:
        if row["exchange"] == "SHFE" and row["trade_date"] == weeks[-1][-1]:
            row["open_interest"] = None
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    latest = result["latest"]
    assert latest["weekEndDate"] == weeks[-1][-1]
    assert latest["validContractCount"] == 6 and latest["weightedContractCount"] == 5
    assert latest["dataQualityStatus"] == "PARTIAL"
    assert latest["coverage"]["missingWeightProducts"] == ["SHFE.CU"]
    assert latest["coverage"]["weightBasis"] is None


def test_true_zero_amount_does_not_remove_three_component_heat() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in facts:
        row["turnover_cny"] = 0
    result = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    assert result["weeklyPoints"][-1]["breadthScore"] == 100
    assert result["weeklyPoints"][-1]["totalScore"] is not None
    assert result["latest"]["totalScore"] is not None
    assert "turnoverScore" not in result["latest"]
    assert result["latest"]["upTurnoverCny"] == 0


def test_non_official_rows_indices_and_options_cannot_change_heat() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    before = calculate_weekly_heat(facts, coverage, calendar, as_of_day=calendar[-1])
    expanded = list(facts)
    for row in facts:
        if row["exchange"] == "SHFE":
            expanded.append({**row, "contract_code": "CU8888", "product_code": "INDEX", "settlement": 0})
            expanded.append({**row, "contract_code": "CU2612C100", "settlement": 0})
            expanded.append({**row, "adapter": "TDX_LOCAL", "contract_code": "CU2610", "settlement": 0})
        if row["exchange"] == "CFFEX":
            expanded.append({**row, "product_code": "HO", "contract_code": "HO2612", "settlement": 0})
    after = calculate_weekly_heat(expanded, _coverage(expanded), calendar, as_of_day=calendar[-1])
    assert before["latest"]["totalScore"] == after["latest"]["totalScore"]
    assert after["latest"]["validContractCount"] == 6


def test_month_average_future_is_its_own_product() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in list(facts):
        if row["exchange"] == "DCE":
            facts.append({**row, "product_code": "L-F", "contract_code": "L-F2612"})
    result = calculate_weekly_heat(facts, _coverage(facts), calendar, as_of_day=calendar[-1], product_key="DCE.L-F")
    assert result["latest"]["eligibleProductCount"] == 1
    assert result["productPoints"][-1]["productKey"] == "DCE.L-F"


def test_product_weekly_streak_uses_compounding_sign_and_extreme_chain_is_stable() -> None:
    weeks = _weeks(2)
    calendar = [day for days in weeks for day in days]
    values = {day: 0.0 for day in calendar}
    values[weeks[0][0]] = .1
    values[weeks[0][1]] = -.095  # Positive arithmetic sum, negative compounded growth.
    for day in weeks[1]:
        values[day] = 1e100
    lookup = product_weekly_streaks_from_daily({"SHFE.CU": values}, calendar, calendar[-1])
    assert lookup[(weeks[0][-1], "SHFE.CU")] == -1
    assert lookup[(weeks[1][-1], "SHFE.CU")] == 1


def test_contract_expiring_midweek_keeps_real_amount_gap_without_polluting_oi_weights() -> None:
    weeks = _weeks(3)
    facts, coverage, calendar = _inputs(weeks)
    for row in list(facts):
        if row["exchange"] == "SHFE" and row["trade_date"] != weeks[-1][-1]:
            facts.append({**row, "contract_code": "CU2611", "turnover_cny": 100_000})
    result = calculate_weekly_heat(facts, _coverage(facts), calendar, as_of_day=calendar[-1])
    latest = result["latest"]
    assert latest["weekEndDate"] == weeks[-1][-1] and latest["pendingWeek"] is None
    assert latest["weightedContractCount"] == 6 and latest["validContractCount"] == 6
    assert latest["totalContractCount"] == 6
    assert result["weeklyPoints"][-1]["totalContractCount"] == 7
    assert latest["breadthScore"] == 100
    assert latest["dataQualityStatus"] == "PARTIAL"
    assert result["weeklyPoints"][-1]["coverage"]["turnoverAmount"] < .08
    assert "SHFE.CU:CU2611:NO_WEEK_END_SETTLEMENT" in result["weeklyPoints"][-1]["coverage"]["invalidContracts"]
