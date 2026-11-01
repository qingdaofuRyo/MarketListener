"""Capture CZCE official HTML ranking originals without writing Catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from market_monitor.czce_html_rank_backfill import CzceHtmlClient, capture_czce_html_ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="郑商所持仓排名 HTML 原件断点回补")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2025-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--min-interval-seconds", type=float, default=0.2,
                        help="请求间隔，受共享5次/秒及30000次/日限制")
    parser.add_argument("--cookie-stdin", action="store_true",
                        help="从标准输入读取一次临时Cookie；仅保存在进程内存")
    args = parser.parse_args()
    cookie = sys.stdin.readline().rstrip("\r\n") if args.cookie_stdin else ""
    client = CzceHtmlClient(
        args.data_root, min_interval_seconds=args.min_interval_seconds,
        cookie=cookie,
    )
    try:
        result = capture_czce_html_ranks(
            args.data_root, client,
            start_day=args.start_day, end_day=args.end_day, max_days=args.max_days,
        )
    finally:
        client.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {"PASS_RAW", "BATCH_LIMIT", "BUDGET_PAUSED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
