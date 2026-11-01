"""Read-only, source-isolated projection of locally captured KPL history."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .a_share_calendar import has_verified_a_share_calendar, is_verified_a_share_trading_day
from .kpl_market_probe import HISTORY_URL, MAX_BYTES, _validate

_ZONE = ZoneInfo("Asia/Shanghai")


def read_kpl_history(root: Path, *, now: datetime | None = None) -> dict:
    """Merge intact same-source captures by day, retaining observation time."""
    current = now or datetime.now(timezone.utc)
    empty = {"available": False, "source": "开盘啦", "fetchedAt": None, "points": [],
             "calendarIncomplete": False}
    base = Path(root) / "bronze" / "kpl_market_history"
    candidates: list[tuple[datetime, list]] = []
    try:
        folders = list(base.iterdir())
    except OSError:
        return empty
    if len(folders) > 5000:
        return empty
    for folder in folders:
        try:
            if not folder.is_dir() or len(folder.name) != 64:
                continue
            raw_file = folder / "response.raw"
            if raw_file.stat().st_size > MAX_BYTES:
                continue
            raw = raw_file.read_bytes()
            digest = sha256(raw).hexdigest()
            if digest != folder.name:
                continue
            data = json.loads(raw.decode("utf-8-sig"))
            observed = _validate(data, current.astimezone(_ZONE).date(), history=True)
            if observed is None:
                continue
            observations = list((folder / "observations").glob("*.json"))
            if len(observations) > 5000:
                continue
            for path in observations:
                try:
                    if path.stat().st_size > 4096:
                        continue
                    meta = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(meta, dict) or meta.get("schema") != "kpl-market-bronze-v1"
                            or meta.get("sha256") != digest or meta.get("observedDate") != observed
                            or meta.get("httpStatus") != 200 or meta.get("quality") != "UNVERIFIED"
                            or meta.get("requestedUrl") != HISTORY_URL):
                        continue
                    stamp = datetime.fromisoformat(meta["fetchedAt"])
                    if stamp.tzinfo is None or stamp > current:
                        continue
                    # An observation cannot substantiate a response that
                    # claims a trading date later than the fetch itself.
                    if date.fromisoformat(observed) > stamp.astimezone(_ZONE).date():
                        continue
                    candidates.append((stamp, data["info"]))
                except (OSError, ValueError, TypeError, KeyError, OverflowError):
                    continue
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            continue
    if not candidates:
        return empty
    fetched = max(stamp for stamp, _ in candidates)
    # A later fetch supersedes an earlier observation of the same trading day.
    # Rows from the same snapshot share its observation timestamp, not a
    # claimed exchange publication time or an inferred final close.
    points: dict[str, dict] = {}
    for stamp, rows in sorted(candidates, key=lambda item: item[0]):
        for row in rows:
            row_day = date.fromisoformat(row[6])
            if has_verified_a_share_calendar(row_day.year) and not is_verified_a_share_trading_day(row_day):
                continue
            points[row[6]] = {"day": row[6], "limitUp": row[0], "limitDown": row[1],
                              "naturalLimitUp": row[2], "everLimitDown": row[3],
                              "brokenRatePct": row[4], "brokenCount": row[5],
                              "observedAt": stamp.isoformat()}
    if not points:
        return empty
    end = date.fromisoformat(max(points))
    start = max(date.fromisoformat(min(points)), end - timedelta(days=366))
    points = {key: value for key, value in points.items() if date.fromisoformat(key) >= start}
    day = start
    incomplete = False
    while day <= end:
        if not has_verified_a_share_calendar(day.year):
            incomplete = True
        elif is_verified_a_share_trading_day(day):
            points.setdefault(day.isoformat(), {"day": day.isoformat(), "limitUp": None,
                "limitDown": None, "naturalLimitUp": None, "everLimitDown": None,
                "brokenRatePct": None, "brokenCount": None, "observedAt": None})
        day += timedelta(days=1)
    local = datetime.fromisoformat(points[end.isoformat()]["observedAt"]).astimezone(_ZONE)
    phase = ("INTRADAY" if local.date() == end and local.time() < time(15, 30)
             else "AFTER_CLOSE" if local.date() == end else "HISTORICAL")
    return {"available": True, "source": "开盘啦", "fetchedAt": fetched.isoformat(),
            "latestDay": end.isoformat(), "latestPhase": phase,
            "latestObservedAt": points[end.isoformat()]["observedAt"], "calendarIncomplete": incomplete,
            "points": [points[key] for key in sorted(points)]}
