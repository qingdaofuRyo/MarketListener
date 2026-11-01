"""Offline import of a visibly downloaded DCE ranking ZIP.

The desktop capture verifies the Chrome/Edge History row when Bronze is made.
This importer rechecks the retained proof fields and the immutable ZIP; it
does not need a browser profile, the original Downloads file, or network.
Only an explicit ``write`` call opens the Catalog under the shared writer lock.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .czce_html_rank_backfill import _write_json
from .dce_batch_rank_import import (
    _exclusive_authorized_writer_window,
    _reconcile_authorized_overlap,
    _utc,
)
from .dce_batch_zip import parse_dce_batch_zip
from .dce_desktop_batch_capture import ADAPTER as CAPTURE_ADAPTER
from .dce_desktop_batch_capture import (
    BRONZE,
    SCHEMA as CAPTURE_SCHEMA,
    _official_url,
    _same_file_state,
)
from .futures_calendar import load_futures_trading_calendar
from .futures_member_positions import normalise_exchange_member_position_ranks
from .futures_member_rank_source import DCE_BATCH_SOURCE, DCE_DESKTOP_BATCH_SOURCE
from .futures_member_trade_ranks import normalise_exchange_member_trade_ranks
from .storage import MarketStore


STATE = Path("state") / "dce_desktop_batch_import"
IMPORT_ADAPTER = "official-dce-desktop-browser-batch-v1"
CAPTURE_METHOD = "manual visible Chrome/Edge button download; History Blob record"
_MAX_MANIFEST_BYTES = 32 * 1024
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PreparedDceDesktopDay:
    trading_day: str
    archive_sha256: str
    manifest_sha256: str
    browser: str
    source_page: str
    entry_count: int
    ranked_contract_count: int
    positions: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    coverage: dict[str, Any]

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "tradingDay": self.trading_day,
            "archiveSha256": self.archive_sha256,
            "manifestSha256": self.manifest_sha256,
            "browser": self.browser,
            "sourcePage": self.source_page,
            "entryCount": self.entry_count,
            "rankedContractCount": self.ranked_contract_count,
            "positionRows": len(self.positions),
            "tradeRows": len(self.trades),
            "source": DCE_DESKTOP_BATCH_SOURCE,
        }


def prepare_dce_desktop_batch_day(root: Path, trading_day: str) -> PreparedDceDesktopDay:
    """Fail closed on a changed manifest, ZIP, date, or capture-time proof."""

    day = date.fromisoformat(trading_day).isoformat()
    compact = day.replace("-", "")
    base = Path(root) / BRONZE / compact
    manifest_path = base.with_suffix(".json")
    zip_path = base.with_suffix(".zip")
    if not manifest_path.is_file() or not zip_path.is_file():
        raise FileNotFoundError(f"DCE desktop batch Bronze is incomplete for {day}")
    manifest_before = manifest_path.stat()
    if not 0 < manifest_before.st_size <= _MAX_MANIFEST_BYTES:
        raise ValueError(f"DCE desktop batch manifest size is invalid for {day}")
    with manifest_path.open("rb") as stream:
        manifest_opened = os.fstat(stream.fileno())
        manifest_bytes = stream.read(_MAX_MANIFEST_BYTES + 1)
        manifest_after = os.fstat(stream.fileno())
    if (not _same_file_state(manifest_before, manifest_opened)
            or not _same_file_state(manifest_opened, manifest_after)
            or not _same_file_state(manifest_after, manifest_path.stat())):
        raise ValueError(f"DCE desktop batch manifest changed during read for {day}")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"DCE desktop batch manifest is invalid for {day}") from error
    if not isinstance(manifest, dict):
        raise ValueError(f"DCE desktop batch manifest is invalid for {day}")

    page = manifest.get("source")
    origin = manifest.get("downloadOrigin")
    if (not isinstance(page, str) or _official_url(page, page=True) != page
            or not isinstance(origin, str)
            or origin != f"blob:{urlsplit(page).scheme}://www.dce.com.cn"):
        raise ValueError(f"DCE desktop browser page or Blob proof is invalid for {day}")
    if any((
        manifest.get("schema") != CAPTURE_SCHEMA,
        manifest.get("publisher") != "DCE",
        manifest.get("adapter") != CAPTURE_ADAPTER,
        manifest.get("tradeDate") != day,
        manifest.get("method") != CAPTURE_METHOD,
        manifest.get("browser") not in {"chrome", "edge"},
        manifest.get("historyReadMode") is not None,
        manifest.get("status") != "PASS_RAW",
        manifest.get("qualityStatus") != "PASS_RAW",
        manifest.get("authoritativeDataWritten") is not False,
        manifest.get("httpStatus") is not None,
    )):
        raise ValueError(f"DCE desktop batch capture proof differs for {day}")
    try:
        downloaded_at = datetime.fromisoformat(manifest["downloadedAtUtc"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"DCE desktop batch History time is invalid for {day}") from error
    if downloaded_at.tzinfo is None or not 2020 <= downloaded_at.year <= datetime.now().year + 1:
        raise ValueError(f"DCE desktop batch History time is invalid for {day}")

    zip_before = zip_path.stat()
    if not 0 < zip_before.st_size <= _MAX_ARCHIVE_BYTES:
        raise ValueError(f"DCE desktop batch ZIP size is invalid for {day}")
    with zip_path.open("rb") as stream:
        zip_opened = os.fstat(stream.fileno())
        raw = stream.read(_MAX_ARCHIVE_BYTES + 1)
        zip_after = os.fstat(stream.fileno())
    if (not _same_file_state(zip_before, zip_opened)
            or not _same_file_state(zip_opened, zip_after)
            or not _same_file_state(zip_after, zip_path.stat())):
        raise ValueError(f"DCE desktop batch ZIP changed during read for {day}")
    parsed = parse_dce_batch_zip(raw, trading_day=compact)
    if any((
        type(manifest.get("bytes")) is not int or manifest["bytes"] != len(raw),
        manifest.get("payloadSha256") != parsed.archive_sha256,
        manifest.get("summary") != parsed.summary,
        parsed.entry_count != len(parsed.frames) + len(parsed.empty_contracts) + len(parsed.excluded_contracts),
    )):
        raise ValueError(f"DCE desktop batch ZIP differs from manifest for {day}")

    # The capture summary was made with the original parser scope. Reparse
    # monthly-average reports for Silver without changing that raw evidence.
    parsed = parse_dce_batch_zip(raw, trading_day=compact, include_monthly_average=True)
    volume_count, long_count, short_count = parsed.row_counts
    if not all((volume_count, long_count, short_count)):
        raise ValueError(f"DCE desktop batch lacks a published ranking direction for {day}")
    timestamp = downloaded_at.isoformat(timespec="seconds")
    positions = normalise_exchange_member_position_ranks(
        parsed.frames, exchange="DCE", trading_day=day,
        source=DCE_DESKTOP_BATCH_SOURCE, collected_at=timestamp,
    )
    trades = normalise_exchange_member_trade_ranks(
        parsed.frames, exchange="DCE", trading_day=day,
        source=DCE_DESKTOP_BATCH_SOURCE, collected_at=timestamp,
    )
    if (len(positions) != long_count + short_count
            or len(trades) != volume_count):
        raise ValueError(f"DCE desktop batch normalization lost a ranking direction for {day}")
    provenance = {
        "publisher": "DCE", "source_url": page, "adapter": IMPORT_ADAPTER,
        "retrieved_at": timestamp, "payload_sha256": parsed.archive_sha256,
        "quality_status": "PASS", "legacy_adapter": None,
    }
    position_rows = [{**row.to_dict(), **provenance} for row in positions]
    trade_rows = [{**row.to_dict(), **provenance} for row in trades]
    coverage = {
        "trading_day": day, "exchange": "DCE", "status": "PASS",
        "contract_count": len(parsed.frames), "record_count": len(position_rows),
        "source": DCE_DESKTOP_BATCH_SOURCE, "error": None,
        "collected_at": timestamp, **provenance,
    }
    return PreparedDceDesktopDay(
        trading_day=day, archive_sha256=parsed.archive_sha256,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        browser=manifest["browser"], source_page=page,
        entry_count=parsed.entry_count, ranked_contract_count=len(parsed.frames),
        positions=position_rows, trades=trade_rows, coverage=coverage,
    )


def _same_facts(prior: list[Mapping[str, Any]], current: list[Mapping[str, Any]], *, trade: bool) -> bool:
    fields = (("contract_code", "rank", "member_key", "volume", "volume_change")
              if trade else
              ("contract_code", "side", "rank", "member_key", "position", "position_change"))
    return Counter(tuple(row[field] for field in fields) for row in prior) == Counter(
        tuple(row[field] for field in fields) for row in current
    )


def _covered_by_complete_source(store: MarketStore, prepared: PreparedDceDesktopDay) -> str | None:
    """Skip equivalent completed sources; never import conflicting same-day facts."""

    day = prepared.trading_day
    coverage = store.list_futures_member_position_coverage(trading_day=day, exchange="DCE")
    positions = store.list_futures_member_position_ranks(trading_day=day, exchange="DCE")
    trades = store.list_futures_member_trade_ranks(trading_day=day, exchange="DCE")
    for source in (DCE_BATCH_SOURCE, DCE_DESKTOP_BATCH_SOURCE, "official-dce-member-ranking"):
        entries = [row for row in coverage if row["source"] == source and row["status"] == "PASS"]
        if not entries:
            continue
        old_positions = [row for row in positions if row["source"] == source]
        old_trades = [row for row in trades if row["source"] == source]
        if not old_positions or not old_trades:
            raise ValueError(f"DCE desktop batch complete {source} has no ranking facts for {day}")
        if source == "official-dce-member-ranking":
            _reconcile_authorized_overlap(store, prepared)
        elif (not _same_facts(old_positions, prepared.positions, trade=False)
              or not _same_facts(old_trades, prepared.trades, trade=True)):
            raise ValueError(f"DCE desktop batch differs from complete {source} for {day}")
        return source
    _reconcile_authorized_overlap(store, prepared)
    return None


def import_dce_desktop_batch_ranks(
    root: Path, *, start_day: str, end_day: str,
    max_days: int | None = None, write: bool = False,
) -> dict[str, Any]:
    """Validate a trading-date range; write only in an explicit lock window."""

    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or max_days is not None and max_days < 1:
        raise ValueError("invalid DCE desktop batch import range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified futures trading calendar has no days in range")
    selected = days[:max_days] if max_days is not None else days
    report: dict[str, Any] = {
        "status": "RUNNING", "source": DCE_DESKTOP_BATCH_SOURCE,
        "startDay": start.isoformat(), "endDay": end.isoformat(),
        "calendarDays": len(days), "selectedDays": len(selected),
        "verifiedDays": 0, "missingDays": 0, "importedDays": 0,
        "coveredDays": 0, "positionRows": 0, "tradeRows": 0,
        "authoritativeDataWritten": False,
    }

    def process(store: MarketStore | None) -> None:
        for day in selected:
            manifest = root / BRONZE / f"{day.replace('-', '')}.json"
            if not manifest.is_file():
                report["missingDays"] += 1
                continue
            prepared = prepare_dce_desktop_batch_day(root, day)
            report["verifiedDays"] += 1
            report["positionRows"] += len(prepared.positions)
            report["tradeRows"] += len(prepared.trades)
            if store is None:
                continue
            already = _covered_by_complete_source(store, prepared)
            if already is not None:
                report["coveredDays"] += 1
                report["currentDay"] = day
                continue
            store.replace_futures_member_rank_day_source(
                trading_day=day, exchange="DCE", source=DCE_DESKTOP_BATCH_SOURCE,
                positions=prepared.positions, trades=prepared.trades,
                coverage=prepared.coverage,
            )
            report["importedDays"] += 1
            report["authoritativeDataWritten"] = True
            _write_json(root / STATE / f"{day}.json", {
                **prepared.summary, "status": "PASS", "qualityStatus": "PASS",
                "authoritativeDataWritten": True, "importedAtUtc": _utc(),
            })
            report["currentDay"] = day
            _write_json(root / STATE / "progress.json", report)

    if not write:
        process(None)
    else:
        with _exclusive_authorized_writer_window(root / "state" / "dce_authorized_rank_writer.lock"):
            store: MarketStore | None = None
            try:
                store = MarketStore(root)
                process(store)
            except Exception as error:
                report.update(status="FAILED", errorType=type(error).__name__)
                raise
            finally:
                report["updatedAtUtc"] = _utc()
                _write_json(root / STATE / "progress.json", report)
                if store is not None:
                    store.close()
    report["status"] = (
        "PARTIAL_RAW" if report["missingDays"] else
        "BATCH_LIMIT" if len(selected) < len(days) else
        "PASS" if write else "VALIDATED"
    )
    if write:
        report["updatedAtUtc"] = _utc()
        _write_json(root / STATE / "progress.json", report)
    return report


__all__ = ("PreparedDceDesktopDay", "prepare_dce_desktop_batch_day", "import_dce_desktop_batch_ranks")
