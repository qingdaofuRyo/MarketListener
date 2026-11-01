"""Official-only market distribution facts and gap behavior."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import market_monitor.futures_distribution_bubbles as bubble_module
from market_monitor.futures_distribution_bubbles import (
    _daily_horizontal_change,
    _horizontal_value,
    market_distributions,
)
from market_monitor.futures_calendar import CALENDAR_PROVIDER, CALENDAR_SCHEMA
from market_monitor.futures_official_daily_sources import PARSER_VERSION
from market_monitor.futures_official_multipliers import load_official_multiplier_registry
from market_monitor.futures_settlement_parameters import (
    PARSER_VERSION as SETTLEMENT_PARSER_VERSION,
    ensure_schema as ensure_settlement_schema,
)
from market_monitor.storage import MarketStore
from market_monitor.futures_weekly_heat import FORMULA_VERSION as WEEKLY_FORMULA_VERSION
from market_monitor.web_app import create_web_app


_DAYS = ("2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29")
_VENUES = {
    "SHFE": ("CU", "CU2611"), "INE": ("SC", "SC2611"),
    "DCE": ("JM", "JM2611"), "CZCE": ("AP", "AP2611"),
    "GFEX": ("SI", "SI2611"), "CFFEX": ("IF", "IF2611"),
}


def _fact(day: str, exchange: str, product: str, contract: str, *,
          oi: float = 100, settlement: float = 100, previous: float | None = 100,
          amount: float | None = 1000, volume: float = 10) -> dict:
    return {
        "trade_date": day, "exchange": exchange, "contract_code": contract,
        "product_code": product, "close": settlement, "settlement": settlement,
        "previous_settlement": previous, "volume": volume,
        "open_interest": oi, "turnover_raw": 0.1,
        "turnover_cny": amount, "turnover_unit": "CNY_10K",
        "turnover_sidedness": "ONE_SIDED" if amount is not None else None,
        "source_url": "https://example.invalid/official-fixture",
        "adapter": f"official-{exchange.lower()}-daily-v2",
        "payload_sha256": "a" * 64, "retrieved_at": "2026-09-29T12:00:00+08:00",
        "quality_status": "PASS", "volume_sidedness": "ONE_SIDED",
        "open_interest_sidedness": "ONE_SIDED",
        "turnover_quality_status": "PASS" if amount is not None else "UNVERIFIED",
        "parser_version": PARSER_VERSION,
    }


def _record(store: MarketStore, day: str, exchange: str, rows: list[dict], status: str = "PASS") -> None:
    store.record_futures_official_daily(rows, {
        "trade_date": day, "exchange": exchange, "status": status,
        "record_count": len(rows), "usable_count": len(rows),
        "adapter": f"official-{exchange.lower()}-daily-v2",
        "source_url": "https://example.invalid/official-fixture",
        "payload_sha256": "a" * 64, "error": None,
        "retrieved_at": "2026-09-29T12:00:00+08:00",
        "parser_version": PARSER_VERSION,
    })


def _margin(root: Path, day: str, exchange: str, product: str, contract: str,
            long_rate: float | None, short_rate: float | None, *,
            report_day: str | None = None, interval_evidence: bool = False) -> None:
    store = MarketStore(root)
    try:
        ensure_settlement_schema(store.connection)
        report = report_day or day
        compact = report.replace("-", "")
        source_url = (f"http://www.cffex.com.cn/sj/jscs/{compact[:6]}/{compact[6:]}/{compact}_1.csv"
                      if exchange == "CFFEX" else f"https://{exchange.lower()}.example.invalid/{contract}")
        store.connection.execute(
            """INSERT INTO futures_settlement_parameter_official (
                 trade_date,exchange,contract_code,product_code,source_contract_code,
                 spec_long_rate,spec_short_rate,margin_policy,source_margin_unit,
                 source_url,payload_sha256,retrieved_at,parser_version,quality_status,
                 effective_from,effective_to,interval_source_url,interval_payload_sha256
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [report, exchange, contract, product, contract, long_rate, short_rate,
             "MAX_SPEC", "PERCENT", source_url,
             "b" * 64, "2026-09-29T12:00:00+08:00", SETTLEMENT_PARSER_VERSION,
             "PASS", day, day,
             "https://official.example.invalid/interval" if interval_evidence else None,
             "c" * 64 if interval_evidence else None],
        )
        if exchange == "CFFEX":
            store.connection.execute(
                """INSERT INTO futures_settlement_parameter_coverage
                   (trade_date,exchange,status,record_count,usable_count,source_url,
                    payload_sha256,retrieved_at,parser_version,error)
                   VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
                [report, exchange, "PASS", 1, 1,
                 source_url,
                 "b" * 64, "2026-09-29T12:00:00+08:00", SETTLEMENT_PARSER_VERSION],
            )
    finally:
        store.close()


def _root(tmp_path: Path, *, gap: bool = False, zero_base: bool = False,
          zero_volume: bool = False, late_month: bool = False,
          late_cffex_month: bool = False, new_product: bool = False) -> Path:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        for day_index, day in enumerate(_DAYS):
            for exchange, (product, contract) in _VENUES.items():
                if gap and day_index == 2 and exchange == "GFEX":
                    _record(store, day, exchange, [], "FAILED")
                    continue
                oi = 0 if zero_base and exchange == "DCE" and day_index == 0 else 100
                volume = 0 if zero_volume and exchange == "DCE" and day_index == 2 else 10
                rows = [_fact(day, exchange, product, contract, oi=oi, volume=volume)]
                if exchange == "SHFE":
                    # The second CU contract is three times larger.  On day 2,
                    # +10% and -10% become -5% after position weighting.
                    rows[0]["settlement"] = 110 if day_index == 1 else 100
                    rows.append(_fact(
                        day, exchange, product, "CU2612", oi=300,
                        settlement=90 if day_index == 1 else 100,
                    ))
                    if late_month and day_index >= len(_DAYS) - 3:
                        rows.append(_fact(day, exchange, product, "CU2701", oi=100,
                                          settlement=100 + 10 * (day_index - len(_DAYS) + 3)))
                if late_cffex_month and exchange == "CFFEX" and day_index >= len(_DAYS) - 3:
                    rows.append(_fact(day, exchange, product, "IF2703", oi=50,
                                      settlement=120 + 5 * (day_index - len(_DAYS) + 3)))
                if new_product and exchange == "DCE" and day_index >= len(_DAYS) - 3:
                    rows.append(_fact(day, exchange, "PP", "PP2701", oi=50,
                                      settlement=200 + 10 * (day_index - len(_DAYS) + 3)))
                _record(store, day, exchange, rows)
    finally:
        store.close()
    calendar_path = root / "state" / "cn_futures_trading_calendar.json"
    calendar_path.parent.mkdir(parents=True, exist_ok=True)
    calendar_path.write_text(json.dumps({
        "schema": CALENDAR_SCHEMA, "provider": CALENDAR_PROVIDER,
        "market": "CN_FUTURE", "retrievedAt": "2026-09-29T12:00:00+08:00",
        "tradingDays": [*_DAYS, "2026-09-30", "2026-10-08"],
    }), encoding="utf-8")
    return root


def test_product_return_is_contract_oi_weighted_and_market_total_requires_sources(tmp_path: Path) -> None:
    root = _root(tmp_path, gap=True)
    result = market_distributions(root, metric="return", range_name="all", window=1)
    copper = next(series for series in result["series"] if series["key"] == "SHFE.CU")
    assert copper["values"][1] == pytest.approx(-5.0)
    assert copper["xValues"][1] == pytest.approx(-5.0)
    assert copper["yValues"][1] == pytest.approx(-5.0)
    assert result["totals"][2] is None
    assert result["coverage"][2]["missingExchanges"] == ["GFEX"]
    assert copper["weeklyStreakValues"][3] == -1  # The unfinished week cannot change it.


def test_direction_uses_weighted_daily_returns_and_preserves_gaps(tmp_path: Path) -> None:
    root = _root(tmp_path, gap=True)
    result = market_distributions(root, metric="return", range_name="all")
    copper = next(series for series in result["series"] if series["key"] == "SHFE.CU")
    silicon = next(series for series in result["series"] if series["key"] == "GFEX.SI")
    assert result["directionFormulaVersion"] == bubble_module.DIRECTION_FORMULA_VERSION
    assert copper["directionSumValues"] == pytest.approx([0, -5, -5, -5])
    assert copper["directionUpCountValues"] == [0, 0, 0, 0]
    assert copper["directionDownCountValues"] == [0, 1, 1, 1]
    assert copper["directionFlatCountValues"] == [1, 1, 2, 3]
    assert copper["directionWindowCountValues"] == [1, 2, 3, 4]
    assert copper["directionStatusValues"] == ["SHORT"] * 4
    assert silicon["directionStatusValues"] == ["SHORT", "SHORT", "MISSING", "MISSING"]
    assert silicon["directionSumValues"][2:] == [None, None]
    assert silicon["directionUpCountValues"][2:] == [None, None]
    assert "2026-09-28" in silicon["directionReasonValues"][3]


def test_short_move_uses_three_verified_trading_days_without_future_leakage() -> None:
    days = ["2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05"]
    records = {day: {"openInterest": 1, "weightedReturn": value}
               for day, value in zip(days, [.01, -.015, .015, .0196], strict=True)}
    points = bubble_module._short_move_3_points(days, records, calendar_verified=True)
    assert points[:2] == [None, None]
    assert points[2:] == pytest.approx([1, 1.96])
    # Friday-to-Monday counts as adjacent verified trading dates, not calendar days.
    assert bubble_module._short_move_3_points(days[:3], records, calendar_verified=True)[-1] == pytest.approx(1)
    assert points[-1:] == pytest.approx([1.96])  # Display slicing must preserve prior history.
    assert bubble_module._short_move_3_points(days, records, calendar_verified=False) == [None] * 4
    records[days[1]]["openInterest"] = 0
    assert bubble_module._short_move_3_points(days, records, calendar_verified=True)[-2:] == [None, None]
    records[days[1]]["openInterest"] = 1
    records[days[1]]["weightedReturn"] = None
    assert bubble_module._short_move_3_points(days, records, calendar_verified=True)[-1] is None


def test_short_move_api_and_stale_calendar_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path, gap=True)
    result = market_distributions(root, metric="return", range_name="all")
    assert result["shortMoveFormulaVersion"] == bubble_module.SHORT_MOVE_FORMULA_VERSION
    copper = next(series for series in result["series"] if series["key"] == "SHFE.CU")
    assert copper["shortMove3Values"][:2] == [None, None]
    assert copper["shortMove3Values"][2:] == pytest.approx([-5, -5])
    silicon = next(series for series in result["series"] if series["key"] == "GFEX.SI")
    assert silicon["shortMove3Values"][2:] == [None, None]
    snapshot = market_distributions(root, metric="return", range_name="1y", snapshot_only=True)
    assert next(series for series in snapshot["series"] if series["key"] == "SHFE.CU")["shortMove3Values"] == copper["shortMove3Values"]
    calendar = root / "state" / "cn_futures_trading_calendar.json"
    calendar.unlink()
    stale = market_distributions(root, metric="return", range_name="all", snapshot_only=True)
    assert all(all(value is None for value in series["shortMove3Values"]) for series in stale["series"])


def test_short_rank_needs_complete_three_day_coverage_and_uses_midranks() -> None:
    days = ["2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05"]
    coverage = {day: {"missingExchanges": []} for day in days}
    moves = {f"P{index}": [None, None, float(index // 2), float(index)] for index in range(20)}
    ranks, statuses, counts = bubble_module._short_move_rank_points(days, coverage, moves, calendar_verified=True)
    assert statuses == ["INCOMPLETE", "INCOMPLETE", "COMPLETE", "COMPLETE"]
    assert counts == [None, None, 20, 20]
    assert ranks["P0"][2] == ranks["P1"][2] == pytest.approx(.05)
    assert ranks["P18"][2] == ranks["P19"][2] == pytest.approx(.95)
    assert ranks["P0"][3] == pytest.approx(.025)
    # The later comparison cannot influence the earlier day or range slicing.
    older, _, _ = bubble_module._short_move_rank_points(days[:3], coverage,
        {key: values[:3] for key, values in moves.items()}, calendar_verified=True)
    assert older["P19"][-1] == ranks["P19"][2]
    missing_one = dict(moves)
    missing_one["P0"] = [None] * 4
    insufficient, state, counts = bubble_module._short_move_rank_points(days, coverage, missing_one, calendar_verified=True)
    assert counts[-1] == 19 and state[-1] == "INCOMPLETE" and all(series[-1] is None for series in insufficient.values())
    coverage[days[1]]["missingExchanges"] = ["DCE"]
    incomplete, state, counts = bubble_module._short_move_rank_points(days, coverage, moves, calendar_verified=True)
    assert state[2:] == ["INCOMPLETE", "INCOMPLETE"]
    assert counts[2:] == [None, None]
    assert all(series[-1] is None for series in incomplete.values())
    _, state, _ = bubble_module._short_move_rank_points(days, coverage, moves, calendar_verified=False)
    assert state == ["INCOMPLETE"] * 4


def test_direction_window_rolls_and_gap_is_not_bridged() -> None:
    start = date(2026, 1, 1)
    days = [(start + timedelta(days=index)).isoformat() for index in range(71)]
    records = {day: {"weightedReturn": .01, "openInterest": 1} for day in days}
    records[days[60]]["weightedReturn"] = -.02
    records[days[61]]["weightedReturn"] = 0
    records[days[62]]["weightedReturn"] = 1e-14  # Floating-point cancellation is a flat day.
    points = bubble_module._direction_points(days, records, calendar_verified=True)
    assert points[0] == {"sum": pytest.approx(1), "up": 1, "down": 0, "flat": 0,
                         "count": 1, "status": "SHORT", "reason": None}
    assert points[59]["sum"] == pytest.approx(60)
    assert points[59]["status"] == "FULL"
    assert points[60]["sum"] == pytest.approx(57)  # Old +1 expires and −2 enters.
    assert (points[61]["up"], points[61]["down"], points[61]["flat"]) == (58, 1, 1)
    assert (points[62]["up"], points[62]["down"], points[62]["flat"]) == (57, 1, 2)
    records[days[10]]["openInterest"] = 0
    points = bubble_module._direction_points(days, records, calendar_verified=True)
    assert points[10]["status"] == points[69]["status"] == "MISSING"
    assert points[69]["sum"] is None and "缺少有效持仓" in points[69]["reason"]
    assert points[70]["status"] == "FULL"  # The zero-OI day has exited the window.
    records[days[10]]["openInterest"] = 1
    records[days[10]]["weightedReturn"] = None
    assert bubble_module._direction_points(days, records, calendar_verified=True)[11]["sum"] is None


def test_direction_rejects_missing_or_changed_calendar_even_for_saved_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    live = market_distributions(root, metric="return", range_name="all")
    assert live["series"][0]["directionStatusValues"][-1] == "SHORT"
    snapshot = market_distributions(root, metric="return", range_name="all", snapshot_only=True)
    assert snapshot["series"][0]["directionStatusValues"][-1] == "SHORT"
    calendar_path = root / "state" / "cn_futures_trading_calendar.json"
    payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    payload["tradingDays"].remove("2026-09-28")
    calendar_path.write_text(json.dumps(payload), encoding="utf-8")
    stale = market_distributions(root, metric="return", range_name="all", snapshot_only=True)
    assert stale["series"][0]["directionStatusValues"] == ["CALENDAR_UNVERIFIED"] * len(_DAYS)
    assert stale["series"][0]["directionSumValues"] == [None] * len(_DAYS)
    calendar_path.unlink()
    missing = market_distributions(root, metric="return", range_name="all")
    assert missing["series"][0]["directionStatusValues"] == ["CALENDAR_UNVERIFIED"] * len(_DAYS)


def test_bubble_product_sums_every_month_before_display(tmp_path: Path) -> None:
    root = _root(tmp_path)
    position = market_distributions(root, metric="open_interest", range_name="all")
    turnover = market_distributions(root, metric="turnover", range_name="all")
    notional = market_distributions(root, metric="notional", range_name="all")
    def row(payload: dict) -> dict:
        return next(item for item in payload["series"] if item["key"] == "SHFE.CU")
    multiplier = load_official_multiplier_registry().resolve(_DAYS[1], "SHFE", "CU").contract_multiplier
    assert row(position)["values"][1] == 400  # CU2611 and CU2612, one-sided OI.
    assert row(turnover)["values"][1] == 2000
    assert row(notional)["values"][1] == pytest.approx((100 * 110 + 300 * 90) * multiplier)


def test_deposit_sums_each_month_with_its_own_two_sided_margin(tmp_path: Path) -> None:
    root = _root(tmp_path)
    day = _DAYS[1]
    before = market_distributions(root, metric="deposit", range_name="all")
    copper = next(item for item in before["series"] if item["key"] == "SHFE.CU")
    assert copper["values"][1] is None
    _margin(root, day, "SHFE", "CU", "CU2611", .10, .12)
    _margin(root, day, "SHFE", "CU", "CU2612", .08, .09)
    after = market_distributions(root, metric="deposit", range_name="all")
    copper = next(item for item in after["series"] if item["key"] == "SHFE.CU")
    multiplier = load_official_multiplier_registry().resolve(day, "SHFE", "CU").contract_multiplier
    assert copper["values"][1] == pytest.approx(((100 * 110) * .22 + (300 * 90) * .17) * multiplier)
    assert copper["missingMarginContractCountValues"][1] == 0
    assert copper["depositReasonValues"][1] is None
    assert after["dataVersion"] != before["dataVersion"]
    assert next(item for item in market_distributions(root, metric="turnover", range_name="all")["series"]
                if item["key"] == "SHFE.CU")["values"][1] == 2000


def test_deposit_snapshot_is_published_only_after_margin_enrichment(tmp_path: Path) -> None:
    root = _root(tmp_path)
    market_distributions(root, metric="notional", range_name="all")
    assert market_distributions(root, metric="deposit", range_name="all", snapshot_only=True)["available"] is False
    market_distributions(root, metric="deposit", range_name="all")
    cached = market_distributions(root, metric="deposit", range_name="all", snapshot_only=True)
    assert cached["available"] is True
    assert cached["snapshotStatus"] == "CACHED_AWAITING_REFRESH"
    assert cached["dataVersion"] == market_distributions(
        root, metric="notional", range_name="all", snapshot_only=True,
    )["dataVersion"]
    assert market_distributions(root, metric="return", range_name="all", snapshot_only=True)["available"] is False


def test_deposit_snapshot_never_claims_uncomputed_older_range(tmp_path: Path) -> None:
    root = _root(tmp_path)
    market_distributions(root, metric="deposit", range_name="1y")
    assert market_distributions(root, metric="deposit", range_name="1y", snapshot_only=True)["available"] is True
    assert market_distributions(root, metric="deposit", range_name="all", snapshot_only=True)["available"] is False
    market_distributions(root, metric="deposit", range_name="all")
    assert market_distributions(root, metric="deposit", range_name="all", snapshot_only=True)["available"] is True


def test_snapshot_prefers_source_date_and_version_over_late_cache_rewrite(tmp_path: Path) -> None:
    root = tmp_path / "data"
    for key, day, source_stamp, built_at in (
        ("current", "2026-10-16", "2026-10-16T18:00:00+08:00", 2),
        ("old-version", "2026-10-16", "2026-10-15T18:00:00+08:00", 1),
        ("old-day", "2026-10-09", "2026-10-17T18:00:00+08:00", 3),
    ):
        bubble_module._write_core_snapshot(root, (key,), {
            "latestDay": day, "dates": [day], "weeklyStreak": {},
            "sourceVersionStamp": source_stamp, "sourceBuiltAtNs": built_at,
        })
    selected = bubble_module._read_latest_core_snapshot(root, require_ten_day=False)
    assert selected is not None
    assert selected[0]["latestDay"] == "2026-10-16"
    assert selected[0]["sourceVersionStamp"] == "2026-10-16T18:00:00+08:00"


def test_concurrent_deposit_range_refresh_cannot_clear_earlier_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    day = _DAYS[1]
    _margin(root, day, "SHFE", "CU", "CU2611", .10, .12)
    _margin(root, day, "SHFE", "CU", "CU2612", .08, .09)
    market_distributions(root, metric="deposit", range_name="1y")
    building = threading.Event()
    resume_first = threading.Event()
    cleared = threading.Event()
    resume_second = threading.Event()
    original_rank = bubble_module._rank_value
    original_enrich = bubble_module._enrich_deposit

    def pause_first_response(value: float | None) -> float:
        if not building.is_set():
            building.set()
            assert resume_first.wait(10)
        return original_rank(value)

    def pause_second_enrichment(connection, core, data_root, start_day, end_day):
        if start_day == "2023-01-01":
            for records in core["products"].values():
                for point in records.values():
                    point["deposit"] = None
            cleared.set()
            assert resume_second.wait(10)
        return original_enrich(connection, core, data_root, start_day, end_day)

    monkeypatch.setattr(bubble_module, "_rank_value", pause_first_response)
    monkeypatch.setattr(bubble_module, "_enrich_deposit", pause_second_enrichment)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="deposit") as pool:
        first = pool.submit(market_distributions, root, metric="deposit", range_name="1y")
        try:
            assert building.wait(10)
            second = pool.submit(market_distributions, root, metric="deposit", range_name="all")
            assert cleared.wait(10)
            resume_first.set()
            payload = first.result(timeout=10)
            copper = next(item for item in payload["series"] if item["key"] == "SHFE.CU")
            assert copper["values"][1] is not None
        finally:
            resume_first.set()
            resume_second.set()
        second.result(timeout=10)


def test_missing_month_margin_keeps_product_deposit_null_and_zero_oi_is_zero(tmp_path: Path) -> None:
    root = _root(tmp_path, zero_base=True)
    day = _DAYS[1]
    _margin(root, day, "SHFE", "CU", "CU2611", .10, .12)
    payload = market_distributions(root, metric="deposit", range_name="all")
    copper = next(item for item in payload["series"] if item["key"] == "SHFE.CU")
    coal = next(item for item in payload["series"] if item["key"] == "DCE.JM")
    assert copper["values"][1] is None
    assert copper["missingMarginContractCountValues"][1] == 1
    assert "1个有持仓月份合约" in copper["depositReasonValues"][1]
    assert coal["values"][0] == 0


def test_cffex_latest_verified_publication_applies_until_next_publication(tmp_path: Path) -> None:
    root = _root(tmp_path)
    day = _DAYS[1]
    _margin(root, day, "CFFEX", "IF", "IF2611", .1, .12, report_day=_DAYS[0])
    with_interval = market_distributions(root, metric="deposit", range_name="all")
    index = next(item for item in with_interval["series"] if item["key"] == "CFFEX.IF")
    assert index["values"][1] is not None
    assert index["missingMarginContractCountValues"][1] == 0
    assert index["marginPublicationDayValues"][1] == _DAYS[0]


def test_cffex_new_publication_missing_contract_does_not_reuse_older_rate(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _margin(root, _DAYS[1], "CFFEX", "IF", "IF2611", .1, .12, report_day=_DAYS[0])
    _margin(root, _DAYS[2], "CFFEX", "IF", "IF2703", .2, .2, report_day=_DAYS[1])
    payload = market_distributions(root, metric="deposit", range_name="all")
    index = next(item for item in payload["series"] if item["key"] == "CFFEX.IF")
    assert index["values"][0] is not None
    assert index["values"][1] is None
    assert index["marginPublicationDayValues"][1] == _DAYS[1]
    assert index["missingMarginContractCountValues"][1] == 1


def test_new_partial_cffex_publication_blocks_old_rate_and_invalidates_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _margin(root, _DAYS[1], "CFFEX", "IF", "IF2611", .1, .12, report_day=_DAYS[0])
    before = market_distributions(root, metric="deposit", range_name="all")
    index = next(item for item in before["series"] if item["key"] == "CFFEX.IF")
    assert index["values"][1] is not None
    compact = _DAYS[1].replace("-", "")
    url = f"http://www.cffex.com.cn/sj/jscs/{compact[:6]}/{compact[6:]}/{compact}_1.csv"
    store = MarketStore(root)
    try:
        store.connection.execute(
            """INSERT INTO futures_settlement_parameter_coverage
               (trade_date,exchange,status,record_count,usable_count,source_url,
                payload_sha256,retrieved_at,parser_version,error)
               VALUES (?,'CFFEX','PARTIAL',1,0,?,?,?,?,'INCOMPLETE_ROWS')""",
            [_DAYS[1], url, "d" * 64, "2026-09-29T12:00:00+08:00", SETTLEMENT_PARSER_VERSION],
        )
    finally:
        store.close()
    after = market_distributions(root, metric="deposit", range_name="all")
    index = next(item for item in after["series"] if item["key"] == "CFFEX.IF")
    assert after["dataVersion"] != before["dataVersion"]
    assert index["values"][1] is None
    assert index["missingMarginContractCountValues"][1] == 1


def test_non_cffex_prior_margin_report_cannot_supply_deposit(tmp_path: Path) -> None:
    root = _root(tmp_path)
    day = _DAYS[1]
    for contract in ("CU2611", "CU2612"):
        _margin(root, day, "SHFE", "CU", contract, .1, .12,
                report_day=_DAYS[0], interval_evidence=True)
    result = market_distributions(root, metric="deposit", range_name="all")
    copper = next(item for item in result["series"] if item["key"] == "SHFE.CU")
    assert copper["values"][1] is None
    assert copper["missingMarginContractCountValues"][1] == 2


def test_return_bubble_uses_daily_change_even_without_twenty_day_history(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = market_distributions(root, metric="return", range_name="all", window=20)
    copper = next(series for series in result["series"] if series["key"] == "SHFE.CU")
    assert result["formulaVersion"].endswith("v15")
    assert copper["yValues"][1] == pytest.approx(copper["values"][1])
    assert copper["yValues"][1] == pytest.approx(-5)
    assert copper["xValues"][1] == pytest.approx(-5)
    assert copper["sizeValues"][1] is not None


def test_product_weighted_settlement_survives_new_month_without_listing(tmp_path: Path, monkeypatch) -> None:
    days = []
    cursor = date(2026, 9, 14)
    while len(days) < 11:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    monkeypatch.setattr(sys.modules[__name__], "_DAYS", tuple(days))
    root = _root(tmp_path, late_month=True, late_cffex_month=True)
    result = market_distributions(root, metric="return", range_name="all")
    copper = next(series for series in result["series"] if series["key"] == "SHFE.CU")
    index_future = next(series for series in result["series"] if series["key"] == "CFFEX.IF")
    # CU's first endpoint has two months at 110/90 with OI 100/300; the last
    # endpoint includes a third month at 120 with OI 100.
    assert copper["tenDayReturnValues"][-1] == pytest.approx(100 * (104 / 95 - 1))
    assert copper["tenDayCoverageValues"][-1] == 1
    assert copper["tenDayWindowMinValues"][-1] == 10
    assert index_future["tenDayReturnValues"][-3] is None  # Only nine product days so far.
    assert index_future["tenDayReturnValues"][-2:] == pytest.approx([
        100 * (108.3333333333 / 100 - 1), 10,
    ])
    assert index_future["tenDayCoverageValues"][-2:] == [1, 1]


def test_new_product_short_window_requires_verified_product_listing(tmp_path: Path, monkeypatch) -> None:
    days = []
    cursor = date(2026, 9, 14)
    while len(days) < 11:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    monkeypatch.setattr(sys.modules[__name__], "_DAYS", tuple(days))
    root = _root(tmp_path, new_product=True)
    without = market_distributions(root, metric="return", range_name="all")
    plastic = next(series for series in without["series"] if series["key"] == "DCE.PP")
    assert plastic["tenDayReturnValues"][-1] is None
    store = MarketStore(root)
    try:
        store.connection.execute(
            "INSERT INTO futures_product_listing_evidence VALUES (?,?,?,?,?,?,?,?)",
            ["DCE.PP", "https://www.dce.com.cn/products/fixture", "b" * 64,
             "2026-09-29T12:00:00+08:00", days[-3], "DAY", "PASS", "synthetic listing"],
        )
    finally:
        store.close()
    with_listing = market_distributions(root, metric="return", range_name="all")
    plastic = next(series for series in with_listing["series"] if series["key"] == "DCE.PP")
    assert plastic["tenDayReturnValues"][-3:] == pytest.approx([0, 5, 10])
    assert plastic["tenDayWindowMinValues"][-1] == 3
    assert plastic["shortWindowContractCountValues"][-1] is None
    assert with_listing["dataVersion"] != without["dataVersion"]


def test_product_weighted_return_breaks_on_a_missing_exchange_day(tmp_path: Path, monkeypatch) -> None:
    days = []
    cursor = date(2026, 9, 14)
    while len(days) < 11:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    monkeypatch.setattr(sys.modules[__name__], "_DAYS", tuple(days))
    silicon = next(series for series in market_distributions(
        _root(tmp_path, gap=True), metric="return", range_name="all",
    )["series"] if series["key"] == "GFEX.SI")
    assert silicon["tenDayReturnValues"][-1] is None


def test_axis_speed_uses_adjacent_trading_days_and_preserves_missing_and_zero(tmp_path: Path) -> None:
    root = _root(tmp_path, gap=True)
    copper = next(item for item in market_distributions(
        root, metric="return", range_name="all", window=60,
    )["series"] if item["key"] == "SHFE.CU")
    assert copper["yValues"][:3] == pytest.approx([0, -5, 0])
    assert copper["xValues"][0] is None
    assert copper["xValues"][1:3] == pytest.approx([-5, 5])
    silicon = next(item for item in market_distributions(
        root, metric="return", range_name="all", window=1,
    )["series"] if item["key"] == "GFEX.SI")
    assert silicon["yValues"][2] is None
    assert silicon["yValues"][3] == 0
    assert silicon["xValues"][2:4] == [None, None]
    assert _daily_horizontal_change(3, 4) == -1
    assert _daily_horizontal_change(0, 0) == 0
    assert _daily_horizontal_change(None, 4) is None


def test_first_visible_day_uses_prior_hidden_trading_day_and_formula_changes_data_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_DAYS", (
        "2025-09-29", "2025-09-30", "2026-09-29", "2026-09-30",
    ))
    root = _root(tmp_path)
    result = market_distributions(root, metric="return", range_name="1y", window=20)
    assert result["dates"][0] == "2025-09-30"
    copper = next(item for item in result["series"] if item["key"] == "SHFE.CU")
    assert copper["yValues"][0] == pytest.approx(-5)
    assert copper["xValues"][0] == pytest.approx(-5)
    monkeypatch.setattr(bubble_module, "FORMULA_VERSION", "futures-market-distributions-official-v10-test")
    revised = market_distributions(root, metric="return", range_name="1y", window=20)
    assert revised["dataVersion"] != result["dataVersion"]


@pytest.mark.parametrize("window", [1, 5, 20, 60])
@pytest.mark.parametrize("metric,field", [
    ("open_interest", "openInterest"), ("notional", "notional"),
    ("turnover", "turnover"), ("volume", "volume"),
    ("return", "weightedReturn"),
])
def test_horizontal_window_values_have_daily_percentage_point_speed(
    window: int, metric: str, field: str,
) -> None:
    days = [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(130)]
    def raw(index: int) -> float:
        return float(100 + index * index)
    records = {day: {field: float(100 + index * index) if metric != "return"
                     else (index - 65) / 1000} for index, day in enumerate(days)}
    current = _horizontal_value(records, days, 125, window, metric, field)
    previous = _horizontal_value(records, days, 124, window, metric, field)
    assert current is not None and previous is not None
    if metric == "return":
        expected_current, expected_previous = 6.0, 5.9
    elif metric in {"turnover", "volume"}:
        def activity(end: int) -> float:
            return 100 * (
                sum(raw(index) for index in range(end - window + 1, end + 1))
                / sum(raw(index) for index in range(end - 2 * window + 1, end - window + 1)) - 1
            )
        expected_current, expected_previous = activity(125), activity(124)
    else:
        expected_current = 100 * (raw(125) / raw(125 - window) - 1)
        expected_previous = 100 * (raw(124) / raw(124 - window) - 1)
    assert current == pytest.approx(expected_current)
    assert previous == pytest.approx(expected_previous)
    assert _daily_horizontal_change(current, previous) == pytest.approx(expected_current - expected_previous)
    records[days[124]][field] = None
    assert _horizontal_value(records, days, 124, window, metric, field) is None
    if metric == "return" or window == 1:
        assert _daily_horizontal_change(
            _horizontal_value(records, days, 125, window, metric, field),
            _horizontal_value(records, days, 124, window, metric, field),
        ) is None


def test_zero_base_has_no_percent_change_but_preserves_true_zero(tmp_path: Path) -> None:
    root = _root(tmp_path, zero_base=True)
    result = market_distributions(root, metric="open_interest", range_name="all", window=1)
    coal = next(series for series in result["series"] if series["key"] == "DCE.JM")
    assert coal["values"][0] == 0
    assert coal["yValues"][1] is None
    assert coal["xValues"][1] is None
    assert coal["sizeValues"][0] == 0
    assert result["totals"][0] is not None


def test_turnover_compares_equal_length_windows_and_notional_uses_multiplier(tmp_path: Path) -> None:
    root = _root(tmp_path)
    amount = market_distributions(root, metric="turnover", range_name="all", window=1)
    copper = next(series for series in amount["series"] if series["key"] == "SHFE.CU")
    assert copper["values"][0] == 2000
    assert copper["yValues"][1] == 0
    assert copper["xValues"][1] is None
    assert copper["xValues"][2] == 0
    assert amount["totals"][0] == 7000
    notional = market_distributions(root, metric="notional", range_name="all", window=1)
    copper_value = next(series for series in notional["series"] if series["key"] == "SHFE.CU")
    assert copper_value["values"][0] == 400 * 100 * 5


def test_genuine_zero_bubble_area_survives_when_axes_are_valid(tmp_path: Path) -> None:
    result = market_distributions(_root(tmp_path, zero_volume=True), metric="volume", range_name="all", window=1)
    coal = next(series for series in result["series"] if series["key"] == "DCE.JM")
    assert coal["values"][2] == 0
    assert coal["xValues"][2] == -100
    assert coal["yValues"][2] == -100
    assert coal["sizeValues"][2] == 0


def test_sha_mismatch_does_not_publish_a_complete_market_total(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = MarketStore(root)
    try:
        store.connection.execute(
            "UPDATE futures_contract_daily_official_coverage SET payload_sha256=? "
            "WHERE trade_date=? AND exchange=?",
            ["b" * 64, _DAYS[-1], "DCE"],
        )
    finally:
        store.close()
    result = market_distributions(root, metric="open_interest", range_name="all", window=1)
    assert result["coverage"][-1]["missingExchanges"] == ["DCE"]
    assert result["totals"][-1] is None


def test_metric_and_window_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="metric"):
        market_distributions(tmp_path, metric="unknown")
    with pytest.raises(ValueError, match="window"):
        market_distributions(tmp_path, window=2)


def test_market_distribution_api_returns_aligned_matrices(tmp_path: Path) -> None:
    root = _root(tmp_path)
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    response = client.get("/api/futures/market-distributions", params={"metric": "return", "range": "all", "window": 1})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["dates"]) == len(payload["totals"]) == len(payload["coverage"])
    assert all(len(series["xValues"]) == len(payload["dates"]) for series in payload["series"])
    deposit = client.get("/api/futures/market-distributions", params={"metric": "deposit", "range": "all"})
    assert deposit.status_code == 200
    deposit_payload = deposit.json()
    assert deposit_payload["unit"] == "CNY"
    assert deposit_payload["dates"] == payload["dates"]
    assert deposit_payload["dataVersion"] == payload["dataVersion"]
    assert all(len(series["depositReasonValues"]) == len(payload["dates"])
               for series in deposit_payload["series"])
    assert client.get("/api/futures/market-distributions", params={"metric": "unknown"}).status_code == 422


def test_gold_weekly_streak_takes_priority_over_descriptive_daily_chain(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = MarketStore(root)
    try:
        store.connection.execute(
            "CREATE TABLE futures_weekly_heat_product_gold (formula_version VARCHAR, "
            "week_end_date VARCHAR, product_key VARCHAR, payload_json VARCHAR, calculated_at VARCHAR)"
        )
        store.connection.execute(
            "INSERT INTO futures_weekly_heat_product_gold VALUES (?,?,?,?,?)",
            [WEEKLY_FORMULA_VERSION, "2026-09-25", "SHFE.CU",
             json.dumps({"streakWeeks": 4}), "2026-10-04T00:00:00+00:00"],
        )
    finally:
        store.close()
    result = market_distributions(root, metric="return", range_name="all", window=1)
    copper = next(item for item in result["series"] if item["key"] == "SHFE.CU")
    assert copper["weeklyStreakValues"][1] == 4
    assert copper["weeklyStreakValues"][3] == 4  # Current week has not finished.
    assert copper["xValues"][1] == pytest.approx(-5)
