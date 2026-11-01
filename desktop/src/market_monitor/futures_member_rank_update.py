"""Fill uncovered trading days with source-isolated official member rankings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .futures_calendar import load_futures_trading_calendar, sync_futures_trading_calendar
from .futures_official_backfill import BEIJING, default_backfill_range
from .futures_auto_refresh import member_rank_continuation_marker
from .storage import MarketStore


_SOURCE_VENUES = {
    "CFFEX": ("CFFEX",),
    "DCE": ("DCE",),
    "CZCE": ("CZCE",),
    "SHFE": ("SHFE", "INE"),
    "GFEX": ("GFEX",),
}
_CAPTURE_SCRIPTS = {
    "CFFEX": ("capture_cffex_xml_ranks.py", "--workers", "8"),
    "SHFE": ("capture_shfe_ranks.py", "--workers", "4"),
    "DCE": ("capture_dce_android_batch_ranks.py",),
    "CZCE": ("capture_czce_workbook_ranks.py",),
    "GFEX": ("capture_gfex_ranks.py", "--no-cookie"),
}
_GOOD_COVERAGE = frozenset({"PASS", "NO_PUBLICATION"})
_GOOD_CAPTURE = frozenset({"PASS_RAW", "PASS", "BATCH_LIMIT"})
HISTORY_START_DAY = "2023-01-01"


def _official_coverage_by_day(data_root: Path) -> dict[str, set[str]]:
    store = MarketStore(data_root)
    try:
        coverage: dict[str, set[str]] = {}
        for row in store.list_futures_member_position_coverage(commodity_only=False):
            if (
                str(row.get("source") or "").startswith("official-")
                and str(row.get("status") or "").upper() in _GOOD_COVERAGE
            ):
                coverage.setdefault(str(row["trading_day"]), set()).add(str(row["exchange"]))
        return coverage
    finally:
        store.close()


def _capture_command(
    *, project_root: Path, data_root: Path, source: str, trading_day: str,
) -> list[str]:
    script, *options = _CAPTURE_SCRIPTS[source]
    command = [
        sys.executable, str(project_root / "scripts" / script),
        "--data-root", str(data_root), "--start-day", trading_day,
        "--end-day", trading_day, *options,
    ]
    if source == "GFEX":
        command.extend(("--min-interval-seconds", "0.2"))
    return command


def _run_capture(
    *, project_root: Path, data_root: Path, source: str, trading_day: str,
) -> dict[str, Any]:
    if source == "DCE":
        from .dce_credentials import load_accounts

        if load_accounts():
            return {"status": "PASS", "captureStatus": "AUTHORIZED_API_READY",
                    "detail": "双账号授权API；由主写者采集及提交正式排名"}
    desktop_root = project_root / "desktop"
    environment = os.environ.copy()
    existing_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(desktop_root / "src"), existing_path) if value
    )
    environment.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    command = _capture_command(
        project_root=project_root, data_root=data_root,
        source=source, trading_day=trading_day,
    )
    try:
        completed = subprocess.run(
            command, cwd=project_root, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "FAILED", "detail": "Bronze采集进程超过300秒"}
    output_lines = (completed.stdout + "\n" + completed.stderr).splitlines()
    summary: dict[str, Any] = {}
    for line in reversed(output_lines):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("status"), str):
            summary = value
            break
    status = str(summary.get("status") or "FAILED")
    detail = str(
        summary.get("errorCode") or summary.get("reason") or summary.get("error")
        or summary.get("errorType") or summary.get("status") or "采集脚本未返回状态"
    )
    evidence = {
        field: summary[field] for field in ("httpStatus", "apiCode", "errorCode", "errorType", "reason")
        if isinstance(summary.get(field), (str, int, float, bool))
    }
    if completed.returncode != 0 or status not in _GOOD_CAPTURE:
        return {"status": "FAILED", "captureStatus": status, "detail": detail, **evidence}
    return {"status": "PASS", "captureStatus": status, "detail": detail}


def _source_pause_required(outcome: dict[str, Any]) -> bool:
    """Pause documented host refusal or exhausted transient retries, not a missing report."""

    try:
        http_status = int(outcome.get("httpStatus") or 0)
    except (TypeError, ValueError):
        http_status = 0
    if http_status in {403, 412, 429, 567} or 500 <= http_status <= 599:
        # A daily child process already applies bounded retries to transient 5xx.
        return True
    if outcome.get("captureStatus") in {"BLOCKED", "RATE_PAUSED", "BUDGET_PAUSED", "PAUSED_HOST", "PAUSED_RATE_LIMIT"}:
        return True
    text = " ".join(str(outcome.get(field) or "") for field in ("detail", "reason", "errorCode", "errorType")).casefold()
    return (
        bool(re.search(r"(?:http[_ :]*|status(?:code)?[= :]*)(403|412|429|5\d\d)(?!\d)", text))
        or any(marker in text for marker in (
            "captcha", "challenge", "waf", "验证码", "验证页", "频率限制",
            "rate_paused", "rate limit", "requestbudgetexhausted",
        ))
    )


def _dce_import_pause_required(imported: dict[str, Any]) -> bool:
    """Stop later DCE dates after an authorized API refusal or budget pause."""

    status = str(imported.get("status") or "").upper()
    if status in {"BATCH_LIMIT", "BUDGET_PAUSED", "RATE_PAUSED", "TLS_BLOCKED", "PAUSED_HOST"}:
        return True
    report = imported.get("report")
    if not isinstance(report, dict):
        return False
    try:
        api_code = int(report.get("apiCode") or 0)
    except (TypeError, ValueError):
        api_code = 0
    return api_code in {403, 412, 429, 501, 567} or 500 <= api_code <= 599 or _source_pause_required({
        "captureStatus": status,
        "httpStatus": report.get("httpStatus"),
        "detail": report.get("pauseReason"),
        "errorCode": report.get("errorCode"),
        "errorType": report.get("errorType"),
    })


def _dce_batch_continuation(imported: dict[str, Any], trading_day: str) -> dict[str, Any] | None:
    """Expose committed checkpoint counts without account or transport details."""

    progress = imported.get("report")
    if (imported.get("status") != "BATCH_LIMIT" or not isinstance(progress, dict)
            or progress.get("status") != "BATCH_LIMIT" or progress.get("currentDay") != trading_day):
        return None
    continuation = {
        "reason": "DCE_AUTHORIZED_BATCH_LIMIT", "source": "DCE_AUTHORIZED_API",
        "tradingDay": trading_day, "processedContracts": progress.get("processedContracts"),
        "completedContracts": progress.get("dayCompletedContracts"),
        "totalContracts": progress.get("dayTotalContracts"),
        "remainingContracts": progress.get("dayPendingContracts"), "retryAfterSeconds": 900,
    }
    return continuation if member_rank_continuation_marker({"continuation": continuation}) else None


def _import_source(data_root: Path, source: str, trading_day: str) -> dict[str, Any]:
    if source in {"CFFEX", "SHFE"}:
        from .official_rank_bronze_import import import_official_rank_bronze

        report = import_official_rank_bronze(
            data_root, exchange=source, start_day=trading_day,
            end_day=trading_day, write=True,
        )
    elif source == "DCE":
        from .dce_credentials import load_accounts

        accounts = load_accounts()
        if accounts:
            from .dce_authorized_backfill import DceClient, backfill
            from .dce_parallel_backfill import DceClientPool

            clients = [DceClient(data_root, item["api_key"], item["secret"], account_policy=(1, 3))
                       for item in accounts]
            pool = DceClientPool(data_root, clients)
            try:
                report = backfill(data_root, pool, trading_day, trading_day,
                                  max_days=1, max_contracts=25)
            finally:
                pool.close()
            return {"status": str(report.get("status") or "UNKNOWN"), "report": report}
        from .dce_batch_rank_import import import_dce_batch_ranks

        report = import_dce_batch_ranks(
            data_root, start_day=trading_day, end_day=trading_day, write=True,
        )
    elif source == "CZCE":
        from .czce_workbook_rank_import import import_czce_workbook_ranks

        report = import_czce_workbook_ranks(
            data_root, start_day=trading_day, end_day=trading_day, write=True,
        )
    else:
        from .gfex_rank_import import import_gfex_rank_bronze

        report = import_gfex_rank_bronze(
            data_root, start_day=trading_day, end_day=trading_day, write=True,
        )
    return {"status": str(report.get("status") or "UNKNOWN"), "report": report}


def run_latest_member_rank_update(data_root: Path, *, recent_trading_days: int | None = None) -> dict[str, Any]:
    """Repair uncovered source days; automatic runs bound the recent window."""

    if recent_trading_days is not None and not 1 <= recent_trading_days <= 30:
        raise ValueError("recent_trading_days must be between 1 and 30")

    root = Path(data_root).resolve()
    calendar = load_futures_trading_calendar(root)
    now = datetime.now(BEIJING)
    cutoff = (now.date() if now.hour >= 16 else now.date() - timedelta(days=1)).isoformat()
    if calendar is None or not calendar.trading_days or calendar.trading_days[-1] < cutoff:
        try:
            sync_futures_trading_calendar(root)
        except Exception:
            # Another queued task may have refreshed the local calendar while
            # the network refresh failed. Use that verified result if complete.
            calendar = load_futures_trading_calendar(root)
            if calendar is None or not calendar.trading_days or calendar.trading_days[-1] < cutoff:
                raise
        else:
            calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("缺少核验的期货交易日历，不能按工作日推断排名缺口")
    _start_day, trading_day = default_backfill_range(root)
    all_days = calendar.between(HISTORY_START_DAY, trading_day)
    days = all_days[-recent_trading_days:] if recent_trading_days is not None else all_days
    coverage_before = _official_coverage_by_day(root)
    covered = coverage_before.get(trading_day, set())
    missing_days_by_source = {
        source: [day for day in days if not set(venues).issubset(coverage_before.get(day, set()))]
        for source, venues in _SOURCE_VENUES.items()
    }
    missing_sources = [source for source, missing in missing_days_by_source.items() if missing]
    report: dict[str, Any] = {
        "status": "RUNNING", "tradingDay": trading_day,
        "startDay": days[0] if days and recent_trading_days is not None else HISTORY_START_DAY,
        "endDay": trading_day, "expectedDays": len(days),
        "pendingSourceDays": sum(len(missing) for missing in missing_days_by_source.values()),
        "pendingDaysBySource": {source: missing for source, missing in missing_days_by_source.items() if missing},
        "calendarProvider": calendar.provider,
        "coveredVenuesBefore": sorted(covered),
        "sources": {}, "missingVenues": [], "missingDaysByVenue": {},
    }
    if not days:
        report.update(status="PARTIAL", detail="2023年以来没有已结束且经过核验的交易日")
        return report
    if not missing_sources:
        report.update(
            status="PASS", skippedSources=list(_SOURCE_VENUES),
            coveredVenuesAfter=sorted(covered), updatedSourceDays=0,
        )
        return report

    project_root = Path(__file__).resolve().parents[2].parent
    capture_results: dict[str, dict[str, dict[str, Any]]] = {}

    def capture_source(source: str) -> dict[str, dict[str, Any]]:
        outcomes: dict[str, dict[str, Any]] = {}
        paused_on: str | None = None
        for day in missing_days_by_source[source]:
            if paused_on is not None:
                outcomes[day] = {
                    "status": "DEFERRED", "detail": f"{paused_on} 源端拒绝或重试耗尽；本轮后续日期暂停，等待续跑",
                }
                continue
            try:
                outcomes[day] = _run_capture(
                    project_root=project_root, data_root=root, source=source, trading_day=day,
                )
            except Exception as error:
                outcomes[day] = {
                    "status": "FAILED", "detail": f"{type(error).__name__}: {error}",
                }
            if outcomes[day]["status"] != "PASS" and _source_pause_required(outcomes[day]):
                paused_on = day
        return outcomes

    # One worker owns each source's dates. Bronze readers may run concurrently;
    # no capture process is allowed to open the authoritative Catalog writer.
    with ThreadPoolExecutor(max_workers=min(5, len(missing_sources)), thread_name_prefix="rank-capture") as pool:
        futures = {
            pool.submit(capture_source, source): source
            for source in missing_sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                capture_results[source] = future.result()
            except Exception as error:
                capture_results[source] = {
                    day: {"status": "FAILED", "detail": f"{type(error).__name__}: {error}"}
                    for day in missing_days_by_source[source]
                }

    for source in missing_sources:
        report["sources"][source] = {
            "status": "RUNNING", "expectedDays": len(missing_days_by_source[source]),
            "days": [],
        }

    # Import only checked originals, in date order, on the calling thread.
    # An error on one date or exchange never turns another source into a gap.
    paused_imports: dict[str, str] = {}
    for day in days:
        for source in missing_sources:
            paused_on = paused_imports.get(source)
            outcome = ({"status": "DEFERRED",
                        "detail": f"{paused_on} 授权API额度到达或源端暂停；本轮后续日期待续跑"}
                       if paused_on else capture_results[source].get(day))
            if outcome is None:
                continue
            day_result = {"tradingDay": day, **outcome}
            if outcome["status"] == "PASS":
                try:
                    imported = _import_source(root, source, day)
                except Exception as error:
                    day_result.update(
                        importStatus="FAILED", importError=f"{type(error).__name__}: {error}",
                    )
                else:
                    day_result["importStatus"] = imported["status"]
                    if source == "DCE" and outcome.get("captureStatus") == "AUTHORIZED_API_READY":
                        continuation = _dce_batch_continuation(imported, day)
                        if continuation is not None:
                            day_result["continuation"] = continuation
                    if source == "DCE" and _dce_import_pause_required(imported):
                        paused_imports[source] = day
            report["sources"][source]["days"].append(day_result)

    coverage_after = _official_coverage_by_day(root)
    covered_after = coverage_after.get(trading_day, set())
    expected_venues = {venue for venues in _SOURCE_VENUES.values() for venue in venues}
    missing_days_by_venue = {
        venue: missing for venue in sorted(expected_venues)
        if (missing := [day for day in days if venue not in coverage_after.get(day, set())])
    }
    updated_source_days = 0
    for source in missing_sources:
        source_report = report["sources"][source]
        source_days = source_report["days"]
        remaining = [
            day for day in missing_days_by_source[source]
            if not set(_SOURCE_VENUES[source]).issubset(coverage_after.get(day, set()))
        ]
        healed = len(missing_days_by_source[source]) - len(remaining)
        continuations = [item["continuation"] for item in source_days
                         if isinstance(item.get("continuation"), dict)]
        updated_source_days += healed
        source_report.update(
            status="PASS" if not remaining else "PARTIAL" if healed or continuations else "FAILED",
            importStatus="PASS" if not remaining else "BATCH_LIMIT" if continuations else "PARTIAL",
            capturedDays=sum(item["status"] == "PASS" for item in source_days),
            verifiedDays=healed, missingDays=remaining,
        )
        if source == "DCE" and any(item.get("captureStatus") == "AUTHORIZED_API_READY"
                                   for item in source_days):
            source_report["captureStatus"] = "AUTHORIZED_API_READY"
        if continuations:
            continuation = continuations[0]
            source_report["continuation"] = continuation
            source_report["detail"] = (
                f"授权API分批采集：{continuation['tradingDay']}已核验"
                f"{continuation['completedContracts']}/{continuation['totalContracts']}个合约，"
                f"本批新增{continuation['processedContracts']}个，"
                f"剩余{continuation['remainingContracts']}个，等待自动续跑"
            )
            if (set(missing_days_by_venue) == {"DCE"} and all(
                item.get("status") == "DEFERRED" or (
                    item.get("status") == "PASS" and item.get("importStatus") in {"PASS", "BATCH_LIMIT"}
                ) for item in source_days
            )):
                report["continuation"] = continuation
        failures = [item for item in source_days if item["status"] != "PASS" or item.get("importStatus") == "FAILED"]
        if failures and not continuations:
            first_failure = failures[0]
            source_report["detail"] = (
                f"{first_failure['tradingDay']}："
                f"{first_failure.get('importError') or first_failure.get('detail') or first_failure['status']}"
            )
    report.update(
        status="PASS" if not missing_days_by_venue else "PARTIAL",
        coveredVenuesAfter=sorted(covered_after), missingVenues=list(missing_days_by_venue),
        missingDaysByVenue=missing_days_by_venue, updatedSourceDays=updated_source_days,
    )
    return report


__all__ = ("run_latest_member_rank_update",)
