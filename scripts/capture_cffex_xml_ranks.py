"""Capture CFFEX's published daily member-ranking XML into Bronze only.

The first attempt has no artificial request interval. Transient failures reduce
the number of simultaneous product requests and introduce bounded backoff.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from market_monitor.czce_html_rank_backfill import _read_json, _write_bytes, _write_json
from market_monitor.futures_calendar import load_futures_trading_calendar
from market_monitor.futures_official_backfill import _process_file_lock
from market_monitor.futures_official_member_sources import (
    cffex_rank_products,
    parse_cffex_rank_csv,
    parse_cffex_rank_xml,
)


SCHEMA = "cffex-xml-rank-capture/v1"
BRONZE = Path("bronze") / "cffex_xml_rank_capture"
STATE = Path("state") / "cffex_xml_rank_capture"
SOURCE = "http://www.cffex.com.cn/sj/ccpm/{month}/{day}/{product}.xml?id=0"
HEADERS = {
    "Accept": "text/plain, */*; q=0.01",
    "Referer": "http://www.cffex.com.cn/cn/ccpm.html",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
}
LEGACY_URL = re.compile(r"/ccpm/(20\d{2})(\d{2})/(\d{2})/([A-Z]+)(?:_1\.csv|\.xml)$")
MAX_RETRY_AFTER_SECONDS = 300.0


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_url(day: str, product: str) -> str:
    compact = day.replace("-", "")
    return SOURCE.format(month=compact[:6], day=compact[6:], product=product)


def _summarize_xml(raw: bytes, day: str, product: str) -> dict[str, int]:
    frames = parse_cffex_rank_xml(raw, trading_day=day.replace("-", ""), product=product)
    if not frames:
        raise ValueError("CFFEX XML has no published contract rankings")
    required = ("vol_party_name", "long_party_name", "short_party_name")
    for contract, frame in frames.items():
        prefix = re.match(r"[A-Z]+", contract)
        if prefix is None or prefix.group() != product:
            raise ValueError("CFFEX XML contract product differs from request")
        if frame[list(required)].isna().any().any():
            raise ValueError("CFFEX XML omits a published ranking direction")
    return {"contractCount": len(frames), "rankRows": sum(len(frame) for frame in frames.values())}


def _summarize_legacy(raw: bytes, day: str, product: str, source_url: str) -> dict[str, int]:
    if source_url.endswith(".csv"):
        frames = parse_cffex_rank_csv(raw, trading_day=day.replace("-", ""))
        if not frames:
            raise ValueError("CFFEX CSV has no published contract rankings")
        for contract in frames:
            prefix = re.match(r"[A-Z]+", contract)
            if prefix is None or prefix.group() != product:
                raise ValueError("CFFEX CSV product differs from request")
        return {"contractCount": len(frames), "rankRows": sum(len(frame) for frame in frames.values())}
    return _summarize_xml(raw, day, product)


def _own_manifest(root: Path, day: str, product: str) -> dict[str, Any] | None:
    path = root / BRONZE / day / f"{product}.xml"
    manifest = _read_json(path.with_suffix(".json"))
    if manifest is None or not path.is_file():
        return None
    if not (
        manifest.get("schema") == SCHEMA
        and manifest.get("status") == "PASS_RAW"
        and manifest.get("tradingDay") == day
        and manifest.get("product") == product
        and manifest.get("sourceUrl") == _source_url(day, product)
        and manifest.get("authoritativeDataWritten") is False
        and manifest.get("rawPath") == path.relative_to(root).as_posix()
    ):
        return None
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != manifest.get("payloadSha256"):
        return None
    try:
        summary = _summarize_xml(raw, day, product)
    except ValueError:
        return None
    return {"status": "PASS_RAW", "sourcePath": manifest["rawPath"],
            "payloadSha256": manifest["payloadSha256"], **summary}


def _legacy_index(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Reuse only old official responses whose checksum, date and rows verify."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for directory in (root / "bronze").glob("official_futures_member_cffex_*"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            manifest = _read_json(path)
            if manifest is None or manifest.get("publisher") != "CFFEX":
                continue
            source_url = str(manifest.get("sourceUrl") or "").split("?", 1)[0]
            match = LEGACY_URL.search(source_url)
            if match is None:
                continue
            compact = "".join(match.groups()[:3])
            product = match.group(4)
            day = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
            try:
                raw = base64.b64decode(manifest["contentBase64"], validate=True)
                if sha256(raw).hexdigest() != manifest["payloadSha256"]:
                    continue
                summary = _summarize_legacy(raw, day, product, source_url)
            except (KeyError, TypeError, ValueError, UnicodeError):
                continue
            index[(day, product)] = {
                "status": "PASS_RAW", "sourcePath": path.relative_to(root).as_posix(),
                "payloadSha256": manifest["payloadSha256"], **summary,
            }
    return index


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _fetch_once(day: str, product: str, *, timeout: float) -> dict[str, Any]:
    url = _source_url(day, product)
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, headers=HEADERS, timeout=(min(timeout, 8.0), timeout))
    except requests.RequestException as error:
        return {"product": product, "status": "TRANSIENT", "errorType": type(error).__name__}
    host = urlsplit(response.url).hostname
    if host not in {"cffex.com.cn", "www.cffex.com.cn"}:
        return {"product": product, "status": "BLOCKED", "httpStatus": response.status_code,
                "errorType": "UnexpectedRedirect"}
    status = response.status_code
    if status in {403, 412}:
        return {"product": product, "status": "BLOCKED", "httpStatus": status}
    if status == 404:
        return {"product": product, "status": "NO_PUBLICATION", "httpStatus": status,
                "sourceUrl": url}
    if status == 429 or 500 <= status <= 599:
        return {"product": product, "status": "TRANSIENT", "httpStatus": status,
                "retryAfterSeconds": _retry_after_seconds(response.headers.get("Retry-After"))}
    if status != 200:
        return {"product": product, "status": "FAILED", "httpStatus": status}
    raw = response.content
    if not raw or len(raw) > 8_000_000 or "html" in response.headers.get("Content-Type", "").lower():
        return {"product": product, "status": "BLOCKED", "httpStatus": status,
                "errorType": "UnexpectedContent"}
    try:
        summary = _summarize_xml(raw, day, product)
    except ValueError as error:
        return {"product": product, "status": "FAILED", "httpStatus": status,
                "errorType": type(error).__name__, "error": str(error)[:160]}
    return {"product": product, "status": "PASS_RAW", "httpStatus": status,
            "sourceUrl": url, "raw": raw, **summary}


def _save_xml(root: Path, day: str, result: dict[str, Any]) -> dict[str, Any]:
    product = result["product"]
    raw = result["raw"]
    path = root / BRONZE / day / f"{product}.xml"
    digest = sha256(raw).hexdigest()
    _write_bytes(path, raw)
    manifest = {
        "schema": SCHEMA, "status": "PASS_RAW", "publisher": "CFFEX",
        "tradingDay": day, "product": product, "sourceUrl": result["sourceUrl"],
        "retrievedAt": _timestamp(), "payloadSha256": digest,
        "rawPath": path.relative_to(root).as_posix(),
        "byteCount": len(raw), "contractCount": result["contractCount"],
        "rankRows": result["rankRows"], "authoritativeDataWritten": False,
    }
    _write_json(path.with_suffix(".json"), manifest)
    return {"status": "PASS_RAW", "sourcePath": manifest["rawPath"],
            "payloadSha256": digest, "contractCount": result["contractCount"],
            "rankRows": result["rankRows"]}


def _capture_day(
    root: Path, day: str, existing: dict[str, dict[str, Any]], *, workers: int,
    max_attempts: int, timeout: float, sleep: Any = time.sleep,
    fetch: Any | None = None,
) -> tuple[dict[str, dict[str, Any]], int, str]:
    products = cffex_rank_products(day.replace("-", ""))
    results = dict(existing)
    pending = [product for product in products if product not in results]
    attempts = {product: 0 for product in pending}
    width = workers
    while pending:
        wave, pending = pending[:width], pending[width:]
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(fetch or _fetch_once, day, product, timeout=timeout): product
                       for product in wave}
            outcomes = [future.result() for future in as_completed(futures)]
        transient: list[dict[str, Any]] = []
        blocked = False
        for outcome in outcomes:
            product = outcome["product"]
            attempts[product] += 1
            status = outcome["status"]
            if status == "PASS_RAW":
                results[product] = _save_xml(root, day, outcome)
            elif status == "TRANSIENT":
                transient.append(outcome)
            else:
                results[product] = {key: value for key, value in outcome.items() if key != "raw"}
                blocked |= status == "BLOCKED"
        if blocked:
            return results, width, "BLOCKED"
        if any(item["status"] == "FAILED" for item in results.values()):
            return results, width, "FAILED"
        if transient:
            width = max(1, width // 2)
            if any(attempts[item["product"]] >= max_attempts for item in transient):
                for item in transient:
                    item["status"] = "FAILED"
                    item["errorType"] = "RetryExhausted"
                    results[item["product"]] = item
                return results, width, "FAILED"
            delay = max(
                max(2 ** (attempts[item["product"]] - 1) for item in transient),
                max(float(item.get("retryAfterSeconds", 0)) for item in transient),
            )
            if delay > MAX_RETRY_AFTER_SECONDS:
                for item in transient:
                    item["status"] = "RATE_PAUSED"
                    results[item["product"]] = item
                return results, width, "RATE_PAUSED"
            pending = [item["product"] for item in transient] + pending
            sleep(delay)
    if not any(item["status"] == "PASS_RAW" for item in results.values()):
        return results, width, "NO_PUBLICATION"
    return results, width, "PASS_RAW"


def _day_covered(day: str, products: dict[str, dict[str, Any]]) -> bool:
    expected = cffex_rank_products(day.replace("-", ""))
    return (len(products) == len(expected)
            and all(product in products for product in expected)
            and all(item["status"] in {"PASS_RAW", "NO_PUBLICATION"}
                    for item in products.values())
            and any(item["status"] == "PASS_RAW" for item in products.values()))


def capture(
    root: Path, *, start_day: str, end_day: str, workers: int = 8,
    max_attempts: int = 4, max_days: int | None = None,
    timeout: float = 20.0, dry_run: bool = False,
) -> dict[str, Any]:
    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or not 1 <= workers <= 8 or max_attempts < 1 or timeout <= 0:
        raise ValueError("invalid CFFEX capture range or concurrency parameters")
    if max_days is not None and max_days < 1:
        raise ValueError("max_days must be positive")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified trading calendar has no days in range")
    state_dir = root / STATE
    with _process_file_lock(state_dir / "capture.lock"):
        legacy = _legacy_index(root)
        existing_by_day: dict[str, dict[str, dict[str, Any]]] = {}
        for day in days:
            existing: dict[str, dict[str, Any]] = {}
            for product in cffex_rank_products(day.replace("-", "")):
                own = _own_manifest(root, day, product)
                prior = own or legacy.get((day, product))
                if prior is not None:
                    existing[product] = prior
                else:
                    marker = _read_json(root / BRONZE / day / f"{product}.json")
                    if marker is not None and marker.get("status") == "NO_PUBLICATION" and (
                        marker.get("schema") == SCHEMA and marker.get("tradingDay") == day
                        and marker.get("product") == product
                        and marker.get("sourceUrl") == _source_url(day, product)
                    ):
                        existing[product] = {"status": "NO_PUBLICATION", "httpStatus": 404,
                                             "sourceUrl": marker["sourceUrl"]}
            if existing and all(item["status"] == "NO_PUBLICATION" for item in existing.values()):
                # A calendar trading day with no verified rankings needs a fresh probe.
                existing.clear()
            existing_by_day[day] = existing
        complete = sum(_day_covered(day, existing_by_day[day]) for day in days)
        pending = [day for day in days if not _day_covered(day, existing_by_day[day])]
        progress: dict[str, Any] = {
            "schema": SCHEMA, "status": "DRY_RUN" if dry_run else "RUNNING",
            "startDay": start.isoformat(), "endDay": end.isoformat(),
            "calendarDays": len(days), "completedDays": complete,
            "pendingDays": len(pending), "newDaysThisRun": 0,
            "initialWorkers": workers, "currentWorkers": workers,
            "authoritativeDataWritten": False, "updatedAt": _timestamp(),
        }
        if dry_run:
            progress["firstPendingDay"] = pending[0] if pending else None
            return progress
        selected = pending[:max_days] if max_days is not None else pending
        _write_json(state_dir / "progress.json", progress)
        width = workers
        for day in selected:
            try:
                results, width, status = _capture_day(
                    root, day, existing_by_day[day], workers=width,
                    max_attempts=max_attempts, timeout=timeout,
                )
            except Exception as error:
                progress.update(status="FAILED", errorDay=day,
                                errorType=type(error).__name__, updatedAt=_timestamp())
                _write_json(state_dir / "progress.json", progress)
                raise
            for product, item in results.items():
                if status == "PASS_RAW" and item["status"] == "NO_PUBLICATION":
                    marker = {"schema": SCHEMA, "publisher": "CFFEX",
                              "status": "NO_PUBLICATION", "tradingDay": day, "product": product,
                              "sourceUrl": _source_url(day, product), "httpStatus": 404,
                              "retrievedAt": _timestamp(), "authoritativeDataWritten": False}
                    _write_json(root / BRONZE / day / f"{product}.json", marker)
            day_manifest = {
                "schema": SCHEMA, "tradingDay": day, "publisher": "CFFEX",
                "status": status, "products": results, "expectedProducts": list(
                    cffex_rank_products(day.replace("-", ""))),
                "authoritativeDataWritten": False, "updatedAt": _timestamp(),
            }
            _write_json(state_dir / f"{day.replace('-', '')}.json", day_manifest)
            if status == "PASS_RAW":
                progress["completedDays"] += 1
                progress["newDaysThisRun"] += 1
            progress.update(currentDay=day, currentWorkers=width,
                            pendingDays=len(days) - progress["completedDays"],
                            updatedAt=_timestamp())
            if status != "PASS_RAW":
                progress.update(status=status, errorDay=day)
            _write_json(state_dir / "progress.json", progress)
            print(json.dumps({"day": day, "status": status, "products": len(results),
                              "workers": width}, ensure_ascii=False), flush=True)
            if status != "PASS_RAW":
                break
        else:
            progress["status"] = "PASS_RAW" if len(selected) == len(pending) else "BATCH_LIMIT"
            progress["updatedAt"] = _timestamp()
            _write_json(state_dir / "progress.json", progress)
        return progress


def main() -> int:
    parser = argparse.ArgumentParser(description="中金所逐日逐品种并发采集官方 XML 排名原件（只写 Bronze）")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", default=(datetime.now(ZoneInfo("Asia/Shanghai")).date()
                                          - timedelta(days=1)).isoformat())
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = capture(args.data_root, start_day=args.start_day, end_day=args.end_day,
                     workers=args.workers, max_attempts=args.max_attempts,
                     max_days=args.max_days, timeout=args.timeout, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {"DRY_RUN", "PASS_RAW", "BATCH_LIMIT", "RATE_PAUSED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
