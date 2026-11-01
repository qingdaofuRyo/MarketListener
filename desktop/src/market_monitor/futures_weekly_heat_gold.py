"""Versioned daily futures heat Gold with completed-week trend snapshots."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
from functools import lru_cache
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import duckdb

from .atomic_files import replace_with_retry
from .futures_calendar import load_futures_trading_calendar
from .futures_official_daily_sources import PARSER_VERSION
from .futures_weekly_heat import FORMULA_VERSION, calculate_weekly_heat
from .storage import MarketStore


_PRODUCT_KEY = re.compile(r"^(CFFEX|SHFE|INE|DCE|CZCE|GFEX)\.[A-Z]{1,3}(?:-F)?$")
_SNAPSHOT_SCHEMA = 1
_MAX_POINTER_BYTES = 1024 * 1024
_MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_SNAPSHOT_FILE = re.compile(r"^[a-f0-9]{64}\.json\.gz$")
_TABLE = """
CREATE TABLE IF NOT EXISTS futures_weekly_heat_gold (
    formula_version VARCHAR NOT NULL, trade_date VARCHAR NOT NULL,
    week_end_date VARCHAR, payload_json VARCHAR NOT NULL,
    calculated_at VARCHAR NOT NULL,
    PRIMARY KEY(formula_version, trade_date)
)"""
_PRODUCT_TABLE = """
CREATE TABLE IF NOT EXISTS futures_weekly_heat_product_gold (
    formula_version VARCHAR NOT NULL, week_end_date VARCHAR NOT NULL,
    product_key VARCHAR NOT NULL, payload_json VARCHAR NOT NULL,
    calculated_at VARCHAR NOT NULL,
    PRIMARY KEY(formula_version, week_end_date, product_key)
)"""
_PRODUCT_DAILY_TABLE = """
CREATE TABLE IF NOT EXISTS futures_daily_heat_product_gold (
    formula_version VARCHAR NOT NULL, trade_date VARCHAR NOT NULL,
    product_key VARCHAR NOT NULL, payload_json VARCHAR NOT NULL,
    calculated_at VARCHAR NOT NULL,
    PRIMARY KEY(formula_version, trade_date, product_key)
)"""


def _ensure_tables(store: MarketStore) -> None:
    store.connection.execute(_TABLE)
    store.connection.execute(_PRODUCT_TABLE)
    store.connection.execute(_PRODUCT_DAILY_TABLE)


def _next_day_previous_settlement_seed(
    facts: list[dict[str, Any]], coverage: list[dict[str, Any]],
    calendar_days: tuple[str, ...], as_of_day: str,
) -> tuple[dict[tuple[str, str, str], float], dict[str, Any] | None]:
    """Use an official next-day prior settlement, with its own dated SHA proof."""

    visible = [day for day in calendar_days if day <= as_of_day]
    first = next((day for day in visible if day >= "2023-01-01"), None)
    if first is None or first not in visible or visible.index(first) == 0:
        return {}, None
    previous = visible[visible.index(first) - 1]
    if any(row.get("trade_date") == previous and row.get("exchange") == "DCE"
           and row.get("status") == "PASS" and row.get("parser_version") == PARSER_VERSION
           for row in coverage):
        return {}, None
    evidence = next((row for row in coverage
                     if row.get("trade_date") == first and row.get("exchange") == "DCE"
                     and row.get("adapter") == "official-dce-daily-v2"
                     and row.get("status") == "PASS"
                     and row.get("parser_version") == PARSER_VERSION), None)
    if evidence is None:
        return {}, None
    candidates = [row for row in facts if row.get("trade_date") == first
                  and row.get("exchange") == "DCE"
                  and row.get("adapter") == "official-dce-daily-v2"
                  and row.get("quality_status") == "PASS"
                  and row.get("parser_version") == PARSER_VERSION]
    digest = str(evidence.get("payload_sha256") or "")
    if (not candidates or len(candidates) != evidence.get("record_count")
            or len(candidates) != evidence.get("usable_count") or len(digest) != 64
            or any(row.get("payload_sha256") != digest for row in candidates)):
        return {}, None
    values: dict[tuple[str, str, str], float] = {}
    for row in candidates:
        value = row.get("previous_settlement")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return {}, None
        key = previous, "DCE", str(row["contract_code"])
        if key in values:
            return {}, None
        values[key] = float(value)
    return values, {"previousTradingDay": previous, "sourceTradingDay": first,
                    "sourceField": "previous_settlement", "sourcePayloadSha256": digest,
                    "contractCount": len(values), "kind": "NEXT_TRADING_DAY_OFFICIAL_PRIOR_SETTLEMENT"}


def _snapshot_dir(root: Path) -> Path:
    return root / "cache" / "futures-weekly-heat"


def _pointer_path(root: Path) -> Path:
    return root / "state" / "futures_weekly_heat_publication.json"


def _build_marker_path(root: Path) -> Path:
    return root / "state" / "futures_weekly_heat_build.json"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_small_json(path: Path, limit: int) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _valid_trade_date(value: Any) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _write_snapshot_file(root: Path, *, kind: str, product_key: str | None,
                         points: list[dict[str, Any]]) -> dict[str, Any]:
    payload = json.dumps(
        {"schemaVersion": _SNAPSHOT_SCHEMA, "formulaVersion": FORMULA_VERSION,
         "kind": kind, "productKey": product_key, "points": points},
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(payload) > _MAX_UNCOMPRESSED_BYTES:
        raise ValueError("weekly heat snapshot exceeds uncompressed budget")
    digest = sha256(payload).hexdigest()
    compressed = gzip.compress(payload, compresslevel=1, mtime=0)
    if len(compressed) > _MAX_COMPRESSED_BYTES:
        raise ValueError("weekly heat snapshot exceeds compressed budget")
    name = f"{digest}.json.gz"
    descriptor = {"file": name, "sha256": digest, "recordCount": len(points)}
    if _load_snapshot_file(root, descriptor, kind=kind, product_key=product_key) is None:
        _atomic_write(_snapshot_dir(root) / name, compressed)
    return descriptor


@lru_cache(maxsize=8)
def _cached_snapshot_payload(path: str, digest: str, mtime_ns: int,
                             compressed_size: int) -> dict[str, Any] | None:
    # The stat fields make a repaired content-addressed file miss this cache.
    del mtime_ns, compressed_size
    try:
        with gzip.open(path, "rb") as stream:
            raw = stream.read(_MAX_UNCOMPRESSED_BYTES + 1)
        if len(raw) > _MAX_UNCOMPRESSED_BYTES or sha256(raw).hexdigest() != digest:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, EOFError, ValueError, TypeError):
        return None


def _load_snapshot_file(root: Path, descriptor: Any, *, kind: str,
                        product_key: str | None) -> list[dict[str, Any]] | None:
    if not isinstance(descriptor, dict):
        return None
    name, digest, count = (descriptor.get("file"), descriptor.get("sha256"),
                           descriptor.get("recordCount"))
    if (not isinstance(name, str) or _SNAPSHOT_FILE.fullmatch(name) is None
            or not isinstance(digest, str) or name != f"{digest}.json.gz"
            or not isinstance(count, int) or count < 0):
        return None
    path = _snapshot_dir(root) / name
    try:
        stat = path.stat()
        if stat.st_size > _MAX_COMPRESSED_BYTES:
            return None
        payload = _cached_snapshot_payload(
            str(path), digest, stat.st_mtime_ns, stat.st_size,
        )
    except OSError:
        return None
    if (not isinstance(payload, dict)
            or payload.get("schemaVersion") != _SNAPSHOT_SCHEMA
            or payload.get("formulaVersion") != FORMULA_VERSION
            or payload.get("kind") != kind or payload.get("productKey") != product_key):
        return None
    points = payload.get("points")
    if not isinstance(points, list) or len(points) != count:
        return None
    dates = [point.get("tradeDate") for point in points if isinstance(point, dict)]
    if (len(dates) != len(points)
            or any(not _valid_trade_date(day) for day in dates)
            or dates != sorted(set(dates))
            or (product_key is not None
                and any(point.get("productKey") != product_key for point in points))):
        return None
    return points


def _load_generation(root: Path, generation: Any,
                     product_key: str | None) -> tuple[list[dict[str, Any]],
                                                        list[dict[str, Any]] | None] | None:
    if (not isinstance(generation, dict) or not isinstance(generation.get("id"), str)
            or not isinstance(generation.get("asOfDay"), str)
            or not isinstance(generation.get("products"), dict)):
        return None
    market = _load_snapshot_file(root, generation.get("market"),
                                 kind="market", product_key=None)
    if market is None:
        return None
    if product_key is None:
        return market, None
    descriptor = generation["products"].get(product_key)
    if descriptor is None:
        return market, []
    product = _load_snapshot_file(root, descriptor,
                                  kind="product", product_key=product_key)
    if product is None or not {point["tradeDate"] for point in product}.issubset(
            {point["tradeDate"] for point in market}):
        return None
    return market, product


def _publish_weekly_heat_snapshot(
    root: Path, *, build_id: str, as_of_day: str,
    daily: list[tuple[str, str, str | None, str, str]],
    product_daily: list[tuple[str, str, str, str, str]],
) -> None:
    """Switch one pointer only after the three Gold tables have committed."""
    market_points = sorted((json.loads(row[3]) for row in daily),
                           key=lambda point: point["tradeDate"])
    by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in product_daily:
        by_product[row[2]].append(json.loads(row[3]))
    market_descriptor = _write_snapshot_file(
        root, kind="market", product_key=None, points=market_points,
    )
    product_descriptors = {
        key: _write_snapshot_file(root, kind="product", product_key=key,
                                  points=sorted(points, key=lambda point: point["tradeDate"]))
        for key, points in sorted(by_product.items())
    }
    previous_pointer = _read_small_json(_pointer_path(root), _MAX_POINTER_BYTES)
    previous = (previous_pointer or {}).get("current")
    if _load_generation(root, previous, None) is None:
        previous = (previous_pointer or {}).get("previous")
    generation = {
        "id": build_id, "asOfDay": as_of_day,
        "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market": market_descriptor, "products": product_descriptors,
    }
    pointer = json.dumps(
        {"schemaVersion": _SNAPSHOT_SCHEMA, "formulaVersion": FORMULA_VERSION,
         "current": generation, "previous": previous},
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(pointer) > _MAX_POINTER_BYTES:
        raise ValueError("weekly heat publication pointer exceeds metadata budget")
    _atomic_write(_pointer_path(root), pointer)
    marker = _read_small_json(_build_marker_path(root), 16 * 1024)
    if marker is not None and marker.get("id") == build_id:
        try:
            _build_marker_path(root).unlink(missing_ok=True)
        except OSError:
            # The pointer already committed and identifies this build; an old
            # matching marker does not make the new publication stale.
            pass


def rebuild_weekly_heat_gold(data_root: Path, *, as_of_day: str) -> dict[str, Any]:
    """Atomically publish current daily heat without replacing archived formulas."""

    root = Path(data_root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None or as_of_day not in calendar.trading_days:
        raise ValueError("weekly heat needs a verified as_of trading day")
    build_id = uuid4().hex
    _atomic_write(
        _build_marker_path(root),
        json.dumps({"id": build_id, "asOfDay": as_of_day},
                   separators=(",", ":")).encode("utf-8"),
    )
    store = MarketStore(root)
    try:
        facts = store.list_futures_official_daily(end_day=as_of_day)
        coverage = store.list_futures_official_daily_coverage(end_day=as_of_day)
        seed_prices, seed_evidence = _next_day_previous_settlement_seed(
            facts, coverage, calendar.trading_days, as_of_day,
        )
        result = calculate_weekly_heat(
            facts, coverage, calendar.trading_days, as_of_day=as_of_day,
            start_day="2023-01-01", end_day=as_of_day,
            seed_prices=seed_prices,
        )
        if seed_evidence is not None:
            for point in [*result["points"], *result["productPoints"], *result["productDailyPoints"]]:
                if point.get("coverage", {}).get("derivedSeedContracts"):
                    point["coverage"]["seedEvidence"] = seed_evidence
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        daily = [
            (FORMULA_VERSION, point["tradeDate"], point.get("weekEndDate"),
             json.dumps(point, ensure_ascii=False, allow_nan=False), timestamp)
            for point in result["points"]
        ]
        products = [
            (FORMULA_VERSION, point["weekEndDate"], point["productKey"],
             json.dumps({key: value for key, value in point.items() if key != "contracts"},
                        ensure_ascii=False, allow_nan=False), timestamp)
            for point in result["productPoints"]
            if point["weekEndDate"] >= "2023-01-01"
        ]
        product_daily = [
            (FORMULA_VERSION, point["tradeDate"], point["productKey"],
             json.dumps(point, ensure_ascii=False, allow_nan=False), timestamp)
            for point in result["productDailyPoints"]
        ]
        _ensure_tables(store)
        try:
            store.connection.execute("BEGIN TRANSACTION")
            store.connection.execute(
                "DELETE FROM futures_weekly_heat_gold WHERE formula_version=?", [FORMULA_VERSION]
            )
            store.connection.execute(
                "DELETE FROM futures_weekly_heat_product_gold WHERE formula_version=?", [FORMULA_VERSION]
            )
            store.connection.execute(
                "DELETE FROM futures_daily_heat_product_gold WHERE formula_version=?", [FORMULA_VERSION]
            )
            if daily:
                store.connection.executemany(
                    "INSERT INTO futures_weekly_heat_gold VALUES (?,?,?,?,?)", daily
                )
            if products:
                store.connection.executemany(
                    "INSERT INTO futures_weekly_heat_product_gold VALUES (?,?,?,?,?)", products
                )
            if product_daily:
                store.connection.executemany(
                    "INSERT INTO futures_daily_heat_product_gold VALUES (?,?,?,?,?)", product_daily
                )
            store.connection.execute("COMMIT")
        except Exception:
            store.connection.execute("ROLLBACK")
            raise
    finally:
        store.close()
    _publish_weekly_heat_snapshot(
        root, build_id=build_id, as_of_day=as_of_day,
        daily=daily, product_daily=product_daily,
    )
    latest = result["latest"]
    pending = sum(point.get("pendingWeek") is not None for point in result["points"])
    partial = sum(
        point.get("dataQualityStatus") in {"PARTIAL", "PENDING"}
        or (point.get("dataQualityStatus") == "UNAVAILABLE" and point.get("pendingWeek") is not None)
        for point in result["points"]
    )
    return {
        "status": "PASS" if latest and latest.get("dataQualityStatus") == "PASS"
        and pending == 0 and partial == 0
        else "PARTIAL" if daily else "NO_DATA",
        "formulaVersion": FORMULA_VERSION, "asOfDay": as_of_day,
        "writtenDays": len(daily), "writtenProductWeeks": len(products),
        "writtenProductDays": len(product_daily),
        "pendingDays": pending, "partialDays": partial,
        "latestWeekEnd": latest.get("weekEndDate") if latest else None,
        "latestQuality": latest.get("dataQualityStatus") if latest else None,
        "seedEvidence": seed_evidence,
    }


def publish_existing_weekly_heat_gold_snapshot(data_root: Path) -> dict[str, Any]:
    """Offline, read-only Gold migration; call while the product writer is stopped.

    This never calculates history or changes a Gold row. It reads a consistent
    current-formula transaction and only switches the derived file pointer after
    all market and product snapshot files have been written and validated.
    """
    root = Path(data_root)
    if not (root / "catalog.duckdb").is_file():
        return {"status": "UNAVAILABLE", "reason": "CATALOG_MISSING"}
    try:
        connection = duckdb.connect(str(root / "catalog.duckdb"), read_only=True)
    except (duckdb.IOException, duckdb.ConnectionException):
        return {"status": "UNAVAILABLE", "reason": "CATALOG_BUSY"}
    try:
        connection.execute("BEGIN TRANSACTION")
        tables = {str(row[0]) for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('futures_weekly_heat_gold','futures_weekly_heat_product_gold',"
            "'futures_daily_heat_product_gold')"
        ).fetchall()}
        if len(tables) != 3:
            return {"status": "UNAVAILABLE", "reason": "GOLD_TABLES_MISSING"}
        daily = connection.execute(
            "SELECT formula_version,trade_date,week_end_date,payload_json,calculated_at "
            "FROM futures_weekly_heat_gold WHERE formula_version=? ORDER BY trade_date",
            [FORMULA_VERSION],
        ).fetchall()
        if not daily:
            return {"status": "UNAVAILABLE", "reason": "FORMULA_VERSION_MISSING",
                    "formulaVersion": FORMULA_VERSION}
        product_daily = connection.execute(
            "SELECT formula_version,trade_date,product_key,payload_json,calculated_at "
            "FROM futures_daily_heat_product_gold WHERE formula_version=? "
            "ORDER BY product_key,trade_date",
            [FORMULA_VERSION],
        ).fetchall()
        connection.execute("COMMIT")
    finally:
        connection.close()
    try:
        market_dates = {row[1] for row in daily}
        if (len(market_dates) != len(daily)
                or any(not _valid_trade_date(row[1])
                       or row[1] != json.loads(row[3]).get("tradeDate") for row in daily)
                or any(not _valid_trade_date(row[1]) or row[1] not in market_dates
                       or row[1] != json.loads(row[3]).get("tradeDate")
                       or row[2] != json.loads(row[3]).get("productKey")
                       or _PRODUCT_KEY.fullmatch(row[2]) is None for row in product_daily)
                or len({(row[1], row[2]) for row in product_daily}) != len(product_daily)):
            return {"status": "UNAVAILABLE", "reason": "GOLD_ROW_KEYS_INVALID"}
    except (ValueError, TypeError, AttributeError):
        return {"status": "UNAVAILABLE", "reason": "GOLD_PAYLOAD_INVALID"}
    build_id = uuid4().hex
    as_of_day = daily[-1][1]
    _atomic_write(
        _build_marker_path(root),
        json.dumps({"id": build_id, "asOfDay": as_of_day},
                   separators=(",", ":")).encode("utf-8"),
    )
    _publish_weekly_heat_snapshot(
        root, build_id=build_id, as_of_day=as_of_day,
        daily=daily, product_daily=product_daily,
    )
    return {"status": "PASS", "formulaVersion": FORMULA_VERSION,
            "asOfDay": as_of_day, "writtenDays": len(daily),
            "writtenProductDays": len(product_daily), "snapshotVersion": build_id}


def read_weekly_heat_gold(
    data_root: Path, *, start_day: str | None = None,
    end_day: str | None = None, product_key: str | None = None,
) -> dict[str, Any]:
    """Serve one verified publication without opening catalog.duckdb."""
    if product_key is not None:
        product_key = product_key.upper()
        if _PRODUCT_KEY.fullmatch(product_key) is None:
            raise ValueError("invalid product_key")
    root = Path(data_root)
    empty = {"available": False, "formulaVersion": FORMULA_VERSION,
             "config": {}, "points": [], "latest": None, "productKey": product_key,
             "snapshotStatus": "UNAVAILABLE", "snapshotAsOfDay": None,
             "snapshotPublishedAt": None, "snapshotVersion": None,
             "limitations": ["多空热度尚未构建或已发布快照不可用。"]}
    pointer = _read_small_json(_pointer_path(root), _MAX_POINTER_BYTES)
    if (pointer is None or pointer.get("schemaVersion") != _SNAPSHOT_SCHEMA
            or pointer.get("formulaVersion") != FORMULA_VERSION):
        return empty
    marker = _read_small_json(_build_marker_path(root), 16 * 1024)
    selected_generation: dict[str, Any] | None = None
    selected_data: tuple[list[dict[str, Any]], list[dict[str, Any]] | None] | None = None
    fallback = False
    for index, candidate in enumerate((pointer.get("current"), pointer.get("previous"))):
        loaded = _load_generation(root, candidate, product_key)
        if loaded is not None:
            selected_generation, selected_data, fallback = candidate, loaded, index == 1
            break
    if selected_generation is None or selected_data is None:
        return {**empty, "snapshotReason": "PUBLICATION_CORRUPT"}
    market_points, product_points = selected_data
    points = [point for point in market_points
              if (not start_day or point["tradeDate"] >= start_day)
              and (not end_day or point["tradeDate"] <= end_day)]
    if product_key and points:
        by_day = {point["tradeDate"]: point for point in (product_points or [])}
        points = [by_day.get(point["tradeDate"], {
            "tradeDate": point["tradeDate"], "weekEndDate": point.get("weekEndDate"),
            "totalScore": None, "score": None, "breadthScore": None, "streakScore": None,
            "magnitudeScore": None, "upCount": None, "downCount": None,
            "upWeightedReturnPct": None, "downWeightedReturnPct": None,
            "upTurnoverCny": None, "downTurnoverCny": None,
            "coverage": {"direction": 0.0, "turnover": 0.0, "missingExchanges": [],
                         "dailyMissingExchanges": [], "invalidContracts": []},
            "dataQualityStatus": "UNAVAILABLE", "productKey": product_key,
        }) for point in points]
    # A caller cannot mutate the hot immutable generation used by other GETs.
    points = deepcopy(points)
    pending = (_build_marker_path(root).is_file() and
               (marker is None or marker.get("id") != selected_generation["id"]))
    missing_catalog = not (root / "catalog.duckdb").is_file()
    status = "STALE" if fallback or pending or missing_catalog else "CURRENT"
    reason = ("CURRENT_PUBLICATION_CORRUPT" if fallback else
              "REBUILD_NOT_PUBLISHED" if pending else
              "CATALOG_MISSING" if missing_catalog else None)
    metadata = {"snapshotStatus": status, "snapshotAsOfDay": selected_generation["asOfDay"],
                "snapshotPublishedAt": selected_generation.get("publishedAt"),
                "snapshotVersion": selected_generation["id"], "snapshotReason": reason}
    if not points:
        return {**empty, **metadata, "snapshotStatus": "EMPTY" if status == "CURRENT" else status}
    from .futures_weekly_heat import _config  # Formally versioned UI configuration.

    return {"available": any(point.get("totalScore") is not None for point in points),
            "formulaVersion": FORMULA_VERSION, "config": _config(), "points": points,
            "latest": points[-1], "productKey": product_key,
            "limitations": ["数量和涨跌幅逐日更新；连续周只使用当日已结束的交易周。成交额只作为原始事实展示。",
                            "来源缺报保持断点；连续周自第二个同向周加持且不设封顶。"],
            **metadata}


__all__ = ("rebuild_weekly_heat_gold", "publish_existing_weekly_heat_gold_snapshot",
           "read_weekly_heat_gold")
