from __future__ import annotations

from datetime import date
from pathlib import Path

from market_monitor import a_share_silver_recovery as recovery
from market_monitor.collector import _metric
from market_monitor.storage import MarketStore


def test_recovery_replaces_preclose_observation_and_is_idempotent(
    tmp_path: Path, monkeypatch,
) -> None:
    day = "2026-10-20"
    monkeypatch.setattr(recovery, "_beijing_today", lambda: date(2026, 10, 22))
    store = MarketStore(tmp_path)
    try:
        store.upsert_gold_metrics([_metric(
            "A_SHARE_BREADTH", "CN.A_SHARE.BREADTH", day, "1d", "当日成交额(亿)", 99,
            "invalid early snapshot", "source=stock_zh_a_spot_tx", metric_key="TOTAL_AMOUNT_YI",
            timestamp=f"{day}T08:49:00+08:00",
        )])
    finally:
        store.close()
    monkeypatch.setattr(recovery, "_silver_rows", lambda *_: [
        (day, 5500, 5490, 14_715.3, 3700, 150, 1640),
    ])
    first = recovery.recover_recent_a_share_breadth(tmp_path)
    second = recovery.recover_recent_a_share_breadth(tmp_path)
    assert first == {"recoveredDays": [day], "metricCount": 4}
    assert second == {"recoveredDays": [], "metricCount": 0}
    store = MarketStore(tmp_path)
    try:
        rows = store.connection.execute("""
            SELECT metric_id, value, calculation_method FROM gold_metrics
            WHERE metric_id LIKE 'A_SHARE_BREADTH:%' ORDER BY metric_id
        """).fetchall()
    finally:
        store.close()
    assert len(rows) == 4
    assert rows[0][2].startswith("source=tdx-cn-v2; historical_silver_recovery;")
    assert any(metric_id.endswith(":TOTAL_AMOUNT_YI") and value == 14_715.3
               for metric_id, value, _ in rows)


def test_recovery_requires_near_complete_pairing(tmp_path: Path, monkeypatch) -> None:
    day = "2026-10-20"
    monkeypatch.setattr(recovery, "_beijing_today", lambda: date(2026, 10, 22))
    monkeypatch.setattr(recovery, "_silver_rows", lambda *_: [(day, 5500, 5000, 12_000, 2500, 100, 2400)])
    assert recovery.recover_recent_a_share_breadth(tmp_path)["metricCount"] == 0
