from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import zipfile

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import market_monitor.local_backup as backup_module
from market_monitor.local_backup import (
    BackupError,
    create_profile_backup,
    preview_profile_restore,
    recover_interrupted_profile_restore,
    restore_profile_backup,
    rotate_profile_backups,
    profile_backups,
    verify_profile_backup,
)


def test_portable_profile_backup_uses_sqlite_snapshot_and_rejects_wrong_password(tmp_path):
    root = tmp_path / "data"
    (root / "personal").mkdir(parents=True)
    (root / "personal" / "watchlist.json").write_text(json.dumps({"synthetic": ["A2611"]}), encoding="utf-8")
    database = root / "personal" / "accounts.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO synthetic(value) VALUES ('fixture')")
    (root / "state").mkdir()
    (root / "state" / "control_settings.json").write_text('{"theme":"dark"}', encoding="utf-8")
    password = "synthetic backup passphrase 2026"
    backup = create_profile_backup(root, passphrase=password)
    verified = verify_profile_backup(backup, passphrase=password)
    assert verified["status"] == "VERIFIED"
    assert verified["captureConsistency"] == "FILE_RECHECKED_UNCOORDINATED"
    assert verified["fileCount"] == 3
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM synthetic").fetchone()[0] == "fixture"
    with pytest.raises(BackupError):
        verify_profile_backup(backup, passphrase="different synthetic passphrase")
    changed = bytearray(backup.read_bytes())
    changed[-1] ^= 1
    backup.write_bytes(changed)
    with pytest.raises(BackupError):
        verify_profile_backup(backup, passphrase=password)


def _synthetic_profile(root):
    (root / "personal").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "personal" / "watchlist.json").write_text('{"symbols":["A2611"]}', encoding="utf-8")
    (root / "state" / "control_settings.json").write_text('{"theme":"dark"}', encoding="utf-8")
    with closing(sqlite3.connect(root / "personal" / "accounts.sqlite")) as db:
        db.execute("CREATE TABLE fixture(value TEXT)")
        db.execute("INSERT INTO fixture VALUES ('before')")
        db.commit()


@pytest.mark.parametrize("change", ["json", "sqlite", "added_file"])
def test_online_capture_rejects_sources_changed_between_passes(tmp_path, monkeypatch, change):
    root = tmp_path / "data"
    _synthetic_profile(root)
    original_snapshot_files = backup_module._snapshot_files
    calls = 0

    def change_after_first_pass(data_root, **kwargs):
        nonlocal calls
        calls += 1
        files = original_snapshot_files(data_root, **kwargs)
        if calls == 1:
            if change == "json":
                (root / "personal" / "watchlist.json").write_text(
                    '{"symbols":["AFTER"]}', encoding="utf-8",
                )
            elif change == "added_file":
                (root / "strategies").mkdir()
                (root / "strategies" / "new.json").write_text('{"version":1}', encoding="utf-8")
            else:
                with closing(sqlite3.connect(root / "personal" / "accounts.sqlite")) as db:
                    db.execute("UPDATE fixture SET value = 'after'")
                    db.commit()
        return files

    monkeypatch.setattr(backup_module, "_snapshot_files", change_after_first_pass)
    with pytest.raises(BackupError, match="发生变化"):
        create_profile_backup(root, passphrase="synthetic portable password 2026")
    assert calls == 2
    assert profile_backups(root) == []


def test_online_capture_has_a_total_deadline(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _synthetic_profile(root)
    monkeypatch.setattr(backup_module, "PROFILE_CAPTURE_SECONDS", 0)
    with pytest.raises(BackupError, match="超时"):
        create_profile_backup(root, passphrase="synthetic portable password 2026")
    assert profile_backups(root) == []


def test_restore_preview_exact_scope_and_sqlite_snapshot(tmp_path):
    root = tmp_path / "data"
    _synthetic_profile(root)
    password = "synthetic portable password 2026"
    archive = create_profile_backup(root, passphrase=password)
    with closing(sqlite3.connect(root / "personal" / "accounts.sqlite")) as db:
        db.execute("UPDATE fixture SET value = 'after'")
        db.commit()
    (root / "personal" / "watchlist.json").write_text('{"symbols":["NEW"]}', encoding="utf-8")
    (root / "personal" / "extra.json").write_text('{"temporary":true}', encoding="utf-8")
    (root / "personal" / "accounts.sqlite-wal").write_bytes(b"stale sidecar")
    preview = preview_profile_restore(archive, root, passphrase=password)
    assert preview["status"] == "PREVIEW"
    assert preview["removeCount"] == 2
    assert {item["path"] for item in preview["paths"] if item["action"] == "REMOVE"} == {
        "personal/extra.json", "personal/accounts.sqlite-wal",
    }
    with pytest.raises(BackupError, match="停止"):
        restore_profile_backup(archive, root, expected_plan_digest=preview["planDigest"], passphrase=password)
    result = restore_profile_backup(
        archive, root, expected_plan_digest=preview["planDigest"], passphrase=password, offline=True,
    )
    assert result["status"] == "RESTORED"
    assert not (root / "personal" / "extra.json").exists()
    assert not (root / "personal" / "accounts.sqlite-wal").exists()
    assert json.loads((root / "personal" / "watchlist.json").read_text(encoding="utf-8")) == {
        "symbols": ["A2611"],
    }
    with closing(sqlite3.connect(root / "personal" / "accounts.sqlite")) as db:
        assert db.execute("SELECT value FROM fixture").fetchone()[0] == "before"
    assert recover_interrupted_profile_restore(root, offline=True)["status"] == "NO_PENDING"


def test_failed_preview_and_changed_target_do_not_modify_profile(tmp_path):
    root = tmp_path / "data"
    _synthetic_profile(root)
    password = "synthetic portable password 2026"
    archive = create_profile_backup(root, passphrase=password)
    preview = preview_profile_restore(archive, root, passphrase=password)
    watchlist = root / "personal" / "watchlist.json"
    watchlist.write_text('{"symbols":["CHANGED"]}', encoding="utf-8")
    with pytest.raises(BackupError, match="重新预览"):
        restore_profile_backup(
            archive, root, expected_plan_digest=preview["planDigest"], passphrase=password, offline=True,
        )
    with pytest.raises(BackupError):
        preview_profile_restore(archive, root, passphrase="different synthetic password")
    assert watchlist.read_text(encoding="utf-8") == '{"symbols":["CHANGED"]}'
    changed = bytearray(archive.read_bytes())
    changed[-1] ^= 1
    archive.write_bytes(changed)
    with pytest.raises(BackupError):
        preview_profile_restore(archive, root, passphrase=password)
    assert watchlist.read_text(encoding="utf-8") == '{"symbols":["CHANGED"]}'


def test_restore_io_failure_rolls_back_all_files(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _synthetic_profile(root)
    password = "synthetic portable password 2026"
    archive = create_profile_backup(root, passphrase=password)
    watchlist = root / "personal" / "watchlist.json"
    watchlist.write_text('{"symbols":["AFTER"]}', encoding="utf-8")
    before = watchlist.read_bytes()
    preview = preview_profile_restore(archive, root, passphrase=password)
    original_replace = backup_module.os.replace
    raised = False

    def fail_once(source, destination):
        nonlocal raised
        if not raised and destination == watchlist and "staged" in str(source):
            raised = True
            raise OSError("synthetic disk interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", fail_once)
    with pytest.raises(BackupError, match="已回滚"):
        restore_profile_backup(
            archive, root, expected_plan_digest=preview["planDigest"], passphrase=password, offline=True,
        )
    assert raised
    assert watchlist.read_bytes() == before
    assert not (root / ".profile-restore").exists()


def test_process_interruption_can_be_rolled_back_before_ready(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _synthetic_profile(root)
    password = "synthetic portable password 2026"
    archive = create_profile_backup(root, passphrase=password)
    watchlist = root / "personal" / "watchlist.json"
    watchlist.write_text('{"symbols":["AFTER"]}', encoding="utf-8")
    before = watchlist.read_bytes()
    preview = preview_profile_restore(archive, root, passphrase=password)
    original_replace = backup_module.os.replace
    interrupted = False

    def interrupt_once(source, destination):
        nonlocal interrupted
        if not interrupted and destination == watchlist and "staged" in str(source):
            interrupted = True
            raise KeyboardInterrupt("synthetic process stop")
        return original_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        restore_profile_backup(
            archive, root, expected_plan_digest=preview["planDigest"], passphrase=password, offline=True,
        )
    assert (root / ".profile-restore" / "journal.json").is_file()
    with pytest.raises(BackupError, match="回滚"):
        create_profile_backup(root, passphrase=password)
    assert recover_interrupted_profile_restore(root, offline=True)["status"] == "ROLLED_BACK"
    assert watchlist.read_bytes() == before
    assert not (root / ".profile-restore").exists()


@pytest.mark.parametrize("unsafe_name", [
    "personal/../state/control_settings.json",
    "personal/sub\\..\\secret.json",
    "personal/CON.json",
])
def test_forged_encrypted_archive_rejects_unsafe_windows_paths(tmp_path, unsafe_name):
    password = "synthetic portable password 2026"
    content = b"fixture"
    manifest = {"schemaVersion": 1, "createdAt": "2026-10-11T00:00:00+00:00",
                "files": [{"path": unsafe_name, "bytes": len(content),
                           "sha256": sha256(content).hexdigest()}]}
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(unsafe_name, content)
    salt, nonce = os.urandom(16), os.urandom(12)
    mode, key = backup_module._key(password, salt, create=True)
    header = json.dumps({"schemaVersion": 1, "mode": mode,
                         "salt": salt.hex(), "nonce": nonce.hex()},
                        sort_keys=True, separators=(",", ":")).encode()
    archive_path = tmp_path / "forged.mlbackup"
    archive_path.write_bytes(
        backup_module.MAGIC + len(header).to_bytes(4, "big") + header +
        AESGCM(key).encrypt(nonce, buffer.getvalue(), header),
    )
    with pytest.raises(BackupError):
        verify_profile_backup(archive_path, passphrase=password)


def test_local_key_mode_uses_injected_current_user_key(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _synthetic_profile(root)
    monkeypatch.setattr(backup_module, "_local_master_key", lambda: b"synthetic" * 4)
    archive = create_profile_backup(root)
    assert verify_profile_backup(archive)["status"] == "VERIFIED"
    assert preview_profile_restore(archive, root)["fileCount"] == 3


def test_retention_keeps_seven_days_four_weeks_three_months(tmp_path):
    root = tmp_path / "data"
    directory = root / "backups" / "profile"
    directory.mkdir(parents=True)
    latest = datetime(2026, 10, 11, tzinfo=timezone.utc)
    for days in range(100):
        stamp = (latest - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
        (directory / f"profile-{stamp}-{days:08x}.mlbackup").write_bytes(b"synthetic")
    assert rotate_profile_backups(root) > 0
    kept = profile_backups(root)
    kept_days = {datetime.fromisoformat(item["createdAt"]).date() for item in kept}
    assert {latest.date() - timedelta(days=days) for days in range(7)} <= kept_days
    assert len({day - timedelta(days=day.weekday()) for day in kept_days}) >= 4
    assert len({day.strftime("%Y-%m") for day in kept_days}) >= 3
    assert len(kept) <= 14
