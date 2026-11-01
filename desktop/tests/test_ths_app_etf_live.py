from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from market_monitor.ths_app_etf_live import URL, capture, credential_path, load_session, save_session, validate_session
from market_monitor.ths_app_etf_view import read_ths_app_etf


def config() -> dict:
    return {"url": URL, "headers": {"Cookie": "synthetic-cookie", "X-Fuyao-Auth": "synthetic-auth",
                                    "X-Auth-ProgId": "synthetic-app", "Content-Type": "application/json"},
            "payload": {"code_selectors": {"include": [{"type": "synthetic", "values": [
                f"20:{i:06d}" for i in range(81)]}, {"type": "synthetic", "values": []}],
                "exclude": [], "intersection": []},
                "indexes": [{"index_id": item} for item in
                            ("security_name", "last_price", "price_change_ratio_pct", "turnover")],
                "page_info": {"page_size": 40, "page_begin": 0, "code_begin": 0},
                "sort": [{"idx": 2, "type": "asc"}]}}


class Response:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self.body = json.dumps(payload).encode()

    def iter_content(self, chunk_size: int):
        yield self.body

    def close(self):
        pass


class Client:
    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.offsets: list[int] = []

    def post(self, url, *, headers, json, timeout, allow_redirects, stream):
        assert url == URL and allow_redirects is False and stream is True
        assert headers["cookie"] == "synthetic-cookie"
        offset = json["page_info"]["page_begin"]
        self.offsets.append(offset)
        if self.mode == "auth":
            return Response(401, {})
        if self.mode == "redirect":
            return Response(302, {})
        if self.mode == "limited":
            return Response(429, {})
        codes = json["code_selectors"]["include"][0]["values"]
        selected = codes[offset:offset + 40]
        if self.mode == "duplicate" and offset == 40:
            selected = [codes[0], *selected[1:]]
        if self.mode == "short" and offset == 40:
            selected = selected[:-1]
        indexes = [{"index_id": x["index_id"], "timestamp": "0"} for x in json["indexes"]]
        rows = [{"code": code, "values": [{"idx": 0, "value": "合成ETF"},
                {"idx": 1, "value": 1.0}, {"idx": 2, "value": 0},
                {"idx": 3, "value": 0}]} for code in selected]
        return Response(200, {"status_code": 0, "data": {"total": 81, "indexes": indexes, "data": rows}})


def test_complete_pages_publish_only_sanitized_observation(tmp_path: Path):
    client = Client()
    result = capture(tmp_path, config(), client=client, pause=lambda _: None)
    assert result["status"] == "UNVERIFIED" and result["count"] == 81
    assert client.offsets == [0, 40, 80]
    observed = read_ths_app_etf(tmp_path)
    assert observed["count"] == 81 and observed["quoteTime"] is None
    assert observed["rows"][0]["turnover"] == 0
    assert "synthetic-cookie" not in json.dumps(observed)
    for path in tmp_path.rglob("*.json"):
        assert "synthetic-cookie" not in path.read_text(encoding="utf-8")
        assert "synthetic-auth" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("mode", "status"), [("auth", "AUTH_EXPIRED"), ("redirect", "HTTP_ERROR"),
                                               ("limited", "RATE_LIMITED"), ("short", "FAILED"),
                                               ("duplicate", "FAILED")])
def test_failed_or_partial_pages_never_publish(tmp_path: Path, mode: str, status: str):
    assert capture(tmp_path, config(), client=Client(mode), pause=lambda _: None)["status"] == status
    assert not list(tmp_path.rglob("*.json"))


def test_rejects_arbitrary_destination_and_headers():
    value = config()
    value["url"] = "https://example.invalid/"
    with pytest.raises(ValueError):
        validate_session(value)
    value = config()
    value["headers"]["X-Unrelated"] = "synthetic"
    with pytest.raises(ValueError):
        validate_session(value)
    value = deepcopy(config())
    value["payload"]["code_selectors"]["include"][0]["values"][1] = "20:000000"
    with pytest.raises(ValueError):
        validate_session(value)


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_session_is_encrypted_for_current_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    save_session(config())
    raw = credential_path().read_bytes()
    assert b"synthetic-cookie" not in raw and b"synthetic-auth" not in raw
    assert load_session()["headers"]["cookie"] == "synthetic-cookie"
