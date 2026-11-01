"""Start and inspect pinned, loopback-only local research model servers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from .research_corpus import _digest

_SPECS = {
    "text": {"port": 18081, "alias": "qwen-local",
             "file": "qwen3-4b-instruct-2507-q8_0.gguf",
             "sha": "ae916ede1c010a26955ee8ae2e908bf8815a3f135ec860439ab924701c69d5f1"},
    "visual": {"port": 18082, "alias": "qwen-visual",
               "file": "Qwen3VL-4B-Instruct-Q4_K_M.gguf",
               "sha": "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a",
               "projector": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf",
               "projectorSha": "30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d"},
}


def _spec(kind: str) -> dict:
    try:
        return _SPECS[kind]
    except KeyError as error:
        raise ValueError("Unknown local model kind") from error


def model_status(data_root: Path, kind: str) -> dict[str, object]:
    spec = _spec(kind)
    url = f"http://127.0.0.1:{spec['port']}"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(url + "/health"), timeout=2) as response:
            if json.load(response).get("status") != "ok":
                return {"kind": kind, "status": "LOADING", "url": url}
        with opener.open(Request(url + "/props"), timeout=2) as response:
            props = json.load(response)
    except (OSError, URLError, ValueError):
        return {"kind": kind, "status": "STOPPED", "url": url}
    expected_path = (Path(data_root) / "research" / "models" / spec["file"]).resolve()
    if props.get("model_alias") != spec["alias"] or Path(str(props.get("model_path") or "")).resolve() != expected_path:
        return {"kind": kind, "status": "FOREIGN_PORT", "url": url}
    return {"kind": kind, "status": "RUNNING", "url": url,
            "alias": spec["alias"], "runtimeBuild": props.get("build_info")}


def all_model_status(data_root: Path) -> dict[str, object]:
    return {kind: model_status(data_root, kind) for kind in _SPECS}


def start_model(data_root: Path, kind: str) -> dict[str, object]:
    spec = _spec(kind)
    current = model_status(data_root, kind)
    if current["status"] == "RUNNING":
        return current
    if current["status"] != "STOPPED":
        raise ValueError("The local model port is occupied")
    for other in _SPECS:
        if other != kind and model_status(data_root, other)["status"] == "RUNNING":
            raise ValueError("请先停止另一个本机模型，避免显存竞争")
    root = Path(data_root).resolve() / "research"
    executable = root / "runtime" / "llama-b11734-vulkan" / (
        "llama-server.exe" if os.name == "nt" else "llama-server")
    model_path = root / "models" / spec["file"]
    if not executable.is_file() or not model_path.is_file() or _digest(model_path) != spec["sha"]:
        raise ValueError("本机模型或 llama.cpp 运行时缺失或校验失败")
    arguments = [str(executable), "-m", str(model_path), "--host", "127.0.0.1",
                 "--port", str(spec["port"]), "--alias", spec["alias"], "-c", "8192",
                 "-ngl", "99", "--no-webui"]
    if kind == "visual":
        projector = root / "models" / spec["projector"]
        if not projector.is_file() or _digest(projector) != spec["projectorSha"]:
            raise ValueError("视觉投影器缺失或校验失败")
        arguments.extend(["--mmproj", str(projector), "--image-min-tokens", "1024"])
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
             getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)) if os.name == "nt" else 0
    with (logs / f"llama-{kind}.stdout.log").open("ab") as stdout, (
        logs / f"llama-{kind}.stderr.log").open("ab") as stderr:
        process = subprocess.Popen(arguments, cwd=executable.parent, stdout=stdout, stderr=stderr,
                                   creationflags=flags)
    for _ in range(60):
        if process.poll() is not None:
            raise ValueError("本机模型启动失败；请查看内部日志")
        current = model_status(data_root, kind)
        if current["status"] == "RUNNING":
            (root / "runtime" / f"llama-{kind}.pid").write_text(str(process.pid), encoding="ascii")
            return current
        time.sleep(.5)
    process.terminate()
    raise ValueError("本机模型在 30 秒内未就绪")


def stop_model(data_root: Path, kind: str) -> dict[str, object]:
    spec = _spec(kind)
    current = model_status(data_root, kind)
    if current["status"] == "STOPPED":
        return current
    if current["status"] != "RUNNING":
        raise ValueError("不能停止未识别的端口服务")
    if os.name != "nt":
        raise ValueError("当前仅支持 Windows 本机模型管理")
    command = ["powershell", "-NoProfile", "-Command",
               f"(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort {spec['port']} "
               "-State Listen -ErrorAction Stop).OwningProcess"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
    pid = int(result.stdout.strip())
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if model_status(data_root, kind)["status"] == "STOPPED":
            return model_status(data_root, kind)
        time.sleep(.25)
    raise ValueError("模型停止请求已发出，但端口仍在监听")
