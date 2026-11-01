from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from market_monitor import f10
from market_monitor.f10 import _acquire_market_lock, _release_after_fetch, _release_market_lock


def test_f10_lock_releases_on_return_and_exception(tmp_path: Path):
    @_release_after_fetch
    def fetch(root: Path, *, market: str = "CN", fail: bool = False) -> dict:
        assert _acquire_market_lock(root, market)
        if fail:
            raise ValueError("synthetic failure")
        return {"status": "PASS"}

    assert fetch(tmp_path)["status"] == "PASS"
    with pytest.raises(ValueError, match="synthetic"):
        fetch(tmp_path, fail=True)
    assert fetch(tmp_path)["status"] == "PASS"


def test_f10_lock_excludes_same_market_threads_and_processes(tmp_path: Path):
    assert _acquire_market_lock(tmp_path, "CN")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(_acquire_market_lock, tmp_path, "CN").result() is False
        code = ("from pathlib import Path; import sys; "
                "from market_monitor.f10 import _acquire_market_lock; "
                "print(_acquire_market_lock(Path(sys.argv[1]), 'CN'))")
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                                capture_output=True, text=True, timeout=20, check=True)
        assert result.stdout.strip() == "False"
        assert _acquire_market_lock(tmp_path, "HK")
        _release_market_lock(tmp_path, "HK")
    finally:
        _release_market_lock(tmp_path, "CN")
    assert _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_waiting_thread_releases_lock_it_acquires_after_other_thread(tmp_path: Path):
    entered, proceed = threading.Event(), threading.Event()
    @_release_after_fetch
    def fetch(root: Path, *, market: str = "CN") -> dict:
        entered.set()
        assert proceed.wait(5)
        assert _acquire_market_lock(root, market)
        return {"status": "PASS"}
    assert _acquire_market_lock(tmp_path, "CN")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fetch, tmp_path)
        assert entered.wait(5)
        _release_market_lock(tmp_path, "CN")
        proceed.set()
        assert future.result(timeout=5)["status"] == "PASS"
    assert _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_detail_refresh_prioritizes_missing_then_oldest_without_using_done_markers():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    records = {"a": {"detail_fetched_at": "2026-09-01T00:00:00+00:00"},
               "b": {"detail_fetched_at": "2026-01-01T00:00:00+00:00"},
               "c": {"detail_fetched_at": "2027-01-01T00:00:00+00:00"},
               "d": {"detail_fetched_at": "2026-05-01T00:00:00+00:00"}}
    assert f10._pending_detail_codes(["a", "b", "c", "d", "e"], records, now=now,
                                     refresh_after_days=90) == ["e", "c", "b", "d"]
    assert f10._pending_detail_codes(["a", "b", "c", "d", "e"], records, now=now,
                                     refresh_after_days=None) == ["e"]


def test_failed_company_does_not_block_other_pending_companies_and_remains_retryable():
    pending = ["failed-a", "new-b", "failed-c", "new-d"]
    assert f10._order_retry_queue(pending, ["failed-a", "failed-c"]) == [
        "new-b", "new-d", "failed-a", "failed-c"
    ]
    assert f10._order_retry_queue(["failed-a", "failed-c"], ["failed-a", "failed-c"]) == [
        "failed-a", "failed-c"
    ]


def test_revenue_failure_queue_advances_and_rotates_across_batches(tmp_path: Path, monkeypatch):
    codes = [f"{i:06d}" for i in range(18)]
    attempts = []
    def fetch(code):
        attempts.append(code)
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(f10, "fetch_business_analysis", fetch)
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(f10.time, "sleep", lambda _: None)
    for _ in range(3):
        result = f10.run_revenue_fetch(tmp_path, codes=codes, detail_delay_seconds=0.02)
        assert result["status"] == "PARTIAL_FAILURE" and result["failed_codes"] == 8
    assert attempts[:18] == codes
    assert attempts[18:24] == codes[:6]
    queue = f10._load_retry_queue(tmp_path / "f10" / "cn" / "revenue_retry.json")
    assert queue == codes[6:] + codes[:6]


def test_revenue_retry_success_is_removed_from_queue(tmp_path: Path, monkeypatch):
    path = tmp_path / "f10" / "cn" / "revenue_retry.json"
    f10._write_summary(path, {"retry_queue": ["000001"]})
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "fetched_at": f10._now(),
                                                                   "revenue_breakdown": [{"item": "样本", "period": "2026-06-30"}]})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["status"] == "PASS" and f10._load_retry_queue(path) == []


def test_company_failure_queue_rotates_and_migrates_legacy_state(tmp_path: Path, monkeypatch):
    codes = [f"{i:06d}" for i in range(18)]
    attempts = []
    f10._save_state(tmp_path, "CN", {"done": [], "failed": codes[:8]})
    def fetch(code, **_kwargs):
        attempts.append(code)
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(f10, "load_a_share_universe", lambda **_: [{"code": code} for code in codes])
    monkeypatch.setattr(f10, "fetch_company_survey", fetch)
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(f10.time, "sleep", lambda _: None)
    for _ in range(3):
        f10.run_f10_fetch(tmp_path, skip_quotes=True, detail_delay_seconds=0.02)
    assert attempts[:18] == codes[8:] + codes[:8]
    assert attempts[18:24] == codes[8:14]
    assert set(f10._load_state(tmp_path, "CN")["retry_queue"]) == set(codes)


def test_stale_refresh_replaces_existing_company_and_reports_export_failure(tmp_path: Path, monkeypatch):
    inserts = []
    class Store:
        connection = None
        def __init__(self, _root):
            self.connection = self
        def register_default_datasets(self):
            pass
        def execute(self, sql, parameters=None):
            if "INSERT INTO f10_company" in sql:
                inserts.append(parameters[0])
        def commit(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr(f10, "MarketStore", Store)
    monkeypatch.setattr(f10, "load_a_share_universe", lambda **_: [{"code": "000001", "name": "样本"}])
    monkeypatch.setattr(f10, "_load_existing_records", lambda *_, **_kwargs: {
        "000001": {"code": "000001", "detail_fetched_at": "2000-01-01T00:00:00+00:00"}})
    monkeypatch.setattr(f10, "fetch_company_survey", lambda code, **_: {"code": code, "name": "样本"})
    def fail_export(*_args, **_kwargs):
        raise OSError("synthetic export failure")
    monkeypatch.setattr(f10, "export_atlas_f10", fail_export)
    result = f10.run_f10_fetch(tmp_path, skip_quotes=True, detail_delay_seconds=0.02)
    assert inserts == ["000001"]
    assert result["new_details"] == 1 and result["total_details"] == 1
    assert result["status"] == "PARTIAL_FAILURE" and result["failed_codes"] == 1
    assert _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_revenue_refresh_revisits_stale_entry_and_releases_lock(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    (folder / "revenue_20260101.jsonl").write_text(
        json.dumps({"code": "000001", "fetched_at": "2000-01-01T00:00:00+00:00"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "fetched_at": f10._now(),
                                                                   "revenue_breakdown": [{"item": "样本", "period": "2026-06-30"}]})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["new_revenue"] == 1 and result["already_done"] == 1
    assert result["remaining_details"] == 0 and result["status"] == "PASS"
    summary = f10.read_f10_batch_summary(tmp_path, "REVENUE_UPDATE")
    assert summary and summary["updated"] == 1 and summary["remaining"] == 0
    assert _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_f10_summary_exposes_only_safe_aggregate(tmp_path: Path):
    target = tmp_path / "f10" / "cn" / "summary.json"
    f10._write_summary(target, {"status": "PARTIAL", "new_details": 25, "remaining_details": 17,
                               "failed_codes": 0, "completed_at": "2026-10-31T01:50:19+00:00",
                               "errors": [{"code": "synthetic-private"}]})
    result = f10.read_f10_batch_summary(tmp_path, "F10_UPDATE_CN")
    assert result and result["updated"] == 25 and result["remaining"] == 17 and result["failed"] == 0
    assert "synthetic-private" not in json.dumps(result)
    f10._write_summary(target, {"status": "PASS", "new_details": True, "remaining_details": 0,
                               "failed_codes": 0, "completed_at": "2026-10-31T01:50:19+00:00"})
    assert f10.read_f10_batch_summary(tmp_path, "F10_UPDATE_CN") is None


def test_export_preserves_old_snapshot_when_source_is_corrupt(tmp_path: Path):
    source = tmp_path / "f10" / "cn"
    source.mkdir(parents=True)
    (source / "details_20261030.jsonl").write_text('{"code":"000001","name":"样本"}\n', encoding="utf-8")
    (source / "revenue_20261030.jsonl").write_text('{broken\n', encoding="utf-8")
    target = tmp_path / "industry" / "f10" / "cn_f10.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text('previous observation\n', encoding="utf-8")
    with pytest.raises(ValueError, match="revenue records"):
        f10.export_atlas_f10(tmp_path, markets=("CN",))
    assert target.read_text(encoding="utf-8") == 'previous observation\n'


@pytest.mark.parametrize("bad_line", ["{broken", "[]", '{}',
                                        '{"code":"000001","revenue_breakdown":"bad"}',
                                        '{"code":"000001","revenue_breakdown":[null]}'])
def test_revenue_source_corruption_stops_before_fetch_or_mutation(tmp_path: Path, monkeypatch, bad_line):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    path = folder / "revenue_20260101.jsonl"
    path.write_text(bad_line + "\n", encoding="utf-8")
    original = path.read_bytes()
    snapshot = tmp_path / "industry" / "f10" / "cn_f10.jsonl"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("previous observation\n", encoding="utf-8")
    def unexpected_fetch(_code):
        raise AssertionError("must not request provider when local source is corrupt")
    monkeypatch.setattr(f10, "fetch_business_analysis", unexpected_fetch)
    with pytest.raises(ValueError, match="revenue records cannot be read safely"):
        f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert path.read_bytes() == original
    assert snapshot.read_text(encoding="utf-8") == "previous observation\n"
    assert not (folder / "revenue_retry.json").exists()
    assert _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_revenue_detail_corruption_stops_before_fetch(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    (folder / "details_20260101.jsonl").write_text("{broken\n", encoding="utf-8")
    def unexpected_fetch(_code):
        raise AssertionError("must not request provider when details are corrupt")
    monkeypatch.setattr(f10, "fetch_business_analysis", unexpected_fetch)
    with pytest.raises(ValueError, match="detail record is malformed"):
        f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert not list(folder.glob("revenue_*.jsonl"))


def test_revenue_publication_recovers_before_new_fetch(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    marker = folder / "revenue_publication_pending.json"
    snapshot = tmp_path / "industry" / "f10" / "cn_f10.jsonl"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("previous observation\n", encoding="utf-8")
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "fetched_at": f10._now(),
        "revenue_breakdown": [{"item": "样本", "period": "2026-06-30"}]})
    def fail_export(*_args, **_kwargs):
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(f10, "export_atlas_f10", fail_export)
    with pytest.raises(OSError, match="synthetic publication failure"):
        f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert marker.exists() and len(list(folder.glob("revenue_*.jsonl"))) == 1
    assert snapshot.read_text(encoding="utf-8") == "previous observation\n"

    exports = []
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: exports.append("published"))
    def unexpected_fetch(_code):
        raise AssertionError("recovery should not request provider")
    monkeypatch.setattr(f10, "fetch_business_analysis", unexpected_fetch)
    f10.run_revenue_fetch(tmp_path, codes=["000001"], limit=0, detail_delay_seconds=0.02)
    assert exports == ["published"] and not marker.exists()


def test_revenue_parent_timestamp_is_carried_without_changing_original():
    original = {"fetched_at": "2026-10-30T02:00:00+00:00", "revenue_breakdown": [
        {"item": "继承"}, {"item": "独立", "fetched_at": "2026-10-29T02:00:00+00:00"}]}
    rows = f10._revenue_rows_with_fetched_at(original)
    assert rows[0]["fetched_at"] == original["fetched_at"]
    assert rows[1]["fetched_at"] == "2026-10-29T02:00:00+00:00"
    assert "fetched_at" not in original["revenue_breakdown"][0]
    assert "fetched_at" not in f10._revenue_rows_with_fetched_at({
        "fetched_at": "2026-10-30T02:00:00", "revenue_breakdown": [{"item": "无时区"}]
    })[0]


def test_failed_publication_recovery_does_not_start_provider_fetch(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    marker = folder / "revenue_publication_pending.json"
    f10._write_summary(marker, {"started_at": "2026-10-31T00:00:00+00:00"})
    calls = []
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: calls.append(code))
    def fail_export(*_args, **_kwargs):
        raise OSError("synthetic recovery failure")
    monkeypatch.setattr(f10, "export_atlas_f10", fail_export)
    with pytest.raises(OSError, match="synthetic recovery failure"):
        f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert marker.exists() and calls == [] and not list(folder.glob("revenue_*.jsonl"))


def test_standalone_publication_recovery_uses_local_data_and_releases_lock(tmp_path: Path, monkeypatch):
    marker = tmp_path / "f10" / "cn" / "revenue_publication_pending.json"
    f10._write_summary(marker, {"started_at": "2026-10-31T00:00:00+00:00"})
    exports = []
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: exports.append("published"))
    def unexpected_fetch(_code):
        raise AssertionError("local recovery must not request provider")
    monkeypatch.setattr(f10, "fetch_business_analysis", unexpected_fetch)
    assert f10.recover_revenue_publication(tmp_path)["status"] == "PUBLISHED"
    assert not marker.exists() and exports == ["published"]
    assert f10.recover_revenue_publication(tmp_path)["status"] == "NO_PENDING"
    assert _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_standalone_publication_failure_keeps_marker(tmp_path: Path, monkeypatch):
    marker = tmp_path / "f10" / "cn" / "revenue_publication_pending.json"
    f10._write_summary(marker, {"started_at": "2026-10-31T00:00:00+00:00"})
    def fail_export(*_args, **_kwargs):
        raise OSError("synthetic recovery failure")
    monkeypatch.setattr(f10, "export_atlas_f10", fail_export)
    with pytest.raises(OSError, match="synthetic recovery failure"):
        f10.recover_revenue_publication(tmp_path)
    assert marker.exists() and _acquire_market_lock(tmp_path, "CN")
    _release_market_lock(tmp_path, "CN")


def test_empty_revenue_response_does_not_replace_existing(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    (folder / "revenue_20260101.jsonl").write_text(json.dumps({"code": "000001",
        "fetched_at": "2000-01-01T00:00:00+00:00", "revenue_breakdown": [{"item": "原业务"}]}) + "\n", encoding="utf-8")
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "revenue_breakdown": []})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["status"] == "PARTIAL_FAILURE" and result["new_revenue"] == 0
    assert len(list(folder.glob("revenue_*.jsonl"))) == 1


def test_empty_revenue_is_not_fresh_and_prior_empty_entry_is_retried(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    path = folder / "revenue_20261031.jsonl"
    path.write_text(json.dumps({"code": "000001", "fetched_at": f10._now(), "revenue_breakdown": []}) + "\n",
                    encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "revenue_breakdown": []})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["status"] == "PARTIAL_FAILURE" and result["remaining_details"] == 1
    assert result["new_revenue"] == 0 and result["failed_codes"] == 1
    assert path.read_bytes() == original
    assert f10._load_retry_queue(folder / "revenue_retry.json") == ["000001"]


def test_empty_first_revenue_response_is_not_saved_as_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "revenue_breakdown": []})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["new_revenue"] == 0 and result["remaining_details"] == 1
    assert result["status"] == "PARTIAL_FAILURE"
    assert not list((tmp_path / "f10" / "cn").glob("revenue_*.jsonl"))


@pytest.mark.parametrize("new_period", ["2025-12-31", None, "2026-02-30"])
def test_revenue_rejects_regression_or_unknown_report_period(tmp_path: Path, monkeypatch, new_period):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    path = folder / "revenue_20260101.jsonl"
    path.write_text(json.dumps({"code": "000001", "fetched_at": "2000-01-01T00:00:00+00:00",
        "revenue_breakdown": [{"item": "原业务", "period": "2026-06-30"}]}) + "\n", encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code,
        "revenue_breakdown": [{"item": "新业务", "period": new_period}]})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["status"] == "PARTIAL_FAILURE" and result["new_revenue"] == 0
    assert path.read_bytes() == original and len(list(folder.glob("revenue_*.jsonl"))) == 1


def test_revenue_missing_period_is_refetched_even_when_record_is_recent(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    (folder / "revenue_20260101.jsonl").write_text(json.dumps({"code": "000001", "fetched_at": f10._now(),
        "revenue_breakdown": [{"item": "旧业务", "period": None}]}) + "\n", encoding="utf-8")
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code, "fetched_at": f10._now(),
        "revenue_breakdown": [{"item": "新业务", "period": "2026-06-30 00:00:00"}]})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["status"] == "PASS" and result["new_revenue"] == 1


def test_revenue_new_window_retains_verified_older_periods_with_original_fetch_time(tmp_path: Path, monkeypatch):
    folder = tmp_path / "f10" / "cn"
    folder.mkdir(parents=True)
    original = {"code": "000001", "fetched_at": "2026-01-01T00:00:00+00:00", "revenue_breakdown": [
        {"item": "旧期间", "period": "2024-12-31", "income": 5, "currency": "CNY"},
        {"item": "旧同期间", "period": "2025-12-31", "income": 10, "currency": "CNY"},
        {"item": "无法核期", "income": 9, "currency": "CNY"},
    ]}
    (folder / "revenue_20260101.jsonl").write_text(json.dumps(original) + "\n", encoding="utf-8")
    monkeypatch.setattr(f10, "fetch_business_analysis", lambda code: {"code": code,
        "fetched_at": "2026-10-31T00:00:00+00:00", "revenue_breakdown": [
            {"item": "已更正", "period": "2025-12-31", "income": 11, "currency": "CNY"},
            {"item": "新期间", "period": "2026-06-30", "income": 12, "currency": "CNY"},
        ]})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    result = f10.run_revenue_fetch(tmp_path, codes=["000001"], detail_delay_seconds=0.02)
    assert result["status"] == "PASS"
    saved = f10._load_revenue_records(tmp_path, "CN")["000001"]
    rows = saved["revenue_breakdown"]
    assert [row["item"] for row in rows] == ["已更正", "新期间", "旧期间"]
    assert rows[2]["fetched_at"] == "2026-01-01T00:00:00+00:00"
    assert "fetched_at" not in rows[0] and "fetched_at" not in original["revenue_breakdown"][0]
    exported_rows = f10._revenue_rows_with_fetched_at(saved)
    assert exported_rows[0]["fetched_at"] == "2026-10-31T00:00:00+00:00"
    assert exported_rows[2]["fetched_at"] == "2026-01-01T00:00:00+00:00"


def test_company_refresh_is_bounded_and_reports_remaining(tmp_path: Path, monkeypatch):
    class Store:
        def __init__(self, _root): self.connection = self
        def register_default_datasets(self): pass
        def execute(self, *_args): pass
        def commit(self): pass
        def close(self): pass
    codes = [f"{i:06d}" for i in range(30)]
    monkeypatch.setattr(f10, "MarketStore", Store)
    monkeypatch.setattr(f10, "load_a_share_universe", lambda **_: [{"code": code, "name": "样本"} for code in codes])
    monkeypatch.setattr(f10, "fetch_company_survey", lambda code, **_: {"code": code, "name": "样本"})
    monkeypatch.setattr(f10, "export_atlas_f10", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(f10.time, "sleep", lambda _: None)
    result = f10.run_f10_fetch(tmp_path, skip_quotes=True, limit_details=25)
    assert result["new_details"] == 25 and result["remaining_details"] == 5
    assert result["status"] == "PARTIAL"


def test_hk_universe_does_not_fetch_website_when_local_list_missing(tmp_path: Path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("unexpected website request")
    monkeypatch.setattr(f10, "_get_bytes", no_network)
    with pytest.raises(RuntimeError, match="local securities list missing"):
        f10.load_hk_universe(cache_dir=tmp_path)
