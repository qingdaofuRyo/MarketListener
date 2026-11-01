"""Official announcement search preserves evidence but never assigns risk status."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from market_monitor import cninfo_disclosure
from market_monitor.cninfo_disclosure import (PROGRESS_FILE, SCHEMA, SEARCH_URL, STATE_FILE,
                                              sync_cninfo_keyword_hits, sync_due_cninfo_keyword_hits)


def _item(identifier: str, title: str) -> dict:
    return {"secCode": "600293", "secName": "三峡新材", "orgId": "gssh0600293",
            "announcementId": identifier, "announcementTitle": title,
            "announcementTime": 1792425600000,
            "adjunctUrl": f"finalpage/2026-10-20/{identifier}.PDF", "pageColumn": "SHZB"}


class _Response:
    url = SEARCH_URL

    def __init__(self, items: list[dict], total: int | None = None) -> None:
        self.payload = {"totalAnnouncement": len(items) if total is None else total,
                        "announcements": items}
        self.content = json.dumps(self.payload).encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def post(self, url: str, *, data: dict, headers: dict, timeout: int) -> _Response:
        assert url == SEARCH_URL and timeout == 20
        assert headers["Origin"] == "https://www.cninfo.com.cn"
        self.requests.append(dict(data))
        return self.responses.pop(0)


def test_keyword_hits_are_deduplicated_and_not_legal_status(tmp_path: Path) -> None:
    result = sync_cninfo_keyword_hits(
        tmp_path, "2026-10-20", "2026-10-20", keywords=("停牌", "退市"),
        session=_Session([
            _Response([_item("1225611295", "停牌前一交易日股东情况")]),
            _Response([_item("1225611295", "公司<em>退市</em>风险提示")]),
        ]), min_interval_seconds=0,
    )
    assert result["status"] == "PASS" and result["storedHits"] == 2
    saved = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert saved["schema"] == SCHEMA
    assert len(saved["items"]) == 2
    assert {row["keyword"] for row in saved["items"]} == {"停牌", "退市"}
    assert all(row["matchStatus"] == "KEYWORD_HIT_PENDING_REVIEW" for row in saved["items"])
    assert all(row["reviewedRiskStatus"] is None for row in saved["items"])
    assert saved["items"][0]["pdfUrl"].startswith("https://static.cninfo.com.cn/finalpage/")
    assert "<em>" not in saved["items"][1]["title"]


def test_pagination_and_existing_state_are_resumable(tmp_path: Path) -> None:
    first = _Session([
        _Response([_item("1225611295", "关于停牌的进展公告")], total=2),
        _Response([_item("1225610705", "关于筹划停牌的公告")], total=2),
    ])
    report = sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-10-20",
                                      keywords=("停牌",), session=first, min_interval_seconds=0)
    assert report["keywords"]["停牌"] == {"reportedTotal": 2, "fetchedRows": 2,
                                               "complete": True, "pages": 2}
    assert [item["pageNum"] for item in first.requests] == [1, 2]
    second = _Session([_Response([_item("1225611295", "关于停牌的进展公告")])])
    again = sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-10-20",
                                     keywords=("停牌",), session=second, min_interval_seconds=0)
    assert again["storedHits"] == 2
    assert again["newHits"] == 0


def test_all_market_search_keeps_shenzhen_shanghai_and_beijing_records(tmp_path: Path) -> None:
    sh = _item("1225611295", "上海公司股票交易异常波动公告")
    sz = _item("1225610705", "深圳公司股票交易异常波动公告")
    bj = _item("1225610806", "北京公司股票交易异常波动公告")
    sz.update(secCode="301313", pageColumn="SZCY")
    bj.update(secCode="920690", pageColumn="BJS")
    session = _Session([_Response([sh, sz, bj])])
    sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-10-20",
                              keywords=("交易异常",), session=session, min_interval_seconds=0)
    assert session.requests[0]["column"] == "szse"  # the site labels this default 深沪京
    saved = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["items"]
    assert {row["pageColumn"] for row in saved} == {"SHZB", "SZCY", "BJS"}


def test_risk_signal_title_remains_pending_review(tmp_path: Path) -> None:
    event = _item("1225611295", "关于收到中国证监会<em>立案告知书</em>的公告")
    sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-10-20",
                              keywords=("立案告知书",), session=_Session([_Response([event])]),
                              min_interval_seconds=0)
    row = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["items"][0]
    assert row["keyword"] == "立案告知书"
    assert row["reviewedRiskStatus"] is None
    assert row["matchStatus"] == "KEYWORD_HIT_PENDING_REVIEW"


def test_search_result_without_title_phrase_is_labeled_explicitly(tmp_path: Path) -> None:
    event = _item("1225611295", "关于股东股份交易的异常情况说明")
    sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-10-20",
                              keywords=("交易异常",), session=_Session([_Response([event])]),
                              min_interval_seconds=0)
    row = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))["items"][0]
    assert row["titleContainsKeyword"] is False
    assert row["matchStatus"] == "KEYWORD_HIT_PENDING_REVIEW"


def test_malformed_identity_or_future_date_fails_closed(tmp_path: Path) -> None:
    invalid = _item("1225611295", "关于停牌的公告")
    invalid["adjunctUrl"] = "../private.pdf"
    with pytest.raises(ValueError, match="invalid"):
        sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-10-20",
                                  keywords=("停牌",), session=_Session([_Response([invalid])]),
                                  min_interval_seconds=0)
    assert not (tmp_path / STATE_FILE).exists()
    with pytest.raises(ValueError, match="window"):
        sync_cninfo_keyword_hits(tmp_path, "2026-10-20", "2026-11-30")


def test_due_disclosures_resume_week_at_a_time_after_long_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows: list[tuple[str, str]] = []

    def complete(_root: Path, start: str, end: str, **_kwargs: object) -> dict:
        windows.append((start, end))
        return {"status": "PASS", "startDay": start, "endDay": end,
                "newHits": 0, "storedHits": 0}

    monkeypatch.setattr(cninfo_disclosure, "sync_cninfo_keyword_hits", complete)
    bootstrap = sync_due_cninfo_keyword_hits(tmp_path, today=date(2026, 10, 20))
    assert windows == [("2026-10-17", "2026-10-20")]
    assert bootstrap["completeThroughDay"] == "2026-10-20"

    first = sync_due_cninfo_keyword_hits(tmp_path, today=date(2026, 11, 4))
    second = sync_due_cninfo_keyword_hits(tmp_path, today=date(2026, 11, 4))
    third = sync_due_cninfo_keyword_hits(tmp_path, today=date(2026, 11, 4))
    assert windows[1:] == [("2026-10-21", "2026-10-27"),
                           ("2026-10-28", "2026-11-03"),
                           ("2026-11-01", "2026-11-04")]
    assert first["backlogRemaining"] and second["backlogRemaining"]
    assert third["completeThroughDay"] == "2026-11-04"
    assert not third["backlogRemaining"]
    saved = json.loads((tmp_path / PROGRESS_FILE).read_text(encoding="utf-8"))
    assert set(saved["completeThroughByKeyword"].values()) == {"2026-11-04"}


def test_due_disclosures_narrow_capped_window_without_skipping_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows: list[tuple[str, str]] = []

    def capped(_root: Path, start: str, end: str, **_kwargs: object) -> dict:
        windows.append((start, end))
        return {"status": "PASS" if start == end else "PARTIAL",
                "startDay": start, "endDay": end, "newHits": 0, "storedHits": 0}

    monkeypatch.setattr(cninfo_disclosure, "sync_cninfo_keyword_hits", capped)
    report = sync_due_cninfo_keyword_hits(tmp_path, today=date(2026, 10, 20))
    assert windows == [("2026-10-17", "2026-10-20"),
                       ("2026-10-17", "2026-10-19"),
                       ("2026-10-17", "2026-10-17")]
    assert report["completeThroughDay"] == "2026-10-17"
    assert report["backlogRemaining"]
    assert set(json.loads((tmp_path / PROGRESS_FILE).read_text(encoding="utf-8"))
               ["completeThroughByKeyword"].values()) == {"2026-10-17"}


def test_due_disclosures_never_advance_a_partial_single_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def partial(_root: Path, start: str, end: str, **_kwargs: object) -> dict:
        return {"status": "PARTIAL", "startDay": start, "endDay": end,
                "newHits": 0, "storedHits": 0}

    monkeypatch.setattr(cninfo_disclosure, "sync_cninfo_keyword_hits", partial)
    report = sync_due_cninfo_keyword_hits(tmp_path, today=date(2026, 10, 20))
    assert report["completeThroughDay"] is None
    assert report["backlogRemaining"]
    assert not (tmp_path / PROGRESS_FILE).exists()


def test_disclosure_operation_remains_partial_while_backlog_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_monitor.operations import OperationKind
    from market_monitor.web_app import _operation_handlers

    monkeypatch.setattr(cninfo_disclosure, "sync_due_cninfo_keyword_hits", lambda _root: {
        "status": "PASS", "startDay": "2026-10-21", "endDay": "2026-10-27",
        "newHits": 2, "storedHits": 5, "completeThroughDay": "2026-10-27",
        "backlogRemaining": True,
    })
    handler = _operation_handlers(tmp_path)[OperationKind.DISCLOSURES_UPDATE]
    detail = handler()
    assert detail.startswith("PARTIAL_FAILURE:")
    assert "已完整覆盖至 2026-10-27" in detail
