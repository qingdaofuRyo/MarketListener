"""Backfill dated exchange statistics while the customer service is stopped."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.official_exchange_daily import (
    sync_official_exchange_daily,
    sync_official_monthly_issuance,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--start-day", required=True)
    parser.add_argument("--end-day", required=True)
    parser.add_argument("--start-month", help="first completed YYYY-MM month for BSE issuance")
    parser.add_argument("--end-month", help="last completed YYYY-MM month for BSE issuance")
    args = parser.parse_args()
    daily = sync_official_exchange_daily(args.data_root, start_day=args.start_day,
                                         end_day=args.end_day)
    result: dict[str, object] = {"daily": daily}
    if args.start_month or args.end_month:
        if not args.start_month or not args.end_month:
            parser.error("--start-month and --end-month must be supplied together")
        result["monthly"] = sync_official_monthly_issuance(
            args.data_root, start_month=args.start_month, end_month=args.end_month,
        )
    print(json.dumps(result, ensure_ascii=False))
    if daily.get("failedDays") or (isinstance(result.get("monthly"), dict)
                                   and result["monthly"].get("failedMonths")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
