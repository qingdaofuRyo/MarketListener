"""Concurrent, resumable SHFE/INE official member rankings to Bronze only."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import requests

from .czce_html_rank_backfill import _read_json, _write_bytes, _write_json
from .futures_calendar import load_futures_trading_calendar
from .futures_official_backfill import _process_file_lock
from .futures_official_member_sources import parse_shfe_rank_json


SCHEMA = "shfe-member-rank-bronze-capture/v1"
ADAPTER = "official-shfe-ine-rank-dat-raw-v1"
URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{day}.dat"
REFERER = "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_params=pm"
BRONZE = Path("bronze") / "shfe_rank_capture"
STATE = Path("state") / "shfe_rank_capture"
INE_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})
MAX_BYTES = 16 * 1024 * 1024


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_url(day: str) -> str:
    return URL.format(day=day.replace("-", ""))


def validate_shfe_rank(raw: bytes, day: str) -> dict[str, Any]:
    """Check official status/date and count published contracts by venue."""

    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("SHFE ranking response has invalid size")
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("SHFE ranking response is not a JSON object")
    code = payload.get("o_code")
    if code is None or str(code).strip().zfill(4) != "0000":
        raise ValueError("SHFE ranking response has a non-success o_code")
    if str(payload.get("report_date") or "").strip() != day.replace("-", ""):
        raise ValueError("SHFE ranking report_date does not match request")
    frames = parse_shfe_rank_json(raw)
    if not frames:
        raise ValueError("SHFE ranking response has no dated contract rows")
    venues = {venue: {"contractCount": 0, "rankingRows": 0} for venue in ("SHFE", "INE")}
    for contract, frame in frames.items():
        product = contract.rstrip("0123456789")
        venue = "INE" if product in INE_PRODUCTS else "SHFE"
        venues[venue]["contractCount"] += 1
        venues[venue]["rankingRows"] += len(frame)
    return {
        "contractCount": len(frames),
        "rankingRows": sum(len(frame) for frame in frames.values()),
        "venues": venues,
    }


def _completed(root: Path, day: str) -> dict[str, Any] | None:
    raw_path = root / BRONZE / f"{day}.dat"
    manifest = _read_json(root / BRONZE / f"{day}.json")
    if manifest is None or not raw_path.is_file():
        return None
    if not (
        manifest.get("schema") == SCHEMA
        and manifest.get("status") == "PASS_RAW"
        and manifest.get("tradingDay") == day
        and manifest.get("sourceUrl") == _source_url(day)
        and manifest.get("rawPath") == raw_path.relative_to(root).as_posix()
        and manifest.get("authoritativeDataWritten") is False
    ):
        return None
    raw = raw_path.read_bytes()
    if sha256(raw).hexdigest() != manifest.get("payloadSha256"):
        return None
    try:
        summary = validate_shfe_rank(raw, day)
    except (UnicodeError, ValueError, TypeError):
        return None
    return manifest if summary == manifest.get("summary") else None


class AdaptiveGate:
    """Start at full concurrency, then back off all workers after transient errors."""

    def __init__(self, workers: int) -> None:
        if workers < 1 or workers > 8:
            raise ValueError("SHFE worker count must be between 1 and 8")
        self.workers = workers
        self.capacity = workers
        self.active = 0
        self.failures = 0
        self.success_streak = 0
        self.next_allowed = 0.0
        self.stopped = False
        self.condition = threading.Condition()

    def acquire(self) -> bool:
        with self.condition:
            while not self.stopped:
                delay = self.next_allowed - time.monotonic()
                if self.active < self.capacity and delay <= 0:
                    self.active += 1
                    return True
                self.condition.wait(timeout=max(0.05, delay) if delay > 0 else 0.25)
            return False

    def release(self) -> None:
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def transient(self, retry_after: float = 0) -> None:
        with self.condition:
            self.failures += 1
            self.success_streak = 0
            self.capacity = max(1, self.capacity // 2)
            delay = min(60.0, max(retry_after, 0.5 * 2 ** min(self.failures - 1, 7)))
            self.next_allowed = max(self.next_allowed, time.monotonic() + delay)
            self.condition.notify_all()

    def success(self) -> None:
        with self.condition:
            self.failures = 0
            self.success_streak += 1
            if self.success_streak >= 8 and self.capacity < self.workers:
                self.capacity += 1
                self.success_streak = 0
            self.condition.notify_all()

    def stop(self) -> None:
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


class ShfeRankClient:
    """Use one HTTP session per thread and no initial inter-request delay."""

    def __init__(self, *, workers: int = 4, max_attempts: int = 5) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.gate = AdaptiveGate(workers)
        self.max_attempts = max_attempts
        self.local = threading.local()
        self.sessions: list[requests.Session] = []
        self.sessions_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self.local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.headers.update({
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": REFERER,
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
            })
            self.local.session = session
            with self.sessions_lock:
                self.sessions.append(session)
        return session

    def close(self) -> None:
        for session in self.sessions:
            session.close()

    def fetch(self, day: str) -> dict[str, Any]:
        url = _source_url(day)
        outcome: dict[str, Any] = {"tradingDay": day, "sourceUrl": url, "httpStatus": None,
                                   "contentType": None, "payloadSha256": None,
                                   "retrievedAt": _utc(), "attempts": 0}
        for attempt in range(1, self.max_attempts + 1):
            if not self.gate.acquire():
                return {**outcome, "status": "CANCELLED"}
            try:
                outcome["attempts"] = attempt
                try:
                    response = self._session().get(url, timeout=25, allow_redirects=False)
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == self.max_attempts:
                        return {**outcome, "status": "FAILED", "error": "transient network retries exhausted"}
                    self.gate.transient()
                    continue
                raw = bytes(response.content)
                status = int(response.status_code)
                content_type = str(response.headers.get("Content-Type") or "")
                outcome.update(httpStatus=status, contentType=content_type or None,
                               payloadSha256=sha256(raw).hexdigest(), retrievedAt=_utc(),
                               bytes=len(raw))
                if status in {403, 412}:
                    self.gate.stop()
                    return {**outcome, "status": "BLOCKED", "error": f"official HTTP {status}"}
                if status == 404:
                    return {**outcome, "status": "MISSING_REPORT", "error": "official HTTP 404"}
                if status == 429 or 500 <= status <= 599:
                    if attempt == self.max_attempts:
                        return {**outcome, "status": "FAILED", "error": f"official HTTP {status}; retries exhausted"}
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = min(60.0, max(0.0, float(retry_after)))
                    except ValueError:
                        delay = 0.0
                    self.gate.transient(delay)
                    continue
                if status != 200:
                    self.gate.stop()
                    return {**outcome, "status": "FAILED", "error": f"official HTTP {status}"}
                if "json" not in content_type.lower():
                    self.gate.stop()
                    return {**outcome, "status": "FAILED", "error": "official response is not JSON"}
                try:
                    summary = validate_shfe_rank(raw, day)
                except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
                    self.gate.stop()
                    return {**outcome, "status": "FAILED", "error": str(error)[:200],
                            "errorType": type(error).__name__}
                self.gate.success()
                return {**outcome, "status": "PASS_RAW", "summary": summary, "raw": raw}
            finally:
                self.gate.release()
        return {**outcome, "status": "FAILED", "error": "request retries exhausted"}


def capture_shfe_ranks(root: Path, *, start_day: str = "2023-01-01",
                       end_day: str | None = None, workers: int = 4,
                       max_attempts: int = 5, max_days: int | None = None,
                       dry_run: bool = False,
                       client: ShfeRankClient | None = None) -> dict[str, Any]:
    """Capture each verified trading day as source bytes plus SHA/date evidence."""

    root = Path(root)
    end_day = end_day or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if date.fromisoformat(start_day) > date.fromisoformat(end_day):
        raise ValueError("invalid SHFE ranking date range")
    if max_days is not None and max_days < 1:
        raise ValueError("max_days must be positive")
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified CN futures trading calendar is missing")
    days = calendar.between(start_day, end_day)
    if not days:
        raise ValueError("verified calendar has no trading days in range")
    existing = {day for day in days if _completed(root, day) is not None}
    pending = [day for day in days if day not in existing]
    selected = pending[:max_days] if max_days is not None else pending
    progress: dict[str, Any] = {
        "schema": SCHEMA, "publisher": ["SHFE", "INE"], "status": "DRY_RUN" if dry_run else "RUNNING",
        "startDay": start_day, "endDay": end_day, "calendarDays": len(days),
        "completedDays": len(existing), "pendingDays": len(pending), "newDaysThisRun": 0,
        "missingDays": 0, "workersRequested": workers,
        "authoritativeDataWritten": False, "updatedAt": _utc(),
    }
    if dry_run:
        progress["firstPendingDay"] = pending[0] if pending else None
        return progress
    state_dir = root / STATE
    bronze_dir = root / BRONZE
    active_client = client or ShfeRankClient(workers=workers, max_attempts=max_attempts)
    with _process_file_lock(state_dir / "capture.lock"):
        _write_json(state_dir / "progress.json", progress)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shfe-rank") as pool:
                iterator = iter(selected)
                futures: dict[Future[dict[str, Any]], str] = {}

                def submit_next() -> bool:
                    try:
                        day = next(iterator)
                    except StopIteration:
                        return False
                    futures[pool.submit(active_client.fetch, day)] = day
                    return True

                for _ in range(workers):
                    if not submit_next():
                        break
                halted = False
                while futures:
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        day = futures.pop(future)
                        try:
                            result = future.result()
                        except Exception as error:
                            active_client.gate.stop()
                            result = {"tradingDay": day, "sourceUrl": _source_url(day),
                                      "status": "FAILED", "httpStatus": None,
                                      "error": type(error).__name__, "attempts": 0}
                        raw = result.pop("raw", None)
                        status = result.get("status")
                        if status == "CANCELLED":
                            continue
                        record = {
                            "schema": SCHEMA, "publisher": ["SHFE", "INE"], "adapter": ADAPTER,
                            "qualityStatus": "PASS_RAW" if status == "PASS_RAW" else "UNKNOWN",
                            "authoritativeDataWritten": False, **result,
                        }
                        if status == "PASS_RAW":
                            raw_path = bronze_dir / f"{day}.dat"
                            record["rawPath"] = raw_path.relative_to(root).as_posix()
                            _write_bytes(raw_path, raw)
                            _write_json(bronze_dir / f"{day}.json", record)
                            progress["completedDays"] += 1
                            progress["newDaysThisRun"] += 1
                        elif status == "MISSING_REPORT":
                            progress["missingDays"] += 1
                        else:
                            record["qualityStatus"] = "FAILED"
                            halted = True
                            active_client.gate.stop()
                            progress.update(errorDay=day, errorType=record.get("errorType") or status,
                                            httpStatus=record.get("httpStatus"))
                        _write_json(state_dir / f"{day}.json", record)
                        progress.update(currentDay=day, pendingDays=len(days) - progress["completedDays"],
                                        updatedAt=_utc())
                        _write_json(state_dir / "progress.json", progress)
                        if not halted:
                            submit_next()
                    if halted:
                        # The executor finishes only already-started requests; no new dates are queued.
                        continue
            if halted:
                progress["status"] = "FAILED"
            elif progress["completedDays"] == len(days):
                progress["status"] = "PASS_RAW"
            elif progress["missingDays"]:
                progress["status"] = "PARTIAL_RAW"
            else:
                progress["status"] = "BATCH_LIMIT"
        finally:
            active_client.close()
            progress["updatedAt"] = _utc()
            _write_json(state_dir / "progress.json", progress)
    return progress


__all__ = ("AdaptiveGate", "ShfeRankClient", "capture_shfe_ranks", "validate_shfe_rank")
