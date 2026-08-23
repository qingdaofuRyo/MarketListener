from datetime import datetime, timezone

from market_monitor.aggregation import aggregate_bars, aggregate_daily_bars


def minute(open_time, close_time, trading_day="2026-08-03", close=100.0):
    return {"trading_day": trading_day, "bar_open_time": open_time, "bar_close_time": close_time, "period": "15m", "open": close - 1, "high": close + 1, "low": close - 2, "close": close, "volume": 10, "amount": 100, "open_interest": None}


def daily(day, code, open_price=100.0, high=101.0, low=99.0, close=100.5, volume=10.0, amount=1000.0, oi=5.0):
    return {
        "instrument_key": {"country_or_market": "CN", "exchange": "SSE", "asset_type": "STOCK", "code": code},
        "instrument_id": code,
        "trading_day": day,
        "period": "1d",
        "bar_open_time": f"{day}T09:30:00+08:00",
        "bar_close_time": f"{day}T15:00:00+08:00",
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
        "open_interest": oi,
    }


def test_a_share_lunch_break_never_merges_into_a_single_hour_bar():
    rows = [minute("2026-08-03T11:15:00+08:00", "2026-08-03T11:30:00+08:00"), minute("2026-08-03T13:00:00+08:00", "2026-08-03T13:15:00+08:00", close=110)]
    result = aggregate_bars(rows, 60, "CN_STOCK")
    assert len(result) == 2
    assert result[0]["is_partial"] is False
    assert result[1]["is_partial"] is True
    assert result[0]["bar_close_time"] == "2026-08-03T11:30:00+08:00"


def test_tail_bar_is_marked_partial_and_ohlcv_is_aggregated():
    rows = [minute("2026-08-03T09:30:00+08:00", "2026-08-03T09:45:00+08:00", close=100), minute("2026-08-03T09:45:00+08:00", "2026-08-03T10:00:00+08:00", close=105)]
    result = aggregate_bars(rows, 60, "CN_STOCK")
    assert len(result) == 1
    assert result[0]["open"] == 99 and result[0]["close"] == 105
    assert result[0]["high"] == 106.0 and result[0]["low"] == 98.0
    assert result[0]["volume"] == 20.0 and result[0]["is_partial"]


def test_future_night_session_belongs_to_next_trading_day():
    rows = [minute("2026-08-03T21:00:00+08:00", "2026-08-03T21:15:00+08:00", trading_day="2026-08-03"), minute("2026-08-04T09:00:00+08:00", "2026-08-04T09:15:00+08:00", trading_day="2026-08-04")]
    result = aggregate_bars(rows, 30, "CN_FUTURE")
    assert [item["trading_day"] for item in result] == ["2026-08-04", "2026-08-04"]


def test_future_extended_night_profiles_accept_0100_and_0230_without_crossing_breaks():
    one_am = [minute("2026-08-03T23:45:00+08:00", "2026-08-04T00:00:00+08:00", trading_day="2026-08-03"), minute("2026-08-04T00:45:00+08:00", "2026-08-04T01:00:00+08:00", trading_day="2026-08-04")]
    result = aggregate_bars(one_am, 60, "CN_FUTURE_0100")
    assert len(result) == 2
    assert all(item["trading_day"] == "2026-08-04" for item in result)

    two_thirty = [minute("2026-08-04T02:15:00+08:00", "2026-08-04T02:30:00+08:00", trading_day="2026-08-04")]
    result = aggregate_bars(two_thirty, 60, "CN_FUTURE_0230")
    assert len(result) == 1 and result[0]["is_partial"] is False


def test_daily_bars_aggregate_to_weekly():
    days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
    rows = [daily(day, "600519") for day in days]
    result = aggregate_daily_bars(rows, "1w")
    assert len(result) == 1
    bar = result[0]
    assert bar["period"] == "1w"
    assert bar["trading_day"] == "2026-08-07"
    assert bar["bar_open_time"] == "2026-08-03T09:30:00+08:00"
    assert bar["bar_close_time"] == "2026-08-07T15:00:00+08:00"
    assert bar["open"] == 100.0 and bar["close"] == 100.5
    assert bar["high"] == 101.0 and bar["low"] == 99.0
    assert bar["volume"] == 50.0 and bar["amount"] == 5000.0
    assert bar["open_interest"] == 5.0
    assert bar["aggregated_from"] == "1d"


def test_daily_bars_aggregate_to_monthly_without_mixing_instruments():
    rows = [
        daily("2026-07-31", "600519", close=10.0, open_price=9.0, high=10.5, low=8.5),
        daily("2026-08-03", "600519", close=11.0, open_price=10.0, high=11.5, low=9.5),
        daily("2026-08-04", "000001", close=12.0, open_price=11.0, high=12.5, low=10.5),
    ]
    result = aggregate_daily_bars(rows, "1mo")
    assert len(result) == 3
    july_600519 = next(bar for bar in result if bar["trading_day"] == "2026-07-31")
    assert july_600519["period"] == "1mo"
    assert july_600519["open"] == 9.0 and july_600519["close"] == 10.0
    assert july_600519["high"] == 10.5 and july_600519["low"] == 8.5
    assert july_600519["volume"] == 10.0
    august_000001 = next(bar for bar in result if "000001" in str(bar["instrument_key"]))
    assert august_000001["trading_day"] == "2026-08-04"


def test_daily_bars_aggregate_to_quarterly_and_yearly_without_mixing_instruments():
    rows = [
        daily("2026-01-30", "600519", open_price=10.0, high=12.0, low=9.0, close=11.0),
        daily("2026-03-31", "600519", open_price=11.0, high=14.0, low=10.0, close=13.0),
        daily("2026-04-01", "600519", open_price=13.0, high=15.0, low=12.0, close=14.0),
        daily("2026-04-01", "000001", open_price=20.0, high=21.0, low=19.0, close=20.5),
    ]
    quarters = aggregate_daily_bars(rows, "1q")
    assert len(quarters) == 3
    first_quarter = next(item for item in quarters if item["trading_day"] == "2026-03-31")
    assert first_quarter["open"] == 10.0 and first_quarter["close"] == 13.0
    assert first_quarter["high"] == 14.0 and first_quarter["low"] == 9.0
    years = aggregate_daily_bars(rows, "1y")
    stock_year = next(item for item in years if "600519" in str(item["instrument_key"]))
    assert stock_year["open"] == 10.0 and stock_year["close"] == 14.0


def test_weekly_aggregation_marks_partial_when_now_is_given():
    rows = [daily("2026-08-03", "600519")]
    result = aggregate_daily_bars(rows, "1w", now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc))
    assert result[0]["is_partial"] is True


def test_daily_aggregation_rejects_unknown_period():
    try:
        aggregate_daily_bars([], "2y")
    except ValueError as exc:
        assert "output_period" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_optional_fields_remain_null_when_any_input_is_missing():
    rows = [
        minute("2026-08-03T09:30:00+08:00", "2026-08-03T09:45:00+08:00", close=100),
        minute("2026-08-03T09:45:00+08:00", "2026-08-03T10:00:00+08:00", close=101),
    ]
    rows[1]["volume"] = None
    rows[1]["amount"] = None
    rows[1]["high"] = None
    rows[1]["low"] = None
    result = aggregate_bars(rows, 60, "CN_STOCK")
    assert result[0]["volume"] is None
    assert result[0]["amount"] is None
    assert result[0]["high"] is None
    assert result[0]["low"] is None
