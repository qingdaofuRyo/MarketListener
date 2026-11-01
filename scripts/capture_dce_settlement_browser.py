"""Capture exact dated DCE 181 originals through an already open Android browser."""
import argparse
import json
from pathlib import Path

from market_monitor.futures_dce_settlement_browser import DceSettlementBrowser
from market_monitor.futures_settlement_parameters import sync_settlement_parameters


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", default=None)
    parser.add_argument("--adb-port", type=int, default=5038)
    args = parser.parse_args()
    browser = DceSettlementBrowser(adb_port=args.adb_port)
    try:
        def progress(result):
            if result.get("status") != "PASS" or result["trade_date"].endswith(("01", "15", "30")):
                print(json.dumps({k: result.get(k) for k in ("trade_date", "status", "error", "record_count")}), flush=True)
        report = sync_settlement_parameters(args.data_root, start_day=args.start_day, end_day=args.end_day,
                                            exchanges=["DCE"], workers=1, bronze_only=True,
                                            dce_fetcher=browser.capture, progress=progress)
        report.pop("outcomes", None)
        print(json.dumps(report), flush=True)
    finally:
        browser.close()


if __name__ == "__main__":
    main()
