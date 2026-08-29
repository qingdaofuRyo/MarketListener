"""Persisted, data-driven trading calendar used by Chinese futures Gold jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Sequence

from .calendar import CalendarDay, TradingCalendar


CALENDAR_SCHEMA = "market-listener/cn-futures-trading-calendar/v1"
CALENDAR_PROVIDER = "akshare.tool_trade_date_hist_sina"
CALENDAR_RELATIVE_PATH = Path("state") / "cn_futures_trading_calendar.json"
CALENDAR_MARKET = "CN_FUTURE"

CalendarFetcher = Callable[[], Any]


@dataclass(frozen=True)
class FuturesTradingCalendar:
    calendar: TradingCalendar
    trading_days: tuple[str, ...]
    provider: str
    retrieved_at: str

    def between(self, start: str, end: str) -> list[str]:
        return [
            value.isoformat()
            for value in self.calendar.trading_days_between(
                CALENDAR_MARKET,
                date.fromisoformat(start),
                date.fromisoformat(end),
            )
        ]


def sync_futures_trading_calendar(
    data_root: Path,
    *,
    fetcher: CalendarFetcher | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Refresh the shared CN trading-day list without coupling it to API reads."""

    table = (fetcher or _akshare_calendar)()
    days = sorted({value for row in _positional_rows(table) if (value := _day(row[0])) is not None})
    if not days:
        raise ValueError("trading calendar provider returned no valid dates")
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "schema": CALENDAR_SCHEMA,
        "provider": CALENDAR_PROVIDER,
        "market": CALENDAR_MARKET,
        "retrievedAt": timestamp,
        "tradingDays": days,
    }
    path = Path(data_root) / CALENDAR_RELATIVE_PATH
    _atomic_write_json(path, payload)
    return {
        "status": "UPDATED",
        "path": str(path),
        "tradingDayCount": len(days),
        "startDay": days[0],
        "endDay": days[-1],
        "provider": CALENDAR_PROVIDER,
    }


def load_futures_trading_calendar(data_root: Path) -> FuturesTradingCalendar | None:
    """Load the persisted calendar; missing evidence stays explicitly absent."""

    path = Path(data_root) / CALENDAR_RELATIVE_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError(f"invalid futures trading calendar: {path}") from error
    if not isinstance(payload, dict) or payload.get("schema") != CALENDAR_SCHEMA:
        raise ValueError("unsupported futures trading calendar schema")
    if payload.get("provider") != CALENDAR_PROVIDER or payload.get("market") != CALENDAR_MARKET:
        raise ValueError("unexpected futures trading calendar identity")
    retrieved_at = payload.get("retrievedAt")
    raw_days = payload.get("tradingDays")
    if not isinstance(retrieved_at, str) or not isinstance(raw_days, list):
        raise ValueError("invalid futures trading calendar metadata")
    days = tuple(sorted({_required_day(value) for value in raw_days}))
    entries = [
        CalendarDay(
            market=CALENDAR_MARKET,
            calendar_date=date.fromisoformat(value),
            is_trading_day=True,
            provider=CALENDAR_PROVIDER,
            retrieved_at=retrieved_at,
        )
        for value in days
    ]
    return FuturesTradingCalendar(
        calendar=TradingCalendar(entries),
        trading_days=days,
        provider=CALENDAR_PROVIDER,
        retrieved_at=retrieved_at,
    )


def _akshare_calendar() -> Any:
    import akshare as ak

    return ak.tool_trade_date_hist_sina()


def _positional_rows(table: Any) -> Iterable[Sequence[Any]]:
    iterator = getattr(table, "itertuples", None)
    if callable(iterator):
        yield from iterator(index=False, name=None)
        return
    if isinstance(table, Iterable) and not isinstance(table, (str, bytes, dict)):
        for row in table:
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and row:
                yield row


def _day(value: Any) -> str | None:
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed.weekday() < 5 else None


def _required_day(value: Any) -> str:
    parsed = _day(value)
    if parsed is None:
        raise ValueError(f"invalid trading calendar day: {value!r}")
    return parsed


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
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


__all__ = (
    "CALENDAR_MARKET",
    "CALENDAR_PROVIDER",
    "CALENDAR_RELATIVE_PATH",
    "CALENDAR_SCHEMA",
    "FuturesTradingCalendar",
    "load_futures_trading_calendar",
    "sync_futures_trading_calendar",
)
