"""Parallel official daily Bronze capture, followed by one serial Catalog importer."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import threading
import time
from zipfile import ZipFile, is_zipfile

import requests

from capture_dce_android_batch_ranks import BrowserPage, _discover_page, _remove_forward
from market_monitor.atomic_files import replace_with_retry
from market_monitor.futures_calendar import load_futures_trading_calendar
from market_monitor.futures_official_backfill import (
    DEFAULT_EXCHANGES, HostRateLimiter, HostPaused, _daily_bronze_index,
    _process_file_lock, _retry_after_seconds, _store_cffex_archive, run_official_futures_backfill,
)
from market_monitor.futures_official_daily_sources import PARSER_VERSION, official_daily_request, parse_official_daily


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, path)


class NoNetwork:
    trust_env = False

    def request(self, *args, **kwargs):
        raise HostPaused("本轮原件缺失；只导入已核验的官网原件")


def run(root: Path, *, start_day: str, end_day: str, write: bool = False, exchanges: tuple = DEFAULT_EXCHANGES) -> dict:
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified calendar is missing")
    days = calendar.between(start_day, end_day)
    raw_cache = _daily_bronze_index(root)
    cffex_cache: dict[str, tuple[bytes, str]] = {}
    for path in sorted((root / "bronze" / "official_futures_cffex_archives").glob("*.zip")):
        try:
            raw = path.read_bytes()
            with ZipFile(BytesIO(raw)) as archive:
                for name in archive.namelist():
                    if len(name) == 14 and name.endswith("_1.csv"):
                        compact = name[:8]
                        cffex_cache[f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"] = raw, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except (ValueError, OSError):
            continue
    state = root / "state" / "official_daily_history_capture"
    if len(exchanges) == 1:
        state = state / exchanges[0].lower()
    lock = threading.RLock()
    paused: dict[str, str] = {}
    local = threading.local()
    progress = {"status": "RUNNING", "parserVersion": PARSER_VERSION, "startDay": start_day,
                "endDay": end_day, "days": len(days), "tasks": len(days) * len(exchanges),
                "verified": 0, "newResponses": 0, "failed": 0, "unavailable": 0, "lastErrors": {},
                "authoritativeDataWritten": False}

    def record(exchange: str, day: str, status: str, error: str | None = None, new: bool = False):
        with lock:
            progress["verified" if status == "PASS_RAW" else "unavailable" if status == "NO_PUBLICATION" else "failed"] += 1
            progress["newResponses"] += int(new)
            progress.update(currentDay=day, currentExchange=exchange, updatedAtUtc=datetime.now(timezone.utc).isoformat())
            if error:
                progress["lastErrors"][exchange] = error
            write_json(state / "progress.json", progress)
            count = progress["verified"] + progress["failed"] + progress["unavailable"]
            if count % 25 == 0:
                print(json.dumps({k: progress[k] for k in ("verified", "newResponses", "failed", "unavailable", "tasks")}), flush=True)

    def save(exchange: str, day: str, raw: bytes, timestamp: str, content_type: str):
        spec = official_daily_request(exchange, day)
        rows = parse_official_daily(raw, exchange=exchange, trading_day=day, source_url=spec["url"], retrieved_at=timestamp)
        digest = sha256(raw).hexdigest()
        if exchange == "CFFEX":
            _store_cffex_archive(root, day.replace("-", "")[:6], raw, spec["url"], timestamp, digest)
        else:
            write_json(root / "bronze" / f"official_futures_daily_{exchange.lower()}" / f"{day}-{digest[:12]}.json", {
                "publisher": exchange, "sourceUrl": spec["url"], "tradingDay": day,
                "retrievedAt": timestamp, "payloadSha256": digest, "contentType": content_type,
                "contentBase64": base64.b64encode(raw).decode("ascii"), "parserVersion": PARSER_VERSION,
                "qualityStatus": "PASS_RAW", "contractCount": len(rows),
            })

    def cached(exchange: str, day: str) -> bool:
        found = cffex_cache.get(day) if exchange == "CFFEX" else raw_cache.get((day, exchange))
        if found:
            try:
                raw, timestamp = found
                parse_official_daily(raw, exchange=exchange, trading_day=day,
                                     source_url=official_daily_request(exchange, day)["url"], retrieved_at=timestamp)
                record(exchange, day, "PASS_RAW")
                return True
            except ValueError:
                pass
        return False

    def fetch(exchange: str, day: str):
        if cached(exchange, day):
            return
        if exchange in paused:
            record(exchange, day, "FAILED", paused[exchange])
            return
        if not hasattr(local, "session"):
            local.session = requests.Session()
            local.session.trust_env = False
        session = local.session
        spec = official_daily_request(exchange, day)
        limiter = HostRateLimiter(root, min_interval_seconds=2 if exchange == "GFEX" else 1 if exchange == "CZCE" else .2)
        host = spec["url"].split("/")[2]
        error = "unknown source failure"
        for attempt in range(3):
            try:
                limiter.acquire(host)
                cooldown = 0
                try:
                    response = session.request(spec["method"], spec["url"], data=spec.get("data"),
                                               json=spec.get("json"), headers=spec.get("headers"), timeout=25, allow_redirects=False)
                    if response.status_code in {429, 567}:
                        cooldown = max(60, _retry_after_seconds(response.headers.get("Retry-After")))
                finally:
                    limiter.complete(host, retry_after_seconds=cooldown)
                if response.status_code == 404:
                    record(exchange, day, "FAILED", "MISSING_REPORT: HTTP 404 on a verified trading day")
                    return
                if 300 <= response.status_code < 400:
                    record(exchange, day, "FAILED", "UNRESOLVED_OFFICIAL_REDIRECT")
                    return
                if response.status_code in {403, 412, 429, 567}:
                    error = f"official HTTP {response.status_code}"
                    paused[exchange] = error
                    break
                response.raise_for_status()
                raw = bytes(response.content)
                content_type = str(response.headers.get("Content-Type") or "")
                if "html" in content_type.lower():
                    error = "source returned HTML; body not archived"
                    paused[exchange] = error
                    break
                timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                save(exchange, day, raw, timestamp, content_type)
                record(exchange, day, "PASS_RAW", new=True)
                return
            except (requests.RequestException, ValueError, RuntimeError) as failure:
                error = type(failure).__name__ + ": " + str(failure).split(" for url:")[0][:120]
                if attempt < 2:
                    time.sleep(2 ** attempt)
        record(exchange, day, "FAILED", error)

    def cffex():
        monthly: dict[str, bytes] = {}
        session = requests.Session()
        session.trust_env = False
        limiter = HostRateLimiter(root)
        host = official_daily_request("CFFEX", days[0])["url"].split("/")[2] if days else "www.cffex.com.cn"
        try:
            for day in days:
                if cached("CFFEX", day):
                    continue
                if "CFFEX" in paused:
                    record("CFFEX", day, "FAILED", paused["CFFEX"])
                    continue
                month = day.replace("-", "")[:6]
                try:
                    if month not in monthly:
                        limiter.acquire(host)
                        cooldown = 0.0
                        try:
                            response = session.get(official_daily_request("CFFEX", day)["url"], timeout=30)
                            if response.status_code in {429, 567}:
                                cooldown = max(60, _retry_after_seconds(response.headers.get("Retry-After")))
                        finally:
                            limiter.complete(host, retry_after_seconds=cooldown)
                        if response.status_code in {403, 412, 429, 567}:
                            paused["CFFEX"] = f"official HTTP {response.status_code}"
                        response.raise_for_status()
                        if not is_zipfile(BytesIO(response.content)):
                            raise ValueError("CFFEX response is not ZIP")
                        monthly[month] = bytes(response.content)
                    save("CFFEX", day, monthly[month], datetime.now(timezone.utc).isoformat(), "application/zip")
                    record("CFFEX", day, "PASS_RAW", new=True)
                except (requests.RequestException, ValueError, RuntimeError) as error:
                    record("CFFEX", day, "FAILED", type(error).__name__)
        finally:
            session.close()

    def dce():
        pending = [day for day in days if not cached("DCE", day)]
        if not pending:
            return
        adb = Path(r"C:\leidian\LDPlayer14\adb.exe")
        port = None
        page = None
        try:
            with _process_file_lock(root / "state" / "dce_android_batch_capture" / "capture.lock"):
                port, target = _discover_page(adb, 5038, "127.0.0.1:5555")
                page = BrowserPage(target)
                page.preflight()
                for i, day in enumerate(pending):
                    raw, content_type = page.capture_daily_quotes(day.replace("-", ""))
                    save("DCE", day, raw, datetime.now(timezone.utc).isoformat(), content_type)
                    record("DCE", day, "PASS_RAW", new=True)
                    time.sleep(1)
        except Exception as error:
            for day in pending[i if 'i' in locals() else 0:]:
                record("DCE", day, "FAILED", getattr(error, "code", type(error).__name__))
        finally:
            if page:
                page.close()
            if port is not None:
                _remove_forward(adb, 5038, "127.0.0.1:5555", port)

    with _process_file_lock(state / "capture.lock"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            if "DCE" in exchanges:
                futures.append(pool.submit(dce))
            if "CFFEX" in exchanges:
                futures.append(pool.submit(cffex))
            for exchange in exchanges:
                if exchange in {"DCE", "CFFEX"}:
                    continue
                for day in days:
                    futures.append(pool.submit(fetch, exchange, day))
            for future in as_completed(futures):
                future.result()
        if write:
            print(json.dumps({"status": "IMPORTING", "verified": progress["verified"]}), flush=True)
            report = run_official_futures_backfill(root, start_day=start_day, end_day=end_day,
                exchanges=exchanges, retry_failed=True, session=NoNetwork())
            progress["importStatus"] = report["status"]
            progress["importedSourceDays"] = report["passed"]
            progress["authoritativeDataWritten"] = report["passed"] > 0
        progress["status"] = (
            "PASS" if progress["failed"] == 0 and progress["unavailable"] == 0
            and (not write or progress.get("importStatus") == "PASS") else "PARTIAL"
        )
        write_json(state / "progress.json", progress)
    return progress


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--exchange", action="append", choices=DEFAULT_EXCHANGES)
    args = parser.parse_args()
    result = run(args.data_root, start_day=args.start_day, end_day=args.end_day, write=args.write,
                 exchanges=tuple(args.exchange) if args.exchange else DEFAULT_EXCHANGES)
    print(json.dumps(result, ensure_ascii=False), flush=True)
