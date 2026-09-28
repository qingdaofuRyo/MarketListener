"""Read-only, bounded probe of exchange daily contract turnover through AKShare."""

from __future__ import annotations

import argparse
from datetime import date
import json
import math
import subprocess
import sys
from typing import Any


_EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")


def _number(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _worker(exchange: str, day: str) -> dict[str, Any]:
    import akshare as ak

    frame = ak.get_futures_daily(
        start_date=day.replace("-", ""), end_date=day.replace("-", ""), market=exchange
    )
    rows = frame.to_dict(orient="records")
    usable = [
        row for row in rows
        if str(row.get("symbol") or "").strip() and (_number(row.get("turnover")) or 0) > 0
    ]
    return {
        "exchange": exchange,
        "tradingDay": day,
        "status": "PASS" if usable else "NO_USABLE_TURNOVER",
        "rows": len(rows),
        "rowsWithPositiveTurnover": len(usable),
        "columns": list(frame.columns),
        "sample": [
            {
                "symbol": str(row.get("symbol") or ""),
                "turnover": _number(row.get("turnover")),
                "volume": _number(row.get("volume")),
                "settlement": _number(row.get("settle")),
            }
            for row in usable[:3]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", choices=_EXCHANGES, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            result = _worker(args.exchange, args.date.isoformat())
        except Exception as error:
            result = {
                "exchange": args.exchange,
                "tradingDay": args.date.isoformat(),
                "status": "FAILED",
                "error": f"{type(error).__name__}: {error}"[:300],
            }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "PASS" else 2
    try:
        completed = subprocess.run(
            [sys.executable, __file__, "--exchange", args.exchange, "--date", args.date.isoformat(), "--worker"],
            capture_output=True, text=True, timeout=args.timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"exchange": args.exchange, "tradingDay": args.date.isoformat(), "status": "TIMEOUT"}))
        return 2
    output = completed.stdout.strip().splitlines()
    if not output:
        print(json.dumps({"exchange": args.exchange, "tradingDay": args.date.isoformat(), "status": "FAILED", "error": completed.stderr[-300:]}))
        return 2
    print(output[-1])
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
