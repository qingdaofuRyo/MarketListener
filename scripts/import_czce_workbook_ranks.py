"""Audit official CZCE workbooks and optionally import their ranked facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.czce_workbook_rank_import import import_czce_workbook_ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="核验郑商所逐日排名工作簿，可在单写者窗口导入")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--write", action="store_true", help="显式写入来源隔离事实；Catalog写者占用时立即失败")
    args = parser.parse_args()
    try:
        result = import_czce_workbook_ranks(
            args.data_root, start_day=args.start_day, end_day=args.end_day,
            max_days=args.max_days, write=args.write,
        )
    except RuntimeError as error:
        if str(error) != "DCE_AUTHORIZED_WRITER_ACTIVE":
            raise
        print(json.dumps({"status": "BLOCKED", "reason": "CATALOG_WRITER_ACTIVE"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"VALIDATED", "PASS", "BATCH_LIMIT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
