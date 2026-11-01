"""Private, lifecycle-bound ownership of terminals launched by an auto download."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import json
from pathlib import Path
import time

from .process_runtime import process_start_token
from .tdx_afterhours import save_report
from .tdx_download_ui import TERMINALS, _process_image_path


_SCHEMA = "tdx-auto-owned-process-v1"


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _path(data_root: Path, terminal: str) -> Path:
    if terminal not in TERMINALS:
        raise ValueError("unknown terminal")
    return Path(data_root) / "state" / "tdx_download_ui" / f"owned-{terminal}.json"


def _matches(pid: int, token: str, executable: Path) -> bool:
    if pid <= 0 or not token or not _process_alive(pid) or process_start_token(pid) != token:
        return False
    try:
        return _process_image_path(pid).as_posix().casefold() == executable.resolve().as_posix().casefold()
    except (OSError, RuntimeError):
        return False


def remember_auto_owned(data_root: Path, terminal: str, pid: int,
                        executable: Path) -> tuple[int, str]:
    """Record only a PID returned by this auto download's fresh launch."""

    executable = Path(executable).resolve()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        token = process_start_token(pid)
        if token and _matches(pid, token, executable):
            save_report(_path(data_root, terminal), {
                "schema": _SCHEMA, "terminal": terminal, "pid": pid,
                "processStartToken": token, "executable": str(executable),
                "recordedAt": datetime.now(timezone.utc).isoformat(),
            })
            return pid, token
        time.sleep(.1)
    raise RuntimeError("TDX_OWNED_PROCESS_IDENTITY_UNAVAILABLE")


def load_auto_owned(data_root: Path, terminal: str,
                    executable: Path) -> tuple[int, str] | None:
    """An already-running user process cannot acquire auto-close ownership."""

    path = _path(data_root, terminal)
    try:
        if path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid, token = value.get("pid"), value.get("processStartToken")
    if (value.get("schema") != _SCHEMA or value.get("terminal") != terminal
            or type(pid) is not int or not isinstance(token, str)
            or value.get("executable") != str(Path(executable).resolve())):
        return None
    return (pid, token) if _matches(pid, token, Path(executable)) else None


def clear_exited_auto_owned(data_root: Path, terminal: str,
                            pid: int, token: str) -> None:
    """Drop a matching record only after the exact process has exited."""

    path = _path(data_root, terminal)
    try:
        if path.stat().st_size > 4096:
            return
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if (value.get("schema") == _SCHEMA and value.get("terminal") == terminal
            and value.get("pid") == pid and value.get("processStartToken") == token
            and not _process_alive(pid)):
        path.unlink(missing_ok=True)


__all__ = ("remember_auto_owned", "load_auto_owned", "clear_exited_auto_owned")
