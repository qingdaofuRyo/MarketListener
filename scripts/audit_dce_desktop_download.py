"""Validate a manual Edge/Chrome DCE ranking ZIP against its download record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.dce_desktop_batch_capture import audit_desktop_batch_download


def main() -> int:
    parser = argparse.ArgumentParser(description="核验真实Edge/Chrome手动下载的大商所日排名ZIP")
    parser.add_argument("--zip", type=Path, required=True, help="原始浏览器下载ZIP路径")
    parser.add_argument("--history", type=Path, required=True, help="该浏览器profile的History文件路径")
    parser.add_argument("--browser", choices=("chrome", "edge"), required=True)
    parser.add_argument("--trade-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--capture", action="store_true", help="显式保存独立Bronze；不写正式数据库")
    args = parser.parse_args()
    try:
        report = audit_desktop_batch_download(
            args.zip, args.history, browser=args.browser,
            trading_day=args.trade_date, data_root=args.data_root,
            capture=args.capture,
        )
    except (OSError, RuntimeError, ValueError) as error:
        reason = str(error)
        status = "BLOCKED" if reason == "DCE_DESKTOP_HISTORY_LOCKED" else "FAILED"
        print(json.dumps({"status": status, "reason": reason}, ensure_ascii=False))
        return 2 if status == "BLOCKED" else 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
