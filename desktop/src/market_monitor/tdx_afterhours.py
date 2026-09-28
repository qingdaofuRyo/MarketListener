"""Bounded official-TQ download probe, not a full-market scheduled downloader.

Never reads account configuration or imports raw files into authoritative Silver.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from typing import Any

BEIJING = timezone(timedelta(hours=8))
ROOTS = {"financial": Path(r"C:\tongdaxin"), "futures": Path(r"C:\new_tdxqh")}
SUFFIXES = {
    "SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "31", "HI": "27",
    "CSI": "62", "CNI": "102", "HG": "38", "CFF": "47", "SHF": "30",
    "INE": "30", "DCE": "29", "CZC": "28", "GFE": "66",
}


def latest_slot(now: datetime) -> str:
    """One logical due slot at 23:00 Beijing time, including missed days."""
    if now.tzinfo is None:
        raise ValueError("调度时间必须包含时区")
    local = now.astimezone(BEIJING)
    slot = local.replace(hour=23, minute=0, second=0, microsecond=0)
    if local < slot:
        slot -= timedelta(days=1)
    return slot.isoformat()


def terminal_preflight(root: Path) -> dict[str, Any]:
    required = (root / "tdxw.exe", root / "PYPlugins/user/tqcenter.py",
                root / "PYPlugins/TPythClient.dll", root / "PYPlugins/tdxrpcx64.dll")
    missing = [str(p) for p in required if not p.is_file()]
    return {"status": "BLOCKED" if missing else "READY_FOR_PROBE", "missing": missing}


def symbol_file(root: Path, symbol: str, period: str) -> Path:
    if period not in ("1d", "5m"):
        raise ValueError("仅允许日线和5分钟线")
    if not re.fullmatch(r"[A-Z0-9_-]+\.[A-Z0-9]+", symbol):
        raise ValueError("不安全或未知的品种代码")
    code, suffix = symbol.rsplit(".", 1)
    prefix = SUFFIXES.get(suffix)
    if prefix is None:
        raise ValueError(f"尚未验证此市场的文件映射：{suffix}")
    folder, extension = ("lday", "day") if period == "1d" else ("fzline", "lc5")
    if prefix in ("sh", "sz", "bj"):
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("沪深京代码必须为六位数字")
        return root / "vipdoc" / prefix / folder / f"{prefix}{code}.{extension}"
    return root / "vipdoc/ds" / folder / f"{prefix}#{code}.{extension}"


def file_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "MISSING"}
    before = path.stat()
    if before.st_size < 32 or before.st_size % 32:
        return {"status": "INVALID_RECORD_LENGTH", "bytes": before.st_size}
    with path.open("rb") as stream:
        stream.seek(max(0, before.st_size - 32 * 96))
        tail = stream.read()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return {"status": "WRITING"}
    record = tail[-32:]
    if path.suffix == ".day":
        day = str(struct.unpack_from("<I", record)[0])
        stamp = datetime.strptime(day, "%Y%m%d").isoformat()
    else:
        packed, minute = struct.unpack_from("<HH", record)
        year, remainder = packed // 2048 + 2004, packed % 2048
        month, day = divmod(remainder, 100)
        if minute >= 24 * 60:
            raise ValueError("分钟线包含无效时间")
        stamp = datetime(year, month, day, minute // 60, minute % 60).isoformat()
    return {"status": "READABLE", "records": before.st_size // 32,
            "lastRawTimestamp": stamp, "tailSha256": hashlib.sha256(tail).hexdigest(),
            "completeThrough": None, "completeness": "NOT_VERIFIED"}


def refresh_result(value: Any) -> dict[str, Any]:
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, dict) or str(result.get("ErrorId")) != "0":
        raise RuntimeError("官方接口未返回成功；不推进下载检查点")
    return {"ErrorId": "0"}


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def process_lock(path: Path):
    """OS releases the lock on crash; do not delete/recreate the lock inode."""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def probe(root: Path, symbols: list[str], period: str, download: bool) -> dict[str, Any]:
    """Called in a bounded subprocess; DLL calls can otherwise block forever."""
    paths = [symbol_file(root, s, period) for s in symbols]
    preflight = terminal_preflight(root)
    if preflight["status"] == "BLOCKED":
        return {**preflight, "download": "NOT_RUN"}
    before = [file_evidence(p) for p in paths]
    with ExitStack() as stack:
        stack.enter_context(os.add_dll_directory(str(root / "PYPlugins")))
        spec = importlib.util.spec_from_file_location("official_tq_afterhours", root / "PYPlugins/user/tqcenter.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载官方接口")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tq = module.tq
        try:
            # Explicit DLL path keeps the two same-named TdxW processes isolated.
            tq.initialize(str(Path(__file__).resolve()), dll_path=str(root / "PYPlugins/TPythClient.dll"))
            if download:
                refresh_result(tq.refresh_kline(stock_list=symbols, period=period))
        finally:
            tq.close()
    after = [file_evidence(p) for p in paths]
    readable = all(x["status"] == "READABLE" for x in after)
    return {"status": "API_OK" if not download else "DOWNLOADED_UNVERIFIED" if readable else "FAILED",
            "download": "API_OK" if download else "NOT_RUN", "period": period,
            "symbols": symbols, "before": before, "after": after,
            "import": "NOT_RUN", "completeThrough": None}


def run_probe(args: argparse.Namespace, root: Path, symbols: list[str], period: str) -> dict[str, Any]:
    output = args.state_dir / "worker-result.json"
    output.unlink(missing_ok=True)
    command = [sys.executable, "-m", "market_monitor.tdx_afterhours", "worker", "--root", str(root),
               "--period", period, "--symbols", *symbols, "--state-dir", str(args.state_dir)]
    if args.download:
        command.append("--download")
    try:
        # Do not store arbitrary vendor stdout: it may contain account details.
        result = subprocess.run(command, timeout=660, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "download": "UNKNOWN", "import": "NOT_RUN"}
    if not output.is_file():
        return {"status": "FAILED", "workerExitCode": result.returncode}
    return json.loads(output.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通达信官方接口盘后下载探针（非全市场自动任务）")
    parser.add_argument("command", choices=("preflight", "probe", "worker"))
    parser.add_argument("--terminal", choices=ROOTS, default="futures")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--symbols", nargs="+", default=[])
    parser.add_argument("--period", choices=("1d", "5m"), default="1d")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=Path("data_control/state/tdx_afterhours"))
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.resolve()
    root = (args.root or ROOTS[args.terminal]).resolve()
    if args.command == "preflight":
        report = {key: terminal_preflight(value) for key, value in ROOTS.items()}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if any(x["status"] == "BLOCKED" for x in report.values()) else 0
    if not args.symbols or len(args.symbols) > 20 or len(set(args.symbols)) != len(args.symbols):
        parser.error("探针必须显式指定1～20个不重复品种")
    for symbol in args.symbols:
        try:
            symbol_file(root, symbol, args.period)
        except ValueError as error:
            parser.error(str(error))
    if args.command == "worker":
        try:
            report = probe(root, args.symbols, args.period, args.download)
        except Exception as error:
            report = {"status": "FAILED", "errorType": type(error).__name__,
                      "reason": "连接或下载失败；请检查客户端登录、接口版本与数据权限", "import": "NOT_RUN"}
        save_report(args.state_dir / "worker-result.json", report)
        return 0 if report["status"] in ("API_OK", "DOWNLOADED_UNVERIFIED") else 2
    with process_lock(args.state_dir / "run.lock"):
        report = run_probe(args, root, args.symbols, args.period)
        report["observedAt"] = datetime.now(BEIJING).isoformat()
        save_report(args.state_dir / "latest.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in ("API_OK", "DOWNLOADED_UNVERIFIED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
