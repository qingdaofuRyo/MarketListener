"""Cross-process gate for a coordinated subset of personal-profile writers.

The gate covers a complete logical mutation, not each individual file write.
Profile backup holds its read side only while enumerating and capturing files.
Unregistered writers remain outside this guarantee and must be audited before
the archive can be described as an application-wide consistent snapshot.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from typing import Iterator, Literal


class ProfileGateBusy(TimeoutError):
    """A bounded wait could not enter the profile transaction gate."""


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()
_LOCK_NAME = ".profile-transaction.lock"


def _local_lock(key: str) -> threading.RLock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _gate(root: Path, mode: Literal["read", "write"], timeout: float) -> Iterator[None]:
    root = Path(root)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("个人资料根目录无效")
    root.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(root.resolve()))
    deadline = time.monotonic() + max(0.0, timeout)
    local = _local_lock(key)
    if not local.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise ProfileGateBusy("个人资料正在备份或更新，请稍后重试")
    held: dict[str, tuple[str, object]] = getattr(_HELD, "locks", {})
    stream = None
    nested = key in held
    try:
        if nested:
            previous_mode, _stream = held[key]
            if previous_mode == "read" and mode == "write":
                raise RuntimeError("个人资料读锁不能升级为写锁")
        else:
            path = root / _LOCK_NAME
            if path.is_symlink():
                raise ValueError("个人资料锁路径不能是链接")
            stream = path.open("a+b")
            if path.is_symlink():
                raise ValueError("个人资料锁路径不能是链接")
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(),
                                       msvcrt.LK_NBRLCK if mode == "read" else msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(),
                                    (fcntl.LOCK_SH if mode == "read" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ProfileGateBusy("个人资料正在备份或更新，请稍后重试") from None
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            held[key] = (mode, stream)
            _HELD.locks = held
        yield
    finally:
        if not nested and stream is not None:
            if key in held:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    held.pop(key, None)
            stream.close()
        local.release()


def profile_read_gate(root: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold a shared lock while capturing the complete profile file set."""
    return _gate(root, "read", timeout)


def profile_write_gate(root: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold an exclusive lock for a complete logical profile mutation."""
    return _gate(root, "write", timeout)


__all__ = ("ProfileGateBusy", "profile_read_gate", "profile_write_gate")
