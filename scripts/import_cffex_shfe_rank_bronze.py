"""Audit CFFEX and SHFE/INE ranking Bronze; optionally import one source/day."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from market_monitor.official_rank_bronze_import import import_official_rank_bronze


def main() -> int:
    parser = argparse.ArgumentParser(description="核验中金所或上期所（含上期能源）每日排名原件并可导入")
    parser.add_argument("--data-root", type=Path, default=Path("data_control"))
    parser.add_argument("--exchange", choices=("CFFEX", "SHFE"), required=True)
    parser.add_argument("--start-day", default="2023-01-01")
    parser.add_argument("--end-day", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--write", action="store_true", help="在单写者窗口写入来源隔离的正式排名事实")
    args = parser.parse_args()
    try:
        report = import_official_rank_bronze(
            args.data_root, exchange=args.exchange, start_day=args.start_day,
            end_day=args.end_day, max_days=args.max_days, write=args.write,
        )
    except RuntimeError as error:
        if str(error) != "DCE_AUTHORIZED_WRITER_ACTIVE":
            raise
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"VALIDATED", "PASS", "BATCH_LIMIT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
