"""Customer data watch keeps keyword leads separate from legal risk verdicts."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.cninfo_disclosure import SCHEMA, STATE_FILE
from market_monitor.web_app import create_web_app


def test_disclosure_watch_filters_official_pdf_and_exposes_pending_only(tmp_path: Path) -> None:
    root = tmp_path / "data"
    path = root / STATE_FILE
    path.parent.mkdir(parents=True)
    common = {"announcementId": "123456789", "securityCode": "600001",
              "securityName": "测试证券", "title": "停牌前一日公告",
              "publicationDay": "2026-10-20", "publishedAt": "2026-10-20T03:00:00Z",
              "keyword": "停牌", "matchStatus": "KEYWORD_HIT_PENDING_REVIEW",
              "reviewedRiskStatus": "UNVERIFIED_RISK", "fetchedAt": "2026-10-20T04:00:00Z"}
    path.write_text(json.dumps({"schema": SCHEMA, "items": [
        {**common, "pdfUrl": "https://static.cninfo.com.cn/finalpage/2026-10-20/a.PDF"},
        {**common, "announcementId": "123456790", "pdfUrl": "https://evil.example/a.pdf"},
    ]}), encoding="utf-8")
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    value = client.get("/api/data/disclosure-watch", params={"keyword": "停牌"}).json()
    assert value["available"] and value["total"] == 1
    assert value["items"][0]["reviewedRiskStatus"] is None
    assert value["items"][0]["matchStatus"] == "KEYWORD_HIT_PENDING_REVIEW"
    assert value["items"][0]["titleContainsKeyword"] is True
    assert client.get("/api/data/disclosure-watch", params={"keyword": "不存在"}).status_code == 422
    categories = client.get("/api/data/risk-signal-categories").json()
    assert categories["status"] == "CANDIDATE_ONLY"
    assert len(categories["categories"]) == 4
    interest = client.get("/api/data/search-interest-sources").json()
    assert interest["series"] == []
    assert len(interest["sources"]) == 2


def test_disclosure_watch_marks_fulltext_only_result_as_non_title_hit(tmp_path: Path) -> None:
    root = tmp_path / "data"
    path = root / STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": SCHEMA, "items": [{
        "announcementId": "123456789", "securityCode": "600001",
        "title": "关于股东股份交易的异常情况说明", "keyword": "交易异常",
        "publicationDay": "2026-10-20", "publishedAt": "2026-10-20T03:00:00Z",
        "pdfUrl": "https://static.cninfo.com.cn/finalpage/2026-10-20/a.PDF",
        "matchStatus": "KEYWORD_HIT_PENDING_REVIEW", "titleContainsKeyword": True,
    }]}), encoding="utf-8")
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    value = client.get("/api/data/disclosure-watch").json()
    assert value["total"] == 1
    assert value["items"][0]["titleContainsKeyword"] is False


def test_disclosure_watch_reports_malformed_snapshot_as_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "data"
    path = root / STATE_FILE
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    client = TestClient(create_web_app(root), client=("127.0.0.1", 50000))
    assert client.get("/api/data/disclosure-watch").status_code == 503
