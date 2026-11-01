"""Capture GFEX official raw rankings without opening the market database."""
import argparse
import getpass
import json
from pathlib import Path
import sys

from market_monitor.futures_calendar import load_futures_trading_calendar
from market_monitor.futures_official_backfill import _process_file_lock
from market_monitor.gfex_rank_backfill import GfexClient, capture


def main() -> int:
    parser = argparse.ArgumentParser(description="广期所排名原件回补，不写Catalog")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2025-01-01")
    parser.add_argument("--end-day")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--min-interval-seconds", type=float, default=0.2,
                        help="请求间隔，受共享5次/秒上限约束")
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--cookie-stdin", action="store_true")
    authentication.add_argument("--no-cookie", action="store_true")
    args = parser.parse_args()
    calendar = load_futures_trading_calendar(args.data_root)
    if calendar is None:
        parser.error("缺少交易日历")
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    cutoff = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
    end = args.end_day or max(day for day in calendar.trading_days if day <= cutoff)
    cookie = "" if args.no_cookie else (
        json.loads(sys.stdin.readline()).get("cookie", "") if args.cookie_stdin
        else getpass.getpass("GFEX临时Cookie（可为空）: ")
    )
    client = GfexClient(args.data_root, cookie, interval=args.min_interval_seconds)
    lock = args.data_root / "state" / "gfex_rank_capture.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _process_file_lock(lock):
            result = capture(args.data_root, client, args.start_day, end, args.max_days)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["status"] in {"PASS_RAW", "BATCH_LIMIT", "BUDGET_PAUSED"} else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
