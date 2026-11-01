"""Resume CZCE HTML member-ranking downloads into verified raw files only."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import requests

from .atomic_files import replace_with_retry
from .czce_rank_html import parse_czce_rank_html
from .futures_calendar import load_futures_trading_calendar
from .futures_official_backfill import (
    DEFAULT_DAILY_HOST_BUDGET,
    DEFAULT_MIN_INTERVAL_SECONDS,
    GovernedRequestsSession,
    RequestBudgetExhausted,
    _process_file_lock,
)


SOURCE_URL = "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataHolding.htm"
SCHEMA = "czce-html-rank-capture/v1"
ADAPTER = "official-czce-rank-html-raw-v1"
_BRONZE = Path("bronze") / "czce_html_rank_capture"
_STATE = Path("state") / "czce_html_rank_capture"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _source_url(day: str) -> str:
    compact = day.replace("-", "")
    return SOURCE_URL.format(year=compact[:4], day=compact)


def _completed_raw(root: Path, day: str) -> dict[str, Any] | None:
    """Trust a completed artifact only when its bytes match its manifest."""

    bronze_dir = root / _BRONZE
    raw_path = bronze_dir / f"{day}.htm"
    manifest = _read_json(bronze_dir / f"{day}.json")
    if manifest is None or not raw_path.is_file():
        return None
    if not (
        manifest.get("schema") == SCHEMA
        and manifest.get("status") == "PASS_RAW"
        and manifest.get("tradingDay") == day
        and manifest.get("sourceUrl") == _source_url(day)
        and manifest.get("authoritativeDataWritten") is False
        and manifest.get("rawPath") == raw_path.relative_to(root).as_posix()
    ):
        return None
    digest = sha256(raw_path.read_bytes()).hexdigest()
    return manifest if digest == manifest.get("payloadSha256") else None


class CzceHtmlClient:
    """One official GET per day through the shared request budget ledger."""

    def __init__(
        self,
        root: Path,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        cookie: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.czce.com.cn/cn/jysj/ccpm/H077003004index_1.htm",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36",
        })
        if cookie:
            self.session.headers["Cookie"] = cookie
        self.governed = GovernedRequestsSession(
            root, min_interval_seconds=min_interval_seconds,
            daily_budget=DEFAULT_DAILY_HOST_BUDGET, session=self.session,
        )

    def get(self, day: str) -> requests.Response:
        return self.governed.request(
            "GET", _source_url(day), timeout=30, allow_redirects=False,
        )

    def close(self) -> None:
        self.session.headers.pop("Cookie", None)
        self.session.cookies.clear()
        self.governed.close()


def capture_czce_html_ranks(
    root: Path,
    client: CzceHtmlClient,
    *,
    start_day: str = "2025-01-01",
    end_day: str = "2026-09-30",
    max_days: int | None = None,
) -> dict[str, Any]:
    """Capture validated HTML across calendar days without opening Catalog."""

    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    if start > end or (max_days is not None and max_days < 1):
        raise ValueError("invalid CZCE ranking range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("missing verified futures trading calendar")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("no verified futures trading days in CZCE ranking range")

    state_dir = root / _STATE
    bronze_dir = root / _BRONZE
    progress_path = state_dir / "progress.json"
    progress: dict[str, Any] = {
        "schema": SCHEMA, "publisher": "CZCE", "adapter": ADAPTER,
        "status": "RUNNING", "startDay": start.isoformat(), "endDay": end.isoformat(),
        "calendarDays": len(days), "completedDays": 0, "newDaysThisRun": 0,
        "authoritativeDataWritten": False, "updatedAt": _timestamp(),
    }

    with _process_file_lock(state_dir / "capture.lock"):
        try:
            for day in days:
                progress.update(currentDay=day, updatedAt=_timestamp())
                _write_json(progress_path, progress)
                existing = _completed_raw(root, day)
                state_path = state_dir / f"{day}.json"
                if existing is not None:
                    # A complete Bronze manifest repairs a state-write
                    # interruption without making another official request.
                    state = _read_json(state_path)
                    if state != existing:
                        _write_json(state_path, existing)
                    progress["completedDays"] += 1
                    continue
                if max_days is not None and progress["newDaysThisRun"] >= max_days:
                    progress["status"] = "BATCH_LIMIT"
                    break
                progress["newDaysThisRun"] += 1
                url = _source_url(day)
                evidence: dict[str, Any] = {
                    "schema": SCHEMA, "tradingDay": day, "publisher": "CZCE",
                    "sourceUrl": url, "adapter": ADAPTER, "retrievedAt": _timestamp(),
                    "status": "FAILED", "qualityStatus": "FAILED",
                    "httpStatus": None, "contentType": None, "payloadSha256": None,
                    "authoritativeDataWritten": False,
                }
                try:
                    response = client.get(day)
                    raw = bytes(response.content)
                    evidence.update(
                        retrievedAt=_timestamp(), httpStatus=int(response.status_code),
                        contentType=str(response.headers.get("Content-Type") or "") or None,
                        payloadSha256=sha256(raw).hexdigest(),
                    )
                    if response.status_code != 200:
                        raise ValueError(f"official HTTP {response.status_code}")
                    if "html" not in str(evidence["contentType"] or "").lower():
                        raise ValueError("official response is not HTML")
                    frames = parse_czce_rank_html(raw, trading_day=day.replace("-", ""))
                    raw_path = bronze_dir / f"{day}.htm"
                    evidence.update(
                        status="PASS_RAW", qualityStatus="PASS_RAW",
                        contractCount=len(frames),
                        rankingRows=sum(len(frame) for frame in frames.values()),
                        rawPath=raw_path.relative_to(root).as_posix(),
                    )
                    _write_bytes(raw_path, raw)
                    _write_json(bronze_dir / f"{day}.json", evidence)
                    _write_json(state_path, evidence)
                    progress["completedDays"] += 1
                except RequestBudgetExhausted:
                    evidence.update(status="BUDGET_PAUSED", qualityStatus="BUDGET_PAUSED",
                                    error="shared official request budget exhausted")
                    _write_json(state_path, evidence)
                    progress.update(status="BUDGET_PAUSED", errorDay=day)
                    break
                except Exception as error:
                    # Never store challenge or error response bodies. Retain
                    # only HTTP status, type and digest for diagnosis.
                    reason = str(error)[:240] if isinstance(error, ValueError) else "official request or file operation failed"
                    evidence.update(status="FAILED", qualityStatus="FAILED",
                                    errorType=type(error).__name__, error=reason)
                    _write_json(state_path, evidence)
                    progress.update(status="FAILED", errorDay=day,
                                    errorType=type(error).__name__,
                                    httpStatus=evidence["httpStatus"])
                    break
            else:
                progress["status"] = "PASS_RAW"
        finally:
            progress["updatedAt"] = _timestamp()
            _write_json(progress_path, progress)
    return progress


__all__ = ("CzceHtmlClient", "capture_czce_html_ranks")
