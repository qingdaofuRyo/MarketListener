"""Read a bounded, source-isolated APP ETF observation without network access."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

_CODE = re.compile(r"(?:20|36):\d{6}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def read_ths_app_etf(root: Path, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    empty = {"available": False, "status": "UNAVAILABLE", "rows": [], "count": 0}
    folder = Path(root) / "bronze" / "ths_app_etf"
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    except OSError:
        return empty
    best = None
    best_time = None
    for path in files:
        try:
            if path.stat().st_size > 2_000_000:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema") != "ths-app-etf-observation-v1"
                    or data.get("quality") != "UNVERIFIED" or data.get("scope") != "APP_TRACKING_INDEX_FILTER"):
                continue
            rows, pages = data.get("rows"), data.get("capturedPages")
            if not isinstance(rows, list) or not 1 <= len(rows) <= 3000 or data.get("count") != len(rows):
                continue
            if not isinstance(pages, list) or not 1 <= len(pages) <= 100:
                continue
            offsets, times = [], []
            for page in pages:
                if not isinstance(page, dict) or type(page.get("pageBegin")) is not int:
                    raise ValueError("bad page")
                if not _SHA.fullmatch(str(page.get("responseSha256", ""))):
                    raise ValueError("bad hash")
                stamp = datetime.fromisoformat(page["observedAt"])
                if stamp.tzinfo is None or stamp > current:
                    raise ValueError("bad time")
                offsets.append(page["pageBegin"])
                times.append(stamp.astimezone(timezone.utc))
            if offsets != list(range(0, len(rows), 40)):
                continue
            seen, result = set(), []
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not _CODE.fullmatch(row["code"]):
                    raise ValueError("bad code")
                if row["code"] in seen or not isinstance(row.get("name"), str) or len(row["name"]) > 120:
                    raise ValueError("bad identity")
                seen.add(row["code"])
                stamp = datetime.fromisoformat(row["observedAt"])
                if stamp.tzinfo is None or stamp.astimezone(timezone.utc) != times[index // 40]:
                    raise ValueError("bad observation time")
                values = []
                for key in ("price", "changePct", "turnover"):
                    value = row.get(key)
                    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                        raise ValueError("bad value")
                    values.append(value)
                result.append({"code": row["code"], "name": row["name"], "price": values[0],
                               "changePct": values[1], "turnover": values[2],
                               "observedAt": stamp.astimezone(timezone.utc).isoformat()})
            capture_end = max(times)
            if best_time is None or capture_end > best_time:
                best_time = capture_end
                best = {"available": True, "status": "UNVERIFIED", "scope": "跟踪指数过滤",
                        "count": len(result), "captureStart": min(times).isoformat(),
                        "captureEnd": capture_end.isoformat(), "quoteTime": None, "rows": result}
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            continue
    return best or empty
