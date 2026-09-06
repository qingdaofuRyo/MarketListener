"""Controlled synchronization of provenance-bearing external-market series.

The chart process intentionally never fetches third-party data.  This module
is the explicit producer for the local VIX standard series consumed by
``external_market.load_vix_series``.  It accepts only CBOE's published CSV,
validates every dated close, and atomically replaces the local document after
the entire response has passed validation.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable
from urllib.request import Request, urlopen

from market_monitor.external_market import VIX_INSTRUMENT_ID, VIX_LOCAL_SERIES_PATH


CBOE_VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
_USER_AGENT = "MarketListener/0.1 (local research terminal; VIX sync)"


class ExternalMarketSyncError(ValueError):
    """Raised when an external source cannot produce an auditable local series."""


def _iso_date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError as error:
        raise ExternalMarketSyncError(f"CBOE VIX 日期无效：{value!r}") from error


def parse_cboe_vix_history_csv(text: str) -> tuple[tuple[str, float], ...]:
    """Parse CBOE's DATE/OPEN/HIGH/LOW/CLOSE CSV without dropping bad rows."""

    reader = csv.DictReader(StringIO(text))
    fields = {str(item or "").strip().upper() for item in reader.fieldnames or ()}
    if not {"DATE", "CLOSE"}.issubset(fields):
        raise ExternalMarketSyncError("CBOE VIX CSV 缺少 DATE/CLOSE 列")
    points: list[tuple[str, float]] = []
    seen: set[str] = set()
    for row in reader:
        raw_date = str(row.get("DATE") or "").strip()
        raw_close = str(row.get("CLOSE") or "").strip()
        if not raw_date and not raw_close:
            continue
        if not raw_date or not raw_close:
            raise ExternalMarketSyncError("CBOE VIX CSV 含不完整数据行")
        trading_date = _iso_date(raw_date)
        try:
            close = float(raw_close)
        except ValueError as error:
            raise ExternalMarketSyncError(f"CBOE VIX 收盘价无效：{raw_close!r}") from error
        if not math.isfinite(close) or close < 0:
            raise ExternalMarketSyncError(f"CBOE VIX 收盘价无效：{raw_close!r}")
        if trading_date in seen:
            raise ExternalMarketSyncError(f"CBOE VIX CSV 包含重复交易日：{trading_date}")
        seen.add(trading_date)
        points.append((trading_date, close))
    if not points:
        raise ExternalMarketSyncError("CBOE VIX CSV 没有有效收盘价")
    return tuple(sorted(points))


def _fetch_cboe_vix_history(timeout_seconds: float) -> str:
    request = Request(
        CBOE_VIX_HISTORY_URL,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed public CBOE endpoint
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def build_cboe_vix_document(
    text: str,
    *,
    retrieved_at: str,
) -> dict[str, Any]:
    """Build one validated VIX local-standard-series document from CBOE CSV."""

    points = parse_cboe_vix_history_csv(text)
    digest = sha256(text.encode("utf-8")).hexdigest()
    return {
        "schemaVersion": 1,
        "externalInstrumentId": VIX_INSTRUMENT_ID,
        "source": "CBOE VIX_History.csv",
        "sourceUrl": CBOE_VIX_HISTORY_URL,
        "sourceStatus": "PASS",
        "retrievedAt": retrieved_at,
        "sourceSha256": digest,
        "asOfDate": points[-1][0],
        "points": [
            {"tradingDate": trading_date, "value": value}
            for trading_date, value in points
        ],
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def sync_cboe_vix_history(
    data_root: Path,
    *,
    timeout_seconds: float = 20.0,
    fetch_text: Callable[[float], str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch, validate and atomically persist CBOE VIX history for chart use."""

    if timeout_seconds <= 0:
        raise ExternalMarketSyncError("VIX 同步超时必须大于 0")
    text = (fetch_text or _fetch_cboe_vix_history)(timeout_seconds)
    retrieved_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    document = build_cboe_vix_document(text, retrieved_at=retrieved_at)
    target = Path(data_root) / VIX_LOCAL_SERIES_PATH
    _atomic_json(target, document)
    return {
        "status": "PASS",
        "source": document["source"],
        "sourceUrl": document["sourceUrl"],
        "sourceSha256": document["sourceSha256"],
        "retrievedAt": document["retrievedAt"],
        "asOfDate": document["asOfDate"],
        "pointCount": len(document["points"]),
        "path": str(target),
    }


__all__ = [
    "CBOE_VIX_HISTORY_URL",
    "ExternalMarketSyncError",
    "build_cboe_vix_document",
    "parse_cboe_vix_history_csv",
    "sync_cboe_vix_history",
]
