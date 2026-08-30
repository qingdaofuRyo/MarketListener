from __future__ import annotations

import json
from pathlib import Path

import duckdb

from market_monitor.market_query_cache import rebuild_kline_query_cache
from market_monitor.retired_market_data import prune_retired_market_data
from market_monitor.storage import MarketStore, PartitionKey


def _bar(instrument_id: str, *, market: str, asset_type: str, day: str = "2026-08-14") -> dict[str, object]:
    symbol = instrument_id.split(".")[-2 if instrument_id.endswith(".TDX_LOCAL") else -1]
    return {
        "instrument_id": instrument_id,
        "symbol": symbol,
        "name": symbol,
        "market": market,
        "asset_type": asset_type,
        "period": "1d",
        "bar_open_time": f"{day}T00:00:00+08:00",
        "bar_close_time": f"{day}T15:00:00+08:00",
        "trading_date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 1.0,
        "amount": 10.5,
        "source": "fixture",
        "quality_status": "PASS",
    }


def test_prune_retired_market_data_keeps_non_hz_rows_and_updates_cache(tmp_path: Path) -> None:
    store = MarketStore(tmp_path)
    run_id = store.begin_run("fixture")
    store.write_silver_bars(
        PartitionKey("HK", "INDEX", "1d", 2026, "TDX-LOCAL-HK-INDEX-1d-2026-fixture"),
        [
            _bar("HK.HKEX.INDEX.HSI.TDX_LOCAL", market="HK", asset_type="INDEX"),
            _bar("HK.HKEX.INDEX.HZ5014.TDX_LOCAL", market="HK", asset_type="INDEX"),
        ],
        "2026-08-14T00:00:00+08:00",
        run_id,
    )
    store.write_silver_bars(
        PartitionKey("GLOBAL", "CRYPTO", "1d", 2026, "GLOBAL-CRYPTO-1d-2026"),
        [
            _bar("GLOBAL.CRYPTO.BTCUSDT", market="GLOBAL", asset_type="CRYPTO"),
            _bar("GLOBAL.CRYPTO.ETHUSDT", market="GLOBAL", asset_type="CRYPTO"),
        ],
        "2026-08-14T00:00:00+08:00",
        run_id,
    )
    store.write_silver_bars(
        PartitionKey("HK", "FUND", "5m", 2026, "TDX-LOCAL-HK-FUND-5m-2026-fixture"),
        [_bar("HK.HKEX.FUND.02800.TDX_LOCAL", market="HK", asset_type="FUND") | {"period": "5m"}],
        "2026-08-14T00:00:00+08:00",
        run_id,
    )
    store.write_silver_bars(
        PartitionKey("CN", "FUTURE", "1d", 2026, "TDX-LOCAL-CN-FUTURE-1d-2026-fixture"),
        [_bar("CN.TDX.FUTURE.CES100.CONTRACT.TDX", market="CN", asset_type="FUTURE")],
        "2026-08-14T00:00:00+08:00",
        run_id,
    )
    store.finish_run(run_id, "COMPLETE")
    store.close()
    rebuild_kline_query_cache(tmp_path)

    summary = prune_retired_market_data(tmp_path)

    assert summary["retiredRows"] == 5
    assert summary["touchedPartitions"] == 4
    catalog = duckdb.connect(str(tmp_path / "catalog.duckdb"), read_only=True)
    rows = catalog.execute("SELECT partition_id, row_count FROM partitions ORDER BY partition_id").fetchall()
    catalog.close()
    assert rows == [("TDX-LOCAL-HK-INDEX-1d-2026-fixture", 1)]
    active = next((tmp_path / "silver").rglob("TDX-LOCAL-HK-INDEX*.parquet"))
    with duckdb.connect(database=":memory:") as connection:
        instruments = connection.execute("SELECT instrument_id FROM read_parquet(?)", [str(active)]).fetchall()
    assert instruments == [("HK.HKEX.INDEX.HSI.TDX_LOCAL",)]
    with duckdb.connect(str(tmp_path / "state" / "kline_query.duckdb"), read_only=True) as cache:
        assert cache.execute("SELECT instrument_id FROM instrument_period").fetchall() == [
            ("HK.HKEX.INDEX.HSI.TDX_LOCAL",)
        ]
    report = Path(summary["backupRoot"]).parent / "report.json"
    assert json.loads(report.read_text(encoding="utf-8"))["tongdaxinSourceFilesModified"] is False
