"""Monotonic Silver revisions with a bounded, fail-closed change journal.

The revision and its recent change chain share one atomically replaced file.
Writers serialize revisions across processes; readers can prove an exact file
set only while their complete base-to-current chain is still retained.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .atomic_files import replace_with_retry


_LOCK = threading.RLock()
_VERSION_FILE = "market_data_version.json"
_LOCK_FILE = "market_data_version.lock"
_SCHEMA_VERSION = 2
_MAX_TRANSITIONS = 2048
_MAX_STATE_BYTES = 4 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 30.0
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_INVALID_REVISION = "invalid-silver-revision-state"


def _fresh_unknown_epoch() -> int:
    """Start a new provenance lineage outside any prior small counter range."""

    return (1 << 61) | secrets.randbits(61)


def _valid_state(state: dict[str, object] | None) -> bool:
    if _revision(state) is None or _validated_journal(state) is None:
        return False
    pending = _validated_pending(state)
    if pending is None:
        return False
    transitions = state.get("journal", []) if state is not None else []
    known = {item["current"] for item in transitions}
    return all(revision in known for revision in pending)


def _legacy_revision(state: dict[str, object] | None) -> str | None:
    """Recognize the exact pre-journal layout without trusting its history."""

    if (state is not None and state.get("schemaVersion") == 1
            and set(state).issubset({"schemaVersion", "revision", "updatedAt"})):
        return _revision(state)
    return None


def market_data_version(data_root: Path) -> str:
    """Return the current local-market revision without scanning Silver files."""

    path = _version_path(data_root)
    state = _read_state(path)
    if _valid_state(state):
        return str(state["revision"])
    legacy = _legacy_revision(state)
    if legacy is not None:
        return legacy
    # A damaged ledger is evidence of unknown writes. Never overwrite it or
    # reset its epoch: an older index could otherwise become comparable again.
    if path.exists():
        return _INVALID_REVISION
    with _revision_lock(data_root):
        state = _read_state(path)
        if _valid_state(state):
            return str(state["revision"])
        legacy = _legacy_revision(state)
        if legacy is not None:
            return legacy
        if path.exists():
            return _INVALID_REVISION
        revision = f"bootstrap-{uuid4().hex}"
        _write(path, revision, [], unknown_epoch=_fresh_unknown_epoch())
        return revision


def migrate_legacy_market_data_version(data_root: Path) -> str | None:
    """Upgrade an exact pre-journal ledger at a controlled startup boundary.

    The UNKNOWN transition invalidates every prior index. Only a later
    Catalog/physical full audit can publish a CURRENT replacement.
    Damaged or unfamiliar ledgers remain untouched for manual review.
    """

    path = _version_path(data_root)
    with _revision_lock(data_root):
        state = _read_state(path)
        if _valid_state(state):
            return str(state["revision"])
        legacy = _legacy_revision(state)
        if legacy is None:
            return None
        current = f"migrated-{uuid4().hex}"
        _write(
            path, current,
            [{"previous": legacy, "current": current, "kind": "UNKNOWN"}],
            unknown_epoch=_fresh_unknown_epoch() + 1,
        )
        return current


def advance_market_data_version(
    data_root: Path,
    *,
    changed_file: Path | str | None = None,
    sha256: str | None = None,
    pending: bool = False,
) -> tuple[str, str]:
    """Invalidate Silver readers and return ``(previous, current)``.

    ``changed_file`` is an exact Parquet path below ``data_root/silver`` and
    ``sha256`` is the digest of its new bytes. An omitted or invalid identity
    advances the version as UNKNOWN, forbidding incremental publication. Set
    ``pending`` before mutating a published file and settle after promotion;
    an interrupted mutation then cannot publish a query index as CURRENT.
    """

    root = Path(data_root)
    path = _version_path(root)
    with _revision_lock(root):
        state = _read_state(path)
        if path.exists() and not _valid_state(state) and _legacy_revision(state) is None:
            raise RuntimeError("Silver revision state is damaged; source write refused")
        previous = _revision(state) or f"bootstrap-{uuid4().hex}"
        current = f"r-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:10]}"
        journal = _validated_journal(state)
        unsettled = _validated_pending(state)
        if unsettled is None:
            raise RuntimeError("Silver revision has an invalid pending-write ledger")
        identity = _file_identity(root, changed_file, sha256) if journal is not None else None
        transition: dict[str, object] = {
            "previous": previous,
            "current": current,
            "kind": "FILE" if identity is not None else "UNKNOWN",
        }
        if identity is not None:
            transition["path"], transition["sha256"] = identity
        if pending:
            transition["ownerPid"] = os.getpid()
        # Never fabricate a chain from legacy or damaged state.
        journal = ((journal or []) + [transition])[-_MAX_TRANSITIONS:]
        if pending:
            unsettled.append(current)
        unknown_epoch = _unknown_epoch(state)
        if unknown_epoch is None:
            unknown_epoch = _fresh_unknown_epoch()
        if identity is None:
            unknown_epoch += 1
        _write(path, current, journal, unsettled, unknown_epoch=unknown_epoch)
        return previous, current


def settle_market_data_version(data_root: Path, revision: str) -> None:
    """Mark a previously invalidated Silver write durable after promotion."""

    path = _version_path(data_root)
    with _revision_lock(data_root):
        state = _read_state(path)
        current = _revision(state)
        journal = _validated_journal(state)
        unsettled = _validated_pending(state)
        if current is None or journal is None or unsettled is None or revision not in unsettled:
            raise RuntimeError("Silver pending revision is missing or damaged")
        unsettled.remove(revision)
        _write(path, current, journal, unsettled, unknown_epoch=_unknown_epoch(state))


def revision_is_settled(data_root: Path, revision: str) -> bool:
    """A cache may claim CURRENT only after every published write settles."""

    state = _read_state(_version_path(data_root))
    return (
        _valid_state(state) and _revision(state) == revision
        and _validated_pending(state) == []
    )


def market_unknown_epoch(data_root: Path, *, initialize: bool = False) -> int | None:
    """Return the monotonic count of unproven Silver writes.

    A legacy ledger has no comparable epoch until a complete audited index
    adopts its current physical state. GET callers must leave ``initialize``
    false; the full audit/build path may initialize it under the revision lock.
    """

    path = _version_path(data_root)
    state = _read_state(path)
    if not _valid_state(state):
        return None
    epoch = _unknown_epoch(state)
    if epoch is not None or not initialize:
        return epoch
    with _revision_lock(data_root):
        state = _read_state(path)
        if not _valid_state(state):
            return None
        epoch = _unknown_epoch(state)
        if epoch is not None:
            return epoch
        revision = _revision(state)
        journal = _validated_journal(state)
        pending = _validated_pending(state)
        if (state is None or "unknownEpoch" in state or revision is None
                or journal is None or pending != []):
            return None
        epoch = _fresh_unknown_epoch()
        _write(path, revision, journal, pending, unknown_epoch=epoch)
        return epoch


def pending_market_writes(data_root: Path) -> dict[str, object]:
    """Return a bounded administrative status without exposing local paths."""

    path = _version_path(data_root)
    state = _read_state(path)
    if state is None and path.exists():
        return {"status": "INVALID", "count": None}
    if state is not None and not _valid_state(state):
        return {"status": "INVALID", "count": None}
    pending = _validated_pending(state)
    if pending is None:
        return {"status": "INVALID", "count": None}
    return {"status": "PENDING" if pending else "CLEAR", "count": len(pending)}


def recover_abandoned_pending_write(data_root: Path) -> dict[str, object]:
    """Settle one dead writer only when Catalog and physical bytes prove safety.

    A mismatched Catalog/file pair remains pending for explicit source repair.
    Active writers, unknown bulk moves and ambiguous multiple pending writes
    are never inferred to be complete.
    """

    root = Path(data_root)
    path = _version_path(root)
    state = _read_state(path)
    if (state is None and path.exists()) or (state is not None and not _valid_state(state)):
        return {"status": "REVIEW_REQUIRED", "reason": "INVALID_REVISION_STATE"}
    pending = _validated_pending(state)
    if pending is None:
        return {"status": "REVIEW_REQUIRED", "reason": "INVALID_PENDING_LEDGER"}
    if not pending:
        return {"status": "CLEAR", "pendingCount": 0}
    if len(pending) != 1:
        return {"status": "REVIEW_REQUIRED", "reason": "MULTIPLE_PENDING_WRITES"}
    journal = _validated_journal(state)
    if journal is None:
        return {"status": "REVIEW_REQUIRED", "reason": "INVALID_REVISION_JOURNAL"}
    intent = next((item for item in journal if item["current"] == pending[0]), None)
    if intent is None or intent["kind"] != "FILE":
        return {"status": "REVIEW_REQUIRED", "reason": "UNPROVEN_WRITE_INTENT"}
    owner = intent.get("ownerPid")
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        return {"status": "REVIEW_REQUIRED", "reason": "UNKNOWN_WRITER"}
    if _process_is_running(owner):
        return {"status": "OWNER_RUNNING", "pendingCount": 1}

    relative = str(intent["path"])
    newer = journal[journal.index(intent) + 1:]
    if any(item["kind"] == "UNKNOWN" for item in newer):
        return {"status": "REVIEW_REQUIRED", "reason": "UNKNOWN_LATER_WRITE"}
    latest = next(
        (item for item in reversed(journal) if item["kind"] == "FILE" and item["path"] == relative),
        intent,
    )
    target = root / relative
    try:
        physical_sha = _sha256_file(target) if target.is_file() else None
        catalog_rows = _catalog_partition_rows(root, relative)
    except (OSError, ValueError, RuntimeError):
        return {"status": "RETRY_LATER", "reason": "EVIDENCE_UNAVAILABLE"}
    if catalog_rows is None:
        return {"status": "RETRY_LATER", "reason": "CATALOG_BUSY"}
    if len(catalog_rows) > 1:
        return {"status": "REVIEW_REQUIRED", "reason": "DUPLICATE_CATALOG_PATH"}
    if catalog_rows:
        catalog_sha, catalog_status = catalog_rows[0]
        if catalog_status != "COMPLETE" or not _SHA256.fullmatch(catalog_sha):
            return {"status": "REVIEW_REQUIRED", "reason": "INVALID_CATALOG_PARTITION"}
        if physical_sha != catalog_sha.lower():
            return {"status": "REVIEW_REQUIRED", "reason": "CATALOG_FILE_MISMATCH"}
        # Either the abandoned write was committed, a later verified write
        # superseded it, or its old file remained intact after a failed move.
        if latest is not intent and catalog_sha.lower() != str(latest["sha256"]).lower():
            return {"status": "REVIEW_REQUIRED", "reason": "LATER_WRITE_MISMATCH"}
        recovery = "COMMITTED_OR_SUPERSEDED" if catalog_sha.lower() == str(latest["sha256"]).lower() else "ABORTED"
    elif physical_sha is None and latest is intent:
        recovery = "ABORTED_NEW_PARTITION"
    else:
        return {"status": "REVIEW_REQUIRED", "reason": "UNREGISTERED_FILE"}

    with _revision_lock(root):
        if _read_state(path) != state:
            return {"status": "RETRY_LATER", "reason": "REVISION_CHANGED"}
        _write(path, str(state["revision"]), journal, [], unknown_epoch=_unknown_epoch(state))
    return {"status": "RECOVERED", "mode": recovery, "pendingCount": 0}


def changes_between(data_root: Path, base_revision: str, target_revision: str) -> dict[str, str] | None:
    """Return exact root-relative Silver paths and latest SHA, or ``None``.

    ``target_revision`` must still be current. Legacy journals, unknown
    writes, damaged links and truncated history fail closed.
    """

    state = _read_state(_version_path(data_root))
    if not isinstance(base_revision, str) or not base_revision:
        return None
    if not isinstance(target_revision, str) or target_revision != _revision(state):
        return None
    if _validated_pending(state) != []:
        return None
    journal = _validated_journal(state)
    if journal is None:
        return None
    if base_revision == target_revision:
        return {}
    cursor = target_revision
    changed: dict[str, str] = {}
    for transition in reversed(journal):
        if transition["current"] != cursor or transition["kind"] != "FILE":
            return None
        changed.setdefault(str(transition["path"]), str(transition["sha256"]))
        cursor = str(transition["previous"])
        if cursor == base_revision:
            return changed
    return None


def _version_path(data_root: Path) -> Path:
    return Path(data_root) / "state" / _VERSION_FILE


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _revision(state: dict[str, object] | None) -> str | None:
    revision = state.get("revision") if state is not None else None
    return revision if isinstance(revision, str) and bool(revision) else None


def _file_identity(
    data_root: Path, changed_file: Path | str | None, sha256: str | None,
) -> tuple[str, str] | None:
    if changed_file is None or not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        return None
    try:
        root = Path(data_root).resolve()
        file = Path(changed_file)
        candidates = [file.resolve()] if file.is_absolute() else [
            file.resolve(), (root / file).resolve(),
        ]
    except (OSError, ValueError, TypeError):
        return None
    matches: set[str] = set()
    for candidate in candidates:
        try:
            relative = candidate.relative_to(root)
            parts = relative.parts
            if (
                len(parts) >= 2 and parts[0].casefold() == "silver"
                and relative.suffix.casefold() == ".parquet" and len(relative.as_posix()) <= 1024
            ):
                matches.add(relative.as_posix())
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        return None
    return matches.pop(), sha256.lower()


def _valid_relative_silver_path(path: object) -> bool:
    if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path:
        return False
    if path.startswith("/") or ":" in path:
        return False
    parts = path.split("/")
    return (
        len(parts) >= 2 and parts[0].casefold() == "silver"
        and all(part not in ("", ".", "..") for part in parts)
        and parts[-1].lower().endswith(".parquet")
    )


def _validated_journal(state: dict[str, object] | None) -> list[dict[str, object]] | None:
    if state is None or state.get("schemaVersion") != _SCHEMA_VERSION:
        return None
    if "unknownEpoch" in state and _unknown_epoch(state) is None:
        return None
    journal = state.get("journal")
    if not isinstance(journal, list) or len(journal) > _MAX_TRANSITIONS:
        return None
    revision = _revision(state)
    if revision is None:
        return None
    previous_current: str | None = None
    seen: set[str] = set()
    for item in journal:
        if not isinstance(item, dict):
            return None
        previous = item.get("previous")
        current = item.get("current")
        kind = item.get("kind")
        if (
            not isinstance(previous, str) or not previous
            or not isinstance(current, str) or not current
            or current == previous or current in seen
            or (previous_current is not None and previous != previous_current)
        ):
            return None
        if kind == "FILE":
            if not _valid_relative_silver_path(item.get("path")):
                return None
            sha = item.get("sha256")
            if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
                return None
        elif kind != "UNKNOWN":
            return None
        if "ownerPid" in item:
            owner = item["ownerPid"]
            if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
                return None
        seen.add(current)
        previous_current = current
    if journal and journal[-1]["current"] != revision:
        return None
    return journal


def _validated_pending(state: dict[str, object] | None) -> list[str] | None:
    if state is None:
        return []
    values = state.get("pending", [])
    if not isinstance(values, list) or len(values) > _MAX_TRANSITIONS:
        return None
    if any(not isinstance(value, str) or not value for value in values):
        return None
    if len(set(values)) != len(values):
        return None
    return list(values)


def _unknown_epoch(state: dict[str, object] | None) -> int | None:
    if state is None:
        return None
    value = state.get("unknownEpoch")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _write(
    path: Path, revision: str, journal: list[dict[str, object]], pending: list[str] | None = None,
    *, unknown_epoch: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = {
        "schemaVersion": _SCHEMA_VERSION,
        "revision": revision,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "journal": journal,
        "pending": pending or [],
    }
    if unknown_epoch is not None:
        payload["unknownEpoch"] = unknown_epoch
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(content) > _MAX_STATE_BYTES:
        raise RuntimeError("Silver revision journal exceeds the bounded state size")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _revision_lock(data_root: Path) -> Iterator[None]:
    lock_path = Path(data_root) / "state" / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for Silver revision lock") from None
                    time.sleep(0.025)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for Silver revision lock") from None
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _catalog_partition_rows(root: Path, relative: str) -> list[tuple[str, str]] | None:
    import duckdb

    database = root / "catalog.duckdb"
    if not database.is_file():
        return None
    try:
        with duckdb.connect(str(database)) as connection:
            return [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT sha256, status FROM partitions "
                    "WHERE replace(file_path, chr(92), '/') = ?",
                    [relative],
                ).fetchall()
            ]
    except duckdb.Error:
        return None


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x1000, 0, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            status = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


__all__ = (
    "advance_market_data_version", "changes_between", "market_data_version",
    "migrate_legacy_market_data_version",
    "market_unknown_epoch", "pending_market_writes", "recover_abandoned_pending_write", "revision_is_settled",
    "settle_market_data_version",
)
