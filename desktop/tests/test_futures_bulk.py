from __future__ import annotations

import struct
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from market_monitor.futures_bulk import _ak_domestic, decode_lc5_day, read_tdx_file, run_bulk_futures
from market_monitor.market_classification import classify_market
from market_monitor.web_app import create_web_app


class _EmptyAk:
    def futures_display_main_sina(self):
        return pd.DataFrame(columns=["symbol", "exchange", "name"])

    def futures_index_ccidx(self, *, symbol: str):
        return pd.DataFrame()


class _DomesticAk:
    def __init__(self) -> None:
        self.daily_request: tuple[str, str, str] | None = None

    def futures_display_main_sina(self):
        return pd.DataFrame([{"symbol": "V0", "exchange": "DCE", "name": "PVC主连"}])

    def futures_zh_minute_sina(self, *, symbol: str, period: str):
        return pd.DataFrame()

    def futures_main_sina(self, *, symbol: str, start_date: str, end_date: str):
        self.daily_request = (symbol, start_date, end_date)
        return pd.DataFrame()


def _future_root(tmp_path: Path) -> Path:
    root = tmp_path / "tdx"
    day = root / "vipdoc" / "ds" / "lday"
    five = root / "vipdoc" / "ds" / "fzline"
    day.mkdir(parents=True)
    five.mkdir(parents=True)
    (day / "29#JML7.day").write_bytes(struct.pack("<IffffIIf", 20260814, 1, 2, 0.5, 1.5, 10, 20, 1.4))
    (day / "29#JML8.day").write_bytes(struct.pack("<IffffIIf", 20260814, 2, 3, 1, 2.5, 11, 21, 2.4))
    (day / "29#JML9.day").write_bytes(struct.pack("<IffffIIf", 20260814, 3, 4, 2, 3.5, 12, 22, 3.4))
    raw_day = (2026 - 2004) * 2048 + 814
    (five / "29#JML8.lc5").write_bytes(struct.pack("<HHffffIIf", raw_day, 9 * 60, 2, 3, 1, 2.5, 11, 21, 0))
    (root / "T0002" / "hq_cache").mkdir(parents=True)
    (root / "T0002" / "hq_cache" / "code2qhidx.ini").write_bytes("29_JML9=焦煤指数|100\n".encode("gbk"))
    return root


def test_tdx_reader_and_bulk_job_keep_l7_l8_l9_separate(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    rows = read_tdx_file(root / "vipdoc" / "ds" / "fzline" / "29#JML8.lc5")
    assert decode_lc5_day((2026 - 2004) * 2048 + 814) == "2026-08-14"
    assert rows[0]["time"] == "09:00:00"

    data_root = tmp_path / "data"
    summary = run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    assert summary["状态"] == "完成"
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    listed = client.get("/api/market/instruments", params={"seriesKind": "MAIN"}).json()
    main = next(item for item in listed["items"] if item["productCode"] == "JM")
    assert main["instrumentId"] == "CN.DCE.FUTURE.JM.MAIN"
    assert main["name"] == "焦煤主连"
    assert main["actualSource"] == "通达信期货通"
    assert main["latestTimeText"] == "2026-08-14 09:00:00"
    assert main["capitalDeposit"] == 660
    assert main["contractMultiplier"] == 60
    assert main["marginRate"] == pytest.approx(0.2)
    assert main["capitalDepositFormulaVersion"] == "2026-01-v1"
    assert "大商所" in main["contractSpecSource"]
    bars = client.get(f"/api/market/instruments/{main['instrumentId']}/bars", params={"period": "5m"})
    assert bars.status_code == 200
    assert bars.json()["bars"][0]["instrumentId"] == main["instrumentId"]
    assert bars.json()["bars"][0]["actualSource"] == "通达信期货通"
    assert bars.json()["bars"][0]["capitalDeposit"] == 660
    secondary = client.get("/api/market/instruments", params={"seriesKind": "SECONDARY"}).json()["items"]
    weighted = client.get("/api/market/instruments", params={"seriesKind": "WEIGHTED"}).json()["items"]
    assert next(item for item in secondary if item["productCode"] == "JM")["name"] == "焦煤次连"
    assert next(item for item in weighted if item["productCode"] == "JM")["name"] == "焦煤加权"


def test_bulk_checkpoint_skips_unchanged_tdx_files(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    data_root = tmp_path / "data"
    first = run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    second = run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    assert first["写入K线"] > 0
    assert second["写入K线"] == 0


def test_bulk_checkpoint_reads_only_appended_futures_records(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    data_root = tmp_path / "data"
    run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    path = root / "vipdoc" / "ds" / "lday" / "29#JML8.day"
    with path.open("ab") as stream:
        stream.write(struct.pack("<IffffIIf", 20260815, 2.5, 3.5, 2, 3, 12, 22, 2.9))

    summary = run_bulk_futures(
        data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk()
    )

    assert summary["写入K线"] == 1
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    bars = client.get(
        "/api/market/instruments/CN.DCE.FUTURE.JM.MAIN/bars", params={"period": "1d"}
    ).json()["bars"]
    assert [bar["tradingDate"] for bar in bars] == ["2026-08-14", "2026-08-15"]


def test_bulk_tdx_skips_minutes_before_complete_market_coverage(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    path = root / "vipdoc" / "ds" / "fzline" / "29#JML8.lc5"
    old_raw_day = (2017 - 2004) * 2048 + 102
    current_raw_day = (2026 - 2004) * 2048 + 814
    path.write_bytes(
        struct.pack("<HHffffIIf", old_raw_day, 9 * 60, 1, 2, 0.5, 1.5, 10, 20, 0)
        + struct.pack("<HHffffIIf", current_raw_day, 9 * 60, 2, 3, 1, 2.5, 11, 21, 0)
    )

    summary = run_bulk_futures(tmp_path / "data", tdx_futures_root=root, include_global=False, api=_EmptyAk())

    # Three daily bars plus only the post-2018 minute bar are persisted.
    assert summary["写入K线"] == 4


def test_bulk_imports_specific_month_contracts_and_searches_polysilicon(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    day = root / "vipdoc" / "ds" / "lday"
    five = root / "vipdoc" / "ds" / "fzline"
    (day / "66#PS2609.day").write_bytes(
        struct.pack("<IffffIIf", 20260814, 8700, 8800, 8650, 8750, 100, 200, 8740)
    )
    raw_day = (2026 - 2004) * 2048 + 814
    (five / "66#PS2609.lc5").write_bytes(
        struct.pack("<HHffffIIf", raw_day, 9 * 60, 8700, 8750, 8680, 8720, 100, 200, 0)
    )
    metadata = "29_JML9=焦煤指数|100\n66_PS2609=多晶硅2609|8750\n"
    (root / "T0002" / "hq_cache" / "code2qhidx.ini").write_bytes(metadata.encode("gbk"))
    data_root = tmp_path / "data"

    run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    response = client.get(
        "/api/market/instruments",
        params={"q": "多晶硅", "seriesKind": "CONTRACT", "pageSize": 30},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    contract = response.json()["items"][0]
    assert contract["instrumentId"] == "CN.GFEX.FUTURE.PS2609.CONTRACT"
    assert contract["symbol"] == "PS2609"
    assert contract["name"] == "多晶硅2609"
    assert contract["productCode"] == "PS"
    assert contract["actualSource"] == "通达信期货通"
    assert client.get(
        f"/api/market/instruments/{contract['instrumentId']}/bars", params={"period": "1d"}
    ).status_code == 200
    assert client.get(
        f"/api/market/instruments/{contract['instrumentId']}/bars", params={"period": "5m"}
    ).status_code == 200


def test_tdx_commodity_index_uses_index_metadata_name(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    (root / "vipdoc" / "ds" / "lday" / "42#T003.day").write_bytes(
        struct.pack("<IffffIIf", 20260814, 180, 182, 178, 181, 100, 200, 0)
    )
    (root / "T0002" / "hq_cache" / "code2qhidx.ini").write_bytes(
        "29_JML9=焦煤指数|100\nIndex3=T003|工业品|181.36|2|42_T006,42_T007\n".encode("gbk")
    )
    data_root = tmp_path / "data"

    run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    response = client.get("/api/market/instruments", params={"q": "工业品"})

    assert response.status_code == 200
    items = response.json()["items"]
    assert any(
        item["instrumentId"] == "CN.TDX.INDEX.T003.COMMODITY_INDEX" and item["name"] == "工业品"
        for item in items
    )


def test_tdx_classifies_dce_f_cffex_ranked_and_option_volatility_series(tmp_path: Path) -> None:
    root = _future_root(tmp_path)
    day = root / "vipdoc" / "ds" / "lday"
    payload = struct.pack("<IffffIIf", 20260814, 20, 21, 19, 20.5, 100, 200, 20.4)
    for filename in ("29#L-F2609.day", "47#ICL0.day", "47#IF300.day", "68#V050C0.day"):
        (day / filename).write_bytes(payload)

    data_root = tmp_path / "data"
    run_bulk_futures(data_root, tdx_futures_root=root, include_global=False, api=_EmptyAk())
    items = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000)).get(
        "/api/market/instruments", params={"pageSize": 100}
    ).json()["items"]
    by_id = {item["instrumentId"]: item for item in items}

    assert by_id["CN.DCE.FUTURE.L-F2609.CONTRACT"]["seriesKind"] == "CONTRACT"
    assert by_id["CN.CFFEX.FUTURE.IC.RANKED_0"]["seriesKind"] == "RANKED_0"
    assert classify_market(by_id["CN.CFFEX.INDEX.IF300.FUTURES_UNDERLYING_INDEX"]) == "cn-future-index"
    volatility = by_id["CN.TDX_OPTION_VOLATILITY.INDEX.V050C0.OPTION_VOLATILITY_INDEX"]
    assert classify_market(volatility) == "cn-future-index"
    assert volatility["actualSource"] == "通达信期货通"


def test_akshare_domestic_daily_backfill_starts_in_2000() -> None:
    api = _DomesticAk()

    _ak_domestic(api, [])

    assert api.daily_request is not None
    assert api.daily_request[0:2] == ("V0", "20000101")
