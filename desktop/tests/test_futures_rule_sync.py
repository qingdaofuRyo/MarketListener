from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any, Iterable

import duckdb

from market_monitor.futures_rule_sync import (
    SNAPSHOT_PROVIDER,
    SNAPSHOT_RELATIVE_PATH,
    SNAPSHOT_SCHEMA,
    load_rule_book,
    sync_futures_rule_snapshots,
)


class FakeRuleTable:
    def __init__(self, rows: Iterable[list[Any]]) -> None:
        self._rows = list(rows)

    def itertuples(self, *, index: bool, name: None) -> Iterable[tuple[Any, ...]]:
        assert index is False
        assert name is None
        return (tuple(row) for row in self._rows)


def _provider_table() -> FakeRuleTable:
    # Only positions 2, 3, 5, and 8 are contractual.  Labels are intentionally
    # absent so the test cannot accidentally validate name-based parsing.
    return FakeRuleTable(
        [
            [
                "上海期货交易所",
                "铜",
                "CU",
                "16%",
                7,
                "5",
                10,
                500,
                "CU2610合约交易保证金比例为17.0%; CU2611合约涨跌幅度为10%",
                None,
            ],
            ["大连商品交易所", "豆粕", "M", 12.0, 6, 10, 1, 500, "M2611合约交易保证金为13％", None],
            ["中国金融期货交易所", "沪深300", "IF", 15, 10, 300, 0.2, 20, "", None],
            ["上海期货交易所", "铜期权", "CU_O", None, None, 5, 2, 100, "", None],
            ["上海期货交易所", "坏数据", "BAD", 0, 1, -1, 1, 1, "BAD2610合约交易保证金比例为20%", None],
        ]
    )


def _write_silver_partition(
    data_root: Path,
    name: str,
    rows: Iterable[tuple[str, str, str]],
) -> Path:
    path = (
        data_root
        / "silver"
        / "market=CN"
        / "asset_type=FUTURE"
        / "period=1d"
        / f"{name}.parquet"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE bars (instrument_id VARCHAR, bar_json VARCHAR)"
        )
        for day_text, exchange, product in rows:
            instrument_id = f"CN.{exchange}.FUTURE.{product}.WEIGHTED"
            payload = {
                "instrument_id": instrument_id,
                "market": "CN",
                "asset_type": "FUTURE",
                "period": "1d",
                "trading_date": day_text,
                "exchange": exchange,
                "product_code": product,
                "symbol": f"{product}0",
            }
            connection.execute(
                "INSERT INTO bars VALUES (?, ?)",
                [instrument_id, json.dumps(payload, ensure_ascii=False)],
            )
        escaped_path = str(path).replace("'", "''")
        connection.execute(f"COPY bars TO '{escaped_path}' (FORMAT PARQUET)")
    finally:
        connection.close()
    return path


def _weekdays(start: date, count: int) -> list[str]:
    result: list[str] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def test_sync_uses_latest_ten_silver_trading_days_and_positional_rules(tmp_path: Path) -> None:
    trading_days = _weekdays(date(2026, 7, 27), 12)
    weekend = date(2026, 8, 1).isoformat()
    silver_rows = [
        (day_text, exchange, product)
        for day_text in [*trading_days, weekend]
        for exchange, product in [
            ("SHFE", "CU"),
            ("DCE", "M"),
            ("CFFEX", "IF"),
            ("SHFE", "CU_O"),
        ]
    ]
    _write_silver_partition(tmp_path, "inventory", silver_rows)
    fetched: list[str] = []

    def fake_fetcher(day_text: str) -> FakeRuleTable:
        fetched.append(day_text)
        return _provider_table()

    result = sync_futures_rule_snapshots(
        tmp_path,
        lookback_days=10,
        fetcher=fake_fetcher,
        retrieved_at="2026-08-20T08:00:00+00:00",
    )

    expected_days = trading_days[-10:]
    assert result["status"] == "UPDATED"
    assert result["tradingDays"] == expected_days
    assert result["fetchedDayCount"] == 10
    assert fetched == [day.replace("-", "") for day in expected_days]
    assert weekend.replace("-", "") not in fetched

    path = tmp_path / SNAPSHOT_RELATIVE_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == SNAPSHOT_SCHEMA
    assert payload["provider"] == SNAPSHOT_PROVIDER
    assert payload["retrievedAt"] == "2026-08-20T08:00:00+00:00"
    assert list(payload["days"]) == expected_days
    latest = payload["days"][expected_days[-1]]
    assert latest["provider"] == SNAPSHOT_PROVIDER
    assert latest["retrievedAt"] == "2026-08-20T08:00:00+00:00"
    assert latest["products"] == {
        "DCE.M": {"contractMultiplier": 10.0, "marginRate": 0.12},
        "SHFE.CU": {"contractMultiplier": 5.0, "marginRate": 0.16},
    }
    assert latest["contractOverrides"] == {
        "DCE.M2611": {"marginRate": 0.13},
        "SHFE.CU2610": {"marginRate": 0.17},
    }
    assert all("CFFEX" not in key and "_O" not in key for key in latest["products"])

    rule_book = load_rule_book(tmp_path)
    base = rule_book.resolve(expected_days[-1], "SHFE", "cu", "cu2612")
    override = rule_book.resolve(expected_days[-1], "SHFE", "CU", "CU2610")
    assert base is not None
    assert (base.contract_multiplier, base.margin_rate, base.source) == (5.0, 0.16, "BASE")
    assert override is not None
    assert (override.contract_multiplier, override.margin_rate, override.source) == (
        5.0,
        0.17,
        "CONTRACT_OVERRIDE",
    )
    assert rule_book.resolve_multiplier(expected_days[-1], "SHFE", "CU") == 5.0
    assert rule_book.resolve("2026-07-01", "SHFE", "CU", "CU2610") is None
    assert rule_book.resolve_multiplier("2026-07-01", "SHFE", "CU") is None
    assert rule_book.resolve(expected_days[-1], "CFFEX", "IF", "IF2610") is None


def test_sync_is_idempotent_and_preserves_existing_day_snapshots(tmp_path: Path) -> None:
    first_days = ["2026-08-03", "2026-08-04"]
    _write_silver_partition(
        tmp_path,
        "first",
        [(day_text, "SHFE", "CU") for day_text in first_days],
    )
    first_calls: list[str] = []
    sync_futures_rule_snapshots(
        tmp_path,
        lookback_days=2,
        fetcher=lambda day: first_calls.append(day) or _provider_table(),
        retrieved_at="2026-08-04T08:00:00+00:00",
    )
    assert first_calls == ["20260803", "20260804"]

    _write_silver_partition(
        tmp_path,
        "second",
        [("2026-08-05", "DCE", "M"), ("2026-08-06", "DCE", "M")],
    )
    second_calls: list[str] = []
    result = sync_futures_rule_snapshots(
        tmp_path,
        lookback_days=2,
        fetcher=lambda day: second_calls.append(day) or _provider_table(),
        retrieved_at="2026-08-06T08:00:00+00:00",
    )
    assert result["status"] == "UPDATED"
    assert second_calls == ["20260805", "20260806"]
    path = tmp_path / SNAPSHOT_RELATIVE_PATH
    before_repeat = path.read_bytes()
    merged = json.loads(before_repeat)
    assert list(merged["days"]) == [
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
    ]
    assert "SHFE.CU" in merged["days"]["2026-08-03"]["products"]
    assert "DCE.M" in merged["days"]["2026-08-06"]["products"]

    unexpected_calls: list[str] = []
    repeated = sync_futures_rule_snapshots(
        tmp_path,
        lookback_days=2,
        fetcher=lambda day: unexpected_calls.append(day) or _provider_table(),
        retrieved_at="2099-01-01T00:00:00+00:00",
    )
    assert repeated["status"] == "UNCHANGED"
    assert unexpected_calls == []
    assert path.read_bytes() == before_repeat


def test_sync_persists_successful_days_when_one_provider_day_fails(tmp_path: Path) -> None:
    trading_days = ["2026-08-03", "2026-08-04", "2026-08-05"]
    _write_silver_partition(
        tmp_path,
        "partial",
        [(day_text, "SHFE", "CU") for day_text in trading_days],
    )

    def partial_fetcher(day_text: str) -> FakeRuleTable:
        if day_text == "20260804":
            raise ValueError("No tables found")
        return _provider_table()

    partial = sync_futures_rule_snapshots(
        tmp_path,
        lookback_days=3,
        fetcher=partial_fetcher,
        retrieved_at="2026-08-05T08:00:00+00:00",
    )
    assert partial["status"] == "PARTIAL"
    assert partial["fetchedDays"] == ["2026-08-03", "2026-08-05"]
    assert partial["failureCount"] == 1
    assert partial["failures"][0]["tradingDay"] == "2026-08-04"
    stored = json.loads((tmp_path / SNAPSHOT_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert list(stored["days"]) == ["2026-08-03", "2026-08-05"]

    recovered_calls: list[str] = []
    recovered = sync_futures_rule_snapshots(
        tmp_path,
        lookback_days=3,
        fetcher=lambda day: recovered_calls.append(day) or _provider_table(),
        retrieved_at="2026-08-06T08:00:00+00:00",
    )
    assert recovered["status"] == "UPDATED"
    assert recovered_calls == ["20260804"]
    complete = json.loads((tmp_path / SNAPSHOT_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert list(complete["days"]) == trading_days


def test_missing_snapshot_loads_as_empty_rule_book_without_network(tmp_path: Path) -> None:
    rule_book = load_rule_book(tmp_path)
    assert rule_book.trading_days == ()
    assert rule_book.resolve("2026-08-01", "SHFE", "CU", "CU2610") is None

    called = False

    def unexpected_fetcher(day: str) -> FakeRuleTable:
        nonlocal called
        called = True
        return _provider_table()

    result = sync_futures_rule_snapshots(tmp_path, fetcher=unexpected_fetcher)
    assert result["status"] == "NO_DATA"
    assert called is False
    assert not (tmp_path / SNAPSHOT_RELATIVE_PATH).exists()


def test_sync_migrates_per_day_provenance_without_refetching(tmp_path: Path) -> None:
    _write_silver_partition(tmp_path, "one", [("2026-08-21", "SHFE", "CU")])
    target = tmp_path / SNAPSHOT_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": SNAPSHOT_SCHEMA,
                "provider": SNAPSHOT_PROVIDER,
                "retrievedAt": "2026-08-21T08:00:00+00:00",
                "days": {
                    "2026-08-21": {
                        "products": {"SHFE.CU": {"contractMultiplier": 5, "marginRate": 0.16}},
                        "contractOverrides": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    result = sync_futures_rule_snapshots(
        tmp_path,
        fetcher=lambda _day: (_ for _ in ()).throw(AssertionError("must not refetch")),
    )
    assert result["status"] == "UPDATED"
    snapshot = json.loads(target.read_text(encoding="utf-8"))["days"]["2026-08-21"]
    assert snapshot["provider"] == SNAPSHOT_PROVIDER
    assert snapshot["retrievedAt"] == "2026-08-21T08:00:00+00:00"
