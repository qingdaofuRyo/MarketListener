"""The CZCE workbook source keeps a verifiable daily raw checkpoint."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

from openpyxl import Workbook

from market_monitor.czce_workbook_rank_backfill import capture_czce_workbook_ranks
from market_monitor.futures_calendar import sync_futures_trading_calendar


def _calendar(root: Path, *days: str) -> None:
    sync_futures_trading_calendar(root, fetcher=lambda: [(day,) for day in days],
                                  retrieved_at="2026-10-02T16:00:00+08:00")


def _workbook(day: str) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append([f"郑州商品交易所期货持仓排名表({day})"])
    sheet.append([f"合约：AP610    日期：{day}"])
    sheet.append(["名次", "会员简称", "交易量（手）", "增减量", "会员简称", "持买仓量", "增减量",
                  "会员简称", "持卖仓量", "增减量"])
    sheet.append([1, "甲期货", "1,000", 2, "乙期货", 300, 1, "丙期货", 200, -1])
    sheet.append(["合计", None, "1,000", 2, None, 300, 1, None, 200, -1])
    sink = BytesIO()
    book.save(sink)
    return sink.getvalue()


class _Response:
    def __init__(self, raw: bytes, code: int = 200) -> None:
        self.content, self.status_code = raw, code
        self.headers = {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class _Client:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.days: list[str] = []

    def get(self, day: str) -> _Response:
        self.days.append(day)
        return self.responses.pop(0)


def test_resumable_workbook_keeps_one_writer_and_checks_raw_digest(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-29", "2026-09-30")
    first = _Client(_Response(_workbook("2026-09-29")))
    report = capture_czce_workbook_ranks(tmp_path, first, start_day="2026-09-29", end_day="2026-09-30", max_days=1)
    assert report["status"] == "BATCH_LIMIT"
    assert first.days == ["2026-09-29"]
    bronze = tmp_path / "bronze" / "czce_workbook_rank_capture"
    record = json.loads((bronze / "2026-09-29.json").read_text(encoding="utf-8"))
    assert record["status"] == "PASS_RAW"
    assert record["contractCount"] == record["rankingRows"] == 1
    assert record["positionRows"] == 2 and record["tradeRows"] == 1
    assert record["authoritativeDataWritten"] is False

    second = _Client(_Response(_workbook("2026-09-30")))
    resumed = capture_czce_workbook_ranks(tmp_path, second, start_day="2026-09-29", end_day="2026-09-30")
    assert resumed["status"] == "PASS_RAW" and resumed["completedDays"] == 2
    assert second.days == ["2026-09-30"]

    (bronze / "2026-09-29.xlsx").write_bytes(b"corrupt")
    repaired = _Client(_Response(_workbook("2026-09-29")))
    again = capture_czce_workbook_ranks(tmp_path, repaired, start_day="2026-09-29", end_day="2026-09-30")
    assert again["status"] == "PASS_RAW" and repaired.days == ["2026-09-29"]


def test_http_challenge_is_recorded_without_promoting_body(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-29")
    blocked = _Client(_Response(b"<html>verification</html>", 412))
    result = capture_czce_workbook_ranks(tmp_path, blocked, start_day="2026-09-29", end_day="2026-09-29")
    assert result["status"] == "FAILED" and result["httpStatus"] == 412
    bronze = tmp_path / "bronze" / "czce_workbook_rank_capture"
    assert not (bronze / "2026-09-29.xlsx").exists()
    state = json.loads((tmp_path / "state" / "czce_workbook_rank_capture" / "2026-09-29.json").read_text())
    assert state["authoritativeDataWritten"] is False
    assert state["payloadSha256"] and state["status"] == "FAILED"


def test_wrong_date_workbook_remains_raw_evidence_but_not_passed(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-29")
    wrong = _Client(_Response(_workbook("2026-09-28")))
    result = capture_czce_workbook_ranks(tmp_path, wrong, start_day="2026-09-29", end_day="2026-09-29")
    assert result["status"] == "FAILED"
    bronze = tmp_path / "bronze" / "czce_workbook_rank_capture"
    assert (bronze / "2026-09-29.xlsx").exists()
    manifest = json.loads((bronze / "2026-09-29.json").read_text())
    assert manifest["status"] == "FAILED" and manifest["qualityStatus"] == "FAILED"
    assert manifest["errorType"] == "ValueError"


def test_two_missing_formats_are_explicit_and_do_not_stop_other_dates(tmp_path: Path) -> None:
    _calendar(tmp_path, "2025-01-02", "2025-01-03")
    missing = _Response(b"not found", 404)
    missing.source_url = "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2025/20250102/FutureDataHolding.xlsx"
    missing.attempted_urls = [
        "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2025/20250102/FutureDataHolding.xls",
        missing.source_url,
    ]
    first = _Client(missing, _Response(_workbook("2025-01-03")))
    result = capture_czce_workbook_ranks(tmp_path, first, start_day="2025-01-02", end_day="2025-01-03")
    assert result["status"] == "PARTIAL_RAW"
    assert result["missingDays"] == result["completedDays"] == 1
    state = json.loads((tmp_path / "state" / "czce_workbook_rank_capture" / "2025-01-02.json").read_text())
    assert state["status"] == "MISSING_REPORT" and state["qualityStatus"] == "UNKNOWN"
    assert not (tmp_path / "bronze" / "czce_workbook_rank_capture" / "2025-01-02.xlsx").exists()
    no_requests = _Client()
    resumed = capture_czce_workbook_ranks(tmp_path, no_requests, start_day="2025-01-02", end_day="2025-01-03")
    assert resumed["status"] == "PARTIAL_RAW" and no_requests.days == []
