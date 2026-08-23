"""Resumable TickDB downloader.

The API key is read only from ``TICKDB_API_KEY`` and is never persisted.
Raw API responses are stored as gzip-compressed JSON under data_control/tickdb.
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://api.tickdb.ai"
DEFAULT_INTERVALS = ("1d", "5m", "15m", "30m", "1h", "2h", "4h", "1m", "1w", "1M")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CN_ETF_PREFIXES = (
    "159",
    "510",
    "511",
    "512",
    "513",
    "514",
    "515",
    "516",
    "517",
    "518",
    "560",
    "561",
    "562",
    "563",
    "588",
)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:120] or hashlib.sha256(value.encode()).hexdigest()[:20]


def cached_catalog(root: Path) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for path in sorted((root / "raw" / "catalog").glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            products.extend(json.load(handle).get("data", {}).get("products", []))
    return products


def audit_scope(root: Path, scope: str, intervals: tuple[str, ...]) -> list[dict[str, Any]]:
    products = [
        item
        for item in cached_catalog(root)
        if item.get("is_active", True) and Downloader.in_scope(item, scope)
    ]

    def group(item: dict[str, Any]) -> str:
        if Downloader.is_cn_etf(item):
            return "CN_ETF"
        return f"{item.get('market')}_INDEX"

    result: list[dict[str, Any]] = []
    db = sqlite3.connect(root / "checkpoint.sqlite3")
    try:
        states = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT task_key,status,cursor,records,requests,updated_at FROM tasks WHERE task_key LIKE 'kline:%'"
            )
        }
        for group_name in ("CN_INDEX", "HK_INDEX", "CN_ETF"):
            selected = [item for item in products if group(item) == group_name]
            for interval in intervals:
                complete = failed = unsupported = records = requests_ = raw_files = 0
                latest_updated_at: str | None = None
                latest_bar_time: int | None = None
                for item in selected:
                    product_type = str(item.get("type") or "")
                    symbol = str(item.get("symbol") or "")
                    task_key = f"kline:{product_type}:{symbol}:{interval}"
                    state = states.get(task_key)
                    if state:
                        status, cursor, saved_records, saved_requests, updated_at = state
                        complete += status == "complete"
                        failed += status == "failed"
                        unsupported += status == "unsupported"
                        records += int(saved_records or 0)
                        requests_ += int(saved_requests or 0)
                        latest_updated_at = max(latest_updated_at or updated_at, updated_at)
                        if cursor is not None:
                            latest_bar_time = max(latest_bar_time or int(cursor), int(cursor))
                    directory = root / "raw" / "kline" / product_type / interval / safe_name(symbol)
                    raw_files += sum(1 for path in directory.glob("*.json.gz") if path.is_file())
                result.append(
                    {
                        "group": group_name,
                        "interval": interval,
                        "targetTasks": len(selected),
                        "complete": complete,
                        "failed": failed,
                        "unsupported": unsupported,
                        "rawFiles": raw_files,
                        "savedRecords": records,
                        "requests": requests_,
                        "latestBarTime": (
                            datetime.fromtimestamp(latest_bar_time / 1000, SHANGHAI).isoformat()
                            if latest_bar_time is not None
                            else None
                        ),
                        "latestUpdatedAt": latest_updated_at,
                    }
                )
    finally:
        db.close()
    return result


class TickDBResponseError(RuntimeError):
    """An API rejection with only non-sensitive diagnostic fields."""


class Downloader:
    def __init__(self, root: Path, api_key: str, deadline: datetime, requests_per_minute: int) -> None:
        self.root = root
        self.raw = root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.deadline = deadline
        self.minimum_interval = 60.0 / max(1, requests_per_minute - 1)
        self.last_request = 0.0
        self.request_count = 0
        self.rate_lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "User-Agent": "MarketListener/1.0"})
        self.db = sqlite3.connect(root / "checkpoint.sqlite3")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
            task_key TEXT PRIMARY KEY, status TEXT NOT NULL, cursor INTEGER,
            records INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL)"""
        )
        self.db.commit()

    def expired(self) -> bool:
        return datetime.now(SHANGHAI) >= self.deadline

    def request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.expired():
            raise SystemExit("TickDB API key deadline reached")
        for attempt in range(5):
            # Serialize request starts, not the HTTP round trips.  This keeps
            # aggregate concurrency below the provider's per-minute limit
            # while slow responses no longer reduce throughput.
            with self.rate_lock:
                delay = self.minimum_interval - (time.monotonic() - self.last_request)
                if delay > 0:
                    time.sleep(delay)
                self.last_request = time.monotonic()
            response = self.session.get(BASE_URL + path, params=params, timeout=45)
            with self.rate_lock:
                self.request_count += 1
            if response.status_code == 429:
                time.sleep(max(float(response.headers.get("Retry-After", "3")), self.minimum_interval))
                continue
            if not response.ok:
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = {}
                code = error_payload.get("code")
                message = error_payload.get("message")
                if code is not None or message:
                    raise TickDBResponseError(
                        f"TickDB {code if code is not None else 'unknown'}: "
                        f"{message or 'HTTP request rejected'} (HTTP {response.status_code})"
                    )
                raise TickDBResponseError(f"TickDB HTTP {response.status_code}")
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"TickDB {payload.get('code')}: {payload.get('message')}")
            return payload
        raise RuntimeError("TickDB rate limit persisted after retries")

    def write_raw(self, relative: Path, payload: dict[str, Any]) -> None:
        target = self.raw / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, target)

    def state(self, task_key: str) -> tuple[str, int | None, int, int] | None:
        row = self.db.execute(
            "SELECT status,cursor,records,requests FROM tasks WHERE task_key=?", (task_key,)
        ).fetchone()
        return row if row else None

    def updated_at(self, task_key: str) -> datetime | None:
        row = self.db.execute("SELECT updated_at FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def update(
        self, task_key: str, status: str, cursor: int | None, records: int, requests_: int, error: str | None = None
    ) -> None:
        self.db.execute(
            """INSERT INTO tasks VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(task_key) DO UPDATE SET status=excluded.status,cursor=excluded.cursor,
            records=excluded.records,requests=excluded.requests,error=excluded.error,
            updated_at=excluded.updated_at""",
            (task_key, status, cursor, records, requests_, error, datetime.now(SHANGHAI).isoformat()),
        )
        self.db.commit()

    def fetch_catalog(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        offset = 0
        while True:
            relative = Path("catalog") / f"{offset:08d}.json.gz"
            target = self.raw / relative
            if target.exists() and not refresh:
                with gzip.open(target, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
            else:
                payload = self.request("/v1/symbols/available", {"limit": 1000, "offset": offset})
                self.write_raw(relative, payload)
            page = payload.get("data", {}).get("products", [])
            products.extend(page)
            total = int(payload.get("data", {}).get("pagination", {}).get("total", len(products)))
            if not page or len(products) >= total:
                break
            offset += len(page)
        self.update("catalog", "complete", None, len(products), (offset // 1000) + 1)
        return products

    @staticmethod
    def is_cn_etf(item: dict[str, Any]) -> bool:
        symbol = str(item.get("symbol") or "")
        return (
            item.get("market") == "CN"
            and item.get("type") == "stock"
            and len(symbol) == 6
            and symbol.isdigit()
            and symbol.startswith(CN_ETF_PREFIXES)
        )

    @classmethod
    def in_scope(cls, item: dict[str, Any], scope: str) -> bool:
        if scope == "all":
            return True
        if scope == "cn-hk-index-etf":
            return (
                item.get("type") == "indices"
                and item.get("market") in {"CN", "HK"}
            ) or cls.is_cn_etf(item)
        raise ValueError(f"unknown TickDB scope: {scope}")

    def latest_local_time(self, product_type: str, interval: str, symbol: str) -> int | None:
        latest: int | None = None
        directory = self.raw / "kline" / product_type / interval / safe_name(symbol)
        for path in directory.glob("*.json.gz"):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    payload = json.load(handle)
                for row in payload.get("data", {}).get("klines", []):
                    timestamp = int(row["time"])
                    latest = timestamp if latest is None else max(latest, timestamp)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return latest

    def fetch_reference(self) -> None:
        jobs = [
            ("intervals", "/v1/market/intervals/kline", {}),
            ("sessions-CN", "/v1/market/trading-sessions", {"market": "CN"}),
            ("sessions-HK", "/v1/market/trading-sessions", {"market": "HK"}),
            ("sessions-US", "/v1/market/trading-sessions", {"market": "US"}),
        ]
        for market in ("CN", "HK", "US"):
            year = self.deadline.year
            for month in range(1, self.deadline.month + 1):
                begin = f"{year}{month:02d}01"
                end = f"{year}{month:02d}{calendar.monthrange(year, month)[1]:02d}"
                jobs.append(
                    (
                        f"trade-days-{market}-{year}-{month:02d}",
                        "/v1/market/trade-days",
                        {
                            "market": market,
                            "beg_day": begin,
                            "end_day": min(end, self.deadline.strftime("%Y%m%d")),
                        },
                    )
                )
        for key, path, params in jobs:
            if self.state(key) and self.state(key)[0] == "complete":
                continue
            try:
                payload = self.request(path, params)
                self.write_raw(Path("reference") / f"{safe_name(key)}.json.gz", payload)
                self.update(key, "complete", None, 1, 1)
            except Exception as error:
                self.update(key, "failed", None, 0, 1, f"{type(error).__name__}: {error}"[:500])

    @staticmethod
    def ordered_products(products: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        type_order = {"futures": 0, "stock": 1, "indices": 2, "forex": 3, "crypto": 4}

        def continuous_priority(item: dict[str, Any]) -> int:
            symbol = str(item.get("symbol") or "")
            if item.get("type") == "futures" and symbol.endswith(("7777", "8888", "9999")):
                return 0
            return 1

        return sorted(
            (item for item in products if item.get("is_active", True)),
            key=lambda item: (
                0 if item.get("market") == "CN" else 1,
                type_order.get(str(item.get("type")), 9),
                continuous_priority(item),
                str(item.get("symbol")),
            ),
        )

    def fetch_tickers(self, products: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[str]] = {}
        for item in self.ordered_products(products):
            market = str(item.get("market") or "")
            product_type = str(item.get("type") or "")
            symbol = str(item.get("symbol") or "")
            if product_type == "futures" and (
                market == "HK" or (market == "CN" and not symbol.endswith(("7777", "8888", "9999")))
            ):
                continue
            grouped.setdefault(product_type, []).append(symbol)
        for product_type, symbols in grouped.items():
            for offset in range(0, len(symbols), 50):
                batch = [symbol for symbol in symbols[offset : offset + 50] if symbol]
                if not batch:
                    continue
                task_key = f"ticker:{product_type}:{offset}"
                if self.state(task_key) and self.state(task_key)[0] == "complete":
                    continue
                try:
                    payload = self.request(
                        "/v1/market/ticker", {"symbols": ",".join(batch), "type": product_type}
                    )
                    self.write_raw(
                        Path("ticker") / product_type / f"{offset:08d}.json.gz", payload
                    )
                    self.update(task_key, "complete", None, len(payload.get("data") or []), 1)
                except Exception as error:
                    self.update(task_key, "failed", None, 0, 1, f"{type(error).__name__}: {error}"[:500])

    def fetch_history(
        self,
        products: list[dict[str, Any]],
        intervals: tuple[str, ...],
        exclude_types: frozenset[str],
        exclude_global_indices: bool,
        markets: frozenset[str] = frozenset(),
        product_types: frozenset[str] = frozenset(),
        retry_unsupported: bool = False,
        latest_only: bool = False,
        latest_limit: int = 100,
        scope: str = "all",
        refresh_complete: bool = False,
        resume_since: datetime | None = None,
        workers: int = 1,
    ) -> None:
        priority = {
            ("CN", "indices"): 0,
            ("HK", "indices"): 1,
            ("CN", "stock"): 2,
            ("CN", "futures"): 3,
            ("HK", "stock"): 4,
            ("US", "stock"): 5,
        }
        ordered = [
            item
            for item in self.ordered_products(products)
            if self.in_scope(item, scope)
            and str(item.get("type")) not in exclude_types
            and (not markets or str(item.get("market")) in markets)
            and (not product_types or str(item.get("type")) in product_types)
            and not (exclude_global_indices and item.get("market") == "GLOBAL" and item.get("type") == "indices")
            and (str(item.get("market")), str(item.get("type"))) in priority
            and not (
                item.get("type") == "futures"
                and (item.get("market") != "CN" or not str(item.get("symbol") or "").endswith(("7777", "8888", "9999")))
            )
        ]
        ordered.sort(key=lambda item: (priority[(str(item["market"]), str(item["type"]))], str(item["symbol"])))
        if latest_only and workers > 1:
            self.fetch_latest_concurrently(
                ordered,
                intervals,
                latest_limit,
                retry_unsupported,
                refresh_complete,
                resume_since,
                workers,
            )
            return
        for interval in intervals:
            for item in ordered:
                symbol = str(item.get("symbol") or "")
                product_type = str(item.get("type") or "")
                if not symbol or not product_type:
                    continue
                task_key = f"kline:{product_type}:{symbol}:{interval}"
                saved = self.state(task_key)
                refreshed_this_run = (
                    saved is not None
                    and resume_since is not None
                    and (self.updated_at(task_key) or datetime.min.replace(tzinfo=SHANGHAI)) >= resume_since
                )
                if saved and (
                    (saved[0] == "complete" and (not refresh_complete or refreshed_this_run))
                    or (saved[0] == "unsupported" and not retry_unsupported)
                ):
                    continue
                cursor = saved[1] if saved and saved[1] else int(time.time() * 1000)
                records = saved[2] if saved else 0
                requests_ = saved[3] if saved else 0
                page_number = requests_
                if latest_only:
                    try:
                        payload = self.request(
                            "/v1/market/kline",
                            {"symbol": symbol, "type": product_type, "interval": interval, "limit": latest_limit},
                        )
                        klines = payload.get("data", {}).get("klines", [])
                        previous_latest = saved[1] if saved and saved[1] else self.latest_local_time(
                            product_type, interval, symbol
                        )
                        timestamps = [int(row["time"]) for row in klines if row.get("time") is not None]
                        latest = max(timestamps, default=previous_latest)
                        new_records = (
                            len(timestamps)
                            if previous_latest is None
                            else sum(timestamp > previous_latest for timestamp in timestamps)
                        )
                        self.write_raw(
                            Path("kline") / product_type / interval / safe_name(symbol) / "latest.json.gz",
                            payload,
                        )
                        if new_records and latest is not None:
                            self.write_raw(
                                Path("kline")
                                / product_type
                                / interval
                                / safe_name(symbol)
                                / f"incremental-{latest:013d}.json.gz",
                                payload,
                            )
                        previous_records = saved[2] if saved else 0
                        previous_requests = saved[3] if saved else 0
                        self.update(
                            task_key,
                            "complete",
                            latest,
                            previous_records + new_records,
                            previous_requests + 1,
                        )
                    except Exception as error:
                        message = f"{type(error).__name__}: {error}"[:500]
                        status = "unsupported" if "403" in message or "not supported" in message.lower() else "failed"
                        self.update(task_key, status, None, 0, 1, message)
                    if self.request_count and self.request_count % 50 == 0:
                        print(f"requests={self.request_count} interval={interval} symbol={symbol}", flush=True)
                    continue
                while not self.expired():
                    try:
                        payload = self.request(
                            "/v1/market/kline",
                            {"symbol": symbol, "type": product_type, "interval": interval, "limit": 1000, "end_time": cursor},
                        )
                        klines = payload.get("data", {}).get("klines", [])
                        self.write_raw(
                            Path("kline") / product_type / interval / safe_name(symbol) / f"{page_number:06d}.json.gz",
                            payload,
                        )
                        requests_ += 1
                        records += len(klines)
                        if not klines or len(klines) < 1000:
                            self.update(task_key, "complete", None, records, requests_)
                            break
                        next_cursor = min(int(row["time"]) for row in klines) - 1
                        if next_cursor >= cursor:
                            self.update(task_key, "failed", cursor, records, requests_, "non-decreasing cursor")
                            break
                        cursor = next_cursor
                        page_number += 1
                        self.update(task_key, "running", cursor, records, requests_)
                    except Exception as error:
                        message = f"{type(error).__name__}: {error}"[:500]
                        status = "unsupported" if "403" in message or "not supported" in message.lower() else "failed"
                        self.update(task_key, status, cursor, records, requests_, message)
                        break
                if self.request_count and self.request_count % 50 == 0:
                    print(f"requests={self.request_count} interval={interval} symbol={symbol}", flush=True)

    def fetch_latest_concurrently(
        self,
        products: list[dict[str, Any]],
        intervals: tuple[str, ...],
        latest_limit: int,
        retry_unsupported: bool,
        refresh_complete: bool,
        resume_since: datetime | None,
        workers: int,
    ) -> None:
        jobs: list[tuple[str, str, str, tuple[str, int | None, int, int] | None]] = []
        for interval in intervals:
            for item in products:
                symbol = str(item.get("symbol") or "")
                product_type = str(item.get("type") or "")
                if not symbol or not product_type:
                    continue
                task_key = f"kline:{product_type}:{symbol}:{interval}"
                saved = self.state(task_key)
                refreshed_this_run = (
                    saved is not None
                    and resume_since is not None
                    and (self.updated_at(task_key) or datetime.min.replace(tzinfo=SHANGHAI)) >= resume_since
                )
                if saved and (
                    (saved[0] == "complete" and (not refresh_complete or refreshed_this_run))
                    or (saved[0] == "unsupported" and not retry_unsupported)
                ):
                    continue
                jobs.append((task_key, product_type, symbol, saved))

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_jobs = {
                executor.submit(
                    self.request,
                    "/v1/market/kline",
                    {
                        "symbol": symbol,
                        "type": product_type,
                        "interval": task_key.rsplit(":", 1)[1],
                        "limit": latest_limit,
                    },
                ): (task_key, product_type, symbol, saved)
                for task_key, product_type, symbol, saved in jobs
            }
            for future in concurrent.futures.as_completed(future_jobs):
                task_key, product_type, symbol, saved = future_jobs[future]
                interval = task_key.rsplit(":", 1)[1]
                try:
                    payload = future.result()
                    klines = payload.get("data", {}).get("klines", [])
                    previous_latest = saved[1] if saved and saved[1] else self.latest_local_time(
                        product_type, interval, symbol
                    )
                    timestamps = [int(row["time"]) for row in klines if row.get("time") is not None]
                    latest = max(timestamps, default=previous_latest)
                    new_records = (
                        len(timestamps)
                        if previous_latest is None
                        else sum(timestamp > previous_latest for timestamp in timestamps)
                    )
                    relative = Path("kline") / product_type / interval / safe_name(symbol)
                    self.write_raw(relative / "latest.json.gz", payload)
                    if new_records and latest is not None:
                        self.write_raw(relative / f"incremental-{latest:013d}.json.gz", payload)
                    self.update(
                        task_key,
                        "complete",
                        latest,
                        (saved[2] if saved else 0) + new_records,
                        (saved[3] if saved else 0) + 1,
                    )
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"[:500]
                    status = "unsupported" if "403" in message or "not supported" in message.lower() else "failed"
                    self.update(
                        task_key,
                        status,
                        saved[1] if saved else None,
                        saved[2] if saved else 0,
                        (saved[3] if saved else 0) + 1,
                        message,
                    )
                completed += 1
                if completed % 50 == 0:
                    print(
                        f"requests={self.request_count} completed={completed}/{len(jobs)} "
                        f"interval={interval} symbol={symbol}",
                        flush=True,
                    )

    def summary(self) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT status,count(*),sum(records),sum(requests) FROM tasks GROUP BY status ORDER BY status"
        ).fetchall()
        return {"deadline": self.deadline.isoformat(), "requests_this_run": self.request_count, "tasks": rows}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("data_control/tickdb"))
    parser.add_argument("--deadline", default="2026-08-23T11:29:02+08:00")
    parser.add_argument("--rpm", type=int, default=30)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--exclude-kline-types", default="crypto,forex")
    parser.add_argument("--exclude-global-indices", action="store_true")
    parser.add_argument("--markets", default="", help="optional comma-separated TickDB markets, e.g. CN")
    parser.add_argument("--types", default="", help="optional comma-separated product types, e.g. indices")
    parser.add_argument("--retry-unsupported", action="store_true", help="retry selected tasks previously marked unsupported")
    parser.add_argument("--skip-tickers", action="store_true", help="do not request ticker snapshots")
    parser.add_argument("--latest-only", action="store_true", help="request only the provider's latest window; do not use historical paging")
    parser.add_argument("--latest-limit", type=int, default=100, help="bar count for --latest-only (1-1000; plan limits still apply)")
    parser.add_argument(
        "--scope",
        choices=("all", "cn-hk-index-etf"),
        default="all",
        help="restrict the catalog to a maintained product scope",
    )
    parser.add_argument("--workers", type=int, default=1, help="parallel HTTP workers (global RPM limit is shared)")
    parser.add_argument("--audit-only", action="store_true", help="print cached scope/checkpoint statistics without API access")
    parser.add_argument("--refresh-catalog", action="store_true", help="replace cached catalog pages from the API")
    parser.add_argument("--refresh-complete", action="store_true", help="refresh tasks already marked complete")
    parser.add_argument(
        "--resume-since",
        default="",
        help="with --refresh-complete, skip tasks updated on/after this ISO timestamp",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    intervals = tuple(part.strip() for part in args.intervals.split(",") if part.strip())
    if args.audit_only:
        print(json.dumps(audit_scope(args.root, args.scope, intervals), ensure_ascii=False), flush=True)
        return 0
    api_key = os.environ.get("TICKDB_API_KEY")
    if not api_key:
        print("TICKDB_API_KEY is required", file=sys.stderr)
        return 2
    deadline = datetime.fromisoformat(args.deadline).astimezone(SHANGHAI)
    resume_since = datetime.fromisoformat(args.resume_since).astimezone(SHANGHAI) if args.resume_since else None
    if not 1 <= args.latest_limit <= 1000:
        raise ValueError("--latest-limit must be between 1 and 1000")
    if not 1 <= args.workers <= 16:
        raise ValueError("--workers must be between 1 and 16")
    downloader = Downloader(args.root, api_key, deadline, args.rpm)
    try:
        products = downloader.fetch_catalog(refresh=args.refresh_catalog)
        downloader.fetch_reference()
        downloader.fetch_history(
            products,
            intervals,
            frozenset(part.strip() for part in args.exclude_kline_types.split(",") if part.strip()),
            args.exclude_global_indices,
            frozenset(part.strip() for part in args.markets.split(",") if part.strip()),
            frozenset(part.strip() for part in args.types.split(",") if part.strip()),
            args.retry_unsupported,
            args.latest_only,
            args.latest_limit,
            args.scope,
            args.refresh_complete,
            resume_since,
            args.workers,
        )
        if not args.skip_tickers:
            downloader.fetch_tickers(products)
        print(json.dumps(downloader.summary(), ensure_ascii=False), flush=True)
        return 0
    finally:
        downloader.session.close()
        downloader.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
