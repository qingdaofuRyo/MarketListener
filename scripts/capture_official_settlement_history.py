"""Capture exchange margin originals concurrently, or import saved bytes serially."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from market_monitor.futures_settlement_parameters import (
    EXCHANGES, import_settlement_bronze, sync_settlement_parameters,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="官网投机保证金结算参数：并发采原件、单写者串行入库")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day")
    parser.add_argument("--exchanges", nargs="+", choices=EXCHANGES, default=list(EXCHANGES))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--min-interval-seconds", type=float, default=0)
    parser.add_argument("--import-bronze", action="store_true",
                        help="只重新解析SHA核验的已存原件；需在现有单写者窗口运行")
    args = parser.parse_args()
    root = args.data_root.resolve()
    if args.import_bronze:
        connection = duckdb.connect(str(root / "catalog.duckdb"))
        try:
            report = import_settlement_bronze(root, connection, start_day=args.start_day, end_day=args.end_day)
        finally:
            connection.close()
    else:
        completed = 0

        def progress(item: dict) -> None:
            nonlocal completed
            completed += 1
            if completed % 30 == 0 or item["status"] not in {"PASS", "PARTIAL"}:
                print(json.dumps({"completed": completed, "exchange": item["exchange"],
                                  "tradeDate": item["trade_date"], "status": item["status"],
                                  "error": item.get("error")}, ensure_ascii=False), flush=True)

        report = sync_settlement_parameters(root, start_day=args.start_day, end_day=args.end_day,
                                            exchanges=args.exchanges, workers=args.workers,
                                            max_days=args.max_days, timeout=args.timeout,
                                            min_interval_seconds=args.min_interval_seconds,
                                            bronze_only=True, progress=progress)
    report.pop("outcomes", None)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
