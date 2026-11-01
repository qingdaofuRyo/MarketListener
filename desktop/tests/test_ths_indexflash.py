from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_monitor.ths_indexflash import probe_indexflash, read_indexflash_status
from market_monitor.web_api.data_sections import router


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code, self.body = status, body
        self.closed = False

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.options = None

    def get(self, url: str, **kwargs):
        self.options = (url, kwargs)
        return self.response


def test_json_is_private_evidence_never_published_as_daily_metrics(tmp_path: Path) -> None:
    body = json.dumps({"zdfb_data": {"znum": 606, "dnum": 4922},
                       "last_zdt": {"ztzs": 42, "dtzs": 23},
                       "jrbx_data": {"last_zdf": 1.26}}, ensure_ascii=False).encode("utf-8")
    response = FakeResponse(200, body)
    session = FakeSession(response)
    status = probe_indexflash(tmp_path, session=session)
    assert status["status"] == "UNVERIFIED" and response.closed
    assert session.options[1]["allow_redirects"] is False
    assert "Cookie" not in session.options[1]["headers"]
    digest = hashlib.sha256(body).hexdigest()
    folder = tmp_path / "bronze" / "ths_indexflash" / digest
    assert (folder / "response.raw").read_bytes() == body
    observations = list((folder / "observations").glob("*.json"))
    assert len(observations) == 1
    assert json.loads(observations[0].read_text())["sha256"] == digest
    assert not (tmp_path / "catalog.duckdb").exists()
    app = FastAPI()
    app.state.data_root = tmp_path
    app.include_router(router)
    payload = TestClient(app).get("/api/data/ths-indexflash/status").json()
    assert payload == status
    assert set(payload) == {"status", "checkedAt"}
    assert "4922" not in json.dumps(payload)
    probe_indexflash(tmp_path, session=FakeSession(FakeResponse(200, body)))
    assert len(list((folder / "observations").glob("*.json"))) == 2


def test_blocked_and_invalid_response_never_save_private_body(tmp_path: Path) -> None:
    for status, body, expected in [(403, b"private blocked page", "BLOCKED"),
                                   (429, b"throttled", "BLOCKED"),
                                   (200, b"<html>challenge</html>", "FAILED"),
                                   (200, b"{}", "FAILED"),
                                   (200, b"{" + b"x" * 1_000_001, "FAILED")]:
        response = FakeResponse(status, body)
        result = probe_indexflash(tmp_path, session=FakeSession(response))
        assert result["status"] == expected and response.closed
        assert read_indexflash_status(tmp_path) == result
        assert not (tmp_path / "bronze" / "ths_indexflash").exists()
    assert not (tmp_path / "catalog.duckdb").exists()


def test_invalid_status_does_not_leak_arbitrary_data(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ths_indexflash_status.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"schema":"ths-indexflash-probe-v1","status":"OTHER","checkedAt":"secret"}')
    assert read_indexflash_status(tmp_path) == {"status": "NEVER", "checkedAt": None}
