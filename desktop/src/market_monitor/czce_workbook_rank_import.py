"""Validate CZCE daily workbooks and import source-isolated member rankings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .czce_html_rank_backfill import _read_json, _write_json
from .czce_rank_workbook import parse_czce_rank_workbook_checked
from .czce_workbook_rank_backfill import ADAPTER as CAPTURE_ADAPTER, SCHEMA as CAPTURE_SCHEMA, _source_url
from .dce_batch_rank_import import _exclusive_authorized_writer_window
from .futures_calendar import load_futures_trading_calendar
from .futures_member_positions import normalise_exchange_member_position_ranks
from .futures_member_rank_source import CZCE_WORKBOOK_SOURCE
from .futures_member_trade_ranks import normalise_exchange_member_trade_ranks
from .storage import MarketStore


BRONZE = Path("bronze") / "czce_workbook_rank_capture"
STATE = Path("state") / "czce_workbook_rank_import"
IMPORT_ADAPTER = "official-czce-rank-workbook-import-v1"


@dataclass(frozen=True)
class PreparedCzceWorkbookDay:
    trading_day: str
    payload_sha256: str
    workbook_format: str
    contract_count: int
    ranking_rows: int
    positions: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    coverage: dict[str, Any]

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "tradingDay": self.trading_day,
            "payloadSha256": self.payload_sha256,
            "workbookFormat": self.workbook_format,
            "contractCount": self.contract_count,
            "rankingRows": self.ranking_rows,
            "positionRows": len(self.positions),
            "tradeRows": len(self.trades),
            "source": CZCE_WORKBOOK_SOURCE,
        }


def prepare_czce_workbook_day(root: Path, trading_day: str) -> PreparedCzceWorkbookDay:
    """Require official Bronze provenance, byte hash, report date and row counts."""

    day = date.fromisoformat(trading_day).isoformat()
    base = Path(root) / BRONZE / day
    manifest = _read_json(base.with_suffix(".json"))
    if manifest is None:
        raise FileNotFoundError(f"CZCE workbook Bronze manifest is missing for {day}")
    if not isinstance(manifest, dict):
        raise ValueError(f"CZCE workbook Bronze manifest is malformed for {day}")
    source_url = manifest.get("sourceUrl")
    format_name = str(source_url or "").rsplit(".", 1)[-1]
    if format_name not in {"xls", "xlsx"}:
        raise ValueError(f"CZCE workbook Bronze format is unsupported for {day}")
    raw_path = base.with_suffix(f".{format_name}")
    if any((
        manifest.get("schema") != CAPTURE_SCHEMA,
        manifest.get("publisher") != "CZCE",
        manifest.get("adapter") != CAPTURE_ADAPTER,
        manifest.get("tradingDay") != day,
        source_url != _source_url(day, format_name),
        manifest.get("rawPath") != raw_path.relative_to(Path(root)).as_posix(),
        manifest.get("httpStatus") != 200,
        manifest.get("status") != "PASS_RAW",
        manifest.get("qualityStatus") != "PASS_RAW",
        manifest.get("authoritativeDataWritten") is not False,
    )):
        raise ValueError(f"CZCE workbook Bronze provenance or PASS_RAW state differs for {day}")
    try:
        retrieved_at = datetime.fromisoformat(str(manifest.get("retrievedAt")))
    except ValueError as error:
        raise ValueError(f"CZCE workbook capture time is invalid for {day}") from error
    if retrieved_at.tzinfo is None:
        raise ValueError(f"CZCE workbook capture time lacks timezone for {day}")
    raw = raw_path.read_bytes()
    digest = sha256(raw).hexdigest()
    if manifest.get("bytes") != len(raw) or manifest.get("payloadSha256") != digest:
        raise ValueError(f"CZCE workbook Bronze bytes or SHA256 differs for {day}")
    frames = parse_czce_rank_workbook_checked(raw, trading_day=day.replace("-", ""))
    rank_count = sum(len(frame) for frame in frames.values())
    timestamp = retrieved_at.isoformat(timespec="seconds")
    positions = normalise_exchange_member_position_ranks(
        frames, exchange="CZCE", trading_day=day,
        source=CZCE_WORKBOOK_SOURCE, collected_at=timestamp,
    )
    trades = normalise_exchange_member_trade_ranks(
        frames, exchange="CZCE", trading_day=day,
        source=CZCE_WORKBOOK_SOURCE, collected_at=timestamp,
    )
    if (not frames or not positions or not trades or any((
        manifest.get("contractCount") != len(frames),
        manifest.get("rankingRows") != rank_count,
        manifest.get("positionRows") != len(positions),
        manifest.get("tradeRows") != len(trades),
    ))):
        raise ValueError(f"CZCE workbook manifest row counts differ from parsed report for {day}")
    provenance = {
        "publisher": "CZCE", "source_url": source_url,
        "adapter": IMPORT_ADAPTER, "retrieved_at": timestamp,
        "payload_sha256": digest, "quality_status": "PASS",
        "legacy_adapter": None,
    }
    position_rows = [{**row.to_dict(), **provenance} for row in positions]
    trade_rows = [{**row.to_dict(), **provenance} for row in trades]
    coverage = {
        "trading_day": day, "exchange": "CZCE", "status": "PASS",
        "contract_count": len(frames), "record_count": len(position_rows),
        "source": CZCE_WORKBOOK_SOURCE, "error": None,
        "collected_at": timestamp, **provenance,
    }
    return PreparedCzceWorkbookDay(
        trading_day=day, payload_sha256=digest, workbook_format=format_name,
        contract_count=len(frames), ranking_rows=rank_count,
        positions=position_rows, trades=trade_rows, coverage=coverage,
    )


def _already_imported(store: MarketStore, root: Path, prepared: PreparedCzceWorkbookDay) -> bool:
    day = prepared.trading_day
    checkpoint = _read_json(root / STATE / f"{day}.json")
    if not isinstance(checkpoint, dict) or any((
        checkpoint.get("status") != "PASS",
        checkpoint.get("qualityStatus") != "PASS",
        checkpoint.get("authoritativeDataWritten") is not True,
        any(checkpoint.get(key) != value for key, value in prepared.summary.items()),
    )):
        return False
    coverage = [
        row for row in store.list_futures_member_position_coverage(trading_day=day, exchange="CZCE")
        if row["source"] == CZCE_WORKBOOK_SOURCE
    ]
    if len(coverage) != 1 or any((
        coverage[0]["status"] != "PASS",
        coverage[0]["record_count"] != len(prepared.positions),
        coverage[0]["contract_count"] != prepared.contract_count,
        coverage[0]["payload_sha256"] != prepared.payload_sha256,
    )):
        return False
    position_count = sum(
        row["source"] == CZCE_WORKBOOK_SOURCE
        for row in store.list_futures_member_position_ranks(trading_day=day, exchange="CZCE")
    )
    trade_count = sum(
        row["source"] == CZCE_WORKBOOK_SOURCE
        for row in store.list_futures_member_trade_ranks(trading_day=day, exchange="CZCE")
    )
    return position_count == len(prepared.positions) and trade_count == len(prepared.trades)


def import_czce_workbook_ranks(
    root: Path, *, start_day: str = "2023-01-01", end_day: str = "2026-09-30",
    max_days: int | None = None, write: bool = False,
) -> dict[str, Any]:
    """Audit a date range, or import verified days under the single-writer lock."""

    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or max_days is not None and max_days < 1:
        raise ValueError("invalid CZCE workbook import range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified futures trading calendar has no days in range")
    report: dict[str, Any] = {
        "status": "RUNNING", "source": CZCE_WORKBOOK_SOURCE,
        "startDay": start.isoformat(), "endDay": end.isoformat(),
        "calendarDays": len(days), "verifiedDays": 0, "missingDays": 0,
        "importedDays": 0, "resumedDays": 0,
        "positionRows": 0, "tradeRows": 0,
        "authoritativeDataWritten": False,
    }
    if not write:
        selected = days[:max_days] if max_days is not None else days
        for day in selected:
            if not (root / BRONZE / f"{day}.json").is_file():
                report["missingDays"] += 1
                continue
            prepared = prepare_czce_workbook_day(root, day)
            report["verifiedDays"] += 1
            report["positionRows"] += len(prepared.positions)
            report["tradeRows"] += len(prepared.trades)
        report["selectedDays"] = len(selected)
        report["status"] = (
            "PARTIAL_RAW" if report["missingDays"] else
            "BATCH_LIMIT" if len(selected) < len(days) else "VALIDATED"
        )
        return report

    state = root / STATE
    writer_lock = root / "state" / "dce_authorized_rank_writer.lock"
    with _exclusive_authorized_writer_window(writer_lock):
        store: MarketStore | None = None
        try:
            for day in days:
                report["currentDay"] = day
                if not (root / BRONZE / f"{day}.json").is_file():
                    report["missingDays"] += 1
                    continue
                prepared = prepare_czce_workbook_day(root, day)
                report["verifiedDays"] += 1
                if store is None:
                    store = MarketStore(root)
                if _already_imported(store, root, prepared):
                    report["resumedDays"] += 1
                    continue
                if max_days is not None and report["importedDays"] >= max_days:
                    report["status"] = "BATCH_LIMIT"
                    break
                position_count, trade_count = store.replace_futures_member_rank_day_source(
                    trading_day=day, exchange="CZCE", source=CZCE_WORKBOOK_SOURCE,
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
                report["updatedAtUtc"] = _utc()
                _write_json(state / "progress.json", report)
            else:
                report["status"] = "PARTIAL_RAW" if report["missingDays"] else "PASS"
        except Exception as error:
            report.update(status="FAILED", errorDay=day, errorType=type(error).__name__)
            raise
        finally:
            report["updatedAtUtc"] = _utc()
            _write_json(state / "progress.json", report)
            if store is not None:
                store.close()
    return report


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ("PreparedCzceWorkbookDay", "prepare_czce_workbook_day", "import_czce_workbook_ranks")
