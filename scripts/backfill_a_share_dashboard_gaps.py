"""Backfill verified historical A-share dashboard gaps while the API is stopped.

The local TDX recovery reads only quality-gated Silver rows. Historical limit
pools are requested for explicit completed trading days from Eastmoney.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from market_monitor.a_share_silver_recovery import recover_recent_a_share_breadth
from market_monitor.collector import _collect_zt_pool, _persist_results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--pool-day", action="append", default=[], help="YYYYMMDD, repeatable")
    parser.add_argument("--lookback-days", type=int, default=30)
    args = parser.parse_args()
    if args.lookback_days < 1 or args.lookback_days > 366:
        parser.error("--lookback-days must be 1..366")
    recovered = recover_recent_a_share_breadth(args.data_root, lookback_days=args.lookback_days)
    print(f"Silver A-share breadth recovery: {recovered}")
    for day in args.pool_day:
        result = _collect_zt_pool(day)
        if result.status != "PASS":
            raise RuntimeError(f"{day} historical pool incomplete: {result.detail}")
        _persist_results(args.data_root, [result])
        print(f"Eastmoney {day} historical pool: {result.rows} verified metrics")


if __name__ == "__main__":
    main()
