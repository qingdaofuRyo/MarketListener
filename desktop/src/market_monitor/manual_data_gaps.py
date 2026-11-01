"""Bounded administrator inventory of persisted data coverage evidence.

This module never downloads data or scans terminal files during a GET. A TDX
audit is explicitly refreshed by TDX_COVERAGE_CHECK; exchange coverage is
read from the small catalog coverage tables and a verified trading calendar.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import time
from typing import Any

from .futures_calendar import load_futures_trading_calendar
from .futures_official_daily_sources import PARSER_VERSION as DAILY_PARSER_VERSION
from .futures_settlement_parameters import PARSER_VERSION as SETTLEMENT_PARSER_VERSION
from .storage import connect_catalog
from .tdx_coverage import load_latest_tdx_coverage


_BEIJING = timezone(timedelta(hours=8))
_VENUES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")
_VENUE_NAMES = {"SHFE": "上期所", "INE": "上期能源", "DCE": "大商所",
                "CZCE": "郑商所", "GFEX": "广期所", "CFFEX": "中金所"}
_SOURCE_KINDS = (
    ("daily", "官网日行情", "FUTURES_OFFICIAL_DAILY_UPDATE"),
    ("ranking", "日成交持仓排名", "FUTURES_MEMBER_RANK_UPDATE"),
    ("settlement", "结算保证金", "FUTURES_SETTLEMENT_PARAMS_UPDATE"),
)


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=2)
def _tdx_report_cached(path: str, _mtime_ns: int, _size: int) -> dict[str, Any] | None:
    return load_latest_tdx_coverage(Path(path))


def _tdx_report(root: Path) -> dict[str, Any] | None:
    path = root / "reports" / "tdx-coverage" / "latest.json"
    try:
        stamp = path.stat()
    except OSError:
        return None
    return _tdx_report_cached(str(root), stamp.st_mtime_ns, stamp.st_size)


def _bounded_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        text = str(value)
        if "T" in text:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (stamp.astimezone(_BEIJING) if stamp.tzinfo else stamp).date().isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _filtered_dates(
    dates: list[dict[str, Any]], start: str | None, end: str | None, limit: int,
) -> tuple[list[dict[str, Any]], int]:
    matching = [item for item in dates if (not start or item["date"] >= start)
                and (not end or item["date"] <= end)]
    matching.sort(key=lambda item: (item["date"], item.get("time") or "", item.get("kind") or ""),
                  reverse=True)
    return matching[:limit], len(matching)


def tdx_gap_items(
    data_root: Path, *, start: str | None = None, end: str | None = None,
    limit: int = 40, local_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return persisted TDX date candidates without scanning raw files."""
    root = Path(data_root)
    report = _tdx_report(root)
    rows = report.get("files", []) if isinstance(report, dict) else []
    rows = rows if isinstance(rows, list) else []
    indexed = bool(report and report.get("gapDateIndexVersion") == "tdx-gap-date-index-v1")
    date_index = report.get("gapDateIndex", []) if indexed else []
    date_index = date_index if isinstance(date_index, list) else []
    generated = str(report.get("generatedAt") or "") if report else None
    audit_day = _bounded_date(generated)
    stale = bool(audit_day and (datetime.now(_BEIJING).date() - date.fromisoformat(audit_day)).days > 7)
    groups = (local_status or {}).get("roots", [])
    if generated:
        try:
            checked_at = datetime.fromisoformat(generated)
            stale = stale or any(
                isinstance(item, dict) and item.get("lastFileModifiedAt")
                and datetime.fromisoformat(str(item["lastFileModifiedAt"])) > checked_at
                for item in groups
            )
        except (TypeError, ValueError):
            stale = True
    group_lookup = {
        (str(root_item.get("terminal")), str(group.get("period"))): group
        for root_item in groups if isinstance(root_item, dict)
        for group in root_item.get("groups", []) if isinstance(group, dict)
    }
    result: list[dict[str, Any]] = []
    for terminal, terminal_label in (("financial", "通达信金融终端"), ("futures", "通达信期货通")):
        for period, period_label in (("1d", "日线"), ("5m", "5分钟线")):
            members = [item for item in rows if isinstance(item, dict)
                       and item.get("terminal") == terminal and item.get("period") == period]
            starts = [_bounded_date(str(item.get("earliestAt") or "")) for item in members]
            ends = [_bounded_date(str(item.get("latestAt") or "")) for item in members]
            starts = [value for value in starts if value]
            ends = [value for value in ends if value]
            raw_count = sum(max(0, int(item.get("gapCount") or 0)) for item in members)
            candidate_dates: list[dict[str, Any]] = []
            if indexed:
                for raw in date_index:
                    if not isinstance(raw, dict) or raw.get("terminal") != terminal or raw.get("period") != period:
                        continue
                    if not (day := _bounded_date(raw.get("date"))):
                        continue
                    candidate_dates.append({"date": day, "kind": str(raw.get("kind") or "原始文件候选缺口"),
                                            "confidence": "CANDIDATE", "affectedFiles": raw.get("affectedFiles"),
                                            "gapCount": raw.get("gapCount"),
                                            "sampleContracts": raw.get("sampleContracts", [])})
            else:
                for item in members:
                    for raw in item.get("sampleGaps", []):
                        if not isinstance(raw, dict) or not (day := _bounded_date(raw.get("date"))):
                            continue
                        candidate_dates.append({"date": day, "time": raw.get("time"),
                                                "kind": str(raw.get("kind") or "原始文件候选缺口"),
                                                "confidence": "SAMPLE_ONLY"})
            displayed, filtered_count = _filtered_dates(candidate_dates, start, end, limit)
            distinct_dates = len({item["date"] for item in candidate_dates
                                  if (not start or item["date"] >= start)
                                  and (not end or item["date"] <= end)})
            local = group_lookup.get((terminal, period), {})
            imported_latest = _bounded_date(local.get("importedLatestDate"))
            raw_latest = max(ends) if ends else None
            source_ahead = bool(raw_latest and imported_latest and raw_latest > imported_latest)
            if report is None:
                status = "UNKNOWN"
            elif stale:
                status = "AUDIT_STALE"
            elif source_ahead:
                status = "SOURCE_AHEAD"
            elif raw_count:
                status = "CANDIDATE_GAPS"
            elif members:
                status = "NO_GAPS_OBSERVED"
            else:
                status = "UNKNOWN"
            result.append({
                "key": f"tdx.{terminal}.{period}", "label": f"{terminal_label} · {period_label}",
                "updateMode": "MANUAL_DOWNLOAD_AUTO_IMPORT",
                "actionKind": "TDX_LOCAL_IMPORT", "checkOperation": "TDX_COVERAGE_CHECK",
                "dateSemantics": "TRADING_DAY_CANDIDATE", "status": status,
                "auditAt": generated, "startDate": min(starts) if starts else None,
                "latestDate": raw_latest, "importedLatestDate": imported_latest,
                "expectedThroughDate": None, "missingDates": displayed,
                "missingCount": distinct_dates if indexed else None,
                "candidateDateCount": distinct_dates if indexed else None,
                "candidateGapCount": raw_count if report else None,
                "returnedCount": len(displayed),
                "truncated": (filtered_count > len(displayed)) if indexed else bool(raw_count),
                "notes": (
                    ("原始文件逐条时间戳审计；日期索引包含全部候选缺口日期。" if indexed else
                     "原始文件逐条时间戳审计；旧报告每个文件仅保存最多8条样本，所选日期范围只筛选这些样本。")
                    + "缺口次数按文件和候选5分钟时段计，不等同缺失交易日或官方数据缺报。"
                    + ("审计已超过7日或有较新本地文件，请运行“检查通达信文件”。" if stale else "")
                    + ("本地文件末日超过已入库末日，可运行“导入本地数据”。" if source_ahead else "")
                ) if report else "尚无逐条时间戳审计；请运行“检查通达信文件”。",
            })
    return result


def _read_covered_exchange_days(root: Path) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[tuple[str, str]]], str | None]:
    """Read compact coverage evidence with the same writable DuckDB mode as the writer."""
    covered = {name: set() for name, _, _ in _SOURCE_KINDS}
    unpublished = {name: set() for name, _, _ in _SOURCE_KINDS}
    if not (root / "catalog.duckdb").is_file():
        return covered, unpublished, "尚无正式数据目录"
    connection = connect_catalog(root)
    try:
        tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
        if {"futures_contract_daily_official_coverage", "futures_contract_daily_official"} <= tables:
            rows = connection.execute(
                "SELECT c.trade_date,c.exchange FROM futures_contract_daily_official_coverage c "
                "JOIN futures_contract_daily_official f ON f.trade_date=c.trade_date "
                "AND f.exchange=c.exchange AND f.adapter=c.adapter "
                "WHERE c.status='PASS' AND c.parser_version=? AND c.adapter LIKE '%-daily-v2' "
                "GROUP BY c.trade_date,c.exchange,c.adapter,c.usable_count "
                "HAVING COUNT(*)>0 AND COUNT(*)=c.usable_count "
                "AND COUNT(*)=COUNT(CASE WHEN f.parser_version=? AND f.quality_status='PASS' THEN 1 END)",
                [DAILY_PARSER_VERSION, DAILY_PARSER_VERSION],
            ).fetchall()
            covered["daily"] = {(str(day), str(venue)) for day, venue in rows}
        if {"futures_member_position_coverage", "futures_member_position_ranks"} <= tables:
            rows = connection.execute(
                "SELECT c.trading_day,c.exchange,c.status FROM futures_member_position_coverage c "
                "WHERE c.source LIKE 'official-%' AND c.status='NO_PUBLICATION' "
                "OR c.source LIKE 'official-%' AND c.status='PASS' AND c.record_count>0 "
                "AND EXISTS (SELECT 1 FROM futures_member_position_ranks f "
                "WHERE f.trading_day=c.trading_day AND f.exchange=c.exchange AND f.source=c.source)"
            ).fetchall()
            for day, venue, status in rows:
                target = unpublished if status == "NO_PUBLICATION" else covered
                target["ranking"].add((str(day), str(venue)))
        if {"futures_settlement_parameter_coverage", "futures_settlement_parameter_official"} <= tables:
            rows = connection.execute(
                "WITH facts AS (SELECT trade_date,exchange,source_url,COUNT(*) n, "
                "COUNT(DISTINCT payload_sha256) sha_n,MIN(payload_sha256) sha "
                "FROM futures_settlement_parameter_official "
                "WHERE parser_version=? AND quality_status='PASS' GROUP BY 1,2,3) "
                "SELECT c.trade_date,c.exchange FROM futures_settlement_parameter_coverage c "
                "JOIN facts f USING(trade_date,exchange,source_url) "
                "WHERE c.status='PASS' AND c.parser_version=? AND c.record_count=f.n "
                "AND c.usable_count=f.n AND f.sha_n=1 AND c.payload_sha256=f.sha "
                "AND length(c.payload_sha256)=64",
                [SETTLEMENT_PARSER_VERSION, SETTLEMENT_PARSER_VERSION],
            ).fetchall()
            covered["settlement"] = {(str(day), str(venue)) for day, venue in rows}
            rows = connection.execute(
                "SELECT trade_date,exchange FROM futures_settlement_parameter_coverage "
                "WHERE status='NO_PUBLICATION' AND parser_version=?",
                [SETTLEMENT_PARSER_VERSION],
            ).fetchall()
            unpublished["settlement"] = {(str(day), str(venue)) for day, venue in rows}
    finally:
        connection.close()
    return covered, unpublished, None


@lru_cache(maxsize=4)
def _cached_covered_exchange_days(
    root_text: str, _catalog_stamp: tuple[int, int], _wal_stamp: tuple[int, int], _minute: int,
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[tuple[str, str]]], str | None]:
    return _read_covered_exchange_days(Path(root_text))


def _covered_exchange_days(root: Path) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[tuple[str, str]]], str | None]:
    def stamp(path: Path) -> tuple[int, int]:
        try:
            value = path.stat()
        except OSError:
            return (0, 0)
        return value.st_mtime_ns, value.st_size

    catalog = root / "catalog.duckdb"
    return _cached_covered_exchange_days(
        str(root.resolve()), stamp(catalog), stamp(catalog.with_name(catalog.name + ".wal")),
        int(time.monotonic() // 10),
    )


def official_gap_items(data_root: Path, *, start: str | None = None,
                       end: str | None = None, limit: int = 40,
                       source: str | None = None) -> list[dict[str, Any]]:
    root = Path(data_root)
    try:
        calendar = load_futures_trading_calendar(root)
    except ValueError:
        calendar = None
    now = datetime.now(_BEIJING)
    cutoff = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    all_days = [day for day in (calendar.trading_days if calendar else ())
                if "2023-01-01" <= day <= cutoff.isoformat()]
    eligible = [day for day in all_days if (not start or day >= start) and (not end or day <= end)]
    try:
        covered, unpublished, catalog_issue = _covered_exchange_days(root)
    except Exception:
        covered = {name: set() for name, _, _ in _SOURCE_KINDS}
        unpublished = {name: set() for name, _, _ in _SOURCE_KINDS}
        catalog_issue = "正式覆盖目录暂不可读"
    calendar_issue = None if calendar and all_days else "缺少覆盖目标日期的核验交易日历"
    result: list[dict[str, Any]] = []
    for kind, label, operation in _SOURCE_KINDS:
        for venue in _VENUES:
            key = f"official.{kind}.{venue.lower()}"
            if source and source != key:
                continue
            available = {day for day, exchange in covered[kind] if exchange == venue}
            no_publication = {day for day, exchange in unpublished[kind] if exchange == venue}
            if kind == "settlement" and venue == "CFFEX":
                first_evidence = min(available) if available else None
                latest_publication = max((day for day in available if not eligible or day <= eligible[-1]),
                                         default=None)
                status = ("UNKNOWN" if catalog_issue or calendar_issue or first_evidence is None else
                          "CALENDAR_STALE" if calendar.trading_days[-1] < cutoff.isoformat() else
                          "CURRENT_WITHIN_OBSERVED_RANGE")
                result.append({
                    "key": key, "label": f"{_VENUE_NAMES[venue]} · {label}",
                    "updateMode": "PUBLICATION_EVENT_AND_MANUAL", "actionKind": operation,
                    "checkOperation": None, "dateSemantics": "LATEST_VERIFIED_PUBLICATION",
                    "status": status, "auditAt": calendar.retrieved_at if calendar else None,
                    "startDate": first_evidence, "latestDate": latest_publication,
                    "expectedThroughDate": all_days[-1] if all_days else None,
                    "missingDates": [], "missingCount": 0 if first_evidence else None,
                    "unavailableCount": 0, "noNewPublicationDays": len(no_publication & set(eligible)),
                    "returnedCount": 0, "truncated": False,
                    "notes": (catalog_issue or calendar_issue or
                              "中金所按所选交易日前最近完整官网结算参数公布表逐合约沿用；无新公布不构成逐日缺口，"
                              "新表未列出的合约仍保持缺率。"),
                })
                continue
            first_evidence = min(available | no_publication) if available or no_publication else None
            # Before the first observed source day we lack listing/publication
            # evidence, so such dates are UNKNOWN instead of fabricated gaps.
            considered = [day for day in eligible if first_evidence and day >= first_evidence]
            missing = [day for day in considered if day not in available and day not in no_publication]
            displayed = [{"date": day, "kind": "核验交易日缺正式覆盖",
                          "confidence": "VERIFIED_MISSING"} for day in missing[-limit:][::-1]]
            unavailable_count = sum(day in no_publication for day in considered)
            if catalog_issue or calendar_issue or first_evidence is None:
                status = "UNKNOWN"
            elif calendar.trading_days[-1] < cutoff.isoformat():
                status = "CALENDAR_STALE"
            elif missing:
                status = "VERIFIED_MISSING"
            else:
                status = "CURRENT_WITHIN_OBSERVED_RANGE"
            result.append({
                "key": key,
                "label": f"{_VENUE_NAMES[venue]} · {label}",
                "updateMode": "AUTO_START_AND_MANUAL", "actionKind": operation,
                "checkOperation": None, "dateSemantics": "TRADING_DAY_VERIFIED",
                "status": status, "auditAt": calendar.retrieved_at if calendar else None,
                "startDate": first_evidence,
                "latestDate": max(available) if available else None,
                "expectedThroughDate": all_days[-1] if all_days else None,
                "missingDates": displayed, "missingCount": len(missing) if first_evidence else None,
                "unavailableCount": unavailable_count,
                "returnedCount": len(displayed), "truncated": len(missing) > len(displayed),
                "notes": (catalog_issue or calendar_issue or
                          "仅从首次正式覆盖/无公布记录起检查；更早日期因品种上市和来源历史未核定，状态未知。"
                          + ("部分日期官网明确无公布，已从缺口中排除。" if unavailable_count else "")
                          + ("交易日历未覆盖今日之前的全部日期，末端待重新核验。"
                             if status == "CALENDAR_STALE" else "")),
            })
    return result


def exchange_stat_items(data_root: Path, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    """Summarize official daily Gold observations by exchange, not by stock."""
    root = Path(data_root)
    facts: dict[str, tuple[Any, ...]] = {}
    error: str | None = None
    if not (root / "catalog.duckdb").is_file():
        error = "尚无正式数据目录"
    else:
        try:
            connection = connect_catalog(root)
            try:
                rows = connection.execute(
                    "SELECT split_part(instrument_id,'.',2), count(*), count(DISTINCT instrument_id), "
                    "count(DISTINCT metric_name), min(trading_date), max(trading_date), max(timestamp) "
                    "FROM gold_metrics WHERE metric_id LIKE 'OFFICIAL_EXCHANGE_DAILY:%' "
                    "AND instrument_id LIKE 'CN.%.MARKET.%' AND period='1d' "
                    "AND split_part(instrument_id,'.',2) IN ('SSE','SZSE','BSE') "
                    "AND (? IS NULL OR trading_date >= ?) AND (? IS NULL OR trading_date <= ?) GROUP BY 1",
                    [start, start, end, end],
                ).fetchall()
                facts = {str(row[0]): row[1:] for row in rows}
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - absence or lock is unknown, never reported as zero
            error = "交易所统计目录暂不可读"
    items = []
    for venue, label in (("SSE", "上交所官网日统计"), ("SZSE", "深交所官网日统计"), ("BSE", "北交所官网日统计")):
        row = facts.get(venue)
        items.append({
            "key": f"exchange.{venue.lower()}", "label": label, "updateMode": "AUTO_START_AND_MANUAL",
            "actionKind": "EQUITY_DASHBOARD_UPDATE", "checkOperation": None,
            "dateSemantics": "SOURCE_DEPENDENT", "status": "UNKNOWN",
            "auditAt": None, "startDate": row[3] if row else None,
            "latestDate": row[4] if row else None, "expectedThroughDate": None,
            "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
            "observedStatistics": ({"records": int(row[0]), "categories": int(row[1]), "metrics": int(row[2]),
                                    "lastWrittenAt": row[5]} if row else None),
            "notes": (error or "按所选日期范围内官网日统计 Gold 已入库记录汇总；类别数和指标名称数不等于个股数量，"
                       "起止观测日期不证明连续覆盖。本机写入时间不代表官网发布时间或成功抓取时间。"),
        })
    return items


def _latest_delisted_job(root: Path) -> dict[str, Any] | None:
    path = root / "operations.json"
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            return None
        candidates = []
        for value in values:
            if not isinstance(value, dict) or value.get("kind") != "A_SHARE_DELISTED_UPDATE":
                continue
            created = datetime.fromisoformat(str(value.get("created_at") or ""))
            if created.tzinfo is not None:
                candidates.append((created.astimezone(timezone.utc), value))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def delisted_snapshot_items(data_root: Path, *, start: str | None = None, end: str | None = None,
                            limit: int = 40) -> list[dict[str, Any]]:
    """Describe verified source snapshots without claiming daily continuity."""
    from .exchange_delisted import DelistedSourceError, load_exchange

    root = Path(data_root)
    job = _latest_delisted_job(root)
    items = []
    for venue, label in (("SSE", "上交所终止上市名单"), ("SZSE", "深交所终止上市名单")):
        try:
            rows, snapshot = load_exchange(root, venue, include_review_changes=True)
            selected = [row for row in rows if (not start or row["delistedAt"] >= start)
                        and (not end or row["delistedAt"] <= end)]
            days = sorted(row["delistedAt"] for row in selected)
            snapshot_info = {"status": snapshot["status"], "records": len(selected) if snapshot["status"] == "PASS" else None,
                             "totalRecords": len(rows) if snapshot["status"] == "PASS" else None,
                             "fetchedAt": snapshot.get("fetchedAt")}
            review = snapshot.get("review")
            if isinstance(review, dict):
                changes = review.get("changes") or []
                matching = [entry for entry in changes if any(
                    isinstance(day, str) and (not start or day >= start) and (not end or day <= end)
                    for day in (entry.get("previousDate"), entry.get("currentDate")))]
                snapshot_info["review"] = {"affectedCount": review.get("affectedCount"),
                                           "version": review.get("sha256"),
                                           "observedAt": review.get("observedAt"),
                                           "lastCheckedAt": review.get("lastCheckedAt"),
                                           "affectedIds": [item["instrumentId"] for item in matching[:limit]],
                                           "matchedCount": len(matching), "changes": matching[:limit],
                                           "truncated": len(matching) > limit,
                                           "sourceUrl": ("https://www.sse.com.cn/assortment/stock/list/delisting/"
                                                         if venue == "SSE" else "https://www.szse.cn/market/stock/suspend/index.html")}
        except DelistedSourceError:
            days = []
            snapshot_info = {"status": "INVALID", "records": None, "totalRecords": None, "fetchedAt": None}
        if job is not None:
            status = str(job.get("status") or "")
            if status not in {"QUEUED", "RUNNING", "PASS", "PARTIAL_FAILURE", "FAILED", "CANCELLED"}:
                status = "UNKNOWN"
            detail = str(job.get("detail") or "")
            outcome = re.search(rf"(?:^|[；、\s]){venue}=(PASS|SKIPPED_FRESH|FAILED)(?=$|[；、\s])", detail)
            snapshot_info["lastJob"] = {"status": status, "sourceOutcome": outcome.group(1) if outcome else None,
                                       "at": job.get("completed_at") or job.get("started_at") or job.get("created_at")}
        items.append({"key": f"exchange.{venue.lower()}-delisted", "label": label,
                      "updateMode": "AUTO_DAILY_19_AND_MANUAL", "actionKind": "A_SHARE_DELISTED_UPDATE",
                      "checkOperation": None, "dateSemantics": "EVENT_DRIVEN",
                      "status": "EVENT_DRIVEN" if snapshot_info["status"] == "PASS" else "UNKNOWN",
                      "auditAt": snapshot_info["fetchedAt"], "startDate": days[0] if days else None,
                      "latestDate": days[-1] if days else None, "expectedThroughDate": None,
                      "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
                      "listingSnapshot": snapshot_info,
                      "notes": "日期是名单中股票的摘牌生效日期，采集时间是本机取得快照的时间；"
                               "仅含交易所名单中识别出的 A 股，不代表历史时点已获取或逐日完整。北交所不在此名单中。"})
    return items


def macro_and_hk_items(data_root: Path, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    """Registered macro series and saved HKEX cap, each with its own frequency."""
    from .macro_series import DEFAULT_MACRO_SERIES

    root = Path(data_root)
    facts: dict[tuple[str, str], tuple[Any, ...]] = {}
    error: str | None = None
    series = [(item.series_id, item.frequency) for item in DEFAULT_MACRO_SERIES]
    if not (root / "catalog.duckdb").is_file():
        error = "尚无正式数据目录"
    else:
        try:
            connection = connect_catalog(root)
            try:
                rows = connection.execute(
                    "SELECT instrument_id, period, count(*), min(trading_date), max(trading_date), max(timestamp) "
                    "FROM gold_metrics WHERE split_part(metric_id,':',1)=instrument_id "
                    "GROUP BY instrument_id, period"
                ).fetchall()
                facts = {(str(row[0]), str(row[1])): row[2:] for row in rows if (str(row[0]), str(row[1])) in series}
                hk = connection.execute(
                    "SELECT count(*), min(trading_date), max(trading_date), max(timestamp) FROM gold_metrics "
                    "WHERE metric_id LIKE 'HKEX_MARKET_CAP:%' AND instrument_id='HK.HKEX.EQUITY.ALL' AND period='1d'"
                ).fetchone()
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - unavailable is not an empty dataset
            facts, error, hk = {}, "指标目录暂不可读", None
    if not (root / "catalog.duckdb").is_file():
        hk = None
    items = []
    for definition in DEFAULT_MACRO_SERIES:
        row = facts.get((definition.series_id, definition.frequency))
        items.append({
            "key": f"macro.{definition.series_id.lower().replace('_', '-')}", "label": definition.name,
            "updateMode": "SOURCE_DEPENDENT", "actionKind": None, "checkOperation": None,
            "dateSemantics": "SOURCE_DEPENDENT", "status": "UNKNOWN", "auditAt": None,
            "startDate": None, "latestDate": None, "expectedThroughDate": None,
            "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
            "observedSeries": ({"records": int(row[0]), "first": str(row[1]), "latest": str(row[2]),
                                "frequency": definition.frequency, "lastWrittenAt": row[3]} if row else None),
            "notes": (error or f"全库指标范围，不随所选日期筛选；登记来源：{definition.source}。"
                       "登记来源不等于逐条已核验来源；观测日期可能为统计期或公布日，不能推断期间连续。"),
        })
    items.append({
        "key": "hkex.market-cap", "label": "港交所已保存市值档案", "updateMode": "SOURCE_DEPENDENT",
        "actionKind": None, "checkOperation": None, "dateSemantics": "SOURCE_DEPENDENT", "status": "UNKNOWN",
        "auditAt": None, "startDate": None, "latestDate": None, "expectedThroughDate": None,
        "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
        "observedSeries": ({"records": int(hk[0]), "first": str(hk[1]), "latest": str(hk[2]),
                            "frequency": "DAILY", "lastWrittenAt": hk[3]} if hk and hk[0] else None),
        "notes": (error or "全库已保存主板及 GEM 市值观测，不随所选日期筛选；起止不代表连续。"
                   "港交所档案自动网页采集受权限限制，当前未启用。"),
    })
    return items


def event_driven_items(data_root: Path, *, limit: int = 40) -> list[dict[str, Any]]:
    root = Path(data_root)
    try:
        operations = json.loads((root / "operations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        operations = []
    operations = operations if isinstance(operations, list) else []
    rows = []
    for key, label, operation, path in (
        ("f10.cn", "A股 F10", "F10_UPDATE_CN", root / "f10" / "cn" / "summary.json"),
        ("f10.hk", "港股 F10", "F10_UPDATE_HK", root / "f10" / "hk" / "summary.json"),
        ("f10.revenue", "收入构成", "REVENUE_UPDATE", root / "f10" / "cn" / "revenue_summary.json"),
        ("reports", "研报处理", "REPORT_PROCESS", root / "reports" / "batch_summary.json"),
        ("contract-info", "期货合约信息", "FUTURES_CONTRACT_INFO_UPDATE",
         root / "state" / "futures_contract_info_last_checked.json"),
        ("industry-chain", "产业链资料", "CHAIN_REBUILD", root / "state" / "industry_chain_status.json"),
        ("industry-atlas", "产业图谱", "ATLAS_BUILD", root / "state" / "industry_atlas_status.json"),
    ):
        summary = _json_object(path)
        checkpoint = _json_object(root / "f10" / key.rsplit(".", 1)[-1] / "state.json") if key in {"f10.cn", "f10.hk"} else {}
        failed_codes = sorted({str(code) for code in checkpoint.get("failed", [])
                               if isinstance(code, (str, int)) and str(code)})
        refresh_failures = sorted(str(code) for code in (summary.get("pageFailures") or {})) if key == "contract-info" else []
        missing_items = ([{"code": code, "status": "FETCH_FAILED"} for code in failed_codes]
                         + [{"code": code, "status": "REFRESH_FAILED"} for code in refresh_failures])
        matches = [item for item in operations if isinstance(item, dict) and item.get("kind") == operation]
        last_operation = max(matches, key=lambda item: str(item.get("completed_at") or ""), default={})
        updated_at = (summary.get("completed_at") or summary.get("updated_at")
                      or summary.get("completedAt") or summary.get("checkedAt")
                      or last_operation.get("completed_at"))
        batch = None
        if key in {"f10.cn", "f10.hk", "f10.revenue"}:
            fields = ("new_revenue" if key == "f10.revenue" else "new_details",
                      "remaining_details", "failed_codes", "total_revenue" if key == "f10.revenue" else "total_details")
            values = [summary.get(field) for field in fields]
            completed_at = summary.get("completed_at")
            if (all(type(value) is int and value >= 0 for value in values)
                    and isinstance(completed_at, str)):
                try:
                    parsed = datetime.fromisoformat(completed_at)
                    if parsed.tzinfo is not None:
                        batch = {"updated": values[0], "remaining": values[1], "failed": values[2],
                                 "stored": values[3], "completedAt": completed_at}
                except ValueError:
                    pass
        rows.append({
            "key": key, "label": label,
            "updateMode": ("AUTO_WEEKLY_AND_MANUAL" if key == "contract-info" else
                           "AUTO_START_AND_MANUAL" if key in {"f10.cn", "f10.hk", "f10.revenue"} else "MANUAL_TASK_EVENT"),
            "actionKind": operation, "checkOperation": None,
            "dateSemantics": "EVENT_DRIVEN", "status": "EVENT_DRIVEN",
            "auditAt": updated_at, "startDate": None,
            "latestDate": None, "expectedThroughDate": None,
            "missingDates": [], "missingCount": None, "returnedCount": 0,
            "missingItems": missing_items[:limit],
            "missingItemCount": len(missing_items), "truncated": len(missing_items) > limit,
            "lastRecordedBatch": batch,
            "notes": ("按证券代码、报告期或文件事件检查覆盖；没有每天应公布的数据日期，不能按交易日推断缺报。"
                      + ("失败代码来自最近采集断点，不等于完整未采集代码清单。" if key in {"f10.cn", "f10.hk"} else "")
                      + ("刷新失败不等于已有合约事实缺失。" if key == "contract-info" else "")),
        })
    rows.append({
        "key": "market.providers", "label": "其他行情来源", "updateMode": "AUTO_START_AND_MANUAL",
        "actionKind": "MARKET_UPDATE", "checkOperation": None,
        "dateSemantics": "SOURCE_DEPENDENT", "status": "UNKNOWN", "auditAt": None,
        "startDate": None, "latestDate": None, "expectedThroughDate": None,
        "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
        "notes": "来源和频率各异；此查询尚无统一逐日缺口证据，请查看数据源清单及各任务状态。",
    })
    return rows


def collector_activity_items(data_root: Path) -> list[dict[str, Any]]:
    from .collector_history import latest_collector_statuses

    entries = (
        ("MACRO_SERIES", "宏观数据采集"), ("BLS_PAYROLL_CHANGE", "BLS 非农就业采集"),
        ("THS_CPI", "同花顺 CPI 采集"), ("CN_ZT_POOL", "A股涨跌停池采集"),
        ("CN_ZT_OPEN_RETURN", "昨日涨停收益采集"), ("A_SHARE_BREADTH", "A股涨跌家数采集"),
        ("CN_MARGIN", "融资融券采集"), ("HSGT_FLOW", "沪深港通资金采集"),
        ("HK_STOCK_BAR", "港股个股行情采集"), ("HK_INDEX_BAR", "港股指数行情采集"),
        ("CN_STOCK_BAR", "A股个股行情采集"), ("CN_INDEX_BAR", "A股指数行情采集"),
        ("CN_ETF_BAR", "A股 ETF 行情采集"), ("GLOBAL_INDEX_BAR", "全球指数行情采集"),
        ("FUTURE_MAIN_BAR", "国内期货主力采集"), ("FUTURE_INDEX_BAR", "国内商品指数采集"),
        ("FUTURE_GLOBAL_BAR", "外盘期货行情采集"), ("USD_INDEX_VIX", "美元指数及 VIX 采集"),
    )
    latest = latest_collector_statuses(Path(data_root))
    return [{"key": f"collector.{dataset.lower().replace('_', '-')}", "label": label,
             "updateMode": "SOURCE_DEPENDENT", "actionKind": None, "checkOperation": None,
             "dateSemantics": "EVENT_DRIVEN", "status": "EVENT_DRIVEN", "auditAt": None,
             "startDate": None, "latestDate": None, "expectedThroughDate": None,
             "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
             "collectorActivity": latest.get(dataset),
             "notes": "仅记录采集会话完成时各任务的状态及处理量；处理量不等于新增或入库量。"
                      "仅查最近 256 个保存会话；记录从功能启用后开始，旧会话只有当前摘要可查。"} for dataset, label in entries]


def legacy_pytdx_intraday_item(data_root: Path) -> dict[str, Any]:
    """Count legacy collector partitions; no claim that all rows are corrupt."""
    root = Path(data_root)
    result = None
    if (root / "catalog.duckdb").is_file():
        try:
            connection = connect_catalog(root)
            try:
                result = connection.execute(
                    "SELECT count(*), coalesce(sum(row_count), 0), max(data_cutoff) FROM partitions "
                    "WHERE partition_id LIKE 'CN-STOCK-30m-%' AND status='COMPLETE'"
                ).fetchone()
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - keep unknown distinct from empty
            result = None
    return {"key": "quality.pytdx-30m", "label": "通达信协议历史 30 分钟线待核验",
            "updateMode": "SOURCE_DEPENDENT", "actionKind": None, "checkOperation": None,
            "dateSemantics": "SOURCE_DEPENDENT", "status": "UNKNOWN", "auditAt": None,
            "startDate": None, "latestDate": None, "expectedThroughDate": None,
            "missingDates": [], "missingCount": None, "returnedCount": 0, "truncated": False,
            "qualityIssue": ({"partitions": int(result[0]), "rows": int(result[1]), "lastCutoff": result[2]}
                             if result else None),
            "notes": "旧协议采集器曾将分钟时间写为零点；此处统计相关历史分区，需逐条核验，"
                     "不推断全部记录错误。未核清原始时间口径前，新 30 分钟采集暂停；本地终端文件不受此项统计影响。"}


__all__ = ("tdx_gap_items", "official_gap_items", "event_driven_items", "exchange_stat_items", "delisted_snapshot_items", "macro_and_hk_items",
           "collector_activity_items", "legacy_pytdx_intraday_item")
