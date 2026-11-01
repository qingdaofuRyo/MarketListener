"""Verified 2026 mainland stock-exchange closure days for daily A-share data.

The SSE, SZSE and BSE 2026 stock holiday notices agree on these closures.
This is deliberately separate from the Chinese futures trading calendar.
https://www.sse.com.cn/disclosure/dealinstruc/closed/
"""

from __future__ import annotations

from datetime import date, timedelta


def _days_between(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


_SSE_2026_HOLIDAYS = frozenset().union(*(
    _days_between(date(2026, 1, 1), date(2026, 1, 3)),
    _days_between(date(2026, 2, 15), date(2026, 2, 23)),
    _days_between(date(2026, 4, 4), date(2026, 4, 6)),
    _days_between(date(2026, 5, 1), date(2026, 5, 5)),
    _days_between(date(2026, 6, 19), date(2026, 6, 21)),
    _days_between(date(2026, 9, 25), date(2026, 9, 27)),
    _days_between(date(2026, 10, 1), date(2026, 10, 7)),
))


def is_known_a_share_closed_day(day: date) -> bool:
    """Exclude weekends and closures explicitly verified for 2026."""

    return day.weekday() >= 5 or day in _SSE_2026_HOLIDAYS


def has_verified_a_share_calendar(year: int) -> bool:
    """Whether this year's complete exchange closure calendar is known."""

    return year == 2026


def is_verified_a_share_trading_day(day: date) -> bool:
    """The write path needs a complete, year-specific official calendar."""

    return has_verified_a_share_calendar(day.year) and not is_known_a_share_closed_day(day)


__all__ = ("has_verified_a_share_calendar", "is_known_a_share_closed_day", "is_verified_a_share_trading_day")
