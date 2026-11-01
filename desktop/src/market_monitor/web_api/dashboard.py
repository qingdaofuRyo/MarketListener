"""Dashboard and metrics API: real-data panels from the local research terminal.

Endpoints:
  GET /api/dashboard/definitions
  GET /api/dashboard/{id}
  GET /api/metrics/ranking
  GET /api/metrics/heatmap

All values come from local silver parquet partitions, the catalog.duckdb
``gold_metrics`` table or the control-center health report.  Empty data is
reported as ``{"available": false}`` instead of synthetic all-zero charts.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query, Request

from market_monitor.control_center import build_control_center_report
from market_monitor.futures_dashboard import compute_futures_breadth
from market_monitor.market_breadth import compute_daily_breadth
from market_monitor.web_api.common import bars_by_instrument, clean, load_inventory, now_iso

MAX_SERIES_POINTS = 1000
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_GOLD_SERIES = 8

# A 股涨跌广度只统计个股。ETF 和指数的价格变化不能混入个股家数，
# 否则“上涨/下跌家数”会与交易所口径不一致。
_CN_STOCK_ASSET_TYPE = "STOCK"
_GOLD_VALUE_METRICS = ("最新价", "收盘", "收盘价", "close", "比特币价格", "以太坊价格")
_EXCLUDED_METRIC_PREFIXES = (
    "FUTURES_BREADTH:",
    "A_SHARE_BREADTH:",
    "FUTURES_OI_LEADERBOARD:",
    "CN_MARGIN:",
    "HSGT_FLOW:",
    "CN_ZT_POOL:",
)

DASHBOARD_SPECS: tuple[dict[str, str], ...] = (
    {
        "id": "market-breadth",
        "title": "A股涨跌广度",
        "category": "breadth",
        "description": "本地 CN 股票日线计算的每日上涨、下跌与平盘家数；涨跌停/连板须由权威涨停池或分板规则另行验证",
    },
    {
        "id": "limit-pool",
        "title": "A股涨停池与连板",
        "category": "breadth",
        "description": "东财权威涨停/跌停池的家数、最高连板高度和昨日涨停今日接盘收益率；仅展示已落库交易日",
    },
    {
        "id": "futures-breadth",
        "title": "期货涨跌家数",
        "category": "breadth",
        "description": "本地 CN 期货日线计算的每日上涨/下跌/平盘家数",
    },
    {
        "id": "gold-metrics",
        "title": "Gold 指标",
        "category": "gold",
        "description": "catalog.duckdb gold_metrics 中的真实指标序列",
    },
    {
        "id": "gold-silver-ratio",
        "title": "金银比",
        "category": "gold",
        "description": "同一交易日、同一美元计价口径下的 COMEX 黄金收盘价 ÷ 白银收盘价；仅展示本地已落库的真实派生序列",
    },
    {
        "id": "storage",
        "title": "存储占用",
        "category": "storage",
        "description": "bronze/silver/quarantine 本地存储占用（字节）",
    },
    {
        "id": "quality",
        "title": "数据质量",
        "category": "quality",
        "description": "Silver 分区质量状态与隔离区问题数",
    },
    {
        "id": "freshness",
        "title": "数据新鲜度",
        "category": "freshness",
        "description": "Silver 分区新鲜/陈旧数量",
    },
    {
        "id": "runs",
        "title": "采集运行",
        "category": "runs",
        "description": "数据源运行记录状态计数",
    },
    {
        "id": "partitions",
        "title": "Silver 分区",
        "category": "partitions",
        "description": "Silver 分区数与入库行数",
    },
)
ALLOWED_IDS = frozenset(spec["id"] for spec in DASHBOARD_SPECS)
RANKING_CATEGORIES = frozenset({"futures", "gold", "breadth"})
HEATMAP_CATEGORIES = frozenset({"breadth", "gold", "storage"})

dashboard_router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
metrics_router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _resolve_data_root(request: Request) -> Path:
    """Locate the local data_control root.

    Prefers ``app.state.data_root`` (set by the FastAPI host); falls back to
    the repository convention ``<cwd>/data_control`` so the router still works
    when the host has not exposed the injected root on app state.
    """
    state = getattr(request.app, "state", None)
    configured = getattr(state, "data_root", None) if state is not None else None
    if configured:
        return Path(configured)
    return Path.cwd() / "data_control"


def _clean_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _downsample_points(points: Sequence[dict[str, Any]], max_points: int = MAX_SERIES_POINTS) -> list[dict[str, Any]]:
    """Server-side equal-spacing downsampling, always keeping the last point."""
    if len(points) <= max_points:
        return list(points)
    step = len(points) / max_points
    indices = {min(len(points) - 1, round(index * step)) for index in range(max_points)}
    indices.add(len(points) - 1)
    return [points[index] for index in sorted(indices)]


def _gold_metric_rows(
    data_root: Path,
    *,
    where: str = "",
    params: Sequence[Any] = (),
    limit: int = 100000,
) -> list[tuple[str, str, str, float, str]]:
    catalog = Path(data_root) / "catalog.duckdb"
    if not catalog.is_file():
        return []
    try:
        import duckdb

        connection = duckdb.connect(str(catalog))
        try:
            query = (
                "SELECT instrument_id, trading_date, metric_name, value, metric_id "
                "FROM gold_metrics"
            )
            if where:
                query += f" WHERE {where}"
            query += " ORDER BY trading_date ASC, metric_id ASC"
            if limit and limit > 0:
                query += f" LIMIT {int(limit)}"
            return [
                (str(row[0]), str(row[1]), str(row[2]), float(row[3]), str(row[4]))
                for row in connection.execute(query, list(params)).fetchall()
                if row[3] is not None
            ]
        finally:
            connection.close()
    except Exception:
        return []


def _inventory_groups(data_root: Path) -> tuple[set[str], set[str]]:
    if not (Path(data_root) / "silver").is_dir():
        return set(), set()
    inventory = load_inventory(data_root)
    market_ids: set[str] = set()
    futures_ids: set[str] = set()
    for instrument_id, info in inventory.instruments.items():
        if info.get("market") != "CN":
            continue
        asset_type = str(info.get("assetType") or "")
        if asset_type == _CN_STOCK_ASSET_TYPE:
            market_ids.add(instrument_id)
        elif asset_type == "FUTURE":
            futures_ids.add(instrument_id)
    return market_ids, futures_ids


def _all_daily_bars(data_root: Path) -> dict[str, list[dict[str, Any]]]:
    return bars_by_instrument(
        data_root,
        period="1d",
        limit_per_instrument=2000,
        max_instruments=1000,
    )


def _group_by_day(bars: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for instrument_bars in bars.values():
        for bar in instrument_bars:
            day = str(bar.get("trading_date") or bar.get("trading_day") or "").strip()
            if day:
                grouped.setdefault(day, []).append(dict(bar))
    return grouped


def _market_breadth_snapshots(data_root: Path) -> list[Any]:
    market_ids, _ = _inventory_groups(data_root)
    if not market_ids:
        return []
    all_bars = _all_daily_bars(data_root)
    filtered = {instrument_id: bars for instrument_id, bars in all_bars.items() if instrument_id in market_ids}
    grouped = _group_by_day(filtered)
    if not grouped:
        return []
    try:
        return compute_daily_breadth(grouped)
    except Exception:
        return []


def _futures_breadth_snapshots(data_root: Path) -> list[Any]:
    _, futures_ids = _inventory_groups(data_root)
    if not futures_ids:
        return []
    all_bars = _all_daily_bars(data_root)
    filtered = {instrument_id: bars for instrument_id, bars in all_bars.items() if instrument_id in futures_ids}
    grouped = _group_by_day(filtered)
    if not grouped:
        return []
    try:
        return compute_futures_breadth(grouped)
    except Exception:
        return []


def _snapshot_series(
    snapshots: Sequence[Any],
    fields: Sequence[tuple[str, str]],
    *,
    day_attribute: str = "trading_date",
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for name, attribute in fields:
        points = [{"t": getattr(snapshot, day_attribute), "value": getattr(snapshot, attribute)} for snapshot in snapshots]
        series.append({"name": name, "points": _downsample_points(points)})
    return series


def _market_breadth_dashboard(data_root: Path) -> dict[str, Any]:
    snapshots = _market_breadth_snapshots(data_root)
    if not snapshots:
        return {"available": False}
    return {
        "available": True,
        "id": "market-breadth",
        "title": "市场广度",
        "unit": "家数",
        "series": _snapshot_series(
            snapshots,
            (
                ("上涨", "advances"),
                ("下跌", "declines"),
                ("平盘", "unchanged"),
            ),
        ),
        "generatedAt": now_iso(),
        "source": "local-computed",
    }


def _limit_pool_dashboard(data_root: Path) -> dict[str, Any]:
    rows = _gold_metric_rows(data_root, where="metric_id LIKE ?", params=["CN_ZT_POOL:%"])
    if not rows:
        return {"available": False}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for _instrument_id, trading_date, metric_name, value, _metric_id in rows:
        grouped.setdefault(metric_name, []).append({"t": trading_date, "value": value})
    series = [
        {"name": metric_name, "points": _downsample_points(points)}
        for metric_name, points in sorted(grouped.items())
    ]
    return {
        "available": True,
        "id": "limit-pool",
        "title": "A股涨停池与连板",
        "unit": "家数 / 高度 / %",
        "series": series,
        "generatedAt": now_iso(),
        "source": "akshare-eastmoney: CN_ZT_POOL",
    }


def _futures_breadth_dashboard(data_root: Path) -> dict[str, Any]:
    snapshots = _futures_breadth_snapshots(data_root)
    if not snapshots:
        return {"available": False}
    return {
        "available": True,
        "id": "futures-breadth",
        "title": "期货涨跌家数",
        "unit": "家数",
        "series": _snapshot_series(
            snapshots,
            (
                ("上涨", "advances"),
                ("下跌", "declines"),
                ("平盘", "unchanged"),
            ),
            day_attribute="trading_day",
        ),
        "generatedAt": now_iso(),
        "source": "local-computed",
    }


def _gold_metrics_dashboard(data_root: Path) -> dict[str, Any]:
    rows = _gold_metric_rows(data_root)
    if not rows:
        return {"available": False}
    grouped: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for instrument_id, trading_date, metric_name, value, metric_id in rows:
        if metric_id.startswith(_EXCLUDED_METRIC_PREFIXES):
            continue
        grouped.setdefault((instrument_id, metric_name), []).append((trading_date, value))
    if not grouped:
        return {"available": False}
    chosen = sorted(
        grouped,
            key=lambda key: (-len(grouped[key]), key[1], key[0]),
    )[:MAX_GOLD_SERIES]
    series: list[dict[str, Any]] = []
    name_counts = Counter(key[1] for key in chosen)
    for instrument_id, metric_name in chosen:
        points = [{"t": day, "value": value} for day, value in grouped[(instrument_id, metric_name)]]
        name = metric_name if name_counts[metric_name] == 1 else f"{metric_name} · {instrument_id}"
        series.append({"name": name, "points": _downsample_points(points)})
    return {
        "available": True,
        "id": "gold-metrics",
        "title": "Gold 指标",
        "unit": "指标值",
        "series": series,
        "generatedAt": now_iso(),
        "source": "catalog.duckdb",
    }


def _gold_silver_ratio_dashboard(data_root: Path) -> dict[str, Any]:
    rows = _gold_metric_rows(data_root, where="metric_id LIKE ?", params=["GOLD_SILVER_RATIO:%"])
    if not rows:
        return {"available": False}
    points = [{"t": trading_date, "value": value} for _instrument_id, trading_date, _name, value, _metric_id in rows]
    return {
        "available": True,
        "id": "gold-silver-ratio",
        "title": "金银比",
        "unit": "比值",
        "series": [{"name": "金银比", "points": _downsample_points(points)}],
        "generatedAt": now_iso(),
        "source": "local-derived: COMEX GC/SI close",
    }


def _control_center_dashboard(dashboard_id: str, data_root: Path) -> dict[str, Any]:
    report = build_control_center_report(data_root)
    generated_at = str(report.get("generated_at") or now_iso())
    if dashboard_id == "storage":
        storage = dict(report.get("storage") or {})
        if not storage or not any(int(value) > 0 for value in storage.values()):
            return {"available": False}
        series = [
            {"name": str(area), "points": [{"t": generated_at, "value": int(size)}]}
            for area, size in sorted(storage.items())
        ]
        return {
            "available": True,
            "id": "storage",
            "title": "存储占用",
            "unit": "字节",
            "series": series,
            "generatedAt": generated_at,
            "source": "control-center",
        }
    if dashboard_id == "quality":
        partitions = list(report.get("partitions") or [])
        quarantine = list(report.get("quarantine") or [])
        if not partitions and not quarantine:
            return {"available": False}
        series = [
            {
                "name": f"质量-{status}",
                "points": [{"t": generated_at, "value": count}],
            }
            for status, count in sorted(Counter(str(row.get("status") or "UNKNOWN") for row in partitions).items())
        ]
        if quarantine:
            issue_count = sum(int(row.get("issue_count") or 0) for row in quarantine)
            series.append({"name": "隔离问题数", "points": [{"t": generated_at, "value": issue_count}]})
        return {
            "available": True,
            "id": "quality",
            "title": "数据质量",
            "unit": "分区数",
            "series": series,
            "generatedAt": generated_at,
            "source": "control-center",
        }
    if dashboard_id == "freshness":
        partitions = list(report.get("partitions") or [])
        if not partitions:
            return {"available": False}
        stale = sum(1 for row in partitions if bool(row.get("stale")))
        series = [
            {"name": "新鲜", "points": [{"t": generated_at, "value": len(partitions) - stale}]},
            {"name": "陈旧", "points": [{"t": generated_at, "value": stale}]},
        ]
        return {
            "available": True,
            "id": "freshness",
            "title": "数据新鲜度",
            "unit": "分区数",
            "series": series,
            "generatedAt": generated_at,
            "source": "control-center",
        }
    if dashboard_id == "runs":
        runs = sorted(report.get("runs") or [], key=lambda row: str(row.get("started_at") or ""))
        if not runs:
            return {"available": False}
        status_counts: Counter[str] = Counter()
        total_points: list[dict[str, Any]] = []
        by_status: dict[str, list[dict[str, Any]]] = {}
        for index, row in enumerate(runs, start=1):
            started_at = str(row.get("started_at") or generated_at)
            status = str(row.get("status") or "UNKNOWN")
            status_counts[status] += 1
            by_status.setdefault(status, []).append({"t": started_at, "value": status_counts[status]})
            total_points.append({"t": started_at, "value": index})
        series = [{"name": "运行总数", "points": _downsample_points(total_points)}]
        series.extend(
            {"name": f"状态-{status}", "points": _downsample_points(points)}
            for status, points in sorted(by_status.items())
        )
        return {
            "available": True,
            "id": "runs",
            "title": "采集运行",
            "unit": "次数",
            "series": series,
            "generatedAt": generated_at,
            "source": "control-center",
        }
    if dashboard_id == "partitions":
        partitions = sorted(report.get("partitions") or [], key=lambda row: str(row.get("updated_at") or ""))
        if not partitions:
            return {"available": False}
        count_points: list[dict[str, Any]] = []
        rows_points: list[dict[str, Any]] = []
        total_rows = 0
        for index, row in enumerate(partitions, start=1):
            updated_at = str(row.get("updated_at") or generated_at)
            total_rows += int(row.get("row_count") or 0)
            count_points.append({"t": updated_at, "value": index})
            rows_points.append({"t": updated_at, "value": total_rows})
        series = [
            {"name": "分区数", "points": _downsample_points(count_points)},
            {"name": "入库行数", "points": _downsample_points(rows_points)},
        ]
        return {
            "available": True,
            "id": "partitions",
            "title": "Silver 分区",
            "unit": "分区/行",
            "series": series,
            "generatedAt": generated_at,
            "source": "control-center",
        }
    raise HTTPException(status_code=404, detail="unknown dashboard")


def _dashboard_payload(dashboard_id: str, data_root: Path) -> dict[str, Any]:
    if dashboard_id == "market-breadth":
        return _market_breadth_dashboard(data_root)
    if dashboard_id == "limit-pool":
        return _limit_pool_dashboard(data_root)
    if dashboard_id == "futures-breadth":
        return _futures_breadth_dashboard(data_root)
    if dashboard_id == "gold-metrics":
        return _gold_metrics_dashboard(data_root)
    if dashboard_id == "gold-silver-ratio":
        return _gold_silver_ratio_dashboard(data_root)
    return _control_center_dashboard(dashboard_id, data_root)


def _availability(data_root: Path) -> dict[str, bool]:
    market_ids, futures_ids = _inventory_groups(data_root)
    report = build_control_center_report(data_root)
    storage = dict(report.get("storage") or {})
    partitions = list(report.get("partitions") or [])
    return {
        "market-breadth": bool(market_ids),
        "limit-pool": bool(_gold_metric_rows(data_root, where="metric_id LIKE ?", params=["CN_ZT_POOL:%"], limit=1)),
        "futures-breadth": bool(futures_ids),
        "gold-metrics": bool(_gold_metric_rows(data_root, limit=1)),
        "gold-silver-ratio": bool(_gold_metric_rows(data_root, where="metric_id LIKE ?", params=["GOLD_SILVER_RATIO:%"], limit=1)),
        "storage": bool(storage) and any(int(value) > 0 for value in storage.values()),
        "quality": bool(partitions) or bool(report.get("quarantine")),
        "freshness": bool(partitions),
        "runs": bool(report.get("runs")),
        "partitions": bool(partitions),
    }


def _ranking_frames(
    data_root: Path,
    category: str,
    limit: int,
) -> list[dict[str, Any]]:
    if category == "futures":
        rows = _gold_metric_rows(data_root, where="metric_name = ?", params=["净持仓"])
        frames: dict[str, list[dict[str, Any]]] = {}
        for instrument_id, trading_date, _metric_name, value, _metric_id in rows:
            frames.setdefault(trading_date, []).append({"name": instrument_id, "value": value})
    elif category == "gold":
        placeholders = ", ".join("?" for _ in _GOLD_VALUE_METRICS)
        rows = _gold_metric_rows(
            data_root,
            where=f"metric_name IN ({placeholders})",
            params=list(_GOLD_VALUE_METRICS),
        )
        frames = {}
        seen: set[tuple[str, str]] = set()
        for instrument_id, trading_date, _metric_name, value, _metric_id in rows:
            key = (trading_date, instrument_id)
            if key in seen:
                continue
            seen.add(key)
            frames.setdefault(trading_date, []).append({"name": instrument_id, "value": value})
    else:  # breadth
        snapshots = _market_breadth_snapshots(data_root)
        frames = {
            snapshot.trading_date: [
                {"name": "上涨", "value": snapshot.advances},
                {"name": "下跌", "value": snapshot.declines},
            ]
            for snapshot in snapshots
        }
    output: list[dict[str, Any]] = []
    for trading_date in sorted(frames, reverse=True):
        items = sorted(frames[trading_date], key=lambda item: item["value"], reverse=True)[:limit]
        output.append({"t": trading_date, "items": items})
        if len(output) >= limit:
            break
    return output


def _heatmap_payload(data_root: Path, category: str, limit: int) -> dict[str, Any]:
    if category == "breadth":
        snapshots = _market_breadth_snapshots(data_root)
        if not snapshots:
            return {"available": False, "x": [], "y": [], "cells": []}
        indicators = (
            ("上涨", "advances"),
            ("下跌", "declines"),
            ("平盘", "unchanged"),
        )
        dates = [snapshot.trading_date for snapshot in snapshots][-limit:]
        by_date = {snapshot.trading_date: snapshot for snapshot in snapshots}
        y = [name for name, _attribute in indicators]
        cells = [
            {"x": x_index, "y": y_index, "value": getattr(by_date[day], attribute)}
            for y_index, (_name, attribute) in enumerate(indicators)
            for x_index, day in enumerate(dates)
        ]
        return {"category": category, "available": True, "x": dates, "y": y, "cells": cells}
    if category == "gold":
        placeholders = ", ".join("?" for _ in _GOLD_VALUE_METRICS)
        rows = _gold_metric_rows(
            data_root,
            where=f"metric_name IN ({placeholders})",
            params=list(_GOLD_VALUE_METRICS),
        )
        if not rows:
            return {"available": False, "x": [], "y": [], "cells": []}
        value_map: dict[tuple[str, str], float] = {}
        for instrument_id, trading_date, _metric_name, value, _metric_id in rows:
            key = (trading_date, instrument_id)
            if key not in value_map:
                value_map[key] = value
        dates = sorted({day for day, _instrument in value_map})[-limit:]
        instruments = sorted({instrument for _day, instrument in value_map})
        cells = [
            {"x": x_index, "y": y_index, "value": value_map[(day, instrument)]}
            for y_index, instrument in enumerate(instruments)
            for x_index, day in enumerate(dates)
            if (day, instrument) in value_map
        ]
        return {"category": category, "available": True, "x": dates, "y": instruments, "cells": cells}
    # storage: areas on x, dates on y
    report = build_control_center_report(data_root)
    storage = dict(report.get("storage") or {})
    if not storage or not any(int(value) > 0 for value in storage.values()):
        return {"available": False, "x": [], "y": [], "cells": []}
    areas = sorted(storage)[:limit]
    dates = [str(report.get("generated_at") or now_iso())]
    cells = [{"x": x_index, "y": 0, "value": int(storage[area])} for x_index, area in enumerate(areas)]
    return {"category": category, "available": True, "x": areas, "y": dates, "cells": cells}


@dashboard_router.get("/definitions")
def dashboard_definitions(request: Request) -> dict[str, Any]:
    from market_monitor.admin_boundary import INTERNAL_DASHBOARDS

    data_root = _resolve_data_root(request)
    availability = _availability(data_root)
    items = [
        {
            "id": spec["id"],
            "title": spec["title"],
            "category": spec["category"],
            "available": bool(availability[spec["id"]]),
            "description": spec["description"],
        }
        for spec in DASHBOARD_SPECS
        if not (getattr(request.app.state, "customer_surface", False)
                and not getattr(request.state, "internal_authorized", False)
                and spec["id"] in INTERNAL_DASHBOARDS)
    ]
    return clean({"items": items})


@dashboard_router.get("/{dashboard_id}")
def dashboard_detail(dashboard_id: str, request: Request) -> dict[str, Any]:
    if dashboard_id not in ALLOWED_IDS:
        raise HTTPException(status_code=404, detail="unknown dashboard")
    data_root = _resolve_data_root(request)
    return clean(_dashboard_payload(dashboard_id, data_root))


@metrics_router.get("/ranking")
def metrics_ranking(
    request: Request,
    category: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    if category not in RANKING_CATEGORIES:
        raise HTTPException(status_code=400, detail="unknown ranking category")
    data_root = _resolve_data_root(request)
    frames = _ranking_frames(data_root, category, _clean_limit(limit))
    if not frames:
        return clean({"category": category, "available": False, "frames": []})
    return clean({"category": category, "available": True, "frames": frames})


@metrics_router.get("/heatmap")
def metrics_heatmap(
    request: Request,
    category: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    if category not in HEATMAP_CATEGORIES:
        raise HTTPException(status_code=400, detail="unknown heatmap category")
    data_root = _resolve_data_root(request)
    payload = _heatmap_payload(data_root, category, _clean_limit(limit))
    payload.setdefault("category", category)
    return clean(payload)


__all__ = ("dashboard_router", "metrics_router")
