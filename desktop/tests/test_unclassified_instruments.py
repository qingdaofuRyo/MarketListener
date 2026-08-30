from __future__ import annotations

from pathlib import Path
import struct

from market_monitor.unclassified_instruments import clear_unclassified_cache, scan_unclassified_tdx


def _write_day(path: Path, *, day: int, close: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<IfffffII", day, close - 1, close + 1, close - 2, close, 1_000.0, 10, 0))


def _write_lc5(path: Path, *, packed_day: int, minutes: int, close: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHfffffII", packed_day, minutes, close - 1, close + 1, close - 2, close, 1_000.0, 10, 0))


def test_scanner_groups_periods_reads_latest_close_and_skips_known_names(tmp_path: Path) -> None:
    financial = tmp_path / "financial"
    (financial / "vipdoc" / "sh").mkdir(parents=True)
    _write_day(financial / "vipdoc" / "ds" / "lday" / "62#000300.day", day=20260827, close=4492.25)
    # 2026-08-28 packed as (2026 - 2004) * 2048 + 8 * 100 + 28.
    _write_lc5(financial / "vipdoc" / "ds" / "fzline" / "62#000300.lc5", packed_day=45884, minutes=630, close=4501.5)
    _write_day(financial / "vipdoc" / "ds" / "lday" / "31#00700.day", day=20260827, close=480.0)
    _write_day(financial / "vipdoc" / "ds" / "lday" / "38#1_GDP.day", day=20260827, close=1_401_879.25)
    _write_day(financial / "vipdoc" / "ds" / "lday" / "27#HZ5014.day", day=20260827, close=2_000.0)
    _write_day(financial / "vipdoc" / "ds" / "lday" / "98#02261F.day", day=20260827, close=12.0)
    _write_lc5(financial / "vipdoc" / "ds" / "fzline" / "49#02800.lc5", packed_day=45884, minutes=630, close=20.0)

    futures = tmp_path / "futures"
    _write_day(futures / "vipdoc" / "ds" / "lday" / "68#V050C0.day", day=20260827, close=25.0)
    _write_day(futures / "vipdoc" / "ds" / "lday" / "47#IF2609.day", day=20260827, close=4200.0)
    _write_day(futures / "vipdoc" / "ds" / "lday" / "62#000300.day", day=20260827, close=4500.0)
    clear_unclassified_cache()

    items = scan_unclassified_tdx(financial, futures, refresh=True)

    assert items == []
