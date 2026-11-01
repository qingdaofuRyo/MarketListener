import os
from pathlib import Path
import threading
import time

import pytest

from market_monitor import atomic_files


def test_publish_new_bytes_is_complete_and_never_replaces_existing_secret(tmp_path):
    target = tmp_path / "secrets" / "control.dpapi"
    assert atomic_files.publish_new_bytes(target, b"first-complete-envelope") is True
    assert target.read_bytes() == b"first-complete-envelope"
    assert atomic_files.publish_new_bytes(target, b"different-envelope") is False
    assert target.read_bytes() == b"first-complete-envelope"
    assert list(target.parent.glob("*.tmp")) == []


def test_publish_new_bytes_failed_link_never_creates_partial_target(tmp_path, monkeypatch):
    target = tmp_path / "secrets" / "control.dpapi"

    def fail_before_publish(source, destination):
        assert Path(source).read_bytes() == b"complete-envelope"
        assert not Path(destination).exists()
        raise OSError("synthetic interrupted publish")

    monkeypatch.setattr(atomic_files.os, "link", fail_before_publish)
    with pytest.raises(OSError, match="interrupted publish"):
        atomic_files.publish_new_bytes(target, b"complete-envelope")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_transient_replace_keeps_original_until_success(tmp_path, monkeypatch):
    target, source = tmp_path / "state.json", tmp_path / "state.tmp"
    target.write_text("old", encoding="utf-8")
    source.write_text("new", encoding="utf-8")
    original_replace = os.replace
    attempts, waits = [], []

    def busy_then_replace(src, dst):
        attempts.append(1)
        if len(attempts) <= 2:
            assert Path(dst).read_text(encoding="utf-8") == "old"
            raise PermissionError("synthetic sharing violation")
        original_replace(src, dst)

    monkeypatch.setattr(atomic_files.os, "replace", busy_then_replace)
    monkeypatch.setattr(atomic_files.time, "sleep", waits.append)
    atomic_files.replace_with_retry(source, target)
    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()
    assert waits == [0.05, 0.1]


def test_persistent_replace_failure_preserves_checkpoint(tmp_path, monkeypatch):
    target, source = tmp_path / "state.json", tmp_path / "state.tmp"
    target.write_text("old", encoding="utf-8")
    source.write_text("new", encoding="utf-8")
    attempts = []

    def denied(*args):
        attempts.append(1)
        raise PermissionError("synthetic persistent access failure")

    monkeypatch.setattr(atomic_files.os, "replace", denied)
    monkeypatch.setattr(atomic_files.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        atomic_files.replace_with_retry(source, target)
    assert len(attempts) == 6
    assert target.read_text(encoding="utf-8") == "old"
    assert source.read_text(encoding="utf-8") == "new"


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing behavior")
def test_windows_open_reader_then_release_allows_retry(tmp_path):
    target, source = tmp_path / "state.json", tmp_path / "state.tmp"
    target.write_text("old", encoding="utf-8")
    source.write_text("new", encoding="utf-8")
    reader = target.open(encoding="utf-8")
    try:
        with pytest.raises(PermissionError):
            os.replace(source, target)

        def release_reader():
            time.sleep(0.08)
            reader.close()

        thread = threading.Thread(target=release_reader)
        thread.start()
        try:
            atomic_files.replace_with_retry(source, target)
        finally:
            thread.join()
    finally:
        reader.close()
    assert target.read_text(encoding="utf-8") == "new"
