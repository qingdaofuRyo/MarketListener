"""Cheap process diagnostics, without catalog reads or third-party collectors."""

from __future__ import annotations

import os
from pathlib import Path


def process_start_token(pid: int) -> str | None:
    """Return an OS lifecycle identity, rather than treating a reused PID as its owner."""
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                return f"windows-filetime:{created.dwHighDateTime << 32 | created.dwLowDateTime}"
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        # The command name in parentheses can contain spaces or parentheses.
        # Remaining fields start at field 3; process starttime is field 22.
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        started = int(fields[19])
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux-start:{boot}:{started}"
    except (OSError, ValueError, IndexError):
        return None


def process_runtime() -> dict:
    times = os.times()
    memory: int | None = None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in (
                        "peak_working_set", "working_set", "peak_paged", "paged",
                        "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile",
                    )
                ]

            kernel = ctypes.WinDLL("kernel32")
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi")
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                memory = counters.working_set
        except (OSError, AttributeError):
            pass
    elif Path("/proc/self/statm").is_file():
        try:
            memory = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError):
            pass
    return {"pid": os.getpid(), "cpuSeconds": times.user + times.system, "logicalCpus": os.cpu_count() or 1, "memoryBytes": memory}
