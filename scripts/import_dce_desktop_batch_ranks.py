"""Offline audit and explicit import of visible desktop-browser DCE ZIPs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.dce_desktop_batch_import import import_dce_desktop_batch_ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="核验真实桌面浏览器下载的大商所每日会员排名 ZIP")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", required=True)
    parser.add_argument("--end-day", required=True)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--write", action="store_true", help="在单写者窗口显式导入已核验 Bronze")
    args = parser.parse_args()
    try:
        result = import_dce_desktop_batch_ranks(
            args.data_root, start_day=args.start_day, end_day=args.end_day,
            max_days=args.max_days, write=args.write,
        )
    except RuntimeError as error:
        if str(error) != "DCE_AUTHORIZED_WRITER_ACTIVE":
            raise
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"VALIDATED", "PASS", "BATCH_LIMIT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
