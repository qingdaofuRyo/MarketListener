"""Read and align verified local external-market series.

The browser never reaches a third-party source.  An external series becomes
available only after a producer writes this small, provenance-bearing local
document with an explicit PASS source status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

VIX_INSTRUMENT_ID = "US.CBOE.INDEX.VIX"
VIX_LOCAL_SERIES_PATH = Path("external_market") / "vix" / "series.json"


@dataclass(frozen=True)
class ExternalMarketSeries:
    external_instrument_id: str
    source: str | None
    as_of_date: str | None
    points: tuple[tuple[str, float], ...]
    status: str
    reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "externalInstrumentId": self.external_instrument_id,
            "source": self.source,
            "asOfDate": self.as_of_date,
            "sourcePointCount": len(self.points),
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExternalMarketAlignment:
    series: ExternalMarketSeries
    values: tuple[float | None, ...]
    points: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    status: str
    reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            **self.series.to_public_dict(),
            "status": self.status,
            "reason": self.reason or self.series.reason,
            "coverage": self.coverage,
            "points": [dict(point) for point in self.points],
        }


def _unavailable(reason: str) -> ExternalMarketSeries:
    return ExternalMarketSeries(VIX_INSTRUMENT_ID, None, None, (), "unavailable", reason)


def _date_text(value: Any) -> str | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def load_vix_series(data_root: Path) -> ExternalMarketSeries:
    """Read only a source-verified, local VIX close series; never fetch it."""

    path = data_root / VIX_LOCAL_SERIES_PATH
    if not path.is_file():
        return _unavailable("缺少本地 VIX 标准序列")
    payload = _load_json(path)
    if not isinstance(payload, Mapping):
        return _unavailable("本地 VIX 标准序列格式无效")
    if payload.get("schemaVersion") != 1:
        return _unavailable("本地 VIX 标准序列版本无效")
    if payload.get("externalInstrumentId") != VIX_INSTRUMENT_ID:
        return _unavailable("本地 VIX 标准标的映射无效")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip() or payload.get("sourceStatus") != "PASS":
        return _unavailable("本地 VIX 序列没有 PASS 来源证据")
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        return _unavailable("本地 VIX 标准序列没有可用点")
    points: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in raw_points:
        if not isinstance(item, Mapping):
            return _unavailable("本地 VIX 标准序列包含无效点")
        trading_date = _date_text(item.get("tradingDate"))
        value = _finite(item.get("value"))
        if trading_date is None or value is None or trading_date in seen:
            return _unavailable("本地 VIX 标准序列日期或数值无效")
        seen.add(trading_date)
        points.append((trading_date, value))
    points.sort(key=lambda item: item[0])
    as_of_date = _date_text(payload.get("asOfDate"))
    if as_of_date is None or as_of_date != points[-1][0]:
        return _unavailable("本地 VIX asOfDate 与最新点不一致")
    return ExternalMarketSeries(
        VIX_INSTRUMENT_ID,
        source.strip(),
        as_of_date,
        tuple(points),
        "ready",
    )


def align_external_series(
    bars: Sequence[Mapping[str, Any]],
    series: ExternalMarketSeries | None,
) -> ExternalMarketAlignment:
    """Align exact trading dates, leaving every absent date as ``None``."""

    source = series or _unavailable("缺少本地 VIX 标准序列")
    def bar_trading_date(bar: Mapping[str, Any]) -> str | None:
        for field in ("tradingDate", "trading_date", "barOpenTime", "bar_open_time"):
            parsed = _date_text(bar.get(field))
            if parsed is not None:
                return parsed
        return None

    dates = [bar_trading_date(bar) for bar in bars]
    coverage = {
        "inputBarCount": len(bars),
        "matchedBarCount": 0,
        "sourcePointCount": len(source.points),
        "firstInputDate": next((value for value in dates if value), None),
        "lastInputDate": next((value for value in reversed(dates) if value), None),
    }
    if source.status != "ready":
        return ExternalMarketAlignment(source, tuple(None for _ in bars), (), coverage, "unavailable", source.reason)
    values_by_date = dict(source.points)
    values = tuple(values_by_date.get(value) if value else None for value in dates)
    points = tuple(
        {"barIndex": index, "tradingDate": trading_date, "value": value}
        for index, (trading_date, value) in enumerate(zip(dates, values, strict=True))
        if trading_date is not None and value is not None
    )
    coverage["matchedBarCount"] = len(points)
    if not points:
        return ExternalMarketAlignment(source, values, points, coverage, "unavailable", "当前图表区间没有可对齐的 VIX 交易日")
    return ExternalMarketAlignment(source, values, points, coverage, "ready")


__all__ = [
    "ExternalMarketAlignment",
    "ExternalMarketSeries",
    "VIX_INSTRUMENT_ID",
    "VIX_LOCAL_SERIES_PATH",
    "align_external_series",
    "load_vix_series",
]
