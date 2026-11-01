from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pytest

from market_monitor import ths_etf_nav_probe as probe


def response(rows, info=None):
    items = {"f" + code: {"code": code, "name": "示例ETF", "newdate": day,
                          "newnet": "1.2345", "rate": rate} for code, day, rate in rows}
    return ("g(" + json.dumps({"error": {"id": 0}, "data": {"info": info or {"star": "2026-10-29", "count": 1},
                         "data": items}}, ensure_ascii=False) + ")").encode()


class Response:
    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code, self.closed = content, status, False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Client:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


def test_pages_commit_all_rows_with_real_dates_and_hashes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(probe, "PAGE_SIZE", 2)
    one = response([("000001", "2026-10-29", "2.00"), ("000002", "2026-10-28", "1.00")])
    two = response([("000003", "", "-")])
    client = Client([Response(one), Response(two)])
    result = probe.capture_etf_nav(tmp_path, session=client)
    assert result["status"] == "UNVERIFIED" and result["recordCount"] == 3
    assert result["missingDateCount"] == 1
    folder = next((tmp_path / "bronze" / "ths_etf_nav").iterdir())
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pageHashes"] == [sha256(one).hexdigest(), sha256(two).hexdigest()]
    assert [r["navDate"] for r in manifest["records"]] == ["2026-10-29", "2026-10-28", None]
    assert all(r[1]["allow_redirects"] is False for r in client.requests)
    assert not (tmp_path / "catalog.duckdb").exists()


@pytest.mark.parametrize("case", ["duplicate", "info_changed", "wrong_order", "bad_jsonp", "forbidden", "oversized", "future"])
def test_bad_or_incomplete_pagination_never_publishes(tmp_path: Path, monkeypatch, case):
    monkeypatch.setattr(probe, "PAGE_SIZE", 2)
    one = response([("000001", "2026-10-29", "2"), ("000002", "2026-10-29", "1")])
    second = response([("000003", "2026-10-29", "0")])
    if case == "duplicate": second = response([("000001", "2026-10-29", "0")])
    if case == "info_changed": second = response([("000003", "2026-10-29", "0")], {"star": "2026-10-28"})
    if case == "wrong_order": second = response([("000003", "2026-10-29", "3")])
    if case == "bad_jsonp": second = b"g({});evil()"
    if case == "oversized": second = b"x" * (probe.MAX_PAGE_BYTES + 1)
    if case == "future": second = response([("000003", "2099-01-01", "0")])
    session = Client([Response(one), Response(second, 403 if case == "forbidden" else 200)])
    assert probe.capture_etf_nav(tmp_path, session=session)["status"] == "FAILED"
    assert not (tmp_path / "bronze" / "ths_etf_nav").exists()


def test_page_limit_and_bad_dates_fail(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(probe, "PAGE_SIZE", 2)
    monkeypatch.setattr(probe, "MAX_PAGES", 1)
    page = response([("000001", "2026-10-29", "1"), ("000002", "2026-10-29", "0")])
    assert probe.capture_etf_nav(tmp_path, session=Client([Response(page)]))["status"] == "FAILED"
    with pytest.raises(ValueError):
        probe._parse(response([("000001", "2026-99-01", "1")]), date(2026, 10, 30))


def test_short_page_cannot_hide_missing_current_date_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(probe, "PAGE_SIZE", 2)
    page = response([("000001", "2026-10-29", "1")], {"star": "2026-10-29", "count": 3})
    assert probe.capture_etf_nav(tmp_path, session=Client([Response(page)]))["status"] == "FAILED"
    assert not (tmp_path / "bronze" / "ths_etf_nav").exists()
