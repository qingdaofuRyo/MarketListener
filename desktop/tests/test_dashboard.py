from datetime import datetime, timedelta, timezone

from market_monitor.dashboard import build_health_report, render_markdown
from market_monitor.quality import quarantine_partition, validate_partition
from market_monitor.storage import MarketStore, PartitionKey


def test_dashboard_surfaces_failed_runs_stale_partitions_and_quarantine(tmp_path) -> None:
    data_root = tmp_path / "data"
    store = MarketStore(data_root)
    run_id = store.begin_run("test_provider")
    store.finish_run(run_id, "FAILED", "network down")
    key = PartitionKey("CN", "STOCK", "1d", 2026, "CN-STOCK-1d-2026")
    store.write_silver_bars(
        key,
        [{"instrument_key": {"country_or_market": "CN", "exchange": "SSE", "asset_type": "STOCK", "code": "600519"}, "period": "1d", "bar_open_time": "2026-08-03T09:30:00+08:00", "close": 100.0}],
        "2026-08-03T15:00:00+08:00",
        run_id,
    )
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    store.connection.execute("UPDATE partitions SET updated_at=? WHERE partition_id=?", (old, key.partition_id))
    store.connection.commit()
    store.close()

    report = validate_partition("bad-partition", [], "2026-08-03T15:00:00+08:00")
    quarantine_partition(data_root, "bad-partition", [], report)

    health = build_health_report(data_root, stale_after_seconds=3600)

    assert any(source["status"] == "FAILED" and "network down" in source["detail"] for source in health.sources)
    assert any(partition["partition_id"] == key.partition_id and partition["stale"] for partition in health.partitions)
    assert any(entry["partition_id"] == "bad-partition" for entry in health.quarantine)
    assert health.storage["silver"] > 0
    markdown = render_markdown(health)
    assert "network down" in markdown and "bad-partition" in markdown


def test_dashboard_marks_malformed_quarantine_reports_as_blocking(tmp_path) -> None:
    data_root = tmp_path / "data"
    target = data_root / "quarantine" / "broken"
    target.mkdir(parents=True)
    (target / "quality-report.json").write_text("{not json", encoding="utf-8")

    health = build_health_report(data_root)

    assert any(entry["partition_id"] == "broken" and entry["blocking"] for entry in health.quarantine)


def test_dashboard_stays_available_when_another_process_holds_catalog(monkeypatch, tmp_path) -> None:
    class LockedStore:
        def __init__(self, _root):
            raise OSError("catalog is locked")

    monkeypatch.setattr("market_monitor.dashboard.MarketStore", LockedStore)
    health = build_health_report(tmp_path / "data")
    assert health.sources[0]["run_id"] == "catalog-busy"
    assert health.sources[0]["status"] == "RUNNING"
