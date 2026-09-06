"""Pure calculations used by the market list snapshot.

The list deliberately bases short-window performance on *trading bars*, not
calendar days.  Keeping this small calculation outside the FastAPI adapter
makes the definition testable and prevents a client-side copy of the formula.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


RETURN_WINDOWS = (3, 5, 10, 22, 44)


def finite_number(value: Any) -> float | None:
    """Return a finite numeric value, preserving unavailable inputs as None."""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _trading_key(bar: Mapping[str, Any]) -> str:
    """Use the source trading day before an ISO bar timestamp."""

    for field in ("trading_day", "tradingDate", "trading_date", "bar_open_time", "barOpenTime"):
        value = str(bar.get(field) or "").strip()
        if value:
            return value[:10]
    return ""


def valid_daily_closes(bars: Iterable[Mapping[str, Any]]) -> list[tuple[str, float]]:
    """Return one finite close for every completed, quality-eligible trading day."""

    unique: dict[str, float] = {}
    for bar in bars:
        if bool(bar.get("is_partial") or bar.get("isPartial")):
            continue
        quality = str(bar.get("quality_status") or bar.get("qualityStatus") or "").upper()
        if quality and quality not in {"PASS", "OK", "AVAILABLE", "可用"}:
            continue
        key = _trading_key(bar)
        close = finite_number(bar.get("close"))
        if key and close is not None:
            unique[key] = close
    return sorted(unique.items())


def trailing_trading_returns(
    bars: Iterable[Mapping[str, Any]], windows: Iterable[int] = RETURN_WINDOWS
) -> dict[int, float | None]:
    """Calculate close-to-close returns over N valid daily trading bars.

    A window needs N+1 eligible closes.  Missing history, a zero baseline and
    non-finite arithmetic remain unavailable rather than becoming a synthetic
    zero percent return.
    """

    closes = valid_daily_closes(bars)
    result: dict[int, float | None] = {}
    for raw_window in windows:
        window = int(raw_window)
        if window < 1 or len(closes) <= window:
            result[window] = None
            continue
        baseline = closes[-window - 1][1]
        latest = closes[-1][1]
        if baseline == 0:
            result[window] = None
            continue
        value = (latest / baseline - 1.0) * 100.0
        result[window] = value if math.isfinite(value) else None
    return result
