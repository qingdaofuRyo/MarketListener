"""Local internal console and supervisor for the customer web service.

This process owns no market database connection. Internal data actions are
forwarded to the customer runtime with a server-only credential, keeping its
existing single-process catalog/operation coordination.
"""

from __future__ import annotations

import argparse
import ctypes
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request as URLRequest, build_opener

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from . import __version__
from .admin_boundary import ADMIN_TOKEN_ENV, ADMIN_TOKEN_HEADER
from .local_session import (CONSOLE_CSRF_HEADER, LocalSessionManager, install_local_sessions,
                            load_control_token)
from .operations import OperationKind
from .process_runtime import process_start_token
from .profile_gate import profile_write_gate


_backend_opener = build_opener(ProxyHandler({}))


CONSOLE_SERVICE = "marketlistener-internal-console"
CSRF_HEADER = CONSOLE_CSRF_HEADER
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MarketListenerServices"


def _research_pid_alive(pid: object) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(number, 0)
        except OSError:
            return False
        return True
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, number)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(process)
OPERATION_LABELS = {
    "BACKUP_CREATE": "创建个人资料加密备份",
    "MARKET_READMODEL_PUBLISH": "发布行情读取快照",
    "MARKET_UPDATE": "更新行情", "FUTURES_OFFICIAL_DAILY_UPDATE": "更新官方期货日行情",
    "FUTURES_CONTRACT_INFO_UPDATE": "更新期货合约信息",
    "FUTURES_SETTLEMENT_PARAMS_UPDATE": "更新期货结算参数",
    "FUTURES_MARKET_FINANCIAL_UPDATE": "更新期货行情指标",
    "FUTURES_MEMBER_RANK_UPDATE": "更新日成交持仓排名",
    "TDX_COVERAGE_CHECK": "检查通达信文件", "TDX_LOCAL_IMPORT": "导入本地数据",
    "TDX_FINANCIAL_LAUNCH": "启动通达信金融终端",
    "TDX_FINANCIAL_AUTO_DOWNLOAD": "下载金融终端盘后数据",
    "TDX_FUTURES_LAUNCH": "启动通达信期货通",
    "TDX_FUTURES_AUTO_DOWNLOAD": "下载期货通盘后数据",
    "FUTURES_HEAT_UPDATE": "更新期货热度", "FUTURES_STRUCTURE_UPDATE": "重建期货结构",
    "EQUITY_DASHBOARD_UPDATE": "更新数据页市场指标", "DISCLOSURES_UPDATE": "检索巨潮公告关键词",
    "A_SHARE_DELISTED_UPDATE": "更新沪深摘牌名单",
    "F10_UPDATE_CN": "更新A股F10", "F10_UPDATE_HK": "更新港股F10", "REVENUE_UPDATE": "更新收入构成",
    "F10_REVENUE_REPUBLISH": "恢复收入构成快照",
    "REPORT_PROCESS": "处理研报", "REPORT_VERIFY": "校验研报", "CHAIN_REBUILD": "重建产业链",
    "ATLAS_BUILD": "构建产业图谱", "ANDROID_PACKAGE_BUILD": "构建签名数据包", "STATUS_REFRESH": "刷新状态",
}


@dataclass(frozen=True)
class ConsoleSettings:
    auto_start: bool = True
    auto_update: bool = True
    background_priority: bool = True
    theme: str = "dark"


def load_console_settings(path: Path) -> ConsoleSettings:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ConsoleSettings()
    if not isinstance(value, dict) or value.get("schema") != "marketlistener/control-settings/v1":
        return ConsoleSettings()
    return ConsoleSettings(
        auto_start=value.get("auto_start", True) is True,
        auto_update=value.get("auto_update", True) is True,
        background_priority=value.get("background_priority", True) is True,
        theme=value.get("theme") if value.get("theme") in {"dark", "light", "system"} else "dark",
    )


def backend_command(project: Path, data_root: Path, port: int, settings: ConsoleSettings) -> list[str]:
    executable = project / "desktop" / ".venv" / "Scripts" / "python.exe" if os.name == "nt" else project / "desktop" / ".venv" / "bin" / "python"
    if not executable.is_file():
        raise FileNotFoundError("项目Python环境尚未安装")
    command = [str(executable), "-m", "market_monitor", "serve", "--data-root", str(data_root), "--host", "127.0.0.1", "--port", str(port), "--quiet"]
    if not settings.auto_update:
        command.append("--no-auto-update-on-startup")
    return command


class ServiceSupervisor:
    def __init__(self, project: Path, data_root: Path, *, backend_port: int = 8765) -> None:
        self.project = project.resolve()
        self.data_root = data_root.resolve()
        self.backend_port = backend_port
        self.backend_url = f"http://127.0.0.1:{backend_port}"
        self.settings_path = self.data_root / "state" / "control_settings.json"
        self.settings = load_console_settings(self.settings_path)
        self.backend_token = secrets.token_urlsafe(32)
        self.process: subprocess.Popen | None = None
        self.backend_runtime_pid: int | None = None
        self.backend_runtime_token: str | None = None
        self.lock = threading.RLock()
        self.action: str | None = None
        self.action_phase: str | None = None
        self.action_started_at: str | None = None
        self.action_started_monotonic: float | None = None
        self.action_error: str | None = None
        self.startup_id: str | None = None
        self.started_at: float | None = None
        self.console_started_at = time.monotonic()
        self.terminal_busy = False
        self.terminal_jobs: dict[str, dict] = {}
        self.log_dir = self.data_root / "logs"
        self.shutting_down = False
        self.cpu_sample: tuple[float, float] | None = None
        self.resource_sample_at = 0.0
        self.resource_sample_process: subprocess.Popen | None = None
        self.resource_snapshot: dict[str, float | int | None] = {"cpuPercent": None, "memoryBytes": None}
        from .tdx_local_status import TdxLocalStatus

        self.tdx_status = TdxLocalStatus(
            self.data_root,
            lambda: self.backend_request("/api/internal/tdx-import-inventory", timeout=12),
        )

    def backend_request(self, path: str, *, method: str = "GET", payload: dict | None = None, timeout: float = 2) -> dict:
        if not path.startswith("/api/"):
            raise ValueError("invalid internal API path")
        request = URLRequest(
            self.backend_url + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={ADMIN_TOKEN_HEADER: self.backend_token, "Content-Type": "application/json"}, method=method,
        )
        try:
            with _backend_opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 403:
                raise RuntimeError("当前客户服务由其他启动方式管理；请退出原启动程序后，由本管理台启动服务") from None
            raise RuntimeError(f"客户服务接口返回HTTP {error.code}") from None
        except (URLError, OSError, ValueError):
            raise RuntimeError("客户服务暂时不可用") from None

    def ready(self) -> bool:
        try:
            value = self.backend_request("/api/ready", timeout=1)
            return value.get("status") == "READY"
        except RuntimeError:
            return False

    def capture_backend_identity(self) -> None:
        """Bind the owned service to its authenticated runtime PID and lifecycle."""

        value = self.backend_request("/api/internal/runtime", timeout=1)
        pid = value.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("客户服务进程身份无效")
        token = process_start_token(pid)
        if token is None:
            raise RuntimeError("客户服务进程身份暂不可核验")
        if self.backend_runtime_pid is not None and (
            self.backend_runtime_pid != pid or self.backend_runtime_token != token
        ):
            raise RuntimeError("客户服务进程身份发生变化")
        self.backend_runtime_pid = pid
        self.backend_runtime_token = token

    def startup_stage(self) -> dict | None:
        """Read only the bounded progress record for this supervisor's launch."""

        if self.startup_id is None:
            return None
        path = self.data_root / "state" / "service_startup.json"
        try:
            if path.stat().st_size > 4096:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("schema") != 1 or value.get("launchId") != self.startup_id:
            return None
        phase = value.get("phase")
        if phase not in {
            "APP_CONSTRUCTION", "RESTORE_RECOVERY", "CATALOG_MIGRATION",
            "QUERY_INDEX_SETUP", "READMODEL_CHECK", "BACKGROUND_WATCHER",
            "BACKUP_QUEUE", "UPDATE_QUEUE", "READY", "FAILED",
        }:
            return None
        return {"phase": phase, "status": value.get("status"), "updatedAt": value.get("updatedAt")}

    def snapshot(self) -> dict:
        with self.lock:
            process = self.process
            started_at = self.started_at
        owned = process is not None and process.poll() is None
        ready = self.ready()
        resources: dict = {"cpuPercent": None, "memoryBytes": None}
        if owned and ready:
            now = time.monotonic()
            with self.lock:
                sample_due = (self.resource_sample_process is not process or
                              now - self.resource_sample_at >= 6)
                previous_cpu_sample = self.cpu_sample if self.resource_sample_process is process else None
                if not sample_due:
                    resources = dict(self.resource_snapshot)
            if sample_due:
                try:
                    runtime = self.backend_request("/api/internal/runtime", timeout=.5)
                    sampled_at = time.monotonic()
                    cpu = float(runtime["cpuSeconds"])
                    logical_cpus = max(1, int(runtime["logicalCpus"]))
                    if previous_cpu_sample is not None:
                        seconds = sampled_at - previous_cpu_sample[0]
                        resources["cpuPercent"] = round(max(0, min(100, (cpu - previous_cpu_sample[1]) / max(seconds, .001) * 100 / logical_cpus)), 1)
                    resources["memoryBytes"] = runtime.get("memoryBytes")
                    with self.lock:
                        if self.process is process:
                            self.cpu_sample = (sampled_at, cpu)
                            self.resource_snapshot = dict(resources)
                            self.resource_sample_at = sampled_at
                            self.resource_sample_process = process
                except (RuntimeError, KeyError, ValueError, TypeError, OverflowError):
                    with self.lock:
                        if self.process is process:
                            self.resource_snapshot = dict(resources)
                            self.resource_sample_at = time.monotonic()
                            self.resource_sample_process = process
        else:
            with self.lock:
                self.resource_sample_at = 0.0
        with self.lock:
            action = self.action
            action_phase = self.action_phase
            action_started_at = self.action_started_at
            action_elapsed_seconds = (int(time.monotonic() - self.action_started_monotonic)
                                      if action and self.action_started_monotonic is not None else None)
            action_error = self.action_error
            startup_stage = self.startup_stage()
        return {
            "ready": ready, "owned": owned, "pid": process.pid if owned else None,
            "state": "busy" if action else "running" if ready else "unresponsive" if owned else "stopped",
            "action": action, "actionPhase": action_phase,
            "actionStartedAt": action_started_at, "actionElapsedSeconds": action_elapsed_seconds,
            "startupStage": startup_stage,
            "error": action_error,
            "uptimeSeconds": int(time.monotonic() - started_at) if owned and started_at else None,
            "backendUrl": self.backend_url + "/", "settings": asdict(self.settings),
            "consoleUptimeSeconds": int(time.monotonic() - self.console_started_at),
            "terminalJobs": self.terminal_jobs, "terminalBusy": self.terminal_busy,
            "resources": resources,
        }

    def schedule(self, action: str) -> None:
        if action not in {"start", "stop", "restart"}:
            raise ValueError("unknown action")
        with self.lock:
            if self.shutting_down:
                raise RuntimeError("管理服务正在关闭")
            if self.action:
                raise RuntimeError("已有服务操作正在执行")
            if action == "start" and self.process is not None and self.process.poll() is None:
                raise RuntimeError("客户服务进程仍在运行；请等待就绪探针恢复，或使用“重启服务”")
            if action in {"stop", "restart"} and (self.process is None or self.process.poll() is not None):
                raise RuntimeError("只能停止或重启由本管理台启动的服务")
            self.action, self.action_error = action, None
            self.action_phase = "STOPPING" if action in {"stop", "restart"} else "STARTING"
            self.action_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.action_started_monotonic = time.monotonic()

        def run() -> None:
            try:
                if action in {"stop", "restart"}:
                    self.stop()
                if action in {"start", "restart"}:
                    self.start()
            except Exception as error:
                with self.lock:
                    self.action_error = str(error)
            finally:
                with self.lock:
                    self.action = None
                    self.action_phase = None
                    self.action_started_at = None
                    self.action_started_monotonic = None

        try:
            threading.Thread(target=run, name="control-service", daemon=True).start()
        except (RuntimeError, OSError) as error:
            with self.lock:
                self.action = None
                self.action_phase = None
                self.action_started_at = None
                self.action_started_monotonic = None
                self.action_error = f"无法启动服务控制线程：{type(error).__name__}"
            raise RuntimeError(self.action_error) from error

    def start(self) -> None:
        if self.shutting_down:
            return
        with self.lock:
            if self.action:
                self.action_phase = "STARTING"
        if self.process is not None and self.process.poll() is None:
            if self.ready():
                return
            raise RuntimeError("客户服务进程仍在运行但暂未响应；请等待或重启服务")
        with socket.socket() as probe:
            probe.settimeout(.4)
            port_in_use = probe.connect_ex(("127.0.0.1", self.backend_port)) == 0
        if port_in_use:
            if self.ready():
                return
            raise RuntimeError(f"端口{self.backend_port}被其他程序占用或服务暂未就绪")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.project / "desktop" / "src")
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment[ADMIN_TOKEN_ENV] = self.backend_token
        self.startup_id = secrets.token_hex(16)
        environment["MARKETLISTENER_STARTUP_ID"] = self.startup_id
        if self.settings.background_priority:
            for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
                environment.setdefault(name, "2")
        options: dict = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | (subprocess.BELOW_NORMAL_PRIORITY_CLASS if self.settings.background_priority else 0)
        else:
            options["start_new_session"] = True
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.shutting_down:
            return
        with (self.log_dir / "web-backend.log").open("ab") as output:
            self.process = subprocess.Popen(backend_command(self.project, self.data_root, self.backend_port, self.settings), cwd=self.project, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options)
        self.backend_runtime_pid = None
        self.backend_runtime_token = None
        self.started_at = time.monotonic()
        self.cpu_sample = None
        with self.lock:
            if self.action:
                self.action_phase = "WAITING_READY"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.shutting_down:
                self.stop()
                return
            if self.process.poll() is not None:
                raise RuntimeError("客户服务启动失败，请查看后台日志")
            if self.ready():
                if os.name == "nt":
                    try:
                        self.capture_backend_identity()
                    except RuntimeError:
                        time.sleep(.5)
                        continue
                return
            time.sleep(.5)
        with self.lock:
            if self.action:
                self.action_phase = "CLEANING_UP"
        last_stage = self.startup_stage()
        try:
            self.stop()
        except Exception as error:
            raise RuntimeError(
                f"客户服务启动后90秒仍未就绪（阶段：{(last_stage or {}).get('phase') or '未知'}），"
                f"清理进程失败：{error}；请查看客户服务运行日志"
            ) from error
        raise RuntimeError(
            f"客户服务启动后90秒仍未就绪（阶段：{(last_stage or {}).get('phase') or '未知'}），"
            "已停止本次进程；请查看客户服务运行日志"
        )

    def stop(self) -> None:
        process = self.process
        if process is None:
            self.process = None
            self.backend_runtime_pid = None
            self.backend_runtime_token = None
            self.started_at = None
            self.cpu_sample = None
            self.startup_id = None
            return

        # The Windows venv redirector starts a second Python process. That backend
        # can launch TDX GUI windows which must survive an incomplete download.
        # A recursive taskkill would also terminate those independently owned GUIs.
        if os.name == "nt":
            if self.backend_runtime_pid is None or self.backend_runtime_token is None:
                self.capture_backend_identity()
            runtime_pid = self.backend_runtime_pid
            runtime_token = self.backend_runtime_token
            if runtime_pid is None or runtime_token is None:
                raise RuntimeError("客户服务进程身份暂不可核验")
            current_token = process_start_token(runtime_pid)
            if current_token != runtime_token:
                raise RuntimeError("客户服务进程身份已变化或暂不可核验，拒绝终止未知进程")
            subprocess.run(["taskkill", "/PID", str(runtime_pid), "/F"],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           capture_output=True, timeout=15, check=False)
            deadline = time.monotonic() + 8
            while process_start_token(runtime_pid) == runtime_token and time.monotonic() < deadline:
                time.sleep(.1)
            if process_start_token(runtime_pid) == runtime_token:
                raise RuntimeError("客户服务Python进程未能停止")
            if process.poll() is None and runtime_pid != process.pid:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/F"],
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               capture_output=True, timeout=15, check=False)
        else:
            if process.poll() is not None:
                self.process = None
                self.started_at = None
                self.cpu_sample = None
                self.startup_id = None
                return
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            else:
                raise RuntimeError("客户服务仍在退出，请稍后重试") from None
        self.process = None
        self.backend_runtime_pid = None
        self.backend_runtime_token = None
        self.started_at = None
        self.cpu_sample = None
        self.startup_id = None

    def save_settings(self, value: ConsoleSettings) -> None:
        with profile_write_gate(self.data_root):
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"schema": "marketlistener/control-settings/v1", **asdict(value)}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.settings_path)
        self.settings = value

    def startup_enabled(self) -> bool:
        if os.name != "nt":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, RUN_NAME)
            return value == self.startup_command()
        except FileNotFoundError:
            return False

    def startup_command(self) -> str:
        return f'"{self.project / "desktop/.venv/Scripts/pythonw.exe"}" "{self.project / "scripts/start_marketlistener.py"}" --target none'

    def set_startup(self, enabled: bool) -> None:
        if os.name != "nt":
            raise RuntimeError("当前系统请使用其服务管理器配置自动启动")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, self.startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass

    def run_terminal(self, terminal: str, action: str) -> None:
        if os.name != "nt":
            raise RuntimeError("通达信桌面下载当前仅在已安装终端的Windows数据生产机上可用")
        if terminal not in {"financial", "futures"} or action not in {"launch", "download"}:
            raise ValueError("invalid terminal action")
        with self.lock:
            if self.terminal_busy:
                raise RuntimeError("已有通达信任务正在运行")
            self.terminal_busy = True
            self.terminal_jobs[terminal] = {"status": "RUNNING", "action": action, "detail": "正在执行"}

        def run() -> None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                state_dir = self.data_root / "state" / "tdx_download_ui"
                python = self.project / "desktop/.venv/Scripts/python.exe"
                extra: list[str] = []
                if terminal == "financial" and action == "download":
                    from .tdx_gui_auto_refresh import _is_x86_python, financial_minute_start

                    python = self.data_root / "tools" / "python311-x86" / "python.exe"
                    if not _is_x86_python(python):
                        raise RuntimeError("TDX_32BIT_RUNTIME_MISSING")
                    extra = ["--auto-login-saved", "--extended-minute-start",
                             financial_minute_start(self.data_root, datetime.now(timezone.utc)).isoformat()]
                command = [str(python), "-m", "market_monitor.tdx_download_ui", "--terminal", terminal,
                           "--launch-only" if action == "launch" else "--execute", *extra]
                command.extend(["--state-dir", str(state_dir)])
                environment = os.environ.copy()
                environment.update(PYTHONPATH=str(self.project / "desktop/src"), PYTHONUTF8="1",
                                   PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
                path = self.log_dir / "web-tdx.log"
                with path.open("ab") as output:
                    offset = output.tell()
                    child = subprocess.Popen(command, cwd=self.project, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                code = child.wait()
                report: dict = {}
                with path.open("rb") as output:
                    output.seek(offset)
                    for line in output.read().decode("utf-8", "replace").splitlines():
                        if line.startswith("{"):
                            try:
                                report = json.loads(line)
                            except ValueError:
                                continue
                status = report.get("terminals", {}).get(terminal) if action == "download" else report.get("status")
                self.terminal_jobs[terminal] = {"status": "PASS" if code == 0 else "FAILED", "action": action, "detail": status or "驱动异常，请查看通达信日志"}
                if action == "download" and code == 0 and report.get("status") == "UI_DONE_UNVERIFIED":
                    submitted = self.backend_request("/api/operations", method="POST", payload={"kind": "TDX_LOCAL_IMPORT"}, timeout=5)
                    operation_id = submitted.get("operation", {}).get("operation_id")
                    deadline = time.monotonic() + 1800
                    while operation_id and time.monotonic() < deadline:
                        items = self.backend_request("/api/operations").get("items", [])
                        matched = next((item for item in items if item.get("operation_id") == operation_id), {})
                        if matched.get("status") in {"PASS", "PARTIAL_FAILURE", "FAILED", "CANCELLED"}:
                            self.backend_request("/api/operations", method="POST", payload={"kind": "TDX_COVERAGE_CHECK"}, timeout=5)
                            break
                        time.sleep(3)
            except Exception as error:
                existing = self.terminal_jobs.get(terminal, {})
                if existing.get("status") == "PASS" and action == "download":
                    self.terminal_jobs[terminal] = {**existing, "followUpError": str(error)}
                else:
                    self.terminal_jobs[terminal] = {"status": "FAILED", "action": action, "detail": str(error)}
            finally:
                self.terminal_busy = False

        threading.Thread(target=run, name="control-tdx", daemon=True).start()

    def tail(self, source: str) -> list[str]:
        names = {"backend": "web-backend.log", "console": "web-admin.log", "terminal": "web-tdx.log"}
        path = self.log_dir / names[source]
        if not path.exists() and source == "backend":
            path = self.log_dir / "launcher-backend.log"
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                value = stream.read(65536).decode("utf-8", "replace").replace(self.backend_token, "[redacted]")
            return value.replace("\r", "\n").splitlines()[-350:]
        except OSError:
            return []


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    auto_start: bool
    auto_update: bool
    background_priority: bool
    theme: Literal["dark", "light", "system"]


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: OperationKind


class StartupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class ProfileBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^profile-\d{8}T\d{6}Z-[0-9a-f]{8}\.mlbackup$")
    passphrase: str | None = Field(default=None, min_length=12, max_length=1024)


class ProfileRestoreRequest(ProfileBackupRequest):
    planDigest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["scan", "process", "propose", "visual", "visual-queue", "visual-batch", "publish"]
    maxPages: int = Field(default=500, ge=1, le=100_000)
    modelUrl: str | None = None
    modelName: str | None = None
    extractionVersion: str | None = None
    retryProblemPages: bool = False
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)


class ResearchCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    kind: Literal["INDUSTRY_RELATION", "QUANT_METHOD"]
    payload: dict
    evidenceText: str = Field(min_length=8, max_length=2000)


class ResearchReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["APPROVED", "REJECTED"]
    reviewer: str = Field(min_length=2, max_length=100)
    note: str = Field(min_length=2, max_length=1000)
    currentVerified: bool = False


class ResearchRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


class ResearchVisualRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    modelUrl: str = Field(min_length=10, max_length=200)
    modelName: str = Field(min_length=2, max_length=100)
    label: str = Field(min_length=2, max_length=100)


class ResearchVisualPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["INDUSTRY_RELATION", "QUANT_METHOD"]
    payload: dict


class ResearchOCRResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["UMI", "RAPID"]
    reviewer: str = Field(min_length=2, max_length=100)
    note: str = Field(min_length=2, max_length=1000)


def create_admin_app(supervisor: ServiceSupervisor, *, port: int = 8766, auto_start: bool = False,
                     shutdown: Callable | None = None, assets_dir: Path | None = None,
                     control_token: str | None = None) -> FastAPI:
    assets = assets_dir or Path(__file__).with_name("admin_web_dist")
    restore_lock = threading.RLock()
    restore_jobs: dict[str, dict] = {}
    research_lock = threading.RLock()
    research_jobs: dict[str, dict] = {}
    research_cancellations: dict[str, threading.Event] = {}

    def offline_profile_path(name: str) -> Path:
        if (supervisor.action or supervisor.terminal_busy or
                supervisor.process is not None and supervisor.process.poll() is None):
            raise HTTPException(409, "请先停止客户服务与终端任务，再预览或恢复个人资料")
        with socket.socket() as connection:
            connection.settimeout(.2)
            if connection.connect_ex(("127.0.0.1", supervisor.backend_port)) == 0:
                raise HTTPException(409, "客户端口仍在监听，请先停止客户服务")
        return supervisor.data_root / "backups" / "profile" / name

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        supervisor.tdx_status.refresh()
        if auto_start and supervisor.settings.auto_start:
            supervisor.schedule("start")
        yield
        supervisor.shutting_down = True
        if supervisor.process is not None:
            supervisor.stop()

    app = FastAPI(title="MarketListener Internal Console", docs_url=None, redoc_url=None, lifespan=lifespan)
    install_local_sessions(app, LocalSessionManager("admin", port), control_token=control_token,
                           admin_local_access=True)

    @app.middleware("http")
    async def local_management_only(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    def proxy(path: str, method: str = "GET", payload: dict | None = None) -> dict:
        try:
            return supervisor.backend_request(path, method=method, payload=payload, timeout=5)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/admin/ready")
    def ready() -> dict:
        return {"service": CONSOLE_SERVICE, "status": "READY"}

    @app.get("/api/admin/state")
    def state(request: Request) -> dict:
        return {**supervisor.snapshot(), "csrfToken": request.state.local_session.csrf, "role": "本机管理员", "scope": "仅本机访问", "version": __version__, "startupEnabled": supervisor.startup_enabled(), "startupSupported": os.name == "nt", "projectPath": str(supervisor.project), "dataPath": str(supervisor.data_root), "operationLabels": OPERATION_LABELS}

    @app.post("/api/admin/open-product")
    def open_product() -> dict:
        value = proxy("/api/internal/local-session/bootstrap", "POST", {})
        return {"bootstrap": value["bootstrap"], "url": supervisor.backend_url + "/"}

    @app.post("/api/admin/service/{action}", status_code=202)
    def service(action: Literal["start", "stop", "restart"]) -> dict:
        with restore_lock:
            if any(item["status"] in {"QUEUED", "RUNNING"} for item in restore_jobs.values()):
                raise HTTPException(409, "个人资料恢复尚未结束，请等待作业完成")
        try:
            supervisor.schedule(action)
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "ACCEPTED"}

    @app.get("/api/admin/tasks")
    def tasks() -> dict:
        return proxy("/api/operations")

    @app.get("/api/admin/backups/profile")
    def profile_backups() -> dict:
        from .local_backup import profile_backups as list_profile_backups

        items = list_profile_backups(supervisor.data_root)
        return {"items": items, "total": len(items)}

    @app.post("/api/admin/backups/profile/verify")
    def verify_profile_backup(request: ProfileBackupRequest) -> dict:
        from .local_backup import BackupError, verify_profile_backup as verify_archive

        try:
            return verify_archive(supervisor.data_root / "backups" / "profile" / request.name,
                                  passphrase=request.passphrase)
        except BackupError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/admin/backups/profile/restore-preview")
    def preview_profile_backup(request: ProfileBackupRequest) -> dict:
        from .local_backup import BackupError, preview_profile_restore

        path = offline_profile_path(request.name)
        try:
            return preview_profile_restore(path, supervisor.data_root, passphrase=request.passphrase)
        except BackupError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/admin/backups/profile/restore", status_code=202)
    def start_profile_restore(request: ProfileRestoreRequest) -> dict:
        from .local_backup import BackupError, restore_profile_backup

        path = offline_profile_path(request.name)
        with restore_lock:
            if any(item["status"] in {"QUEUED", "RUNNING"} for item in restore_jobs.values()):
                raise HTTPException(409, "已有个人资料恢复任务正在运行")
            identifier = secrets.token_hex(16)
            restore_jobs[identifier] = {"id": identifier, "status": "QUEUED",
                                        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "name": request.name}

        def perform() -> None:
            with restore_lock:
                restore_jobs[identifier]["status"] = "RUNNING"
            try:
                offline_profile_path(request.name)
                result = restore_profile_backup(path, supervisor.data_root,
                                                expected_plan_digest=request.planDigest,
                                                passphrase=request.passphrase, offline=True)
            except (BackupError, HTTPException, OSError) as error:
                with restore_lock:
                    restore_jobs[identifier].update(status="FAILED", detail=str(error),
                        completedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            except Exception as error:
                with restore_lock:
                    restore_jobs[identifier].update(status="FAILED", detail="恢复过程异常，保留回滚日志",
                        errorType=type(error).__name__,
                        completedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            else:
                with restore_lock:
                    restore_jobs[identifier].update(status="PASS", result=result,
                        completedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        threading.Thread(target=perform, name="marketlistener-profile-restore", daemon=True).start()
        return {"id": identifier, "status": "QUEUED"}

    @app.get("/api/admin/backups/profile/restore/{identifier}")
    def profile_restore_job(identifier: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise HTTPException(400, "恢复任务标识无效")
        with restore_lock:
            value = restore_jobs.get(identifier)
            if value is None:
                raise HTTPException(404, "恢复任务不存在；服务重启后可查看恢复日志")
            return dict(value)

    @app.post("/api/admin/tasks", status_code=202)
    def submit(request: TaskRequest) -> dict:
        return proxy("/api/operations", "POST", {"kind": request.kind})

    @app.post("/api/admin/tasks/{identifier}/cancel")
    def cancel(identifier: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise HTTPException(400, "任务标识无效")
        return proxy(f"/api/operations/{identifier}/cancel", "POST")

    @app.get("/api/admin/coverage")
    def coverage() -> dict:
        return proxy("/api/tdx-afterhours/coverage")

    @app.get("/api/admin/manual-data-gaps")
    def manual_data_gaps(
        source: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int = 40,
    ) -> dict:
        from .manual_data_gaps import tdx_gap_items

        if limit < 1 or limit > 200 or start and end and start > end:
            raise HTTPException(422, "日期范围或数量无效")
        if source is not None and (len(source) > 80 or not re.fullmatch(r"[a-z0-9.-]+", source)):
            raise HTTPException(422, "数据源标识无效")
        start_day = start.isoformat() if start else None
        end_day = end.isoformat() if end else None
        local = (supervisor.tdx_status.snapshot(enabled=supervisor.settings.auto_update,
                                                trigger_scan=False)
                 if source is None or source.startswith("tdx.") else {})
        items = (tdx_gap_items(supervisor.data_root, start=start_day, end=end_day,
                               limit=limit, local_status=local)
                 if source is None or source.startswith("tdx.") else [])
        query = f"?limit={limit}"
        if start_day:
            query += f"&start={start_day}"
        if end_day:
            query += f"&end={end_day}"
        if source:
            query += f"&source={source}"
        backend_error = None
        if source is None or not source.startswith("tdx."):
            try:
                remote = proxy("/api/internal/manual-data-gaps" + query)
                items.extend(remote.get("items", []))
            except HTTPException as error:
                backend_error = str(error.detail)
        if source:
            items = [item for item in items if item.get("key") == source]
        return {"generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "items": items, "backendError": backend_error}

    @app.get("/api/admin/tdx-status")
    def tdx_status(refresh: bool = False) -> dict:
        return supervisor.tdx_status.snapshot(refresh=refresh, enabled=supervisor.settings.auto_update)

    @app.get("/api/admin/delisted-review/{exchange}")
    def delisted_review(exchange: Literal["SSE", "SZSE"], version: str, offset: int = 0, limit: int = 40,
                        start: date | None = None, end: date | None = None) -> dict:
        from .exchange_delisted import DelistedReviewChanged, DelistedSourceError, review_page

        if (not re.fullmatch(r"[0-9a-f]{64}", version) or not 0 <= offset <= 100_000
                or not 1 <= limit <= 200 or start and end and start > end):
            raise HTTPException(422, "待核版本、日期或分页无效")
        try:
            return review_page(supervisor.data_root, exchange, version=version, offset=offset, limit=limit,
                               start=start.isoformat() if start else None, end=end.isoformat() if end else None)
        except DelistedReviewChanged as error:
            raise HTTPException(409, "待核名单已变化，请重新查询") from error
        except DelistedSourceError as error:
            raise HTTPException(503, "待核证据不可读取，请检查本机来源") from error

    @app.get("/api/admin/providers")
    def providers() -> dict:
        return proxy("/api/data-sources/providers")

    @app.get("/api/admin/inventory")
    def inventory() -> dict:
        return proxy("/api/data-sources/inventory")

    @app.get("/api/admin/market-publication")
    def market_publication() -> dict:
        return proxy("/api/internal/market-publication")

    @app.get("/api/admin/data/{view}")
    def internal_data(view: Literal["silver", "runs", "partitions", "quarantine", "package", "storage", "quality", "freshness"]) -> dict:
        return proxy(f"/api/data/{view}?page_size=100")

    @app.get("/api/admin/diagnostics")
    def diagnostics() -> dict:
        return {"python": platform.python_version(), "platform": platform.platform(), "clientBuilt": (Path(__file__).with_name("web_dist") / "index.html").is_file(), "dataRootExists": supervisor.data_root.is_dir(), "service": supervisor.snapshot(), "checkedAt": datetime.now(timezone.utc).isoformat(), "employeeAccounts": "本机阶段；员工多账号与角色授权尚未部署"}

    @app.get("/api/admin/logs")
    def logs(source: Literal["backend", "console", "terminal"] = "backend") -> dict:
        return {"source": source, "lines": supervisor.tail(source)}

    @app.put("/api/admin/settings")
    def settings(request: SettingsRequest) -> dict:
        supervisor.save_settings(ConsoleSettings(**request.model_dump()))
        return {"status": "SAVED"}

    @app.put("/api/admin/startup")
    def startup(request: StartupRequest) -> dict:
        try:
            supervisor.set_startup(request.enabled)
        except (RuntimeError, OSError) as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "SAVED"}

    @app.post("/api/admin/terminals/{terminal}/{action}", status_code=202)
    def terminal_action(terminal: Literal["financial", "futures"], action: Literal["launch", "download"]) -> dict:
        try:
            supervisor.run_terminal(terminal, action)
        except (RuntimeError, ValueError) as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "ACCEPTED"}

    @app.post("/api/admin/shutdown", status_code=202)
    def close_console() -> dict:
        with restore_lock:
            if any(item["status"] in {"QUEUED", "RUNNING"} for item in restore_jobs.values()):
                raise HTTPException(409, "个人资料恢复尚未结束")
        if supervisor.action or supervisor.terminal_busy:
            raise HTTPException(409, "请先等待当前服务或通达信操作结束")
        if shutdown is None:
            raise HTTPException(409, "当前运行方式不支持关闭管理服务")
        threading.Timer(.5, shutdown).start()
        return {"status": "ACCEPTED"}

    @app.get("/api/admin/research/status")
    def research_status() -> dict:
        from .research_corpus import corpus_status
        from .research_model_runtime import all_model_status
        from .research_publication import publication_versions

        with research_lock:
            jobs = [dict(value) for value in research_jobs.values()]
        checkpoint = supervisor.data_root / "research" / "private" / "full-extraction.json"
        try:
            background = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            background = None
        if isinstance(background, dict) and background.get("phase") == "RUNNING" and not _research_pid_alive(background.get("pid")):
            background = {**background, "phase": "INTERRUPTED", "detail": "后台进程已退出；逐页断点保留，可继续"}
        return {"corpus": corpus_status(supervisor.data_root), "jobs": jobs[-20:],
                "publications": publication_versions(supervisor.data_root), "background": background,
                "models": all_model_status(supervisor.data_root)}

    @app.post("/api/admin/research/models/{kind}/{action}")
    def research_model_control(kind: Literal["text", "visual"], action: Literal["start", "stop"]) -> dict:
        from .research_model_runtime import start_model, stop_model

        try:
            return start_model(supervisor.data_root, kind) if action == "start" else stop_model(supervisor.data_root, kind)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/admin/research/background/pause")
    def pause_background_research() -> dict:
        flag = supervisor.data_root / "research" / "private" / "pause.flag"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("pause after current page", encoding="utf-8")
        return {"status": "PAUSING"}

    @app.post("/api/admin/research/background/resume", status_code=202)
    def resume_background_research() -> dict:
        checkpoint = supervisor.data_root / "research" / "private" / "full-extraction.json"
        try:
            previous = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if _research_pid_alive(previous.get("pid")):
            raise HTTPException(409, "全量提取进程仍在运行")
        interrupted = previous.get("phase") == "RUNNING"
        if previous.get("phase") not in {"PAUSED", "REVIEW_REQUIRED"} and not interrupted:
            raise HTTPException(409, "只有已暂停或待复核的全量提取可以继续")
        flag = supervisor.data_root / "research" / "private" / "pause.flag"
        flag.unlink(missing_ok=True)
        executable = supervisor.project / "desktop" / ".venv" / "Scripts" / "python.exe"
        if not executable.is_file():
            raise HTTPException(409, "未找到项目 Python 环境")
        script = supervisor.project / "scripts" / "research_full.py"
        logs = supervisor.data_root / "research" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                 getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)) if os.name == "nt" else 0
        with (logs / "full-extraction.stdout.log").open("ab") as stdout, (
            logs / "full-extraction.stderr.log").open("ab") as stderr:
            command = [str(executable), str(script), "--data-root", str(supervisor.data_root)]
            if previous.get("phase") == "REVIEW_REQUIRED":
                command.append("--retry-errors")
            process = subprocess.Popen(command,
                                       cwd=supervisor.project, stdout=stdout, stderr=stderr,
                                       creationflags=flags)
        (supervisor.data_root / "research" / "private" / "full-extraction.pid").write_text(
            str(process.pid), encoding="ascii")
        return {"status": "RUNNING", "pid": process.pid}

    @app.post("/api/admin/research/jobs", status_code=202)
    def research_job(request: ResearchJobRequest) -> dict:
        from .research_corpus import process_pages, propose_from_local_model, scan_reports
        from .research_publication import publish_research

        with research_lock:
            if any(value["status"] == "RUNNING" for value in research_jobs.values()):
                raise HTTPException(409, "已有研报作业正在运行")
            identifier = secrets.token_hex(16)
            cancellation = threading.Event()
            research_cancellations[identifier] = cancellation
            research_jobs[identifier] = {"id": identifier, "action": request.action, "status": "RUNNING",
                                         "createdAt": datetime.now(timezone.utc).isoformat(), "progress": {}}

        def progress(value: dict) -> None:
            with research_lock:
                research_jobs[identifier]["progress"] = value

        def work() -> None:
            try:
                if request.action == "scan":
                    report_roots = (Path.home() / "Documents" / "研报",
                                    Path.home() / "Documents" / "行业产业链研报")
                    result = scan_reports(supervisor.data_root, report_roots, on_progress=progress,
                                          should_stop=cancellation.is_set)
                elif request.action == "process":
                    result = process_pages(supervisor.data_root, max_pages=request.maxPages,
                                           retry_problem_pages=request.retryProblemPages,
                                           on_progress=progress, should_stop=cancellation.is_set)
                elif request.action == "propose":
                    if not request.modelUrl or not request.modelName or not request.extractionVersion:
                        raise ValueError("请提供本机模型地址、名称和抽取版本")
                    result = propose_from_local_model(
                        supervisor.data_root, url=request.modelUrl, model=request.modelName,
                        extraction_version=request.extractionVersion, max_pages=request.maxPages,
                        on_progress=progress, should_stop=cancellation.is_set,
                    )
                elif request.action == "visual":
                    from .research_visual import propose_visual_page

                    if not all((request.sha256, request.page, request.modelUrl,
                                request.modelName, request.extractionVersion)):
                        raise ValueError("请先选原页并填写视觉模型地址、名称和版本")
                    result = propose_visual_page(
                        supervisor.data_root, request.sha256, request.page,
                        url=request.modelUrl, model=request.modelName,
                        label=request.extractionVersion,
                    )
                elif request.action == "visual-queue":
                    from .research_corpus import backfill_visual_queue

                    result = backfill_visual_queue(supervisor.data_root)
                elif request.action == "visual-batch":
                    from .research_visual import propose_visual_batch

                    if not all((request.modelUrl, request.modelName, request.extractionVersion)):
                        raise ValueError("请提供本机视觉模型地址、名称和版本")
                    result = propose_visual_batch(
                        supervisor.data_root, url=request.modelUrl, model=request.modelName,
                        label=request.extractionVersion, max_pages=min(request.maxPages, 200),
                        on_progress=progress, should_stop=cancellation.is_set,
                    )
                else:
                    result = publish_research(supervisor.data_root)
            except (OSError, ValueError, RuntimeError, ImportError) as error:
                with research_lock:
                    research_jobs[identifier].update(status="FAILED", detail=f"{type(error).__name__}: {error}",
                                                     completedAt=datetime.now(timezone.utc).isoformat())
            else:
                with research_lock:
                    research_jobs[identifier].update(status="CANCELLED" if result.get("cancelled") else "PASS", result=result,
                                                     completedAt=datetime.now(timezone.utc).isoformat())

        threading.Thread(target=work, name=f"marketlistener-research-{request.action}", daemon=True).start()
        return {"id": identifier, "status": "RUNNING"}

    @app.post("/api/admin/research/jobs/{identifier}/cancel")
    def cancel_research_job(identifier: str) -> dict:
        with research_lock:
            event = research_cancellations.get(identifier)
            job = research_jobs.get(identifier)
            if event is None or job is None:
                raise HTTPException(404, "研报作业不存在")
            if job["status"] != "RUNNING":
                raise HTTPException(409, "研报作业已结束")
            event.set()
        return {"id": identifier, "status": "CANCELLING"}

    @app.get("/api/admin/research/documents")
    def research_documents(limit: int = 100, offset: int = 0) -> dict:
        from .research_corpus import list_documents

        return list_documents(supervisor.data_root, limit=limit, offset=offset)

    @app.get("/api/admin/research/errors")
    def research_errors(limit: int = 100) -> dict:
        from .research_corpus import list_source_errors

        return list_source_errors(supervisor.data_root, limit=limit)

    @app.get("/api/admin/research/pages/{sha256}/{page}")
    def research_page(sha256: str, page: int) -> dict:
        from .research_corpus import private_page

        value = private_page(supervisor.data_root, sha256, page)
        if value is None:
            raise HTTPException(404, "研报页面尚未处理")
        return value

    @app.get("/api/admin/research/pages/{sha256}/{page}/image")
    def research_page_image(sha256: str, page: int) -> Response:
        from .research_corpus import private_page_png

        try:
            image = private_page_png(supervisor.data_root, sha256, page)
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(404, "PDF 页面不可预览") from error
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/research/pages/{sha256}/{page}/confirm-blank")
    def research_blank(sha256: str, page: int) -> dict:
        from .research_corpus import confirm_blank

        try:
            confirm_blank(supervisor.data_root, sha256, page)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "CONFIRMED_BLANK"}

    @app.post("/api/admin/research/pages/{sha256}/{page}/resolve-ocr")
    def research_resolve_ocr(sha256: str, page: int, request: ResearchOCRResolutionRequest) -> dict:
        from .research_corpus import resolve_ocr_disagreement

        try:
            resolve_ocr_disagreement(supervisor.data_root, sha256, page, request.engine,
                                     request.reviewer, request.note)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "OCR_OK", "engine": request.engine}

    @app.get("/api/admin/research/candidates")
    def research_candidates(status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING",
                            limit: int = 100) -> dict:
        from .research_corpus import list_candidates

        return list_candidates(supervisor.data_root, status=status, limit=limit)

    @app.post("/api/admin/research/candidates")
    def research_add_candidate(request: ResearchCandidateRequest) -> dict:
        from .research_corpus import add_candidate

        try:
            identifier = add_candidate(supervisor.data_root, request.sha256, request.page,
                                       request.kind, request.payload, request.evidenceText)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"id": identifier, "status": "PENDING"}

    @app.post("/api/admin/research/candidates/{identifier}/review")
    def research_review(identifier: str, request: ResearchReviewRequest) -> dict:
        from .research_corpus import review_candidate

        try:
            review_candidate(supervisor.data_root, identifier, request.status, request.reviewer, request.note,
                             current_verified=request.currentVerified)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"id": identifier, "status": request.status}

    @app.post("/api/admin/research/visual/propose")
    def research_visual_propose(request: ResearchVisualRequest) -> dict:
        from .research_visual import propose_visual_page

        try:
            return propose_visual_page(supervisor.data_root, request.sha256, request.page,
                                       url=request.modelUrl, model=request.modelName, label=request.label)
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/admin/research/visual-queue")
    def research_visual_queue(status: Literal["PENDING", "PROPOSED", "MODEL_EMPTY", "ERROR", "REVIEWED"] = "PENDING",
                              limit: int = 100) -> dict:
        from .research_corpus import list_visual_queue

        return list_visual_queue(supervisor.data_root, status=status, limit=limit)

    @app.post("/api/admin/research/visual-queue/{sha256}/{page}/review")
    def research_visual_page_review(sha256: str, page: int, request: ResearchReviewRequest) -> dict:
        from .research_corpus import review_visual_page

        if request.status != "APPROVED":
            raise HTTPException(422, "页级视觉核验只能确认已查看；单项观察可另行拒绝")
        try:
            review_visual_page(supervisor.data_root, sha256, page, request.reviewer, request.note)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "REVIEWED"}

    @app.get("/api/admin/research/visual")
    def research_visual_list(status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING",
                             limit: int = 100) -> dict:
        from .research_visual import list_visual_observations

        return list_visual_observations(supervisor.data_root, status=status, limit=limit)

    @app.post("/api/admin/research/visual/{identifier}/review")
    def research_visual_review(identifier: str, request: ResearchReviewRequest) -> dict:
        from .research_visual import review_visual_observation

        try:
            review_visual_observation(supervisor.data_root, identifier, request.status,
                                      request.reviewer, request.note)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"id": identifier, "status": request.status}

    @app.post("/api/admin/research/visual/{identifier}/candidate")
    def research_visual_candidate(identifier: str, request: ResearchVisualPromotionRequest) -> dict:
        from .research_corpus import add_visual_candidate

        try:
            candidate_id = add_visual_candidate(supervisor.data_root, identifier, request.kind,
                                                request.payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"id": candidate_id, "status": "PENDING"}

    @app.post("/api/admin/research/rollback")
    def research_rollback(request: ResearchRollbackRequest) -> dict:
        from .research_publication import rollback_research

        try:
            return rollback_research(supervisor.data_root, request.version)
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(404, str(error)) from error

    app.mount("/assets", StaticFiles(directory=assets), name="admin-assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(assets / "index.html")

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MarketListener本机内部管理台")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--backend-port", type=int, default=8765)
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--show-login-code", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.backend_port <= 65535 or args.port == args.backend_port:
        parser.error("管理端口与客户端口必须不同且有效")
    if args.show_login_code:
        print(f"管理台已取消浏览器口令，服务启动后直接打开 http://127.0.0.1:{args.port}/")
        return 0
    data_root = args.data_root if args.data_root.is_absolute() else args.project_root / args.data_root
    supervisor = ServiceSupervisor(args.project_root, data_root, backend_port=args.backend_port)
    server: uvicorn.Server | None = None

    def shutdown() -> None:
        if server is not None:
            server.should_exit = True

    app = create_admin_app(supervisor, port=args.port, auto_start=not args.no_auto_start, shutdown=shutdown,
                           control_token=load_control_token("admin", args.port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning", proxy_headers=False))
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
