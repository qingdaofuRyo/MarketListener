"""Standalone Windows download-dialog driver. Never automates authentication.

This adapter requires desktop acceptance before unattended use. It deliberately
fails on unknown controls instead of sending guessed coordinates or keystrokes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Callable

from .tdx_afterhours import BEIJING, latest_slot, process_lock, save_report


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
PERIOD_LABELS = {"日线和实时行情数据", "日线数据", "1分钟线数据", "5分钟线数据"}


class StopRun(RuntimeError):
    """Only allowlisted reason codes may be persisted (no vendor/account text)."""


def plain(value: str) -> str:
    return re.sub(r"[&*\s]", "", value)


def completion_state(message: str, button: str, observed_busy: bool) -> str:
    if any(word in message for word in ("失败", "错误", "中断", "取消", "超时")):
        return "FAILED"
    if "取消下载" in button or "正在下载" in message:
        return "BUSY"
    if observed_busy and "开始下载" in button and re.search(r"下载(?:全部)?(?:完成|完毕)|数据接收完毕", message):
        return "UI_DONE_UNVERIFIED"
    return "UNKNOWN"


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
            or records[b.key].get("status") != "UI_DONE_UNVERIFIED"]


class DialogDriver:
    def __init__(self, exe: Path):
        # Imported only in the Windows worker, not pure-logic tests.
        from pywinauto import Application, Desktop
        self.Application, self.Desktop = Application, Desktop
        self.exe = exe.resolve()
        self.app = None
        self.dialog = None

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

    def connect(self):
        self.unlocked()
        if not self.exe.is_file():
            raise StopRun("EXECUTABLE_MISSING")
        from pywinauto.application import ProcessNotFoundError
        try:
            self.app = self.Application(backend="win32").connect(path=str(self.exe), timeout=5)
        except ProcessNotFoundError:
            raise StopRun("CLIENT_NOT_RUNNING") from None
        # Starting the terminal is an explicit launcher action.  Connecting
        # here must never create a new process or automate authentication.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.unlocked()
            windows = self.windows()
            if any("登录" in window.window_text() for window in windows):
                raise StopRun("WAITING_FOR_LOGIN")
            if any(" - [" in window.window_text() for window in windows):
                self.guard()
                return
            time.sleep(1)
        raise StopRun("WAITING_FOR_LOGIN_OR_MAIN_WINDOW")

    def start(self) -> str:
        """Only start the selected terminal; login remains a user action."""

        self.unlocked()
        if not self.exe.is_file():
            raise StopRun("EXECUTABLE_MISSING")
        from pywinauto.application import ProcessNotFoundError

        try:
            self.Application(backend="win32").connect(path=str(self.exe), timeout=5)
        except ProcessNotFoundError:
            subprocess.Popen([str(self.exe)], cwd=self.exe.parent)
            return "LAUNCHED"
        return "ALREADY_RUNNING"

    def windows(self):
        return self.app.windows(visible_only=True)

    def guard(self):
        self.unlocked()
        # Only inspect window titles; do not read account/password edit controls.
        windows = self.windows()
        if not any(" - [" in w.window_text() for w in windows):
            raise StopRun("WAITING_FOR_LOGIN_OR_MAIN_WINDOW")
        for w in windows:
            if "登录" in w.window_text():
                raise StopRun("WAITING_FOR_LOGIN")

    def find_dialog(self):
        matches = [w for w in self.windows() if w.window_text() == "盘后数据下载"]
        if len(matches) > 1:
            raise StopRun("AMBIGUOUS_DIALOG")
        return matches[0] if matches else None

    def open(self):
        self.guard()
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
        try:
            main.menu_select(f"{menu}->盘后数据下载")
        except Exception:
            # Custom menus cannot be coordinate-guessed.  Let the user open the
            # exact dialog while we wait, then continue the guarded batches.
            print("[通达信] 无法自动打开盘后数据下载菜单；请在终端手动打开该窗口，等待120秒。", flush=True)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                self.dialog = self.find_dialog()
                if self.dialog:
                    self.ensure_idle()
                    return
                time.sleep(1)
            raise StopRun("OPEN_DOWNLOAD_DIALOG_MANUALLY") from None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.dialog = self.find_dialog()
            if self.dialog:
                self.ensure_idle()
                return
            time.sleep(1)
        raise StopRun("DOWNLOAD_DIALOG_NOT_FOUND")

    def control(self, control_id: int):
        return self.app.window(handle=self.dialog.handle).child_window(control_id=control_id).wrapper_object()

    def status(self):
        self.guard()
        return self.control(1224).window_text(), self.control(1).window_text()

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
        time.sleep(0.4)
        if not self.dialog.is_enabled():
            raise StopRun("WAITING_FOR_MARKET_CONNECTION")

    def checkbox(self, label, desired):
        matches = [c for c in self.dialog.children() if plain(c.window_text()) == plain(label)]
        if len(matches) != 1 or not hasattr(matches[0], "get_check_state"):
            raise StopRun("CHECKBOX_NOT_FOUND")
        control = matches[0]
        if not control.is_enabled():
            raise StopRun("CHECKBOX_DISABLED")
        if control.get_check_state() != int(desired):
            self.guard()
            control.check_by_click() if desired else control.uncheck_by_click()
        if control.get_check_state() != int(desired):
            raise StopRun("CHECKBOX_STATE_MISMATCH")

    def prepare(self, batch):
        self.ensure_idle()
        # Clear other periodic selections before enabling precisely one batch.
        names = [plain(n) for n in self.tab_names()]
        if len(names) != 5 or not all("线" in n or "分时图" in n for n in names):
            raise StopRun("UNSUPPORTED_TAB_LAYOUT")
        for name in names:
            self.select_tab(name)
            for control in self.dialog.children():
                label = plain(control.window_text())
                if label in PERIOD_LABELS and control.is_enabled():
                    self.checkbox(label, False)
            # Don't accidentally download a preselected intraday diagram task.
            if "分时图" in name:
                for c in self.dialog.children():
                    if c.friendly_class_name() == "CheckBox" and c.get_check_state():
                        raise StopRun("UNSUPPORTED_SELECTED_INTRADAY_TASK")
        self.select_tab(batch.tab)
        self.checkbox(batch.checkbox, True)
        if batch.all_checkbox:
            self.checkbox(batch.all_checkbox, True)
        if batch.saved_selection:
            all_boxes = [c for c in self.dialog.children()
                         if plain(c.window_text()).startswith("下载所有扩展市场")]
            if len(all_boxes) != 1:
                raise StopRun("EXTENDED_SCOPE_CONTROL_MISSING")
            self.checkbox(all_boxes[0].window_text(), False)
            codes = [self.control(1732).get_item(i, 0).text()
                     for i in range(self.control(1732).item_count())]
            digest = selection_digest(codes)
        else:
            digest = None
        selected = [plain(n) for n in self.tab_names() if "*" in n]
        if selected != [batch.tab]:
            raise StopRun("UNEXPECTED_SELECTED_BATCHES")
        return {"selectionHash": digest, "datePolicy": "CLIENT_SAVED_PROGRESS"}

    def download(self, timeout):
        self.ensure_idle()
        self.guard()
        self.control(1).click()
        observed_busy = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message, button = self.status()
            state = completion_state(message, button, observed_busy)
            observed_busy |= state == "BUSY"
            if state in ("UI_DONE_UNVERIFIED", "FAILED"):
                return state
            time.sleep(3)
        # Do not cancel terminal downloads on timeout.
        raise StopRun("DOWNLOAD_TIMEOUT_OUTCOME_UNKNOWN")


def run_terminal(driver, batches, state, slot, persist, timeout, progress: Callable[[str], None] | None = None):
    todo = pending_keys(batches, state, slot)
    if not todo:
        return
    # An interrupted run is never silently retried, even in a later due slot.
    if any(r.get("status") == "RUNNING_UNKNOWN" for r in state.values()):
        raise StopRun("PREVIOUS_RUN_NEEDS_RECONCILIATION")
    driver.open()
    for batch in todo:
        if progress is not None:
            progress(f"开始 {batch.tab}：{batch.checkbox}")
        evidence = driver.prepare(batch)
        if batch.key == "extended-5m":
            daily = state.get("extended-day", {})
            if daily.get("selectionHash") != evidence.get("selectionHash"):
                raise StopRun("EXTENDED_DAY_MINUTE_SELECTION_MISMATCH")
        state[batch.key] = {"slot": slot, "status": "RUNNING_UNKNOWN", **evidence}
        persist()
        result = driver.download(timeout)
        state[batch.key]["status"] = result
        state[batch.key]["finishedAt"] = datetime.now(BEIJING).isoformat()
        persist()
        if progress is not None:
            progress(f"{batch.tab}：{'界面下载完成，待文件核验' if result == 'UI_DONE_UNVERIFIED' else result}")
        if result != "UI_DONE_UNVERIFIED":
            raise StopRun("BATCH_FAILED")


def terminal_completion(state: dict, slot: str, batches: tuple[Batch, ...]) -> str:
    if all(state.get(batch.key, {}).get("slot") == slot
           and state[batch.key].get("status") == "UI_DONE_UNVERIFIED" for batch in batches):
        return "UI_DONE_UNVERIFIED"
    if any(state.get(batch.key, {}).get("status") == "RUNNING_UNKNOWN" for batch in batches):
        return "PREVIOUS_RUN_NEEDS_RECONCILIATION"
    return "NOT_DOWNLOADED"


def main(argv=None):
    parser = argparse.ArgumentParser(description="通达信已保存品种的六批盘后下载（不自动登录/入库）")
    parser.add_argument("--execute", action="store_true", help="实际操作终端；默认只显示计划")
    parser.add_argument("--launch-only", action="store_true", help="只启动指定终端，登录和下载仍由用户后续触发")
    parser.add_argument("--terminal", choices=("all", "financial", "futures"), default="all")
    parser.add_argument("--financial-root", type=Path, default=Path(r"C:\tongdaxin"))
    parser.add_argument("--futures-root", type=Path, default=Path(r"C:\new_tdxqh"))
    parser.add_argument("--state-dir", type=Path, default=Path("data_control/state/tdx_download_ui"))
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    if not 60 <= args.timeout <= 14400:
        parser.error("timeout须介于60和14400秒")
    if args.execute and args.launch_only:
        parser.error("--execute与--launch-only不能同时使用")
    if args.launch_only and args.terminal == "all":
        parser.error("--launch-only必须指定一个终端")
    selected = tuple(TERMINALS) if args.terminal == "all" else (args.terminal,)
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
    try:
        with process_lock(args.state_dir / "run.lock"):
            path = args.state_dir / "checkpoints.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            outcomes = {
                terminal: terminal_completion(state.get(terminal, {}), report["slot"], TERMINALS[terminal][1])
                for terminal in TERMINALS
            }
            for terminal in selected:
                batches = TERMINALS[terminal][1]
                try:
                    if pending_keys(batches, state.get(terminal, {}), report["slot"]):
                        driver = DialogDriver(roots[terminal] / "tdxw.exe")
                        driver.connect()
                        run_terminal(driver, batches, state.setdefault(terminal, {}), report["slot"],
                                     lambda: save_report(path, state), args.timeout,
                                     progress=lambda message, label=TERMINALS[terminal][2]:
                                     print(f"[通达信] {label}：{message}", flush=True))
                    outcomes[terminal] = "UI_DONE_UNVERIFIED"
                except StopRun as error:
                    outcomes[terminal] = str(error)
                except Exception as error:
                    outcomes[terminal] = f"ADAPTER_ERROR_{type(error).__name__}"
            report.update(status="UI_DONE_UNVERIFIED" if all(v == "UI_DONE_UNVERIFIED" for v in outcomes.values())
                          else "ATTENTION_REQUIRED", terminals=outcomes)
            save_report(args.state_dir / "latest.json", report)
    except (OSError, ValueError):
        report["status"] = "LOCK_OR_STATE_ERROR"
    if __import__("sys").stdout is not None:
        print(json.dumps(report, ensure_ascii=False))
    return 0 if all(report.get("terminals", {}).get(terminal) == "UI_DONE_UNVERIFIED"
                    for terminal in selected) else 2


if __name__ == "__main__":
    raise SystemExit(main())
