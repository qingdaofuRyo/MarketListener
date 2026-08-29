from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient
import pytest

from market_monitor.cli import main
from market_monitor.futures_calendar import (
    CALENDAR_MARKET,
    CALENDAR_PROVIDER,
    CALENDAR_RELATIVE_PATH,
    CALENDAR_SCHEMA,
)
from market_monitor.futures_heat_pipeline import run_futures_heat_pipeline
from market_monitor.futures_rule_sync import SNAPSHOT_PROVIDER, SNAPSHOT_RELATIVE_PATH, SNAPSHOT_SCHEMA
from market_monitor.storage import (
    _FUTURES_LONG_SHORT_HEAT_COLUMNS,
    MarketStore,
    PartitionKey,
    _futures_heat_table_ddl,
)
from market_monitor.web_app import create_web_app


def _gold_row(
    day: str,
    *,
    breadth: float = 20.0,
    fund: float | None = 30.0,
    formula_version: str = "2026-08-v1",
) -> dict[str, object]:
    return {
        "trade_date": day,
        "total_variety_count": 2,
        "valid_variety_count": 2,
        "missing_variety_count": 0,
        "up_variety_count": 1,
        "down_variety_count": 1,
        "flat_variety_count": 0,
        "fund_valid_variety_count": 2,
        "fund_missing_variety_count": 0,
        "up_fund": 2_400_000.0,
        "down_fund": 400_000.0,
        "flat_fund": 0.0,
        "return_coverage": 1.0,
        "fund_coverage": 1.0,
        "breadth_score_daily": breadth,
        "fund_score_daily": fund,
        "breadth_score_10d": breadth,
        "fund_score_10d": fund,
        "divergence": breadth - fund if fund is not None else None,
        "is_warmup": True,
        "data_quality_status": "PARTIAL",
        "formula_version": formula_version,
        "source_cutoff": "2026-08-27T00:00:00+00:00",
        "calculation_method": "fixture-method",
        "calculated_at": "2026-08-27T01:00:00+00:00",
    }


def _bar(
    day: str,
    *,
    exchange: str,
    product: str,
    symbol: str,
    kind: str,
    settlement: float,
    open_interest: float,
) -> dict[str, object]:
    instrument_id = f"CN.{exchange}.FUTURE.{symbol}.{kind}.TDX"
    return {
        "instrument_id": instrument_id,
        "market": "CN",
        "asset_type": "FUTURE",
        "period": "1d",
        "trading_date": day,
        "trading_day": day,
        "bar_open_time": f"{day}T00:00:00+08:00",
        "symbol": symbol,
        "series_kind": kind,
        "product_code": product,
        "exchange": exchange,
        "settlement": settlement,
        "open_interest": open_interest,
        "source": "TDX",
        "actual_source": "TDX",
        "fetched_at": "2026-08-27T00:00:00+00:00",
        "quality_status": "PASS",
    }


def _write_heat_silver(data_root: Path, days: list[str]) -> None:
    rows: list[dict[str, object]] = []
    for index, day in enumerate(days):
        rows.extend(
            [
                _bar(
                    day,
                    exchange="DCE",
                    product="JM",
                    symbol="JML9",
                    kind="WEIGHTED",
                    settlement=100 + index,
                    open_interest=1_000,
                ),
                _bar(
                    day,
                    exchange="DCE",
                    product="JM",
                    symbol="JM2612",
                    kind="CONTRACT",
                    settlement=100 + index,
                    open_interest=1_000,
                ),
                _bar(
                    day,
                    exchange="CZCE",
                    product="AP",
                    symbol="APL9",
                    kind="WEIGHTED",
                    settlement=200 - index,
                    open_interest=1_000,
                ),
                _bar(
                    day,
                    exchange="CZCE",
                    product="AP",
                    symbol="AP2612",
                    kind="CONTRACT",
                    settlement=200 - index,
                    open_interest=1_000,
                ),
                _bar(
                    day,
                    exchange="DCE",
                    product="JM",
                    symbol="JM2505",
                    kind="CONTRACT",
                    settlement=5_000,
                    open_interest=10_000_000,
                ),
                _bar(
                    day,
                    exchange="CFFEX",
                    product="IF",
                    symbol="IFL9",
                    kind="WEIGHTED",
                    settlement=4_000 + index,
                    open_interest=1_000,
                ),
            ]
        )
    store = MarketStore(data_root)
    try:
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "heat-fixture"),
            rows,
            "2026-08-27T00:00:00+00:00",
            "heat-fixture-run",
            update_query_cache=False,
        )
    finally:
        store.close()


def _weekdays(start: date, count: int) -> list[str]:
    days: list[str] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def test_heat_gold_storage_is_idempotent_filterable_and_has_no_user_total(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "data")
    try:
        assert store.upsert_futures_long_short_heat([_gold_row("2026-08-25")]) == 1
        assert store.upsert_futures_long_short_heat([_gold_row("2026-08-25", breadth=40.0)]) == 1
        assert store.upsert_futures_long_short_heat([_gold_row("2026-08-26")]) == 1
        assert store.upsert_futures_long_short_heat(
            [_gold_row("2026-08-26", formula_version="2026-09-v2")]
        ) == 1
        rows = store.list_futures_long_short_heat(start_day="2026-08-26")
        current = store.list_futures_long_short_heat(
            start_day="2026-08-26", formula_version="2026-08-v1"
        )
    finally:
        store.close()
    assert [(row["trade_date"], row["formula_version"]) for row in rows] == [
        ("2026-08-26", "2026-08-v1"),
        ("2026-08-26", "2026-09-v2"),
    ]
    assert len(current) == 1
    assert "total_score_10d" not in current[0]


def test_heat_gold_bounded_replace_prunes_stale_days_without_touching_other_versions(
    tmp_path: Path,
) -> None:
    store = MarketStore(tmp_path / "data")
    try:
        store.upsert_futures_long_short_heat(
            [_gold_row("2026-08-25"), _gold_row("2026-08-26"), _gold_row("2026-08-27")]
        )
        store.upsert_futures_long_short_heat(
            [_gold_row("2026-08-26", formula_version="other-v1")]
        )
        store.replace_futures_long_short_heat(
            [_gold_row("2026-08-27", breadth=40)],
            formula_version="2026-08-v1",
            start_day="2026-08-26",
        )
        current = store.list_futures_long_short_heat(formula_version="2026-08-v1")
        preserved = store.list_futures_long_short_heat(formula_version="other-v1")
    finally:
        store.close()
    assert [row["trade_date"] for row in current] == ["2026-08-25", "2026-08-27"]
    assert [row["trade_date"] for row in preserved] == ["2026-08-26"]


def test_heat_gold_rejects_non_finite_json_values_and_broken_count_invariants(
    tmp_path: Path,
) -> None:
    store = MarketStore(tmp_path / "data")
    try:
        non_finite = _gold_row("2026-08-26")
        non_finite["fund_score_10d"] = float("nan")
        with pytest.raises(ValueError, match="finite"):
            store.upsert_futures_long_short_heat([non_finite])
        broken = _gold_row("2026-08-26")
        broken["up_variety_count"] = 2
        with pytest.raises(ValueError, match="up plus down plus flat"):
            store.upsert_futures_long_short_heat([broken])
    finally:
        store.close()


def test_heat_gold_schema_migrates_legacy_single_date_primary_key_without_data_loss(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    catalog = data_root / "catalog.duckdb"
    legacy = duckdb.connect(str(catalog))
    row = _gold_row("2026-08-26")
    try:
        legacy.execute(
            _futures_heat_table_ddl("futures_long_short_heat_daily").replace(
                "PRIMARY KEY(formula_version, trade_date)", "PRIMARY KEY(trade_date)"
            )
        )
        legacy.execute(
            f"INSERT INTO futures_long_short_heat_daily "
            f"({', '.join(_FUTURES_LONG_SHORT_HEAT_COLUMNS)}) VALUES "
            f"({', '.join('?' for _ in _FUTURES_LONG_SHORT_HEAT_COLUMNS)})",
            [row[column] for column in _FUTURES_LONG_SHORT_HEAT_COLUMNS],
        )
    finally:
        legacy.close()

    store = MarketStore(data_root)
    try:
        assert store.upsert_futures_long_short_heat(
            [_gold_row("2026-08-26", formula_version="2026-09-v2")]
        ) == 1
        migrated = store.list_futures_long_short_heat(start_day="2026-08-26")
        primary = store.connection.execute(
            """SELECT constraint_column_names FROM duckdb_constraints()
            WHERE table_name='futures_long_short_heat_daily' AND constraint_type='PRIMARY KEY'"""
        ).fetchone()[0]
    finally:
        store.close()
    assert tuple(primary) == ("formula_version", "trade_date")
    assert len(migrated) == 2


def test_pipeline_reads_silver_filters_expired_and_cffex_and_writes_requested_range(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    days = _weekdays(date(2026, 8, 3), 12)
    _write_heat_silver(data_root, days)

    summary = run_futures_heat_pipeline(
        data_root,
        start_day=days[1],
        end_day=days[10],
        calculated_at="2026-08-27T01:00:00+00:00",
    )
    store = MarketStore(data_root)
    try:
        rows = store.list_futures_long_short_heat()
    finally:
        store.close()

    assert summary["writtenRows"] == 10
    assert summary["returnCoverage"] == 1.0
    assert summary["fundCoverage"] == 1.0
    assert summary["dataQualityStatus"] in {"PASS", "PARTIAL"}
    assert [rows[0]["trade_date"], rows[-1]["trade_date"]] == [days[1], days[10]]
    assert rows[-1]["total_variety_count"] == 2
    assert rows[-1]["valid_variety_count"] == 2
    assert rows[-1]["fund_coverage"] == 1.0
    assert rows[-1]["up_fund"] < 10_000_000
    assert rows[-1]["breadth_score_10d"] == 0.0
    assert rows[-1]["fund_score_10d"] is not None
    assert "用户" not in rows[-1]["calculation_method"]


def test_pipeline_prefers_exact_day_rule_snapshot_and_contract_override(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    days = _weekdays(date(2026, 8, 3), 11)
    _write_heat_silver(data_root, days)
    snapshots = {
        day: {
            "provider": SNAPSHOT_PROVIDER,
            "retrievedAt": "2026-08-27T00:00:00+00:00",
            "products": {
                "DCE.JM": {"contractMultiplier": 60, "marginRate": 0.2},
                "CZCE.AP": {"contractMultiplier": 10, "marginRate": 0.1},
            },
            "contractOverrides": {"DCE.JM2612": {"marginRate": 0.5}},
        }
        for day in days
    }
    target = data_root / SNAPSHOT_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": SNAPSHOT_SCHEMA,
                "provider": SNAPSHOT_PROVIDER,
                "retrievedAt": "2026-08-27T00:00:00+00:00",
                "days": snapshots,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = run_futures_heat_pipeline(data_root, end_day=days[-1])
    store = MarketStore(data_root)
    try:
        latest = store.list_futures_long_short_heat()[-1]
    finally:
        store.close()

    assert summary["ruleSnapshotDays"] == len(days)
    # JM: 110 settlement * 60 multiplier * 1,000 OI * 50% override * two sides.
    assert latest["up_fund"] == 6_600_000


def test_pipeline_uses_persisted_calendar_and_excludes_non_trading_silver_days(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    days = ["2026-08-03", "2026-08-04", "2026-08-05"]
    _write_heat_silver(data_root, days)
    target = data_root / CALENDAR_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": CALENDAR_SCHEMA,
                "provider": CALENDAR_PROVIDER,
                "market": CALENDAR_MARKET,
                "retrievedAt": "2026-08-27T00:00:00+00:00",
                "tradingDays": [days[0], days[2]],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = run_futures_heat_pipeline(data_root)
    store = MarketStore(data_root)
    try:
        rows = store.list_futures_long_short_heat()
    finally:
        store.close()

    assert summary["calendarSource"] == CALENDAR_PROVIDER
    assert summary["calendarExcludedDays"] == [days[1]]
    assert summary["calendarExcludedRows"] == 6
    assert [row["trade_date"] for row in rows] == [days[0], days[2]]


def test_pipeline_keeps_non_pass_silver_in_coverage_but_never_uses_its_values(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    bars = [
        _bar(
            day,
            exchange="DCE",
            product="JM",
            symbol="JML9",
            kind="WEIGHTED",
            settlement=settlement,
            open_interest=1_000,
        )
        for day, settlement in (("2026-08-03", 100.0), ("2026-08-04", 110.0))
    ]
    for bar in bars:
        bar["quality_status"] = "FAILED"
    store = MarketStore(data_root)
    try:
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "failed-quality-fixture"),
            bars,
            "2026-08-04T16:00:00+08:00",
            "failed-quality-run",
            update_query_cache=False,
        )
    finally:
        store.close()

    run_futures_heat_pipeline(data_root)
    store = MarketStore(data_root)
    try:
        latest = store.list_futures_long_short_heat()[-1]
    finally:
        store.close()
    assert latest["total_variety_count"] == 1
    assert latest["valid_variety_count"] == 0
    assert latest["return_coverage"] == 0.0
    assert latest["breadth_score_daily"] is None
    assert latest["data_quality_status"] == "UNAVAILABLE"


def test_heat_api_reads_gold_only_uses_camel_case_and_validates_dates(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    store = MarketStore(data_root)
    try:
        store.upsert_futures_long_short_heat(
            [
                _gold_row("2026-08-26", breadth=99.0, formula_version="2026-07-legacy"),
                _gold_row("2026-08-26"),
            ]
        )
    finally:
        store.close()

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("API must not scan Silver")

    monkeypatch.setattr("market_monitor.futures_heat_pipeline._read_relevant_silver", fail_scan)
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    response = client.get(
        "/api/futures/heat",
        params={"start_day": "2026-08-26", "end_day": "2026-08-26"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["latest"] == payload["points"][-1]
    assert payload["points"][0]["tradeDate"] == "2026-08-26"
    assert payload["points"][0]["breadthScore10"] == 20.0
    assert "totalScore10" not in payload["points"][0]
    assert payload["config"]["defaultUserWeight"] == {"breadthWeight": 0.4, "fundWeight": 0.6}
    assert payload["config"]["userWeight"] == {"min": 0.0, "max": 1.0, "step": 0.05}
    assert "2/2" not in "".join(payload["limitations"])
    assert client.get("/api/futures/heat", params={"start_day": "bad"}).status_code == 422
    assert client.get(
        "/api/futures/heat",
        params={"start_day": "2026-08-27", "end_day": "2026-08-26"},
    ).status_code == 422


def test_heat_api_empty_gold_is_explicit_and_futures_shell_route_exists(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    payload = client.get("/api/futures/heat").json()
    assert payload["available"] is False
    assert payload["points"] == []
    assert payload["latest"] is None
    assert payload["limitations"]
    assert client.get("/futures/").status_code in {200, 503}


def test_heat_api_discloses_partial_fund_history_coverage(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    missing_fund = _gold_row("2026-08-25", fund=None)
    missing_fund["fund_score_daily"] = None
    missing_fund["fund_score_10d"] = None
    missing_fund["divergence"] = None
    store = MarketStore(data_root)
    try:
        store.upsert_futures_long_short_heat([missing_fund, _gold_row("2026-08-26")])
    finally:
        store.close()

    payload = TestClient(
        create_web_app(data_root), client=("127.0.0.1", 50000)
    ).get("/api/futures/heat").json()
    history_limit = next(item for item in payload["limitations"] if "历史覆盖" in item)
    assert "1/2" in history_limit
    assert "2026-08-26" in history_limit
    assert "null" in history_limit


def test_futures_heat_cli_dispatches_pipeline_and_reports_success(tmp_path: Path, monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_pipeline(data_root: Path, *, start_day: str | None, end_day: str | None):
        captured.update(data_root=data_root, start_day=start_day, end_day=end_day)
        return {"status": "PASS", "writtenRows": 3}

    monkeypatch.setattr("market_monitor.cli.run_futures_heat_pipeline", fake_pipeline)
    assert main(
        [
            "futures-heat",
            "--data-root",
            str(tmp_path / "data"),
            "--start-day",
            "2026-08-01",
            "--end-day",
            "2026-08-26",
        ]
    ) == 0
    assert captured["start_day"] == "2026-08-01"
    assert captured["end_day"] == "2026-08-26"
    assert '"writtenRows": 3' in capsys.readouterr().out


def test_futures_rule_sync_cli_dispatches_controlled_recent_window(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    captured: dict[str, object] = {}

    def fake_sync(data_root: Path, *, lookback_days: int):
        captured.update(data_root=data_root, lookback_days=lookback_days)
        return {
            "status": "UPDATED",
            "fetchedDayCount": 12,
            "productRuleCount": 888,
            "contractOverrideCount": 100,
        }

    monkeypatch.setattr("market_monitor.cli.sync_futures_rule_snapshots", fake_sync)
    assert main(
        [
            "futures-rule-sync",
            "--data-root",
            str(tmp_path / "data"),
            "--lookback-days",
            "12",
        ]
    ) == 0
    assert captured["lookback_days"] == 12
    assert '"fetchedDayCount": 12' in capsys.readouterr().out


def test_futures_calendar_sync_cli_dispatches_persisted_calendar(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    captured: dict[str, object] = {}

    def fake_sync(data_root: Path):
        captured["data_root"] = data_root
        return {
            "status": "UPDATED",
            "tradingDayCount": 8_797,
            "startDay": "1990-12-19",
            "endDay": "2026-12-31",
        }

    monkeypatch.setattr("market_monitor.cli.sync_futures_trading_calendar", fake_sync)
    assert main(
        ["futures-calendar-sync", "--data-root", str(tmp_path / "data")]
    ) == 0
    assert captured["data_root"] == tmp_path / "data"
    assert '"tradingDayCount": 8797' in capsys.readouterr().out
