"""Validate GFEX member-ranking Bronze and import one source/day atomically.

The raw collector writes an official product list, dated contract lists, and
three direction responses per published contract.  A dated empty contract list
is evidence of unavailable rankings, never a zero member position.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .dce_batch_rank_import import _exclusive_authorized_writer_window
from .futures_calendar import load_futures_trading_calendar
from .gfex_rank_backfill import (
    BASE, CONTRACTS, RANKS, VARIETIES, parse_contracts, parse_varieties,
    validate_rank_rows, write_json,
)
from .storage import MarketStore


SOURCE = "official-gfex-browser-member-ranking"
IMPORT_ADAPTER = "official-gfex-rank-bronze-import-v1"
CAPTURE_ADAPTER = "official-gfex-rank-raw-v1"
BRONZE = Path("bronze") / "gfex_rank_capture"
CAPTURE_STATE = Path("state") / "gfex_rank_capture"
IMPORT_STATE = Path("state") / "gfex_rank_import"
_PRODUCT = re.compile(r"[a-z]{1,3}\Z")
_SUMMARY = frozenset({"合计", "总计", "小计"})


@dataclass(frozen=True)
class PreparedGfexDay:
    trading_day: str
    status: str
    product_count: int
    contract_count: int
    direction_count: int
    payload_sha256: str
    positions: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    coverage: dict[str, Any] | None

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "tradingDay": self.trading_day, "status": self.status,
            "productCount": self.product_count, "contractCount": self.contract_count,
            "directionCount": self.direction_count,
            "positionRows": len(self.positions), "tradeRows": len(self.trades),
            "payloadSha256": self.payload_sha256, "source": SOURCE,
        }


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"GFEX JSON must be an object: {path.name}")
    return payload


def _raw_rows(path: Path, *, endpoint: str, body: dict[str, str]) -> tuple[list[Any], dict[str, Any]]:
    proof = _read(path)
    if any((
        proof.get("publisher") != "GFEX",
        proof.get("adapter") != CAPTURE_ADAPTER,
        proof.get("sourceUrl") != BASE + endpoint,
        proof.get("requestBody") != body,
        proof.get("httpStatus") != 200,
        str(proof.get("apiCode")) != "0",
        "json" not in str(proof.get("contentType") or "").lower(),
    )):
        raise ValueError(f"GFEX Bronze provenance mismatch: {path.name}")
    captured = proof.get("retrievedAt")
    try:
        parsed_time = datetime.fromisoformat(captured)
    except (TypeError, ValueError) as error:
        raise ValueError(f"GFEX Bronze timestamp invalid: {path.name}") from error
    if parsed_time.tzinfo is None:
        raise ValueError(f"GFEX Bronze timestamp missing timezone: {path.name}")
    try:
        raw = base64.b64decode(proof["rawBase64"], validate=True)
        payload = json.loads(raw.decode("utf-8-sig"))
    except (binascii.Error, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"GFEX Bronze payload invalid: {path.name}") from error
    digest = hashlib.sha256(raw).hexdigest()
    if digest != proof.get("payloadSha256"):
        raise ValueError(f"GFEX Bronze SHA mismatch: {path.name}")
    if not isinstance(payload, dict) or str(payload.get("code")) != "0" or not isinstance(payload.get("data"), list):
        raise ValueError(f"GFEX Bronze API response invalid: {path.name}")
    parameters = payload.get("param")
    if isinstance(parameters, dict):
        for key, value in body.items():
            if parameters.get(key) != [value]:
                raise ValueError(f"GFEX Bronze response parameters mismatch: {path.name}")
    return payload["data"], proof


def _published_rows(rows: list[Any], *, direction: int, contract: str, day: str,
                    retrieved_at: str, digest: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    source_url = BASE + RANKS
    for row in rows:
        rank_text = str(row.get("contractId") or "").strip()
        name = " ".join(str(row.get("abbr") or "").split())
        if rank_text in _SUMMARY or name in _SUMMARY:
            continue
        rank = int(rank_text)
        # Use the same name-based member key as the other exchange normalizers.
        member_key = re.sub(r"\s+", "", name).casefold()
        value = float(row["todayQty"])
        change_raw = row.get("qtySub", row.get("todayQtyChg"))
        change = None if change_raw in (None, "") else float(change_raw)
        common = {
            "trading_day": day, "exchange": "GFEX",
            "contract_code": contract.upper(),
            "product_code": re.sub(r"\d", "", contract).upper(),
            "rank": rank, "member_key": member_key, "member_name": name,
            "source": SOURCE, "collected_at": retrieved_at,
            "publisher": "GFEX", "source_url": source_url,
            "adapter": IMPORT_ADAPTER, "retrieved_at": retrieved_at,
            "payload_sha256": digest, "quality_status": "PASS",
            "legacy_adapter": None,
        }
        if direction == 1:
            result.append({**common, "volume": value, "volume_change": change})
        else:
            result.append({**common, "side": "LONG" if direction == 2 else "SHORT",
                           "position": value, "position_change": change})
    return result


def prepare_gfex_rank_day(root: Path, trading_day: str) -> PreparedGfexDay:
    """Require every official list and direction response to match its checkpoint."""
    day = date.fromisoformat(trading_day).isoformat()
    compact = day.replace("-", "")
    root = Path(root)
    state = _read(root / CAPTURE_STATE / f"{day}.json")
    if state.get("schema") != "gfex-rank-capture/v1" or state.get("tradingDay") != day or state.get("status") != "PASS_RAW":
        raise ValueError(f"GFEX capture is not PASS_RAW for {day}")
    products = state.get("products")
    contract_lists = state.get("contractLists")
    directions = state.get("directions")
    if not isinstance(products, list) or not products or not all(
        isinstance(product, str) and _PRODUCT.fullmatch(product) for product in products
    ) or len(set(products)) != len(products):
        raise ValueError(f"GFEX product checkpoint invalid for {day}")
    if not isinstance(contract_lists, dict) or set(contract_lists) != set(products) or not isinstance(directions, dict):
        raise ValueError(f"GFEX contract/direction checkpoint invalid for {day}")
    varieties, varieties_proof = _raw_rows(root / BRONZE / "varieties-latest.json", endpoint=VARIETIES, body={})
    if parse_varieties(varieties) != sorted(products):
        raise ValueError(f"GFEX product list differs from Bronze checkpoint for {day}")
    digests = [varieties_proof["payloadSha256"]]
    positions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    expected_keys: set[str] = set()
    contract_count = 0
    timestamps: list[str] = []
    for product in products:
        list_state = contract_lists[product]
        rows, proof = _raw_rows(
            root / BRONZE / day / f"{product}-contracts.json",
            endpoint=CONTRACTS, body={"variety": product, "trade_date": compact},
        )
        codes = parse_contracts(rows, product)
        if not isinstance(list_state, dict) or any((
            list_state.get("contracts") != codes,
            list_state.get("payloadSha256") != proof["payloadSha256"],
            list_state.get("status") != ("PASS" if codes else "NO_PUBLISHED_CONTRACTS"),
        )):
            raise ValueError(f"GFEX contract list differs from checkpoint for {day}/{product}")
        digests.append(proof["payloadSha256"])
        timestamps.append(proof["retrievedAt"])
        contract_count += len(codes)
        for contract in codes:
            for direction in (1, 2, 3):
                key = f"{contract}.{direction}"
                expected_keys.add(key)
                body = {"trade_date": compact, "trade_type": "0", "variety": product,
                        "contract_id": contract, "data_type": str(direction)}
                published, rank_proof = _raw_rows(
                    root / BRONZE / day / f"{key}.json", endpoint=RANKS, body=body,
                )
                validation = validate_rank_rows(published)
                checkpoint = directions.get(key)
                if not isinstance(checkpoint, dict) or any((
                    checkpoint.get("payloadSha256") != rank_proof["payloadSha256"],
                    {field: checkpoint.get(field) for field in validation} != validation,
                    rank_proof.get("validation") != validation,
                    rank_proof.get("qualityStatus", validation["status"]) != validation["status"],
                )):
                    raise ValueError(f"GFEX rank direction differs from checkpoint for {day}/{key}")
                if validation["status"] != "PASS":
                    raise ValueError(f"GFEX published contract has an empty direction for {day}/{key}")
                normalized = _published_rows(
                    published, direction=direction, contract=contract, day=day,
                    retrieved_at=rank_proof["retrievedAt"], digest=rank_proof["payloadSha256"],
                )
                if len(normalized) != validation["publishedRows"]:
                    raise ValueError(f"GFEX normalization lost ranks for {day}/{key}")
                if direction == 1:
                    trades.extend(normalized)
                else:
                    positions.extend(normalized)
                digests.append(rank_proof["payloadSha256"])
                timestamps.append(rank_proof["retrievedAt"])
    if set(directions) != expected_keys or len(expected_keys) != 3 * contract_count:
        raise ValueError(f"GFEX three-direction coverage differs for {day}")
    day_digest = hashlib.sha256("\n".join(digests).encode("ascii")).hexdigest()
    if contract_count == 0:
        retrieved_at = max(timestamps)
        coverage = {
            "trading_day": day, "exchange": "GFEX", "status": "NO_PUBLICATION",
            "contract_count": 0, "record_count": 0, "source": SOURCE,
            "error": "官网当日品种合约清单均为空，未公布成交持仓排名；不代表零持仓",
            "collected_at": retrieved_at, "publisher": "GFEX",
            "source_url": BASE + CONTRACTS, "adapter": IMPORT_ADAPTER,
            "retrieved_at": retrieved_at, "payload_sha256": day_digest,
            "quality_status": "NO_PUBLICATION", "legacy_adapter": None,
        }
        return PreparedGfexDay(day, "SOURCE_UNAVAILABLE", len(products), 0, 0,
                               day_digest, [], [], coverage)
    if not positions or not trades:
        raise ValueError(f"GFEX ranked day has no facts for {day}")
    retrieved_at = max(timestamps)
    coverage = {
        "trading_day": day, "exchange": "GFEX", "status": "PASS",
        "contract_count": contract_count, "record_count": len(positions),
        "source": SOURCE, "error": None, "collected_at": retrieved_at,
        "publisher": "GFEX", "source_url": BASE + RANKS,
        "adapter": IMPORT_ADAPTER, "retrieved_at": retrieved_at,
        "payload_sha256": day_digest, "quality_status": "PASS", "legacy_adapter": None,
    }
    return PreparedGfexDay(day, "PASS", len(products), contract_count, len(expected_keys),
                           day_digest, positions, trades, coverage)


def import_gfex_rank_bronze(
    root: Path, *, start_day: str, end_day: str,
    max_days: int | None = None, write: bool = False,
) -> dict[str, Any]:
    """Audit all requested days; only ``write`` opens the Catalog writer."""
    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if start > end or max_days is not None and max_days < 1:
        raise ValueError("invalid GFEX import range or max_days")
    root = Path(root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is missing")
    days = calendar.between(start.isoformat(), end.isoformat())
    if not days:
        raise ValueError("verified futures trading calendar has no days in range")
    selected = days[:max_days] if max_days is not None else days
    report: dict[str, Any] = {
        "status": "RUNNING", "source": SOURCE, "startDay": start.isoformat(),
        "endDay": end.isoformat(), "calendarDays": len(days), "selectedDays": len(selected),
        "verifiedDays": 0, "unavailableDays": 0, "missingDays": 0,
        "importedDays": 0, "contractDays": 0, "positionRows": 0,
        "tradeRows": 0, "coverageRowsWritten": 0, "authoritativeDataWritten": False,
    }

    def process(store: MarketStore | None) -> None:
        for day in selected:
            if not (root / CAPTURE_STATE / f"{day}.json").is_file():
                report["missingDays"] += 1
                continue
            prepared = prepare_gfex_rank_day(root, day)
            report["verifiedDays"] += 1
            report["contractDays"] += prepared.contract_count
            report["positionRows"] += len(prepared.positions)
            report["tradeRows"] += len(prepared.trades)
            if prepared.status == "SOURCE_UNAVAILABLE":
                report["unavailableDays"] += 1
                if store is not None:
                    assert prepared.coverage is not None
                    store.upsert_futures_member_position_coverage([prepared.coverage])
                    report["coverageRowsWritten"] += 1
                    write_json(root / IMPORT_STATE / f"{day}.json", {
                        **prepared.summary, "qualityStatus": "NO_PUBLICATION",
                        "reason": prepared.coverage["error"],
                        "coverageRecordWritten": True,
                        "authoritativeDataWritten": False,
                        "verifiedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })
                    report["currentDay"] = day
                    write_json(root / IMPORT_STATE / "progress.json", report)
                continue
            if store is None:
                continue
            assert prepared.coverage is not None
            store.replace_futures_member_rank_day_source(
                trading_day=day, exchange="GFEX", source=SOURCE,
                positions=prepared.positions, trades=prepared.trades,
                coverage=prepared.coverage,
            )
            report["coverageRowsWritten"] += 1
            report["importedDays"] += 1
            report["authoritativeDataWritten"] = True
            write_json(root / IMPORT_STATE / f"{day}.json", {
                **prepared.summary, "qualityStatus": "PASS",
                "authoritativeDataWritten": True,
                "importedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            report["currentDay"] = day
            write_json(root / IMPORT_STATE / "progress.json", report)

    if not write:
        process(None)
    else:
        with _exclusive_authorized_writer_window(root / "state" / "dce_authorized_rank_writer.lock"):
            store = MarketStore(root)
            try:
                process(store)
            except Exception as error:
                report.update(status="FAILED", errorType=type(error).__name__)
                raise
            finally:
                report["updatedAtUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                try:
                    write_json(root / IMPORT_STATE / "progress.json", report)
                finally:
                    store.close()
    report["status"] = (
        "PARTIAL_RAW" if report["missingDays"] else
        "BATCH_LIMIT" if len(selected) < len(days) else
        "PASS_WITH_UNAVAILABLE" if write and report["unavailableDays"] else
        "PASS" if write else "VALIDATED_WITH_UNAVAILABLE" if report["unavailableDays"] else "VALIDATED"
    )
    if write:
        report["updatedAtUtc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(root / IMPORT_STATE / "progress.json", report)
    return report


__all__ = ("PreparedGfexDay", "prepare_gfex_rank_day", "import_gfex_rank_bronze")
