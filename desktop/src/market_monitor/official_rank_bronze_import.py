"""Verify CFFEX XML/legacy reports and SHFE/INE JSON before ranking import.

Both collectors own immutable Bronze bytes.  This module only opens Catalog
after an explicit ``write`` request and under the shared single-writer lock.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .czce_html_rank_backfill import _read_json, _write_json
from .dce_batch_rank_import import _exclusive_authorized_writer_window
from .futures_calendar import load_futures_trading_calendar
from .futures_member_positions import normalise_exchange_member_position_ranks
from .futures_member_trade_ranks import normalise_exchange_member_trade_ranks
from .futures_official_member_sources import (
    cffex_rank_products, parse_cffex_rank_csv, parse_cffex_rank_xml,
    parse_shfe_rank_json,
)
from .shfe_rank_backfill import (
    ADAPTER as SHFE_CAPTURE_ADAPTER, BRONZE as SHFE_BRONZE,
    INE_PRODUCTS, SCHEMA as SHFE_CAPTURE_SCHEMA,
    _source_url as shfe_source_url, validate_shfe_rank,
)
from .storage import MarketStore


CFFEX_SOURCE = "official-cffex-published-member-ranking"
SHFE_SOURCE = "official-shfe-ine-dat-member-ranking"
CFFEX_CAPTURE_SCHEMA = "cffex-xml-rank-capture/v1"
CFFEX_BRONZE = Path("bronze") / "cffex_xml_rank_capture"
CFFEX_CAPTURE_STATE = Path("state") / "cffex_xml_rank_capture"
IMPORT_STATE = Path("state") / "cffex_shfe_rank_import"
IMPORT_ADAPTER = "official-cffex-shfe-rank-bronze-import-v1"
_LEGACY_URL = re.compile(r"/sj/ccpm/(20\d{4})/(\d{2})/([A-Z]+)(_1\.csv|\.xml)\Z")


@dataclass(frozen=True)
class PreparedRankDay:
    exchange: str
    trading_day: str
    source: str
    contract_count: int
    raw_rank_rows: int
    payload_sha256: str
    positions: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    coverage: dict[str, Any]

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange, "tradingDay": self.trading_day,
            "source": self.source, "contractCount": self.contract_count,
            "rawRankRows": self.raw_rank_rows,
            "positionRows": len(self.positions), "tradeRows": len(self.trades),
            "payloadSha256": self.payload_sha256,
        }


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} retrieval timestamp is absent")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} retrieval timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} retrieval timestamp has no timezone")
    return parsed.isoformat(timespec="seconds")


def _provenance(day: str, exchange: str, source: str, source_url: str,
                timestamp: str, digest: str) -> dict[str, Any]:
    return {
        "publisher": "CFFEX" if exchange == "CFFEX" else "SHFE",
        "source_url": source_url, "adapter": IMPORT_ADAPTER,
        "retrieved_at": timestamp, "payload_sha256": digest,
        "quality_status": "PASS", "legacy_adapter": None,
    }


def _prepared(exchange: str, day: str, source: str,
              frames: Mapping[str, Any], raw_rank_rows: int,
              digest: str, timestamp: str, source_url: str) -> PreparedRankDay:
    if not frames:
        raise ValueError(f"{exchange} {day} has no published contract frames")
    positions = normalise_exchange_member_position_ranks(
        frames, exchange=exchange, trading_day=day, source=source,
        collected_at=timestamp,
    )
    trades = normalise_exchange_member_trade_ranks(
        frames, exchange=exchange, trading_day=day, source=source,
        collected_at=timestamp,
    )
    own_positions = [row for row in positions if row.exchange == exchange]
    own_trades = [row for row in trades if row.exchange == exchange]
    if not own_positions or not own_trades:
        raise ValueError(f"{exchange} {day} lost a published ranking direction")
    metadata = _provenance(day, exchange, source, source_url, timestamp, digest)
    position_rows = [{**row.to_dict(), **metadata} for row in own_positions]
    trade_rows = [{**row.to_dict(), **metadata} for row in own_trades]
    coverage = {
        "trading_day": day, "exchange": exchange, "status": "PASS",
        "contract_count": len(frames), "record_count": len(position_rows),
        "source": source, "error": None, "collected_at": timestamp,
        **metadata,
    }
    return PreparedRankDay(exchange, day, source, len(frames), raw_rank_rows,
                           digest, position_rows, trade_rows, coverage)


def _cffex_url(day: str, product: str) -> str:
    compact = day.replace("-", "")
    return ("http://www.cffex.com.cn/sj/ccpm/"
            f"{compact[:6]}/{compact[6:]}/{product}.xml?id=0")


def _cffex_frames(raw: bytes, day: str, product: str, *, csv: bool) -> dict[str, Any]:
    compact = day.replace("-", "")
    frames = (parse_cffex_rank_csv(raw, trading_day=compact) if csv else
              parse_cffex_rank_xml(raw, trading_day=compact, product=product))
    if not frames or any(re.match(r"[A-Z]+", contract).group() != product for contract in frames):
        raise ValueError(f"CFFEX {day}/{product} has no matching published contracts")
    for contract, frame in frames.items():
        if frame.empty or frame["rank"].duplicated().any():
            raise ValueError(f"CFFEX {day}/{contract} has empty or repeated ranks")
        for column in ("vol_party_name", "long_party_name", "short_party_name"):
            if frame[column].isna().all():
                raise ValueError(f"CFFEX {day}/{contract} has no {column} direction")
    return frames


def _legacy_index(root: Path) -> dict[tuple[str, str], Path]:
    """Index old official artifacts by URL; each is validated when selected."""
    index: dict[tuple[str, str], Path] = {}
    for directory in (root / "bronze").glob("official_futures_member_cffex_*"):
        for path in directory.glob("*.json"):
            record = _read_json(path)
            if (not isinstance(record, dict) or record.get("publisher") != "CFFEX"
                    or not isinstance(record.get("contentBase64"), str)
                    or not isinstance(record.get("payloadSha256"), str)):
                continue
            url = str(record.get("sourceUrl") or "")
            if urlsplit(url).hostname not in {"cffex.com.cn", "www.cffex.com.cn"}:
                continue
            match = _LEGACY_URL.fullmatch(urlsplit(url).path)
            if match is not None:
                month, day, product, _ = match.groups()
                index[(f"{month[:4]}-{month[4:]}-{day}", product)] = path
    return index


def _read_legacy(root: Path, path: Path, day: str, product: str) -> tuple[bytes, str, str, str]:
    record = _read_json(path)
    if not isinstance(record, dict) or record.get("publisher") != "CFFEX":
        raise ValueError(f"CFFEX legacy provenance invalid for {day}/{product}")
    url = str(record.get("sourceUrl") or "")
    match = _LEGACY_URL.fullmatch(urlsplit(url).path)
    if urlsplit(url).hostname not in {"cffex.com.cn", "www.cffex.com.cn"} or match is None:
        raise ValueError(f"CFFEX legacy URL invalid for {day}/{product}")
    month, day_of_month, url_product, extension = match.groups()
    if (f"{month[:4]}-{month[4:]}-{day_of_month}", url_product) != (day, product):
        raise ValueError(f"CFFEX legacy URL date/product differs for {day}/{product}")
    try:
        raw = base64.b64decode(record["contentBase64"], validate=True)
    except (KeyError, TypeError, binascii.Error) as error:
        raise ValueError(f"CFFEX legacy payload invalid for {day}/{product}") from error
    digest = sha256(raw).hexdigest()
    if not raw or record.get("payloadSha256") != digest:
        raise ValueError(f"CFFEX legacy SHA differs for {day}/{product}")
    # The old direct adapter serialized this timestamp without a timezone on
    # some Windows runs. Preserve the original string rather than inventing one.
    captured = str(record.get("retrievedAt") or "")
    if not captured:
        raise ValueError(f"CFFEX legacy capture timestamp absent for {day}/{product}")
    return raw, digest, captured, extension


def prepare_cffex_rank_day(
    root: Path, trading_day: str, *, legacy_index: Mapping[tuple[str, str], Path] | None = None,
) -> PreparedRankDay:
    day = date.fromisoformat(trading_day).isoformat()
    root = Path(root)
    products = cffex_rank_products(day.replace("-", ""))
    state = _read_json(root / CFFEX_CAPTURE_STATE / f"{day.replace('-', '')}.json")
    if state is not None and (not isinstance(state, dict) or any((
        state.get("schema") != CFFEX_CAPTURE_SCHEMA,
        state.get("publisher") != "CFFEX", state.get("tradingDay") != day,
        state.get("status") != "PASS_RAW",
        state.get("authoritativeDataWritten") is not False,
        state.get("expectedProducts") != list(products),
        not isinstance(state.get("products"), dict),
    ))):
        raise ValueError(f"CFFEX {day} is not a complete PASS_RAW day")
    indexed = legacy_index if legacy_index is not None else _legacy_index(root)
    if state is None and any((day, product) not in indexed for product in products):
        raise FileNotFoundError(f"CFFEX {day} lacks a complete dated capture or legacy artifact")
    checkpoints = state["products"] if state else {}
    if state is not None and set(checkpoints) != set(products):
        raise ValueError(f"CFFEX {day} product checkpoint differs from expected products")
    frames: dict[str, Any] = {}
    digests: list[str] = []
    timestamps: list[str] = []
    rank_rows = 0
    for product in products:
        checkpoint = checkpoints.get(product)
        own_base = root / CFFEX_BRONZE / day / product
        manifest: dict[str, Any] | None = None
        if checkpoint is not None and checkpoint.get("status") == "NO_PUBLICATION":
            marker = _read_json(own_base.with_suffix(".json"))
            if not isinstance(marker, dict) or any((
                marker.get("schema") != CFFEX_CAPTURE_SCHEMA,
                marker.get("status") != "NO_PUBLICATION",
                marker.get("tradingDay") != day,
                marker.get("product") != product,
                marker.get("sourceUrl") != _cffex_url(day, product),
                marker.get("httpStatus") != 404,
                marker.get("authoritativeDataWritten") is not False,
            )):
                raise ValueError(f"CFFEX {day}/{product} 404 marker differs")
            continue
        if checkpoint is not None and checkpoint.get("status") != "PASS_RAW":
            raise ValueError(f"CFFEX {day}/{product} is not PASS_RAW")
        if checkpoint and str(checkpoint.get("sourcePath", "")).startswith(
            CFFEX_BRONZE.as_posix() + "/"
        ):
            manifest = _read_json(own_base.with_suffix(".json"))
            if not isinstance(manifest, dict) or any((
                manifest.get("schema") != CFFEX_CAPTURE_SCHEMA,
                manifest.get("status") != "PASS_RAW", manifest.get("publisher") != "CFFEX",
                manifest.get("tradingDay") != day, manifest.get("product") != product,
                manifest.get("sourceUrl") != _cffex_url(day, product),
                manifest.get("rawPath") != own_base.with_suffix(".xml").relative_to(root).as_posix(),
                manifest.get("authoritativeDataWritten") is not False,
            )):
                raise ValueError(f"CFFEX {day}/{product} raw manifest differs")
            raw = own_base.with_suffix(".xml").read_bytes()
            digest = sha256(raw).hexdigest()
            if (manifest.get("payloadSha256") != digest or
                    manifest.get("byteCount") != len(raw)):
                raise ValueError(f"CFFEX {day}/{product} raw bytes or SHA differs")
            timestamp = _timestamp(manifest.get("retrievedAt"), f"CFFEX {day}/{product}")
            csv = False
        else:
            path = indexed.get((day, product))
            if path is None:
                raise FileNotFoundError(f"CFFEX {day}/{product} legacy artifact absent")
            raw, digest, timestamp, extension = _read_legacy(root, path, day, product)
            csv = extension.endswith(".csv")
        parsed = _cffex_frames(raw, day, product, csv=csv)
        rows = sum(len(frame) for frame in parsed.values())
        if checkpoint and any((
            checkpoint.get("payloadSha256") != digest,
            checkpoint.get("contractCount") != len(parsed),
            checkpoint.get("rankRows") != rows,
        )):
            raise ValueError(f"CFFEX {day}/{product} rank checkpoint differs")
        if checkpoint and not csv and manifest is not None and any((
            manifest.get("contractCount") != len(parsed),
            manifest.get("rankRows") != rows,
        )):
            raise ValueError(f"CFFEX {day}/{product} raw count differs")
        if set(frames) & set(parsed):
            raise ValueError(f"CFFEX {day}/{product} repeats a contract")
        frames.update(parsed)
        rank_rows += rows
        digests.append(digest)
        timestamps.append(timestamp)
    if not frames:
        raise ValueError(f"CFFEX {day} has no published ranked contracts")
    timestamp = max(timestamps)
    day_digest = sha256("\n".join(digests).encode("ascii")).hexdigest()
    prepared = _prepared("CFFEX", day, CFFEX_SOURCE, frames, rank_rows,
                         day_digest, timestamp, "http://www.cffex.com.cn/cn/ccpm.html")
    if len(prepared.trades) < len(frames) or len(prepared.positions) < 2 * len(frames):
        raise ValueError(f"CFFEX {day} lost a contract direction during normalization")
    return prepared


def prepare_shfe_rank_day(root: Path, trading_day: str) -> tuple[PreparedRankDay, ...]:
    day = date.fromisoformat(trading_day).isoformat()
    root = Path(root)
    base = root / SHFE_BRONZE / day
    manifest = _read_json(base.with_suffix(".json"))
    raw_path = base.with_suffix(".dat")
    if not isinstance(manifest, dict) or any((
        manifest.get("schema") != SHFE_CAPTURE_SCHEMA,
        manifest.get("publisher") != ["SHFE", "INE"],
        manifest.get("adapter") != SHFE_CAPTURE_ADAPTER,
        manifest.get("tradingDay") != day,
        manifest.get("sourceUrl") != shfe_source_url(day),
        manifest.get("rawPath") != raw_path.relative_to(root).as_posix(),
        manifest.get("httpStatus") != 200,
        manifest.get("status") != "PASS_RAW",
        manifest.get("qualityStatus") != "PASS_RAW",
        manifest.get("authoritativeDataWritten") is not False,
        "json" not in str(manifest.get("contentType") or "").lower(),
    )):
        raise ValueError(f"SHFE {day} manifest is not PASS_RAW official JSON")
    raw = raw_path.read_bytes()
    digest = sha256(raw).hexdigest()
    if manifest.get("payloadSha256") != digest or manifest.get("bytes") != len(raw):
        raise ValueError(f"SHFE {day} raw bytes or SHA differs")
    summary = validate_shfe_rank(raw, day)
    if manifest.get("summary") != summary:
        raise ValueError(f"SHFE {day} summary differs from official JSON")
    timestamp = _timestamp(manifest.get("retrievedAt"), f"SHFE {day}")
    frames = parse_shfe_rank_json(raw)
    outputs: list[PreparedRankDay] = []
    for exchange in ("SHFE", "INE"):
        selected = {
            contract: frame for contract, frame in frames.items()
            if ((re.match(r"[A-Z]+", contract).group() in INE_PRODUCTS) == (exchange == "INE"))
        }
        expected = summary["venues"][exchange]
        if len(selected) != expected["contractCount"] or sum(map(len, selected.values())) != expected["rankingRows"]:
            raise ValueError(f"SHFE {day} {exchange} contract split differs")
        if not selected:
            # A genuinely unlisted venue is not a fabricated zero coverage row.
            continue
        for contract, frame in selected.items():
            if frame["rank"].duplicated().any() or frame["rank"].lt(1).any():
                raise ValueError(f"SHFE {day}/{contract} has duplicate or invalid ranks")
        prepared = _prepared(
            exchange, day, SHFE_SOURCE, selected, expected["rankingRows"],
            digest, timestamp, shfe_source_url(day),
        )
        published_contracts = {
            row["contract_code"] for row in (*prepared.positions, *prepared.trades)
        }
        if published_contracts != set(selected):
            raise ValueError(f"SHFE {day}/{exchange} lost a published contract")
        outputs.append(prepared)
    return tuple(outputs)


def _already_imported(store: MarketStore, root: Path, prepared: PreparedRankDay) -> bool:
    checkpoint = _read_json(
        root / IMPORT_STATE / prepared.exchange.lower() / f"{prepared.trading_day}.json"
    )
    if not isinstance(checkpoint, dict) or any((
        checkpoint.get("status") != "PASS",
        checkpoint.get("qualityStatus") != "PASS",
        checkpoint.get("authoritativeDataWritten") is not True,
        any(checkpoint.get(key) != value for key, value in prepared.summary.items()),
    )):
        return False
    coverage = [
        row for row in store.list_futures_member_position_coverage(
            trading_day=prepared.trading_day, exchange=prepared.exchange,
        ) if row["source"] == prepared.source
    ]
    if len(coverage) != 1 or any((
        coverage[0]["status"] != "PASS",
        coverage[0]["record_count"] != len(prepared.positions),
        coverage[0]["contract_count"] != prepared.contract_count,
        coverage[0]["payload_sha256"] != prepared.payload_sha256,
    )):
        return False
    positions = store.list_futures_member_position_ranks(
        trading_day=prepared.trading_day, exchange=prepared.exchange,
    )
    trades = store.list_futures_member_trade_ranks(
        trading_day=prepared.trading_day, exchange=prepared.exchange,
    )
    return (sum(row["source"] == prepared.source for row in positions) == len(prepared.positions)
            and sum(row["source"] == prepared.source for row in trades) == len(prepared.trades))


def import_official_rank_bronze(
    root: Path, *, exchange: str, start_day: str = "2023-01-01",
    end_day: str, max_days: int | None = None, write: bool = False,
) -> dict[str, Any]:
    """Audit a trading-day range, or import CFFEX / SHFE+INE as one writer."""
    if exchange not in {"CFFEX", "SHFE"}:
        raise ValueError("exchange must be CFFEX or SHFE (including INE)")
    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or max_days is not None and max_days < 1:
        raise ValueError("invalid official rank import range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified CN futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified calendar has no trading days in range")
    selected = days[:max_days] if not write and max_days else days
    legacy = _legacy_index(root) if exchange == "CFFEX" else None
    report: dict[str, Any] = {
        "exchange": exchange, "source": CFFEX_SOURCE if exchange == "CFFEX" else SHFE_SOURCE,
        "startDay": start.isoformat(), "endDay": end.isoformat(),
        "calendarDays": len(days), "selectedDays": len(selected),
        "verifiedDays": 0, "missingDays": 0, "importedVenueDays": 0,
        "resumedVenueDays": 0, "contractDays": 0, "positionRows": 0,
        "tradeRows": 0, "authoritativeDataWritten": False,
        "status": "RUNNING",
    }

    def process(store: MarketStore | None) -> None:
        for day in selected:
            report["currentDay"] = day
            try:
                prepared_days = (
                    (prepare_cffex_rank_day(root, day, legacy_index=legacy),)
                    if exchange == "CFFEX" else prepare_shfe_rank_day(root, day)
                )
            except FileNotFoundError:
                report["missingDays"] += 1
                continue
            report["verifiedDays"] += 1
            report["contractDays"] += sum(item.contract_count for item in prepared_days)
            report["positionRows"] += sum(len(item.positions) for item in prepared_days)
            report["tradeRows"] += sum(len(item.trades) for item in prepared_days)
            if store is None:
                continue
            for prepared in prepared_days:
                if _already_imported(store, root, prepared):
                    report["resumedVenueDays"] += 1
                    continue
                if max_days is not None and report["importedVenueDays"] >= max_days:
                    report["status"] = "BATCH_LIMIT"
                    return
                store.replace_futures_member_rank_day_source(
                    trading_day=day, exchange=prepared.exchange, source=prepared.source,
                    positions=prepared.positions, trades=prepared.trades,
                    coverage=prepared.coverage,
                )
                report["importedVenueDays"] += 1
                report["authoritativeDataWritten"] = True
                _write_json(
                    root / IMPORT_STATE / prepared.exchange.lower() / f"{day}.json",
                    {**prepared.summary, "status": "PASS", "qualityStatus": "PASS",
                     "authoritativeDataWritten": True,
                     "importedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                )
                report["updatedAtUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _write_json(root / IMPORT_STATE / "progress.json", report)

    if not write:
        process(None)
    else:
        with _exclusive_authorized_writer_window(root / "state" / "dce_authorized_rank_writer.lock"):
            store = MarketStore(root)
            try:
                process(store)
            except Exception as error:
                report.update(status="FAILED", errorDay=report.get("currentDay"),
                              errorType=type(error).__name__)
                raise
            finally:
                report["updatedAtUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _write_json(root / IMPORT_STATE / "progress.json", report)
                store.close()
    if report["status"] == "RUNNING":
        report["status"] = ("PARTIAL_RAW" if report["missingDays"] else
                            "BATCH_LIMIT" if len(selected) < len(days) else
                            "PASS" if write else "VALIDATED")
    if write:
        _write_json(root / IMPORT_STATE / "progress.json", report)
    return report


__all__ = (
    "CFFEX_SOURCE", "SHFE_SOURCE", "PreparedRankDay", "prepare_cffex_rank_day",
    "prepare_shfe_rank_day", "import_official_rank_bronze",
)
