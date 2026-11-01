"""Repair recent dated A-share pools after the source has finalised them."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import duckdb

from .a_share_calendar import is_verified_a_share_trading_day


_REQUIRED = frozenset({"涨停_COUNT", "涨停_MAX_LIANBAN", "跌停_COUNT", "跌停_MAX_LIANBAN",
                       "炸板_COUNT"})
_POOL_METHODS = {
    "涨停_COUNT": "source=stock_zt_pool_em",
    "涨停_MAX_LIANBAN": "source=stock_zt_pool_em",
    "跌停_COUNT": "source=stock_zt_pool_dtgc_em",
    "跌停_MAX_LIANBAN": "source=stock_zt_pool_dtgc_em",
    "炸板_COUNT": "source=stock_zt_pool_zbgc_em; unique=code; close<limit_up; bursts>=1",
}


def _valid_pool_metric(key: str, method: str, value: float) -> bool:
    return (key in _POOL_METHODS and method.startswith(_POOL_METHODS[key])
            and math.isfinite(value) and value > 0)


def _valid_open_return(method: str, value: float) -> bool:
    if (not math.isfinite(value) or
            not method.startswith("source=eastmoney-previous-pool+tencent-dated-daily;") or
            "formula=(close-open)/open*100" not in method or
            "same_day_close_checked=true" not in method):
        return False
    match = re.search(r"(?:^|; )pool_count=(\d+); priced_count=(\d+)(?=;|$)", method)
    if match is None:
        return False
    total, priced = map(int, match.groups())
    return total > 0 and max(3, math.ceil(total * .8)) <= priced <= total


def recent_pool_candidates(
    data_root: Path, *, now: datetime, lookback_days: int = 14, max_days: int = 5,
) -> list[str]:
    """Find holes plus yesterday's potentially preliminary close, oldest first."""

    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.hour < 19:
        return []
    days = [local.date() - timedelta(days=offset) for offset in range(lookback_days + 1)]
    completed = [day for day in days if day < local.date() and is_verified_a_share_trading_day(day)]
    if not completed:
        return []
    latest = max(completed)
    catalog = Path(data_root) / "catalog.duckdb"
    by_day: dict[str, dict[str, str]] = {}
    if catalog.is_file():
        connection = duckdb.connect(str(catalog))
        try:
            rows = connection.execute("""
                SELECT trading_date, metric_id, calculation_method, value, timestamp FROM gold_metrics
                WHERE metric_id LIKE 'CN_ZT_POOL:CN.A_SHARE.%'
                  AND trading_date >= ? AND trading_date <= ?
            """, [min(completed).strftime("%Y%m%d"), latest.strftime("%Y%m%d")]).fetchall()
        except duckdb.Error:
            rows = []
        finally:
            connection.close()
        for day, metric_id, method, value, timestamp in rows:
            key = str(metric_id).rsplit(":", 1)[-1]
            if _valid_pool_metric(key, str(method), float(value)):
                by_day.setdefault(str(day), {})[key] = str(timestamp)
    missing = [day.strftime("%Y%m%d") for day in completed
               if not _REQUIRED.issubset(by_day.get(day.strftime("%Y%m%d"), {}))]
    chosen = sorted(missing, reverse=True)[:max_days]
    latest_text = latest.strftime("%Y%m%d")
    if latest_text not in chosen:
        if len(chosen) < max_days:
            chosen.append(latest_text)
        elif max_days >= 2:
            chosen[-1] = latest_text
    # Rotate through otherwise complete recent days. An old successful fetch
    # can predate a source correction despite all five metric keys existing.
    if len(chosen) < max_days:
        eligible = [day.strftime("%Y%m%d") for day in completed
                    if day.strftime("%Y%m%d") not in chosen and
                    _REQUIRED.issubset(by_day.get(day.strftime("%Y%m%d"), {}))]
        if eligible:
            chosen.append(min(eligible, key=lambda day: min(by_day[day].values())))
    return sorted(chosen)


def recover_recent_a_share_pools(
    data_root: Path, *, now: datetime | None = None, max_days: int = 5,
) -> dict[str, object]:
    """Recheck dated pools and publish only complete, positive source results."""

    from .collector import _collect_zt_pool
    from .storage import MarketStore

    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    days = recent_pool_candidates(data_root, now=now, max_days=max_days)
    passed: list[str] = []
    failed: list[str] = []
    metric_count = 0
    for day in days:
        result = _collect_zt_pool(day)
        keys = {str(item["metric_id"]).rsplit(":", 1)[-1]
                for item in result.persist.gold_metrics}
        if result.status != "PASS" or not _REQUIRED.issubset(keys):
            failed.append(day)
            continue
        store = MarketStore(data_root)
        try:
            metric_count += store.upsert_gold_metrics(result.persist.gold_metrics)
        finally:
            store.close()
        passed.append(day)
    return {"checkedDays": days, "recoveredDays": passed,
            "failedDays": failed, "metricCount": metric_count}


def recover_recent_a_share_open_returns(
    data_root: Path, *, now: datetime | None = None, lookback_days: int = 14,
    max_days: int = 2,
) -> dict[str, object]:
    """Repair bounded recent true open-to-close returns without a blocking day."""

    from .collector import _collect_zt_open_return
    from .storage import MarketStore

    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.hour < 19:
        return {"checkedDays": [], "recoveredDays": [], "failedDays": []}
    days = [local.date() - timedelta(days=offset) for offset in range(1, lookback_days + 1)]
    candidates = [day.strftime("%Y%m%d") for day in days if is_verified_a_share_trading_day(day)]
    catalog = Path(data_root) / "catalog.duckdb"
    trusted: set[str] = set()
    if catalog.is_file() and candidates:
        connection = duckdb.connect(str(catalog))
        try:
            rows = connection.execute("""
                SELECT trading_date, value, calculation_method FROM gold_metrics
                WHERE metric_id LIKE 'CN_ZT_POOL_OPEN_RETURN_V2:%'
                  AND trading_date >= ? AND trading_date <= ?
            """, [min(candidates), max(candidates)]).fetchall()
            trusted = {str(day) for day, value, method in rows
                       if _valid_open_return(str(method), float(value))}
        except duckdb.Error:
            pass
        finally:
            connection.close()
    pending = [day for day in candidates if day not in trusted]
    state_path = Path(data_root) / "state" / "a_share_open_return_attempts.json"
    try:
        attempts = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(attempts, dict):
            attempts = {}
    except (OSError, ValueError):
        attempts = {}
    # Retry the newest miss, while rotating the remaining slots toward dates
    # that have never been attempted or have waited longest.
    selected = pending[:1] + sorted(pending[1:], key=lambda day: (str(attempts.get(day, "")), -int(day)))[
        :max(0, max_days - 1)]
    recovered: list[str] = []
    failed: list[str] = []
    for day in selected:
        attempts[day] = local.isoformat()
        result = _collect_zt_open_return(day)
        metrics = [item for item in result.persist.gold_metrics
                   if str(item["metric_id"]).endswith(":YESTERDAY_LIMIT_UP_OPEN_RETURN")
                   and _valid_open_return(str(item["calculation_method"]), float(item["value"]))]
        if result.status != "PASS" or len(metrics) != 1:
            failed.append(day)
            continue
        store = MarketStore(data_root)
        try:
            store.upsert_gold_metrics(metrics)
        finally:
            store.close()
        recovered.append(day)
    if selected:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({day: attempts[day] for day in candidates if day in attempts},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, state_path)
    return {"checkedDays": selected, "recoveredDays": recovered, "failedDays": failed}


__all__ = ("recent_pool_candidates", "recover_recent_a_share_pools",
           "recover_recent_a_share_open_returns")
