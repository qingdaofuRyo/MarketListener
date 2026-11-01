"""Bounded encrypted snapshots of personal records and local configuration.

Market Parquet, provider credentials and signing keys are deliberately separate
backup classes. This profile archive is a recoverable input, not a live restore.
The two-pass source check detects changes during capture, but does not provide a
cross-file transaction until every profile writer participates in a write gate.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import re
import shutil
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .profile_gate import profile_read_gate, profile_write_gate


MAGIC = b"ML-PROFILE-BACKUP-1\n"
MAX_PLAIN_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 320 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_FILES = 10_000
MAX_MANIFEST_BYTES = 1024 * 1024
MIN_RESTORE_FREE_BYTES = 64 * 1024 * 1024
SQLITE_SNAPSHOT_SECONDS = 30
PROFILE_CAPTURE_SECONDS = 120
_LOCAL_ENTROPY = b"MarketListener/profile-backup/v1"
_LOCAL_PREFIX = b"ML-PROFILE-KEY-1\x00"
_EXACT_FILES = {"state/control_settings.json", "state/launcher_settings.json",
                "state/market_list_publication.json", "packages/ledger.sqlite"}
_PROFILE_PREFIXES = ("personal/", "strategies/")
_RESTORE_DIRECTORY = ".profile-restore"
_WINDOWS_FORBIDDEN = frozenset('<>:"\\|?*')
_WINDOWS_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL"} |
                             {f"COM{index}" for index in range(1, 10)} |
                             {f"LPT{index}" for index in range(1, 10)})


class BackupError(ValueError):
    """A stable error which never contains decrypted personal content."""


def _allowed(name: str) -> bool:
    if not isinstance(name, str) or not name or any(ord(char) < 32 for char in name):
        return False
    if any(char in _WINDOWS_FORBIDDEN for char in name):
        return False
    path = PurePosixPath(name)
    return (name != "manifest.json" and path.as_posix() == name and
            not path.is_absolute() and all(part not in {"", ".", ".."} and
                                           not part.endswith((".", " ")) and
                                           part.split(".", 1)[0].upper() not in _WINDOWS_DEVICES
                                           for part in path.parts) and
            (name in _EXACT_FILES or name.startswith(_PROFILE_PREFIXES)) and
            not name.endswith(("-wal", "-shm", "-journal", ".tmp", ".lock")))


def _is_sqlite(name: str) -> bool:
    return name.lower().endswith((".sqlite", ".sqlite3", ".db"))


def _safe_profile_path(root: Path, name: str) -> Path:
    if not _allowed(name) or root.is_symlink():
        raise BackupError("恢复路径不在个人资料范围内")
    target = root.joinpath(*PurePosixPath(name).parts)
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            raise BackupError("个人资料路径不能包含链接")
    if not target.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise BackupError("恢复路径越界")
    return target


@contextmanager
def _profile_lock(root: Path) -> Iterator[None]:
    """Serialize profile snapshots and restores, including across processes."""
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise BackupError("个人资料根目录无效")
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".profile-backup.lock"
    if path.is_symlink():
        raise BackupError("备份锁路径不能是链接")
    with path.open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.seek(0)
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackupError("另一项备份或恢复任务正在执行") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _profile_restore_lock(root: Path) -> Iterator[None]:
    """Keep restore/recovery outside concurrent participating writer operations."""
    with _profile_lock(root), profile_write_gate(root):
        yield


def _backup_directory(root: Path, *, create: bool = False) -> Path:
    directory = root
    for part in ("backups", "profile"):
        directory = directory / part
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise BackupError("备份目录不能是链接或普通文件")
        if create:
            directory.mkdir(exist_ok=True)
    return directory


def _local_master_key() -> bytes:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        raise BackupError("自动加密备份需要Windows当前用户DPAPI；跨设备备份请使用口令")
    import win32crypt

    path = Path(os.environ["LOCALAPPDATA"]) / "MarketListener" / "secrets" / "profile-backup.dpapi"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        key = secrets.token_bytes(32)
        protected = win32crypt.CryptProtectData(key, "MarketListener profile backup",
                                                _LOCAL_ENTROPY, None, None, 1)
        from .atomic_files import publish_new_bytes

        publish_new_bytes(path, _LOCAL_PREFIX + protected)
    try:
        raw = path.read_bytes()
        if not raw.startswith(_LOCAL_PREFIX) or len(raw) > 16_384:
            raise BackupError("本机备份密钥信封无效")
        _, key = win32crypt.CryptUnprotectData(raw[len(_LOCAL_PREFIX):],
                                               _LOCAL_ENTROPY, None, None, 1)
        if len(key) != 32:
            raise BackupError("本机备份密钥长度无效")
        return key
    except Exception:
        raise BackupError("本机备份密钥无法由当前Windows用户解密") from None


def _key(passphrase: str | None, salt: bytes, *, create: bool) -> tuple[str, bytes]:
    if passphrase is None:
        if not create and os.name != "nt":
            raise BackupError("此备份需要原Windows用户的本机密钥")
        return "DPAPI_CURRENT_USER", sha256(_local_master_key() + salt).digest()
    if len(passphrase) < 12:
        raise BackupError("便携备份口令至少12个字符")
    return "PASSPHRASE_SCRYPT", Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
        passphrase.encode("utf-8"))


def _sqlite_snapshot(source: Path, *, deadline: float | None = None) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise BackupError("个人库不是普通文件")
    if source.stat().st_size > MAX_FILE_BYTES:
        raise BackupError("个人库超过单文件备份上限")
    started = time.monotonic()

    def check_progress(_status: int, _remaining: int, _total: int) -> None:
        if (time.monotonic() - started > SQLITE_SNAPSHOT_SECONDS or
                deadline is not None and time.monotonic() >= deadline):
            raise BackupError("个人库快照超时，请稍后重试")

    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=5)) as original:
        with closing(sqlite3.connect(":memory:")) as snapshot:
            original.backup(snapshot, pages=512, progress=check_progress, sleep=0.1)
            if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupError("个人库快照未通过完整性核验")
            data = snapshot.serialize()
    if len(data) > MAX_FILE_BYTES:
        raise BackupError("个人库超过单文件备份上限")
    if deadline is not None and time.monotonic() >= deadline:
        raise BackupError("个人资料快照超时，请稍后重试")
    return data


def _stable_read(path: Path, *, deadline: float | None = None) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise BackupError("备份来源包含非普通文件")
    for _ in range(3):
        before = path.stat()
        if before.st_size > MAX_FILE_BYTES:
            raise BackupError("个人文件超过单文件备份上限")
        data = path.read_bytes()
        after = path.stat()
        if deadline is not None and time.monotonic() >= deadline:
            raise BackupError("个人资料快照超时，请稍后重试")
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and len(data) == before.st_size:
            return data
    raise BackupError("备份来源写入中，请稍后重试")


def _snapshot_files(root: Path, *, expected: dict[str, bytes] | None = None,
                    deadline: float | None = None) -> dict[str, bytes]:
    """Capture once, or check each source against an earlier capture.

    The check is bounded to one extra file at a time. It also detects files
    added or removed while the first pass was in progress. It cannot certify
    an application transaction spanning multiple files.
    """
    files: dict[str, bytes] = {}
    names: set[str] = set()
    total_bytes = 0

    def capture(name: str, path: Path) -> None:
        nonlocal total_bytes
        if deadline is not None and time.monotonic() >= deadline:
            raise BackupError("个人资料快照超时，请稍后重试")
        data = (_sqlite_snapshot(path, deadline=deadline)
                if _is_sqlite(name) else _stable_read(path, deadline=deadline))
        names.add(name)
        total_bytes += len(data)
        if len(names) > MAX_FILES or total_bytes > MAX_PLAIN_BYTES:
            raise BackupError("个人资料超过本地备份预算")
        if expected is not None:
            if expected.get(name) != data:
                raise BackupError("个人资料在备份期间发生变化，请稍后重试")
        else:
            files[name] = data

    for prefix in _PROFILE_PREFIXES:
        directory = root / prefix.rstrip("/")
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise BackupError("个人目录不能是链接")
        for path in sorted(directory.rglob("*")):
            if path.is_dir():
                if path.is_symlink():
                    raise BackupError("个人目录不能包含链接")
                continue
            name = path.relative_to(root).as_posix()
            if not _allowed(name):
                continue
            capture(name, path)
    for name in sorted(_EXACT_FILES):
        path = root / name
        _safe_profile_path(root, name)
        if path.is_file():
            capture(name, path)
    if expected is not None and names != set(expected):
        raise BackupError("个人资料文件清单在备份期间发生变化，请稍后重试")
    return files


def _plaintext_archive(files: dict[str, bytes]) -> bytes:
    manifest = {"schemaVersion": 1, "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "captureConsistency": "FILE_RECHECKED_UNCOORDINATED",
                "files": [{"path": name, "bytes": len(data), "sha256": sha256(data).hexdigest()}
                          for name, data in sorted(files.items())]}
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    value = buffer.getvalue()
    if len(value) > MAX_PLAIN_BYTES:
        raise BackupError("加密前备份包超过预算")
    return value


def create_profile_backup(root: Path, *, passphrase: str | None = None) -> Path:
    root = Path(root)
    with _profile_lock(root):
        if (root / _RESTORE_DIRECTORY).exists():
            raise BackupError("上次恢复尚未完成回滚")
        directory = _backup_directory(root, create=True)
        deadline = time.monotonic() + PROFILE_CAPTURE_SECONDS
        with profile_read_gate(root, timeout=PROFILE_CAPTURE_SECONDS):
            files = _snapshot_files(root, deadline=deadline)
            _snapshot_files(root, expected=files, deadline=deadline)
        plain = _plaintext_archive(files)
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        mode, key = _key(passphrase, salt, create=True)
        header = {"schemaVersion": 1, "mode": mode, "salt": salt.hex(), "nonce": nonce.hex()}
        head = json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
        encrypted = AESGCM(key).encrypt(nonce, plain, head)
        if len(encrypted) + len(head) + len(MAGIC) + 4 > MAX_ARCHIVE_BYTES:
            raise BackupError("加密备份包超过预算")
        name = f"profile-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}.mlbackup"
        target = directory / name
        temporary = target.with_suffix(".tmp")
        try:
            with temporary.open("wb") as output:
                output.write(MAGIC + len(head).to_bytes(4, "big") + head + encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            verify_profile_backup(target, passphrase=passphrase)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return target


def profile_backups(root: Path) -> list[dict[str, Any]]:
    directory = _backup_directory(Path(root))
    items = []
    for path in directory.glob("profile-*.mlbackup"):
        match = re.fullmatch(r"profile-(\d{8}T\d{6}Z)-[0-9a-f]{8}\.mlbackup", path.name)
        if match is None or not path.is_file() or path.is_symlink():
            continue
        try:
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            items.append({"name": path.name, "createdAt": created.isoformat(timespec="seconds"),
                          "bytes": path.stat().st_size})
        except (OSError, ValueError):
            continue
    return sorted(items, key=lambda item: item["createdAt"], reverse=True)


def profile_backup_due(root: Path, *, now: datetime | None = None) -> bool:
    items = profile_backups(root)
    if not items:
        return True
    latest = datetime.fromisoformat(items[0]["createdAt"])
    return (now or datetime.now(timezone.utc)) - latest >= timedelta(hours=24)


def rotate_profile_backups(root: Path) -> int:
    """After a verified new archive, retain 7 daily, 4 weekly and 3 monthly anchors."""
    root = Path(root)
    with _profile_lock(root):
        items = profile_backups(root)
        if not items:
            return 0
        kept: set[str] = set()
        daily: set[str] = set()
        weekly: set[str] = set()
        monthly: set[str] = set()
        for item in items:
            created = datetime.fromisoformat(item["createdAt"])
            day = created.date().isoformat()
            week = (created.date() - timedelta(days=created.weekday())).isoformat()
            month = created.strftime("%Y-%m")
            if ((day not in daily and len(daily) < 7) or
                    (week not in weekly and len(weekly) < 4) or
                    (month not in monthly and len(monthly) < 3)):
                kept.add(item["name"])
            if len(daily) < 7:
                daily.add(day)
            if len(weekly) < 4:
                weekly.add(week)
            if len(monthly) < 3:
                monthly.add(month)
        directory = _backup_directory(root)
        removed = 0
        for item in items:
            if item["name"] not in kept:
                (directory / item["name"]).unlink()
                removed += 1
        return removed


def _unique_json(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise BackupError("备份JSON包含重复字段")
            result[name] = value
        return result

    return json.loads(raw, object_pairs_hook=object_pairs)


def _load_profile_backup(path: Path, passphrase: str | None) -> tuple[dict[str, Any], dict[str, bytes], str]:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise BackupError("备份文件不存在或超出大小上限")
    try:
        with path.open("rb") as stream:
            if stream.read(len(MAGIC)) != MAGIC:
                raise BackupError("备份格式无效")
            head_size = int.from_bytes(stream.read(4), "big")
            if not 1 <= head_size <= 4096:
                raise BackupError("备份头长度无效")
            head = stream.read(head_size)
            ciphertext = stream.read(MAX_ARCHIVE_BYTES + 1)
        header = _unique_json(head)
        if not isinstance(header, dict) or set(header) != {"schemaVersion", "mode", "salt", "nonce"}:
            raise BackupError("备份加密头无效")
        if header["schemaVersion"] != 1 or header["mode"] not in {"DPAPI_CURRENT_USER", "PASSPHRASE_SCRYPT"}:
            raise BackupError("不支持的备份版本或密钥模式")
        salt, nonce = bytes.fromhex(header["salt"]), bytes.fromhex(header["nonce"])
        if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) > MAX_ARCHIVE_BYTES:
            raise BackupError("备份加密头无效")
        mode, key = _key(passphrase, salt, create=False)
        if mode != header["mode"]:
            raise BackupError("备份口令模式不匹配")
        plain = AESGCM(key).decrypt(nonce, ciphertext, head)
        if len(plain) > MAX_PLAIN_BYTES:
            raise BackupError("解密内容超过大小上限")
        content_sha = sha256(plain).hexdigest()
        with zipfile.ZipFile(BytesIO(plain)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (len(names) > MAX_FILES + 1 or len(names) != len(set(names)) or
                    len({name.casefold() for name in names}) != len(names) or
                    names.count("manifest.json") != 1 or
                    any(not _allowed(name) for name in names if name != "manifest.json")):
                raise BackupError("备份文件清单不闭合")
            for item in infos:
                mode_bits = item.external_attr >> 16
                if (item.is_dir() or item.flag_bits & 1 or
                        (mode_bits & 0o170000) not in {0, 0o100000} or
                        item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise BackupError("备份包含不支持的条目")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise BackupError("备份清单超过大小上限")
            with archive.open(manifest_info) as source:
                manifest_raw = source.read(MAX_MANIFEST_BYTES + 1)
            if len(manifest_raw) != manifest_info.file_size:
                raise BackupError("备份清单长度无效")
            manifest = _unique_json(manifest_raw)
            if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
                raise BackupError("备份清单版本无效")
            records = manifest.get("files")
            if not isinstance(records, list) or len(records) > MAX_FILES:
                raise BackupError("备份清单格式无效")
            declared: dict[str, dict[str, Any]] = {}
            for record in records:
                if (not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"} or
                        not isinstance(record["path"], str) or not _allowed(record["path"]) or
                        type(record["bytes"]) is not int or not 0 <= record["bytes"] <= MAX_FILE_BYTES or
                        not isinstance(record["sha256"], str) or
                        re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None):
                    raise BackupError("备份清单条目无效")
                declared[record["path"]] = record
            if (len(declared) != len(records) or
                    len({name.casefold() for name in declared}) != len(declared) or
                    set(names) - {"manifest.json"} != set(declared) or
                    sum(record["bytes"] for record in records) > MAX_PLAIN_BYTES):
                raise BackupError("备份清单与实际文件不符")
            files: dict[str, bytes] = {}
            for name, record in declared.items():
                info = archive.getinfo(name)
                if info.file_size != record["bytes"]:
                    raise BackupError("备份条目长度无效")
                with archive.open(info) as source:
                    data = source.read(record["bytes"] + 1)
                if len(data) != record["bytes"] or sha256(data).hexdigest() != record["sha256"]:
                    raise BackupError("备份文件哈希校验失败")
                files[name] = data
        return manifest, files, content_sha
    except BackupError:
        raise
    except (InvalidTag, ValueError, KeyError, TypeError, zipfile.BadZipFile, OSError, RuntimeError):
        raise BackupError("备份解密或完整性核验失败；请检查口令与文件") from None


def verify_profile_backup(path: Path, *, passphrase: str | None = None) -> dict[str, Any]:
    manifest, files, content_sha = _load_profile_backup(path, passphrase)
    return {"status": "VERIFIED", "createdAt": manifest.get("createdAt"),
            "fileCount": len(files), "contentSha256": content_sha,
            "captureConsistency": manifest.get("captureConsistency", "UNKNOWN_LEGACY"),
            "scope": "PROFILE_CONFIG_PUBLICATION_AUDIT"}


def _restore_path(root: Path, name: str) -> Path:
    for suffix in ("-wal", "-shm", "-journal"):
        if name.endswith(suffix):
            base = name[:-len(suffix)]
            if not _is_sqlite(base):
                break
            path = Path(str(_safe_profile_path(root, base)) + suffix)
            if path.is_symlink():
                raise BackupError("个人资料路径不能包含链接")
            return path
    return _safe_profile_path(root, name)


def _existing_restore_names(root: Path, incoming: dict[str, bytes]) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise BackupError("恢复目标目录无效")
    names: set[str] = set()
    for prefix in _PROFILE_PREFIXES:
        directory = root / prefix.rstrip("/")
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise BackupError("个人目录不能是链接")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise BackupError("个人目录不能包含链接")
            if path.is_file():
                name = path.relative_to(root).as_posix()
                if _allowed(name):
                    names.add(name)
    for name in _EXACT_FILES:
        path = _safe_profile_path(root, name)
        if path.exists():
            if not path.is_file():
                raise BackupError("恢复目标包含非普通文件")
            names.add(name)
    sqlite_bases = {name for name in names | set(incoming) if _is_sqlite(name)}
    for base in sqlite_bases:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = base + suffix
            path = _restore_path(root, sidecar)
            if path.exists():
                if not path.is_file():
                    raise BackupError("个人库附属文件不是普通文件")
                names.add(sidecar)
    if len(names | set(incoming)) > MAX_FILES + len(sqlite_bases) * 3:
        raise BackupError("恢复范围超过文件数量上限")
    if len({name.casefold() for name in names | set(incoming)}) != len(names | set(incoming)):
        raise BackupError("个人文件名称在Windows下发生冲突")
    return names


def _stable_hash(path: Path) -> tuple[int, str, int]:
    for _ in range(3):
        before = path.stat()
        if before.st_size > MAX_FILE_BYTES:
            raise BackupError("恢复目标文件超过单文件上限")
        digest = sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                if size > MAX_FILE_BYTES:
                    raise BackupError("恢复目标文件超过单文件上限")
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and size == before.st_size:
            return size, digest.hexdigest(), after.st_mtime_ns
    raise BackupError("恢复目标仍在写入，请关闭服务后重试")


def _restore_plan(root: Path, files: dict[str, bytes], content_sha: str) -> tuple[list[dict[str, Any]], str]:
    current = _existing_restore_names(root, files)
    rows: list[dict[str, Any]] = []
    for name in sorted(current | set(files)):
        path = _restore_path(root, name)
        previous = name in current
        size, current_sha, modified = _stable_hash(path) if previous else (0, None, None)
        incoming = files.get(name)
        rows.append({"path": name, "previous": previous, "incoming": incoming is not None,
                     "currentBytes": size if previous else None, "currentSha256": current_sha,
                     "currentModifiedNs": modified,
                     "incomingBytes": len(incoming) if incoming is not None else None,
                     "incomingSha256": sha256(incoming).hexdigest() if incoming is not None else None})
    payload = json.dumps({"contentSha256": content_sha, "rows": rows},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return rows, sha256(payload).hexdigest()


def preview_profile_restore(path: Path, root: Path, *, passphrase: str | None = None) -> dict[str, Any]:
    """Read-only preview. The digest binds archive contents and current target files."""
    root = Path(root)
    manifest, files, content_sha = _load_profile_backup(path, passphrase)
    if (root / _RESTORE_DIRECTORY).exists():
        raise BackupError("上次恢复尚未完成回滚")
    rows, plan_digest = _restore_plan(root, files, content_sha)
    return {"status": "PREVIEW", "createdAt": manifest.get("createdAt"),
            "contentSha256": content_sha, "planDigest": plan_digest,
            "fileCount": len(files), "replaceCount": sum(row["previous"] and row["incoming"] for row in rows),
            "createCount": sum(not row["previous"] and row["incoming"] for row in rows),
            "removeCount": sum(row["previous"] and not row["incoming"] for row in rows),
            "paths": [{"path": row["path"], "action": ("REPLACE" if row["previous"] else "CREATE")
                       if row["incoming"] else "REMOVE"} for row in rows]}


def _write_journal(transaction: Path, journal: dict[str, Any]) -> None:
    raw = json.dumps(journal, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BackupError("恢复日志超过大小上限")
    temporary = transaction / "journal.tmp"
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, transaction / "journal.json")


def _read_journal(root: Path, transaction: Path) -> dict[str, Any]:
    path = transaction / "journal.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BackupError("恢复日志无效，必须保留现场")
    try:
        journal = _unique_json(path.read_bytes())
        if (not isinstance(journal, dict) or journal.get("schemaVersion") != 1 or
                journal.get("phase") not in {"PREPARED", "COMMITTED"} or
                not isinstance(journal.get("rows"), list) or len(journal["rows"]) > MAX_FILES * 4):
            raise BackupError("恢复日志格式无效")
        names: set[str] = set()
        for row in journal["rows"]:
            if (not isinstance(row, dict) or set(row) != {"path", "previous", "incoming"} or
                    not isinstance(row["path"], str) or
                    type(row["previous"]) is not bool or type(row["incoming"]) is not bool):
                raise BackupError("恢复日志条目无效")
            _restore_path(root, row["path"])
            names.add(row["path"])
        if len(names) != len(journal["rows"]):
            raise BackupError("恢复日志包含重复路径")
        return journal
    except (ValueError, KeyError, TypeError, OSError):
        raise BackupError("恢复日志损坏，必须保留现场") from None


def _cleanup_transaction(transaction: Path) -> None:
    for name in ("staged", "originals"):
        directory = transaction / name
        if directory.is_symlink():
            raise BackupError("恢复临时目录不能是链接")
        if directory.exists():
            shutil.rmtree(directory)
    for name in ("journal.tmp", "journal.json"):
        (transaction / name).unlink(missing_ok=True)
    transaction.rmdir()


def _recover_restore_locked(root: Path) -> dict[str, Any]:
    transaction = root / _RESTORE_DIRECTORY
    if not transaction.exists():
        return {"status": "NO_PENDING"}
    if transaction.is_symlink() or not transaction.is_dir():
        raise BackupError("恢复临时目录无效，必须保留现场")
    if not (transaction / "journal.json").exists():
        # A journal is persisted before the first mutation and removed after all rollback/commit work.
        _cleanup_transaction(transaction)
        return {"status": "DISCARDED_PREPARATION"}
    journal = _read_journal(root, transaction)
    if journal["phase"] == "COMMITTED":
        _cleanup_transaction(transaction)
        return {"status": "COMMITTED_CLEANUP"}
    for row in reversed(journal["rows"]):
        name = row["path"]
        target = _restore_path(root, name)
        old = transaction / "originals" / Path(*PurePosixPath(name).parts)
        if old.is_symlink():
            raise BackupError("恢复回滚文件不能是链接")
        if row["previous"] and old.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, target)
        elif row["previous"] and not target.is_file():
            raise BackupError("恢复回滚原件缺失，必须保留现场")
        elif not row["previous"] and target.exists():
            if not target.is_file():
                raise BackupError("恢复目标不是普通文件")
            target.unlink()
    _cleanup_transaction(transaction)
    return {"status": "ROLLED_BACK", "fileCount": len(journal["rows"])}


def recover_interrupted_profile_restore(root: Path, *, offline: bool = False) -> dict[str, Any]:
    """Run before service READY; a crash after commit only needs journal cleanup."""
    if not offline:
        raise BackupError("恢复回滚只能在产品服务停止时运行")
    root = Path(root)
    with _profile_restore_lock(root):
        return _recover_restore_locked(root)


def _validate_staged_file(name: str, path: Path) -> None:
    if _is_sqlite(name):
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise BackupError("待恢复个人库未通过完整性校验")
        except sqlite3.DatabaseError:
            raise BackupError("待恢复个人库无效") from None
    elif name in _EXACT_FILES and name.endswith(".json"):
        if not isinstance(_unique_json(path.read_bytes()), dict):
            raise BackupError("待恢复设置文件格式无效")


def restore_profile_backup(
    path: Path, root: Path, *, expected_plan_digest: str,
    passphrase: str | None = None, offline: bool = False,
) -> dict[str, Any]:
    """Restore a previewed profile while the service is stopped.

    Each live file move is journaled in advance. A normal failure rolls back;
    ``recover_interrupted_profile_restore`` rolls back after process interruption.
    """
    if not offline:
        raise BackupError("个人资料恢复只能在产品服务停止时运行")
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest or "") is None:
        raise BackupError("恢复预览摘要无效")
    root = Path(root)
    with _profile_restore_lock(root):
        if (root / _RESTORE_DIRECTORY).exists():
            raise BackupError("上次恢复尚未完成回滚")
        manifest, files, content_sha = _load_profile_backup(path, passphrase)
        rows, plan_digest = _restore_plan(root, files, content_sha)
        if plan_digest != expected_plan_digest:
            raise BackupError("备份或个人资料自预览后已变化，请重新预览")
        needed = sum(len(data) for data in files.values()) + MIN_RESTORE_FREE_BYTES
        if shutil.disk_usage(root).free < needed:
            raise BackupError("磁盘可用空间不足，恢复未开始")
        transaction = root / _RESTORE_DIRECTORY
        transaction.mkdir()
        try:
            for name, data in files.items():
                target = transaction / "staged" / Path(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                _validate_staged_file(name, target)
            journal = {"schemaVersion": 1, "phase": "PREPARED", "createdAt": manifest.get("createdAt"),
                       "contentSha256": content_sha,
                       "rows": [{"path": row["path"], "previous": row["previous"],
                                 "incoming": row["incoming"]} for row in rows]}
            _write_journal(transaction, journal)
            for row in rows:
                name = row["path"]
                target = _restore_path(root, name)
                old = transaction / "originals" / Path(*PurePosixPath(name).parts)
                if row["previous"]:
                    old.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, old)
                if row["incoming"]:
                    staged = transaction / "staged" / Path(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, target)
            journal["phase"] = "COMMITTED"
            _write_journal(transaction, journal)
            _cleanup_transaction(transaction)
        except Exception as error:
            try:
                recovery = _recover_restore_locked(root)
            except Exception:
                raise BackupError("恢复中断且回滚未完成，请先运行恢复回滚") from error
            if recovery["status"] == "COMMITTED_CLEANUP":
                return {"status": "RESTORED", "createdAt": manifest.get("createdAt"),
                        "contentSha256": content_sha, "planDigest": plan_digest,
                        "fileCount": len(files), "cleanupRecovered": True}
            if isinstance(error, BackupError):
                raise
            raise BackupError("恢复失败，现有个人资料已回滚") from error
        return {"status": "RESTORED", "createdAt": manifest.get("createdAt"),
                "contentSha256": content_sha, "planDigest": plan_digest,
                "fileCount": len(files)}


__all__ = ("BackupError", "create_profile_backup", "profile_backup_due",
           "profile_backups", "rotate_profile_backups", "verify_profile_backup",
           "preview_profile_restore", "restore_profile_backup",
           "recover_interrupted_profile_restore")
