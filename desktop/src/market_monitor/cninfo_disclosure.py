"""Source-isolated CNINFO announcement keyword observations.

Search results are leads for a human reviewer.  Search matching can include
announcement text, so a returned row need not contain the query in its title.
Neither a title nor a body hit establishes the stock's current legal status.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests


SEARCH_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
SEARCH_PAGE = "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
PDF_ORIGIN = "https://static.cninfo.com.cn/"
KEYWORDS = ("停牌", "交易异常", "退市")
# These are search-result leads, not assessed or adjudicated risk categories.
RISK_SIGNAL_TERMS = ("退市风险警示", "可能被实施退市风险警示", "终止上市",
                     "立案告知书", "行政处罚事先告知书", "净资产为负", "市值低于")
SCHEMA = "market-listener/cninfo-keyword-hits/v1"
STATE_FILE = Path("state") / "cninfo_disclosure_keywords.json"
PROGRESS_FILE = Path("state") / "cninfo_disclosure_progress.json"
PROGRESS_SCHEMA = "market-listener/cninfo-keyword-progress/v1"
_BEIJING = ZoneInfo("Asia/Shanghai")
_PDF_PATH = re.compile(r"finalpage/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]+\.PDF", re.I)
_SECURITY_CODE = re.compile(r"\d{6}")
_ANNOUNCEMENT_ID = re.compile(r"\d{6,20}")


class _PlainTitle(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_data(self, data: str) -> None:
        self.fragments.append(data)


def _plain_title(value: str) -> str:
    parser = _PlainTitle()
    parser.feed(value)
    return " ".join(html.unescape("".join(parser.fragments)).split())


def _day(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("CNINFO search dates must use YYYY-MM-DD")
    return parsed


def _announcement(item: Any, keyword: str, start: date, end: date,
                  fetched_at: str, response_sha: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("CNINFO announcement is not an object")
    identifier = str(item.get("announcementId") or "")
    code = str(item.get("secCode") or "")
    title = _plain_title(str(item.get("announcementTitle") or ""))
    name = _plain_title(str(item.get("secName") or ""))
    raw_pdf = str(item.get("adjunctUrl") or "")
    moment = item.get("announcementTime")
    if (not _ANNOUNCEMENT_ID.fullmatch(identifier) or not _SECURITY_CODE.fullmatch(code)
            or not title or not name or not _PDF_PATH.fullmatch(raw_pdf)
            or type(moment) is not int or moment <= 0):
        raise ValueError("CNINFO announcement identity, title, time or PDF path is invalid")
    published = datetime.fromtimestamp(moment / 1000, tz=timezone.utc)
    local_day = published.astimezone(_BEIJING).date()
    if not start <= local_day <= end or published > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("CNINFO announcement publication time is outside the requested window")
    return {
        "announcementId": identifier,
        "securityCode": code,
        "securityName": name,
        "title": title,
        "titleContainsKeyword": keyword in title,
        "publishedAt": published.isoformat(timespec="seconds"),
        "publicationDay": local_day.isoformat(),
        "pdfUrl": PDF_ORIGIN + raw_pdf,
        "pageColumn": str(item.get("pageColumn") or "")[:30],
        "keyword": keyword,
        "matchStatus": "KEYWORD_HIT_PENDING_REVIEW",
        "reviewedRiskStatus": None,
        "source": "CNINFO_OFFICIAL_DISCLOSURE_SEARCH",
        "sourceSearchUrl": SEARCH_PAGE,
        "responseSha256": response_sha,
        "fetchedAt": fetched_at,
    }


def _read_state(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or not isinstance(value.get("items"), list)):
        raise ValueError("Existing CNINFO keyword state has an unknown schema")
    return value["items"]


def _save_state(path: Path, items: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = {"schema": SCHEMA, "items": sorted(items, key=lambda item: (
        item["publicationDay"], item["announcementId"], item["keyword"]))}
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_cninfo_keyword_hits(
    data_root: Path, start_day: str, end_day: str, *,
    keywords: tuple[str, ...] = KEYWORDS,
    session: requests.Session | None = None,
    max_pages_per_keyword: int = 20,
    min_interval_seconds: float = .25,
) -> dict[str, Any]:
    """Capture official title-search observations without inferring stock status."""

    start, end = _day(start_day), _day(end_day)
    if start > end or (end - start).days > 31 or end > datetime.now(_BEIJING).date():
        raise ValueError("CNINFO search window must be a past or current interval of at most 32 days")
    if (not keywords or any(keyword not in (*KEYWORDS, *RISK_SIGNAL_TERMS) for keyword in keywords)
            or len(set(keywords)) != len(keywords)):
        raise ValueError("CNINFO keywords must be distinct configured terms")
    if not 1 <= max_pages_per_keyword <= 100 or min_interval_seconds < 0:
        raise ValueError("Invalid CNINFO request budget")
    own_session = session is None
    session = session or requests.Session()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_items: dict[tuple[str, str], dict[str, Any]] = {}
    keyword_reports: dict[str, dict[str, Any]] = {}
    try:
        for keyword in keywords:
            count = 0
            complete = True
            page = 1
            while page <= max_pages_per_keyword:
                response = session.post(SEARCH_URL, data={
                    "pageNum": page, "pageSize": 30, "column": "szse", "tabName": "fulltext",
                    "plate": "", "stock": "", "searchkey": keyword, "secid": "",
                    "category": "", "trade": "", "seDate": f"{start_day}~{end_day}",
                    "sortName": "", "sortType": "", "isHLtitle": "true",
                }, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://www.cninfo.com.cn",
                            "Referer": SEARCH_PAGE}, timeout=20)
                response.raise_for_status()
                if response.url != SEARCH_URL or len(response.content) > 3_000_000:
                    raise ValueError("CNINFO search redirected or exceeded the expected response size")
                payload = response.json()
                if not isinstance(payload, dict) or type(payload.get("totalAnnouncement")) is not int:
                    raise ValueError("CNINFO search response has no verified total")
                announced = payload.get("announcements") or []
                if not isinstance(announced, list) or len(announced) > 30:
                    raise ValueError("CNINFO announcement page shape changed")
                digest = hashlib.sha256(response.content).hexdigest()
                for item in announced:
                    parsed = _announcement(item, keyword, start, end, fetched_at, digest)
                    new_items[(parsed["announcementId"], keyword)] = parsed
                count += len(announced)
                expected = payload["totalAnnouncement"]
                if count >= expected:
                    break
                if not announced:
                    raise ValueError("CNINFO search stopped before the reported total")
                page += 1
                if min_interval_seconds:
                    time.sleep(min_interval_seconds)
            else:
                complete = False
            keyword_reports[keyword] = {"reportedTotal": expected, "fetchedRows": count,
                                        "complete": complete, "pages": page}
    finally:
        if own_session:
            session.close()
    path = Path(data_root) / STATE_FILE
    existing = {(item["announcementId"], item["keyword"]): item for item in _read_state(path)}
    newly_added = len(new_items.keys() - existing.keys())
    existing.update(new_items)
    _save_state(path, existing.values())
    complete = all(item["complete"] for item in keyword_reports.values())
    return {"status": "PASS" if complete else "PARTIAL", "source": "CNINFO_OFFICIAL_DISCLOSURE_SEARCH",
            "startDay": start_day, "endDay": end_day, "keywords": keyword_reports,
            "newHits": newly_added, "matchedHits": len(new_items),
            "storedHits": len(existing), "fetchedAt": fetched_at,
            "statePath": str(path)}


def _read_complete_through(path: Path, keywords: tuple[str, ...], today: date) -> date | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(payload, dict) or payload.get("schema") != PROGRESS_SCHEMA
            or not isinstance(payload.get("completeThroughByKeyword"), dict)):
        raise ValueError("CNINFO progress has an unknown schema")
    days = []
    for keyword in keywords:
        raw = payload["completeThroughByKeyword"].get(keyword)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("CNINFO progress day is malformed")
        day = _day(raw)
        if day > today:
            raise ValueError("CNINFO progress day is in the future")
        days.append(day)
    return min(days)


def _write_complete_through(path: Path, keywords: tuple[str, ...], day: date) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"schema": PROGRESS_SCHEMA, "completeThroughByKeyword": {}}
    if (not isinstance(payload, dict) or payload.get("schema") != PROGRESS_SCHEMA
            or not isinstance(payload.get("completeThroughByKeyword"), dict)):
        raise ValueError("CNINFO progress has an unknown schema")
    progress = payload["completeThroughByKeyword"]
    for keyword in keywords:
        previous = progress.get(keyword)
        if previous is not None and _day(previous) > day:
            continue
        progress[keyword] = day.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_due_cninfo_keyword_hits(
    data_root: Path, *, today: date | None = None,
    keywords: tuple[str, ...] = (*KEYWORDS, *RISK_SIGNAL_TERMS),
) -> dict[str, Any]:
    """Resume a bounded disclosure window, including recent late publications.

    An absent checkpoint starts with only the last four calendar days. A
    successful window advances the checkpoint; a capped result is retried in
    smaller windows and never marks unseen days complete.
    """

    local_day = today or datetime.now(_BEIJING).date()
    if not keywords or len(set(keywords)) != len(keywords):
        raise ValueError("CNINFO progress keywords must be distinct")
    checkpoint = _read_complete_through(Path(data_root) / PROGRESS_FILE, keywords, local_day)
    if checkpoint is None:
        start = local_day - timedelta(days=3)
    elif (local_day - checkpoint).days <= 4:
        start = checkpoint - timedelta(days=2)
    else:
        start = checkpoint + timedelta(days=1)
    for window_days in (7, 3, 1):
        end = min(local_day, start + timedelta(days=window_days - 1))
        report = sync_cninfo_keyword_hits(data_root, start.isoformat(), end.isoformat(),
                                          keywords=keywords)
        if report["status"] == "PASS":
            _write_complete_through(Path(data_root) / PROGRESS_FILE, keywords, end)
            report["completeThroughDay"] = end.isoformat()
            report["backlogRemaining"] = end < local_day
            return report
    report["completeThroughDay"] = checkpoint.isoformat() if checkpoint else None
    report["backlogRemaining"] = True
    return report


__all__ = ("KEYWORDS", "PROGRESS_FILE", "PROGRESS_SCHEMA", "RISK_SIGNAL_TERMS", "SCHEMA",
           "SEARCH_URL", "STATE_FILE", "sync_cninfo_keyword_hits", "sync_due_cninfo_keyword_hits")
