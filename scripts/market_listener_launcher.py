"""Small Windows GUI that launches the repository's local web service."""

from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser

from PIL import Image, ImageTk
import pystray

from market_listener_icon import render_icon
from market_monitor.profile_gate import profile_write_gate


APP_TITLE = "MarketListener 终端启动器"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
READY_TIMEOUT_SECONDS = 90
HEALTH_PATH = "/api/ready"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "MarketListenerLauncher"
SETTINGS_SCHEMA = "market-listener/launcher-settings/v2"
LEGACY_SETTINGS_SCHEMA = "market-listener/launcher-settings/v1"
MAX_LOG_CHUNK_BYTES = 65_536
MAX_LOG_LINES = 800
TERMINAL_NAMES = {"financial": "通达信金融终端", "futures": "通达信期货通"}
TERMINAL_STATES = {
    "LAUNCHED": "终端已启动，请在软件内完成登录",
    "ALREADY_RUNNING": "终端已在运行，请确认已经登录",
    "UI_DONE_UNVERIFIED": "下载界面已显示完成，正在等待文件覆盖核验",
    "CLIENT_NOT_RUNNING": "终端未运行；请先点击“启动终端”",
    "WAITING_FOR_LOGIN": "终端尚未登录；请先完成登录",
    "WAITING_FOR_LOGIN_OR_MAIN_WINDOW": "尚未出现已登录主窗口；请确认登录完成后重试",
    "OPEN_DOWNLOAD_DIALOG_MANUALLY": "无法自动打开盘后下载菜单；请在终端内打开“盘后数据下载”后重试",
    "PREVIOUS_RUN_NEEDS_RECONCILIATION": "上次下载结果不确定，需先核对终端与本地文件",
    "DOWNLOAD_TIMEOUT_OUTCOME_UNKNOWN": "等待下载超时，但未取消终端任务；请先核对实际结果",
    "EXISTING_DOWNLOAD_BUSY": "终端已有下载任务在运行；未接管或取消该任务",
    "BATCH_FAILED": "本批下载未确认完成，已停止后续批次；请查看完整日志",
    "DOWNLOAD_DIALOG_NOT_FOUND": "未找到盘后数据下载窗口，请确认终端界面",
    "DOWNLOAD_DIALOG_NOT_IDLE": "盘后数据下载窗口未处于可开始状态",
    "WAITING_FOR_MARKET_CONNECTION": "终端行情连接尚未就绪，请稍后重试",
    "AMBIGUOUS_DIALOG": "发现多个盘后下载窗口，请只保留一个",
    "AMBIGUOUS_MAIN_WINDOW": "发现多个终端主窗口，无法安全选择",
    "UNEXPECTED_MODAL": "终端有未处理弹窗，请先在软件内处理",
    "TAB_NOT_FOUND": "未找到预期的日线或5分钟线标签，请检查终端版本",
    "UNSUPPORTED_TAB_LAYOUT": "下载窗口标签布局与已核验版本不同，未继续操作",
    "CHECKBOX_NOT_FOUND": "未找到预期下载选项，未继续操作",
    "CHECKBOX_DISABLED": "预期下载选项不可选，请检查终端状态",
    "CHECKBOX_STATE_MISMATCH": "下载选项未能切换到预期状态，未继续操作",
    "EXTENDED_SCOPE_CONTROL_MISSING": "金融终端扩展市场已保存品种列表不可读",
    "EXTENDED_DAY_MINUTE_SELECTION_MISMATCH": "扩展市场日线与5分钟线品种范围不一致，未继续操作",
    "RETIRED_00Y_SELECTION": "已保存范围含停用的00Y序列，请先调整",
    "EMPTY_OR_UNREADABLE_SELECTION": "已保存品种范围为空或不可读，未继续操作",
    "UNEXPECTED_SELECTED_BATCHES": "选中批次与目标不一致，未继续操作",
    "UNSUPPORTED_SELECTED_INTRADAY_TASK": "检测到不支持的盘中任务选择，未继续操作",
    "EXECUTABLE_MISSING": "未找到终端程序文件",
    "DESKTOP_LOCKED": "桌面已锁定，无法操作终端",
    "LOCK_OR_STATE_ERROR": "下载任务已被占用，或状态文件不可读",
}
OPERATION_NAMES = {
    "MARKET_UPDATE": "更新行情",
    "FUTURES_OFFICIAL_DAILY_UPDATE": "更新交易所期货日行情",
    "TDX_COVERAGE_CHECK": "检查通达信本地文件",
    "TDX_AFTERHOURS_DOWNLOAD": "下载通达信盘后数据",
    "TDX_FINANCIAL_LAUNCH": "启动通达信金融终端",
    "TDX_FINANCIAL_AUTO_DOWNLOAD": "自动下载金融终端盘后数据",
    "TDX_FUTURES_LAUNCH": "启动通达信期货通",
    "TDX_FUTURES_AUTO_DOWNLOAD": "自动下载期货通盘后数据",
    "TDX_LOCAL_IMPORT": "导入通达信本地数据",
    "FUTURES_HEAT_UPDATE": "更新期货热度",
    "FUTURES_STRUCTURE_UPDATE": "更新期货结构",
    "EQUITY_DASHBOARD_UPDATE": "更新数据页市场指标",
    "DISCLOSURES_UPDATE": "检索巨潮公告关键词",
}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ACCESS_LINE = re.compile(r'^INFO:\s+\S+ - "(?:GET|HEAD|OPTIONS) [^\"]+ HTTP/[^\"]+" (\d{3})')
PROGRESS_LINE = re.compile(r"^\s*\d{1,3}%\|.*\|\s*\d+/\d+")


@dataclass(frozen=True)
class LauncherSettings:
    silent_start: bool = False
    close_to_tray: bool = False


def load_settings(path: Path) -> LauncherSettings:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LauncherSettings()
    if not isinstance(payload, dict) or payload.get("schema") not in {SETTINGS_SCHEMA, LEGACY_SETTINGS_SCHEMA}:
        return LauncherSettings()
    return LauncherSettings(
        silent_start=payload.get("silentStart") is True,
        close_to_tray=(
            payload.get("closeToTray") is True
            if payload.get("schema") == SETTINGS_SCHEMA
            else payload.get("minimizeOnClose") is True
        ),
    )


def save_settings(path: Path, settings: LauncherSettings) -> None:
    with profile_write_gate(path.parent.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({
                "schema": SETTINGS_SCHEMA,
                "silentStart": settings.silent_start,
                "closeToTray": settings.close_to_tray,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def windows_startup_command(executable: Path) -> str:
    return f'"{Path(executable).resolve()}" --startup'


def _windows_registry(registry=None):
    if registry is not None:
        return registry
    import winreg

    return winreg


def startup_is_enabled(executable: Path, *, registry=None) -> bool:
    if os.name != "nt" and registry is None:
        return False
    api = _windows_registry(registry)
    try:
        with api.OpenKey(api.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, api.KEY_READ) as key:
            value, _kind = api.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return False
    return str(value).strip() == windows_startup_command(executable)


def set_startup_enabled(executable: Path, enabled: bool, *, registry=None) -> None:
    if os.name != "nt" and registry is None:
        raise OSError("开机启动仅支持 Windows")
    api = _windows_registry(registry)
    if enabled:
        with api.CreateKeyEx(api.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, api.KEY_SET_VALUE) as key:
            api.SetValueEx(key, RUN_VALUE_NAME, 0, api.REG_SZ, windows_startup_command(executable))
    else:
        try:
            with api.OpenKey(api.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, api.KEY_SET_VALUE) as key:
                api.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            pass


def _ancestors(path: Path):
    current = path.resolve()
    yield current
    yield from current.parents


def find_project_root(
    *,
    frozen: bool | None = None,
    executable: Path | None = None,
    script_file: Path | None = None,
) -> Path:
    """Find the repository containing this launcher and its local venv."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    start = (
        Path(executable or sys.executable).resolve().parent
        if is_frozen
        else Path(script_file or __file__).resolve().parent
    )
    for candidate in _ancestors(start):
        marker = candidate / "Plan_R4.md"
        python = candidate / "desktop" / ".venv" / "Scripts" / "python.exe"
        if marker.is_file() and python.is_file():
            return candidate
    raise FileNotFoundError(
        "未找到 MarketListener 项目目录或 desktop\\.venv\\Scripts\\python.exe。"
        "请将启动器放在项目 artifacts\\launcher 目录中运行。"
    )


def build_backend_command(
    project_root: Path,
    *,
    data_root: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> list[str]:
    """Build the hidden backend command without invoking a shell."""

    root = Path(project_root).resolve()
    python = root / "desktop" / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise FileNotFoundError(f"找不到项目虚拟环境：{python}")
    if not 1 <= port <= 65535:
        raise ValueError("服务端口必须在1至65535之间")
    resolved_data_root = Path(data_root or root / "data_control")
    if not resolved_data_root.is_absolute():
        resolved_data_root = root / resolved_data_root
    return [
        str(python), "-m", "market_monitor", "serve",
        "--data-root", str(resolved_data_root.resolve()),
        "--host", host,
        "--port", str(port),
    ]


def _service_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (HTTPError, OSError, TimeoutError, URLError):
        return False


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def read_log_chunk(path: Path, offset: int, *, max_bytes: int = MAX_LOG_CHUNK_BYTES) -> tuple[bytes, int, bool]:
    """Read appended log bytes and detect truncation without loading the whole file."""

    try:
        size = path.stat().st_size
    except OSError:
        return b"", offset, False
    truncated = size < offset
    start = 0 if truncated else offset
    if start == 0 and size > max_bytes:
        start = size - max_bytes
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(max_bytes)
            return raw, stream.tell(), truncated
    except OSError:
        return b"", offset, False


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def build_tdx_command(project_root: Path, terminal: str, *, download: bool) -> list[str]:
    if terminal not in TERMINAL_NAMES:
        raise ValueError(f"未知通达信终端：{terminal}")
    root = Path(project_root).resolve()
    python = root / "desktop" / ".venv" / "Scripts" / "python.exe"
    starts = None
    if download:
        from market_monitor.tdx_gui_auto_refresh import batch_start_dates

        starts = batch_start_dates(root / "data_control", terminal, datetime.now(timezone.utc))
    if download and terminal == "financial":
        from market_monitor.tdx_gui_auto_refresh import _is_x86_python

        python = root / "data_control" / "tools" / "python311-x86" / "python.exe"
        if not _is_x86_python(python):
            raise FileNotFoundError(f"找不到金融终端的32位Python运行时：{python}")
    if not python.is_file():
        raise FileNotFoundError(f"找不到项目虚拟环境：{python}")
    command = [str(python), "-m", "market_monitor.tdx_download_ui"]
    command.extend(["--execute" if download else "--launch-only", "--terminal", terminal])
    if download:
        from market_monitor.tdx_download_ui import FINANCIAL, FUTURES

        if terminal == "financial":
            command.append("--auto-login-saved")
        command.extend(["--state-dir", str(root / "data_control" / "state" / "tdx_download_ui")])
        for batch in FINANCIAL if terminal == "financial" else FUTURES:
            command.extend(["--batch-start", f"{batch.key}:{starts[batch.key].isoformat()}"])
    return command


def _format_report(payload: dict[str, object]) -> tuple[str, str] | None:
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    terminal = payload.get("terminal")
    if isinstance(terminal, str) and terminal in TERMINAL_NAMES:
        name = TERMINAL_NAMES[terminal]
        level = "success" if status in {"LAUNCHED", "ALREADY_RUNNING"} else "warning"
        return level, f"{name}：{TERMINAL_STATES.get(status, status)}"
    outcomes = payload.get("terminals")
    if isinstance(outcomes, dict):
        parts = [
            f"{TERMINAL_NAMES.get(key, key)}：{TERMINAL_STATES.get(str(value), str(value))}"
            for key, value in outcomes.items()
        ]
        level = "success" if status == "UI_DONE_UNVERIFIED" else "warning"
        return level, "；".join(parts)
    return "info", f"任务状态：{status}"


def format_backend_line(line: str) -> tuple[str, str] | None:
    """Summarize noisy service output without altering the raw log file."""

    value = ANSI_ESCAPE.sub("", line).strip()
    if not value or PROGRESS_LINE.match(value):
        return None
    if value.startswith("{") and value.endswith("}"):
        try:
            payload = json.loads(value)
        except ValueError:
            pass
        else:
            if isinstance(payload, dict):
                formatted = _format_report(payload)
                if formatted is not None:
                    return formatted
    access = ACCESS_LINE.match(value)
    if access is not None:
        code = int(access.group(1))
        return ("warning", value) if code >= 400 else None
    if value.startswith("[通达信]"):
        return "task", value.removeprefix("[通达信]").strip()
    if value.startswith("ERROR:") or "Traceback (most recent call last)" in value:
        return "error", value.removeprefix("ERROR:").strip()
    if value.startswith("WARNING:") or "UserWarning:" in value:
        return "warning", value.removeprefix("WARNING:").strip()
    if re.match(r"^[\w.]+(?:Error|Exception):", value):
        return "error", value
    if value.startswith(("File \"", "warnings.warn(", "^")):
        return None
    if value.startswith("INFO:"):
        message = value.removeprefix("INFO:").strip()
        if message.startswith("Uvicorn running on"):
            return "success", "本地网页服务已监听 127.0.0.1:8765"
        if message in {"Application startup complete.", "Shutting down", "Application shutdown complete."}:
            return "service", message
        return None
    if value.startswith("\"") or value in {"{", "}", "[", "]", "},", "],"}:
        return None
    if len(value) > 280:
        value = value[:277] + "…"
    return "task", value


class LauncherLogFormatter:
    def __init__(self) -> None:
        self.pending = ""
        self.json_lines: list[str] = []
        self.json_depth = 0

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        source = self.pending + chunk.replace("\r", "\n")
        lines = source.split("\n")
        self.pending = lines.pop()
        events: list[tuple[str, str]] = []
        for line in lines:
            stripped = line.strip()
            if self.json_lines or stripped == "{":
                self.json_lines.append(line)
                self.json_depth += line.count("{") - line.count("}")
                if self.json_depth <= 0:
                    compact = "".join(part.strip() for part in self.json_lines)
                    self.json_lines.clear()
                    self.json_depth = 0
                    formatted = format_backend_line(compact)
                    if formatted is not None:
                        events.append(formatted)
                elif len(self.json_lines) > 100:
                    self.json_lines.clear()
                    self.json_depth = 0
                continue
            formatted = format_backend_line(line)
            if formatted is not None:
                events.append(formatted)
        return events


def _service_environment(project_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(project_root / "desktop" / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + existing_pythonpath if existing_pythonpath else source_root
    )
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _hidden_subprocess_options() -> dict[str, object]:
    options: dict[str, object] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup_info
    return options


def create_tray_image() -> Image.Image:
    return render_icon(64)


def _set_windows_app_id() -> None:
    """Give the frozen launcher its own taskbar identity on Windows."""

    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MarketListener.Launcher")
    except (AttributeError, OSError):
        pass


class LauncherWindow:
    def __init__(
        self,
        root: tk.Tk,
        project_root: Path,
        *,
        started_by_windows: bool = False,
        settings: LauncherSettings | None = None,
    ) -> None:
        self.root = root
        self.project_root = project_root.resolve()
        self.url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/"
        self.health_url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}{HEALTH_PATH}"
        self.log_path = self.project_root / "data_control" / "logs" / "launcher-backend.log"
        self.tdx_log_path = self.project_root / "data_control" / "logs" / "launcher-tdx.log"
        self.settings_path = self.project_root / "data_control" / "state" / "launcher_settings.json"
        self.settings = settings if settings is not None else load_settings(self.settings_path)
        self.silent_for_this_run = self.settings.silent_start
        self.startup_supported = os.name == "nt" and bool(getattr(sys, "frozen", False))
        try:
            startup_enabled = startup_is_enabled(Path(sys.executable)) if self.startup_supported else False
            startup_error = None
        except OSError as error:
            startup_enabled = False
            startup_error = str(error)
        self.process: subprocess.Popen[bytes] | None = None
        self.owns_process = False
        self.busy = False
        self.ready = False
        self.closing = False
        self.log_offset = _log_size(self.log_path)
        self.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.log_formatter = LauncherLogFormatter()
        self.tdx_log_offset = _log_size(self.tdx_log_path)
        self.tdx_log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.tdx_log_formatter = LauncherLogFormatter()
        self.launch_started_at = datetime.now(timezone.utc)
        self.operation_seen: dict[str, str] = {}
        self.operation_poll_busy = False
        self.import_operation_id: str | None = None
        self.coverage_check_queued = False
        self.tdx_busy_terminals: set[str] = set()
        self.tdx_import_after_download = False
        self.tdx_output_queue: SimpleQueue[tuple[str, object]] = SimpleQueue()
        self.tray_actions: SimpleQueue[tuple[str, str | None]] = SimpleQueue()
        self.tray_ready = threading.Event()
        self.tray_icon: pystray.Icon | None = None
        self.tray_shutdown_pending = False

        root.title(APP_TITLE)
        self.window_icon_images = [ImageTk.PhotoImage(render_icon(size), master=root) for size in (16, 32, 64, 256)]
        root.iconphoto(True, *self.window_icon_images)
        root.geometry("860x690")
        root.minsize(760, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="MarketListener", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="服务启动时自动启动两款通达信终端，并按盘后时段检查下载；异常弹窗和数据覆盖仍单独核验。",
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(4, 10))

        self.status = tk.StringVar(value="正在启动后端服务…")
        ttk.Label(frame, textvariable=self.status, wraplength=710).pack(anchor="w", pady=(0, 5))
        ttk.Label(frame, text=self.url, foreground="#2463a6").pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="启动 / 重试", command=self.start_backend)
        self.start_button.pack(side="left")
        self.open_button = ttk.Button(buttons, text="打开网页终端", command=self.open_web, state="disabled")
        self.open_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(buttons, text="停止后端", command=self.stop_backend, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="退出程序", command=lambda: self.close(force_exit=True)).pack(side="right")

        options = ttk.LabelFrame(frame, text="启动选项", padding=10)
        options.pack(fill="x", pady=(14, 10))
        self.startup_var = tk.BooleanVar(value=startup_enabled)
        self.silent_var = tk.BooleanVar(value=self.settings.silent_start)
        self.close_to_tray_var = tk.BooleanVar(value=self.settings.close_to_tray)
        self.startup_checkbox = ttk.Checkbutton(
            options, text="开机自启", variable=self.startup_var, command=self._toggle_startup,
            state="normal" if self.startup_supported else "disabled",
        )
        self.startup_checkbox.grid(row=0, column=0, sticky="w", padx=(0, 24))
        ttk.Checkbutton(
            options, text="静默启动（仅在系统托盘运行）",
            variable=self.silent_var, command=self._save_options,
        ).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(
            options, text="关闭时最小化到托盘",
            variable=self.close_to_tray_var, command=self._save_options,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(options, text="静默启动设置将在下次启动时生效。", foreground="#666666").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0),
        )

        tdx_frame = ttk.LabelFrame(frame, text="通达信盘后数据 · 两款终端自动；也可手动检查", padding=10)
        tdx_frame.pack(fill="x", pady=(0, 10))
        self.tdx_buttons: list[ttk.Button] = []
        self.tdx_buttons_by_terminal: dict[str, tuple[ttk.Button, ttk.Button]] = {}
        self.tdx_status: dict[str, tk.StringVar] = {}
        for row, (terminal, name) in enumerate(TERMINAL_NAMES.items()):
            ttk.Label(tdx_frame, text=name, width=17).grid(row=row, column=0, sticky="w", pady=3)
            start = ttk.Button(
                tdx_frame, text="启动终端",
                command=lambda key=terminal: self.run_tdx_action(key, download=False),
            )
            start.grid(row=row, column=1, padx=(0, 8), pady=3)
            download = ttk.Button(
                tdx_frame, text="下载日线和5分钟线",
                command=lambda key=terminal: self.run_tdx_action(key, download=True),
            )
            download.grid(row=row, column=2, padx=(0, 10), pady=3)
            state = tk.StringVar(value="等待手动操作")
            ttk.Label(tdx_frame, textvariable=state, wraplength=310).grid(row=row, column=3, sticky="w")
            self.tdx_status[terminal] = state
            self.tdx_buttons.extend((start, download))
            self.tdx_buttons_by_terminal[terminal] = (start, download)
        ttk.Label(
            tdx_frame,
            text="金融终端使用已保存模拟账户自动登录；两款软件可同时下载。界面完成后再核对原始文件和入库质量。",
            foreground="#666666", wraplength=790,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        log_frame = ttk.LabelFrame(frame, text="运行记录", padding=8)
        log_frame.pack(fill="both", expand=True)
        notebook = ttk.Notebook(log_frame)
        notebook.pack(fill="both", expand=True)
        summary_frame = ttk.Frame(notebook)
        raw_frame = ttk.Frame(notebook)
        notebook.add(summary_frame, text="运行摘要")
        notebook.add(raw_frame, text="完整日志（后端/通达信）")
        self.log_text = scrolledtext.ScrolledText(
            summary_frame, height=10, wrap="word", state="disabled",
            font=("Microsoft YaHei UI", 9), background="#111827", foreground="#e5e7eb",
            insertbackground="#e5e7eb",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("error", foreground="#fca5a5")
        self.log_text.tag_configure("warning", foreground="#fcd34d")
        self.log_text.tag_configure("success", foreground="#6ee7b7")
        self.log_text.tag_configure("service", foreground="#93c5fd")
        self.log_text.tag_configure("task", foreground="#ddd6fe")
        self.raw_log_text = scrolledtext.ScrolledText(
            raw_frame, height=10, wrap="word", state="disabled",
            font=("Consolas", 9), background="#111827", foreground="#d1d5db",
            insertbackground="#d1d5db",
        )
        self.raw_log_text.pack(fill="both", expand=True)
        ttk.Label(frame, text="完整日志：data_control\\logs\\launcher-backend.log、launcher-tdx.log", foreground="#666666").pack(
            anchor="w", pady=(8, 0),
        )

        for path, label in ((self.log_path, "后端"), (self.tdx_log_path, "通达信")):
            history, _, _ = read_log_chunk(path, 0)
            if history:
                self._append_to_view(
                    self.raw_log_text,
                    f"\n──── {label}历史日志（最近内容；运行摘要仅显示本次启动后的新事件） ────\n"
                    + history.decode("utf-8", "replace").replace("\r", "\n"),
                )
        self._append_log("[启动器] 图形程序已启动。\n")
        if startup_error:
            self._append_log(f"[启动器] 读取开机启动设置失败：{startup_error}\n")
        self._start_tray()
        if self.silent_for_this_run:
            root.withdraw()
        root.after(1500 if started_by_windows else 300, self.start_backend)
        root.after(1000, self._poll_process)
        root.after(350, self._poll_logs)
        root.after(300, self._drain_tdx_output)
        root.after(2500, self._poll_operations)
        root.after(100, self._drain_tray_actions)
        root.after(5000, self._verify_tray_ready)

    @staticmethod
    def _append_to_view(view: scrolledtext.ScrolledText, value: str, tag: str | None = None) -> None:
        at_bottom = view.yview()[1] >= 0.98
        view.configure(state="normal")
        view.insert("end", value, (tag,) if tag else ())
        line_count = int(view.index("end-1c").split(".", 1)[0])
        if line_count > MAX_LOG_LINES:
            view.delete("1.0", f"{line_count - MAX_LOG_LINES + 1}.0")
        view.configure(state="disabled")
        if at_bottom:
            view.see("end")

    def _append_event(self, level: str, message: str) -> None:
        labels = {
            "info": "提示", "service": "服务", "task": "任务",
            "success": "完成", "warning": "注意", "error": "错误",
        }
        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"{stamp}  [{labels.get(level, '提示')}] {message.strip()}\n"
        self._append_to_view(self.log_text, text, level)

    def _append_log(self, value: str) -> None:
        """Display launcher events in both in-app views."""

        self._append_to_view(self.raw_log_text, value)
        for line in value.splitlines():
            message = line.removeprefix("[启动器]").strip()
            if not message:
                continue
            level = "error" if "错误" in message or "失败" in message else "info"
            self._append_event(level, message)

    def _append_backend_output(self, value: str, *, tdx: bool = False) -> None:
        self._append_to_view(self.raw_log_text, value.replace("\r", "\n"))
        formatter = self.tdx_log_formatter if tdx else self.log_formatter
        for level, message in formatter.feed(value):
            self._append_event(level, message)

    def _poll_logs(self) -> None:
        raw, next_offset, truncated = read_log_chunk(self.log_path, self.log_offset)
        if truncated:
            self.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self.log_formatter = LauncherLogFormatter()
            self._append_log("\n[启动器] 日志文件已重新创建。\n")
        self.log_offset = next_offset
        if raw:
            self._append_backend_output(self.log_decoder.decode(raw, final=False))
        tdx_raw, tdx_offset, tdx_truncated = read_log_chunk(self.tdx_log_path, self.tdx_log_offset)
        if tdx_truncated:
            self.tdx_log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self.tdx_log_formatter = LauncherLogFormatter()
            self._append_log("[启动器] 通达信任务日志已重新创建。\n")
        self.tdx_log_offset = tdx_offset
        if tdx_raw:
            self._append_backend_output(self.tdx_log_decoder.decode(tdx_raw, final=False), tdx=True)
        self.root.after(750, self._poll_logs)

    def run_tdx_action(self, terminal: str, *, download: bool) -> None:
        if terminal not in TERMINAL_NAMES or terminal in self.tdx_busy_terminals:
            return
        self.tdx_busy_terminals.add(terminal)
        for button in self.tdx_buttons_by_terminal[terminal]:
            button.configure(state="disabled")
        name = TERMINAL_NAMES[terminal]
        action = "下载日线和5分钟线" if download else "启动终端"
        self.tdx_status[terminal].set(f"正在{action}…")
        self._append_log(f"[启动器] {name}：{action}由用户手动触发。\n")
        threading.Thread(
            target=self._tdx_worker,
            args=(terminal, download),
            name=f"marketlistener-tdx-{terminal}",
            daemon=True,
        ).start()

    def _tdx_worker(self, terminal: str, download: bool) -> None:
        report: dict[str, object] = {}
        error: str | None = None
        exit_code = 2
        try:
            command = build_tdx_command(self.project_root, terminal, download=download)
            self.tdx_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.tdx_log_path.open("ab") as log_file:
                start_offset = log_file.tell()
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=_service_environment(self.project_root),
                    close_fds=True,
                    **_hidden_subprocess_options(),
                )
            exit_code = process.wait()
            with self.tdx_log_path.open("rb") as log_file:
                log_file.seek(start_offset)
                for line in log_file.read().decode("utf-8", "replace").splitlines():
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(payload, dict):
                            report = payload
        except (OSError, ValueError) as caught:
            error = f"{type(caught).__name__}: {caught}"
        self.tdx_output_queue.put(("tdx_done", (terminal, download, exit_code, report, error)))

    def _on_tdx_done(
        self, terminal: str, download: bool, exit_code: int,
        report: dict[str, object], error: str | None,
    ) -> None:
        self.tdx_busy_terminals.discard(terminal)
        for button in self.tdx_buttons_by_terminal[terminal]:
            button.configure(state="normal")
        status = str(report.get("status") or "UNKNOWN")
        if download:
            terminals = report.get("terminals")
            if isinstance(terminals, dict):
                status = str(terminals.get(terminal) or status)
        message = TERMINAL_STATES.get(status, status)
        if error:
            message = error
        elif exit_code != 0 and status == "UNKNOWN":
            message = f"任务异常退出（代码 {exit_code}），请查看完整日志"
        self.tdx_status[terminal].set(message)
        level = "success" if exit_code == 0 else "warning"
        self._append_event(level, f"{TERMINAL_NAMES[terminal]}：{message}")
        if download and exit_code == 0 and status == "UI_DONE_UNVERIFIED":
            self.tdx_import_after_download = True
        if not self.tdx_busy_terminals and self.tdx_import_after_download:
            self.tdx_import_after_download = False
            self._queue_local_import()

    def _queue_local_import(self) -> None:
        if not self.ready:
            self._append_event("warning", "两款终端下载已完成；后端未就绪，尚未排队导入本地K线。")
            return
        self.coverage_check_queued = False
        self.import_operation_id = None

        def submit() -> None:
            try:
                request = Request(
                    f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api/operations",
                    data=json.dumps({"kind": "TDX_LOCAL_IMPORT"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    payload = json.load(response)
                operation = payload.get("operation", {})
                operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
                self.tdx_output_queue.put(("import", operation_id))
            except (HTTPError, URLError, OSError, ValueError) as caught:
                self.tdx_output_queue.put(("import_error", f"本地K线导入未能排队：{caught}"))

        threading.Thread(target=submit, name="marketlistener-tdx-import", daemon=True).start()

    def _queue_coverage_check(self) -> None:
        if self.coverage_check_queued:
            return
        self.coverage_check_queued = True

        def submit() -> None:
            try:
                request = Request(
                    f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api/operations",
                    data=json.dumps({"kind": "TDX_COVERAGE_CHECK"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    json.load(response)
                self.tdx_output_queue.put(("coverage", "本地文件覆盖检查已排队。"))
            except (HTTPError, URLError, OSError, ValueError) as caught:
                self.tdx_output_queue.put(("coverage_error", f"本地文件覆盖检查未能排队：{caught}"))

        threading.Thread(target=submit, name="marketlistener-tdx-coverage", daemon=True).start()

    def _poll_operations(self) -> None:
        if self.closing:
            return
        if self.ready and not self.operation_poll_busy:
            self.operation_poll_busy = True

            def fetch() -> None:
                try:
                    with urlopen(f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api/operations", timeout=3) as response:
                        payload = json.load(response)
                    items = payload.get("items", [])
                    self.tdx_output_queue.put(("operations", items))
                except (HTTPError, URLError, OSError, ValueError):
                    self.tdx_output_queue.put(("operations", []))

            threading.Thread(target=fetch, name="marketlistener-operations-view", daemon=True).start()
        self.root.after(3000, self._poll_operations)

    def _show_operations(self, items: object) -> None:
        self.operation_poll_busy = False
        if not isinstance(items, list):
            return
        cutoff = self.launch_started_at - timedelta(minutes=2)
        for item in reversed(items[:30]):
            if not isinstance(item, dict):
                continue
            operation_id = str(item.get("operation_id") or "")
            status = str(item.get("status") or "")
            kind = str(item.get("kind") or "")
            if not operation_id or not status or kind not in OPERATION_NAMES:
                continue
            try:
                created_at = datetime.fromisoformat(str(item.get("created_at")))
                if created_at.tzinfo is None or created_at < cutoff:
                    continue
            except ValueError:
                continue
            if self.operation_seen.get(operation_id) == status:
                continue
            self.operation_seen[operation_id] = status
            labels = {
                "QUEUED": ("task", "已排队"), "RUNNING": ("task", "运行中"),
                "PASS": ("success", "完成"),
                "PARTIAL_FAILURE": ("warning", "部分完成"),
                "FAILED": ("error", "失败"), "CANCELLED": ("warning", "已取消"),
            }
            level, state = labels.get(status, ("info", status))
            detail = str(item.get("detail") or "").removeprefix("PARTIAL_FAILURE:").removeprefix("PASS:").strip()
            if len(detail) > 220:
                detail = detail[:217] + "…"
            suffix = f" · {detail}" if detail and status not in {"QUEUED", "RUNNING"} else ""
            self._append_event(level, f"{OPERATION_NAMES[kind]}：{state}{suffix}")
            if (
                operation_id == self.import_operation_id
                and kind == "TDX_LOCAL_IMPORT"
                and status in {"PASS", "PARTIAL_FAILURE", "FAILED"}
            ):
                self._queue_coverage_check()

    def _drain_tdx_output(self) -> None:
        while True:
            try:
                kind, payload = self.tdx_output_queue.get_nowait()
            except Empty:
                break
            if kind == "tdx_done":
                self._on_tdx_done(*payload)
            elif kind == "operations":
                self._show_operations(payload)
            elif kind == "import":
                self.import_operation_id = str(payload) if payload else None
                self._append_event("task", f"本地K线导入已排队（任务 {payload or '未知'}）。")
            elif kind == "import_error":
                self._append_event("warning", str(payload))
            elif kind == "coverage":
                self._append_event("task", str(payload))
            elif kind == "coverage_error":
                self.coverage_check_queued = False
                self._append_event("warning", str(payload))
        if not self.closing:
            self.root.after(300, self._drain_tdx_output)

    def _start_tray(self) -> None:
        def queue_action(name: str):
            return lambda _icon, _item: self.tray_actions.put((name, None))

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", queue_action("show"), default=True),
            pystray.MenuItem("打开网页终端", queue_action("open")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 MarketListener", queue_action("exit")),
        )
        try:
            icon = pystray.Icon("MarketListener", create_tray_image(), APP_TITLE, menu)
            self.tray_icon = icon

            def setup(ready_icon: pystray.Icon) -> None:
                try:
                    ready_icon.visible = True
                    self.tray_ready.set()
                    if self.tray_shutdown_pending:
                        ready_icon.stop()
                except Exception as error:
                    self.tray_actions.put(("error", f"{type(error).__name__}: {error}"))

            icon.run_detached(setup=setup)
        except Exception as error:
            self.tray_actions.put(("error", f"{type(error).__name__}: {error}"))

    def _drain_tray_actions(self) -> None:
        while True:
            try:
                action, detail = self.tray_actions.get_nowait()
            except Empty:
                break
            if action == "show":
                self.show_window()
            elif action == "open":
                self.open_web()
            elif action == "exit":
                self.close(force_exit=True)
                return
            elif action == "error":
                self._append_log(f"[启动器] 系统托盘不可用：{detail}\n")
                self.root.deiconify()
        self.root.after(100, self._drain_tray_actions)

    def _verify_tray_ready(self) -> None:
        if self.tray_ready.is_set():
            return
        self._append_log("[启动器] 系统托盘图标未就绪，已显示主窗口以便处理。\n")
        self.root.deiconify()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def hide_to_tray(self) -> None:
        if not self.tray_ready.is_set():
            self.status.set("系统托盘尚未就绪，窗口保持打开。")
            self._append_log("[启动器] 系统托盘尚未就绪，未隐藏主窗口。\n")
            return
        self._append_log("[启动器] 主窗口已隐藏到系统托盘。\n")
        self.root.withdraw()

    def _stop_tray(self) -> None:
        icon = self.tray_icon
        if icon is None:
            return
        if not self.tray_ready.is_set():
            self.tray_shutdown_pending = True
            return
        try:
            icon.stop()
        except Exception:
            pass

    def _save_options(self) -> None:
        updated = LauncherSettings(
            silent_start=self.silent_var.get(),
            close_to_tray=self.close_to_tray_var.get(),
        )
        try:
            save_settings(self.settings_path, updated)
        except OSError as error:
            self.silent_var.set(self.settings.silent_start)
            self.close_to_tray_var.set(self.settings.close_to_tray)
            self._append_log(f"[启动器] 保存启动选项失败：{error}\n")
            messagebox.showerror(APP_TITLE, f"保存启动选项失败：{error}", parent=self.root)
            return
        self.settings = updated
        self._append_log("[启动器] 启动选项已保存。\n")

    def _toggle_startup(self) -> None:
        enabled = self.startup_var.get()
        try:
            set_startup_enabled(Path(sys.executable), enabled)
        except OSError as error:
            self.startup_var.set(not enabled)
            self._append_log(f"[启动器] 设置开机启动失败：{error}\n")
            messagebox.showerror(APP_TITLE, f"设置开机启动失败：{error}", parent=self.root)
            return
        self._append_log(f"[启动器] 已{'启用' if enabled else '关闭'}当前用户的开机启动。\n")

    def start_backend(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.ready = False
        self.status.set("正在检查后端并等待服务就绪…")
        self._append_log("[启动器] 正在检查本地后端服务…\n")
        self.start_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        threading.Thread(target=self._start_worker, name="marketlistener-launcher", daemon=True).start()

    def _start_worker(self) -> None:
        if _service_ready(self.health_url):
            owned = self.owns_process and self.process is not None and self.process.poll() is None
            self.root.after(0, lambda owned=owned: self._on_ready(owned=owned))
            return
        if _port_is_open(DEFAULT_HOST, DEFAULT_PORT):
            self.root.after(
                0,
                lambda: self._on_error(
                    f"端口 {DEFAULT_PORT} 已被其他程序占用，但它没有返回 MarketListener 健康响应。"
                ),
            )
            return

        try:
            command = build_backend_command(self.project_root)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=_service_environment(self.project_root),
                    close_fds=True,
                    **_hidden_subprocess_options(),
                )
            self.process = process
            self.owns_process = True
        except (OSError, ValueError) as error:
            message = f"启动后端失败：{error}"
            self.root.after(0, lambda message=message: self._on_error(message))
            return

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                message = f"后端进程提前退出（代码 {process.returncode}），请查看下方日志。"
                self.root.after(0, lambda: self._on_error(message))
                return
            if _service_ready(self.health_url):
                self.root.after(0, lambda: self._on_ready(owned=True))
                return
            time.sleep(0.5)

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self.root.after(0, lambda: self._on_error(f"后端在{READY_TIMEOUT_SECONDS}秒内未就绪，请查看启动日志。"))

    def _on_ready(self, *, owned: bool) -> None:
        self.busy = False
        self.ready = True
        self.owns_process = owned
        if self.silent_for_this_run:
            self.status.set("后端服务已就绪，程序正在系统托盘运行。")
        else:
            self.status.set("后端服务已就绪，可点击“打开网页终端”。" if owned else "检测到已有后端服务，可点击“打开网页终端”。")
        self.start_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.stop_button.configure(state="normal" if owned else "disabled")
        self._append_log("[启动器] 后端服务已就绪。\n")
        if not owned:
            self._append_log("[启动器] 当前服务由其他进程启动；可查看项目日志，但此窗口不能停止该服务。\n")

    def _on_error(self, message: str) -> None:
        self.busy = False
        self.ready = False
        if self.silent_for_this_run and self.tray_ready.is_set() and self.tray_icon is not None:
            try:
                self.tray_icon.notify("后端启动失败，请从托盘打开窗口查看日志。", APP_TITLE)
            except Exception:
                self.root.deiconify()
        else:
            self.root.deiconify()
        self.status.set("后端未就绪。请查看下方日志或重试。")
        self.start_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.stop_button.configure(state="normal" if self.owns_process else "disabled")
        self._append_log(f"[启动器] 错误：{message}\n")

    def open_web(self) -> None:
        if not self.ready:
            return
        self._append_log("[启动器] 打开网页终端由用户手动触发。\n")
        opened = webbrowser.open_new_tab(self.url)
        if not opened:
            self.status.set(f"服务已就绪；请手动打开 {self.url}")
            self._append_log("[启动器] 默认浏览器未确认打开，请使用上方地址。\n")

    def stop_backend(self) -> None:
        process = self.process
        if not self.owns_process or process is None or process.poll() is not None:
            self.status.set("当前没有由此启动器管理的后端进程。")
            self.stop_button.configure(state="disabled")
            return
        self.status.set("正在停止后端服务…")
        self._append_log("[启动器] 正在停止由此启动器创建的后端服务…\n")
        self.stop_button.configure(state="disabled")

        def stop_worker() -> None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                message = str(error)
                self.root.after(0, lambda message=message: self._append_log(f"[启动器] 停止后端失败：{message}\n"))
            finally:
                self.root.after(0, self._on_stopped)

        threading.Thread(target=stop_worker, name="marketlistener-stop", daemon=True).start()

    def _on_stopped(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.closing = False
            self.root.deiconify()
            self.status.set("后端仍在运行，请重试停止。")
            self.stop_button.configure(state="normal")
            self.root.after(100, self._drain_tray_actions)
            self.root.after(1000, self._poll_process)
            return
        self.ready = False
        self.owns_process = False
        self.process = None
        self.status.set("后端服务已停止。")
        self._append_log("[启动器] 后端服务已停止。\n")
        self.start_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        if self.closing:
            self._destroy_app()

    def _poll_process(self) -> None:
        if self.closing:
            return
        if self.owns_process and self.process is not None and self.process.poll() is not None:
            self._on_stopped()
        self.root.after(1000, self._poll_process)

    def close(self, *, force_exit: bool = False) -> None:
        if not force_exit and self.close_to_tray_var.get():
            self.hide_to_tray()
            return
        self._exit_application()

    def _exit_application(self) -> None:
        if self.closing:
            return
        self.closing = True
        self._append_log("[启动器] 正在退出程序。\n")
        self.root.withdraw()
        process = self.process
        if self.owns_process and process is not None and process.poll() is None:
            self.stop_backend()
            return
        self._destroy_app()

    def _destroy_app(self) -> None:
        self._stop_tray()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test:
        try:
            find_project_root()
        except FileNotFoundError:
            return 1
        return 0
    try:
        project_root = find_project_root()
    except FileNotFoundError as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, str(error), parent=root)
        root.destroy()
        return 1
    settings = load_settings(project_root / "data_control" / "state" / "launcher_settings.json")
    _set_windows_app_id()
    root = tk.Tk()
    if settings.silent_start:
        root.withdraw()
    LauncherWindow(root, project_root, started_by_windows=args.startup, settings=settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
