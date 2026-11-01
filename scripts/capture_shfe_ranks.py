"""Download SHFE and INE daily member rankings from the official SHFE site."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.shfe_rank_backfill import capture_shfe_ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="上期所及上期能源每日成交持仓排名原件回补")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", help="默认截至北京时间今天")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = capture_shfe_ranks(
        args.data_root, start_day=args.start_day, end_day=args.end_day,
        workers=args.workers, max_attempts=args.max_attempts,
        max_days=args.max_days, dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {"PASS_RAW", "PARTIAL_RAW", "BATCH_LIMIT", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
