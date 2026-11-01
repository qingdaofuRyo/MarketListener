"""SHFE/INE raw capture guards for report identity and safe resume."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import threading

import pytest

from market_monitor.futures_calendar import sync_futures_trading_calendar
from market_monitor.shfe_rank_backfill import (
    AdaptiveGate,
    capture_shfe_ranks,
    validate_shfe_rank,
)


def _raw(day: str) -> bytes:
    payload = {
        "o_code": 0,
        "report_date": day.replace("-", ""),
        "o_cursor": [
            {"INSTRUMENTID": "cu2302                   ", "RANK": 1,
             "PARTICIPANTABBR1": "成交席位", "CJ1": 10,
             "PARTICIPANTABBR2": "多头席位", "CJ2": 20,
             "PARTICIPANTABBR3": "空头席位", "CJ3": 30},
            {"INSTRUMENTID": "sc2302                   ", "RANK": 1,
             "PARTICIPANTABBR1": "成交席位", "CJ1": 11,
             "PARTICIPANTABBR2": "多头席位", "CJ2": 21,
             "PARTICIPANTABBR3": "空头席位", "CJ3": 31},
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_shfe_report_validates_date_code_and_separates_ine() -> None:
    summary = validate_shfe_rank(_raw("2023-01-03"), "2023-01-03")

    assert summary == {
        "contractCount": 2, "rankingRows": 2,
        "venues": {"SHFE": {"contractCount": 1, "rankingRows": 1},
                   "INE": {"contractCount": 1, "rankingRows": 1}},
    }
    with pytest.raises(ValueError, match="report_date"):
        validate_shfe_rank(_raw("2023-01-03"), "2023-01-04")
    failed = json.loads(_raw("2023-01-03"))
    failed["o_code"] = 501
    with pytest.raises(ValueError, match="o_code"):
        validate_shfe_rank(json.dumps(failed).encode(), "2023-01-03")


class _FakeClient:
    def __init__(self) -> None:
        self.gate = AdaptiveGate(2)
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def fetch(self, day: str) -> dict:
        with self.lock:
            self.calls.append(day)
        raw = _raw(day)
        return {
            "tradingDay": day, "sourceUrl":
                f"https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{day.replace('-', '')}.dat",
            "retrievedAt": "2026-10-03T00:00:00+00:00", "httpStatus": 200,
            "contentType": "application/json", "payloadSha256": sha256(raw).hexdigest(),
            "bytes": len(raw), "attempts": 1, "status": "PASS_RAW",
            "summary": validate_shfe_rank(raw, day), "raw": raw,
        }

    def close(self) -> None:
        pass


def test_capture_resumes_and_rechecks_raw_hash(tmp_path: Path) -> None:
    days = ["2023-01-03", "2023-01-04", "2023-01-05"]
    sync_futures_trading_calendar(tmp_path, fetcher=lambda: [(day,) for day in days])
    first = _FakeClient()
    progress = capture_shfe_ranks(
        tmp_path, start_day=days[0], end_day=days[-1], max_days=2, workers=2, client=first,
    )
    assert progress["status"] == "BATCH_LIMIT"
    assert progress["completedDays"] == 2
    assert set(first.calls) == set(days[:2])

    second = _FakeClient()
    progress = capture_shfe_ranks(
        tmp_path, start_day=days[0], end_day=days[-1], workers=2, client=second,
    )
    assert progress["status"] == "PASS_RAW"
    assert second.calls == [days[-1]]
    assert progress["authoritativeDataWritten"] is False

    corrupt = tmp_path / "bronze" / "shfe_rank_capture" / f"{days[0]}.dat"
    corrupt.write_bytes(b"corrupted")
    third = _FakeClient()
    progress = capture_shfe_ranks(
        tmp_path, start_day=days[0], end_day=days[-1], workers=2, client=third,
    )
    assert progress["status"] == "PASS_RAW"
    assert third.calls == [days[0]]
    assert corrupt.read_bytes() == _raw(days[0])


def test_adaptive_gate_reduces_then_recovers_concurrency() -> None:
    gate = AdaptiveGate(4)
    assert gate.capacity == 4
    gate.transient()
    assert gate.capacity == 2
    gate.transient()
    assert gate.capacity == 1
    for _ in range(8):
        gate.success()
    assert gate.capacity == 2
