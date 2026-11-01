"""Rate-limited, resumable collection of official futures daily reports."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from io import BytesIO
import zipfile

import requests

from .futures_calendar import load_futures_trading_calendar
from .atomic_files import replace_with_retry
from .futures_official_daily_sources import (
    PARSER_VERSION,
    official_daily_request,
    parse_official_daily,
)
from .storage import MarketStore


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")
MAX_REQUESTS_PER_SECOND = 5
MIN_REQUEST_INTERVAL_SECONDS = 1 / MAX_REQUESTS_PER_SECOND
DEFAULT_MIN_INTERVAL_SECONDS = MIN_REQUEST_INTERVAL_SECONDS
MAX_DAILY_REQUEST_BUDGET = 30_000
DEFAULT_DAILY_REQUEST_BUDGET = MAX_DAILY_REQUEST_BUDGET
DEFAULT_DAILY_HOST_BUDGET = DEFAULT_DAILY_REQUEST_BUDGET
_GLOBAL_BUDGET_KEY = "__GLOBAL__"
_BUDGET_PATH = Path("state") / "official_futures_request_budget.json"


class RequestBudgetExhausted(RuntimeError):
    """The daily host request cap was reached; a later invocation can resume."""


class HostPaused(RuntimeError):
    """The official host rejected the request and is paused for this run."""


def effective_request_policy(min_interval_seconds: float, daily_budget: int) -> tuple[float, int]:
    """Apply the user-approved global ceiling to CLI and programmatic callers."""

    if min_interval_seconds < 0 or daily_budget < 1:
        raise ValueError("rate interval must be non-negative and daily budget positive")
    if daily_budget > MAX_DAILY_REQUEST_BUDGET:
        raise ValueError(f"all official hosts share a maximum daily request budget of {MAX_DAILY_REQUEST_BUDGET}")
    return max(min_interval_seconds, MIN_REQUEST_INTERVAL_SECONDS), daily_budget


@contextmanager
def _process_file_lock(path: Path) -> Iterator[None]:
    """Hold an OS file lock across processes while budget state is read or written."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class HostRateLimiter:
    """Reserve globally spaced request starts without serializing HTTP responses."""

    def __init__(
        self,
        data_root: Path,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        daily_budget: int = DEFAULT_DAILY_HOST_BUDGET,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        min_interval_seconds, daily_budget = effective_request_policy(min_interval_seconds, daily_budget)
        self.path = Path(data_root) / _BUDGET_PATH
        self.min_interval_seconds = min_interval_seconds
        self.daily_budget = daily_budget
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(BEIJING))
        self.ledger = self._read()
        self._active_host: str | None = None

    def acquire(self, host: str) -> None:
        if self._active_host is not None:
            raise RuntimeError("a host request lease is already active")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            with _process_file_lock(self.path.with_suffix(".lock")):
                self.ledger = self._read()
                today_now = self.now().astimezone(BEIJING)
                today = today_now.date().isoformat()
                bucket = self.ledger.setdefault(host, {"day": today, "count": 0, "lastRequestAt": None})
                global_bucket = self.ledger.get(_GLOBAL_BUDGET_KEY)
                if not isinstance(global_bucket, dict) or global_bucket.get("day") != today:
                    previous_host_counts = [
                        item for name, item in self.ledger.items()
                        if name != _GLOBAL_BUDGET_KEY and item.get("day") == today
                    ]
                    prior_timestamps: list[datetime] = []
                    for item in previous_host_counts:
                        last_request = item.get("lastRequestAt")
                        if isinstance(last_request, str):
                            try:
                                prior_timestamps.append(datetime.fromisoformat(last_request))
                            except ValueError:
                                pass
                    global_bucket = {
                        "day": today,
                        "count": sum(int(item.get("count") or 0) for item in previous_host_counts),
                        "lastRequestAt": max(prior_timestamps).isoformat(timespec="microseconds") if prior_timestamps else None,
                    }
                    self.ledger[_GLOBAL_BUDGET_KEY] = global_bucket
                paused_until = bucket.get("pausedUntil")
                if isinstance(paused_until, str):
                    try:
                        paused_time = datetime.fromisoformat(paused_until)
                    except ValueError:
                        bucket.pop("pausedUntil", None)
                    else:
                        if today_now < paused_time:
                            raise RequestBudgetExhausted(f"{host} 根据 Retry-After 暂停至 {paused_until}")
                if bucket.get("day") != today:
                    bucket.update({"day": today, "count": 0, "lastRequestAt": None})
                count = int(bucket.get("count") or 0)
                if count >= self.daily_budget:
                    raise RequestBudgetExhausted(f"{host} 已达到每日请求上限 {self.daily_budget}")
                global_count = int(global_bucket.get("count") or 0)
                if global_count >= self.daily_budget:
                    raise RequestBudgetExhausted(f"所有官网主机已达到每日总请求上限 {self.daily_budget}")
                remaining = 0.0
                for previous, interval in (
                    (global_bucket.get("lastRequestAt"), MIN_REQUEST_INTERVAL_SECONDS),
                    (bucket.get("lastRequestAt"), self.min_interval_seconds),
                ):
                    if not isinstance(previous, str):
                        continue
                    try:
                        elapsed = (today_now - datetime.fromisoformat(previous)).total_seconds()
                    except ValueError:
                        continue
                    remaining = max(remaining, interval - elapsed)
                if remaining <= 0:
                    bucket["count"] = count + 1
                    requested_at = today_now.isoformat(timespec="microseconds")
                    bucket["lastRequestAt"] = requested_at
                    global_bucket["count"] = global_count + 1
                    global_bucket["lastRequestAt"] = requested_at
                    self._write()
                    self._active_host = host
                    return
            self.sleep(remaining)

    def complete(self, host: str, *, retry_after_seconds: float = 0.0) -> None:
        if self._active_host != host:
            raise RuntimeError("no matching official host request lease is active")
        try:
            if retry_after_seconds > 0:
                with _process_file_lock(self.path.with_suffix(".lock")):
                    self.ledger = self._read()
                    now = self.now().astimezone(BEIJING)
                    bucket = self.ledger[host]
                    paused_until = now + timedelta(seconds=retry_after_seconds)
                    previous = bucket.get("pausedUntil")
                    if isinstance(previous, str):
                        try:
                            paused_until = max(paused_until, datetime.fromisoformat(previous))
                        except ValueError:
                            pass
                    bucket["pausedUntil"] = paused_until.isoformat(timespec="seconds")
                    self._write()
        finally:
            self._active_host = None

    def defer(self, host: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with _process_file_lock(self.path.with_suffix(".lock")):
            self.ledger = self._read()
            bucket = self.ledger.setdefault(
                host, {"day": self.now().astimezone(BEIJING).date().isoformat(), "count": 0}
            )
            bucket["pausedUntil"] = (
                self.now().astimezone(BEIJING) + timedelta(seconds=seconds)
            ).isoformat(timespec="seconds")
            self._write()

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"invalid official request budget file: {self.path}") from error
        if not isinstance(payload, dict) or payload.get("schema") != "official-futures-request-budget/v1":
            raise ValueError("unsupported official request budget schema")
        hosts = payload.get("hosts")
        if not isinstance(hosts, dict):
            raise ValueError("invalid official request budget hosts")
        return {str(host): dict(item) for host, item in hosts.items() if isinstance(item, Mapping)}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "official-futures-request-budget/v1", "hosts": self.ledger}
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix=".official-budget-",
                suffix=".tmp", dir=self.path.parent, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


class GovernedRequestsSession:
    """requests.Session façade that applies the same host budget to rank APIs."""

    def __init__(
        self,
        data_root: Path,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        daily_budget: int = DEFAULT_DAILY_HOST_BUDGET,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.trust_env = False
        self.limiter = HostRateLimiter(
            data_root, min_interval_seconds=min_interval_seconds,
            daily_budget=daily_budget, sleep=sleep,
        )

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        host = urlparse(url).hostname or "unknown"
        for attempt in range(2):
            self.limiter.acquire(host)
            retry_after = 0.0
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            finally:
                self.limiter.complete(host, retry_after_seconds=retry_after)
            if not (500 <= response.status_code < 600 and attempt == 0):
                return response
        return response

    def close(self) -> None:
        self.session.close()


def default_backfill_range(data_root: Path, *, today: date | None = None) -> tuple[str, str]:
    """Return the last completed trading date and the prior 365-day window."""

    calendar = load_futures_trading_calendar(Path(data_root))
    if calendar is None:
        raise ValueError("缺少期货交易日历；请先运行 futures-calendar-sync，不能用工作日猜交易日")
    if today is not None:
        cutoff = today - timedelta(days=1)
    else:
        now = datetime.now(BEIJING)
        cutoff = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    completed = [value for value in calendar.trading_days if date.fromisoformat(value) <= cutoff]
    if not completed:
        raise ValueError("期货交易日历中没有最近一个已完成交易日")
    end = completed[-1]
    start = (date.fromisoformat(end) - timedelta(days=365)).isoformat()
    return start, end


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return min(max(float(value.strip()), 0.0), 86_400.0)
    except ValueError:
        try:
            parsed = datetime.strptime(value.strip(), "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
            return min(max((parsed - datetime.now(timezone.utc)).total_seconds(), 0.0), 86_400.0)
        except ValueError:
            return 0.0


def _looks_like_challenge(raw: bytes, content_type: str) -> bool:
    prefix = raw.lstrip()[:80].lower()
    return "text/html" in content_type or prefix.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body")
    )


def _find_cffex_archive(data_root: Path, trading_day: str) -> tuple[bytes, Path, str] | None:
    directory = Path(data_root) / "bronze" / "official_futures_cffex_archives"
    compact_day = trading_day.replace("-", "")
    entry = f"{compact_day}_1.csv"
    for path in sorted(directory.glob(f"{compact_day[:6]}-*.zip"), reverse=True):
        try:
            raw = path.read_bytes()
            if not zipfile.is_zipfile(BytesIO(raw)):
                continue
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                if entry in archive.namelist():
                    manifest = path.with_suffix(".json")
                    try:
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        retrieved_at = str(payload.get("retrievedAt") or "")
                        datetime.fromisoformat(retrieved_at)
                        expected_url = official_daily_request("CFFEX", trading_day)["url"]
                        if payload.get("sourceUrl") != expected_url:
                            continue
                        if payload.get("payloadSha256") != hashlib.sha256(raw).hexdigest():
                            continue
                    except (OSError, ValueError, TypeError, KeyError):
                        continue
                    return raw, path, retrieved_at
        except (OSError, zipfile.BadZipFile):
            continue
    return None


def _daily_bronze_index(root: Path) -> dict[tuple[str, str], tuple[bytes, str]]:
    result: dict[tuple[str, str], tuple[bytes, str]] = {}
    for exchange in DEFAULT_EXCHANGES:
        if exchange == "CFFEX":
            continue
        directory = root / "bronze" / f"official_futures_daily_{exchange.lower()}"
        for path in directory.glob("*.json"):
            try:
                proof = json.loads(path.read_text(encoding="utf-8"))
                day = str(proof["tradingDay"])
                date.fromisoformat(day)
                raw = base64.b64decode(proof["contentBase64"], validate=True)
                if hashlib.sha256(raw).hexdigest() != proof["payloadSha256"]:
                    continue
                saved_url = urlparse(str(proof["sourceUrl"]))
                expected_url = urlparse(str(official_daily_request(exchange, day)["url"]))
                if (
                    saved_url.scheme not in {"http", "https"}
                    or (saved_url.netloc, saved_url.path, saved_url.query)
                    != (expected_url.netloc, expected_url.path, expected_url.query)
                ):
                    continue
                timestamp = str(proof["retrievedAt"])
                datetime.fromisoformat(timestamp)
                key = day, exchange
                if key not in result or timestamp > result[key][1]:
                    result[key] = raw, timestamp
            except (KeyError, TypeError, ValueError, OSError):
                continue
    return result


def _store_cffex_archive(
    data_root: Path, month: str, raw: bytes, source_url: str, retrieved_at: str, digest: str,
) -> Path:
    directory = Path(data_root) / "bronze" / "official_futures_cffex_archives"
    directory.mkdir(parents=True, exist_ok=True)
    archive_path = directory / f"{month}-{digest[:12]}.zip"
    if not archive_path.exists():
        _atomic_write_bytes(archive_path, raw)
        _atomic_write_json_file(archive_path.with_suffix(".json"), {
            "publisher": "CFFEX", "sourceUrl": source_url,
            "retrievedAt": retrieved_at, "payloadSha256": digest,
            "contentType": "application/zip",
        })
    return archive_path


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{path.name}.",
            suffix=".tmp", dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _persist_deferred_daily(
    store: MarketStore, *, trading_day: str, exchange: str,
    request_spec: Mapping[str, Any], reason: str, status: str = "DEFERRED",
) -> None:
    """Record a no-network pause so bounded reruns can advance past it."""

    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    adapter = f"official-{exchange.lower()}-daily-v2"
    run_id = store.begin_run(f"OFFICIAL_FUTURES_DAILY_{exchange}")
    store.record_futures_official_daily([], {
        "trade_date": trading_day, "exchange": exchange, "status": status,
        "record_count": 0, "usable_count": 0, "adapter": adapter,
        "source_url": str(request_spec["url"]), "payload_sha256": None,
        "error": reason[:500], "retrieved_at": retrieved_at,
    })
    store.finish_run(run_id, status, reason[:500])


def run_official_futures_backfill(
    data_root: Path,
    *,
    start_day: str,
    end_day: str,
    exchanges: Sequence[str] = DEFAULT_EXCHANGES,
    max_days: int | None = None,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    daily_host_budget: int = DEFAULT_DAILY_HOST_BUDGET,
    dry_run: bool = False,
    retry_failed: bool = False,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch only calendar trading dates; successful source-days are resumable."""

    min_interval_seconds, daily_host_budget = effective_request_policy(min_interval_seconds, daily_host_budget)
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    if start > end:
        raise ValueError("start_day must not be after end_day")
    selected = tuple(dict.fromkeys(str(exchange).upper() for exchange in exchanges))
    if not selected or any(exchange not in DEFAULT_EXCHANGES for exchange in selected):
        raise ValueError("exchanges must be a non-empty subset of the six domestic futures exchanges")
    calendar = load_futures_trading_calendar(Path(data_root))
    if calendar is None:
        raise ValueError("缺少期货交易日历；请先运行 futures-calendar-sync，不能用工作日猜交易日")
    calendar_days = calendar.between(start.isoformat(), end.isoformat())
    if max_days is not None:
        if max_days < 1:
            raise ValueError("max_days must be positive")
    if dry_run:
        days = calendar_days[:max_days] if max_days is not None else calendar_days
        return {
            "status": "DRY_RUN", "startDay": start.isoformat(), "endDay": end.isoformat(),
            "calendarTradingDays": len(calendar_days), "tradingDayCount": len(days), "exchanges": list(selected),
            "requests": [
                {"exchange": exchange, "tradingDay": day,
                 **{key: value for key, value in official_daily_request(exchange, day).items() if key != "json"}}
                for day in days for exchange in selected
            ],
            "rateLimit": {
                "scope": "global",
                "minIntervalSeconds": min_interval_seconds,
                "maxRequestsPerSecond": round(1 / min_interval_seconds, 3) if min_interval_seconds > 0 else None,
                "maxRequestsPerDay": daily_host_budget,
            },
        }

    root = Path(data_root)
    http = session or requests.Session()
    http.trust_env = False
    limiter = HostRateLimiter(
        root, min_interval_seconds=min_interval_seconds, daily_budget=daily_host_budget, sleep=sleep,
    )
    store = MarketStore(root)
    host_paused: set[str] = set()
    host_failed_paused: set[str] = set()
    outcomes: list[dict[str, Any]] = []
    cffex_archive_cache: dict[str, tuple[bytes, Path, str]] = {}
    raw_cache = _daily_bronze_index(root)
    try:
        store.register_default_datasets()
        cached_status = {
            (str(row[0]), str(row[1])): (str(row[2]), str(row[3] or ""), str(row[4] or ""), str(row[5] or ""))
            for row in store.connection.execute(
                "SELECT trade_date, exchange, status, retrieved_at, error, parser_version FROM futures_contract_daily_official_coverage "
                "WHERE adapter LIKE '%-daily-v2'",
            ).fetchall()
        }
        verified_fact_days = {
            (str(row[0]), str(row[1]))
            for row in store.connection.execute(
                "SELECT f.trade_date, f.exchange FROM futures_contract_daily_official f "
                "JOIN futures_contract_daily_official_coverage c ON c.trade_date=f.trade_date "
                "AND c.exchange=f.exchange AND c.adapter=f.adapter "
                "WHERE c.adapter LIKE '%-daily-v2' AND c.status='PASS' AND c.parser_version=? "
                "GROUP BY f.trade_date, f.exchange, c.usable_count "
                "HAVING COUNT(*) > 0 AND COUNT(*)=c.usable_count "
                "AND COUNT(*) = COUNT(CASE WHEN f.parser_version=? "
                "AND f.quality_status='PASS' THEN 1 END)",
                [PARSER_VERSION, PARSER_VERSION],
            ).fetchall()
        }

        def blocked_failure(message: str) -> bool:
            text = message.lower()
            return any(marker in text for marker in ("403", "412", "429", "text/html", "html", "verification", "校验", "验证页"))

        if not retry_failed:
            for (day, exchange), (status, _, error, _version) in cached_status.items():
                if status != "FAILED" or not blocked_failure(error) or exchange not in selected:
                    continue
                host = urlparse(str(official_daily_request(exchange, day)["url"])).hostname or exchange.lower()
                host_paused.add(host)
                host_failed_paused.add(host)
        def already_attempted(day: str, exchange: str) -> bool:
            previous = cached_status.get((day, exchange))
            if previous is None:
                return False
            previous_status = previous[0]
            if previous_status == "PASS":
                return previous[3] == PARSER_VERSION and (day, exchange) in verified_fact_days
            available = raw_cache.get((day, exchange))
            if available is None and exchange == "CFFEX":
                archive = _find_cffex_archive(root, day)
                if archive is not None:
                    available = archive[0], archive[2]
            if available is not None:
                try:
                    parse_official_daily(
                        available[0], exchange=exchange, trading_day=day,
                        source_url=str(official_daily_request(exchange, day)["url"]),
                        retrieved_at=available[1],
                    )
                except ValueError:
                    pass
                else:
                    return False
            return previous_status in {"FAILED", "NO_PUBLICATION", "UNSUPPORTED"} and not retry_failed

        eligible_days = [
            day for day in calendar_days
            if any(not already_attempted(day, exchange) for exchange in selected)
        ]
        days = eligible_days[:max_days] if max_days is not None else eligible_days
        for day in days:
            for exchange in selected:
                previous = cached_status.get((day, exchange))
                if already_attempted(day, exchange):
                    cached_result = "CACHED" if previous and previous[0] == "PASS" else "CACHED_FAILURE"
                    outcomes.append({"exchange": exchange, "tradingDay": day, "status": cached_result})
                    continue
                request_spec = official_daily_request(exchange, day)
                host = urlparse(str(request_spec["url"])).hostname or exchange.lower()
                cached_raw = raw_cache.get((day, exchange))
                if cached_raw is not None:
                    try:
                        parse_official_daily(
                            cached_raw[0], exchange=exchange, trading_day=day,
                            source_url=str(request_spec["url"]), retrieved_at=cached_raw[1],
                        )
                    except ValueError:
                        # Keep the original audit file but allow this source-day
                        # to recover from a wrong date or obsolete raw format.
                        raw_cache.pop((day, exchange), None)
                        cached_raw = None
                cffex_month = day.replace("-", "")[:6]
                cached_archive = (
                    cffex_archive_cache.get(cffex_month) or _find_cffex_archive(root, day)
                    if exchange == "CFFEX" else None
                )
                if cached_archive is not None:
                    try:
                        with zipfile.ZipFile(BytesIO(cached_archive[0])) as archive:
                            if f"{day.replace('-', '')}_1.csv" not in archive.namelist():
                                cached_archive = _find_cffex_archive(root, day)
                        if cached_archive is not None:
                            parse_official_daily(
                                cached_archive[0], exchange=exchange, trading_day=day,
                                source_url=str(request_spec["url"]), retrieved_at=cached_archive[2],
                            )
                    except (ValueError, zipfile.BadZipFile):
                        cffex_archive_cache.pop(cffex_month, None)
                        cached_archive = None
                if host in host_paused and cached_raw is None and cached_archive is None:
                    reason = f"{host} 本轮已暂停，等待后续续跑"
                    pause_status = "FAILED" if host in host_failed_paused else "DEFERRED"
                    _persist_deferred_daily(
                        store, trading_day=day, exchange=exchange,
                        request_spec=request_spec, reason=reason, status=pause_status,
                    )
                    cached_status[(day, exchange)] = (pause_status, datetime.now(BEIJING).isoformat(), reason, "")
                    outcomes.append({"exchange": exchange, "tradingDay": day, "status": pause_status, "error": reason})
                    continue
                run_id = store.begin_run(f"OFFICIAL_FUTURES_DAILY_{exchange}")
                retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                digest: str | None = None
                rows: list[dict[str, Any]] = []
                status = "FAILED"
                error: str | None = None
                response_status: int | None = None
                archive_path: Path | None = None
                archive_reused = False
                try:
                    source_url = str(request_spec["url"])
                    if cached_raw is not None:
                        raw, retrieved_at = cached_raw
                        archive_reused = True
                        response_status = 200
                        content_type = "application/json" if exchange != "CZCE" else "text/plain"
                    elif cached_archive is not None:
                        raw, archive_path, retrieved_at = cached_archive
                        cffex_archive_cache[cffex_month] = (raw, archive_path, retrieved_at)
                        archive_reused = True
                        response_status = 200
                        content_type = "application/zip"
                    else:
                        for request_attempt in range(2):
                            limiter.acquire(host)
                            retry_after = 0.0
                            try:
                                response = http.request(
                                    str(request_spec["method"]), str(request_spec["url"]),
                                    data=request_spec.get("data"), json=request_spec.get("json"),
                                    headers=request_spec.get("headers"), timeout=30,
                                )
                                if int(response.status_code) in {429, 567}:
                                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                                    if int(response.status_code) == 567:
                                        retry_after = max(retry_after, 60.0)
                            finally:
                                limiter.complete(host, retry_after_seconds=retry_after)
                            retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            if int(response.status_code) == 567 or not (
                                500 <= int(response.status_code) < 600 and request_attempt == 0
                            ):
                                break
                        response_status = int(response.status_code)
                        if response_status in {403, 412, 429, 567}:
                            host_paused.add(host)
                            if response_status in {403, 412}:
                                host_failed_paused.add(host)
                            raise HostPaused(f"HTTP {response_status}; 暂停本次运行对 {host} 的采集")
                        response.raise_for_status()
                        raw = bytes(response.content)
                        content_type = str(response.headers.get("Content-Type") or "").lower()
                        if _looks_like_challenge(raw, content_type):
                            host_paused.add(host)
                            host_failed_paused.add(host)
                            raise ValueError("官网返回 HTML 或验证页；已跳过原始页面存档")
                    digest = hashlib.sha256(raw).hexdigest()
                    if exchange == "CFFEX" and not archive_reused:
                        archive_path = _store_cffex_archive(root, cffex_month, raw, source_url, retrieved_at, digest)
                        cffex_archive_cache[cffex_month] = (raw, archive_path, retrieved_at)
                    if raw and not archive_reused:
                        archived_response: dict[str, Any] = {
                                "publisher": exchange, "sourceUrl": source_url,
                                "adapter": f"official-{exchange.lower()}-daily-v2",
                                "tradingDay": day, "retrievedAt": retrieved_at,
                                "payloadSha256": digest, "contentType": content_type or None,
                        }
                        if exchange == "CFFEX":
                            archived_response["archivePath"] = archive_path.relative_to(root).as_posix() if archive_path else None
                            archived_response["archiveReused"] = archive_reused
                        else:
                            archived_response["contentBase64"] = base64.b64encode(raw).decode("ascii")
                        store.write_bronze(
                            run_id, f"official_futures_daily_{exchange.lower()}", archived_response,
                        )
                    rows = parse_official_daily(
                        raw, exchange=exchange, trading_day=day,
                        source_url=source_url, retrieved_at=retrieved_at,
                    )
                    status = "PASS"
                except RequestBudgetExhausted as budget_error:
                    error = str(budget_error)
                    status = "PAUSED_RATE_LIMIT"
                    host_paused.add(host)
                except Exception as failure:
                    error = f"{type(failure).__name__}: {failure}"[:500]
                    if isinstance(failure, requests.HTTPError) and response_status == 404:
                        error = "MISSING_REPORT: HTTP 404 on a verified trading day"
                    elif isinstance(failure, HostPaused):
                        host_paused.add(host)
                        status = "PAUSED_HOST"
                    # Challenge bodies are intentionally neither archived nor parsed.
                coverage = {
                    "trade_date": day, "exchange": exchange,
                    "status": "DEFERRED" if status in {"PAUSED_RATE_LIMIT", "PAUSED_HOST"} and response_status is None
                    else "DEFERRED" if status == "PAUSED_HOST" and response_status in {429, 567}
                    else "FAILED" if status == "PAUSED_HOST" else status,
                    "record_count": len(rows), "usable_count": len(rows),
                    "adapter": f"official-{exchange.lower()}-daily-v2",
                    "source_url": str(request_spec["url"]), "payload_sha256": digest,
                    "error": error, "retrieved_at": retrieved_at,
                    "parser_version": PARSER_VERSION if status == "PASS" else None,
                }
                store.record_futures_official_daily(rows, coverage)
                store.finish_run(
                    run_id,
                    "PASS" if status == "PASS" else "DEFERRED" if coverage["status"] == "DEFERRED" else "PARTIAL_FAILURE",
                    error,
                )
                cached_status[(day, exchange)] = (str(coverage["status"]), retrieved_at, str(error or ""), str(coverage.get("parser_version") or ""))
                if status == "PASS":
                    verified_fact_days.add((day, exchange))
                outcomes.append({
                    "exchange": exchange, "tradingDay": day, "status": status,
                    "recordCount": len(rows), "httpStatus": response_status, "error": error,
                })
    finally:
        store.close()
        if session is None:
            http.close()
    statuses = {item["status"] for item in outcomes}
    overall = "PASS" if statuses <= {"PASS", "CACHED"} else "PARTIAL"
    return {
        "status": overall, "startDay": start.isoformat(), "endDay": end.isoformat(),
        "calendarTradingDays": len(calendar_days), "selectedTradingDays": len(days), "attempts": len(outcomes),
        "passed": sum(item["status"] in {"PASS", "CACHED"} for item in outcomes),
        "outcomes": outcomes,
        "rateLimit": {
            "scope": "global",
            "minIntervalSeconds": min_interval_seconds,
            "maxRequestsPerSecond": round(1 / min_interval_seconds, 3) if min_interval_seconds > 0 else None,
            "maxRequestsPerDay": daily_host_budget,
        },
        "pausedHosts": sorted(host_paused),
    }


__all__ = (
    "DEFAULT_DAILY_HOST_BUDGET", "DEFAULT_DAILY_REQUEST_BUDGET", "DEFAULT_EXCHANGES", "DEFAULT_MIN_INTERVAL_SECONDS",
    "MAX_DAILY_REQUEST_BUDGET", "MAX_REQUESTS_PER_SECOND", "MIN_REQUEST_INTERVAL_SECONDS",
    "effective_request_policy",
    "GovernedRequestsSession", "HostRateLimiter", "RequestBudgetExhausted", "default_backfill_range",
    "run_official_futures_backfill",
)
