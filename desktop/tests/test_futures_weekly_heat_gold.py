"""A weekly Gold rebuild preserves dated versions and read scope."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread

from fastapi.testclient import TestClient
import pytest

from market_monitor.futures_calendar import sync_futures_trading_calendar
from market_monitor.futures_weekly_heat_gold import (
    _next_day_previous_settlement_seed, publish_existing_weekly_heat_gold_snapshot,
    read_weekly_heat_gold, rebuild_weekly_heat_gold,
)
from market_monitor.storage import MarketStore
from market_monitor.web_app import create_web_app


def test_weekly_gold_idempotent_and_product_filter(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-24",), ("2026-09-25",)])
    store = MarketStore(root)
    store.close()
    assert read_weekly_heat_gold(root)["available"] is False
    point = {"tradeDate": "2026-09-25", "weekEndDate": "2026-09-25",
             "totalScore": 42.0, "score": 42.0, "breadthScore": 60.0,
             "streakScore": 45.0, "magnitudeScore": 30.0,
             "dataQualityStatus": "PASS", "pendingWeek": None,
             "coverage": {"direction": 1.0, "turnover": 1.0, "missingExchanges": []}}
    product = {**point, "productKey": "SHFE.AU", "streakWeeks": 4,
               "weightLog2": 3, "contracts": [{"contractCode": "AU2612"}]}
    synthetic = {"points": [point], "productPoints": [product],
                 "productDailyPoints": [product], "latest": point}
    monkeypatch.setattr("market_monitor.futures_weekly_heat_gold.calculate_weekly_heat", lambda *_args, **_kwargs: synthetic)
    for _ in range(2):
        report = rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
        assert report["writtenDays"] == report["writtenProductWeeks"] == report["writtenProductDays"] == 1
    full = read_weekly_heat_gold(root)
    assert full["latest"]["totalScore"] == 42
    selected = read_weekly_heat_gold(root, product_key="SHFE.AU")
    assert selected["latest"]["streakWeeks"] == 4
    assert selected["latest"]["totalScore"] == 42
    assert selected["latest"]["streakScore"] == 45
    missing = read_weekly_heat_gold(root, product_key="DCE.M")
    assert missing["latest"]["totalScore"] is None
    assert missing["latest"]["streakScore"] is None
    client = TestClient(create_web_app(root, auto_update_on_startup=False))
    response = client.get("/api/futures/weekly-heat", params={"range": "all", "product_key": "SHFE.AU"})
    assert response.status_code == 200
    assert response.json()["latest"]["streakWeeks"] == 4
    assert response.json()["latest"]["streakScore"] == 45
    assert client.get("/api/futures/weekly-heat", params={"range": "10y"}).status_code == 422
    store = MarketStore(root)
    try:
        assert store.connection.execute("SELECT count(*) FROM futures_weekly_heat_gold").fetchone()[0] == 1
        assert store.connection.execute("SELECT count(*) FROM futures_weekly_heat_product_gold").fetchone()[0] == 1
        assert store.connection.execute("SELECT count(*) FROM futures_daily_heat_product_gold").fetchone()[0] == 1
    finally:
        store.close()


def test_derived_seed_needs_full_next_day_source_sha_and_calendar_adjacency():
    days = ("2022-12-30", "2023-01-03")
    facts = [{"trade_date": "2023-01-03", "exchange": "DCE", "contract_code": "M2305",
              "adapter": "official-dce-daily-v2", "quality_status": "PASS",
              "parser_version": "official-futures-daily-v4", "payload_sha256": "a" * 64,
              "previous_settlement": 3000}]
    coverage = [{"trade_date": "2023-01-03", "exchange": "DCE",
                 "adapter": "official-dce-daily-v2", "status": "PASS",
                 "parser_version": "official-futures-daily-v4",
                 "record_count": 1, "usable_count": 1, "payload_sha256": "a" * 64}]
    values, evidence = _next_day_previous_settlement_seed(facts, coverage, days, "2023-01-03")
    assert values == {("2022-12-30", "DCE", "M2305"): 3000.0}
    assert evidence["sourceTradingDay"] == "2023-01-03"
    assert evidence["previousTradingDay"] == "2022-12-30"
    assert evidence["sourcePayloadSha256"] == "a" * 64
    assert _next_day_previous_settlement_seed(facts, [{**coverage[0], "payload_sha256": "b" * 64}], days, "2023-01-03") == ({}, None)


def test_product_daily_gold_does_not_copy_friday_score_to_thursday(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-24",), ("2026-09-25",)])
    days = ["2026-09-24", "2026-09-25"]
    points = [{"tradeDate": day, "weekEndDate": "2026-09-18", "totalScore": 20 + index,
               "dataQualityStatus": "PASS", "pendingWeek": None}
              for index, day in enumerate(days)]
    product_days = [{**point, "productKey": "SHFE.AG", "totalScore": 10 + index,
                     "upCount": 2 + index, "streakScore": 0}
                    for index, point in enumerate(points)]
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: {"points": points, "productPoints": [],
                                  "productDailyPoints": product_days, "latest": points[-1]},
    )
    report = rebuild_weekly_heat_gold(root, as_of_day=days[-1])
    assert report["writtenProductDays"] == 2
    selected = read_weekly_heat_gold(root, product_key="SHFE.AG")
    assert [point["totalScore"] for point in selected["points"]] == [10, 11]
    assert [point["upCount"] for point in selected["points"]] == [2, 3]


def test_weekly_gold_does_not_count_pre_week_warmup_as_partial(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2023-01-03",), ("2023-01-06",)])
    warmup = {"tradeDate": "2023-01-03", "weekEndDate": None, "totalScore": None,
              "dataQualityStatus": "UNAVAILABLE", "pendingWeek": None, "isWarmup": True}
    completed = {"tradeDate": "2023-01-06", "weekEndDate": "2023-01-06", "totalScore": 3.0,
                 "dataQualityStatus": "PASS", "pendingWeek": None}
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: {"points": [warmup, completed], "productPoints": [],
                                  "productDailyPoints": [], "latest": completed},
    )
    report = rebuild_weekly_heat_gold(root, as_of_day="2023-01-06")
    assert report["pendingDays"] == report["partialDays"] == 0
    assert report["status"] == "PASS"


def _synthetic_heat(score: float) -> dict:
    point = {"tradeDate": "2026-09-25", "weekEndDate": "2026-09-25",
             "totalScore": score, "dataQualityStatus": "PASS", "pendingWeek": None}
    product = {**point, "productKey": "SHFE.AU", "streakScore": score}
    return {"points": [point], "productPoints": [product],
            "productDailyPoints": [product], "latest": point}


def test_weekly_heat_get_uses_stale_publication_while_writer_rebuilds(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-25",)])
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: _synthetic_heat(10),
    )
    rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    started, release = Event(), Event()

    def slow_calculation(*_args, **_kwargs):
        started.set()
        assert release.wait(10)
        return _synthetic_heat(20)

    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        slow_calculation,
    )
    worker = Thread(target=rebuild_weekly_heat_gold,
                    args=(root,), kwargs={"as_of_day": "2026-09-25"})
    worker.start()
    try:
        assert started.wait(10)
        # A simulated DuckDB connection failure cannot affect this GET.
        def forbidden_catalog(*_args, **_kwargs):
            raise AssertionError("weekly heat GET must not open DuckDB")

        monkeypatch.setattr("market_monitor.storage.duckdb.connect", forbidden_catalog)
        stale = read_weekly_heat_gold(root, product_key="SHFE.AU")
        assert stale["latest"]["totalScore"] == 10
        assert stale["snapshotStatus"] == "STALE"
        assert stale["snapshotReason"] == "REBUILD_NOT_PUBLISHED"
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    current = read_weekly_heat_gold(root, product_key="SHFE.AU")
    assert current["latest"]["totalScore"] == 20
    assert current["snapshotStatus"] == "CURRENT"


def test_weekly_heat_corrupt_current_snapshot_falls_back_to_prior_generation(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-25",)])
    for score in (10, 20):
        monkeypatch.setattr(
            "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
            lambda *_args, _score=score, **_kwargs: _synthetic_heat(_score),
        )
        rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    pointer = json.loads((root / "state" / "futures_weekly_heat_publication.json").read_text())
    current_file = pointer["current"]["products"]["SHFE.AU"]["file"]
    (root / "cache" / "futures-weekly-heat" / current_file).write_bytes(b"corrupted")
    selected = read_weekly_heat_gold(root, product_key="SHFE.AU")
    assert selected["latest"]["totalScore"] == 10
    assert selected["snapshotStatus"] == "STALE"
    assert selected["snapshotReason"] == "CURRENT_PUBLICATION_CORRUPT"
    # The aggregate file in the current generation remains independently readable.
    aggregate = read_weekly_heat_gold(root)
    assert aggregate["latest"]["totalScore"] == 20
    assert aggregate["snapshotStatus"] == "CURRENT"
    market_file = pointer["current"]["market"]["file"]
    (root / "cache" / "futures-weekly-heat" / market_file).write_bytes(b"corrupted")
    aggregate = read_weekly_heat_gold(root)
    assert aggregate["latest"]["totalScore"] == 10
    assert aggregate["snapshotStatus"] == "STALE"
    previous_file = pointer["previous"]["market"]["file"]
    (root / "cache" / "futures-weekly-heat" / previous_file).write_bytes(b"corrupted")
    unavailable = read_weekly_heat_gold(root)
    assert unavailable["available"] is False
    assert unavailable["snapshotStatus"] == "UNAVAILABLE"
    assert unavailable["snapshotReason"] == "PUBLICATION_CORRUPT"


def test_weekly_heat_empty_publication_is_distinct_from_missing_publication(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-25",)])
    assert read_weekly_heat_gold(root)["snapshotStatus"] == "UNAVAILABLE"
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: {"points": [], "productPoints": [],
                                  "productDailyPoints": [], "latest": None},
    )
    assert rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")["status"] == "NO_DATA"
    empty = read_weekly_heat_gold(root)
    assert empty["available"] is False
    assert empty["snapshotStatus"] == "EMPTY"
    assert empty["snapshotAsOfDay"] == "2026-09-25"


def test_weekly_heat_publish_failure_retains_old_snapshot_and_committed_gold(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-25",)])
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: _synthetic_heat(10),
    )
    rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: _synthetic_heat(20),
    )

    def fail_publish(*_args, **_kwargs):
        raise OSError("publication interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(
            "market_monitor.futures_weekly_heat_gold._publish_weekly_heat_snapshot",
            fail_publish,
        )
        with pytest.raises(OSError, match="publication interrupted"):
            rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    stale = read_weekly_heat_gold(root)
    assert stale["latest"]["totalScore"] == 10
    assert stale["snapshotStatus"] == "STALE"
    store = MarketStore(root)
    try:
        payload = store.connection.execute(
            "SELECT payload_json FROM futures_weekly_heat_gold",
        ).fetchone()[0]
        assert json.loads(payload)["totalScore"] == 20
    finally:
        store.close()
    rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    assert read_weekly_heat_gold(root)["snapshotStatus"] == "CURRENT"


def test_offline_existing_gold_snapshot_migration_does_not_recalculate_or_edit_gold(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-25",)])
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: _synthetic_heat(17),
    )
    rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    pointer = root / "state" / "futures_weekly_heat_publication.json"
    pointer.unlink()
    assert read_weekly_heat_gold(root)["snapshotStatus"] == "UNAVAILABLE"
    store = MarketStore(root)
    try:
        before = [store.connection.execute(
            f"SELECT * FROM {table} ORDER BY ALL",
        ).fetchall() for table in (
            "futures_weekly_heat_gold", "futures_weekly_heat_product_gold",
            "futures_daily_heat_product_gold",
        )]
    finally:
        store.close()

    def forbidden_calculation(*_args, **_kwargs):
        raise AssertionError("offline migration must not recalculate")

    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        forbidden_calculation,
    )
    report = publish_existing_weekly_heat_gold_snapshot(root)
    assert report["status"] == "PASS"
    assert report["writtenDays"] == report["writtenProductDays"] == 1
    assert read_weekly_heat_gold(root, product_key="SHFE.AU")["latest"]["totalScore"] == 17
    store = MarketStore(root)
    try:
        after = [store.connection.execute(
            f"SELECT * FROM {table} ORDER BY ALL",
        ).fetchall() for table in (
            "futures_weekly_heat_gold", "futures_weekly_heat_product_gold",
            "futures_daily_heat_product_gold",
        )]
    finally:
        store.close()
    assert after == before


def test_offline_migration_reports_missing_formula_without_publishing(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "data"
    assert publish_existing_weekly_heat_gold_snapshot(root) == {
        "status": "UNAVAILABLE", "reason": "CATALOG_MISSING",
    }
    sync_futures_trading_calendar(root, fetcher=lambda: [("2026-09-25",)])
    store = MarketStore(root)
    store.close()
    assert publish_existing_weekly_heat_gold_snapshot(root)["reason"] == "GOLD_TABLES_MISSING"
    monkeypatch.setattr(
        "market_monitor.futures_weekly_heat_gold.calculate_weekly_heat",
        lambda *_args, **_kwargs: _synthetic_heat(17),
    )
    rebuild_weekly_heat_gold(root, as_of_day="2026-09-25")
    (root / "state" / "futures_weekly_heat_publication.json").unlink()
    store = MarketStore(root)
    try:
        for table in ("futures_weekly_heat_gold", "futures_weekly_heat_product_gold",
                      "futures_daily_heat_product_gold"):
            store.connection.execute(
                f"UPDATE {table} SET formula_version='archived-only'",
            )
    finally:
        store.close()
    report = publish_existing_weekly_heat_gold_snapshot(root)
    assert report["status"] == "UNAVAILABLE"
    assert report["reason"] == "FORMULA_VERSION_MISSING"
    assert not (root / "state" / "futures_weekly_heat_publication.json").exists()
