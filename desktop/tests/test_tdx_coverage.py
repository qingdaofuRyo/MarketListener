"""Raw timestamp coverage remains independent of TDX price normalization."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import struct

from market_monitor.futures_calendar import sync_futures_trading_calendar
from market_monitor.tdx_coverage import _aggregate, financial_extended_groups, load_latest_tdx_coverage, run_tdx_coverage_check


def _daily(path: Path, *days: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(struct.pack("<I28x", int(day.replace("-", ""))) for day in days))


def _minute(path: Path, *stamps: tuple[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bytearray()
    for day, hour_minute in stamps:
        parsed = date.fromisoformat(day)
        packed = (parsed.year - 2004) * 2048 + parsed.month * 100 + parsed.day
        hour, minute = (int(value) for value in hour_minute.split(":"))
        payload += struct.pack("<HH28x", packed, hour * 60 + minute)
    path.write_bytes(payload)


def _calendar(data_root: Path, days: list[str]) -> None:
    sync_futures_trading_calendar(
        data_root,
        fetcher=lambda: [(date.fromisoformat(day),) for day in days],
        retrieved_at="2026-09-25T00:00:00+00:00",
    )


def _run(data_root: Path, financial: Path, futures: Path) -> dict:
    (financial / "vipdoc").mkdir(parents=True, exist_ok=True)
    (futures / "vipdoc").mkdir(parents=True, exist_ok=True)
    return run_tdx_coverage_check(data_root, financial_root=financial, futures_root=futures)


def test_financial_daily_reports_range_but_does_not_invent_stock_calendar(tmp_path: Path) -> None:
    financial, futures = tmp_path / "financial", tmp_path / "futures"
    _daily(financial / "vipdoc/sh/lday/sh600519.day", "2026-09-21", "2026-09-23")
    report = _run(tmp_path / "data", financial, futures)
    row = report["files"][0]
    assert row["earliestAt"] == "2026-09-21"
    assert row["latestAt"] == "2026-09-23"
    assert row["status"] == "UNVERIFIED"
    assert row["calendarStatus"] == "UNVERIFIED"
    assert report["summary"]["unknownCalendarFiles"] == 1
    assert load_latest_tdx_coverage(tmp_path / "data")["summary"] == report["summary"]


def test_stock_five_minutes_skips_lunch_and_flags_internal_gap(tmp_path: Path) -> None:
    financial, futures = tmp_path / "financial", tmp_path / "futures"
    path = financial / "vipdoc/sh/fzline/sh600519.lc5"
    _minute(path,
            ("2026-09-21", "09:35"), ("2026-09-21", "09:40"),
            ("2026-09-21", "11:30"), ("2026-09-21", "13:05"),
            ("2026-09-21", "13:10"))
    report = _run(tmp_path / "data", financial, futures)
    row = report["files"][0]
    assert row["status"] == "GAPPED"
    assert row["gapCount"] == 21  # 09:45..11:25, never lunch 11:35..13:00
    assert all(sample["time"] < "11:30" for sample in row["sampleGaps"])


def test_extended_hk_five_minutes_checks_within_sessions(tmp_path: Path) -> None:
    financial, futures = tmp_path / "financial", tmp_path / "futures"
    _minute(financial / "vipdoc/ds/fzline/31#00700.lc5",
            ("2026-09-21", "11:55"), ("2026-09-21", "12:00"),
            ("2026-09-21", "13:05"), ("2026-09-21", "13:10"))
    row = _run(tmp_path / "data", financial, futures)["files"][0]
    assert row["status"] == "UNVERIFIED"  # HK cross-day calendar is unavailable.
    assert row["sessionStatus"] == "VERIFIED"
    assert row["gapCount"] == 0  # Lunch break is not a missing interval.


def test_futures_daily_uses_persisted_calendar_and_reuses_unchanged_file(tmp_path: Path) -> None:
    data_root, financial, futures = tmp_path / "data", tmp_path / "financial", tmp_path / "futures"
    _calendar(data_root, ["2026-09-21", "2026-09-22", "2026-09-23"])
    _daily(futures / "vipdoc/ds/lday/28#AP2610.day", "2026-09-21", "2026-09-23")
    first = _run(data_root, financial, futures)
    assert first["files"][0]["status"] == "GAPPED"
    assert first["files"][0]["sampleGaps"] == [{"date": "2026-09-22", "kind": "交易日无记录"}]
    second = _run(data_root, financial, futures)
    assert second["summary"]["scannedFiles"] == 0
    assert second["summary"]["reusedFiles"] == 1


def test_futures_minute_uses_bar_end_and_sorting_not_file_tail(tmp_path: Path) -> None:
    data_root, financial, futures = tmp_path / "data", tmp_path / "financial", tmp_path / "futures"
    _calendar(data_root, ["2026-09-21"])
    path = futures / "vipdoc/ds/fzline/28#AP2610.lc5"
    _minute(path,
            ("2026-09-21", "09:05"), ("2026-09-21", "09:10"),
            ("2026-09-21", "10:15"), ("2026-09-21", "10:35"),
            ("2026-09-21", "23:00"), ("2026-09-21", "14:55"))
    row = _run(data_root, financial, futures)["files"][0]
    assert row["latestAt"] == "2026-09-21T23:00:00+08:00"
    assert row["status"] == "GAPPED"
    assert all(sample.get("time") not in {"10:20", "10:25", "10:30"} for sample in row["sampleGaps"])


def test_truncated_and_unknown_futures_cache_are_not_called_continuous(tmp_path: Path) -> None:
    financial, futures = tmp_path / "financial", tmp_path / "futures"
    broken = financial / "vipdoc/sh/lday/sh600519.day"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"bad")
    _daily(futures / "vipdoc/ds/lday/42#UNKNOWN.day", "2026-09-21")
    _daily(futures / "vipdoc/sh/lday/sh600519.day", "2026-09-21")
    report = _run(tmp_path / "data", financial, futures)
    assert {row["status"] for row in report["files"]} == {"INVALID_RECORD_LENGTH"}
    assert report["summary"]["files"] == 1  # unrecognized futures-cache copies are excluded
    assert report["summary"]["continuousFiles"] == 0


def test_financial_extended_summary_distinguishes_one_latest_file_from_common_date() -> None:
    rows = [
        {"terminal": "financial", "file": f"vipdoc/ds/fzline/12#TEST{index}.lc5", "period": "5m",
         "latestAt": "2026-09-11T16:00:00+08:00"}
        for index in range(3)
    ] + [
        {"terminal": "financial", "file": "vipdoc/ds/fzline/12#OUTLIER.lc5", "period": "5m",
         "latestAt": "2026-09-14T23:55:00+08:00"},
        {"terminal": "futures", "file": "vipdoc/ds/fzline/28#AP2610.lc5", "period": "5m",
         "latestAt": "2026-09-25T15:00:00+08:00"},
    ]
    assert financial_extended_groups(rows) == [{
        "prefix": "12", "period": "5m", "files": 4, "withRecords": 0,
        "commonStartDate": None, "commonStartFiles": 0,
        "commonLatestDate": "2026-09-11", "commonLatestFiles": 3,
    }]


def test_file_being_written_is_not_reported_as_corrupt() -> None:
    summary = _aggregate([{"status": "WRITING", "records": 0, "gapCount": 0}])
    assert summary["writingFiles"] == 1
    assert summary["unreadableFiles"] == 0
