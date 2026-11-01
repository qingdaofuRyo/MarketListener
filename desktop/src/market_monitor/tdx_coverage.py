"""Read-only coverage audit of local TongdaXin daily and five-minute files.

The 32-byte source records are scanned for timestamps only.  Raw coverage is
independent of the tdx-cn-v2 normalization and Silver import quality gates.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct
from typing import Any, Iterable

from .futures_calendar import FuturesTradingCalendar, load_futures_trading_calendar
from .market_classification import market_classification_spec


_BEIJING = timezone(timedelta(hours=8))
_DAY = struct.Struct("<I28x")
_MINUTE = struct.Struct("<HH28x")
_BLOCK_RECORDS = 8192
_RULE_VERSION = "tdx-raw-coverage-v1"
_DATE_INDEX_VERSION = "tdx-gap-date-index-v1"
_REPORT = Path("reports/tdx-coverage/latest.json")
_FILE_PATTERN = re.compile(r"^(?:[a-z]{2}\d{6}|\d+#[A-Za-z0-9_.-]+)\.(?:day|lc5)$", re.IGNORECASE)
_DOMESTIC_PREFIXES = {"28", "29", "30", "47", "66"}
_EQUITY_FUTURES = {"IF", "IH", "IC", "IM"}
_BOND_FUTURES = {"T", "TF", "TS", "TL"}


def load_latest_tdx_coverage(data_root: Path) -> dict[str, Any] | None:
    path = Path(data_root) / _REPORT
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == _RULE_VERSION else None


def run_tdx_coverage_check(
    data_root: Path,
    *,
    financial_root: Path | None = None,
    futures_root: Path | None = None,
) -> dict[str, Any]:
    """Scan the two installations without changing their files or importing data."""

    root = Path(data_root)
    installations = (
        ("financial", Path(financial_root or r"C:\tongdaxin")),
        ("futures", Path(futures_root or r"C:\new_tdxqh")),
    )
    try:
        calendar = load_futures_trading_calendar(root)
    except ValueError:
        calendar = None
    old = load_latest_tdx_coverage(root) or {}
    old_files = {
        (str(row.get("terminal")), str(row.get("file"))): row
        for row in old.get("files", []) if isinstance(row, dict)
    }
    calendar_key = _calendar_key(calendar)
    file_rows: list[dict[str, Any]] = []
    skipped = 0
    missing_roots: list[str] = []
    for terminal, install in installations:
        vipdoc = install / "vipdoc"
        if not vipdoc.is_dir():
            missing_roots.append(terminal)
            continue
        for path in _source_files(terminal, vipdoc):
            relative = path.relative_to(install).as_posix()
            try:
                before = path.stat()
            except OSError:
                file_rows.append(_unreadable(terminal, relative, path.suffix, "UNREADABLE"))
                continue
            key = (terminal, relative)
            previous = old_files.get(key)
            profile = _profile(terminal, path)
            fingerprint = {
                "bytes": before.st_size,
                "mtimeNs": before.st_mtime_ns,
                "ruleVersion": _RULE_VERSION,
                "dateIndexVersion": _DATE_INDEX_VERSION,
                "calendarKey": calendar_key,
                "profile": profile,
            }
            if previous and previous.get("fingerprint") == fingerprint:
                file_rows.append(previous)
                skipped += 1
                continue
            prior_fingerprint = previous.get("fingerprint") if previous else None
            old_calendar_fingerprint = {**fingerprint, "calendarKey": _legacy_calendar_key(calendar)}
            if (previous and isinstance(prior_fingerprint, dict)
                    and prior_fingerprint == old_calendar_fingerprint
                    and "gapDates" in previous):
                file_rows.append({**previous, "fingerprint": fingerprint})
                skipped += 1
                continue
            # Older reports omitted complete gap dates.  Their non-gapped
            # files remain valid when every other fingerprint input matches;
            # only affected files need a full rescan for the new index.
            legacy_fingerprint = {key: value for key, value in fingerprint.items()
                                  if key != "dateIndexVersion"}
            if (previous and previous.get("status") != "GAPPED"
                    and previous.get("fingerprint") == legacy_fingerprint):
                file_rows.append({**previous, "fingerprint": fingerprint, "gapDates": []})
                skipped += 1
                continue
            file_rows.append(_scan_file(path, terminal, relative, profile, calendar, fingerprint))
    groups = _groups(file_rows)
    gap_date_index = _gap_date_index(file_rows)
    summary = _aggregate(file_rows)
    summary["scannedFiles"] = len(file_rows) - skipped
    summary["reusedFiles"] = skipped
    summary["missingTerminals"] = missing_roots
    status = "PASS" if file_rows and not missing_roots and not (
        summary["gapFiles"] or summary["unreadableFiles"] or summary["writingFiles"]
        or summary["unknownCalendarFiles"]
    ) else "PARTIAL_FAILURE"
    report = {
        "schema": _RULE_VERSION,
        "generatedAt": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "status": status,
        "scope": "本机原始 .day/.lc5 文件；首尾观测之外的历史不判定",
        "calendar": {
            "futures": calendar.provider if calendar else None,
            "retrievedAt": calendar.retrieved_at if calendar else None,
            "tradingDaysSha256": _calendar_days_sha(calendar) if calendar else None,
            "securities": None,
        },
        "summary": summary,
        "groups": groups,
        "gapDateIndexVersion": _DATE_INDEX_VERSION,
        "gapDateIndex": gap_date_index,
        "extendedGroups": financial_extended_groups(file_rows),
        "files": file_rows,
    }
    _atomic_json(root / _REPORT, report)
    return report


def financial_extended_groups(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize recognized financial-terminal DS files by source prefix."""

    allowed = set(market_classification_spec().get("tdxFinancialDsPrefixes", {}))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        relative = str(row.get("file") or "")
        if row.get("terminal") != "financial" or not relative.startswith("vipdoc/ds/"):
            continue
        match = re.search(r"/(\d+)#[^/]+\.(?:day|lc5)$", relative, re.IGNORECASE)
        if match and match.group(1) in allowed:
            grouped[(match.group(1), str(row.get("period") or ""))].append(row)
    result: list[dict[str, Any]] = []
    for (prefix, period), items in sorted(grouped.items(), key=lambda item: (int(item[0][0]), item[0][1])):
        starts = [str(item["earliestAt"])[:10] for item in items if item.get("earliestAt")]
        endings = [str(item["latestAt"])[:10] for item in items if item.get("latestAt")]
        start_counts = Counter(starts)
        ending_counts = Counter(endings)
        common_start = sorted(
            start_counts.items(), key=lambda pair: (-pair[1], int(pair[0].replace("-", "")))
        )
        common_ending = sorted(
            ending_counts.items(), key=lambda pair: (-pair[1], -int(pair[0].replace("-", "")))
        )
        result.append({
            "prefix": prefix,
            "period": period,
            "files": len(items),
            "withRecords": sum(1 for item in items if item.get("earliestAt") and item.get("latestAt")),
            "commonStartDate": common_start[0][0] if common_start else None,
            "commonStartFiles": common_start[0][1] if common_start else 0,
            "commonLatestDate": common_ending[0][0] if common_ending else None,
            "commonLatestFiles": common_ending[0][1] if common_ending else 0,
        })
    return result


def _calendar_key(calendar: FuturesTradingCalendar | None) -> str:
    if calendar is None:
        return "missing"
    return f"{calendar.provider}:{_calendar_days_sha(calendar)}"


def _calendar_days_sha(calendar: FuturesTradingCalendar) -> str:
    return sha256("\n".join(calendar.trading_days).encode("ascii")).hexdigest()


def _legacy_calendar_key(calendar: FuturesTradingCalendar | None) -> str:
    if calendar is None:
        return "missing"
    return f"{calendar.provider}:{calendar.retrieved_at}:{len(calendar.trading_days)}:{calendar.trading_days[-1]}"


def _source_files(terminal: str, vipdoc: Path) -> Iterable[Path]:
    markets = ("sh", "sz", "bj", "ds") if terminal == "financial" else ("ds",)
    for market in markets:
        for folder in ("lday", "fzline"):
            directory = vipdoc / market / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.is_file() and path.suffix.lower() in {".day", ".lc5"}:
                    if terminal == "futures" and _profile(terminal, path) == "unknown":
                        # The futures installation also carries copied stock,
                        # HK and index caches.  This dashboard group represents
                        # its recognized domestic futures products only.
                        continue
                    yield path


def _profile(terminal: str, path: Path) -> str:
    if not _FILE_PATTERN.fullmatch(path.name):
        return "unknown"
    if terminal == "financial":
        if path.parent.parent.name.lower() in {"sh", "sz", "bj"}:
            return "cn-security"
        prefix, _, code = path.stem.partition("#")
        if prefix == "27" and code.upper().startswith("HZ"):
            return "unknown"
        if prefix in {"27", "31", "48"}:
            return "hk-security"
        if prefix in {"62", "69", "102"}:
            return "cn-index"
        return "unknown"
    prefix, _, code = path.stem.partition("#")
    if prefix not in _DOMESTIC_PREFIXES:
        return "unknown"
    product = _futures_product(code)
    if product is None:
        return "unknown"
    if prefix == "47":
        if product in _EQUITY_FUTURES:
            return "cffex-equity"
        if product in _BOND_FUTURES:
            return "cffex-bond"
        return "unknown"
    return f"commodity:{product}"


def _futures_product(code: str) -> str | None:
    configured = market_classification_spec().get("futures", {})
    products = set(_EQUITY_FUTURES | _BOND_FUTURES)
    for values in configured.get("commodityProducts", {}).values():
        products.update(str(value).upper() for value in values)
    # TDX continuous files append L7/L8/L9 to the product (e.g. RBL8).
    # Match a registered prefix, longest first, instead of treating RBL as a
    # product that has no trading-session profile.
    return next((value for value in sorted(products, key=lambda item: (-len(item), item))
                 if code.upper().startswith(value)), None)


def _unreadable(terminal: str, relative: str, suffix: str, reason: str) -> dict[str, Any]:
    return {
        "terminal": terminal,
        "file": relative,
        "period": "1d" if suffix.lower() == ".day" else "5m",
        "status": reason,
        "records": 0,
        "earliestAt": None,
        "latestAt": None,
        "gapCount": 0,
        "sampleGaps": [],
        "gapDates": [],
        "fingerprint": None,
    }


def _scan_file(
    path: Path,
    terminal: str,
    relative: str,
    profile: str,
    calendar: FuturesTradingCalendar | None,
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    period = "1d" if path.suffix.lower() == ".day" else "5m"
    base = _unreadable(terminal, relative, path.suffix, "UNREADABLE")
    base["fingerprint"] = fingerprint
    size = int(fingerprint["bytes"])
    if not size or size % 32:
        base["status"] = "INVALID_RECORD_LENGTH"
        return base
    days: set[int] = set()
    minutes: dict[int, int] = defaultdict(int)
    duplicates = 0
    invalid = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(32 * _BLOCK_RECORDS):
                if len(block) % 32:
                    base["status"] = "INVALID_RECORD_LENGTH"
                    return base
                if period == "1d":
                    for (raw_day,) in _DAY.iter_unpack(block):
                        if raw_day in days:
                            duplicates += 1
                        days.add(raw_day)
                else:
                    for packed, minute in _MINUTE.iter_unpack(block):
                        if minute >= 1440 or minute % 5:
                            invalid += 1
                            continue
                        flag = 1 << (minute // 5)
                        if minutes[packed] & flag:
                            duplicates += 1
                        minutes[packed] |= flag
        after = path.stat()
    except OSError:
        base["status"] = "UNREADABLE"
        return base
    if (after.st_size, after.st_mtime_ns) != (size, int(fingerprint["mtimeNs"])):
        base["status"] = "WRITING"
        base["fingerprint"] = None
        return base
    base["records"] = size // 32
    base["duplicateTimestamps"] = duplicates
    if period == "1d":
        parsed = {_parse_day(value) for value in days}
        if None in parsed:
            invalid += 1
        observed = sorted(value for value in parsed if value is not None)
        stamps = [value.isoformat() for value in observed]
        day_set = set(stamps)
    else:
        parsed_minutes: dict[date, int] = {}
        for packed, bitset in minutes.items():
            value = _parse_packed_day(packed)
            if value is None:
                invalid += 1
                continue
            parsed_minutes[value] = parsed_minutes.get(value, 0) | bitset
        observed = sorted(parsed_minutes)
        stamps = [f"{day.isoformat()}T{minute // 60:02d}:{minute % 60:02d}:00+08:00"
                  for day in observed for minute in _bits(parsed_minutes[day])]
        # The daytime dates are the trading-day evidence.  Night bars are
        # assessed within their observed sessions, but never invent a day.
        day_set = {day.isoformat() for day in observed if any(540 <= minute <= 930 for minute in _bits(parsed_minutes[day]))}
    base["earliestAt"] = min(stamps) if stamps else None
    base["latestAt"] = max(stamps) if stamps else None
    base["invalidTimestamps"] = invalid
    if invalid or not stamps:
        base["status"] = "INVALID_TIMESTAMP" if invalid else "EMPTY"
        return base
    samples: list[dict[str, str]] = []
    gap_dates: dict[tuple[str, str], int] = defaultdict(int)
    gap_count = 0
    calendar_known = terminal == "futures" and profile != "unknown" and calendar is not None and bool(day_set)
    if calendar_known:
        first, last = min(day_set), max(day_set)
        assert calendar is not None
        if calendar.trading_days[0] <= first and last <= calendar.trading_days[-1]:
            missing = sorted(set(calendar.between(first, last)) - day_set)
            gap_count += len(missing)
            samples.extend({"date": day, "kind": "交易日无记录"} for day in missing[:8])
            for day in missing:
                gap_dates[(day, "交易日无记录")] += 1
        else:
            calendar_known = False
    if period == "5m":
        for day in observed:
            slots = set(_bits(parsed_minutes[day]))
            expected = _expected_intraday_slots(profile, slots)
            if expected is None:
                continue
            missing_slots = sorted(expected - slots)
            gap_count += len(missing_slots)
            if missing_slots:
                gap_dates[(day.isoformat(), "5分钟候选缺口")] += len(missing_slots)
            samples.extend(
                {"date": day.isoformat(), "time": f"{minute // 60:02d}:{minute % 60:02d}", "kind": "5分钟候选缺口"}
                for minute in missing_slots[: max(0, 8 - len(samples))]
            )
    base["gapCount"] = gap_count
    base["sampleGaps"] = samples[:8]
    base["gapDates"] = [
        {"date": day, "kind": kind, "count": count}
        for (day, kind), count in sorted(gap_dates.items())
    ]
    base["calendarStatus"] = "VERIFIED" if calendar_known else "UNVERIFIED"
    base["sessionStatus"] = "VERIFIED" if period == "1d" or _sessions(profile) else "UNVERIFIED"
    base["status"] = "GAPPED" if gap_count else "CONTINUOUS" if calendar_known and base["sessionStatus"] == "VERIFIED" else "UNVERIFIED"
    return base


def _parse_day(value: int) -> date | None:
    try:
        return date(value // 10000, value // 100 % 100, value % 100)
    except ValueError:
        return None


def _parse_packed_day(value: int) -> date | None:
    year = value // 2048 + 2004
    month, day = divmod(value % 2048, 100)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _bits(value: int) -> Iterable[int]:
    while value:
        lowest = value & -value
        yield (lowest.bit_length() - 1) * 5
        value ^= lowest


def _sessions(profile: str) -> list[tuple[int, int]]:
    if profile in {"cn-security", "cn-index", "cffex-equity"}:
        return [(575, 690), (785, 900)]  # 09:35–11:30, 13:05–15:00
    if profile == "hk-security":
        return [(575, 720), (785, 960)]  # 09:35–12:00, 13:05–16:00
    if profile == "cffex-bond":
        return [(575, 690), (785, 915)]
    if profile.startswith("commodity:"):
        product = profile.split(":", 1)[1]
        sessions = [(545, 615), (635, 690), (815, 900)]
        configured = market_classification_spec().get("futures", {}).get("nightSessions", {})
        for window, products in configured.items():
            if product not in products:
                continue
            ending = str(window).split("-", 1)[-1]
            if ending == "23:00":
                sessions.append((1265, 1380))
            elif ending in {"01:00", "02:30"}:
                sessions.extend([(1265, 1435), (0, 60 if ending == "01:00" else 150)])
            break
        return sessions
    return []


def _expected_intraday_slots(profile: str, observed: set[int]) -> set[int] | None:
    sessions = _sessions(profile)
    if not sessions:
        return None
    expected: set[int] = set()
    for start, end in sessions:
        present = sorted(minute for minute in observed if start <= minute <= end)
        if len(present) < 2:
            continue
        expected.update(range(present[0], present[-1] + 1, 5))
    return expected


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "files": len(rows),
        "readableFiles": 0,
        "unreadableFiles": 0,
        "writingFiles": 0,
        "continuousFiles": 0,
        "gapFiles": 0,
        "unknownCalendarFiles": 0,
        "records": 0,
        "gapCount": 0,
    }
    for row in rows:
        status = row.get("status")
        if status == "WRITING":
            result["writingFiles"] += 1
        elif status in {"UNREADABLE", "INVALID_RECORD_LENGTH", "INVALID_TIMESTAMP", "EMPTY"}:
            result["unreadableFiles"] += 1
        else:
            result["readableFiles"] += 1
        if status == "CONTINUOUS":
            result["continuousFiles"] += 1
        elif status == "GAPPED":
            result["gapFiles"] += 1
        elif status == "UNVERIFIED":
            result["unknownCalendarFiles"] += 1
        result["records"] += int(row.get("records") or 0)
        result["gapCount"] += int(row.get("gapCount") or 0)
    return result


def _groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["terminal"]), str(row["period"]))].append(row)
    output: list[dict[str, Any]] = []
    for (terminal, period), items in sorted(grouped.items()):
        counts = _aggregate(items)
        starts = Counter(str(item["earliestAt"])[:10] for item in items if item.get("earliestAt"))
        endings = Counter(str(item["latestAt"])[:10] for item in items if item.get("latestAt"))
        common_start = sorted(starts.items(), key=lambda pair: (-pair[1], int(pair[0].replace("-", ""))))
        common_ending = sorted(endings.items(), key=lambda pair: (-pair[1], -int(pair[0].replace("-", ""))))
        samples = [gap for item in items for gap in item.get("sampleGaps", [])][:8]
        output.append({
            "terminal": terminal,
            "period": period,
            **counts,
            "commonStartDate": common_start[0][0] if common_start else None,
            "commonStartFiles": common_start[0][1] if common_start else 0,
            "commonLatestDate": common_ending[0][0] if common_ending else None,
            "commonLatestFiles": common_ending[0][1] if common_ending else 0,
            "sampleGaps": samples,
        })
    return output


def _gap_date_index(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact query index built by the explicit coverage task, never by GET."""

    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"affectedFiles": 0, "gapCount": 0, "sampleContracts": []}
    )
    for row in rows:
        terminal, period = str(row.get("terminal") or ""), str(row.get("period") or "")
        file_name = Path(str(row.get("file") or "")).stem
        contract = file_name.partition("#")[2] or file_name
        for gap in row.get("gapDates") or []:
            if not isinstance(gap, dict):
                continue
            day, kind = str(gap.get("date") or ""), str(gap.get("kind") or "")
            if not day or not kind:
                continue
            count = int(gap.get("count") or 0)
            if count <= 0:
                continue
            totals = grouped[(terminal, period, day, kind)]
            totals["affectedFiles"] += 1
            totals["gapCount"] += count
            examples = totals["sampleContracts"]
            if contract and contract not in examples and len(examples) < 5:
                examples.append(contract)
    return [
        {"terminal": terminal, "period": period, "date": day, "kind": kind,
         **totals, "confidence": "CANDIDATE"}
        for (terminal, period, day, kind), totals in sorted(grouped.items())
    ]


def coverage_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate file assessments without exposing per-file paths."""

    scoped = [
        row for row in rows
        if row.get("terminal") != "futures"
        or str((row.get("fingerprint") or {}).get("profile") or "unknown") != "unknown"
    ]
    return _groups(scoped)


def coverage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count only files in the two named dashboard download scopes."""

    scoped = [
        row for row in rows
        if row.get("terminal") != "futures"
        or str((row.get("fingerprint") or {}).get("profile") or "unknown") != "unknown"
    ]
    return _aggregate(scoped)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = (
    "run_tdx_coverage_check",
    "load_latest_tdx_coverage",
    "financial_extended_groups",
    "coverage_groups",
    "coverage_summary",
)
