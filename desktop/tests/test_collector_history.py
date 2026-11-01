import json
from pathlib import Path

from market_monitor.collector_history import latest_collector_statuses, save_collector_history
from market_monitor import collector
from market_monitor.manual_data_gaps import collector_activity_items


def test_history_retains_different_sources_when_new_session_has_only_one(tmp_path: Path):
    save_collector_history(tmp_path, {"completed_at": "2026-10-31T01:00:00+00:00", "tasks": [
        {"dataset_id": "MACRO_SERIES", "status": "PARTIAL_FAILURE", "rows": 10, "error": "synthetic-private"},
        {"dataset_id": "THS_CPI", "status": "PASS", "rows": 90},
    ]})
    save_collector_history(tmp_path, {"completed_at": "2026-10-31T02:00:00+00:00", "tasks": [
        {"dataset_id": "THS_CPI", "status": "FAILED", "rows": 0},
    ]})
    result = latest_collector_statuses(tmp_path)
    assert result["MACRO_SERIES"]["status"] == "PARTIAL_FAILURE"
    assert result["THS_CPI"]["status"] == "FAILED" and result["THS_CPI"]["rowsProcessed"] == 0
    assert "synthetic-private" not in json.dumps(result)
    assert all("synthetic-private" not in p.read_text(encoding="utf-8")
               for p in (tmp_path / "state" / "collector_history").glob("*.json"))
    items = {row["key"]: row for row in collector_activity_items(tmp_path)}
    assert items["collector.macro-series"]["collectorActivity"]["status"] == "PARTIAL_FAILURE"


def test_legacy_summary_only_fills_missing_datasets_and_malformed_rows_are_ignored(tmp_path: Path):
    save_collector_history(tmp_path, {"completed_at": "2026-10-31T02:00:00+00:00", "tasks": [
        {"dataset_id": "THS_CPI", "status": "FAILED", "rows": 0},
        {"dataset_id": "../synthetic", "status": "PASS", "rows": 1},
        {"dataset_id": "MACRO_SERIES", "status": "PASS", "rows": True},
    ]})
    (tmp_path / "control_summary.json").write_text(json.dumps({"completed_at": "2026-10-31T01:00:00+00:00", "tasks": [
        {"dataset_id": "THS_CPI", "status": "PASS", "rows": 90},
        {"dataset_id": "MACRO_SERIES", "status": "PASS", "rows": 7},
    ]}), encoding="utf-8")
    result = latest_collector_statuses(tmp_path)
    assert result["THS_CPI"]["status"] == "FAILED"
    assert result["MACRO_SERIES"]["rowsProcessed"] == 7
    assert "../synthetic" not in result
    (tmp_path / "control_summary.json").write_text(json.dumps({"completed_at": "2026-10-31T03:00:00+00:00", "tasks": [
        {"dataset_id": "THS_CPI", "status": "PASS", "rows": 92},
    ]}), encoding="utf-8")
    assert latest_collector_statuses(tmp_path)["THS_CPI"]["rowsProcessed"] == 92


def test_real_session_records_status_after_persistence(tmp_path: Path, monkeypatch):
    result = collector.CollectionTaskResult("MACRO_SERIES", "宏观", "synthetic", status="PASS", rows=12)
    monkeypatch.setattr(collector, "_run_tasks", lambda *args, **kwargs: [result])
    def failed_persistence(_root, results):
        results[0].status = "FAILED"
    monkeypatch.setattr(collector, "_persist_results", failed_persistence)
    summary = collector.run_fetch_session(tmp_path, tasks=[])
    assert summary["source_history_saved"] is True
    assert latest_collector_statuses(tmp_path)["MACRO_SERIES"]["status"] == "FAILED"


def test_macro_freshness_warnings_are_allowlisted_without_provider_secrets(tmp_path: Path):
    save_collector_history(tmp_path, {"completed_at": "2026-10-31T02:00:00+00:00", "tasks": [
        {"dataset_id": "MACRO_SERIES", "status": "PASS", "rows": 12,
         "detail": "synthetic-private PMI_CAIXIN_MANUFACTURING:STALE_OVER_180_DAYS OTHER:STALE_OVER_180_DAYS"},
    ]})
    result = latest_collector_statuses(tmp_path)["MACRO_SERIES"]
    assert result["freshnessWarnings"] == [{"seriesId": "PMI_CAIXIN_MANUFACTURING", "reason": "STALE_OVER_180_DAYS"}]
    assert "synthetic-private" not in json.dumps(result)
