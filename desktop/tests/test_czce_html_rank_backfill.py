"""Offline request and resume contracts for the CZCE HTML raw collector."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import requests

from market_monitor.czce_html_rank_backfill import CzceHtmlClient, capture_czce_html_ranks
from market_monitor.futures_calendar import sync_futures_trading_calendar


def _report(day: str) -> bytes:
    html = (
        "<html><head><meta charset='utf-8'><title>期货持仓排名表</title></head><body><table>"
        f"<tr><td colspan='10'>合约：AP610&nbsp;日期：{day}</td></tr>"
        "<tr><td>名次</td><td>会员简称</td><td>交易量（手）</td><td>增减量</td>"
        "<td>会员简称</td><td>持买仓量</td><td>增减量</td>"
        "<td>会员简称</td><td>持卖仓量</td><td>增减量</td></tr>"
        "<tr><td>1</td><td>甲期货</td><td>1,000</td><td>2</td>"
        "<td>乙期货</td><td>300</td><td>1</td>"
        "<td>丙期货</td><td>200</td><td>-1</td></tr>"
        "<tr><td>合计</td><td></td><td>1,000</td><td>2</td>"
        "<td></td><td>300</td><td>1</td><td></td><td>200</td><td>-1</td></tr>"
        "</table></body></html>"
    )
    return html.encode("utf-8")


class _Response:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.trust_env = True

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        assert kwargs["timeout"] == 30
        assert kwargs["allow_redirects"] is False
        self.calls.append((method, url))
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def _calendar(root: Path, *days: str) -> None:
    sync_futures_trading_calendar(
        root, fetcher=lambda: [(day,) for day in days],
        retrieved_at="2026-09-30T16:00:00+08:00",
    )


def test_czce_raw_capture_resumes_completed_days_without_re_request(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-29", "2026-09-30")
    raw_29 = _report("2026-09-29")
    first_session = _Session(_Response(raw_29))
    first_client = CzceHtmlClient(tmp_path, session=first_session, cookie="synthetic-cookie")  # type: ignore[arg-type]
    try:
        first = capture_czce_html_ranks(
            tmp_path, first_client, start_day="2026-09-29", end_day="2026-09-30", max_days=1,
        )
    finally:
        first_client.close()
    assert first["status"] == "BATCH_LIMIT"
    assert first["completedDays"] == 1
    assert len(first_session.calls) == 1
    assert first_session.calls[0] == (
        "GET", "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260929/FutureDataHolding.htm",
    )

    bronze = tmp_path / "bronze" / "czce_html_rank_capture"
    state = tmp_path / "state" / "czce_html_rank_capture"
    assert (bronze / "2026-09-29.htm").read_bytes() == raw_29
    manifest = json.loads((bronze / "2026-09-29.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "PASS_RAW"
    assert manifest["payloadSha256"] == sha256(raw_29).hexdigest()
    assert manifest["contractCount"] == 1
    assert manifest["rankingRows"] == 1
    assert manifest["authoritativeDataWritten"] is False
    assert manifest["sourceUrl"] == first_session.calls[0][1]
    assert "synthetic-cookie" not in (bronze / "2026-09-29.json").read_text(encoding="utf-8")
    assert "synthetic-cookie" not in (state / "2026-09-29.json").read_text(encoding="utf-8")

    second_session = _Session(_Response(_report("2026-09-30")))
    second_client = CzceHtmlClient(tmp_path, session=second_session)  # type: ignore[arg-type]
    try:
        second = capture_czce_html_ranks(
            tmp_path, second_client, start_day="2026-09-29", end_day="2026-09-30",
        )
    finally:
        second_client.close()
    assert second["status"] == "PASS_RAW"
    assert second["completedDays"] == 2
    assert second["newDaysThisRun"] == 1
    assert len(second_session.calls) == 1
    assert second_session.calls[0][1].endswith("/20260930/FutureDataHolding.htm")

    (state / "2026-09-29.json").unlink()
    no_request_session = _Session()
    no_request_client = CzceHtmlClient(tmp_path, session=no_request_session)  # type: ignore[arg-type]
    try:
        third = capture_czce_html_ranks(
            tmp_path, no_request_client, start_day="2026-09-29", end_day="2026-09-30",
        )
    finally:
        no_request_client.close()
    assert third["status"] == "PASS_RAW"
    assert third["newDaysThisRun"] == 0
    assert no_request_session.calls == []
    assert (state / "2026-09-29.json").exists()


def test_czce_raw_capture_rejects_http_412_without_archiving_body(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-29")
    challenge = b"<html><title>Verification</title><body>temporary challenge</body></html>"
    session = _Session(_Response(challenge, 412))
    client = CzceHtmlClient(tmp_path, session=session)  # type: ignore[arg-type]
    try:
        result = capture_czce_html_ranks(
            tmp_path, client, start_day="2026-09-29", end_day="2026-09-29",
        )
    finally:
        client.close()
    assert result["status"] == "FAILED"
    state = tmp_path / "state" / "czce_html_rank_capture" / "2026-09-29.json"
    evidence = json.loads(state.read_text(encoding="utf-8"))
    assert evidence["httpStatus"] == 412
    assert evidence["payloadSha256"] == sha256(challenge).hexdigest()
    assert evidence["authoritativeDataWritten"] is False
    assert b"temporary challenge" not in state.read_bytes()
    assert not (tmp_path / "bronze" / "czce_html_rank_capture" / "2026-09-29.htm").exists()


def test_czce_raw_capture_rejects_wrong_report_date_and_html_challenge(tmp_path: Path) -> None:
    _calendar(tmp_path, "2026-09-29", "2026-09-30")
    wrong_day = _report("2026-09-28")
    session = _Session(_Response(wrong_day), _Response(_report("2026-09-30")))
    client = CzceHtmlClient(tmp_path, session=session)  # type: ignore[arg-type]
    try:
        result = capture_czce_html_ranks(
            tmp_path, client, start_day="2026-09-29", end_day="2026-09-30",
        )
    finally:
        client.close()
    assert result["status"] == "FAILED"
    assert result["errorDay"] == "2026-09-29"
    assert len(session.calls) == 1
    assert not (tmp_path / "bronze" / "czce_html_rank_capture" / "2026-09-29.htm").exists()

    challenge_root = tmp_path / "challenge"
    _calendar(challenge_root, "2026-09-29")
    challenge_session = _Session(_Response(b"<html><title>Verification</title></html>"))
    challenge_client = CzceHtmlClient(challenge_root, session=challenge_session)  # type: ignore[arg-type]
    try:
        challenge_result = capture_czce_html_ranks(
            challenge_root, challenge_client, start_day="2026-09-29", end_day="2026-09-29",
        )
    finally:
        challenge_client.close()
    assert challenge_result["status"] == "FAILED"
    assert not (challenge_root / "bronze" / "czce_html_rank_capture" / "2026-09-29.htm").exists()
