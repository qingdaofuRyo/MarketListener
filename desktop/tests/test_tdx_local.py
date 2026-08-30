from __future__ import annotations

import struct
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.tdx_local import (
    _cn_classification,
    _load_tdx_names,
    decode_minute_day,
    financial_ds_metadata,
    read_tdx_local_file,
    run_tdx_local_import,
)
from market_monitor.web_app import create_web_app


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tdx"
    for directory in (
        root / "vipdoc" / "sh" / "lday",
        root / "vipdoc" / "sh" / "fzline",
        root / "vipdoc" / "ds" / "lday",
        root / "vipdoc" / "ds" / "fzline",
    ):
        directory.mkdir(parents=True)
    (root / "vipdoc" / "sh" / "lday" / "sh600000.day").write_bytes(
        struct.pack("<IiiiifII", 20260814, 914, 917, 906, 910, 397_586_112.0, 43_623_080, 0)
    )
    raw_day = (2026 - 2004) * 2048 + 814
    (root / "vipdoc" / "sh" / "fzline" / "sh600000.lc5").write_bytes(
        struct.pack("<HHfffffII", raw_day, 9 * 60, 9.14, 9.17, 9.06, 9.10, 16_072_869.0, 1_765_100, 0)
    )
    (root / "vipdoc" / "ds" / "lday" / "31#00700.day").write_bytes(
        struct.pack("<IfffffII", 20260814, 436.0, 445.0, 436.0, 440.0, 13_498_062_848.0, 306_011, 0)
    )
    (root / "vipdoc" / "ds" / "fzline" / "31#00700.lc5").write_bytes(
        struct.pack("<HHfffffII", raw_day, 16 * 60, 440.8, 441.0, 440.0, 440.0, 1_095_010_304.0, 24_879, 0)
    )
    return root


def test_readers_keep_standard_a_share_and_hk_daily_layouts_separate(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cn = read_tdx_local_file(root / "vipdoc" / "sh" / "lday" / "sh600000.day")
    hk = read_tdx_local_file(root / "vipdoc" / "ds" / "lday" / "31#00700.day", hong_kong=True)
    minute = read_tdx_local_file(root / "vipdoc" / "sh" / "fzline" / "sh600000.lc5")

    assert cn[0]["close"] == 9.1
    assert cn[0]["raw_close"] == 910
    assert cn[0]["price_scale"] == 100.0
    assert cn[0]["amount"] == 397_586_112.0
    assert cn[0]["volume"] == 43_623_080
    assert hk[0]["close"] == 440.0
    assert hk[0]["amount"] == 13_498_062_848.0
    assert minute[0]["time"] == "09:00:00"
    assert decode_minute_day((2026 - 2004) * 2048 + 814) == "2026-08-14"


def test_load_tdx_names_reads_fixed_width_tnf_records(tmp_path: Path) -> None:
    root = tmp_path / "tdx"
    hq_cache = root / "T0002" / "hq_cache"
    hq_cache.mkdir(parents=True)
    record = bytearray(360)
    record[0:6] = b"899050"
    name = "北证50".encode("gbk")
    record[31 : 31 + len(name)] = name
    (hq_cache / "shs.tnf").write_bytes(b"\x00" * 50 + bytes(record))

    names = _load_tdx_names(root)

    assert names == {"CN": {"899050": "北证50"}, "HK": {}}


def test_cn_classification_covers_bond_repo_fund_and_reit_codes() -> None:
    assert _cn_classification("sh", "110075")[0] == "CONVERTIBLE_BOND"
    assert _cn_classification("sh", "126001")[0] == "CONVERTIBLE_BOND"
    assert _cn_classification("sh", "132001")[0] == "EXCHANGEABLE_BOND"
    assert _cn_classification("sh", "201001")[0] == "REPO"
    assert _cn_classification("sh", "204001")[0] == "PLEDGED_REPO"
    assert _cn_classification("sh", "207001")[0] == "REPO"
    assert _cn_classification("sh", "501001")[0] == "LOF"
    assert _cn_classification("sh", "508000")[0] == "REIT"
    assert _cn_classification("sh", "526001")[0] == "ETF"
    assert _cn_classification("sh", "589001")[0] == "ETF"
    assert _cn_classification("sh", "880001")[:2] == ("INDEX", "TDX_BOARD_INDEX")
    assert _cn_classification("sh", "881048")[:2] == ("INDEX", "TDX_INDUSTRY_INDEX")
    assert _cn_classification("sz", "121001")[0] == "CONVERTIBLE_BOND"
    assert _cn_classification("sz", "123127")[0] == "CONVERTIBLE_BOND"
    assert _cn_classification("sz", "124001")[0] == "CONVERTIBLE_BOND"
    assert _cn_classification("sz", "120001")[0] == "EXCHANGEABLE_BOND"
    assert _cn_classification("sz", "131810")[0] == "PLEDGED_REPO"
    assert _cn_classification("sz", "131910")[0] == "REPO"
    assert _cn_classification("sz", "160123")[0] == "LOF"
    assert _cn_classification("sz", "180101")[0] == "REIT"
    assert _cn_classification("sz", "181001")[0] == "REIT"
    assert _cn_classification("sz", "158001")[0] == "ETF"
    assert _cn_classification("sh", "900901")[:2] == ("B_SHARE", "B_SHARE")
    assert _cn_classification("sz", "200002")[:2] == ("B_SHARE", "B_SHARE")


def test_verified_financial_ds_prefixes_keep_float_prices_and_raw_future_units(tmp_path: Path) -> None:
    root = _root(tmp_path)
    lday = root / "vipdoc" / "ds" / "lday"
    for filename, close, amount, volume in (
        ("12#A_CELS.day", 657.887, 0.0, 0),
        ("16#GC00W.day", 3_350.5, 0.0, 12),
        ("27#HSI.day", 24_000.25, 12_000.0, 120),
        ("48#08003.day", 0.067, 402.0, 6_000),
        ("62#000013.day", 309.3275, 11_195.0, 111),
        ("69#992006.day", 8_550.836, 190_922.0, 10_192),
        ("102#470006.day", 3_966.584, 275_369.0, 3_276),
    ):
        spread = max(abs(close) * 0.01, 0.001)
        (lday / filename).write_bytes(
            struct.pack(
                "<IfffffII",
                20260814,
                close,
                close + spread,
                max(0.0001, close - spread),
                close,
                amount,
                volume,
                0,
            )
        )

    assert financial_ds_metadata("31#00700.day") == {
        "market": "HK", "asset_type": "STOCK", "series_kind": "", "exchange": "HKEX",
        "currency": "HKD", "daily_price_format": "FLOAT32", "symbol": "00700", "period": "1d",
    }
    assert financial_ds_metadata("16#GC00W.day")["series_kind"] == "UNVERIFIED_CONTINUOUS"  # type: ignore[index]
    assert financial_ds_metadata("10#AUDUSD.day") is None
    assert financial_ds_metadata("38#1_GDP.day") is None
    assert financial_ds_metadata("49#00001.day") is None
    assert financial_ds_metadata("98#02261F.day") is None

    summary = run_tdx_local_import(tmp_path / "data", tdx_root=root, batch_rows=1_000)

    assert summary["状态"] == "完成"
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    global_index = client.get(
        "/api/market/instruments/GLOBAL.GLOBAL_INDEX.INDEX.A_CELS/bars", params={"period": "1d"}
    ).json()["bars"][0]
    global_future = client.get(
        "/api/market/instruments/GLOBAL.COMEX.FUTURE.GC00W/bars", params={"period": "1d"}
    ).json()["bars"][0]
    hk_index = client.get(
        "/api/market/instruments/HK.HKEX.INDEX.HSI/bars", params={"period": "1d"}
    ).json()["bars"][0]
    hk_gem = client.get(
        "/api/market/instruments/HK.HKEX.STOCK.08003/bars", params={"period": "1d"}
    ).json()["bars"][0]
    csi = client.get(
        "/api/market/instruments/CN.CSI.INDEX.000013/bars", params={"period": "1d"}
    ).json()["bars"][0]

    assert global_index["close"] == 657.887024
    assert global_index["volumeUnit"] == "TDX_INDEX_RAW"
    assert global_future["close"] == 3350.5
    assert global_future["amount"] is None
    assert global_future["volume"] == 12.0
    assert global_future["volumeUnit"] == "TDX_FOREIGN_FUTURE_RAW"
    assert global_future["seriesKind"] == "UNVERIFIED_CONTINUOUS"
    assert hk_index["close"] == 24000.25
    assert hk_gem["close"] == 0.067
    assert csi["close"] == 309.327515


def test_import_is_source_isolated_and_checkpointed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    data_root = tmp_path / "data"
    first = run_tdx_local_import(data_root, tdx_root=root, batch_rows=1_000)
    second = run_tdx_local_import(data_root, tdx_root=root, batch_rows=1_000)

    assert first["状态"] == "完成"
    assert first["写入K线"] == 4
    assert second["写入K线"] == 0
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    instruments = client.get("/api/market/instruments").json()["items"]
    cn = next(item for item in instruments if item["instrumentId"] == "CN.SSE.STOCK.600000")
    hk = next(item for item in instruments if item["instrumentId"] == "HK.HKEX.STOCK.00700")
    assert cn["actualSource"] == "通达信金融终端（本地）"
    assert hk["actualSource"] == "通达信金融终端（本地）"
    bars = client.get("/api/market/instruments/CN.SSE.STOCK.600000/bars", params={"period": "5m"}).json()["bars"]
    assert bars[0]["amount"] == 16_072_869.0
    assert bars[0]["volume"] == 1_765_100.0


def test_import_reads_only_records_appended_after_checkpoint(tmp_path: Path) -> None:
    root = _root(tmp_path)
    data_root = tmp_path / "data"
    run_tdx_local_import(data_root, tdx_root=root, batch_rows=1_000)
    path = root / "vipdoc" / "sh" / "lday" / "sh600000.day"
    with path.open("ab") as stream:
        stream.write(
            struct.pack("<IiiiifII", 20260815, 910, 920, 905, 918, 183_600.0, 20_000, 0)
        )

    summary = run_tdx_local_import(data_root, tdx_root=root, batch_rows=1_000)

    assert summary["写入K线"] == 1
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    bars = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600000/bars", params={"period": "1d"}
    ).json()["bars"]
    assert [bar["tradingDate"] for bar in bars] == ["2026-08-14", "2026-08-15"]


def test_import_can_limit_local_bars_to_requested_date_range(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "vipdoc" / "sh" / "fzline" / "sh600000.lc5"
    old_day = (2024 - 2004) * 2048 + 918
    current_day = (2026 - 2004) * 2048 + 814
    path.write_bytes(
        struct.pack("<HHfffffII", old_day, 9 * 60, 8.0, 8.2, 7.9, 8.1, 80.5, 10, 0)
        + struct.pack("<HHfffffII", current_day, 9 * 60, 9.14, 9.17, 9.06, 9.10, 182.0, 20, 0)
    )

    summary = run_tdx_local_import(
        tmp_path / "data", tdx_root=root, batch_rows=1_000, start_date="2024-09-18", end_date="2024-10-01",
    )

    assert summary["写入K线"] == 1
    assert summary["开始日期"] == "2024-09-18"


def test_asset_specific_daily_price_scales_and_volume_units(tmp_path: Path) -> None:
    root = _root(tmp_path)
    lday = root / "vipdoc" / "sh" / "lday"
    (lday / "sh510300.day").write_bytes(
        struct.pack("<IiiiifII", 20260814, 4680, 4700, 4660, 4690, 469_000.0, 100_000, 0)
    )
    (lday / "sh110075.day").write_bytes(
        struct.pack("<IiiiifII", 20260814, 1060000, 1070000, 1050000, 1065000, 1_065_000.0, 1_000, 0)
    )
    (lday / "sh204001.day").write_bytes(
        struct.pack("<IiiiifII", 20260814, 18000, 19000, 17000, 18500, 2_000_000.0, 2_000, 0)
    )
    (lday / "sh900901.day").write_bytes(
        struct.pack("<IiiiifII", 20260814, 3210, 3240, 3190, 3220, 322_000.0, 10_000, 0)
    )

    summary = run_tdx_local_import(tmp_path / "data", tdx_root=root, batch_rows=1_000)

    assert summary["状态"] == "完成"
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    etf = client.get("/api/market/instruments/CN.SSE.ETF.510300/bars", params={"period": "1d"}).json()["bars"][0]
    bond = client.get("/api/market/instruments/CN.SSE.CONVERTIBLE_BOND.110075/bars", params={"period": "1d"}).json()["bars"][0]
    b_share = client.get("/api/market/instruments/CN.SSE.B_SHARE.900901/bars", params={"period": "1d"}).json()["bars"][0]
    assert etf["close"] == 4.69
    assert bond["close"] == 106.5
    assert bond["volume"] == 10_000.0
    assert b_share["close"] == 3.22
    assert b_share["currency"] == "USD"


def test_row_level_volume_multipliers_preserve_valid_unit_transitions(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "vipdoc" / "sh" / "lday" / "sh510300.day"
    path.write_bytes(
        struct.pack("<IiiiifII", 20260813, 4600, 4700, 4500, 4650, 465_000.0, 100_000, 0)
        + struct.pack("<IiiiifII", 20260814, 4680, 4700, 4660, 4690, 469_000.0, 1_000, 0)
    )

    summary = run_tdx_local_import(tmp_path / "data", tdx_root=root, batch_rows=1_000)

    assert summary["状态"] == "完成"
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    bars = client.get("/api/market/instruments/CN.SSE.ETF.510300/bars", params={"period": "1d"}).json()["bars"]
    assert [bar["volumeMultiplier"] for bar in bars] == [1.0, 100.0]
    assert [bar["volume"] for bar in bars] == [100_000.0, 100_000.0]


def test_unverifiable_nonzero_row_is_quarantined_without_rejecting_valid_rows(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "vipdoc" / "sh" / "lday" / "sh510300.day"
    path.write_bytes(
        struct.pack("<IiiiifII", 20260813, 4600, 4700, 4500, 4650, 465_000.0, 100_000, 0)
        + struct.pack("<IiiiifII", 20260814, 4680, 4700, 4660, 4690, 1.0, 1_000, 0)
    )

    summary = run_tdx_local_import(tmp_path / "data", tdx_root=root, batch_rows=1_000)

    assert summary["状态"] == "完成（含隔离）"
    assert summary["隔离文件"] == 1
    assert summary["隔离K线"] == 1
    assert list((tmp_path / "data" / "quarantine" / "tdx-cn-v2").glob("*.json"))


def test_replace_source_keeps_a_recoverable_backup_and_promotes_v2(tmp_path: Path) -> None:
    root = _root(tmp_path)
    data_root = tmp_path / "data"
    first = run_tdx_local_import(data_root, tdx_root=root, batch_rows=1_000, rebuild_cache=False)
    assert first["写入K线"] == 4

    result = run_tdx_local_import(
        data_root,
        tdx_root=root,
        batch_rows=1_000,
        rebuild_cache=False,
        full_rescan=True,
        replace_source=True,
    )

    assert result["状态"] == "完成"
    assert result["替换旧分区"] > 0
    assert result["提升新分区"] > 0
    assert list(Path(result["旧数据备份"]).rglob("TDX-LOCAL-*.parquet"))
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))
    bar = client.get(
        "/api/market/instruments/CN.SSE.STOCK.600000/bars",
        params={"period": "1d"},
    ).json()["bars"][0]
    assert bar["normalizationVersion"] == "tdx-cn-v2"
