"""Run only while the customer backend is stopped; never pass keys as flags."""
import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import time

from market_monitor.dce_authorized_backfill import DceClient, backfill, now, save_json
from market_monitor.futures_official_backfill import _process_file_lock
from market_monitor.dce_parallel_backfill import DceClientPool


def main() -> int:
    parser = argparse.ArgumentParser(description="离线回补大商所授权成交/多空持仓排名")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2025-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--max-contracts", type=int, help="小规模采样的事实提交合约上限")
    parser.add_argument("--min-interval-seconds", type=float, default=2)
    parser.add_argument("--adaptive-rate", action="store_true",
                        help="加速采样遇501/429后冷却一次并降至2秒；再次失败停批")
    parser.add_argument("--initial-cooldown-seconds", type=float, default=0,
                        help="持有本任务锁等待指定冷却时间，再执行一次断点批次")
    parser.add_argument("--credentials-stdin", action="store_true")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1,
                        help="授权账号数量；仅由主线程写库")
    parser.add_argument("--requests-per-window", type=int, choices=(1, 2, 3),
                        help="每账号每窗口请求数及并发数")
    parser.add_argument("--request-window-seconds", type=float, default=1)
    parser.add_argument("--no-daily-limit", action="store_true", help="明确使用DCE每账号策略，不设本地日上限")
    args = parser.parse_args()
    if args.initial_cooldown_seconds < 0:
        parser.error("冷却时间不能为负")
    if args.workers == 2 and args.adaptive_rate:
        parser.error("双账号采样保留各账号限速；不在限流后切换账号")
    if bool(args.requests_per_window) != args.no_daily_limit:
        parser.error("每账号窗口策略需同时指定requests-per-window和no-daily-limit")
    if args.requests_per_window and (args.request_window_seconds <= 0 or args.adaptive_rate):
        parser.error("窗口时间必须为正；窗口策略使用账号独立冷却")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            parser.error("先通过管理台停止8765；不自动终止未知服务")
    if args.credentials_stdin:
        credentials = json.loads(sys.stdin.readline())
        accounts = credentials.get("accounts", [credentials])
    else:
        credentials = {}
        accounts = [{"api_key": os.environ.get("DCE_API_KEY") or getpass.getpass("DCE API Key: "),
                     "secret": os.environ.get("DCE_API_SECRET") or getpass.getpass("DCE API Secret: ")}]
        if args.workers == 2:
            accounts.append({"api_key": getpass.getpass("DCE second API Key: "),
                             "secret": getpass.getpass("DCE second API Secret: ")})
    if len(accounts) != args.workers:
        parser.error("输入账号数量与workers不一致")
    if args.workers == 2 and accounts[0]["api_key"] == accounts[1]["api_key"]:
        parser.error("双通道需要两个不同的账号")
    lanes = args.requests_per_window or 1
    policy = (args.request_window_seconds, lanes) if args.requests_per_window else None
    clients = [DceClient(args.data_root, account["api_key"], account["secret"], args.min_interval_seconds,
                         adaptive_rate=args.adaptive_rate, account_policy=policy)
               for account in accounts for _ in range(lanes)]
    for account in accounts:
        account.clear()
    credentials.clear()
    client = clients[0] if len(clients) == 1 else DceClientPool(args.data_root, clients, lanes_per_account=lanes)
    try:
        lock = args.data_root / "state" / "dce_authorized_rank_writer.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with _process_file_lock(lock):
            if args.initial_cooldown_seconds:
                progress_path = args.data_root / "state" / "dce_authorized_ranks" / "progress.json"
                progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
                progress.update(status="COOLDOWN_WAIT", pid=os.getpid(), updatedAt=now(),
                                initialCooldownSeconds=args.initial_cooldown_seconds,
                                minIntervalSeconds=client.interval, workerCount=args.workers)
                save_json(progress_path, progress)
                print(json.dumps({"event": "DCE_COOLDOWN_WAIT", "seconds": args.initial_cooldown_seconds,
                                  "updatedAt": now()}), flush=True)
                time.sleep(args.initial_cooldown_seconds)
            result = backfill(args.data_root, client, args.start_day, args.end_day, max_days=args.max_days,
                              max_contracts=args.max_contracts)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["status"] in {"PASS", "BATCH_LIMIT", "BUDGET_PAUSED"} else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
