"""Capture the public THS indexflash response as unverified local evidence.

No market date or field meaning has yet been proved from a complete response.
This probe never promotes a response into Gold or exposes the raw payload.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import requests

URL = "https://q.10jqka.com.cn/api.php?t=indexflash&"
_MAX_BYTES = 1_000_000
_STATES = {"UNVERIFIED", "BLOCKED", "FAILED", "NEVER"}


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_indexflash_status(root: Path) -> dict[str, str | None]:
    try:
        path = Path(root) / "state" / "ths_indexflash_status.json"
        if path.stat().st_size > 4096:
            raise ValueError("oversized status")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != "ths-indexflash-probe-v1":
            raise ValueError("invalid status")
        state, at = value.get("status"), value.get("checkedAt")
        if state not in _STATES or not isinstance(at, str):
            raise ValueError("invalid status")
        parsed = datetime.fromisoformat(at)
        if parsed.tzinfo is None:
            raise ValueError("invalid timestamp")
        return {"status": state, "checkedAt": parsed.astimezone(timezone.utc).isoformat()}
    except (OSError, ValueError, TypeError):
        return {"status": "NEVER", "checkedAt": None}


def probe_indexflash(root: Path, *, session: requests.Session | None = None) -> dict[str, str | None]:
    """Perform one bounded anonymous request; never infer a trading day."""

    root = Path(root)
    client = session or requests.Session()
    owned = session is None
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = "FAILED"
    response = None
    try:
        response = client.get(URL, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/",
                                           "Accept": "application/json, */*"},
                              timeout=(4, 8), allow_redirects=False, stream=True)
        if response.status_code in (401, 403, 429):
            state = "BLOCKED"
        elif response.status_code == 200:
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > _MAX_BYTES:
                    raise ValueError("oversized response")
            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except UnicodeDecodeError:
                payload = json.loads(body.decode("gb18030"))
            if not isinstance(payload, dict) or not payload:
                raise ValueError("unexpected payload")
            digest = sha256(body).hexdigest()
            evidence = root / "bronze" / "ths_indexflash" / digest
            _atomic(evidence / "response.raw", bytes(body))
            completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _atomic(evidence / "observations" / f"{uuid4().hex}.json", json.dumps({"schema": "ths-indexflash-bronze-v1",
                "requestedUrl": URL, "sha256": digest, "requestedAt": started_at,
                "fetchedAt": completed_at, "httpStatus": 200,
                "quality": "UNVERIFIED"}, ensure_ascii=False).encode("utf-8"))
            state = "UNVERIFIED"
    except (requests.RequestException, OSError, ValueError, UnicodeError, TypeError):
        state = "FAILED"
    finally:
        if response is not None:
            response.close()
        if owned:
            client.close()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = {"schema": "ths-indexflash-probe-v1", "status": state, "checkedAt": at}
    _atomic(root / "state" / "ths_indexflash_status.json", json.dumps(status).encode("utf-8"))
    return {"status": state, "checkedAt": at}
