"""The isolated collector must preserve legitimate non-publishing outcomes."""

from dataclasses import asdict

from market_monitor.collector import CollectionTaskResult
from market_monitor.collector_isolation import WIRE_VERSION, _decode_results


def test_isolated_a_share_pre_close_skip_remains_skipped() -> None:
    result = CollectionTaskResult(
        dataset_id="A_SHARE_BREADTH",
        dataset_name="A股涨跌/市值快照",
        source="akshare",
        status="SKIPPED",
        rows=0,
        detail="北京时间15:30前；不写日度数据",
        started_at="2026-10-21T08:26:46+08:00",
        completed_at="2026-10-21T08:26:46+08:00",
    )
    payload = {
        "version": WIRE_VERSION,
        "dataset_id": "A_SHARE_BREADTH",
        "results": [asdict(result)],
    }

    decoded = _decode_results(payload, "A_SHARE_BREADTH")

    assert len(decoded) == 1
    assert decoded[0].status == "SKIPPED"
    assert decoded[0].persist.gold_metrics == []
