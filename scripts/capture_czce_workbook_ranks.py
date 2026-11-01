"""Capture official CZCE XLS/XLSX daily ranks without a second Catalog writer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.czce_workbook_rank_backfill import CzceWorkbookClient, capture_czce_workbook_ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="郑商所持仓排名 XLS/XLSX 原件断点回补")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2025-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--retry-missing", action="store_true", help="重新请求此前两个格式均404的日期")
    parser.add_argument("--min-interval-seconds", type=float, default=2)
    args = parser.parse_args()
    client = CzceWorkbookClient(args.data_root, min_interval_seconds=args.min_interval_seconds)
    try:
        result = capture_czce_workbook_ranks(args.data_root, client, start_day=args.start_day,
                                             end_day=args.end_day, max_days=args.max_days,
                                             retry_missing=args.retry_missing)
    finally:
        client.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {"PASS_RAW", "PARTIAL_RAW", "BATCH_LIMIT", "BUDGET_PAUSED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
