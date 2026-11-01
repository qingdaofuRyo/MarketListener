"""A stranded GUI checkpoint can be retried only after its app is absent."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from market_monitor.operations import Operation, OperationKind, OperationStatus
from market_monitor import tdx_unknown_recovery


def _state(root: Path) -> Path:
    path = root / "state" / "tdx_download_ui" / "checkpoints.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "financial": {
            "cn-day": {"slot": "2026-10-24T23:00:00+08:00", "status": "UI_DONE_UNVERIFIED"},
            "extended-day": {"slot": "2026-10-24T23:00:00+08:00",
                             "status": "RUNNING_UNKNOWN", "startDate": "2026-10-23"},
            "extended-5m": {"slot": "2026-10-24T23:00:00+08:00",
                            "status": "RUNNING_UNKNOWN", "startDate": "2026-10-23"},
        },
        "futures": {"future-day": {"slot": "2026-10-24T23:00:00+08:00",
                                   "status": "UI_DONE_UNVERIFIED"}},
    }), encoding="utf-8")
    return path


def test_absent_exact_terminal_marks_only_unknown_batches_interrupted(tmp_path: Path) -> None:
    path = _state(tmp_path)
    seen: list[Path] = []
    result = tdx_unknown_recovery.reconcile_unknown_download(
        tmp_path, "financial", [], process_probe=lambda exe: seen.append(exe) or False,
        observed_at=datetime(2026, 10, 25, 1, tzinfo=timezone.utc),
    )
    assert result == {"status": "INTERRUPTED_UNVERIFIED", "terminal": "financial",
                      "batches": ["extended-day", "extended-5m"]}
    assert seen == [Path(r"C:\tongdaxin") / "tdxw.exe"]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["financial"]["cn-day"]["status"] == "UI_DONE_UNVERIFIED"
    assert stored["futures"]["future-day"]["status"] == "UI_DONE_UNVERIFIED"
    for key in ("extended-day", "extended-5m"):
        item = stored["financial"][key]
        assert item["status"] == "INTERRUPTED_UNVERIFIED"
        assert item["startDate"] == "2026-10-23"
        assert item["reconciliation"] == {
            "previousStatus": "RUNNING_UNKNOWN",
            "reason": "TERMINAL_PROCESS_ABSENT",
            "observedAt": "2026-10-25T09:00:00+08:00",
        }
    assert tdx_unknown_recovery.reconcile_unknown_download(
        tmp_path, "financial", [], process_probe=lambda _exe: False,
    )["status"] == "NO_UNKNOWN_BATCH"


def test_active_gui_operation_or_running_terminal_preserves_unknown(tmp_path: Path) -> None:
    path = _state(tmp_path)
    before = path.read_bytes()
    active = Operation("active", OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
                       OperationStatus.RUNNING, "2026-10-24T15:00:00+00:00")
    assert tdx_unknown_recovery.reconcile_unknown_download(
        tmp_path, "financial", [active],
        process_probe=lambda _exe: (_ for _ in ()).throw(AssertionError("must not probe")),
    )["status"] == "ACTIVE_GUI_OPERATION"
    assert tdx_unknown_recovery.reconcile_unknown_download(
        tmp_path, "financial", [], process_probe=lambda _exe: True,
    )["status"] == "TERMINAL_STILL_RUNNING"
    assert path.read_bytes() == before


def test_process_inventory_error_or_unavailable_lock_fails_closed(tmp_path: Path, monkeypatch) -> None:
    path = _state(tmp_path)
    before = path.read_bytes()
    assert tdx_unknown_recovery.reconcile_unknown_download(
        tmp_path, "financial", [],
        process_probe=lambda _exe: (_ for _ in ()).throw(RuntimeError("inventory failed")),
    )["status"] == "RECONCILIATION_UNAVAILABLE"
    assert path.read_bytes() == before

    @contextmanager
    def blocked(_path):
        raise OSError("busy")
        yield

    monkeypatch.setattr(tdx_unknown_recovery, "process_lock", blocked)
    assert tdx_unknown_recovery.reconcile_unknown_download(
        tmp_path, "financial", [], process_probe=lambda _exe: False,
    )["status"] == "RECONCILIATION_UNAVAILABLE"
    assert path.read_bytes() == before


def test_process_probe_requires_full_image_identity_and_known_paths(monkeypatch) -> None:
    target = Path(r"C:\tongdaxin\tdxw.exe")

    def response(paths):
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(
            [{"pid": index + 1, "path": path} for index, path in enumerate(paths)])})()

    monkeypatch.setattr(tdx_unknown_recovery.subprocess, "run",
                        lambda *_args, **_kwargs: response([r"C:\new_tdxqh\tdxw.exe"]))
    assert not tdx_unknown_recovery._matching_process_active(target)
    monkeypatch.setattr(tdx_unknown_recovery.subprocess, "run",
                        lambda *_args, **_kwargs: response([r"C:\tongdaxin\tdxw.exe"]))
    assert tdx_unknown_recovery._matching_process_active(target)
    monkeypatch.setattr(tdx_unknown_recovery.subprocess, "run",
                        lambda *_args, **_kwargs: response([None]))
    with pytest.raises(ValueError, match="path unavailable"):
        tdx_unknown_recovery._matching_process_active(target)
