"""Source-selected ranking aggregates for bounded, local chart playback."""

from __future__ import annotations

from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

import duckdb

from .futures import futures_product_name
from .futures_calendar import load_futures_trading_calendar
from .futures_member_rank_source import DCE_BATCH_SOURCE, DCE_DESKTOP_BATCH_SOURCE
from .storage import MarketStore


_CACHE: dict[tuple, dict[str, Any]] = {}
_LOCK = threading.RLock()
_SNAPSHOT_SCHEMA = 2
_SNAPSHOT_VERSION = "official-member-ranking-cache-v2"
_ALL_MEMBERS_KEY = "__all__"
_METRICS = ("gross", "longPosition", "shortPosition", "netLongPosition", "netShortPosition")
_PREFERRED = (
    DCE_DESKTOP_BATCH_SOURCE, "official-czce-workbook-member-ranking",
    "official-gfex-browser-member-ranking", "official-cffex-published-member-ranking",
    "official-shfe-ine-dat-member-ranking",
)


def canonical_member(name: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"[（(]\s*代客\s*[）)]\s*$", "", name)).casefold()


def _snapshot_path(root: Path, identity: tuple[Any, ...]) -> Path:
    digest = sha256(repr((_SNAPSHOT_VERSION, _SNAPSHOT_SCHEMA, identity)).encode("utf-8")).hexdigest()
    return root / "cache" / f"futures-ranking-v{_SNAPSHOT_SCHEMA}-{digest}.json.gz"


def _read_snapshot(root: Path, identity: tuple[Any, ...]) -> dict[str, Any] | None:
    try:
        with gzip.open(_snapshot_path(root, identity), "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        if (payload.get("schema") == _SNAPSHOT_SCHEMA
                and payload.get("formulaVersion") == _SNAPSHOT_VERSION
                and payload.get("identity") == list(identity)):
            return payload["response"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_snapshot(root: Path, identity: tuple[Any, ...], response: dict[str, Any]) -> None:
    path = _snapshot_path(root, identity)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
            json.dump({"schema": _SNAPSHOT_SCHEMA, "formulaVersion": _SNAPSHOT_VERSION,
                       "identity": identity, "response": response},
                      stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
        snapshots = sorted(path.parent.glob(f"futures-ranking-v{_SNAPSHOT_SCHEMA}-*.json.gz"),
                           key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for stale in snapshots[32:]:
            stale.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError):
        temporary.unlink(missing_ok=True)


def ranking_series(
    root: Path, *, view: str, range_name: str = "1y", member_key: str | None = None,
    product_key: str | None = None, contract_code: str | None = None,
    start_day: str | None = None, end_day: str | None = None,
    snapshot_only: bool = False,
) -> dict[str, Any]:
    if view not in {"members", "member-products", "product-members"}:
        raise ValueError("view must be members, member-products or product-members")
    if range_name not in {"1y", "3y", "5y", "all"}:
        raise ValueError("invalid ranking range")
    if view == "member-products" and not member_key:
        raise ValueError("member_key is required for member-products")
    if view == "product-members" and not product_key:
        raise ValueError("product_key is required for product-members")
    if product_key and not re.fullmatch(r"(CFFEX|SHFE|INE|DCE|CZCE|GFEX)\.[A-Z]{1,3}(?:-F)?", product_key):
        raise ValueError("invalid product_key")
    if contract_code and not re.fullmatch(r"[A-Z]{1,3}(?:-F)?\d{3,4}", contract_code):
        raise ValueError("invalid contract_code")
    if contract_code and len(re.search(r"\d+$", contract_code).group()) == 3 and not str(product_key or "").startswith("CZCE."):
        raise ValueError("three-digit contract_code requires a CZCE product")
    root = Path(root)
    member = canonical_member(member_key or "")
    identity = ("series", view, range_name, member, product_key, contract_code, start_day, end_day)
    if snapshot_only:
        cached = _read_snapshot(root, identity)
        return {**cached, "snapshotStatus": "CACHED_AWAITING_REFRESH"} if cached else {
            "available": False, "dates": [], "metrics": {}, "members": [], "products": [], "coverage": [],
        }
    if not (root / "catalog.duckdb").is_file():
        return {"available": False, "dates": [], "metrics": {}, "members": [], "products": [], "coverage": []}
    try:
        store = MarketStore(root)
    except duckdb.IOException as error:
        if "Cannot open file" not in str(error):
            raise
        cached = _read_snapshot(root, identity)
        if cached is None:
            raise
        return {**cached, "snapshotStatus": "CACHED_DURING_UPDATE"}
    try:
        connection = store.connection
        version = connection.execute(
            "SELECT count(*), coalesce(sum(record_count),0), coalesce(sum(hash(coalesce(payload_sha256,'') || status || source || collected_at)),0) "
            "FROM futures_member_position_coverage WHERE source LIKE 'official-%'"
        ).fetchone()
        latest = connection.execute(
            "SELECT max(trading_day) FROM futures_member_position_ranks WHERE source LIKE 'official-%' AND quality_status='PASS'"
        ).fetchone()[0]
        if latest is None:
            return {"available": False, "dates": [], "metrics": {}, "members": [], "products": [], "coverage": []}
        end = min(end_day or str(latest), str(latest))
        start = start_day or (
            (date.fromisoformat(end) - timedelta(days=365 * {"1y": 1, "3y": 3, "5y": 5}[range_name])).isoformat()
            if range_name != "all" else "2023-01-01"
        )
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError("start_day must not be after end_day")
        cache_key = (str(root.resolve()), version, view, member, product_key, contract_code, start, end)
        with _LOCK:
            if cache_key in _CACHE:
                return _CACHE[cache_key]
        saved = _read_snapshot(root, identity)
        if (saved and saved.get("dataVersion") == ":".join(map(str, version))
                and saved.get("latestDay") == str(latest)):
            with _LOCK:
                if len(_CACHE) >= 8:
                    _CACHE.clear()
                _CACHE[cache_key] = saved
            return saved
        preferred = ",".join("'" + item + "'" for item in _PREFERRED)
        prefix = f"""
        WITH choice AS (
          SELECT *, row_number() OVER (PARTITION BY trading_day,exchange ORDER BY
            CASE status WHEN 'PASS' THEN 4 WHEN 'PARTIAL' THEN 3 WHEN 'NO_PUBLICATION' THEN 2 ELSE 0 END DESC,
            CASE WHEN source='{DCE_BATCH_SOURCE}' THEN 4 WHEN source IN ({preferred}) THEN 3 WHEN source='official-dce-member-ranking' THEN 2 ELSE 1 END DESC,
            retrieved_at DESC,source DESC) pick
          FROM futures_member_position_coverage WHERE source LIKE 'official-%'
        ), selected_ranks AS (
          SELECT r.*, regexp_extract(r.contract_code,'([0-9]{{3,4}})$',1) delivery_digits
          FROM futures_member_position_ranks r JOIN choice c
          ON r.trading_day=c.trading_day AND r.exchange=c.exchange AND r.source=c.source
          WHERE c.pick=1 AND c.status IN ('PASS','PARTIAL') AND r.quality_status='PASS'
            AND r.trading_day>='2023-01-01' AND r.trading_day<=?
            AND (regexp_matches(r.contract_code,'^[A-Z]{{1,3}}(-F)?[0-9]{{4}}$')
              OR (r.exchange='CZCE' AND regexp_matches(r.contract_code,'^[A-Z]{{1,3}}[0-9]{{3}}$')))
        ), contract_months AS (
          SELECT *, cast(right(delivery_digits,2) AS INTEGER) delivery_month,
            extract(year FROM cast(trading_day AS DATE))::INTEGER trade_year,
            extract(month FROM cast(trading_day AS DATE))::INTEGER trade_month,
            CASE WHEN length(delivery_digits)=4 THEN 2000+cast(left(delivery_digits,2) AS INTEGER)
              ELSE (floor(extract(year FROM cast(trading_day AS DATE))/10)*10
                +cast(left(delivery_digits,1) AS INTEGER))::INTEGER END candidate_year
          FROM selected_ranks
        ), valid_contracts AS (
          SELECT * FROM contract_months WHERE delivery_month BETWEEN 1 AND 12
            AND (CASE WHEN length(delivery_digits)=4 THEN candidate_year
              WHEN (candidate_year-trade_year)*12+delivery_month-trade_month>=60 THEN candidate_year-10
              WHEN (candidate_year-trade_year)*12+delivery_month-trade_month < -60 THEN candidate_year+10
              ELSE candidate_year END)*12+delivery_month>=trade_year*12+trade_month
            AND (NOT regexp_matches(contract_code,'-F') OR
              (trading_day>='2025-10-29' AND candidate_year*12+delivery_month>trade_year*12+trade_month))
        ), published AS (
          SELECT r.*, lower(regexp_replace(regexp_replace(regexp_replace(trim(replace(replace(member_name,'　',''),' ','')),'[（(]\\s*代客\\s*[）)]\\s*$',''),'\\s+','','g'),'[　 ]','','g')) company_key,
            trim(regexp_replace(trim(replace(replace(member_name,'　',''),' ','')),'[（(]\\s*代客\\s*[）)]\\s*$','')) company_name,
            r.exchange||'.'||r.product_code product_key
          FROM valid_contracts r
        ), paired AS (
          SELECT trading_day,exchange,product_key,contract_code,company_key,min(company_name) company_name,
            sum(position) FILTER(WHERE side='LONG') long_value,
            sum(position) FILTER(WHERE side='SHORT') short_value
          FROM published GROUP BY 1,2,3,4,5
        )
        """
        coverage_rows = connection.execute(
            prefix + "SELECT trading_day,exchange,status FROM choice WHERE pick=1 AND trading_day>='2023-01-01' AND trading_day<=? ORDER BY 1,2",
            [str(latest), str(latest)],
        ).fetchall()
        coverage_by_day: dict[str, dict[str, str]] = {}
        for day, exchange, status in coverage_rows:
            coverage_by_day.setdefault(day, {})[exchange] = status
        expected = {"CFFEX", "SHFE", "INE", "DCE", "CZCE", "GFEX"}
        complete = [day for day, venues in coverage_by_day.items()
                    if all(venues.get(exchange) in {"PASS", "NO_PUBLICATION"} for exchange in expected)]
        baseline = max(complete, default=str(latest))
        members = connection.execute(
            prefix + "SELECT company_key,min(company_name),sum(coalesce(long_value,0)+coalesce(short_value,0)) FILTER(WHERE trading_day=?) FROM paired GROUP BY 1 ORDER BY 3 DESC NULLS LAST,1",
            [str(latest), baseline],
        ).fetchall()
        products = connection.execute(
            prefix + "SELECT product_key,list_sort(list_distinct(list(contract_code))),sum(coalesce(long_value,0)+coalesce(short_value,0)) FILTER(WHERE trading_day=?) FROM paired GROUP BY 1 ORDER BY 3 DESC NULLS LAST,1",
            [str(latest), baseline],
        ).fetchall()
        dimension = "product_key" if view == "member-products" else "company_key"
        name_column = "product_key" if view == "member-products" else "company_name"
        conditions: list[str] = []
        parameters: list[str] = [str(latest)]
        if view == "member-products" and member != _ALL_MEMBERS_KEY:
            conditions.append("company_key=?")
            parameters.append(member)
        if view == "product-members":
            conditions.append("product_key=?")
            parameters.append(str(product_key))
            if contract_code:
                conditions.append("contract_code=?")
                parameters.append(contract_code)
        conditions.append("(trading_day BETWEEN ? AND ? OR trading_day=?)")
        parameters.extend((start, end, baseline))
        query = prefix + f"""
          SELECT trading_day,{dimension},min({name_column}),
            sum(coalesce(long_value,0)+coalesce(short_value,0)),sum(long_value),sum(short_value),
            sum(CASE WHEN long_value IS NOT NULL AND short_value IS NOT NULL THEN greatest(long_value-short_value,0) END),
            sum(CASE WHEN long_value IS NOT NULL AND short_value IS NOT NULL THEN greatest(short_value-long_value,0) END)
          FROM paired WHERE {' AND '.join(conditions)} GROUP BY 1,2 ORDER BY 1,2
        """
        rows = connection.execute(query, parameters).fetchall()
    finally:
        store.close()
    names: dict[str, str] = {}
    values: dict[str, dict[str, tuple]] = {}
    for day, key, name, *numbers in rows:
        names[key] = _product_name(key) if view == "member-products" else name
        values.setdefault(day, {})[key] = tuple(numbers)
    calendar = load_futures_trading_calendar(root)
    dates = calendar.between(start, end) if calendar else sorted(day for day in coverage_by_day if start <= day <= end)
    metric_names = _METRICS if view in {"member-products", "product-members"} else ("gross",)
    metrics = {}
    for metric in metric_names:
        index = _METRICS.index(metric)
        ordered = sorted(names, key=lambda key: (
            -(values.get(baseline, {}).get(key, (None,) * 5)[index]
              if values.get(baseline, {}).get(key, (None,) * 5)[index] is not None else -1), key,
        ))
        metrics[metric] = [{"key": key, "name": names[key], "values": [
            values.get(day, {}).get(key, (None,) * 5)[index] for day in dates
        ]} for key in ordered]
    payload = {
        "available": bool(rows), "view": view, "baselineDay": baseline,
        "latestDay": str(latest), "dates": dates, "metrics": metrics,
        "dataVersion": ":".join(map(str, version)),
        "members": [{"key": key, "name": name, "position": value} for key, name, value in members],
        "products": [{"key": key, "name": _product_name(key), "contracts": contracts, "position": value} for key, contracts, value in products],
        "coverage": [{"tradeDate": day, "missingExchanges": sorted(
            exchange for exchange in expected if coverage_by_day.get(day, {}).get(exchange) not in {"PASS", "NO_PUBLICATION"}
        )} for day in dates],
    }
    with _LOCK:
        if len(_CACHE) >= 8:
            _CACHE.clear()
        _CACHE[cache_key] = payload
    _write_snapshot(root, identity, payload)
    return payload


def ranking_directory(root: Path, *, snapshot_only: bool = False) -> dict[str, Any]:
    """Return selectable entities without transferring historical matrices."""

    root = Path(root)
    identity = ("directory",)
    if snapshot_only:
        cached = _read_snapshot(root, identity)
        return {**cached, "snapshotStatus": "CACHED_AWAITING_REFRESH"} if cached else {
            "available": False, "latestDay": None, "baselineDay": None,
            "members": [], "products": [], "dataVersion": None,
        }
    if not (root / "catalog.duckdb").is_file():
        return {"available": False, "latestDay": None, "baselineDay": None,
                "members": [], "products": [], "dataVersion": None}
    try:
        store = MarketStore(root)
    except duckdb.IOException as error:
        if "Cannot open file" not in str(error):
            raise
        cached = _read_snapshot(root, identity)
        if cached is None:
            raise
        return {**cached, "snapshotStatus": "CACHED_DURING_UPDATE"}
    try:
        latest = store.connection.execute(
            "SELECT max(trading_day) FROM futures_member_position_ranks "
            "WHERE source LIKE 'official-%' AND quality_status='PASS'"
        ).fetchone()[0]
    finally:
        store.close()
    if latest is None:
        return {"available": False, "latestDay": None, "baselineDay": None,
                "members": [], "products": [], "dataVersion": None}
    response = ranking_series(root, view="members", start_day=str(latest), end_day=str(latest))
    directory = {key: response.get(key) for key in (
        "available", "latestDay", "baselineDay", "members", "products", "dataVersion"
    )}
    _write_snapshot(root, identity, directory)
    return directory


def _product_name(key: str) -> str:
    exchange, code = key.split(".", 1)
    return f"{futures_product_name(code, exchange) or code}（{code}）"
