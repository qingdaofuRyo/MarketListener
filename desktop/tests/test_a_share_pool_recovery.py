"""Recent dated pool repair keeps missing and preliminary days honest."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb

from market_monitor import collector
from market_monitor.a_share_pool_recovery import (recent_pool_candidates,
                                                   recover_recent_a_share_open_returns,
                                                   recover_recent_a_share_pools)


def test_recent_pool_candidates_choose_recent_missing_days(tmp_path: Path) -> None:
    sunday = datetime(2026, 10, 25, 20, tzinfo=timezone(timedelta(hours=8)))
    assert recent_pool_candidates(tmp_path, now=sunday, lookback_days=6, max_days=2) == [
        "20261022", "20261023",
    ]
    assert recent_pool_candidates(tmp_path, now=sunday.replace(hour=18)) == []


def test_complete_historical_pool_is_published_and_partial_is_not(
    tmp_path: Path, monkeypatch,
) -> None:
    sunday = datetime(2026, 10, 25, 20, tzinfo=timezone(timedelta(hours=8)))
    calls: list[str] = []

    def complete(day: str) -> SimpleNamespace:
        calls.append(day)
        methods = {"涨停_COUNT": "source=stock_zt_pool_em",
                   "涨停_MAX_LIANBAN": "source=stock_zt_pool_em",
                   "跌停_COUNT": "source=stock_zt_pool_dtgc_em",
                   "跌停_MAX_LIANBAN": "source=stock_zt_pool_dtgc_em",
                   "炸板_COUNT": "source=stock_zt_pool_zbgc_em; unique=code; close<limit_up; bursts>=1"}
        metrics = [collector._metric("CN_ZT_POOL", "CN.A_SHARE.测试", day, "1d", key,
                                     value, "test", methods[key], metric_key=key)
                   for key, value in (("涨停_COUNT", 46), ("涨停_MAX_LIANBAN", 3),
                                      ("跌停_COUNT", 22), ("跌停_MAX_LIANBAN", 3),
                                      ("炸板_COUNT", 20))]
        return SimpleNamespace(status="PASS", persist=SimpleNamespace(gold_metrics=metrics))

    monkeypatch.setattr(collector, "_collect_zt_pool", complete)
    result = recover_recent_a_share_pools(tmp_path, now=sunday, max_days=1)
    assert result["recoveredDays"] == ["20261023"]
    assert result["metricCount"] == 5
    assert calls == ["20261023"]
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gold_metrics").fetchone()[0] == 5

    def partial(day: str) -> SimpleNamespace:
        value = complete(day)
        value.status = "PARTIAL_FAILURE"
        return value

    monkeypatch.setattr(collector, "_collect_zt_pool", partial)
    result = recover_recent_a_share_pools(tmp_path, now=sunday, max_days=1)
    assert result["failedDays"] == ["20261022"]
    assert result["metricCount"] == 0


def test_pool_candidates_recheck_complete_but_old_or_wrong_source(tmp_path: Path) -> None:
    sunday = datetime(2026, 10, 25, 20, tzinfo=timezone(timedelta(hours=8)))
    from market_monitor.storage import MarketStore

    store = MarketStore(tmp_path)
    try:
        metrics = []
        for day in ("20261022", "20261023"):
            for key, method in (("涨停_COUNT", "source=stock_zt_pool_em"),
                                ("涨停_MAX_LIANBAN", "source=stock_zt_pool_em"),
                                ("跌停_COUNT", "source=stock_zt_pool_dtgc_em"),
                                ("跌停_MAX_LIANBAN", "source=stock_zt_pool_dtgc_em"),
                                ("炸板_COUNT", "source=stock_zt_pool_zbgc_em; unique=code; close<limit_up; bursts>=1")):
                metrics.append(collector._metric("CN_ZT_POOL", "CN.A_SHARE.测试", day, "1d", key,
                                                 10, "test", method, metric_key=key))
        store.upsert_gold_metrics(metrics)
    finally:
        store.close()
    assert recent_pool_candidates(tmp_path, now=sunday, lookback_days=3, max_days=2) == [
        "20261022", "20261023",
    ]
    with duckdb.connect(str(tmp_path / "catalog.duckdb")) as connection:
        connection.execute("UPDATE gold_metrics SET calculation_method='source=legacy' "
                           "WHERE trading_date='20261022' AND metric_id LIKE '%:炸板_COUNT'")
    assert "20261022" in recent_pool_candidates(tmp_path, now=sunday, lookback_days=3, max_days=2)


def test_open_return_recovery_moves_to_next_missing_day(tmp_path: Path, monkeypatch) -> None:
    sunday = datetime(2026, 10, 25, 20, tzinfo=timezone(timedelta(hours=8)))
    calls: list[str] = []

    def complete(day: str) -> SimpleNamespace:
        calls.append(day)
        metric = collector._metric(
            "CN_ZT_POOL_OPEN_RETURN_V2", "CN.A_SHARE.YESTERDAY_LIMIT_UP", day, "1d",
            "昨日涨停今日接盘收益率(%)", 2.5, "test",
            "source=eastmoney-previous-pool+tencent-dated-daily; pool_count=10; priced_count=10; "
            "formula=(close-open)/open*100; same_day_close_checked=true",
            metric_key="YESTERDAY_LIMIT_UP_OPEN_RETURN",
        )
        return SimpleNamespace(status="PASS", persist=SimpleNamespace(gold_metrics=[metric]))

    monkeypatch.setattr(collector, "_collect_zt_open_return", complete)
    result = recover_recent_a_share_open_returns(tmp_path, now=sunday, max_days=1)
    assert result["recoveredDays"] == ["20261023"]
    assert calls == ["20261023"]
    result = recover_recent_a_share_open_returns(tmp_path, now=sunday, max_days=1)
    assert result["recoveredDays"] == ["20261022"]
    assert calls == ["20261023", "20261022"]


def test_open_return_failed_latest_does_not_block_older_and_weak_coverage_is_retried(
    tmp_path: Path, monkeypatch,
) -> None:
    from market_monitor.storage import MarketStore

    sunday = datetime(2026, 10, 25, 20, tzinfo=timezone(timedelta(hours=8)))
    weak = collector._metric("CN_ZT_POOL_OPEN_RETURN_V2", "CN.A_SHARE.YESTERDAY_LIMIT_UP",
                             "20261023", "1d", "昨日涨停今日接盘收益率(%)", 2.5,
                             "test", "source=eastmoney-previous-pool+tencent-dated-daily; "
                             "pool_count=20; priced_count=2; formula=(close-open)/open*100; "
                             "same_day_close_checked=true", metric_key="YESTERDAY_LIMIT_UP_OPEN_RETURN")
    store = MarketStore(tmp_path)
    try:
        store.upsert_gold_metrics([weak])
    finally:
        store.close()
    calls: list[str] = []

    def result(day: str) -> SimpleNamespace:
        calls.append(day)
        if day == "20261023":
            return SimpleNamespace(status="PARTIAL_FAILURE", persist=SimpleNamespace(gold_metrics=[]))
        metric = dict(weak, trading_date=day,
                      metric_id=weak["metric_id"].replace("20261023", day),
                      calculation_method="source=eastmoney-previous-pool+tencent-dated-daily; "
                      "pool_count=20; priced_count=19; formula=(close-open)/open*100; "
                      "same_day_close_checked=true")
        return SimpleNamespace(status="PASS", persist=SimpleNamespace(gold_metrics=[metric]))

    monkeypatch.setattr(collector, "_collect_zt_open_return", result)
    recovered = recover_recent_a_share_open_returns(tmp_path, now=sunday, max_days=2)
    assert recovered["checkedDays"] == ["20261023", "20261022"]
    assert recovered["failedDays"] == ["20261023"]
    assert recovered["recoveredDays"] == ["20261022"]
    assert calls == ["20261023", "20261022"]
    second = recover_recent_a_share_open_returns(tmp_path, now=sunday, max_days=2)
    assert second["checkedDays"] == ["20261023", "20261021"]
