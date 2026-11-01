"""Read-only TDX 38# economy observations and aligned global-futures ratios.

The 38# terminal records are observation-period values, not publication-day
events. Ratio inputs come solely from the independent GLOBAL-FUTURE-1d Silver
source; old Gold derived metrics and other providers are never blended in.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request


router = APIRouter(prefix="/api/data/tdx-economy", tags=["data-tdx-economy"])

_MACRO_PARTITION_PREFIX = "TDX-LOCAL-CN-MACRO-1d-"
_FUTURE_PARTITION_PREFIX = "GLOBAL-FUTURE-1d-"
_MACRO_ID = re.compile(r"^TDX_MACRO\.([1-6]|8)_[A-Z0-9-]+$")
_PHYSICAL_MACRO_ID = re.compile(r"^CN\.TDX_MACRO\.MACRO\.([1-6]|8)_[A-Z0-9-]+\.TDX_LOCAL$")
_SOURCE_MACRO = "通达信金融终端（本地 38#，Silver）"
_SOURCE_RATIO = "AKShare 外盘期货历史（GLOBAL-FUTURE Silver）"
_TOPIC = {
    "1": "国民经济", "2": "物价", "3": "景气指数", "4": "贸易",
    "5": "利率与市场", "6": "货币金融", "8": "海外经济",
}
_KNOWN_UNITS = {
    "2_CPI": "指数", "2_PPI": "指数", "3_PMI": "指数", "3_PMIH": "指数",
    "3_PMIN": "指数", "3_PMINH": "指数", "8_NONAG": "万人",
    "8_EFFR": "%", "8_FFRLL": "%", "8_FFRUL": "%",
}
_FUTURES = {
    "GC": "US.COMEX.FUTURE.GC",
    "SI": "US.COMEX.FUTURE.SI",
    "HG": "US.COMEX.FUTURE.HG",
    "CL": "US.NYMEX.FUTURE.CL",
}
_RATIO_META = {
    "GOLD_SILVER_RATIO": ("金银比", "盎司白银/盎司黄金", "SI"),
    "GOLD_COPPER_RATIO": ("金铜比", "磅铜/盎司黄金", "HG"),
    "GOLD_OIL_RATIO": ("金油比", "桶原油/盎司黄金", "CL"),
}
_MAX_POINTS = 3000


def _partition_files(root: Path, prefix: str) -> list[str]:
    """Select only complete catalog-listed files under this data root."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    # Match the running MarketStore connection configuration. DuckDB rejects
    # mixed read-only/read-write connections to one file within a process.
    connection = duckdb.connect(str(catalog))
    try:
        rows = connection.execute(
            "SELECT file_path FROM partitions WHERE partition_id LIKE ? "
            "AND status='COMPLETE' ORDER BY partition_id",
            [prefix + "%"],
        ).fetchall()
    except duckdb.CatalogException:
        return []
    finally:
        connection.close()
    silver_root = (root / "silver").resolve()
    paths: list[str] = []
    for (relative,) in rows:
        path = (root / str(relative)).resolve()
        if path.is_relative_to(silver_root) and path.is_file() and path.suffix == ".parquet":
            paths.append(str(path))
    return paths


def _name_catalog() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / "config" / "tdx_security_names.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _economy_rows(
    root: Path,
    *,
    physical_id: str | None = None,
    limit: int = _MAX_POINTS,
    previews: bool = False,
) -> list[tuple]:
    files = _partition_files(root, _MACRO_PARTITION_PREFIX)
    if not files:
        return []
    # Build two bounded shapes from the same validated source. The explicit
    # regexp below excludes TDX 7_ equity figures and 9_ fund indices.
    base = """
        WITH observations AS (
          SELECT instrument_id,
                 substr(json_extract_string(bar_json, '$.trading_date'), 1, 10) AS day,
                 try_cast(json_extract_string(bar_json, '$.close') AS DOUBLE) AS value,
                 coalesce(json_extract_string(bar_json, '$.fetched_at'), '') AS fetched_at
          FROM read_parquet(?, union_by_name=true)
          WHERE regexp_matches(instrument_id,
                '^CN[.]TDX_MACRO[.]MACRO[.]([1-6]|8)_[A-Z0-9-]+[.]TDX_LOCAL$')
            AND json_extract_string(bar_json, '$.quality_status')='PASS'
            AND json_extract_string(bar_json, '$.normalization_status')='PASS'
        ), valid AS (
          SELECT * FROM observations
          WHERE day ~ '^\\d{4}-\\d{2}-\\d{2}$' AND isfinite(value)
        )
    """
    if previews:
        # Deduplicate each observation date first, then keep a small recent
        # history per instrument. This reads the macro parquet set once for
        # the whole directory instead of issuing a scan for every card.
        sql = base + """
            , distinct_days AS (
              SELECT instrument_id, day, value FROM valid
              QUALIFY row_number() OVER (
                PARTITION BY instrument_id, day ORDER BY fetched_at DESC
              )=1
            )
            SELECT instrument_id, day, value FROM distinct_days
            QUALIFY row_number() OVER (
              PARTITION BY instrument_id ORDER BY day DESC
            )<=?
            ORDER BY instrument_id, day
        """
        parameters: list[Any] = [files, limit]
    elif physical_id is None:
        sql = base + """
            SELECT instrument_id, day, value FROM valid
            QUALIFY row_number() OVER (
              PARTITION BY instrument_id ORDER BY day DESC, fetched_at DESC
            )=1
            ORDER BY instrument_id
        """
        parameters: list[Any] = [files]
    else:
        sql = base + """
            SELECT instrument_id, day, value FROM valid WHERE instrument_id=?
            QUALIFY row_number() OVER (
              PARTITION BY instrument_id, day ORDER BY fetched_at DESC
            )=1
            ORDER BY day DESC LIMIT ?
        """
        parameters = [files, physical_id, limit]
    connection = duckdb.connect(":memory:")
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _global_closes(root: Path) -> dict[str, dict[str, float]]:
    files = _partition_files(root, _FUTURE_PARTITION_PREFIX)
    if not files:
        return {}
    # A fixed source and currency avoid silently joining unlike instruments.
    query = """
        WITH observations AS (
          SELECT instrument_id,
                 substr(json_extract_string(bar_json, '$.trading_date'), 1, 10) AS day,
                 try_cast(json_extract_string(bar_json, '$.close') AS DOUBLE) AS value,
                 coalesce(json_extract_string(bar_json, '$.fetched_at'), '') AS fetched_at
          FROM read_parquet(?, union_by_name=true)
          WHERE instrument_id IN (?, ?, ?, ?)
            AND json_extract_string(bar_json, '$.quality_status')='PASS'
            AND json_extract_string(bar_json, '$.source')='akshare-futures_foreign_hist'
            AND json_extract_string(bar_json, '$.currency')='USD'
        )
        SELECT instrument_id, day, value FROM observations
        WHERE day ~ '^\\d{4}-\\d{2}-\\d{2}$' AND isfinite(value) AND value > 0
        QUALIFY row_number() OVER (
          PARTITION BY instrument_id, day ORDER BY fetched_at DESC
        )=1
        ORDER BY day DESC
    """
    params: list[Any] = [files, *_FUTURES.values()]
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()
    closes: dict[str, dict[str, float]] = {}
    instrument_to_symbol = {instrument_id: symbol for symbol, instrument_id in _FUTURES.items()}
    for instrument_id, day, value in rows:
        closes.setdefault(str(day), {})[instrument_to_symbol[str(instrument_id)]] = float(value)
    return closes


def _ratio_points(closes: dict[str, dict[str, float]], ratio_id: str, limit: int) -> list[dict[str, Any]]:
    denominator_symbol = _RATIO_META[ratio_id][2]
    points: list[dict[str, Any]] = []
    for day in sorted(closes, reverse=True):
        day_closes = closes[day]
        numerator = day_closes.get("GC")
        denominator = day_closes.get(denominator_symbol)
        if numerator is None or denominator is None:
            continue
        if denominator_symbol == "HG":
            # This specific source publishes COMEX HG in US cents/lb.
            denominator /= 100.0
        ratio = numerator / denominator
        if math.isfinite(ratio):
            points.append({"day": day, "value": ratio})
        if len(points) >= limit:
            break
    points.reverse()
    return points


def _ratio_limitations(ratio_id: str) -> list[str]:
    notes = ["仅以同一来源、同一日期的正收盘价计算；缺日不向前填充。"]
    if ratio_id == "GOLD_COPPER_RATIO":
        notes.append("COMEX HG 原报价为美分/磅，计算前换算为美元/磅。")
    return notes


def _macro_item(instrument_id: str, day: str, value: float, names: dict[str, str]) -> dict[str, Any]:
    code = instrument_id.rsplit(".", 2)[1]
    public_id = f"TDX_MACRO.{code}"
    return {
        "id": public_id,
        "name": names.get(public_id) or code,
        "group": "economy",
        "topic": _TOPIC[code[0]],
        "latestDay": day,
        "latestValue": value,
        "unit": _KNOWN_UNITS.get(code, ""),
        "source": _SOURCE_MACRO,
        "available": True,
    }


@router.get("/catalog")
def tdx_economy_catalog(request: Request) -> dict[str, Any]:
    root = Path(request.app.state.data_root)
    names = _name_catalog()
    items = [
        _macro_item(str(instrument_id), str(day), float(value), names)
        for instrument_id, day, value in _economy_rows(root)
        if _PHYSICAL_MACRO_ID.fullmatch(str(instrument_id))
    ]
    closes = _global_closes(root)
    for ratio_id, (name, unit, _denominator) in _RATIO_META.items():
        points = _ratio_points(closes, ratio_id, 1)
        latest = points[-1] if points else None
        items.append({
            "id": ratio_id,
            "name": name,
            "group": "ratio",
            "topic": "商品比值",
            "latestDay": latest["day"] if latest else None,
            "latestValue": latest["value"] if latest else None,
            "unit": unit,
            "source": _SOURCE_RATIO,
            "available": bool(latest),
        })
    return {"items": items}


@router.get("/previews")
def tdx_economy_previews(
    request: Request,
    limit: int = Query(default=36, ge=1, le=120),
) -> dict[str, Any]:
    """Return bounded chart histories for every validated economy series."""

    root = Path(request.app.state.data_root)
    macro_points: dict[str, list[dict[str, Any]]] = {}
    for instrument_id, day, value in _economy_rows(root, limit=limit, previews=True):
        if _PHYSICAL_MACRO_ID.fullmatch(str(instrument_id)) is None:
            continue
        public_id = f"TDX_MACRO.{str(instrument_id).rsplit('.', 2)[1]}"
        macro_points.setdefault(public_id, []).append({"day": str(day), "value": float(value)})
    items = [{"id": id, "points": points} for id, points in sorted(macro_points.items())]
    closes = _global_closes(root)
    items.extend(
        {"id": ratio_id, "points": _ratio_points(closes, ratio_id, limit)}
        for ratio_id in _RATIO_META
    )
    return {"items": items}


@router.get("/series")
def tdx_economy_series(
    request: Request,
    id: str = Query(min_length=1, max_length=80),
    limit: int = Query(default=600, ge=1, le=_MAX_POINTS),
) -> dict[str, Any]:
    root = Path(request.app.state.data_root)
    if id in _RATIO_META:
        name, unit, _denominator = _RATIO_META[id]
        points = _ratio_points(_global_closes(root), id, limit)
        return {
            "id": id, "name": name, "group": "ratio", "topic": "商品比值",
            "unit": unit, "source": _SOURCE_RATIO, "points": points,
            "available": bool(points), "limitations": _ratio_limitations(id),
        }
    match = _MACRO_ID.fullmatch(id)
    if match is None:
        raise HTTPException(status_code=404, detail="未知通达信经济序列")
    code = id.split(".", 1)[1]
    physical_id = f"CN.TDX_MACRO.MACRO.{code}.TDX_LOCAL"
    rows = _economy_rows(root, physical_id=physical_id, limit=limit)
    points = [{"day": str(day), "value": float(value)} for _, day, value in reversed(rows)]
    names = _name_catalog()
    return {
        "id": id, "name": names.get(id) or code, "group": "economy",
        "topic": _TOPIC[match.group(1)], "unit": _KNOWN_UNITS.get(code, ""),
        "source": _SOURCE_MACRO, "points": points, "available": bool(points),
        "limitations": ["日期为通达信终端观测期，不代表公布日期。", "终端未明确标注单位的品种保留原值，单位留空。"],
    }


__all__ = ("router",)
