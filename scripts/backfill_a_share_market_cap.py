"""Backfill audited daily A-share market capitalisation while services are stopped."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.a_share_market_cap_history import sync_a_share_market_cap_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--start-day", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-day", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    result = sync_a_share_market_cap_history(
        args.data_root, start_day=args.start_day, end_day=args.end_day,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result.get("failedDays"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
