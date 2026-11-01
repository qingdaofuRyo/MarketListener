"""CFFEX parameter publications are event-driven, not daily missing facts."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from market_monitor import manual_data_gaps
from market_monitor.futures_calendar import FuturesTradingCalendar
from market_monitor.calendar import CalendarDay, TradingCalendar


def test_cffex_no_new_sheet_is_not_a_daily_margin_gap(tmp_path: Path, monkeypatch) -> None:
    days = ("2026-09-14", "2026-09-18", "2026-09-21", "2026-10-16")
    calendar = TradingCalendar(
        CalendarDay("CN_FUTURE", date.fromisoformat(day), True) for day in days
    )
    observed = FuturesTradingCalendar(
        calendar=calendar, trading_days=days,
        provider="synthetic", retrieved_at="2026-10-16T10:00:00+08:00",
    )
    monkeypatch.setattr(manual_data_gaps, "load_futures_trading_calendar", lambda _: observed)
    monkeypatch.setattr(manual_data_gaps, "_covered_exchange_days", lambda _: (
        {"daily": set(), "ranking": set(), "settlement": {
            ("2026-09-14", "CFFEX"), ("2026-09-21", "CFFEX")}},
        {"daily": set(), "ranking": set(), "settlement": {
            ("2026-09-18", "CFFEX"), ("2026-10-16", "CFFEX")}},
        None,
    ))
    monkeypatch.setattr(manual_data_gaps, "datetime", type("Clock", (), {
        "now": staticmethod(lambda tz: datetime(2026, 10, 18, 18, tzinfo=timezone(timedelta(hours=8))))
    }))

    item = manual_data_gaps.official_gap_items(
        tmp_path, source="official.settlement.cffex",
        start="2026-09-14", end="2026-10-16",
    )[0]
    assert item["dateSemantics"] == "LATEST_VERIFIED_PUBLICATION"
    assert item["latestDate"] == "2026-09-21"
    assert item["noNewPublicationDays"] == 2
    assert item["missingCount"] == 0
    assert item["unavailableCount"] == 0
