from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from market_monitor.futures_exchange_daily import (
    collect_exchange_daily_turnover,
    normalize_exchange_daily,
)
from market_monitor.futures_official_daily_sources import parse_official_daily
from market_monitor.storage import MarketStore
from market_monitor.web_app import create_web_app


class _Frame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


def test_official_daily_keeps_exchange_failures_and_normalizes_czce_month(tmp_path: Path) -> None:
    def fetch(exchange: str, day: str) -> _Frame:
        assert day == "2026-09-08"
        if exchange == "DCE":
            raise ValueError("official endpoint returned HTTP 412")
        return _Frame([
            {
                "date": "20260908", "symbol": "AP610", "close": 7600,
                "settle": 7590, "volume": 100, "open_interest": 80,
                "turnover": 760.25,
            },
            {"date": "20260908", "symbol": "小计", "close": 0, "turnover": 0},
        ])

    result = collect_exchange_daily_turnover(
        tmp_path, trading_day="2026-09-08", exchanges=("CZCE", "DCE"), fetcher=fetch
    )

    assert result["status"] == "PARTIAL"
    store = MarketStore(tmp_path)
    try:
        rows = store.list_futures_official_daily()
        coverage = store.list_futures_official_daily_coverage()
    finally:
        store.close()
    assert len(rows) == 1
    assert rows[0]["contract_code"] == "AP2610"
    assert rows[0]["turnover_cny"] == 7_602_500
    assert rows[0]["turnover_unit"] == "CNY_10K"
    assert rows[0]["turnover_sidedness"] == "ONE_SIDED"
    assert rows[0]["open_interest"] == 80
    assert {item["exchange"]: item["status"] for item in coverage} == {
        "CZCE": "PASS", "DCE": "FAILED",
    }
    assert list((tmp_path / "bronze" / "official_futures_daily_czce").glob("*.json"))
    client = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 50000))
    official = client.get(
        "/api/futures/official-daily",
        params={"trading_day": "2026-09-08", "exchange": "CZCE", "contract_code": "AP2610"},
    ).json()
    assert official["rows"][0]["openInterest"] == 80
    assert official["rows"][0]["settlement"] == 7590
    assert official["rows"][0]["turnoverCny"] == 7_602_500
    assert official["coverage"][0]["status"] == "PASS"


def test_existing_official_rows_keep_turnover_when_open_interest_column_is_added(tmp_path: Path) -> None:
    store = MarketStore(tmp_path)
    try:
        store.connection.execute("DROP TABLE futures_contract_daily_official")
        store.connection.execute(
            """CREATE TABLE futures_contract_daily_official (
                trade_date VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
                contract_code VARCHAR NOT NULL, product_code VARCHAR NOT NULL,
                close DOUBLE NOT NULL, settlement DOUBLE, volume DOUBLE,
                turnover_raw DOUBLE NOT NULL, turnover_cny DOUBLE NOT NULL,
                turnover_unit VARCHAR NOT NULL, turnover_sidedness VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL, adapter VARCHAR NOT NULL,
                payload_sha256 VARCHAR NOT NULL, retrieved_at VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL,
                PRIMARY KEY(trade_date, exchange, contract_code, adapter)
            )"""
        )
        store.connection.execute(
            "INSERT INTO futures_contract_daily_official VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                "2026-09-08", "SHFE", "CU2610", "CU", 100.0, 99.0, 10.0,
                50.0, 500_000.0, "CNY_10K", "ONE_SIDED",
                "https://example.invalid/official-fixture", "fixture", "fixture",
                "2026-09-08T08:00:00+00:00", "PASS",
            ],
        )
    finally:
        store.close()
    reopened = MarketStore(tmp_path)
    try:
        row = reopened.list_futures_official_daily()[0]
    finally:
        reopened.close()
    assert row["open_interest"] is None
    assert row["turnover_cny"] == 500_000


def test_official_daily_rejects_ambiguous_duplicate_contracts() -> None:
    source = {"date": "20260908", "symbol": "AP610", "close": 7600, "turnover": 1}
    with pytest.raises(ValueError, match="duplicate official contract"):
        normalize_exchange_daily(
            [source, dict(source)], exchange="CZCE", trading_day="2026-09-08",
            retrieved_at="2026-09-08T20:00:00+08:00", payload_sha256="fixture",
        )


def test_official_daily_rejects_unaudited_historical_sidedness() -> None:
    with pytest.raises(ValueError, match="pre-2026"):
        normalize_exchange_daily(
            [], exchange="SHFE", trading_day="2025-09-08",
            retrieved_at="2025-09-08T20:00:00+08:00", payload_sha256="fixture",
        )


def test_dce_daily_turnover_is_not_promoted_before_unit_audit() -> None:
    with pytest.raises(ValueError, match="DCE official daily turnover unit"):
        normalize_exchange_daily(
            [{"date": "20260908", "symbol": "M2610", "close": 3000, "turnover": 500}],
            exchange="DCE", trading_day="2026-09-08",
            retrieved_at="2026-09-08T20:00:00+08:00", payload_sha256="fixture",
        )


def test_dce_official_json_keeps_raw_fields_for_unit_audit(tmp_path: Path, monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"data": [{
                "contractId": "m2610", "close": 3000, "clearPrice": 2990,
                "volumn": 100, "openInterest": 80, "turnover": 500,
            }]}

    def post(url: str, *, json: dict[str, str], timeout: int) -> Response:
        assert url.endswith("/dcereport/publicweb/dailystat/dayQuotes")
        assert json["tradeDate"] == "20260908"
        assert json["varietyId"] == "all"
        assert timeout == 15
        return Response()

    monkeypatch.setattr("requests.post", post)
    result = collect_exchange_daily_turnover(
        tmp_path, trading_day="2026-09-08", exchanges=("DCE",),
    )
    assert result["status"] == "PARTIAL"
    assert "unit and sidedness" in result["exchanges"][0]["error"]
    store = MarketStore(tmp_path)
    try:
        assert store.list_futures_official_daily() == []
        assert store.list_futures_official_daily_coverage()[0]["status"] == "FAILED"
    finally:
        store.close()
    bronze = next((tmp_path / "bronze" / "official_futures_daily_dce").glob("*.json"))
    assert json.loads(bronze.read_text(encoding="utf-8"))["rows"][0]["openInterest"] == 80


def test_direct_shfe_parser_separates_ine_and_ignores_tas_rows() -> None:
    raw = json.dumps({"o_curinstrument": [
        {"PRODUCTGROUPID": "cu", "PRODUCTID": "cu_f", "PRODUCTCLASS": "1",
         "DELIVERYMONTH": "2610", "CLOSEPRICE": 80000, "SETTLEMENTPRICE": 79900,
         "VOLUME": 200, "OPENINTEREST": 300, "TURNOVER": 1598},
        {"PRODUCTGROUPID": "sc", "PRODUCTID": "sc_f", "PRODUCTCLASS": "1",
         "DELIVERYMONTH": "2610", "CLOSEPRICE": 500, "SETTLEMENTPRICE": 501,
         "VOLUME": 100, "OPENINTEREST": 120, "TURNOVER": 50},
        {"PRODUCTGROUPID": "sc_tas", "PRODUCTID": "sc_tas", "PRODUCTCLASS": "6",
         "DELIVERYMONTH": "2610", "CLOSEPRICE": "", "SETTLEMENTPRICE": "",
         "VOLUME": "", "OPENINTEREST": "", "TURNOVER": ""},
    ]}).encode()
    metadata = {"trading_day": "2026-09-24", "retrieved_at": "2026-09-27T00:00:00+00:00"}
    shfe = parse_official_daily(
        raw, exchange="SHFE", source_url="https://www.shfe.com.cn/data/kx.dat", **metadata,
    )
    ine = parse_official_daily(
        raw, exchange="INE", source_url="https://www.ine.cn/data/kx.dat", **metadata,
    )
    assert [row["contract_code"] for row in shfe] == ["CU2610"]
    assert [row["contract_code"] for row in ine] == ["SC2610"]
    assert shfe[0]["volume_sidedness"] == "ONE_SIDED"
    assert shfe[0]["turnover_cny"] == 15_980_000


def test_direct_historical_czce_parser_keeps_quantities_without_unverified_turnover() -> None:
    raw = (
        "郑州商品交易所期货每日行情表(2025-09-24)\n"
        "合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|持仓量|增减量|成交额(万元)|交割结算价\n"
        "AP510|6000|6010|6100|5900|6050|6040|50|40|200|300|10|1208.0|0\n"
        "小计| ||||||||||||\n"
    ).encode("utf-8")
    rows = parse_official_daily(
        raw, exchange="CZCE", trading_day="2025-09-24",
        source_url="https://www.czce.com.cn/cn/DFSStaticFiles/Future/2025/20250924/FutureDataDaily.txt",
        retrieved_at="2026-09-27T00:00:00+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["contract_code"] == "AP2510"
    assert rows[0]["volume"] == 200
    assert rows[0]["open_interest"] == 300
    assert rows[0]["settlement"] == 6040
    assert rows[0]["turnover_raw"] == 1208
    assert rows[0]["turnover_cny"] is None
    assert rows[0]["turnover_quality_status"] == "UNVERIFIED"
