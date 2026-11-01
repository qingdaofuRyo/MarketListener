"""Offline DCE browser-batch ZIP audit and optional source-isolated import."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_monitor.dce_batch_rank_import import import_dce_batch_ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="核验大商所每日排名ZIP，单写者窗口可显式导入")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--start-day", default="2025-01-01")
    parser.add_argument("--end-day", default="2026-09-30")
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--write", action="store_true", help="显式写入来源隔离事实；授权任务占用单写者锁时立即失败")
    args = parser.parse_args()
    try:
        result = import_dce_batch_ranks(
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
