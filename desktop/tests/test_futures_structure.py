from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.cli import main
from market_monitor.futures_structure import (
    MEMBER_OPEN_INTEREST_CHART_ID,
    MEMBER_STRUCTURE_FORMULA_VERSION,
    PRODUCT_OPEN_INTEREST_CHART_ID,
    STRUCTURE_FORMULA_VERSION,
    run_member_open_interest_structure_pipeline,
    run_product_open_interest_structure_pipeline,
)
from market_monitor.storage import MarketStore, PartitionKey
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
            _bar("2026-08-20", exchange="TDX", product="CN", symbol="CN001", open_interest=99_999),
        ],
    )
    first = run_product_open_interest_structure_pipeline(data_root, calculated_at="2026-08-29T01:00:00+00:00")
    assert first["status"] == "PASS"
    assert first["baselineCreated"] is True
    assert first["baselineDay"] == "2026-08-20"

    _write(
        data_root,
        [
            _bar("2026-08-21", exchange="DCE", product="JM", symbol="JM2610", open_interest=110),
            _bar("2026-08-21", exchange="CZCE", product="AP", symbol="AP2610", open_interest=1),
            _bar("2026-08-21", exchange="DCE", product="I", symbol="I2610", open_interest=4),
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
    assert payload["stackOrder"] == ["DCE.JM", "OTHER"]
    assert [series["memberKey"] for series in payload["series"]] == ["DCE.JM", "OTHER"]
    assert payload["series"][0]["values"] == [100.0, 110.0]
    assert payload["series"][1]["values"] == [1.0, 1.0]
    assert payload["unclassifiedMembers"] == [{"memberKey": "DCE.I", "memberName": "I"}]
    assert payload["unclassifiedTotals"] == [0, 4.0]
    assert payload["totals"] == [101.0, 115.0]

    drilldown = client.get(
        f"/api/futures/structures/{PRODUCT_OPEN_INTEREST_CHART_ID}", params={"range": "all", "level": "other"}
    ).json()
    assert [series["memberKey"] for series in drilldown["series"]] == ["CZCE.AP"]
    assert client.get(f"/api/futures/structures/{PRODUCT_OPEN_INTEREST_CHART_ID}", params={"range": "bad"}).status_code == 422
    unavailable = client.get("/api/futures/structures/member-open-interest").json()
    assert unavailable["available"] is False
    assert unavailable["limitations"]


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
