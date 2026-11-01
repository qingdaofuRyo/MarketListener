"""Verified, source-isolated snapshots of official exchange delisting lists."""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

import requests

_SOURCES = {
    "SSE": "https://www.sse.com.cn/assortment/stock/list/delisting/",
    "SZSE": "https://www.szse.cn/market/stock/suspend/index.html",
}
_MAX_PAGE_BYTES = 3_000_000
_REFRESH_LOCK = threading.RLock()
_CURATED_PATH = Path(__file__).with_name("config") / "a_share_delisted_evidence.json"


class DelistedSourceError(ValueError):
    pass


class DelistedReviewRequired(DelistedSourceError):
    """A source's historical records changed and need independent review."""


class DelistedReviewChanged(DelistedSourceError):
    """The requested review snapshot is no longer the active one."""


def _parse_date(value: Any) -> str:
    text = str(value or "")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise DelistedSourceError("invalid date")
    return date.fromisoformat(text).isoformat()


def _a_code(exchange: str, code: str) -> bool:
    prefixes = ("600", "601", "603", "605", "688", "689") if exchange == "SSE" else ("000", "001", "002", "003", "300", "301")
    return bool(re.fullmatch(r"\d{6}", code) and code.startswith(prefixes))


def parse_pages(exchange: str, pages: list[bytes]) -> list[dict[str, str]]:
    """Require consistent page counts; never promote B shares or absent rows."""
    if exchange not in _SOURCES or not 1 <= len(pages) <= 50:
        raise DelistedSourceError("invalid source or page count")
    records: list[dict[str, Any]] = []
    expected_total = expected_pages = None
    for number, raw in enumerate(pages, start=1):
        if len(raw) > _MAX_PAGE_BYTES:
            raise DelistedSourceError("response too large")
        try:
            document = json.loads(raw)
            if exchange == "SSE":
                if document.get("actionErrors") or document.get("fieldErrors"):
                    raise DelistedSourceError("source error")
                metadata, data = document["pageHelp"], document["result"]
                page, total, total_pages, page_size = (int(metadata[x]) for x in ("pageNo", "total", "pageCount", "pageSize"))
            else:
                tabs = [row for row in document if isinstance(row, dict) and row.get("metadata", {}).get("tabkey") == "tab2"]
                if len(tabs) != 1 or tabs[0].get("error"):
                    raise DelistedSourceError("missing target tab")
                metadata, data = tabs[0]["metadata"], tabs[0]["data"]
                page, total, total_pages, page_size = (int(metadata[x]) for x in ("pageno", "recordcount", "pagecount", "pagesize"))
            if not isinstance(data, list) or page != number or total < 1 or total_pages < 1 or page_size < 1:
                raise DelistedSourceError("invalid pagination")
            if total_pages != (total + page_size - 1) // page_size or len(data) != min(page_size, total - (page - 1) * page_size):
                raise DelistedSourceError("incomplete page")
            if expected_total is None:
                expected_total, expected_pages = total, total_pages
            if total != expected_total or total_pages != expected_pages:
                raise DelistedSourceError("page count changed")
            records.extend(data)
        except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as error:
            raise DelistedSourceError("invalid response") from error
    if len(pages) != expected_pages or len(records) != expected_total:
        raise DelistedSourceError("missing pages")
    result: dict[str, dict[str, str]] = {}
    for row in records:
        if not isinstance(row, dict):
            raise DelistedSourceError("invalid row")
        if exchange == "SSE":
            kind = str(row.get("STOCK_TYPE") or "")
            if kind == "2":  # separate B-share row
                continue
            if kind not in {"1", "8"}:
                raise DelistedSourceError("unknown SSE classification")
            code, name, raw_date = str(row.get("A_STOCK_CODE") or ""), row.get("COMPANY_ABBR"), row.get("DELIST_DATE")
        else:
            code, name, raw_date = str(row.get("zqdm") or ""), row.get("zqjc"), row.get("zzrq")
        if not re.fullmatch(r"\d{6}", code) or not isinstance(name, str) or not name.strip():
            raise DelistedSourceError("invalid identifier or name")
        day = _parse_date(raw_date)
        if not _a_code(exchange, code):
            continue
        identifier = f"CN.{exchange}.STOCK.{code}"
        if identifier in result:
            raise DelistedSourceError("duplicate A-share identifier")
        result[identifier] = {"instrumentId": identifier, "name": name.strip(), "delistedAt": day,
                              "sourceUrl": _SOURCES[exchange]}
    if not result:
        raise DelistedSourceError("empty A-share result")
    return sorted(result.values(), key=lambda item: item["instrumentId"])


def _fetch_page(session: requests.Session, exchange: str, page: int) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/" if exchange == "SSE" else _SOURCES[exchange]}
    if exchange == "SSE":
        url = "https://query.sse.com.cn/commonQuery.do"
        params = {"sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "isPagination": "true", "STOCK_CODE": "",
                  "CSRC_CODE": "", "REG_PROVINCE": "", "STOCK_TYPE": "1,2,8", "COMPANY_STATUS": "3", "type": "inParams",
                  "pageHelp.beginPage": str(page), "pageHelp.pageSize": "500", "pageHelp.pageNo": str(page), "pageHelp.endPage": str(page)}
    else:
        url = "https://www.szse.cn/api/report/ShowReport/data"
        params = {"SHOWTYPE": "JSON", "CATALOGID": "1793_ssgs", "TABKEY": "tab2", "PAGENO": str(page), "random": "0.315"}
    response = session.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    if len(response.content) > _MAX_PAGE_BYTES:
        raise DelistedSourceError("response too large")
    return response.content


def _page_count(exchange: str, raw: bytes) -> int:
    try:
        document = json.loads(raw)
        if exchange == "SSE":
            count = int(document["pageHelp"]["pageCount"])
        else:
            tabs = [x for x in document if x.get("metadata", {}).get("tabkey") == "tab2"]
            count = int(tabs[0]["metadata"]["pagecount"])
        if not 1 <= count <= 50:
            raise ValueError("page limit")
        return count
    except (KeyError, TypeError, ValueError, IndexError, UnicodeError, AttributeError) as error:
        raise DelistedSourceError("invalid first page") from error


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(value)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _verify_curated_agreement(rows: list[dict[str, str]]) -> None:
    """A new snapshot must not displace a known conflicting official fact."""
    try:
        payload = json.loads(_CURATED_PATH.read_bytes())
        if payload.get("schemaVersion") != 1 or not isinstance(payload.get("items"), list):
            raise ValueError("invalid curated evidence")
        known = {item["instrumentId"]: item["delistedAt"] for item in payload["items"]}
        if any(item["instrumentId"] in known and known[item["instrumentId"]] != item["delistedAt"] for item in rows):
            raise ValueError("conflicting delisting dates")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise DelistedSourceError("curated evidence conflict or unavailable") from error


def _compare_history(previous: list[dict[str, str]], current: list[dict[str, str]]) -> list[dict[str, str | None]]:
    """Find disappeared or revised facts without assuming the reason."""
    old = {item["instrumentId"]: item for item in previous}
    new = {item["instrumentId"]: item for item in current}
    changes: dict[str, dict[str, str | None]] = {}
    for identifier, old_record in old.items():
        old_day = old_record["delistedAt"]
        next_record = new.get(identifier)
        next_day = next_record["delistedAt"] if next_record else None
        if next_day is None or next_day != old_day:
            changes[identifier] = {"instrumentId": identifier, "previousDate": old_day, "currentDate": next_day,
                                   "kind": "MISSING" if next_day is None else "DATE_CHANGED"}
        elif next_record["name"] != old_record["name"]:
            changes[identifier] = {"instrumentId": identifier, "previousDate": old_day, "currentDate": next_day,
                                   "previousName": old_record["name"], "currentName": next_record["name"],
                                   "kind": "NAME_CHANGED"}
    try:
        payload = json.loads(_CURATED_PATH.read_bytes())
        if payload.get("schemaVersion") != 1 or not isinstance(payload.get("items"), list):
            raise ValueError("invalid curated evidence")
        for item in payload["items"]:
            identifier, old_day = item["instrumentId"], item["delistedAt"]
            next_day = new[identifier]["delistedAt"] if identifier in new else None
            if next_day is not None and next_day != old_day:
                changes[identifier] = {"instrumentId": identifier, "previousDate": old_day,
                                       "currentDate": next_day, "kind": "DATE_CHANGED"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise DelistedSourceError("curated evidence unavailable") from error
    return [changes[key] for key in sorted(changes)]


def refresh_exchange(root: Path, exchange: str, *, session: requests.Session | None = None) -> dict[str, Any]:
    with _REFRESH_LOCK:
        return _refresh_exchange(root, exchange, session=session)


def _refresh_exchange(root: Path, exchange: str, *, session: requests.Session | None = None) -> dict[str, Any]:
    if exchange not in _SOURCES:
        raise DelistedSourceError("invalid exchange")
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    first = _fetch_page(client, exchange, 1)
    pages = [first] + [_fetch_page(client, exchange, page) for page in range(2, _page_count(exchange, first) + 1)]
    rows = parse_pages(exchange, pages)
    previous, previous_status = load_exchange(root, exchange, include_review_page_hashes=True)
    changes = _compare_history(previous, rows)
    if changes:
        folder = Path(root) / "bronze" / "exchange-delisted"
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        page_hashes = [sha256(raw).hexdigest() for raw in pages]
        existing_review = previous_status.get("review")
        if isinstance(existing_review, dict) and existing_review.get("pageSha256") == page_hashes:
            # Exact same verified source pages against the same baseline: do
            # not invalidate an operator's in-progress review or pagination.
            check = {"version": existing_review["sha256"], "checkedAt": observed_at}
            _atomic_write(folder / f"{exchange}-review-last-check.json",
                          json.dumps(check, separators=(",", ":")).encode("utf-8"))
            raise DelistedReviewRequired("historical records unchanged; review still required")
        review = {"schemaVersion": 1, "exchange": exchange, "observedAt": observed_at,
                  "baseSha256": previous_status.get("sha256"), "pages": [raw.decode("utf-8") for raw in pages],
                  "pageSha256": page_hashes, "changes": changes}
        content = json.dumps(review, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = sha256(content).hexdigest()
        name = f"{exchange}-review-{digest}.json"
        _atomic_write(folder / name, content)
        _atomic_write(folder / f"{exchange}-review.json", json.dumps({"snapshot": name, "sha256": digest}, separators=(",", ":")).encode())
        raise DelistedReviewRequired("historical records changed; review required")
    _verify_curated_agreement(rows)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    document = {"schemaVersion": 1, "exchange": exchange, "fetchedAt": fetched,
                "sourceUrl": _SOURCES[exchange], "pages": [raw.decode("utf-8") for raw in pages],
                "pageSha256": [sha256(raw).hexdigest() for raw in pages], "records": rows}
    content = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = sha256(content).hexdigest()
    folder = Path(root) / "bronze" / "exchange-delisted"
    name = f"{exchange}-{digest}.json"
    _atomic_write(folder / name, content)
    # The candidate agrees with the existing baseline. Clearing the active
    # review before publishing preserves a safe old baseline if interrupted.
    (folder / f"{exchange}-review.json").unlink(missing_ok=True)
    _atomic_write(folder / f"{exchange}-latest.json", json.dumps({"snapshot": name, "sha256": digest}, separators=(",", ":")).encode())
    return {"exchange": exchange, "status": "PASS", "count": len(rows), "fetchedAt": fetched, "sha256": digest}


def load_exchange(root: Path, exchange: str, *, include_review_changes: bool = False,
                  include_review_page_hashes: bool = False) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if exchange not in _SOURCES:
        raise DelistedSourceError("invalid exchange")
    folder = Path(root) / "bronze" / "exchange-delisted"
    pointer = folder / f"{exchange}-latest.json"
    if not pointer.exists():
        status = {"exchange": exchange, "status": "UNAVAILABLE", "count": 0}
        review = _load_review(folder, exchange, [], None, include_changes=include_review_changes,
                              include_page_hashes=include_review_page_hashes)
        if review:
            status["review"] = review
        return [], status
    try:
        info = json.loads(pointer.read_bytes())
        digest, name = info["sha256"], info["snapshot"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or name != f"{exchange}-{digest}.json":
            raise ValueError("invalid pointer")
        content = (folder / name).read_bytes()
        if len(content) > _MAX_PAGE_BYTES * 50 or sha256(content).hexdigest() != digest:
            raise ValueError("snapshot hash mismatch")
        snapshot = json.loads(content)
        if snapshot["schemaVersion"] != 1 or snapshot["exchange"] != exchange or snapshot["sourceUrl"] != _SOURCES[exchange]:
            raise ValueError("snapshot identity mismatch")
        fetched = datetime.fromisoformat(snapshot["fetchedAt"])
        if fetched.tzinfo is None:
            raise ValueError("missing timezone")
        pages = [value.encode("utf-8") for value in snapshot["pages"]]
        if [sha256(value).hexdigest() for value in pages] != snapshot["pageSha256"]:
            raise ValueError("page hash mismatch")
        rows = parse_pages(exchange, pages)
        if rows != snapshot["records"]:
            raise ValueError("record mismatch")
        status = {"exchange": exchange, "status": "PASS", "count": len(rows), "fetchedAt": snapshot["fetchedAt"], "sha256": digest}
        review = _load_review(folder, exchange, rows, digest, include_changes=include_review_changes,
                              include_page_hashes=include_review_page_hashes)
        if review:
            status["review"] = review
        return rows, status
    except (OSError, KeyError, ValueError, TypeError, UnicodeError, AttributeError) as error:
        raise DelistedSourceError("stored snapshot invalid") from error


def _load_review(folder: Path, exchange: str, previous: list[dict[str, str]], base_digest: str | None,
                 *, include_changes: bool = False, include_page_hashes: bool = False) -> dict[str, Any] | None:
    pointer = folder / f"{exchange}-review.json"
    if not pointer.exists():
        return None
    try:
        info = json.loads(pointer.read_bytes())
        digest, name = info["sha256"], info["snapshot"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or name != f"{exchange}-review-{digest}.json":
            raise ValueError("invalid review pointer")
        raw = (folder / name).read_bytes()
        if len(raw) > _MAX_PAGE_BYTES * 50 or sha256(raw).hexdigest() != digest:
            raise ValueError("review hash mismatch")
        record = json.loads(raw)
        if record["schemaVersion"] != 1 or record["exchange"] != exchange or record["baseSha256"] != base_digest:
            raise ValueError("review baseline changed")
        observed = datetime.fromisoformat(record["observedAt"])
        if observed.tzinfo is None:
            raise ValueError("missing timezone")
        pages = [value.encode("utf-8") for value in record["pages"]]
        if [sha256(value).hexdigest() for value in pages] != record["pageSha256"]:
            raise ValueError("review page hash mismatch")
        current = parse_pages(exchange, pages)
        changes = _compare_history(previous, current)
        if not changes or changes != record["changes"]:
            raise ValueError("review changes mismatch")
        result = {"status": "REVIEW_REQUIRED", "sha256": digest, "observedAt": record["observedAt"],
                  "affectedCount": len(changes), "affectedIds": [item["instrumentId"] for item in changes]}
        last_check = _last_review_check(folder, exchange, digest, observed)
        if last_check:
            result["lastCheckedAt"] = last_check
        if include_page_hashes:
            result["pageSha256"] = record["pageSha256"]
        if include_changes:
            old_names = {row["instrumentId"]: row["name"] for row in previous}
            new_names = {row["instrumentId"]: row["name"] for row in current}
            result["changes"] = [{**item, "previousName": old_names.get(item["instrumentId"]),
                                  "currentName": new_names.get(item["instrumentId"])} for item in changes]
        return result
    except (OSError, KeyError, ValueError, TypeError, UnicodeError, AttributeError) as error:
        raise DelistedSourceError("stored review invalid") from error


def _last_review_check(folder: Path, exchange: str, version: str, observed: datetime) -> str | None:
    """Read optional diagnostics; malformed or old markers cannot change evidence."""
    path = folder / f"{exchange}-review-last-check.json"
    try:
        if path.stat().st_size > 4096:
            return None
        marker = json.loads(path.read_bytes())
        checked = datetime.fromisoformat(marker["checkedAt"])
        if (marker.get("version") != version or checked.tzinfo is None
                or checked.astimezone(timezone.utc) < observed.astimezone(timezone.utc)):
            return None
        return str(marker["checkedAt"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def review_page(root: Path, exchange: str, *, version: str, offset: int = 0, limit: int = 40,
                start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Read one bounded page of the exact active review, never mixed versions."""
    if exchange not in _SOURCES or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ValueError("invalid review identity")
    if not 0 <= offset <= 100_000 or not 1 <= limit <= 200:
        raise ValueError("invalid review page")
    if start:
        _parse_date(start)
    if end:
        _parse_date(end)
    if start and end and start > end:
        raise ValueError("invalid date range")
    with _REFRESH_LOCK:
        _rows, status = load_exchange(root, exchange, include_review_changes=True)
        review = status.get("review")
        if not isinstance(review, dict) or review.get("sha256") != version:
            raise DelistedReviewChanged("active review changed")
        matching = [item for item in review["changes"] if any(
            isinstance(day, str) and (not start or day >= start) and (not end or day <= end)
            for day in (item.get("previousDate"), item.get("currentDate")))]
        return {"exchange": exchange, "version": version, "observedAt": review["observedAt"],
                "sourceUrl": _SOURCES[exchange], "affectedCount": review["affectedCount"],
                "matchedCount": len(matching), "offset": offset, "limit": limit,
                "hasMore": offset + limit < len(matching), "changes": matching[offset:offset + limit]}


def refresh_due(root: Path, now: datetime) -> dict[str, Any]:
    """Refresh each exchange once after the latest 19:00 Beijing slot.

    The caller's independent operation supplies the four-hour failure backoff.
    One failing exchange never discards the other's verified snapshot.
    """
    from market_monitor.data_page_auto_refresh import data_target
    from market_monitor.operations import OperationKind

    if now.tzinfo is None:
        raise ValueError("timezone required")
    _, opened = data_target(OperationKind.A_SHARE_DELISTED_UPDATE, now)
    output = []
    with _REFRESH_LOCK:
        for exchange in ("SSE", "SZSE"):
            try:
                _rows, existing = load_exchange(root, exchange)
                fetched_text = existing.get("fetchedAt")
                fetched = datetime.fromisoformat(fetched_text) if isinstance(fetched_text, str) else None
                if (not existing.get("review") and fetched is not None and fetched.tzinfo is not None
                        and opened <= fetched.astimezone(timezone.utc) <= now.astimezone(timezone.utc)):
                    output.append({**existing, "status": "SKIPPED_FRESH"})
                    continue
            except DelistedSourceError:
                pass
            try:
                output.append(refresh_exchange(root, exchange))
            except (requests.RequestException, DelistedSourceError, OSError, UnicodeError):
                output.append({"exchange": exchange, "status": "FAILED"})
    return {"status": "PASS" if all(item["status"] != "FAILED" for item in output) else "PARTIAL_FAILURE",
            "items": output}
