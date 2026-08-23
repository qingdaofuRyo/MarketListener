from __future__ import annotations

import struct
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.tdx_local import _cn_classification, _load_tdx_names, decode_minute_day, read_tdx_local_file, run_tdx_local_import
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

    assert cn == [{"day": "2026-08-14", "time": "00:00:00", "open": 9.14, "high": 9.17, "low": 9.06, "close": 9.1, "amount": 397_586_112.0, "volume": 43_623_080}]
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
            struct.pack("<IiiiifII", 20260815, 910, 920, 905, 918, 100_000.0, 20_000, 0)
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
        struct.pack("<HHfffffII", old_day, 9 * 60, 8.0, 8.2, 7.9, 8.1, 100.0, 10, 0)
        + struct.pack("<HHfffffII", current_day, 9 * 60, 9.14, 9.17, 9.06, 9.10, 200.0, 20, 0)
    )

    summary = run_tdx_local_import(
        tmp_path / "data", tdx_root=root, batch_rows=1_000, start_date="2024-09-18", end_date="2024-10-01",
    )

    assert summary["写入K线"] == 1
    assert summary["开始日期"] == "2024-09-18"
