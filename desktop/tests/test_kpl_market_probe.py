from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_monitor.kpl_market_probe import probe_kpl_history, probe_kpl_market, read_kpl_status


class Response:
    def __init__(self, body: bytes, code: int = 200):
        self.body, self.status_code, self.closed = body, code, False

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response: Response):
        self.response, self.options = response, None

    def post(self, url: str, **kwargs):
        self.options = (url, kwargs)
        return self.response


def today():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def body(rows):
    return json.dumps({"info": rows, "errcode": "0", "ttag": 0.01}).encode()


def test_live_observation_is_private_and_unverified(tmp_path: Path):
    day = today().isoformat()
    raw = body([[71, 5, 70, 13, 49.2857, 69, day]])
    response = Response(raw)
    session = Session(response)
    result = probe_kpl_market(tmp_path, expected_date=today(), session=session)
    assert result["status"] == "UNVERIFIED" and result["observedDate"] == day
    assert read_kpl_status(tmp_path) == result
    assert response.closed and not session.options[1]["allow_redirects"]
    assert "DeviceID" not in session.options[1]["data"]
    path = tmp_path / "bronze" / "kpl_market_probe" / sha256(raw).hexdigest()
    assert (path / "response.raw").read_bytes() == raw
    assert not (tmp_path / "catalog.duckdb").exists()


def test_history_is_separate_and_stale_remains_unverified(tmp_path: Path):
    raw = body([[72, 6, 70, 13, 49.2857, 69, today().isoformat()],
                [42, 22, 39, 7, 40.5797, 28, (today() - timedelta(days=1)).isoformat()]])
    session = Session(Response(raw))
    result = probe_kpl_history(tmp_path, expected_date=today() - timedelta(days=1), session=session)
    assert result["status"] == "STALE"
    assert read_kpl_status(tmp_path, history=True) == result
    assert read_kpl_status(tmp_path)["status"] == "NEVER"
    assert (tmp_path / "bronze" / "kpl_market_history" / sha256(raw).hexdigest() / "response.raw").exists()
    assert session.options[1]["data"]["st"] == "50"


@pytest.mark.parametrize(("raw", "code", "expected"), [
    (b'{"list":[],"List":[],"errcode":"0"}', 200, "EMPTY"),
    (b'private', 403, "BLOCKED"),
    (b'private', 429, "BLOCKED"),
    (b'{}', 200, "FAILED"),
    (b'X' * 64001, 200, "FAILED"),
    (body([[True, 5, 70, 13, 49.2857, 69, today().isoformat()]]), 200, "FAILED"),
    (body([[1, 5, 70, 13, 101, 69, today().isoformat()]]), 200, "FAILED"),
    (body([[1, 5, 70, 13, 49, 69, (today() + timedelta(days=1)).isoformat()]]), 200, "FAILED"),
    (b'{"info":[],"errcode":"0","token":"private"}', 200, "FAILED"),
], ids=["empty", "forbidden", "throttled", "empty_object", "oversize", "boolean", "percent",
        "future", "private_field"])
def test_empty_invalid_and_blocked_not_saved(tmp_path: Path, raw: bytes, code: int, expected: str):
    response = Response(raw, code)
    result = probe_kpl_market(tmp_path, session=Session(response))
    assert result["status"] == expected and response.closed
    assert result["observedDate"] is None
    assert not (tmp_path / "bronze").exists()


def test_history_rejects_duplicate_dates(tmp_path: Path):
    row = [0, 0, 0, 0, 0, 0, today().isoformat()]
    result = probe_kpl_history(tmp_path, session=Session(Response(body([row, row]))))
    assert result["status"] == "FAILED"
    assert not (tmp_path / "bronze").exists()
