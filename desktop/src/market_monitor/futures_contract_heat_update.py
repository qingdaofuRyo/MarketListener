"""Refresh exchange daily facts and rebuild official futures dashboard metrics."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import subprocess
import sys
from typing import Any

from .futures_calendar import load_futures_trading_calendar, sync_futures_trading_calendar
from .futures_contract_heat_pipeline import run_futures_contract_heat_pipeline
from .futures_official_backfill import BEIJING, DEFAULT_EXCHANGES, HostPaused, run_official_futures_backfill
from .futures_official_daily_sources import PARSER_VERSION
from .futures_official_structure import run_official_product_structure_pipeline
from .storage import MarketStore


HISTORY_START_DAY = "2023-01-01"
_DCE_BROWSER_BATCH_DAYS = 20


class _BronzeOnlySession:
    """The service importer may read cached originals but cannot issue requests."""

    trust_env = False

    def request(self, *args: Any, **kwargs: Any) -> None:
        raise HostPaused("大商所原件缺失；当前步骤只导入已核验的官网原件")


def _covered_official_days(root: Path, days: list[str]) -> set[tuple[str, str]]:
    store = MarketStore(root)
    try:
        rows = store.connection.execute(
            "SELECT c.trade_date, c.exchange FROM futures_contract_daily_official_coverage c "
            "JOIN futures_contract_daily_official f ON f.trade_date=c.trade_date "
            "AND f.exchange=c.exchange AND f.adapter=c.adapter "
            "WHERE c.trade_date>=? AND c.trade_date<=? AND c.status='PASS' "
            "AND c.parser_version=? AND c.adapter LIKE '%-daily-v2' "
            "GROUP BY c.trade_date, c.exchange, c.usable_count "
            "HAVING COUNT(*) > 0 AND COUNT(*)=c.usable_count "
            "AND COUNT(*)=COUNT(CASE WHEN f.parser_version=? AND f.quality_status='PASS' THEN 1 END)",
            [days[0], days[-1], PARSER_VERSION, PARSER_VERSION],
        ).fetchall()
    finally:
        store.close()
    return {(str(day), str(exchange)) for day, exchange in rows}


def _missing_dce_days(root: Path, days: list[str]) -> list[str]:
    covered = {day for day, exchange in _covered_official_days(root, days) if exchange == "DCE"}
    return [day for day in days if day not in covered]


def _update_dce_browser(root: Path, days: list[str]) -> dict[str, Any]:
    """Capture browser originals in a child, then import through the service process."""

    missing = _missing_dce_days(root, days)
    if not missing:
        return {"status": "CACHED", "pendingDays": 0}
    script = Path(__file__).resolve().parents[3] / "scripts" / "capture_official_daily_history.py"
    if not script.is_file():
        return {"status": "FAILED", "pendingDays": len(missing), "error": "大商所雷电日行情采集器不存在"}
    initial_pending = len(missing)
    batches = 0
    while missing:
        batch = missing[:_DCE_BROWSER_BATCH_DAYS]
        command = [
            sys.executable, str(script), "--data-root", str(root),
            "--exchange", "DCE", "--start-day", batch[0], "--end-day", batch[-1],
        ]
        try:
            result = subprocess.run(
                command, cwd=script.parent.parent, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "FAILED", "pendingDays": len(missing), "batches": batches,
                    "error": "雷电浏览器日行情批次未能完成"}
        batches += 1
        imported = run_official_futures_backfill(
            root, start_day=batch[0], end_day=batch[-1], exchanges=("DCE",),
            retry_failed=True, session=_BronzeOnlySession(),
        )
        remaining = _missing_dce_days(root, days)
        if result.returncode or imported.get("status") != "PASS" or len(remaining) >= len(missing):
            return {"status": "PARTIAL", "pendingDays": len(remaining), "batches": batches,
                    "error": "大商所官网日行情浏览器采集未完成；请核对雷电浏览器调试连接与已验证官网页面"}
        missing = remaining
    return {"status": "PASS", "pendingDays": 0, "updatedDays": initial_pending, "batches": batches}


def run_futures_contract_heat_update(data_root: Path, *, today: date | None = None,
                                     recent_trading_days: int | None = None) -> dict[str, Any]:
    """Fill official source gaps and rebuild Gold; automatic runs use a bounded window."""

    if recent_trading_days is not None and not 1 <= recent_trading_days <= 30:
        raise ValueError("recent_trading_days must be between 1 and 30")

    root = Path(data_root).resolve()
    calendar = load_futures_trading_calendar(root)
    now = datetime.now(BEIJING)
    cutoff = (today - timedelta(days=1) if today is not None else
              now.date() if now.hour >= 16 else now.date() - timedelta(days=1)).isoformat()
    if calendar is None or not calendar.trading_days or calendar.trading_days[-1] < cutoff:
        sync_futures_trading_calendar(root)
        calendar = load_futures_trading_calendar(root)
    all_days = list(calendar.between(HISTORY_START_DAY, cutoff)) if calendar is not None else []
    days = all_days[-recent_trading_days:] if recent_trading_days is not None else all_days
    if not days:
        return {
            "status": "PARTIAL_FAILURE", "refreshStatus": "PARTIAL_FAILURE",
            "source": "EXCHANGE_OFFICIAL_DAILY",
            "officialTurnover": {"status": "NO_DATA", "exchanges": [],
                                 "error": "缺少2023年以来已结束交易日的核验日历"},
            "calculation": {"status": "NO_DATA", "latestDay": None, "latestTurnoverCoverage": None},
            "structures": [],
        }
    start_day = days[0] if recent_trading_days is not None else HISTORY_START_DAY

    from .dce_official_api import update_official_dce_api

    try:
        api = update_official_dce_api(root, days)
    except Exception as error:
        api = {"status": "FAILED", "error": type(error).__name__}
    daily = run_official_futures_backfill(
        root, start_day=start_day, end_day=days[-1],
        exchanges=DEFAULT_EXCHANGES, retry_failed=True,
    )
    browser = _update_dce_browser(root, days)
    usable = _covered_official_days(root, days)
    venues = [
        {"exchange": exchange,
         "status": "PASS" if all((day, exchange) in usable for day in days) else "PARTIAL",
         "verifiedDays": sum((day, exchange) in usable for day in days),
         "expectedDays": len(days)}
        for exchange in DEFAULT_EXCHANGES
    ]
    source_complete = all(item["status"] == "PASS" for item in venues)
    official = {"status": "PASS" if source_complete else "PARTIAL", "exchanges": venues,
                "startDay": start_day, "endDay": days[-1],
                "requestBudget": daily.get("rateLimit"), "browser": browser}
    official["authorizedApi"] = api
    calculation = run_futures_contract_heat_pipeline(root)
    structures = [
        run_official_product_structure_pipeline(root, metric=metric, start_day=start_day,
                                                end_day=days[-1], rebuild_baseline=True)
        for metric in ("open_interest", "notional", "volume")
    ]
    # The scheduled capture must keep retrying recent source/structure gaps, but
    # an unchanged legacy weekly-heat quality gap cannot make a complete daily
    # capture re-fetch the same official files every two hours.
    refresh_complete = (
        source_complete and calculation.get("dailyStatus") == "PASS"
        and all(item.get("status") == "PASS" for item in structures)
    )
    refresh_status = "PASS" if refresh_complete else "PARTIAL_FAILURE"
    status = ("PASS" if refresh_complete and calculation.get("status") == "PASS"
              else "PARTIAL_FAILURE")
    return {"status": status, "source": "EXCHANGE_OFFICIAL_DAILY",
            "refreshStatus": refresh_status,
            "officialDaily": official, "officialTurnover": official,
            "calculation": calculation, "structures": structures}


__all__ = ("HISTORY_START_DAY", "run_futures_contract_heat_update")
