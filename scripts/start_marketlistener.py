"""One-click web entry; no desktop GUI or packaged launcher executable."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import html
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import webbrowser


PROJECT = Path(__file__).resolve().parent.parent
ADMIN_URL = "http://127.0.0.1:8766"
CLIENT_URL = "http://127.0.0.1:8765"
_opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
_WATCHDOG_LOG = "data_control/logs/web-startup-watchdog.jsonl"


def record_watchdog_event(project: Path, status: str, phase: str,
                          error_type: str | None = None) -> None:
    """Record only bounded local startup outcomes, never control credentials."""

    if status not in {"ATTEMPT", "PASS", "FAILED"} or phase not in {
        "LOCK", "ADMIN", "SESSION", "CLIENT", "READY",
    }:
        raise ValueError("invalid watchdog event")
    path = project / _WATCHDOG_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "status": status, "phase": phase,
                 "adminReady": bool(read_json(ADMIN_URL + "/api/admin/ready", timeout=.5)),
                 "clientReady": bool(read_json(CLIENT_URL + "/api/ready", timeout=.5))}
        if error_type:
            entry["errorType"] = error_type[:64]
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        # This low-volume diagnostic should stay small even after years of logons.
        if path.stat().st_size > 131_072:
            with path.open("rb") as source:
                source.seek(max(0, path.stat().st_size - 65_536))
                tail = source.read()
            if not tail.startswith(b"{"):
                tail = tail.partition(b"\n")[2]
            temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(tail)
            os.replace(temporary, path)
    except (OSError, ValueError):
        # A diagnostic failure cannot suppress the actual service startup.
        return


def read_json(url: str, *, timeout: float = 1) -> dict | None:
    try:
        with _opener.open(url, timeout=timeout) as response:
            return json.load(response)
    except (HTTPError, URLError, OSError, ValueError):
        return None


def issue_bootstrap(url: str, audience: str) -> str:
    from market_monitor.local_session import CONTROL_HEADER, load_control_token

    port = 8766 if audience == "admin" else 8765
    token = load_control_token(audience, port, create=False)
    request = Request(url + "/api/local-session/issue", data=b"{}", method="POST",
                      headers={"Content-Type": "application/json", CONTROL_HEADER: token})
    try:
        with _opener.open(request, timeout=3) as response:
            value = json.load(response)
        if value.get("audience") != audience or not isinstance(value.get("bootstrap"), str):
            raise ValueError("invalid bootstrap response")
        return value["bootstrap"]
    except (HTTPError, URLError, OSError, ValueError):
        raise RuntimeError("本机安全会话无法建立；请重新加载本机管理与客户服务") from None


def establish_admin_session() -> None:
    bootstrap = issue_bootstrap(ADMIN_URL, "admin")
    request = Request(ADMIN_URL + "/api/local-session/bootstrap",
                      data=json.dumps({"bootstrap": bootstrap}).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Origin": ADMIN_URL})
    with _opener.open(request, timeout=3):
        pass


@contextmanager
def launch_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 30
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个启动请求尚未完成，请稍后重试") from None
                time.sleep(.2)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def ensure_admin(project: Path, *, no_auto_start: bool = False) -> None:
    existing = read_json(ADMIN_URL + "/api/admin/ready")
    if existing and existing.get("service") == "marketlistener-internal-console":
        return
    with socket.socket() as probe:
        probe.settimeout(.4)
        if probe.connect_ex(("127.0.0.1", 8766)) == 0:
            raise RuntimeError("管理端口8766被其他程序占用")
    python = project / "desktop/.venv/Scripts/pythonw.exe" if os.name == "nt" else project / "desktop/.venv/bin/python"
    if not python.is_file():
        raise RuntimeError("项目Python环境尚未安装")
    if not (project / "desktop/src/market_monitor/admin_web_dist/index.html").is_file():
        node = shutil.which("node")
        if not node:
            raise RuntimeError("请先安装项目Node环境并构建desktop/admin-web")
        built = subprocess.run([node, str(project / "desktop/admin-web/build.mjs")], cwd=project, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, timeout=20, check=False)
        if built.returncode != 0:
            raise RuntimeError("内部管理台前端构建失败，请在desktop/admin-web运行npm run build")
    command = [str(python), "-m", "market_monitor.web_admin", "--project-root", str(project), "--data-root", str(project / "data_control")]
    if no_auto_start:
        command.append("--no-auto-start")
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(project / "desktop/src"), PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    log = project / "data_control/logs/web-admin.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        process = subprocess.Popen(command, cwd=project, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        existing = read_json(ADMIN_URL + "/api/admin/ready")
        if existing and existing.get("service") == "marketlistener-internal-console":
            return
        if process.poll() is not None:
            raise RuntimeError("管理服务启动失败，请查看data_control/logs/web-admin.log")
        time.sleep(.3)
    raise RuntimeError("管理服务未能及时启动，请查看后台日志")


def ensure_client() -> None:
    state = read_json(ADMIN_URL + "/api/admin/state", timeout=2)
    if not state:
        raise RuntimeError("无法读取管理服务状态")
    if not state.get("ready") and not state.get("action"):
        request = Request(ADMIN_URL + "/api/admin/service/start", data=b"{}", method="POST", headers={"Content-Type": "application/json", "Origin": ADMIN_URL, "X-MarketListener-Console-Token": state["csrfToken"]})
        with _opener.open(request, timeout=3):
            pass
    deadline = time.monotonic() + 95
    while time.monotonic() < deadline:
        result = read_json(CLIENT_URL + "/api/ready")
        if result and result.get("status") == "READY":
            return
        state = read_json(ADMIN_URL + "/api/admin/state", timeout=2)
        if state and state.get("error") and not state.get("action"):
            raise RuntimeError(state["error"])
        time.sleep(.5)
    raise RuntimeError("客户服务尚未就绪，请从内部管理台查看启动状态")


def show_error(project: Path, message: str) -> None:
    path = project / "data_control/state/launch-error.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<!doctype html><meta charset="utf-8"><title>MarketListener启动结果</title><main style="max-width:760px;margin:60px auto;font:16px/1.8 system-ui"><h1>MarketListener 启动未完成</h1><p>' + html.escape(message) + '</p><p>查看本机 data_control/logs/web-admin.log 获取详细结果。</p></main>', encoding="utf-8")
    webbrowser.open_new_tab(path.as_uri())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="快捷启动MarketListener网页产品或内部管理台")
    parser.add_argument("--target", choices=("admin", "client", "none"), default="client")
    parser.add_argument("--no-auto-start", action="store_true", help="仅启动管理服务，供界面验证使用")
    parser.add_argument("--startup-watchdog", action="store_true",
                        help="供本机登录/解锁计划任务核对后台就绪并记录启动结果")
    args = parser.parse_args(argv)
    if args.startup_watchdog and (args.target != "none" or args.no_auto_start):
        parser.error("--startup-watchdog仅适用于--target none")
    phase = "LOCK"
    if args.startup_watchdog:
        record_watchdog_event(PROJECT, "ATTEMPT", phase)
    try:
        with launch_lock(PROJECT / "data_control/state/web-launch.lock"):
            phase = "ADMIN"
            ensure_admin(PROJECT, no_auto_start=args.no_auto_start)
            phase = "SESSION"
            establish_admin_session()
            if args.target == "client" or args.startup_watchdog:
                if args.startup_watchdog:
                    settings = read_json(ADMIN_URL + "/api/admin/state", timeout=2)
                    if not settings:
                        raise RuntimeError("无法读取管理服务自动启动设置")
                    should_start_client = settings.get("settings", {}).get("auto_start") is True
                else:
                    should_start_client = True
            else:
                should_start_client = False
            if should_start_client:
                phase = "CLIENT"
                if not (PROJECT / "desktop/src/market_monitor/web_dist/index.html").is_file():
                    raise RuntimeError("客户前端尚未构建，请在desktop/web运行npm run build")
                ensure_client()
        if args.target != "none":
            url = ADMIN_URL if args.target == "admin" else CLIENT_URL
            bootstrap = issue_bootstrap(url, "admin" if args.target == "admin" else "product")
            webbrowser.open_new(url + "/#ml-bootstrap=" + bootstrap)
        if args.startup_watchdog:
            record_watchdog_event(PROJECT, "PASS", "READY")
        return 0
    except (RuntimeError, OSError, HTTPError, URLError) as error:
        if args.startup_watchdog:
            record_watchdog_event(PROJECT, "FAILED", phase, type(error).__name__)
        if args.target != "none":
            show_error(PROJECT, str(error))
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
