from argparse import Namespace
from datetime import datetime
from pathlib import Path
import struct
import subprocess

import pytest

from market_monitor import tdx_afterhours as mod


@pytest.mark.parametrize(("now", "expected"), [
    ("2026-09-08T22:59:59+08:00", "2026-09-07T23:00:00+08:00"),
    ("2026-09-08T23:00:00+08:00", "2026-09-08T23:00:00+08:00"),
    ("2026-09-10T08:00:00+08:00", "2026-09-09T23:00:00+08:00"),
    ("2026-09-08T15:00:00+00:00", "2026-09-08T23:00:00+08:00"),
])
def test_latest_due_slot(now, expected):
    assert mod.latest_slot(datetime.fromisoformat(now)) == expected


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        mod.latest_slot(datetime(2026, 9, 8))


@pytest.mark.parametrize("symbol", ["../abc.SHF", "a.SHF", "CL00Y.UNKNOWN", "6005.SH"])
def test_unknown_paths_rejected(tmp_path, symbol):
    with pytest.raises(ValueError):
        mod.symbol_file(tmp_path, symbol, "1d")


def test_verified_paths(tmp_path):
    assert mod.symbol_file(tmp_path, "RBL8.SHF", "1d") == tmp_path / "vipdoc/ds/lday/30#RBL8.day"
    assert mod.symbol_file(tmp_path, "600519.SH", "5m") == tmp_path / "vipdoc/sh/fzline/sh600519.lc5"
    with pytest.raises(ValueError):
        mod.symbol_file(tmp_path, "RBL8.SHF", "1m")


def test_daily_evidence_is_not_completeness(tmp_path):
    path = tmp_path / "sample.day"
    path.write_bytes(struct.pack("<I", 20260908) + bytes(28))
    evidence = mod.file_evidence(path)
    assert evidence["lastRawTimestamp"] == "2026-09-08T00:00:00"
    assert evidence["records"] == 1
    assert evidence["completeThrough"] is None
    assert evidence["completeness"] == "NOT_VERIFIED"


def test_minute_raw_date_not_reassigned(tmp_path):
    path = tmp_path / "sample.lc5"
    packed = (2026 - 2004) * 2048 + 908
    path.write_bytes(struct.pack("<HH", packed, 23 * 60) + bytes(28))
    assert mod.file_evidence(path)["lastRawTimestamp"] == "2026-09-08T23:00:00"
    path.write_bytes(struct.pack("<HH", packed, 1440) + bytes(28))
    with pytest.raises(ValueError):
        mod.file_evidence(path)


def test_missing_and_truncated(tmp_path):
    path = tmp_path / "sample.day"
    assert mod.file_evidence(path)["status"] == "MISSING"
    for size in (0, 1, 31, 33):
        path.write_bytes(bytes(size))
        assert mod.file_evidence(path)["status"] == "INVALID_RECORD_LENGTH"


@pytest.mark.parametrize("value", [{"ErrorId": 0}, '{"ErrorId":"0","Msg":"ignored"}'])
def test_success_sanitizes_vendor_text(value):
    assert mod.refresh_result(value) == {"ErrorId": "0"}


@pytest.mark.parametrize("value", [{}, {"ErrorId": 1}, None, [], '{"ErrorId":"-1"}'])
def test_failure_does_not_advance_checkpoint(value):
    with pytest.raises(RuntimeError):
        mod.refresh_result(value)


def test_preflight_and_probe_missing_sdk(tmp_path):
    report = mod.probe(tmp_path, ["RBL8.SHF"], "1d", True)
    assert report["status"] == "BLOCKED"
    assert report["download"] == "NOT_RUN"
    assert len(report["missing"]) == 4


def test_timeout_never_claims_download_failure_or_success(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 660
        assert kwargs["stdout"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(args[0], 660)

    monkeypatch.setattr(mod.subprocess, "run", timeout)
    report = mod.run_probe(Namespace(state_dir=tmp_path, download=True), tmp_path, ["RBL8.SHF"], "1d")
    assert report == {"status": "TIMEOUT", "download": "UNKNOWN", "import": "NOT_RUN"}


def test_crashed_worker_cannot_reuse_stale_success(tmp_path, monkeypatch):
    mod.save_report(tmp_path / "worker-result.json", {"status": "API_OK"})
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: Namespace(returncode=9))
    report = mod.run_probe(Namespace(state_dir=tmp_path, download=False), tmp_path, ["RBL8.SHF"], "1d")
    assert report["status"] == "FAILED"
    assert report["workerExitCode"] == 9


@pytest.mark.parametrize("symbols", [[], ["RBL8.SHF", "RBL8.SHF"], ["../bad.SHF"]])
def test_cli_rejects_unbounded_or_unsafe_probe(symbols):
    with pytest.raises(SystemExit) as error:
        mod.main(["probe", *( ["--symbols", *symbols] if symbols else [])])
    assert error.value.code == 2


def test_lock_releases_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with mod.process_lock(tmp_path / "run.lock"):
            raise RuntimeError("synthetic failure")
    with mod.process_lock(tmp_path / "run.lock"):
        assert (tmp_path / "run.lock").exists()
