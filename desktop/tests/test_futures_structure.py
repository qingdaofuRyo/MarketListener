from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.cli import main
from market_monitor.futures_structure import (
    MEMBER_OPEN_INTEREST_CHART_ID,
    MEMBER_STRUCTURE_FORMULA_VERSION,
    MEMBER_STRUCTURE_THRESHOLD,
    PRODUCT_STRUCTURE_RANK_LIMIT,
    PRODUCT_NOTIONAL_CHART_ID,
    PRODUCT_NOTIONAL_FORMULA_VERSION,
    PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION,
    PRODUCT_OPEN_INTEREST_CHART_ID,
    SETTLEMENT_PRICE_BASIS,
    STRUCTURE_FORMULA_VERSION,
    _build_baseline,
    run_member_open_interest_structure_pipeline,
    run_product_notional_structure_pipeline,
    run_product_official_notional_structure_pipeline,
    run_product_open_interest_structure_pipeline,
)
from market_monitor.futures_rule_sync import SNAPSHOT_PROVIDER, SNAPSHOT_RELATIVE_PATH, SNAPSHOT_SCHEMA
from market_monitor.futures_official_multipliers import load_official_multiplier_registry
from market_monitor.storage import MarketStore, PartitionKey
from market_monitor.web_api.futures import _structure_payload
from market_monitor.web_app import create_web_app


def _bar(day: str, *, exchange: str, product: str, symbol: str, open_interest: float) -> dict[str, object]:
    return {
        "instrument_id": f"CN.{exchange}.FUTURE.{symbol}.CONTRACT.TDX",
        "market": "CN",
        "asset_type": "FUTURE",
        "period": "1d",
        "trading_date": day,
        "trading_day": day,
        "bar_open_time": f"{day}T00:00:00+08:00",
        "symbol": symbol,
        "series_kind": "CONTRACT",
        "product_code": product,
        "exchange": exchange,
        "settlement": 100.0,
        "open_interest": open_interest,
        "source": "fixture-tdx",
        "actual_source": "fixture-tdx",
        "fetched_at": "2026-08-29T00:00:00+00:00",
        "quality_status": "PASS",
    }


def _write(data_root: Path, rows: list[dict[str, object]]) -> None:
    store = MarketStore(data_root)
    try:
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "structure-fixture"),
            rows,
            "2026-08-29T00:00:00+00:00",
            "structure-fixture-run",
            update_query_cache=False,
        )
    finally:
        store.close()


def _write_rule_snapshot(data_root: Path, days: dict[str, dict[str, float]]) -> None:
    target = data_root / SNAPSHOT_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": SNAPSHOT_SCHEMA,
                "provider": SNAPSHOT_PROVIDER,
                "retrievedAt": "2026-08-29T00:00:00+00:00",
                "days": {
                    day: {
                        "provider": SNAPSHOT_PROVIDER,
                        "retrievedAt": "2026-08-29T00:00:00+00:00",
                        "products": {
                            key: {"contractMultiplier": multiplier, "marginRate": 0.1}
                            for key, multiplier in products.items()
                        },
                        "contractOverrides": {},
                    }
                    for day, products in days.items()
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _coverage(exchange: str, *, status: str = "PASS") -> dict[str, object]:
    return {
        "trading_day": "2026-08-27",
        "exchange": exchange,
        "status": status,
        "contract_count": 1 if status == "PASS" else 0,
        "record_count": 2 if status == "PASS" else 0,
        "source": "fixture-exchange",
        "error": None if status == "PASS" else "fixture source unavailable",
        "collected_at": "2026-08-29T00:00:00+00:00",
    }


def test_product_open_interest_pipeline_fixes_baseline_and_exposes_new_members(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write(
        data_root,
        [
            _bar("2026-08-20", exchange="DCE", product="JM", symbol="JM2610", open_interest=100),
            _bar("2026-08-20", exchange="CZCE", product="AP", symbol="AP2610", open_interest=1),
            _bar("2026-08-20", exchange="CFFEX", product="IF", symbol="IF2610", open_interest=99_999),
            _bar("2026-08-20", exchange="CFFEX", product="IM", symbol="IM1000", open_interest=1_000_000_000),
            _bar("2026-08-20", exchange="TDX", product="CN", symbol="CN001", open_interest=99_999),
        ],
    )
    _write_rule_snapshot(
        data_root,
        {
            "2026-08-20": {"DCE.JM": 60, "CZCE.AP": 10},
            "2026-08-21": {"DCE.JM": 60, "CZCE.AP": 10, "DCE.I": 100},
        },
    )
    first = run_product_open_interest_structure_pipeline(data_root, calculated_at="2026-08-29T01:00:00+00:00")
    assert first["status"] == "PASS"
    assert first["baselineCreated"] is True
    assert first["baselineDay"] == "2026-08-20"
    assert first["rejectedRows"] == 1  # A stored underlying index is not a dated futures contract.

    _write(
        data_root,
        [
            _bar("2026-08-21", exchange="DCE", product="JM", symbol="JM2610", open_interest=110),
            _bar("2026-08-21", exchange="CZCE", product="AP", symbol="AP2610", open_interest=1),
            _bar("2026-08-21", exchange="DCE", product="I", symbol="I2610", open_interest=4),
            _bar("2026-08-21", exchange="CFFEX", product="IF", symbol="IF2610", open_interest=90_000),
        ],
    )
    second = run_product_open_interest_structure_pipeline(data_root, calculated_at="2026-08-29T02:00:00+00:00")
    assert second["baselineCreated"] is False
    assert second["baselineVersion"] == first["baselineVersion"]

    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    payload = client.get(f"/api/futures/structures/{PRODUCT_OPEN_INTEREST_CHART_ID}", params={"range": "all"}).json()
    assert payload["available"] is True
    assert payload["metric"] == "openInterest"
    assert payload["unit"] == "contracts"
    assert payload["priceBasis"] is None
    assert payload["formulaVersion"] == STRUCTURE_FORMULA_VERSION
    assert payload["threshold"] is None
    assert payload["rankLimit"] == PRODUCT_STRUCTURE_RANK_LIMIT
    assert payload["stackOrder"] == ["CFFEX.IF", "DCE.JM", "CZCE.AP"]
    assert [series["memberKey"] for series in payload["series"]] == ["CFFEX.IF", "DCE.JM", "CZCE.AP"]
    assert [member["memberName"] for member in payload["primaryMembers"]] == ["沪深300", "焦煤", "苹果"]
    assert payload["series"][0]["values"] == [99_999.0, 90_000.0]
    assert payload["series"][1]["values"] == [100.0, 110.0]
    assert payload["series"][2]["values"] == [1.0, 1.0]
    assert payload["unclassifiedMembers"] == [{"memberKey": "DCE.I", "memberName": "铁矿石"}]
    assert payload["unclassifiedTotals"] == [0, 4.0]
    assert payload["totals"] == [100_100.0, 90_115.0]

    notional = run_product_notional_structure_pipeline(data_root, calculated_at="2026-08-29T03:00:00+00:00")
    assert notional["status"] == "PASS"
    assert notional["priceBasis"] == SETTLEMENT_PRICE_BASIS
    notional_payload = client.get(
        f"/api/futures/structures/{PRODUCT_NOTIONAL_CHART_ID}", params={"range": "all"}
    ).json()
    assert notional_payload["metric"] == "notionalOpenInterest"
    assert notional_payload["unit"] == "CNY"
    assert notional_payload["priceBasis"] == SETTLEMENT_PRICE_BASIS
    assert notional_payload["formulaVersion"] == PRODUCT_NOTIONAL_FORMULA_VERSION
    assert notional_payload["threshold"] is None
    assert notional_payload["rankLimit"] == PRODUCT_STRUCTURE_RANK_LIMIT
    assert notional_payload["stackOrder"] == ["CFFEX.IF", "DCE.JM", "DCE.I", "CZCE.AP"]
    assert notional_payload["series"][0]["values"] == [2_999_970_000.0, 2_700_000_000.0]
    assert notional_payload["series"][1]["values"] == [600_000.0, 660_000.0]
    assert notional_payload["series"][2]["values"] == [None, 40_000.0]
    assert notional_payload["series"][3]["values"] == [1_000.0, 1_000.0]
    assert notional_payload["unclassifiedTotals"] == [0, 0]
    assert notional_payload["totals"] == [3_000_571_000.0, 2_700_701_000.0]
    assert "CFFEX_OFFICIAL_CONTRACT_SPEC" in notional_payload["source"]

    drilldown = client.get(
        f"/api/futures/structures/{PRODUCT_OPEN_INTEREST_CHART_ID}", params={"range": "all", "level": "other"}
    ).json()
    assert drilldown["series"] == []
    assert client.get(f"/api/futures/structures/{PRODUCT_OPEN_INTEREST_CHART_ID}", params={"range": "bad"}).status_code == 422
    unavailable = client.get("/api/futures/structures/member-open-interest").json()
    assert unavailable["available"] is False
    assert unavailable["limitations"]


def test_product_top_40_is_fixed_by_rank_and_member_threshold_is_unchanged() -> None:
    values = {f"DCE.P{number:02}": float(41 - number) for number in range(41)}
    names = {key: key for key in values}
    product = _build_baseline(
        values, names, "2026-08-26", "2026-08-27T00:00:00+00:00",
        chart_id=PRODUCT_OPEN_INTEREST_CHART_ID,
    )
    member = _build_baseline(
        values, names, "2026-08-26", "2026-08-27T00:00:00+00:00",
        chart_id=MEMBER_OPEN_INTEREST_CHART_ID,
    )
    assert product["threshold"] == 0.0
    assert len(product["primary_members"]) == 40
    assert [item["memberKey"] for item in product["other_members"]] == ["DCE.P40"]
    assert member["threshold"] == MEMBER_STRUCTURE_THRESHOLD
    assert len(member["other_members"]) > 0


def test_partial_product_day_is_a_gap_instead_of_a_false_market_drop() -> None:
    baseline = _build_baseline(
        {"DCE.JM": 100}, {"DCE.JM": "焦煤"},
        "2026-08-20", "2026-08-20T09:00:00+00:00",
        chart_id=PRODUCT_NOTIONAL_CHART_ID,
        formula_version=PRODUCT_NOTIONAL_FORMULA_VERSION,
    )
    rows = [
        {
            "trade_date": day, "member_key": "DCE.JM", "member_name": "焦煤",
            "value": value, "input_row_count": 100, "missing_row_count": missing,
            "data_quality_status": "PASS" if missing == 0 else "PARTIAL",
            "calculated_at": "2026-08-21T09:00:00+00:00",
        }
        for day, value, missing in (
            ("2026-08-20", 100.0, 0),
            ("2026-08-21", 40.0, 60),
        )
    ]
    payload = _structure_payload(baseline, rows, "main")
    assert payload["totals"] == [100.0, None]
    assert payload["series"][0]["values"] == [100.0, None]
    assert payload["coverage"][1]["dataQualityStatus"] == "PARTIAL"


def test_product_notional_payload_exposes_official_missing_inputs() -> None:
    baseline = _build_baseline(
        {"DCE.JM": 1_000.0}, {"DCE.JM": "焦煤"}, "2026-08-20",
        "2026-08-29T01:00:00+00:00", chart_id=PRODUCT_NOTIONAL_CHART_ID,
        formula_version=PRODUCT_NOTIONAL_FORMULA_VERSION,
        price_basis=SETTLEMENT_PRICE_BASIS,
    )
    payload = _structure_payload(
        baseline,
        [{
            "trade_date": "2026-08-21", "member_key": "DCE.JM", "member_name": "焦煤",
            "value": 900.0, "input_row_count": 10, "missing_row_count": 3,
            "data_quality_status": "PARTIAL", "calculated_at": "2026-08-29T01:00:00+00:00",
        }],
        "main",
        [{
            "trade_date": "2026-08-21", "input_contract_count": 10,
            "missing_settlement_count": 1, "missing_open_interest_count": 0,
            "missing_multiplier_count": 2, "missing_products_json": '["DCE.JM"]',
            "missing_contracts_json": '["DCE.JM2610"]',
        }],
    )
    gap = payload["coverage"][0]
    assert gap["missingSettlementCount"] == 1
    assert gap["missingMultiplierCount"] == 2
    assert gap["missingProducts"] == ["焦煤（JM）"]
    assert gap["missingContracts"] == ["DCE.JM2610"]


def test_product_notional_api_attaches_official_v4_gap_reasons(tmp_path: Path) -> None:
    root = tmp_path / "data"
    timestamp = "2026-08-29T01:00:00+00:00"
    store = MarketStore(root)
    try:
        store.register_default_datasets()
        baseline = _build_baseline(
            {"DCE.JM": 1_000.0}, {"DCE.JM": "焦煤"}, "2026-08-20", timestamp,
            chart_id=PRODUCT_NOTIONAL_CHART_ID,
            formula_version=PRODUCT_NOTIONAL_FORMULA_VERSION,
            price_basis=SETTLEMENT_PRICE_BASIS,
        )
        store.upsert_futures_structure_baseline(baseline)
        store.replace_futures_structure_daily(
            [{
                "chart_id": PRODUCT_NOTIONAL_CHART_ID, "direction": "gross",
                "trade_date": "2026-08-21", "member_key": "DCE.JM", "member_name": "焦煤",
                "value": 900.0, "input_row_count": 10, "missing_row_count": 3,
                "data_quality_status": "PARTIAL", "formula_version": PRODUCT_NOTIONAL_FORMULA_VERSION,
                "price_basis": SETTLEMENT_PRICE_BASIS, "source": "fixture",
                "calculated_at": timestamp,
            }],
            chart_id=PRODUCT_NOTIONAL_CHART_ID, direction="gross",
            formula_version=PRODUCT_NOTIONAL_FORMULA_VERSION,
        )
        store.replace_futures_structure_input_coverage(
            [{
                "chart_id": PRODUCT_NOTIONAL_CHART_ID, "direction": "gross",
                "formula_version": PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION,
                "trade_date": "2026-08-21", "input_contract_count": 10,
                "missing_settlement_count": 1, "missing_open_interest_count": 0,
                "missing_multiplier_count": 2, "missing_products_json": '["DCE.JM"]',
                "missing_contracts_json": '["DCE.JM2610"]', "source": "fixture",
                "calculated_at": timestamp,
            }],
            chart_id=PRODUCT_NOTIONAL_CHART_ID,
            formula_version=PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION,
        )
    finally:
        store.close()
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    response = client.get(f"/api/futures/structures/{PRODUCT_NOTIONAL_CHART_ID}", params={"range": "all"})
    assert response.status_code == 200
    gap = response.json()["coverage"][0]
    assert gap["missingSettlementCount"] == 1
    assert gap["missingMultiplierCount"] == 2
    assert gap["missingProducts"] == ["焦煤（JM）"]
    assert gap["missingContracts"] == ["DCE.JM2610"]


def test_official_multiplier_interval_is_independent_of_daily_margin_snapshot() -> None:
    registry = load_official_multiplier_registry()
    assert registry.resolve("2026-01-28", "SHFE", "CU") is None
    copper = registry.resolve("2026-01-29", "SHFE", "CU")
    assert copper is not None and copper.contract_multiplier == 5
    assert copper.payload_sha256 and len(copper.payload_sha256) == 64
    assert registry.resolve("2026-09-24", "CZCE", "AP").contract_multiplier == 10
    assert registry.resolve("2026-09-24", "DCE", "JM") is None


def test_official_notional_has_separate_version_and_missing_reason_coverage(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, [
        _bar("2026-08-20", exchange="SHFE", product="CU", symbol="CU2610", open_interest=100),
        _bar("2026-08-20", exchange="CZCE", product="AP", symbol="AP2610", open_interest=10),
        _bar("2026-08-21", exchange="DCE", product="JM", symbol="JM2610", open_interest=50),
    ])
    result = run_product_official_notional_structure_pipeline(root)
    assert result["status"] == "PARTIAL"
    assert result["formulaVersion"] == PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION
    assert result["baselineDay"] == "2026-08-20"
    store = MarketStore(root)
    try:
        coverage = store.list_futures_structure_input_coverage(
            chart_id=PRODUCT_NOTIONAL_CHART_ID,
            formula_version=PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION,
        )
        official_rows = store.list_futures_structure_daily(
            chart_id=PRODUCT_NOTIONAL_CHART_ID, direction="gross",
            formula_version=PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION,
        )
    finally:
        store.close()
    assert len(official_rows) == 2
    assert sum(row["value"] for row in official_rows) == 100 * 100 * 5 + 10 * 100 * 10
    assert [(row["trade_date"], row["missing_multiplier_count"]) for row in coverage] == [
        ("2026-08-20", 0), ("2026-08-21", 1),
    ]
    assert json.loads(coverage[-1]["missing_products_json"]) == ["DCE.JM"]


def test_futures_structure_cli_passes_explicit_baseline_rebuild(tmp_path: Path, monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_pipeline(
        data_root: Path,
        *,
        start_day: str | None,
        end_day: str | None,
        rebuild_baseline: bool,
    ) -> dict[str, object]:
        captured.update(
            data_root=data_root,
            start_day=start_day,
            end_day=end_day,
            rebuild_baseline=rebuild_baseline,
        )
        return {"status": "PASS", "writtenRows": 3}

    monkeypatch.setattr("market_monitor.cli.run_product_open_interest_structure_pipeline", fake_pipeline)
    assert main(["futures-structure", "--data-root", str(tmp_path / "data"), "--rebuild-baseline"]) == 0
    assert captured["rebuild_baseline"] is True
    assert '"writtenRows": 3' in capsys.readouterr().out


def test_futures_notional_structure_cli_passes_explicit_baseline_rebuild(tmp_path: Path, monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_pipeline(
        data_root: Path,
        *,
        start_day: str | None,
        end_day: str | None,
        rebuild_baseline: bool,
    ) -> dict[str, object]:
        captured.update(
            data_root=data_root,
            start_day=start_day,
            end_day=end_day,
            rebuild_baseline=rebuild_baseline,
        )
        return {"status": "PASS", "writtenRows": 5}

    monkeypatch.setattr("market_monitor.cli.run_product_notional_structure_pipeline", fake_pipeline)
    assert main(["futures-notional-structure", "--data-root", str(tmp_path / "data"), "--rebuild-baseline"]) == 0
    assert captured["rebuild_baseline"] is True
    assert '"writtenRows": 5' in capsys.readouterr().out


def test_member_structure_requires_both_direction_ranks_for_net_and_uses_fixed_baselines(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = MarketStore(data_root)
    try:
        rows: list[dict[str, object]] = []
        for exchange, contract, member, long_position, short_position in (
            ("SHFE", "RB2610", "甲", 120.0, 90.0),
            ("INE", "SC2610", "乙", 80.0, 100.0),
            ("DCE", "A2610", "丙", 50.0, 40.0),
            ("CZCE", "AP2610", "丁", 30.0, 20.0),
            ("GFEX", "SI2610", "戊", 20.0, 10.0),
        ):
            product = "".join(character for character in contract if character.isalpha())
            for side, position in (("LONG", long_position), ("SHORT", short_position)):
                rows.append(
                    {
                        "trading_day": "2026-08-27",
                        "exchange": exchange,
                        "contract_code": contract,
                        "product_code": product,
                        "side": side,
                        "rank": 1,
                        "member_key": member,
                        "member_name": member,
                        "position": position,
                        "position_change": 0.0,
                        "source": "fixture-exchange",
                        "collected_at": "2026-08-29T00:00:00+00:00",
                    }
                )
        store.upsert_futures_member_position_ranks(rows)
        store.upsert_futures_member_position_coverage(
            [_coverage(exchange) for exchange in ("SHFE", "INE", "DCE", "CZCE", "GFEX")]
        )
    finally:
        store.close()

    summary = run_member_open_interest_structure_pipeline(data_root, calculated_at="2026-08-29T02:00:00+00:00")
    assert summary["directions"]["gross"]["status"] == "PASS"
    assert summary["directions"]["net-long"]["baselineDay"] == "2026-08-27"

    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    gross = client.get(
        f"/api/futures/structures/{MEMBER_OPEN_INTEREST_CHART_ID}", params={"range": "all", "direction": "gross"}
    ).json()
    assert gross["available"] is True
    assert gross["formulaVersion"] == MEMBER_STRUCTURE_FORMULA_VERSION
    assert gross["totals"] == [560.0]
    net_long = client.get(
        f"/api/futures/structures/{MEMBER_OPEN_INTEREST_CHART_ID}", params={"direction": "net-long", "range": "all"}
    ).json()
    assert net_long["available"] is True
    assert net_long["totals"] == [60.0]


def test_member_structure_refuses_to_establish_a_baseline_when_exchange_coverage_failed(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = MarketStore(data_root)
    try:
        store.upsert_futures_member_position_ranks(
            [
                {
                    "trading_day": "2026-08-27", "exchange": "SHFE", "contract_code": "RB2610",
                    "product_code": "RB", "side": side, "rank": 1, "member_key": "甲",
                    "member_name": "甲", "position": 100.0, "position_change": 0.0,
                    "source": "fixture-exchange", "collected_at": "2026-08-29T00:00:00+00:00",
                }
                for side in ("LONG", "SHORT")
            ]
        )
        store.upsert_futures_member_position_coverage(
            [
                _coverage("SHFE"),
                _coverage("INE"),
                _coverage("DCE", status="FAILED"),
                _coverage("CZCE"),
                _coverage("GFEX"),
            ]
        )
    finally:
        store.close()

    summary = run_member_open_interest_structure_pipeline(data_root, calculated_at="2026-08-29T02:00:00+00:00")
    assert summary["directions"]["gross"]["status"] == "NO_COMPLETE_COVERAGE"
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    payload = client.get(
        f"/api/futures/structures/{MEMBER_OPEN_INTEREST_CHART_ID}",
        params={"direction": "gross", "range": "all"},
    ).json()

    assert payload["available"] is False
    assert "固定堆叠基准" in payload["limitations"][0]
