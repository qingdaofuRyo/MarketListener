from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_monitor import tdx_confirm_cn as confirm
from market_monitor.tdx_afterhours import process_lock
from market_monitor.tdx_download_ui import StopRun, pending_keys, FINANCIAL

SLOT = "2026-10-29T23:00:00+08:00"


def setup_state(root: Path) -> Path:
    state_dir = root / "state" / "tdx_download_ui"
    state_dir.mkdir(parents=True)
    row = {"slot": SLOT, "status": "RUNNING_UNKNOWN", "selectionHash": None,
           "datePolicy": "LOCAL_COVERAGE_START_CLIENT_END", "startDate": "2026-10-28"}
    path = state_dir / "checkpoints.json"
    path.write_text(json.dumps({"financial": {"cn-day": dict(row), "cn-5m": dict(row)},
                                "futures": {}}), encoding="utf-8")
    return path


class FakeDriver:
    def __init__(self, _exe):
        self.app = SimpleNamespace(process=1234)
        self.dialog = None

    def connect(self): pass
    def find_dialog(self):
        return SimpleNamespace(process_id=lambda: 1234, class_name=lambda: "#32770",
                               window_text=lambda: "盘后数据下载", is_visible=lambda: True,
                               is_enabled=lambda: True)
    def ensure_connected_process(self): pass
    def ensure_idle(self): pass
    def status(self): return ("下载完毕.", "开始下载")
    def tab_names(self): return ["沪深京日线*", "沪深京分钟线*", "扩展市场行情日线"]


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setattr(confirm, "latest_slot", lambda _now: SLOT)
    monkeypatch.setattr(confirm, "load_auto_owned", lambda *_args: (1234, "synthetic"))


def test_user_assertion_records_separate_audit_and_only_extended_pending(tmp_path: Path):
    path = setup_state(tmp_path)
    result = confirm.confirm_financial_cn(tmp_path, SLOT, user_confirmed=True, driver_factory=FakeDriver)
    rows = json.loads(path.read_text(encoding="utf-8"))["financial"]
    assert result["rawCoverage"] == "UNVERIFIED"
    assert [b.key for b in pending_keys(FINANCIAL, rows, SLOT)] == ["extended-day", "extended-5m"]
    for name in ("cn-day", "cn-5m"):
        assert rows[name]["status"] == "UI_DONE_UNVERIFIED"
        assert rows[name]["completionEvidence"]["method"] == "USER_CONFIRMED_AND_WINDOW_VALIDATED"
        assert "finishedAt" not in rows[name]
    files = list((path.parent / "user_confirmations").glob("*.json"))
    assert len(files) == 1
    audit = json.loads(files[0].read_text())
    assert audit["previousStatus"] == "RUNNING_UNKNOWN"
    assert audit["auditId"] == rows["cn-day"]["completionEvidence"]["auditId"]


@pytest.mark.parametrize("case", ["no_consent", "identity", "busy", "wrong_tabs", "mismatch"])
def test_rejection_leaves_original_checkpoint_untouched(tmp_path: Path, monkeypatch, case: str):
    path = setup_state(tmp_path)
    if case == "identity":
        monkeypatch.setattr(confirm, "load_auto_owned", lambda *_args: None)
    if case == "mismatch":
        state = json.loads(path.read_text()); state["financial"]["cn-5m"]["startDate"] = "2026-10-27"
        path.write_text(json.dumps(state), encoding="utf-8")
    class Driver(FakeDriver):
        def status(self):
            return ("正在下载", "取消下载") if case == "busy" else super().status()
        def tab_names(self):
            return ["沪深京日线*"] if case == "wrong_tabs" else super().tab_names()
    before = path.read_bytes()
    with pytest.raises(StopRun):
        confirm.confirm_financial_cn(tmp_path, SLOT, user_confirmed=case != "no_consent", driver_factory=Driver)
    assert path.read_bytes() == before
    assert not (path.parent / "user_confirmations").exists()


def test_active_futures_worker_blocks_confirmation(tmp_path: Path):
    path = setup_state(tmp_path)
    before = path.read_bytes()
    with process_lock(path.parent / "futures.run.lock"):
        with pytest.raises(OSError):
            confirm.confirm_financial_cn(tmp_path, SLOT, user_confirmed=True, driver_factory=FakeDriver)
    assert path.read_bytes() == before
