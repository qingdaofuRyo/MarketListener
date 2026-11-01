from __future__ import annotations

import json
from pathlib import Path

from market_monitor import tdx_process_ownership as ownership


def test_auto_owned_record_reuses_only_same_live_process(tmp_path: Path, monkeypatch) -> None:
    exe = (tmp_path / "tdxw.exe").resolve()
    exe.write_bytes(b"synthetic")
    tokens = {42: "windows-filetime:100"}
    alive = {42}
    monkeypatch.setattr(ownership, "process_start_token", lambda pid: tokens.get(pid))
    monkeypatch.setattr(ownership, "_process_alive", lambda pid: pid in alive)
    monkeypatch.setattr(ownership, "_process_image_path", lambda _pid: exe)
    monkeypatch.setattr(ownership.time, "sleep", lambda _seconds: None)

    assert ownership.remember_auto_owned(tmp_path, "futures", 42, exe) == (42, "windows-filetime:100")
    record = tmp_path / "state/tdx_download_ui/owned-futures.json"
    assert json.loads(record.read_text(encoding="utf-8"))["terminal"] == "futures"
    assert ownership.load_auto_owned(tmp_path, "futures", exe) == (42, "windows-filetime:100")
    assert ownership.load_auto_owned(tmp_path, "financial", exe) is None

    tokens[42] = "windows-filetime:200"  # The OS reused this PID.
    assert ownership.load_auto_owned(tmp_path, "futures", exe) is None
    ownership.clear_exited_auto_owned(tmp_path, "futures", 42, "windows-filetime:100")
    assert record.is_file()  # Never erase a live, different process's record.
    tokens.clear()
    alive.clear()
    ownership.clear_exited_auto_owned(tmp_path, "futures", 42, "windows-filetime:100")
    assert not record.exists()


def test_invalid_or_foreign_owned_record_is_not_reused(tmp_path: Path, monkeypatch) -> None:
    exe = (tmp_path / "tdxw.exe").resolve()
    other = (tmp_path / "different.exe").resolve()
    exe.write_bytes(b"synthetic")
    other.write_bytes(b"synthetic")
    monkeypatch.setattr(ownership, "process_start_token", lambda _pid: "windows-filetime:100")
    monkeypatch.setattr(ownership, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(ownership, "_process_image_path", lambda _pid: other)
    path = tmp_path / "state/tdx_download_ui/owned-futures.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "tdx-auto-owned-process-v1", "terminal": "futures",
                                "pid": 42, "processStartToken": "windows-filetime:100",
                                "executable": str(exe)}), encoding="utf-8")
    assert ownership.load_auto_owned(tmp_path, "futures", exe) is None
    path.write_text("{malformed", encoding="utf-8")
    assert ownership.load_auto_owned(tmp_path, "futures", exe) is None
