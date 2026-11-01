"""Validate official DCE daily ranking ZIPs before source-isolated fact import.

The Android browser collector owns Bronze. This module never downloads data or
reads browser state. Database writes require an explicit call with ``write``
and a separate single-writer window after the authorized collector has stopped.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import os
from pathlib import Path
from typing import Any, Iterator

from .czce_html_rank_backfill import _read_json, _write_json
from .dce_batch_zip import DceBatchZipResult, parse_dce_batch_zip
from .futures_calendar import load_futures_trading_calendar
from .futures_member_positions import normalise_exchange_member_position_ranks
from .futures_member_rank_source import DCE_BATCH_SOURCE
from .futures_member_trade_ranks import normalise_exchange_member_trade_ranks
from .storage import MarketStore


BRONZE = Path("bronze") / "dce_dom_capture"
STATE = Path("state") / "dce_batch_rank_import"
CAPTURE_SCHEMA = "dce-android-batch-capture/v1"
CAPTURE_ADAPTER = "official-dce-android-browser-batch-raw-v1"
IMPORT_ADAPTER = "official-dce-android-browser-batch-v2"
PAGE = "http://www.dce.com.cn/dce/channel/list/176.html"
BATCH_URL = "http://www.dce.com.cn/dcereport/publicweb/dailystat/memberDealPosi/batchDownload"


@dataclass(frozen=True)
class PreparedDceBatchDay:
    trading_day: str
    archive_sha256: str
    entry_count: int
    ranked_contract_count: int
    empty_contract_count: int
    excluded_contracts: tuple[str, ...]
    positions: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    coverage: dict[str, Any]

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "tradingDay": self.trading_day,
            "archiveSha256": self.archive_sha256,
            "entryCount": self.entry_count,
            "rankedContractCount": self.ranked_contract_count,
            "emptyContractCount": self.empty_contract_count,
            "excludedContractCount": len(self.excluded_contracts),
            "excludedContracts": list(self.excluded_contracts),
            "positionRows": len(self.positions),
            "tradeRows": len(self.trades),
            "source": DCE_BATCH_SOURCE,
        }


def prepare_dce_batch_day(root: Path, trading_day: str) -> PreparedDceBatchDay:
    """Require a matching PASS_RAW manifest, byte hash and complete ZIP report."""

    day = date.fromisoformat(trading_day).isoformat()
    compact = day.replace("-", "")
    base = Path(root) / BRONZE / compact
    manifest = _read_json(base.with_suffix(".json"))
    if manifest is None:
        raise FileNotFoundError(f"DCE batch Bronze manifest is missing for {day}")
    if not isinstance(manifest, dict) or any((
        manifest.get("source") != PAGE,
        manifest.get("batchPath") != "/dcereport/publicweb/dailystat/memberDealPosi/batchDownload",
        manifest.get("tradeDate") != day,
        manifest.get("httpStatus") != 200,
        manifest.get("status") != "PASS_RAW",
        manifest.get("authoritativeDataWritten") is not False,
    )):
        raise ValueError(f"DCE batch Bronze provenance or PASS_RAW state differs for {day}")
    raw = base.with_suffix(".zip").read_bytes()
    parsed: DceBatchZipResult = parse_dce_batch_zip(raw, trading_day=compact)
    if parsed.entry_count != len(parsed.frames) + len(parsed.empty_contracts) + len(parsed.excluded_contracts):
        raise ValueError(f"DCE batch entry coverage is inconsistent for {day}")
    if manifest.get("bytes") != len(raw):
        raise ValueError(f"DCE batch Bronze manifest does not match ZIP bytes for {day}")
    if manifest.get("schema") == CAPTURE_SCHEMA:
        if any((
            manifest.get("publisher") != "DCE",
            manifest.get("adapter") != CAPTURE_ADAPTER,
            "zip" not in str(manifest.get("contentType") or "").lower(),
            manifest.get("qualityStatus") != "PASS_RAW",
            manifest.get("payloadSha256") != parsed.archive_sha256,
            manifest.get("summary") != parsed.summary,
        )):
            raise ValueError(f"DCE batch Bronze manifest does not match ZIP bytes for {day}")
    elif manifest.get("schema") is None:
        audit = manifest.get("audit") if isinstance(manifest.get("audit"), dict) else manifest
        if any((
            manifest.get("method") not in {
                "LDPlayer page fetch + CDP Fetch",
                "LDPlayer Android browser DOM click + CDP Fetch",
            },
            manifest.get("sha256") != parsed.archive_sha256,
            manifest.get("entries") != parsed.entry_count,
            audit.get("auditVersion") != "dce_ldplayer_zip_v1",
            audit.get("uniqueContracts") != parsed.entry_count,
            audit.get("allDateContractAndThreeSections") is not True,
            audit.get("crcOk") is not True,
            audit.get("issueCount", 0) != 0,
        )):
            raise ValueError(f"DCE batch legacy Bronze audit does not match ZIP bytes for {day}")
    else:
        raise ValueError(f"DCE batch Bronze manifest schema is unsupported for {day}")
    captured_at = manifest.get("capturedAtUtc")
    try:
        timestamp = datetime.fromisoformat(captured_at)
    except (TypeError, ValueError) as error:
        raise ValueError(f"DCE batch capture time is invalid for {day}") from error
    if timestamp.tzinfo is None:
        raise ValueError(f"DCE batch capture time lacks timezone for {day}")
    retrieved_at = timestamp.isoformat(timespec="seconds")
    # Validate immutable Bronze evidence with its original parser scope, then
    # enlarge the derived futures universe without rewriting the manifest.
    parsed = parse_dce_batch_zip(raw, trading_day=compact, include_monthly_average=True)
    positions = normalise_exchange_member_position_ranks(
        parsed.frames, exchange="DCE", trading_day=day,
        source=DCE_BATCH_SOURCE, collected_at=retrieved_at,
    )
    trades = normalise_exchange_member_trade_ranks(
        parsed.frames, exchange="DCE", trading_day=day,
        source=DCE_BATCH_SOURCE, collected_at=retrieved_at,
    )
    volume_count, long_count, short_count = parsed.row_counts
    if (not positions or not trades
            or len(positions) != long_count + short_count
            or len(trades) != volume_count):
        raise ValueError(f"DCE batch normalization lost a published contract or direction for {day}")
    provenance = {
        "publisher": "DCE", "source_url": BATCH_URL, "adapter": IMPORT_ADAPTER,
        "retrieved_at": retrieved_at, "payload_sha256": parsed.archive_sha256,
        "quality_status": "PASS", "legacy_adapter": None,
    }
    position_rows = [{**row.to_dict(), **provenance} for row in positions]
    trade_rows = [{**row.to_dict(), **provenance} for row in trades]
    coverage = {
        "trading_day": day, "exchange": "DCE", "status": "PASS",
        "contract_count": len(parsed.frames), "record_count": len(position_rows),
        "source": DCE_BATCH_SOURCE, "error": None, "collected_at": retrieved_at,
        **provenance,
    }
    return PreparedDceBatchDay(
        trading_day=day, archive_sha256=parsed.archive_sha256,
        entry_count=parsed.entry_count, ranked_contract_count=len(parsed.frames),
        empty_contract_count=len(parsed.empty_contracts),
        excluded_contracts=parsed.excluded_contracts,
        positions=position_rows, trades=trade_rows, coverage=coverage,
    )


def import_dce_batch_ranks(
    root: Path, *, start_day: str, end_day: str,
    max_days: int | None = None, write: bool = False,
) -> dict[str, Any]:
    """Inspect requested trading days; write only under explicit single-writer use."""

    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or max_days is not None and max_days < 1:
        raise ValueError("invalid DCE batch import range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified futures trading calendar has no days in range")
    report: dict[str, Any] = {
        "status": "RUNNING", "source": DCE_BATCH_SOURCE,
        "startDay": start.isoformat(), "endDay": end.isoformat(),
        "calendarDays": len(days), "verifiedDays": 0, "missingDays": 0,
        "importedDays": 0, "positionRows": 0, "tradeRows": 0,
        "authoritativeDataWritten": False,
    }
    selected = days[:max_days] if max_days is not None else days
    if not write:
        for day in selected:
            if not (root / BRONZE / f"{day.replace('-', '')}.json").is_file():
                report["missingDays"] += 1
                continue
            prepared = prepare_dce_batch_day(root, day)
            report["verifiedDays"] += 1
            report["positionRows"] += len(prepared.positions)
            report["tradeRows"] += len(prepared.trades)
        report["status"] = (
            "PARTIAL_RAW" if report["missingDays"] else
            "BATCH_LIMIT" if len(selected) < len(days) else "VALIDATED"
        )
        report["selectedDays"] = len(selected)
        return report

    state = root / STATE
    with _exclusive_authorized_writer_window(root / "state" / "dce_authorized_rank_writer.lock"):
        store: MarketStore | None = None
        try:
            for day in selected:
                if not (root / BRONZE / f"{day.replace('-', '')}.json").is_file():
                    report["missingDays"] += 1
                    continue
                prepared = prepare_dce_batch_day(root, day)
                report["verifiedDays"] += 1
                if store is None:
                    store = MarketStore(root)
                _reconcile_authorized_overlap(store, prepared)
                position_count, trade_count = store.replace_futures_member_rank_day_source(
                    trading_day=day, exchange="DCE", source=DCE_BATCH_SOURCE,
                    positions=prepared.positions, trades=prepared.trades,
                    coverage=prepared.coverage,
                )
                report["importedDays"] += 1
                report["positionRows"] += position_count
                report["tradeRows"] += trade_count
                report["authoritativeDataWritten"] = True
                _write_json(state / f"{day}.json", {
                    **prepared.summary, "status": "PASS", "qualityStatus": "PASS",
                    "authoritativeDataWritten": True, "importedAtUtc": _utc(),
                })
                report["currentDay"] = day
                _write_json(state / "progress.json", report)
            report["status"] = (
                "PARTIAL_RAW" if report["missingDays"] else
                "BATCH_LIMIT" if len(selected) < len(days) else "PASS"
            )
        except Exception as error:
            report.update(status="FAILED", errorDay=day, errorType=type(error).__name__)
            raise
        finally:
            report["updatedAtUtc"] = _utc()
            _write_json(state / "progress.json", report)
            if store is not None:
                store.close()
    return report


@contextmanager
def _exclusive_authorized_writer_window(path: Path) -> Iterator[None]:
    """Fail fast if the authorized DCE worker already owns the Catalog window."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("DCE_AUTHORIZED_WRITER_ACTIVE") from error
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("DCE_AUTHORIZED_WRITER_ACTIVE") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reconcile_authorized_overlap(store: MarketStore, prepared: PreparedDceBatchDay) -> None:
    """Refuse differing facts, allowing only rank swaps within equal quantities.

    The two official DCE routes may order members with the same quantity in a
    different way. The published quantity at each rank and each member's own
    quantity/change must still match exactly.
    """

    day = prepared.trading_day
    authorized = "official-dce-member-ranking"
    prior_positions = [
        row for row in store.list_futures_member_position_ranks(trading_day=day, exchange="DCE")
        if row["source"] == authorized
    ]
    prior_trades = [
        row for row in store.list_futures_member_trade_ranks(trading_day=day, exchange="DCE")
        if row["source"] == authorized
    ]
    if not prior_positions and not prior_trades:
        return
    authorized_complete = any(
        row["source"] == authorized and row["status"] == "PASS"
        for row in store.list_futures_member_position_coverage(trading_day=day, exchange="DCE")
    )
    for label, prior, batch, partition_fields, quantity_field, change_field in (
        ("position", prior_positions, prepared.positions,
         ("contract_code", "side"), "position", "position_change"),
        ("trade", prior_trades, prepared.trades,
         ("contract_code",), "volume", "volume_change"),
    ):
        def keys(rows: list[dict[str, Any]], *, by_rank: bool) -> Counter[tuple[Any, ...]]:
            return Counter(
                tuple(row[field] for field in partition_fields)
                + ((row["rank"], row[quantity_field]) if by_rank else
                   (row["member_key"], row[quantity_field], row[change_field]))
                for row in rows
            )

        for by_rank in (True, False):
            old_keys = keys(prior, by_rank=by_rank)
            batch_keys = keys(batch, by_rank=by_rank)
            if old_keys - batch_keys:
                raise ValueError(f"DCE batch {label} differs from authorized source for {day}")
            if authorized_complete and batch_keys - old_keys:
                raise ValueError(
                    f"DCE batch {label} contract coverage differs from complete authorized source for {day}"
                )


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ("PreparedDceBatchDay", "prepare_dce_batch_day", "import_dce_batch_ranks")
