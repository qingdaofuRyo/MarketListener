"""Standalone Windows download-dialog driver for the two local terminals.

This adapter requires desktop acceptance before unattended use. It deliberately
fails on unknown controls instead of sending guessed coordinates or keystrokes.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Callable

from .tdx_afterhours import BEIJING, latest_slot, process_lock, save_report
from .process_runtime import process_start_token


@dataclass(frozen=True)
class Batch:
    key: str
    tab: str
    checkbox: str
    all_checkbox: str | None = None
    saved_selection: bool = False


FINANCIAL = (
    Batch("cn-day", "沪深京日线", "日线和实时行情数据", "下载所有AB股类品种的日线数据"),
    Batch("cn-5m", "沪深京分钟线", "5分钟线数据", "下载所有AB股类品种的分钟线数据"),
    Batch("extended-day", "扩展市场行情日线", "日线数据", saved_selection=True),
    Batch("extended-5m", "扩展市场行情分钟线", "5分钟线数据", saved_selection=True),
)
FUTURES = (
    Batch("future-day", "期货市场行情日线", "日线数据", "下载所有国内期货日线数据"),
    Batch("future-5m", "期货市场行情分钟线", "5分钟线数据", "下载所有国内期货分钟数据"),
)
TERMINALS = {
    "financial": (Path(r"C:\tongdaxin"), FINANCIAL, "通达信金融终端"),
    "futures": (Path(r"C:\new_tdxqh"), FUTURES, "通达信期货通"),
}


def _system_input_idle_seconds() -> float:
    """Age of Windows input, including synthetic input; never identify a person."""
    import ctypes
    from ctypes import wintypes

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetLastInputInfo.argtypes = (ctypes.POINTER(LastInputInfo),)
    user32.GetLastInputInfo.restype = wintypes.BOOL
    kernel32.GetTickCount.restype = wintypes.DWORD
    info = LastInputInfo(ctypes.sizeof(LastInputInfo), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise StopRun("INPUT_IDLE_STATUS_UNAVAILABLE")
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def _wait_for_input_quiet(check: Callable[[], None], *,
                          idle_seconds: Callable[[], float] = _system_input_idle_seconds,
                          cancelled: Callable[[], bool] | None = None) -> None:
    """Wait up to a minute, failing closed on activity, lock, or cancellation."""
    deadline = time.monotonic() + 60
    while True:
        check()
        if cancelled is not None and cancelled():
            raise StopRun("INPUT_IDLE_WAIT_CANCELLED")
        if idle_seconds() >= 10:
            check()
            return
        if time.monotonic() >= deadline:
            raise StopRun("WAITING_FOR_INPUT_IDLE")
        time.sleep(.25)
PERIOD_LABELS = {"日线和实时行情数据", "日线数据", "1分钟线数据", "5分钟线数据"}
KNOWN_DAY_ONLY_EXTENDED_CODES = frozenset({"ZR00W"})
_COMPLETE_BATCH_STATUSES = frozenset({"UI_DONE_UNVERIFIED", "SKIPPED_RECENT_RAW"})
_TERMINAL_FAILURE_REASONS = frozenset({
    "TERMINAL_DOWNLOAD_CANCELLED", "MARKET_CONNECTION_DROPPED",
    "EXTENDED_MARKET_HOST_DISCONNECTED", "FUTURES_MARKET_HOST_DISCONNECTED",
    "MARKET_HOST_INITIALIZING",
    "TERMINAL_DOWNLOAD_FAILED",
})


class StopRun(RuntimeError):
    """Only allowlisted reason codes may be persisted (no vendor/account text)."""


def plain(value: str) -> str:
    return re.sub(r"[&*\s]", "", value)


def completion_state(message: str, button: str, observed_busy: bool) -> str:
    if "取消下载" in button or "正在下载" in message:
        return "BUSY"
    if any(word in message for word in (
        "失败", "错误", "中断", "取消", "超时", "请先连接扩展市场行情主站",
        "请先连接期货市场行情主站",
    )):
        return "FAILED"
    if observed_busy and "开始下载" in button and re.search(r"下载(?:全部)?(?:完成|完毕)|数据接收完毕", message):
        return "UI_DONE_UNVERIFIED"
    return "UNKNOWN"


def extended_host_retry_prompt(message: str) -> bool:
    """Recognize only terminal text for the user's bounded reconnect action."""

    return ("请先连接扩展市场行情主站" in message or
            ("连接失败" in message and "下载中断" in message) or
            ("行情断开" in message and "下载失败" in message))


def market_host_retry_prompt(message: str) -> bool:
    return (extended_host_retry_prompt(message) or
            "请先连接期货市场行情主站" in message)


def terminal_failure_reason(message: str) -> str:
    """Persist an allowlisted UI outcome, never arbitrary terminal text."""

    if "行情断开" in message or ("连接失败" in message and "下载中断" in message):
        return "MARKET_CONNECTION_DROPPED"
    if "请先连接扩展市场行情主站" in message:
        return "EXTENDED_MARKET_HOST_DISCONNECTED"
    if "请先连接期货市场行情主站" in message:
        return "FUTURES_MARKET_HOST_DISCONNECTED"
    if "下载被取消" in message or "已取消" in message:
        return "TERMINAL_DOWNLOAD_CANCELLED"
    return "TERMINAL_DOWNLOAD_FAILED"


def selection_digest(codes: list[str]) -> str:
    if not codes or any(not re.fullmatch(r"[A-Za-z0-9_.#-]+", c) for c in codes):
        raise StopRun("EMPTY_OR_UNREADABLE_SELECTION")
    # R4-T049-DATA retired the foreign 00Y continuous series.  The UI driver
    # intentionally reuses the user's saved extended selection, so guard the
    # selection here rather than silently bringing that retired scope back.
    if any(re.search(r"00Y(?:$|[.#_-])", code.upper()) for code in codes):
        raise StopRun("RETIRED_00Y_SELECTION")
    return hashlib.sha256("\n".join(sorted(set(codes))).encode()).hexdigest()


def pending_keys(batches, records, slot):
    return [b for b in batches if records.get(b.key, {}).get("slot") != slot
            or records[b.key].get("status") not in _COMPLETE_BATCH_STATUSES]


@contextmanager
def _checkpoint_write_lock(path: Path):
    """Serialize brief shared JSON updates without serializing terminal downloads."""

    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 10
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StopRun("CHECKPOINT_WRITE_LOCK_BUSY") from None
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _foreground_input_lock(path: Path):
    """Serialize only focus-sensitive input across the two worker processes."""

    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 120
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StopRun("FOREGROUND_INPUT_LOCK_BUSY") from None
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _read_checkpoint_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("checkpoint state must be an object")
    return value


def _save_terminal_checkpoint(path: Path, terminal: str, entries: dict) -> None:
    with _checkpoint_write_lock(path.parent / "checkpoints.write.lock"):
        state = _read_checkpoint_state(path)
        state[terminal] = entries
        save_report(path, state)


def _save_latest_terminal_result(state_dir: Path, report: dict,
                                 selected: tuple[str, ...], outcomes: dict[str, str]) -> None:
    latest_path = state_dir / "latest.json"
    with _checkpoint_write_lock(state_dir / "checkpoints.write.lock"):
        try:
            previous = json.loads(latest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            previous = {}
        previous_terminals = previous.get("terminals") if isinstance(previous, dict) else None
        merged = (dict(previous_terminals)
                  if isinstance(previous_terminals, dict)
                  and previous.get("slot") == report["slot"] else {})
        state = _read_checkpoint_state(state_dir / "checkpoints.json")
        for terminal in TERMINALS:
            merged.setdefault(terminal, terminal_completion(
                state.get(terminal, {}), report["slot"], TERMINALS[terminal][1]))
        for terminal in selected:
            merged[terminal] = outcomes[terminal]
        report["terminals"] = merged
        report["status"] = ("UI_DONE_UNVERIFIED" if all(
            merged.get(terminal) == "UI_DONE_UNVERIFIED" for terminal in TERMINALS
        ) else "COMPLETE_WITH_SKIPS" if all(
            merged.get(terminal) in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}
            for terminal in TERMINALS
        ) else "ATTENTION_REQUIRED")
        save_report(latest_path, report)


def _process_image_path(pid: int) -> Path:
    """Read the running executable path without touching the process state."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        raise StopRun("OWNED_PROCESS_IDENTITY_UNAVAILABLE")
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            raise StopRun("OWNED_PROCESS_IDENTITY_UNAVAILABLE")
        return Path(path.value).resolve()
    finally:
        kernel32.CloseHandle(handle)


def _wait_process_exit(pid: int, timeout_seconds: int) -> bool:
    """Wait for a normal WM_CLOSE exit; never terminate an unresponsive app."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Process already exited.
            return True
        raise StopRun("OWNED_PROCESS_EXIT_UNVERIFIED")
    try:
        return kernel32.WaitForSingleObject(handle, timeout_seconds * 1000) == 0
    finally:
        kernel32.CloseHandle(handle)


class DialogDriver:
    def __init__(self, exe: Path):
        # Imported only in the Windows worker, not pure-logic tests.
        from pywinauto import Application, Desktop
        self.Application, self.Desktop = Application, Desktop
        self.exe = exe.resolve()
        self.app = None
        self.dialog = None
        self.launched_pid: int | None = None
        self.foreground_lock_path: Path | None = None
        self.extended_minute_start: date | None = None
        self.batch_start_dates: dict[str, date] = {}
        self.connection_retries_remaining = 0
        self.last_retryable_connection_prompt = False

    def wait_foreground_quiet(self):
        """Yield before focus/mouse input; the signal also includes our input."""
        if getattr(self, "foreground_lock_path", None) is None:
            return  # Direct test adapters don't use the unattended GUI workflow.
        def check():
            self.unlocked()
            if self.app is not None:
                self.ensure_connected_process()
        _wait_for_input_quiet(check)

    @staticmethod
    def unlocked():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD,
                                                   ctypes.POINTER(wintypes.DWORD)]
        handle = user32.OpenInputDesktop(0, False, 0x0001)
        if not handle:
            raise StopRun("DESKTOP_LOCKED")
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not user32.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                raise StopRun("DESKTOP_LOCKED")
            if name.value.casefold() != "default":
                raise StopRun("DESKTOP_LOCKED")
        finally:
            user32.CloseDesktop(handle)

    @staticmethod
    def login_saved_account(window):
        """Click the known V7.71 saved-account login button once, without reading credentials."""

        if (window.class_name() != "#32770" or
                not re.fullmatch(r"通达信金融终端V\d+\.\d+", window.window_text())):
            raise StopRun("SAVED_LOGIN_WINDOW_UNRECOGNIZED")
        rectangle = window.rectangle()
        if not 540 <= rectangle.width() <= 590 or not 400 <= rectangle.height() <= 455:
            raise StopRun("SAVED_LOGIN_LAYOUT_CHANGED")
        window.set_focus()
        window.click_input(coords=(round(rectangle.width() * .348), round(rectangle.height() * .865)))

    def connect(self, *, auto_login_saved: bool = False):
        self.unlocked()
        if not self.exe.is_file():
            raise StopRun("EXECUTABLE_MISSING")
        from pywinauto.application import ProcessNotFoundError
        try:
            self.app = self.Application(backend="win32").connect(path=str(self.exe), timeout=5)
        except ProcessNotFoundError:
            raise StopRun("CLIENT_NOT_RUNNING") from None
        self.connected_start_token = process_start_token(self.app.process)
        if self.connected_start_token is None:
            raise StopRun("CLIENT_PROCESS_IDENTITY_UNAVAILABLE")
        # Starting the terminal is an explicit launcher action.  Connecting
        # here must never create a new process or automate authentication.
        deadline = time.monotonic() + (60 if auto_login_saved else 15)
        login_clicked = False
        while time.monotonic() < deadline:
            self.unlocked()
            windows = self.windows()
            if any(" - [" in window.window_text() for window in windows):
                self.guard()
                return
            login_windows = [window for window in windows if window.class_name() == "#32770"
                             and re.fullmatch(r"通达信金融终端V\d+\.\d+", window.window_text())]
            if login_windows:
                if not auto_login_saved:
                    raise StopRun("WAITING_FOR_LOGIN")
                if len(login_windows) != 1:
                    raise StopRun("AMBIGUOUS_SAVED_LOGIN_WINDOW")
                if not login_clicked:
                    lock_path = getattr(self, "foreground_lock_path", None)
                    with _foreground_input_lock(lock_path) if lock_path is not None else nullcontext():
                        self.wait_foreground_quiet()
                        self.login_saved_account(login_windows[0])
                    login_clicked = True
            time.sleep(1)
        raise StopRun("WAITING_FOR_LOGIN_OR_MAIN_WINDOW")

    def start(self) -> str:
        """Start the selected terminal; saved login is a separate explicit download option."""

        self.unlocked()
        if not self.exe.is_file():
            raise StopRun("EXECUTABLE_MISSING")
        from pywinauto.application import ProcessNotFoundError

        self.launched_pid = None

        try:
            self.Application(backend="win32").connect(path=str(self.exe), timeout=5)
        except ProcessNotFoundError:
            self.launched_pid = subprocess.Popen([str(self.exe)], cwd=self.exe.parent).pid
            return "LAUNCHED"
        return "ALREADY_RUNNING"

    def windows(self):
        return self.app.windows(visible_only=True)

    def guard(self):
        self.unlocked()
        # During download the main quote window can briefly stop appearing in
        # the visible-window enumeration while the exact download dialog stays
        # present. Keep checking the same process without reading credentials.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            windows = self.windows()
            if any("登录" in w.window_text() or
                   (w.class_name() == "#32770" and re.fullmatch(
                       r"通达信金融终端V\d+\.\d+", w.window_text()))
                   for w in windows):
                raise StopRun("WAITING_FOR_LOGIN")
            if any(" - [" in w.window_text() or w.window_text() == "盘后数据下载"
                   for w in windows):
                return
            dialog = getattr(self, "dialog", None)
            if dialog is not None:
                try:
                    caption = dialog.window_text()
                    if dialog.is_visible() and caption == "盘后数据下载":
                        return
                    if (dialog.is_visible() and dialog.class_name() == "#32770"
                            and caption == "" and any(w.class_name() == "TdxW_MainFrame_Class"
                                                for w in windows)):
                        # The financial terminal blanks both captions while
                        # switching into the extended-minute tab. The already
                        # verified dialog handle and main-window class remain.
                        return
                except (AttributeError, RuntimeError, OSError):
                    pass
            time.sleep(.1)
        raise StopRun("WAITING_FOR_LOGIN_OR_MAIN_WINDOW")

    def find_dialog(self):
        matches = [w for w in self.windows() if w.window_text() == "盘后数据下载"]
        if len(matches) > 1:
            raise StopRun("AMBIGUOUS_DIALOG")
        return matches[0] if matches else None

    def open_financial_native_menu(self, main):
        """Use the verified V7.71 Options control and its native menu text."""

        controls = [child for child in main.descendants()
                    if child.class_name() == "AfxWnd100" and child.control_id() == 3019
                    and child.is_visible()]
        if len(controls) != 1:
            raise StopRun("FINANCIAL_OPTIONS_CONTROL_MISSING")
        self.wait_foreground_quiet()
        self.guard()
        if not controls[0].is_visible():
            raise StopRun("FINANCIAL_OPTIONS_CONTROL_MISSING")
        controls[0].click_input()
        deadline = time.monotonic() + 3
        saw_menu = False
        while time.monotonic() < deadline:
            menus = [window for window in self.Desktop(backend="win32").windows(visible_only=True)
                     if window.class_name() == "#32768" and window.process_id() == main.process_id()]
            saw_menu |= bool(menus)
            matches = []
            for menu in menus:
                try:
                    matches.extend(item for item in menu.menu().items()
                                   if item.text().replace("&", "").split("\t", 1)[0] == "盘后数据下载"
                                   and item.is_enabled())
                except (AttributeError, RuntimeError):
                    continue
            if len(matches) == 1:
                self.wait_foreground_quiet()
                self.guard()
                if not matches[0].is_enabled():
                    raise StopRun("FINANCIAL_DOWNLOAD_MENU_MISSING")
                matches[0].click_input()
                return
            if len(matches) > 1:
                raise StopRun("FINANCIAL_DOWNLOAD_MENU_AMBIGUOUS")
            time.sleep(.1)
        raise StopRun("FINANCIAL_DOWNLOAD_MENU_MISSING" if saw_menu else "FINANCIAL_OPTIONS_MENU_MISSING")

    def open_futures_native_menu(self, main):
        """Use the verified V1.75 System control and native menu label."""

        controls = [child for child in main.descendants()
                    if child.class_name() == "AfxWnd100" and child.control_id() == 1000
                    and child.is_visible()]
        if len(controls) != 1:
            raise StopRun("FUTURES_SYSTEM_CONTROL_MISSING")
        self.wait_foreground_quiet()
        self.guard()
        if not controls[0].is_visible():
            raise StopRun("FUTURES_SYSTEM_CONTROL_MISSING")
        controls[0].click_input()
        # Win32 creates the popup after the click returns. On this terminal a
        # zero-wait lookup intermittently sees no #32768 window at all.
        deadline = time.monotonic() + 3
        saw_menu = False
        while time.monotonic() < deadline:
            menus = [window for window in self.Desktop(backend="win32").windows(visible_only=True)
                     if window.class_name() == "#32768" and window.process_id() == main.process_id()]
            saw_menu |= bool(menus)
            matches = []
            for menu in menus:
                try:
                    matches.extend(item for item in menu.menu().items()
                                   if item.text().replace("&", "").split("\t", 1)[0] == "盘后数据下载"
                                   and item.is_enabled())
                except (AttributeError, RuntimeError):
                    continue
            if len(matches) == 1:
                self.wait_foreground_quiet()
                self.guard()
                if not matches[0].is_enabled():
                    raise StopRun("FUTURES_DOWNLOAD_MENU_MISSING")
                matches[0].click_input()
                return
            if len(matches) > 1:
                raise StopRun("FUTURES_DOWNLOAD_MENU_AMBIGUOUS")
            time.sleep(.1)
        raise StopRun("FUTURES_DOWNLOAD_MENU_MISSING" if saw_menu else "FUTURES_SYSTEM_MENU_MISSING")

    def open(self):
        self.guard()
        if self.acknowledge_host_initializing():
            raise StopRun("MARKET_HOST_INITIALIZING")
        self.dialog = self.find_dialog()
        if self.dialog:
            self.ensure_idle()
            return
        ads = [w for w in self.windows() if w.window_text() == "通达信信息"]
        for ad in ads:
            ad.close()  # Exact same-process ordinary advertisement only.
        mains = [w for w in self.windows() if " - [" in w.window_text()]
        if len(mains) != 1:
            raise StopRun("AMBIGUOUS_MAIN_WINDOW")
        main = mains[0]
        if not main.is_enabled():
            raise StopRun("UNEXPECTED_MODAL")
        menu = "选项" if "金融终端" in main.window_text() else "系统"
        self.wait_foreground_quiet()
        self.guard()
        try:
            main.set_focus()
            main.menu_select(f"{menu}->盘后数据下载")
        except Exception:
            if menu == "选项":
                self.open_financial_native_menu(main)
            else:
                self.open_futures_native_menu(main)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.dialog = self.find_dialog()
            if self.dialog:
                if self.acknowledge_host_initializing():
                    raise StopRun("MARKET_HOST_INITIALIZING")
                self.ensure_idle()
                return
            time.sleep(1)
        raise StopRun("DOWNLOAD_DIALOG_NOT_FOUND")

    def control(self, control_id: int):
        return self.app.window(handle=self.dialog.handle).child_window(control_id=control_id).wrapper_object()

    def status(self):
        self.guard()
        return self.control(1224).window_text(), self.control(1).window_text()

    def acknowledge_host_initializing(self) -> bool:
        """Dismiss only the verified one-button vendor initialization notice."""

        matches = []
        for window in self.windows():
            if (window.class_name() != "#32770" or not window.is_enabled()
                    or window.process_id() != self.app.process):
                continue
            children = window.children()
            notices = [item.window_text() for item in children
                       if item.class_name() == "Static" and item.control_id() == 1726]
            if len(notices) != 1 or not all(fragment in notices[0] for fragment in (
                    "行情连接被主站断开", "当前主站可能正在初始化今天的数据",
                    "几分钟后会自动重新进入")):
                continue
            buttons = [item for item in children
                       if item.class_name() == "Button" and item.control_id() == 1
                       and item.window_text() == "确定" and item.is_enabled()]
            if len(buttons) != 1:
                raise StopRun("MARKET_HOST_NOTICE_LAYOUT_CHANGED")
            matches.append(buttons[0])
        if len(matches) > 1:
            raise StopRun("MARKET_HOST_NOTICE_AMBIGUOUS")
        if not matches:
            return False
        matches[0].click()
        return True

    def ensure_idle(self):
        message, button = self.status()
        if "取消下载" in button or "正在下载" in message:
            raise StopRun("EXISTING_DOWNLOAD_BUSY")
        if plain(button) != "开始下载" or not self.control(1).is_enabled():
            raise StopRun("DOWNLOAD_DIALOG_NOT_IDLE")

    def tab_names(self):
        tabs = self.control(32954)
        return [tabs.get_tab_text(i) for i in range(tabs.tab_count())]

    def select_tab(self, name):
        self.guard()
        names = self.tab_names()
        matches = [i for i, text in enumerate(names) if plain(text) == name]
        if len(matches) != 1:
            raise StopRun("TAB_NOT_FOUND")
        self.control(32954).select(matches[0])
        # Switching into an extended-market tab can reconnect its separate
        # quote host. Only the exact initialization notice may be dismissed.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.unlocked()
            if self.acknowledge_host_initializing():
                raise StopRun("MARKET_HOST_INITIALIZING")
            if self.dialog.is_enabled():
                return
            time.sleep(.25)
        raise StopRun("WAITING_FOR_MARKET_CONNECTION")

    def reconnect_extended_host(self):
        """Click the other verified extended tab when the terminal requests its host."""

        return self._reconnect_host_tabs("请先连接扩展市场行情主站", "扩展市场行情日线",
                                         "扩展市场行情分钟线", "EXTENDED")

    def reconnect_futures_host(self):
        """Click the other verified futures tab when its quote host is disconnected."""

        return self._reconnect_host_tabs("请先连接期货市场行情主站", "期货市场行情日线",
                                         "期货市场行情分钟线", "FUTURES")

    def _reconnect_host_tabs(self, prompt, day_tab, minute_tab, reason_prefix):
        message, button = self.status()
        if prompt not in message:
            return False
        if plain(button) != "开始下载":
            raise StopRun(f"{reason_prefix}_HOST_RECONNECT_NOT_IDLE")
        tabs = self.control(32954)
        names = [plain(value) for value in self.tab_names()]
        day = [i for i, value in enumerate(names) if value == day_tab]
        minute = [i for i, value in enumerate(names) if value == minute_tab]
        if len(day) != 1 or len(minute) != 1 or len(names) != 5:
            raise StopRun(f"{reason_prefix}_HOST_TABS_UNVERIFIED")
        current = tabs.get_selected_tab()
        target = minute[0] if current == day[0] else day[0]
        self.wait_foreground_quiet()
        self.guard()
        if not self.dialog.is_enabled() or tabs.get_selected_tab() != current:
            raise StopRun(f"{reason_prefix}_HOST_TABS_UNVERIFIED")
        self.dialog.set_focus()
        rectangle = tabs.get_tab_rect(target)
        tabs.click_input(coords=((rectangle.left + rectangle.right) // 2,
                                 (rectangle.top + rectangle.bottom) // 2))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.unlocked()
            if tabs.get_selected_tab() == target and self.dialog.is_enabled():
                return True
            time.sleep(.25)
        raise StopRun(f"{reason_prefix}_HOST_RECONNECT_UNVERIFIED")

    def checkbox(self, label, desired, *, expected_tab):
        """Wait briefly only for a missing control on the verified active tab."""
        safe_labels = PERIOD_LABELS | {
            batch.all_checkbox for batch in (*FINANCIAL, *FUTURES) if batch.all_checkbox
        }
        safe_label = (label if label in safe_labels else "扩展市场范围"
                      if plain(label).startswith("下载所有扩展市场") else "未识别控件")
        safe_tab = expected_tab if expected_tab in {batch.tab for batch in (*FINANCIAL, *FUTURES)} else "其他标签"
        deadline = time.monotonic() + 2
        while True:
            self.guard()
            if not self.dialog.is_visible() or not self.dialog.is_enabled():
                raise StopRun("CHECKBOX_DIALOG_NOT_READY")
            names = [plain(name) for name in self.tab_names()]
            matches_tab = [i for i, name in enumerate(names) if name == plain(expected_tab)]
            if len(matches_tab) != 1 or self.control(32954).get_selected_tab() != matches_tab[0]:
                raise StopRun("CHECKBOX_TAB_CHANGED")
            matches = [c for c in self.dialog.children()
                       if c.is_visible() and plain(c.window_text()) == plain(label)]
            if len(matches) > 1 or (matches and not hasattr(matches[0], "get_check_state")):
                print(f"[通达信] 控件检查失败：标签={safe_tab} 控件={safe_label} 匹配数={len(matches)}", flush=True)
                raise StopRun("CHECKBOX_NOT_FOUND")
            if matches:
                break
            if time.monotonic() >= deadline:
                print(f"[通达信] 控件等待超时：标签={safe_tab} 控件={safe_label} 匹配数=0", flush=True)
                raise StopRun("CHECKBOX_NOT_FOUND")
            time.sleep(.2)
        control = matches[0]
        if not control.is_enabled():
            raise StopRun("CHECKBOX_DISABLED")
        if control.get_check_state() != int(desired):
            self.wait_foreground_quiet()
            self.guard()
            if (not self.dialog.is_enabled() or self.control(32954).get_selected_tab() != matches_tab[0]
                    or not control.is_visible() or not control.is_enabled()):
                raise StopRun("CHECKBOX_DIALOG_NOT_READY")
            control.check_by_click() if desired else control.uncheck_by_click()
        for _ in range(10):
            if control.get_check_state() == int(desired):
                return
            time.sleep(.1)
        raise StopRun("CHECKBOX_STATE_MISMATCH")

    def set_batch_start(self, start_day: date) -> None:
        """Change only the enabled start picker; preserve the saved end date."""

        start, end = self.control(1237), self.control(1238)
        if (start.class_name() != "SysDateTimePick32" or not start.is_visible()
                or not start.is_enabled() or end.class_name() != "SysDateTimePick32"
                or not end.is_visible()):
            raise StopRun("BATCH_START_DATE_CONTROL_MISSING")
        previous_end = end.get_time()
        end_day = date(previous_end.wYear, previous_end.wMonth, previous_end.wDay)
        if start_day > end_day:
            raise StopRun("BATCH_START_AFTER_SAVED_END")
        start.set_time(year=start_day.year, month=start_day.month, day=start_day.day)
        current_start = start.get_time()
        if ((current_start.wYear, current_start.wMonth, current_start.wDay)
                != (start_day.year, start_day.month, start_day.day) or end.get_time() != previous_end):
            raise StopRun("BATCH_START_DATE_MISMATCH")

    def set_extended_minute_start(self, start_day: date) -> None:
        """Compatibility entry for the previously supported extended 5-minute tab."""

        self.set_batch_start(start_day)

    def prepare_many(self, batches):
        """Select all requested periods before one guarded download click."""

        self.ensure_idle()
        if not batches or len({batch.key for batch in batches}) != len(batches):
            raise StopRun("INVALID_BATCH_GROUP")
        names = [plain(n) for n in self.tab_names()]
        if len(names) != 5 or not all("线" in n or "分时图" in n for n in names):
            raise StopRun("UNSUPPORTED_TAB_LAYOUT")
        for name in names:
            self.select_tab(name)
            for control in self.dialog.children():
                label = plain(control.window_text())
                if label in PERIOD_LABELS and control.is_visible() and control.is_enabled():
                    self.checkbox(label, False, expected_tab=name)
            # Don't accidentally download a preselected intraday diagram task.
            if "分时图" in name:
                for c in self.dialog.children():
                    if c.is_visible() and c.friendly_class_name() == "CheckBox" and c.get_check_state():
                        raise StopRun("UNSUPPORTED_SELECTED_INTRADAY_TASK")
        evidence_by_key = {}
        for batch in batches:
            self.select_tab(batch.tab)
            self.checkbox(batch.checkbox, True, expected_tab=batch.tab)
            if batch.all_checkbox:
                self.checkbox(batch.all_checkbox, True, expected_tab=batch.tab)
            if batch.saved_selection:
                all_boxes = [c for c in self.dialog.children()
                             if plain(c.window_text()).startswith("下载所有扩展市场")]
                if len(all_boxes) != 1:
                    raise StopRun("EXTENDED_SCOPE_CONTROL_MISSING")
                self.checkbox(all_boxes[0].window_text(), False, expected_tab=batch.tab)
                codes = [self.control(1732).get_item(i, 0).text()
                         for i in range(self.control(1732).item_count())]
                digest = selection_digest(codes)
            else:
                digest = None
            planned_start = self.batch_start_dates.get(batch.key)
            if planned_start is None and batch.key == "extended-5m":
                planned_start = self.extended_minute_start
            if planned_start is not None:
                self.set_batch_start(planned_start)
            evidence = {"selectionHash": digest, "datePolicy": "CLIENT_SAVED_PROGRESS"}
            if planned_start is not None:
                evidence["datePolicy"] = "LOCAL_COVERAGE_START_CLIENT_END"
                evidence["startDate"] = planned_start.isoformat()
            if batch.key == "extended-day":
                excluded = sorted(set(codes) & KNOWN_DAY_ONLY_EXTENDED_CODES)
                evidence["knownDayOnlyExcludedFromMinute"] = excluded
                evidence["minuteComparableHash"] = selection_digest(
                    [code for code in codes if code not in KNOWN_DAY_ONLY_EXTENDED_CODES]
                )
            evidence_by_key[batch.key] = evidence
        selected = [plain(n) for n in self.tab_names() if "*" in n]
        if selected != [batch.tab for batch in batches]:
            raise StopRun("UNEXPECTED_SELECTED_BATCHES")
        return evidence_by_key

    def prepare(self, batch):
        return self.prepare_many((batch,))[batch.key]

    def wait_futures_window_stable(self, batches):
        """Allow a reconnect animation to settle; this does not prove host readiness."""
        expected = [batch.tab for batch in batches]
        dialog = self.dialog
        if dialog is None:
            raise StopRun("DOWNLOAD_DIALOG_NOT_FOUND")
        handle = dialog.handle
        stable = 0
        # Up to 30 seconds for a temporarily disabled connecting dialog, then
        # require three consecutive seconds with verified controls.
        for _ in range(132):
            self.ensure_connected_process()
            self.guard()
            current = self.find_dialog()
            if (current is None or current.handle != handle or current.process_id() != self.app.process
                    or current.class_name() != "#32770" or not current.is_visible()):
                raise StopRun("DOWNLOAD_DIALOG_CHANGED")
            if self.acknowledge_host_initializing():
                raise StopRun("MARKET_HOST_INITIALIZING")
            if not current.is_enabled():
                stable = 0
                time.sleep(.25)
                continue
            self.ensure_idle()
            if [plain(n) for n in self.tab_names() if "*" in n] != expected:
                raise StopRun("UNEXPECTED_SELECTED_BATCHES")
            stable += 1
            time.sleep(.25)
            if stable == 12:
                return
        raise StopRun("WAITING_FOR_MARKET_CONNECTION")

    def begin_download(self):
        """Observe BUSY; retry one explicit quote-host prompt once."""

        self.last_failure_reason = None
        self.ensure_idle()
        for attempt in range(2):
            self.ensure_connected_process()
            self.guard()
            self.control(1).click()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.ensure_connected_process()
                if (getattr(self, "dialog", None) is not None
                        and not self.dialog.is_enabled()
                        and self.acknowledge_host_initializing()):
                    self.last_failure_reason = "MARKET_HOST_INITIALIZING"
                    raise StopRun(self.last_failure_reason)
                message, button = self.status()
                state = completion_state(message, button, False)
                if state == "BUSY":
                    self.last_failure_reason = None
                    return
                if state == "FAILED":
                    self.last_failure_reason = terminal_failure_reason(message)
                    if (market_host_retry_prompt(message) and attempt == 0
                            and self.connection_retries_remaining > 0
                            and plain(button) == "开始下载"):
                        # The vendor can use the first click to reconnect the
                        # quote host; one more click starts the saved batch.
                        self.connection_retries_remaining -= 1
                        # Give the quote host a short, bounded opportunity to
                        # reconnect. Elapsed time is not a readiness signal.
                        time.sleep(3 if "请先连接期货市场行情主站" in message else 1)
                        self.ensure_connected_process()
                        self.guard()
                        refreshed_message, refreshed_button = self.status()
                        if completion_state(refreshed_message, refreshed_button, False) == "BUSY":
                            self.last_failure_reason = None
                            return
                        if plain(refreshed_button) != "开始下载":
                            raise StopRun("DOWNLOAD_START_UNVERIFIED")
                        if (completion_state(refreshed_message, refreshed_button, False) == "FAILED"
                                and not market_host_retry_prompt(refreshed_message)):
                            raise StopRun(terminal_failure_reason(refreshed_message))
                        break
                    raise StopRun(self.last_failure_reason)
                time.sleep(.2)
            else:
                raise StopRun("DOWNLOAD_START_UNVERIFIED")
        raise StopRun("DOWNLOAD_START_UNVERIFIED")

    def wait_download(self, timeout):
        """Read the existing dialog without focus changes while other terminal works."""

        deadline = time.monotonic() + timeout
        needs_busy_confirmation = False
        self.last_failure_reason = None
        self.last_retryable_connection_prompt = False
        while time.monotonic() < deadline:
            self.ensure_connected_process()
            try:
                if (getattr(self, "dialog", None) is not None
                        and not self.dialog.is_enabled()
                        and self.acknowledge_host_initializing()):
                    self.last_failure_reason = "MARKET_HOST_INITIALIZING"
                    return "FAILED"
                message, button = self.status()
            except Exception as error:  # noqa: BLE001 - a transient Win32 handle must not erase outcome
                if type(error).__name__ not in {"ElementNotFoundError", "InvalidWindowHandle"}:
                    raise
                # The extended-market host briefly rebuilds the dialog. Only
                # resume reading if exactly one same-process download window
                # reappears; otherwise retain RUNNING_UNKNOWN for review.
                candidate = self.find_dialog()
                if candidate is not None:
                    self.dialog = candidate
                    needs_busy_confirmation = True
                time.sleep(.5)
                continue
            state = completion_state(message, button, True)
            if needs_busy_confirmation:
                if state == "BUSY":
                    needs_busy_confirmation = False
                elif state == "UI_DONE_UNVERIFIED":
                    raise StopRun("DOWNLOAD_REATTACH_COMPLETION_UNVERIFIED")
            if state in ("UI_DONE_UNVERIFIED", "FAILED"):
                if state == "FAILED":
                    self.last_failure_reason = terminal_failure_reason(message)
                    self.last_retryable_connection_prompt = (
                        market_host_retry_prompt(message) and plain(button) == "开始下载"
                    )
                return state
            time.sleep(3)
        # Do not cancel terminal downloads on timeout.
        raise StopRun("DOWNLOAD_TIMEOUT_OUTCOME_UNKNOWN")

    def ensure_connected_process(self) -> None:
        """Stop waiting once the exact vendor process has exited or changed."""

        expected = getattr(self, "connected_start_token", None)
        if expected is not None and process_start_token(getattr(self.app, "process", 0)) != expected:
            raise StopRun("TERMINAL_EXITED_UNVERIFIED")

    def download(self, timeout):
        """Compatibility entry for controlled single-terminal callers."""

        self.begin_download()
        return self.wait_download(timeout)

    def confirm_owned_exit(self, owned_pid: int, owned_token: str) -> bool:
        """Confirm only the vendor's verified, same-process exit dialog."""

        if process_start_token(owned_pid) != owned_token:
            raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
        windows = self.windows()
        matches = [window for window in windows if window.process_id() == owned_pid
                   and window.class_name() == "#32770" and window.is_visible()
                   and window.is_enabled()
                   and window.window_text() in {"退出确认", "通达信金融终端"}]
        if not matches:
            return False
        if len(matches) != 1:
            raise StopRun("OWNED_EXIT_CONFIRMATION_AMBIGUOUS")
        fields = {(item.class_name(), item.control_id()): item
                  for item in matches[0].children()}
        financial = matches[0].window_text() == "通达信金融终端"
        cancel_id = 2 if financial else 1
        prompt_valid = (not financial and fields.get(("Static", 1726)) is not None
                        and fields[("Static", 1726)].window_text() == "您确定退出本系统吗?请您选择:")
        if financial:
            # V7.71 uses the product title and three fixed buttons without
            # the futures terminal's explanatory Static text.
            prompt_valid = all(not item.window_text() for item in matches[0].children()
                               if item.class_name() == "Static")
        if (not prompt_valid or
                any(fields.get(("Button", identifier)) is None or
                    fields[("Button", identifier)].window_text() != label
                    for identifier, label in ((961, "退出"), (962, "重新登录"),
                                              (cancel_id, "取消"))) or
                sum(item.class_name() == "Button" for item in matches[0].children()) != 3):
            raise StopRun("OWNED_EXIT_CONFIRMATION_LAYOUT_CHANGED")
        fields[("Button", 961)].click()
        return True

    def close_owned(self, owned_pid: int, state: dict, slot: str,
                    batches: tuple[Batch, ...], *, owned_token: str | None = None) -> str:
        """Gracefully close only this run's exact process after every UI batch succeeds."""

        if terminal_completion(state, slot, batches) not in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS"}:
            raise StopRun("OWNED_PROCESS_BATCHES_INCOMPLETE")
        if (owned_pid <= 0 or not owned_token or
                process_start_token(owned_pid) != owned_token or self.app is None
                or getattr(self.app, "process", None) != owned_pid
                or _process_image_path(owned_pid).as_posix().casefold() !=
                self.exe.as_posix().casefold()):
            raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
        if self.dialog is not None:
            self.ensure_idle()
        windows = self.windows()
        mains = [window for window in windows
                 if window.process_id() == owned_pid
                 and window.class_name() == "TdxW_MainFrame_Class"
                 and " - [" in window.window_text()]
        if len(mains) != 1:
            raise StopRun("OWNED_MAIN_WINDOW_UNVERIFIED")
        dialog = self.dialog
        if dialog is None and not mains[0].is_enabled():
            if self.confirm_owned_exit(owned_pid, owned_token):
                if not _wait_process_exit(owned_pid, 15):
                    raise StopRun("OWNED_PROCESS_CLOSE_UNVERIFIED")
                return "CLOSED"
            raise StopRun("OWNED_MAIN_WINDOW_NOT_IDLE")
        allowed_handles = {mains[0].handle}
        if dialog is not None and dialog.is_visible():
            if (dialog.process_id() != owned_pid or dialog.class_name() != "#32770"
                    or dialog.window_text() not in {"盘后数据下载", ""}):
                raise StopRun("OWNED_DOWNLOAD_DIALOG_UNVERIFIED")
            allowed_handles.add(dialog.handle)
        # A transient vendor window may still be finishing after the download
        # dialog reports completion. Recheck briefly, but never dismiss an
        # unidentified window or relax the process identity check.
        deadline = time.monotonic() + 5
        while any(window.process_id() == owned_pid and window.handle not in allowed_handles
                  for window in windows):
            if time.monotonic() >= deadline:
                raise StopRun("OWNED_PROCESS_OTHER_WINDOW_OPEN")
            time.sleep(.25)
            if process_start_token(owned_pid) != owned_token:
                raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
            if dialog is not None:
                self.ensure_idle()
            windows = self.windows()
            if not any(window.handle == mains[0].handle and window.process_id() == owned_pid
                       and window.class_name() == "TdxW_MainFrame_Class" for window in windows):
                raise StopRun("OWNED_MAIN_WINDOW_UNVERIFIED")
        if dialog is not None and dialog.is_visible():
            if process_start_token(owned_pid) != owned_token:
                raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
            dialog.close()
            if _wait_process_exit(owned_pid, 1):
                return "CLOSED"
        if self.confirm_owned_exit(owned_pid, owned_token):
            if not _wait_process_exit(owned_pid, 15):
                raise StopRun("OWNED_PROCESS_CLOSE_UNVERIFIED")
            return "CLOSED"
        from pywinauto.timings import TimeoutError as PywinautoTimeoutError

        if process_start_token(owned_pid) != owned_token:
            raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
        try:
            mains[0].close()
        except PywinautoTimeoutError:
            pass  # An exact vendor exit confirmation can keep the main open.
        if _wait_process_exit(owned_pid, 1):
            return "CLOSED"
        if not self.confirm_owned_exit(owned_pid, owned_token):
            raise StopRun("OWNED_EXIT_CONFIRMATION_UNVERIFIED")
        if not _wait_process_exit(owned_pid, 15):
            raise StopRun("OWNED_PROCESS_CLOSE_UNVERIFIED")
        return "CLOSED"


def run_terminal(driver, batches, state, slot, persist, timeout,
                 progress: Callable[[str], None] | None = None,
                 foreground_lock: Path | None = None):
    todo = pending_keys(batches, state, slot)
    if not todo:
        return
    # An interrupted run is never silently retried, even in a later due slot.
    if any(r.get("status") == "RUNNING_UNKNOWN" for r in state.values()):
        raise StopRun("PREVIOUS_RUN_NEEDS_RECONCILIATION")
    groups = ([tuple(batch for batch in todo if batch.key.startswith("cn-")),
               tuple(batch for batch in todo if batch.key.startswith("extended-"))]
              if any(batch.key.startswith(("cn-", "extended-")) for batch in todo)
              else [tuple(todo)])

    def begin_with_explicit_failure_record(group):
        try:
            driver.begin_download()
        except StopRun as error:
            reason = str(error)
            if reason in _TERMINAL_FAILURE_REASONS:
                finished_at = datetime.now(BEIJING).isoformat()
                for batch in group:
                    state[batch.key].update(status="FAILED", finishedAt=finished_at,
                                            failureReason=reason)
                persist()
            raise

    first_group = True
    for group in (selected for selected in groups if selected):
        with _foreground_input_lock(foreground_lock) if foreground_lock is not None else nullcontext():
            if first_group:
                driver.open()
                first_group = False
            if progress is not None:
                progress("开始" + "、".join(batch.tab for batch in group))
            if len(group) == 1:
                evidence_by_key = {group[0].key: driver.prepare(group[0])}
            else:
                evidence_by_key = driver.prepare_many(group)
            reconnect_name = ("reconnect_extended_host" if any(
                batch.key.startswith("extended-") for batch in group) else
                "reconnect_futures_host" if any(batch.key.startswith("future-") for batch in group)
                else None)
            reconnect = getattr(driver, reconnect_name, None) if reconnect_name else None
            if callable(reconnect) and reconnect():
                # The vendor resets tab content while reconnecting. Re-read the
                # user's saved selection and dates before this group's click.
                evidence_by_key = ({group[0].key: driver.prepare(group[0])} if len(group) == 1
                                   else driver.prepare_many(group))
            if set(evidence_by_key) != {batch.key for batch in group}:
                raise StopRun("BATCH_GROUP_EVIDENCE_MISMATCH")
            stable_wait = getattr(driver, "wait_futures_window_stable", None)
            if callable(stable_wait) and any(batch.key.startswith("future-") for batch in group):
                stable_wait(group)
            if "extended-5m" in evidence_by_key:
                daily = evidence_by_key.get("extended-day", state.get("extended-day", {}))
                minute = evidence_by_key["extended-5m"]
                if daily.get("minuteComparableHash", daily.get("selectionHash")) != minute.get("selectionHash"):
                    raise StopRun("EXTENDED_DAY_MINUTE_SELECTION_MISMATCH")
            for batch in group:
                state[batch.key] = {"slot": slot, "status": "RUNNING_UNKNOWN", **evidence_by_key[batch.key]}
            persist()
            phased = callable(getattr(driver, "begin_download", None)) and callable(
                getattr(driver, "wait_download", None))
            if phased:
                driver.connection_retries_remaining = (
                    1 if any(batch.key.startswith(("extended-", "future-")) for batch in group) else 0
                )
                begin_with_explicit_failure_record(group)
        result = (driver.wait_download(timeout * len(group)) if phased
                  else driver.download(timeout * len(group)))
        if (phased and result == "FAILED"
                and any(batch.key.startswith(("extended-", "future-")) for batch in group)
                and getattr(driver, "last_retryable_connection_prompt", False)
                and driver.connection_retries_remaining > 0):
            with _foreground_input_lock(foreground_lock) if foreground_lock is not None else nullcontext():
                driver.connection_retries_remaining = 0
                begin_with_explicit_failure_record(group)
            result = driver.wait_download(timeout * len(group))
        failure_reason = getattr(driver, "last_failure_reason", None) if result == "FAILED" else None
        if failure_reason not in _TERMINAL_FAILURE_REASONS:
            failure_reason = None
        finished_at = datetime.now(BEIJING).isoformat()
        for batch in group:
            state[batch.key]["status"] = result
            state[batch.key]["finishedAt"] = finished_at
            if failure_reason is not None:
                state[batch.key]["failureReason"] = failure_reason
        persist()
        if progress is not None:
            progress("、".join(batch.tab for batch in group) + "："
                     + ("界面下载完成，待文件核验" if result == "UI_DONE_UNVERIFIED" else result))
        if result != "UI_DONE_UNVERIFIED":
            raise StopRun(failure_reason or ("BATCH_GROUP_FAILED" if len(group) > 1 else "BATCH_FAILED"))


def terminal_completion(state: dict, slot: str, batches: tuple[Batch, ...]) -> str:
    if all(state.get(batch.key, {}).get("slot") == slot
           and state[batch.key].get("status") in _COMPLETE_BATCH_STATUSES for batch in batches):
        statuses = {state[batch.key]["status"] for batch in batches}
        return ("UI_DONE_UNVERIFIED" if statuses == {"UI_DONE_UNVERIFIED"} else
                "NO_GUI_RAW_RECENT" if statuses == {"SKIPPED_RECENT_RAW"} else
                "COMPLETE_WITH_SKIPS")
    if any(state.get(batch.key, {}).get("status") == "RUNNING_UNKNOWN" for batch in batches):
        return "PREVIOUS_RUN_NEEDS_RECONCILIATION"
    return "NOT_DOWNLOADED"


def main(argv=None):
    parser = argparse.ArgumentParser(description="通达信已保存品种的盘后下载；入库独立核验")
    parser.add_argument("--execute", action="store_true", help="实际操作终端；默认只显示计划")
    parser.add_argument("--launch-only", action="store_true", help="只启动指定终端，不在本次命令中登录或下载")
    parser.add_argument("--auto-login-saved", action="store_true", help="仅金融终端：点击已保存模拟账户的登录按钮")
    parser.add_argument("--extended-minute-start", type=str, help="仅修改扩展市场5分钟线起始日期，YYYY-MM-DD")
    parser.add_argument("--batch-start", action="append", default=[],
                        help="只修改指定批次起始日期，格式=batch-key:YYYY-MM-DD；可重复")
    parser.add_argument("--terminal", choices=("all", "financial", "futures"), default="all")
    parser.add_argument("--financial-root", type=Path, default=Path(r"C:\tongdaxin"))
    parser.add_argument("--futures-root", type=Path, default=Path(r"C:\new_tdxqh"))
    parser.add_argument("--state-dir", type=Path, default=Path("data_control/state/tdx_download_ui"))
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--owned-pid", type=int,
                        help="仅自动下载：本轮启动且需在完成后核验退出的终端PID")
    parser.add_argument("--owned-token", type=str,
                        help="仅自动下载：该终端进程的Windows创建时间身份")
    parser.add_argument("--skip-batch", action="append", default=[],
                        help="仅自动任务：已核验无新行情的批次，格式=batch-key:reason")
    args = parser.parse_args(argv)
    if not 60 <= args.timeout <= 14400:
        parser.error("timeout须介于60和14400秒")
    if args.execute and args.launch_only:
        parser.error("--execute与--launch-only不能同时使用")
    if args.launch_only and args.terminal == "all":
        parser.error("--launch-only必须指定一个终端")
    if args.auto_login_saved and (args.terminal != "financial" or not args.execute):
        parser.error("--auto-login-saved仅适用于金融终端下载")
    if ((args.owned_pid is None) != (args.owned_token is None) or
            args.owned_pid is not None and
            (not args.execute or args.terminal == "all" or args.owned_pid <= 0 or
             not re.fullmatch(r"windows-filetime:\d+", args.owned_token))):
        parser.error("--owned-pid和--owned-token须同时用于单终端自动下载")
    minute_start: date | None = None
    if args.extended_minute_start:
        if args.terminal != "financial" or not args.execute:
            parser.error("--extended-minute-start仅适用于金融终端下载")
        try:
            minute_start = datetime.strptime(args.extended_minute_start, "%Y-%m-%d").date()
        except ValueError:
            parser.error("--extended-minute-start必须为YYYY-MM-DD")
        if minute_start > datetime.now(BEIJING).date():
            parser.error("--extended-minute-start不能晚于今天")
    selected = tuple(TERMINALS) if args.terminal == "all" else (args.terminal,)
    allowed_batch_keys = {batch.key for terminal in selected for batch in TERMINALS[terminal][1]}
    skipped_batches: dict[str, str] = {}
    for value in args.skip_batch:
        key, separator, reason = value.partition(":")
        if (not args.execute or args.terminal != "financial" or not separator
                or key not in {"cn-day", "cn-5m"} or key not in allowed_batch_keys
                or key in skipped_batches or reason != "RAW_MAJORITY_RECENT_SILVER_UNVERIFIED"):
            parser.error("--skip-batch须为当前单终端的唯一批次与已核验原因")
        skipped_batches[key] = reason
    batch_starts: dict[str, date] = {}
    for item in args.batch_start:
        key, separator, raw_day = item.partition(":")
        if (not args.execute or not separator or key not in allowed_batch_keys
                or key in batch_starts or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_day)):
            parser.error("--batch-start须为当前终端的唯一批次及YYYY-MM-DD日期")
        try:
            chosen_day = date.fromisoformat(raw_day)
        except ValueError:
            parser.error("--batch-start包含无效日期")
        if chosen_day > datetime.now(BEIJING).date():
            parser.error("--batch-start不能晚于今天")
        batch_starts[key] = chosen_day
    if minute_start is not None:
        if "extended-5m" in batch_starts:
            parser.error("--extended-minute-start与同批次--batch-start不能同时使用")
        batch_starts["extended-5m"] = minute_start
    roots = {"financial": args.financial_root, "futures": args.futures_root}
    report = {"slot": latest_slot(datetime.now(BEIJING)), "status": "PLAN_ONLY",
              "batches": [batch.key for terminal in selected for batch in TERMINALS[terminal][1]],
              "import": "NOT_RUN", "completeThrough": None}
    if args.launch_only:
        terminal = selected[0]
        try:
            report["status"] = DialogDriver(roots[terminal] / "tdxw.exe").start()
        except StopRun as error:
            report["status"] = str(error)
        except Exception as error:
            report["status"] = f"ADAPTER_ERROR_{type(error).__name__}"
        report["terminal"] = terminal
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] in {"LAUNCHED", "ALREADY_RUNNING"} else 2
    if not args.execute:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    args.state_dir = args.state_dir.resolve()
    if skipped_batches:
        from .tdx_gui_auto_refresh import financial_cn_coverage

        reason = financial_cn_coverage(args.state_dir.parent.parent,
                                       datetime.fromisoformat(report["slot"]).date())
        if reason is None or any(value != reason for value in skipped_batches.values()):
            report["status"] = "SKIP_EVIDENCE_STALE"
            print(json.dumps(report, ensure_ascii=False))
            return 2
    try:
        with ExitStack() as locks:
            for terminal in selected:
                locks.enter_context(process_lock(args.state_dir / f"{terminal}.run.lock"))
            path = args.state_dir / "checkpoints.json"
            state = _read_checkpoint_state(path)
            outcomes = {
                terminal: terminal_completion(state.get(terminal, {}), report["slot"], TERMINALS[terminal][1])
                for terminal in TERMINALS
            }
            for terminal in selected:
                batches = TERMINALS[terminal][1]
                try:
                    terminal_state = state.setdefault(terminal, {})
                    if any(item.get("status") == "RUNNING_UNKNOWN"
                           for item in terminal_state.values() if isinstance(item, dict)):
                        raise StopRun("PREVIOUS_RUN_NEEDS_RECONCILIATION")
                    for batch in batches:
                        reason = skipped_batches.get(batch.key)
                        if reason is not None and (terminal_state.get(batch.key, {}).get("slot") != report["slot"]
                                                   or terminal_state[batch.key].get("status") != "UI_DONE_UNVERIFIED"):
                            terminal_state[batch.key] = {"slot": report["slot"], "status": "SKIPPED_RECENT_RAW",
                                                        "reason": reason, "checkedAt": datetime.now(BEIJING).isoformat()}
                    if skipped_batches:
                        _save_terminal_checkpoint(path, terminal, terminal_state)
                    if pending_keys(batches, state.get(terminal, {}), report["slot"]):
                        driver = DialogDriver(roots[terminal] / "tdxw.exe")
                        driver.foreground_lock_path = args.state_dir / "foreground.run.lock"
                        if terminal == "financial":
                            driver.extended_minute_start = minute_start
                        driver.batch_start_dates = batch_starts
                        if args.auto_login_saved:
                            driver.connect(auto_login_saved=True)
                        else:
                            driver.connect()
                        run_terminal(driver, batches, terminal_state, report["slot"],
                                     lambda label=terminal: _save_terminal_checkpoint(
                                         path, label, state[label]), args.timeout,
                                     progress=lambda message, label=TERMINALS[terminal][2]:
                                     print(f"[通达信] {label}：{message}", flush=True),
                                     foreground_lock=args.state_dir / "foreground.run.lock")
                        if args.owned_pid is not None:
                            from .tdx_process_ownership import load_auto_owned

                            identity = load_auto_owned(args.state_dir.parent.parent,
                                                       terminal, driver.exe)
                            if identity != (args.owned_pid, args.owned_token):
                                raise StopRun("OWNED_PROCESS_RECORD_MISMATCH")
                            report["ownedProcessClose"] = driver.close_owned(
                                args.owned_pid, state[terminal], report["slot"], batches,
                                owned_token=args.owned_token)
                    elif args.owned_pid is not None:
                        # A previous attempt may have completed all batches but
                        # failed to close the owned process. Reattach only to
                        # the same recorded process and never start or download.
                        from .tdx_process_ownership import load_auto_owned

                        executable = (roots[terminal] / "tdxw.exe").resolve()
                        identity = load_auto_owned(args.state_dir.parent.parent, terminal, executable)
                        if identity != (args.owned_pid, args.owned_token):
                            raise StopRun("OWNED_PROCESS_RECORD_MISMATCH")
                        driver = DialogDriver(executable)
                        driver.connect(auto_login_saved=False)
                        if getattr(driver.app, "process", None) != args.owned_pid:
                            raise StopRun("OWNED_PROCESS_IDENTITY_CHANGED")
                        driver.dialog = driver.find_dialog()
                        report["ownedProcessClose"] = driver.close_owned(
                            args.owned_pid, terminal_state, report["slot"], batches,
                            owned_token=args.owned_token)
                    outcomes[terminal] = terminal_completion(terminal_state, report["slot"], batches)
                except StopRun as error:
                    outcomes[terminal] = str(error)
                except Exception as error:
                    outcomes[terminal] = f"ADAPTER_ERROR_{type(error).__name__}"
            report.update(status="UI_DONE_UNVERIFIED" if all(outcomes[terminal] == "UI_DONE_UNVERIFIED"
                                                                 for terminal in selected)
                          else "COMPLETE_WITH_SKIPS" if all(outcomes[terminal] in {
                              "UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}
                              for terminal in selected)
                          else "ATTENTION_REQUIRED", terminals=outcomes)
            _save_latest_terminal_result(args.state_dir, report, selected, outcomes)
    except (OSError, ValueError):
        report["status"] = "LOCK_OR_STATE_ERROR"
    if __import__("sys").stdout is not None:
        print(json.dumps(report, ensure_ascii=False))
    return 0 if all(report.get("terminals", {}).get(terminal) in {
        "UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}
                    for terminal in selected) else 2


if __name__ == "__main__":
    raise SystemExit(main())
