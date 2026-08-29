from datetime import date
import json
from pathlib import Path

import pytest

from market_monitor.futures_calendar import (
    CALENDAR_PROVIDER,
    CALENDAR_RELATIVE_PATH,
    load_futures_trading_calendar,
    sync_futures_trading_calendar,
)


class FakeCalendar:
    def itertuples(self, *, index: bool, name: None):
        assert index is False and name is None
        return iter(
            [
                (date(2026, 9, 30),),
                (date(2026, 10, 1),),
                (date(2026, 10, 9),),
                ("bad",),
                (date(2026, 10, 10),),
            ]
        )


def test_calendar_sync_persists_and_loads_unified_trading_days(tmp_path: Path) -> None:
    summary = sync_futures_trading_calendar(
        tmp_path,
        fetcher=FakeCalendar,
        retrieved_at="2026-08-27T00:00:00+00:00",
    )
    assert summary["tradingDayCount"] == 3
    assert summary["provider"] == CALENDAR_PROVIDER
    payload = json.loads((tmp_path / CALENDAR_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert payload["tradingDays"] == ["2026-09-30", "2026-10-01", "2026-10-09"]

    loaded = load_futures_trading_calendar(tmp_path)
    assert loaded is not None
    assert loaded.between("2026-09-30", "2026-10-10") == [
        "2026-09-30",
        "2026-10-01",
        "2026-10-09",
    ]
    assert loaded.calendar.previous_trading_day("CN_FUTURE", date(2026, 10, 9)) == date(2026, 10, 1)


def test_calendar_missing_and_invalid_cache_remain_explicit(tmp_path: Path) -> None:
    assert load_futures_trading_calendar(tmp_path) is None
    target = tmp_path / CALENDAR_RELATIVE_PATH
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_futures_trading_calendar(tmp_path)
