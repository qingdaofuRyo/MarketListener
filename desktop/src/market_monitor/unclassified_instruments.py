"""Read-only audit inventory for local TDX files outside known classifications."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import struct
from threading import RLock
import time
from typing import Any

from . import futures_bulk, tdx_local


_RECORD_SIZE = 32
_CLOSE_OFFSET = 16
_CACHE_SECONDS = 60.0
_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_CACHE_LOCK = RLock()


def clear_unclassified_cache() -> None:
    """Drop the short-lived filesystem scan cache (mainly for tests/import refreshes)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _record_observation(path: Path) -> dict[str, Any] | None:
    """Read only the newest complete 32-byte bar from an unclassified file."""
    try:
        size = path.stat().st_size
        if size < _RECORD_SIZE or size % _RECORD_SIZE:
            return None
        with path.open("rb") as stream:
            stream.seek(-_RECORD_SIZE, 2)
            record = stream.read(_RECORD_SIZE)
        close = struct.unpack_from("<f", record, _CLOSE_OFFSET)[0]
        if not math.isfinite(close):
            close = None
        if path.suffix.casefold() == ".day":
            raw_day = struct.unpack_from("<I", record, 0)[0]
            day = f"{raw_day // 10000:04d}-{raw_day % 10000 // 100:02d}-{raw_day % 100:02d}"
            datetime.fromisoformat(day)
            timestamp = f"{day}T00:00:00+08:00"
            period = "1d"
        else:
            raw_day, minutes = struct.unpack_from("<HH", record, 0)
            day = tdx_local.decode_minute_day(raw_day)
            timestamp = f"{day}T{minutes // 60:02d}:{minutes % 60:02d}:00+08:00"
            datetime.fromisoformat(timestamp)
            period = "5m"
        return {"latestClose": close, "lastBarAt": timestamp, "pricePeriod": period}
    except (OSError, ValueError, struct.error):
        return None


def _recognized_by_financial_terminal(name: str) -> bool:
    return tdx_local._HK_FILE.fullmatch(name) is not None


def _recognized_by_futures_terminal(name: str) -> bool:
    return any(pattern.fullmatch(name) is not None for pattern in (
        futures_bulk._SPECIAL,
        futures_bulk._CONTRACT,
        futures_bulk._INDEX,
    ))


def _scan_terminal(root: Path, *, terminal: str, financial: bool) -> list[dict[str, Any]]:
    metadata = {} if financial else futures_bulk._metadata(root)
    grouped: dict[str, dict[str, Any]] = {}
    recognizer = _recognized_by_financial_terminal if financial else _recognized_by_futures_terminal
    for folder_name in ("lday", "fzline"):
        folder = root / "vipdoc" / "ds" / folder_name
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.suffix.casefold() not in {".day", ".lc5"} or recognizer(path.name):
                continue
            source_code = path.stem.upper()
            market_prefix, separator, code = source_code.partition("#")
            if not separator or not market_prefix.isdigit() or not code:
                market_prefix, code = "", source_code
            row = grouped.setdefault(source_code, {
                "reviewId": f"raw:{terminal}:{source_code}",
                "name": metadata.get(source_code),
                "code": code,
                "sourceCode": source_code,
                "marketPrefix": market_prefix,
                "latestClose": None,
                "lastBarAt": None,
                "pricePeriod": None,
                "periods": [],
                "sourceTerminal": terminal,
                "origin": "RAW_UNRECOGNIZED",
                "classificationStatus": "PENDING_REVIEW",
                "reason": f"文件名未命中{terminal}已登记的市场与品种分类规则",
            })
            period = "1d" if path.suffix.casefold() == ".day" else "5m"
            row["periods"].append(period)
            observation = _record_observation(path)
            if observation and (
                row["lastBarAt"] is None or str(observation["lastBarAt"]) > str(row["lastBarAt"])
            ):
                row.update(observation)
    for row in grouped.values():
        row["periods"] = sorted(set(row["periods"]), key=lambda value: (value != "1d", value))
    return list(grouped.values())


def scan_unclassified_tdx(
    financial_root: Path | None = None,
    futures_root: Path | None = None,
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Combine raw files not accepted by either TDX importer's classification rules."""
    resolved_financial = tdx_local.resolve_tdx_root(financial_root)
    resolved_futures = futures_bulk.resolve_tdx_root(futures_root)
    cache_key = (str(resolved_financial or ""), str(resolved_futures or ""))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if not refresh and cached and cached[0] > now:
            return [dict(item) for item in cached[1]]

    items: list[dict[str, Any]] = []
    if resolved_financial:
        items.extend(_scan_terminal(resolved_financial, terminal="通达信金融终端", financial=True))
    if resolved_futures:
        items.extend(_scan_terminal(resolved_futures, terminal="通达信期货通", financial=False))
    items.sort(key=lambda item: (
        str(item["sourceTerminal"]),
        str(item["marketPrefix"]),
        str(item["code"]),
    ))
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now + _CACHE_SECONDS, items)
    return [dict(item) for item in items]
