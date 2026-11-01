"""Bounded anonymous KPL observation, isolated from published market data.

The positions of the returned values and their coverage remain unverified.
The response is evidence only and must not be interpreted as Gold metrics.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

URL = "https://apphwhq.kaipanla.com/w1/api/index.php"
FORM = {"c": "HomeDingPan", "a": "RiseFallAnalysis", "PhoneOSNew": "1",
        "VerSion": "6.3.20.2", "Red": "0", "apiv": "w48"}
HISTORY_URL = "https://apphis.kaipanla.com/w1/api/index.php"
HISTORY_FORM = {"c": "HisHomeDingPan", "a": "RiseFallAnalysis", "PhoneOSNew": "1",
                "VerSion": "5.22.0.2", "apiv": "w43", "st": "50", "Index": "0"}
MAX_BYTES = 64_000
STATES = {"NEVER", "UNVERIFIED", "EMPTY", "STALE", "BLOCKED", "FAILED"}


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_kpl_status(root: Path, *, history: bool = False) -> dict[str, str | None]:
    try:
        path = Path(root) / "state" / ("kpl_market_history_status.json" if history else "kpl_market_probe_status.json")
        if path.stat().st_size > 4096:
            raise ValueError("oversized status")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != "kpl-market-probe-v1":
            raise ValueError("invalid status")
        state, at, day = data.get("status"), data.get("checkedAt"), data.get("observedDate")
        if state not in STATES or not isinstance(at, str):
            raise ValueError("invalid state")
        stamp = datetime.fromisoformat(at)
        if stamp.tzinfo is None:
            raise ValueError("naive timestamp")
        if day is not None and (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day):
            raise ValueError("invalid date")
        return {"status": state, "checkedAt": stamp.astimezone(timezone.utc).isoformat(),
                "observedDate": day}
    except (OSError, ValueError, TypeError, OverflowError):
        return {"status": "NEVER", "checkedAt": None, "observedDate": None}


def _validate(payload: object, today: date, *, history: bool = False) -> str | None:
    if not isinstance(payload, dict) or str(payload.get("errcode")) != "0":
        raise ValueError("invalid response")
    # Never write an unforeseen response that could contain private fields.
    if not set(payload).issubset({"info", "errcode", "ttag", "list", "List"}):
        raise ValueError("unexpected fields")
    tag = payload.get("ttag")
    if tag is not None and (isinstance(tag, bool) or not isinstance(tag, (int, float)) or not math.isfinite(tag)):
        raise ValueError("invalid tag")
    if any(payload.get(key, []) != [] for key in ("list", "List")):
        raise ValueError("unexpected list")
    rows = payload.get("info")
    if rows is None and payload.get("list") == [] and payload.get("List") == []:
        return None
    if not isinstance(rows, list) or not (1 <= len(rows) <= (50 if history else 1)):
        raise ValueError("invalid observations")
    days: list[str] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 7:
            raise ValueError("invalid row")
        for index, value in enumerate(row[:6]):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("invalid number")
            if value < 0 or (index == 4 and value > 100) or (index != 4 and value != int(value)):
                raise ValueError("invalid value")
        day = row[6]
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or date.fromisoformat(day) > today:
            raise ValueError("invalid date")
        days.append(day)
    if days != sorted(set(days), reverse=True):
        raise ValueError("duplicate or unordered days")
    return days[0]


def probe_kpl_market(root: Path, *, expected_date: date | None = None,
                     session: requests.Session | None = None) -> dict[str, str | None]:
    """Observe once; `expected_date` must come from a verified trading calendar.

    This function never schedules itself and never writes a published series.
    Its own session bypasses environment/system proxies to avoid proxy loops;
    HTTPS certificate validation is retained.
    """
    return _probe(root, expected_date=expected_date, session=session, history=False)


def probe_kpl_history(root: Path, *, expected_date: date | None = None,
                      session: requests.Session | None = None) -> dict[str, str | None]:
    """Capture the first bounded history page as separately unverified evidence."""
    return _probe(root, expected_date=expected_date, session=session, history=True)


def _probe(root: Path, *, expected_date: date | None, session: requests.Session | None,
           history: bool) -> dict[str, str | None]:
    root = Path(root)
    owned = session is None
    client = session or requests.Session()
    if owned:
        client.trust_env = False
    started = datetime.now(timezone.utc)
    response = None
    state, observed = "FAILED", None
    try:
        url, form = (HISTORY_URL, HISTORY_FORM) if history else (URL, FORM)
        response = client.post(url, data=form, headers={"User-Agent": "okhttp/4.9.3"},
                               timeout=(4, 8), allow_redirects=False, stream=True)
        if response.status_code in (401, 403, 429):
            state = "BLOCKED"
        elif response.status_code == 200:
            body = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError("oversized response")
            payload = json.loads(body.decode("utf-8-sig"))
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            observed = _validate(payload, today, history=history)
            if observed is None:
                state = "EMPTY"
            else:
                digest = sha256(body).hexdigest()
                evidence = root / "bronze" / ("kpl_market_history" if history else "kpl_market_probe") / digest
                _atomic(evidence / "response.raw", bytes(body))
                _atomic(evidence / "observations" / f"{uuid4().hex}.json", json.dumps({
                    "schema": "kpl-market-bronze-v1", "requestedUrl": url, "sha256": digest,
                    "requestedAt": started.isoformat(), "fetchedAt": datetime.now(timezone.utc).isoformat(),
                    "observedDate": observed, "httpStatus": 200, "quality": "UNVERIFIED",
                }).encode("utf-8"))
                state = "STALE" if expected_date and observed != expected_date.isoformat() else "UNVERIFIED"
    except (requests.RequestException, OSError, ValueError, UnicodeError, TypeError, OverflowError):
        state, observed = "FAILED", None
    finally:
        if response is not None:
            response.close()
        if owned:
            client.close()
    status = {"status": state, "checkedAt": datetime.now(timezone.utc).isoformat(), "observedDate": observed}
    _atomic(root / "state" / ("kpl_market_history_status.json" if history else "kpl_market_probe_status.json"),
            json.dumps({"schema": "kpl-market-probe-v1", **status}).encode("utf-8"))
    return status
