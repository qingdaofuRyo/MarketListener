"""Read-only R4 data-section metadata and macro-series endpoints."""

from __future__ import annotations

from collections import defaultdict
import re
from pathlib import Path
import threading
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request

from market_monitor.market_data_version import market_data_version
from market_monitor.macro_series import macro_series_index


router = APIRouter(prefix="/api/data", tags=["data-sections"])
_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])(?:-\d{2})?$")

# Every requested R4 macro series is registered in ``macro_series``.  The
# display catalogue derives availability only from persisted Gold rows.
_CN_IDS = (
    "M2_MONEY_SUPPLY", "M1_MONEY_SUPPLY", "M0_MONEY_SUPPLY", "CN_IMPORT_USD_YOY",
    "CN_EXPORT_USD_YOY", "CN_TRADE_BALANCE_USD", "CN_RETAIL_SALES_YOY", "CN_RETAIL_SALES_MOM",
    "PPI", "PMI_MANUFACTURING", "CPI", "CN_FOREX_RESERVES", "CN_ELECTRICITY_CONSUMPTION",
    "DR007", "CN10Y_YIELD",
)
_US_IDS = ("US_CORE_PCE_QOQ_SAAR_FINAL", "US_IMPORTS_SA", "US_NONFARM_PAYROLLS_SA", "FED_FUNDS_RATE")
_US_MACRO_SERIES_IDS = frozenset(_US_IDS)
_MACRO_TOPIC_BY_ID = {
    "M0_MONEY_SUPPLY": "货币与利率",
    "M1_MONEY_SUPPLY": "货币与利率",
    "M2_MONEY_SUPPLY": "货币与利率",
    "DR007": "货币与利率",
    "CN10Y_YIELD": "货币与利率",
    "FED_FUNDS_RATE": "货币与利率",
    "CN_IMPORT_USD_YOY": "贸易",
    "CN_EXPORT_USD_YOY": "贸易",
    "CN_TRADE_BALANCE_USD": "贸易",
    "US_IMPORTS_SA": "贸易",
    "CN_RETAIL_SALES_YOY": "消费与生产",
    "CN_RETAIL_SALES_MOM": "消费与生产",
    "CN_ELECTRICITY_CONSUMPTION": "消费与生产",
    "PMI_MANUFACTURING": "消费与生产",
    "PPI": "物价",
    "CPI": "物价",
    "US_CORE_PCE_QOQ_SAAR_FINAL": "物价",
    "CN_FOREX_RESERVES": "外汇",
    "US_NONFARM_PAYROLLS_SA": "就业",
}
_SOURCE_DATE_MACRO_IDS = frozenset({
    "CN_IMPORT_USD_YOY",
    "CN_EXPORT_USD_YOY",
    "CN_TRADE_BALANCE_USD",
    "CN_FOREX_RESERVES",
    "US_NONFARM_PAYROLLS_SA",
})
_EQUITY_LIST_TYPES: dict[str, str] = {
    "st_warning": "ST 风险警示",
    "delisting_warning": "退市风险警示",
    "regulatory": "监管期",
    "suspension": "停牌期",
}
_HK_OVERVIEW_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_HK_OVERVIEW_LOCK = threading.Lock()


def _macro_metadata(series_id: str) -> dict[str, str]:
    definition = macro_series_index().get(series_id)
    if definition is not None:
        country = "US" if series_id in _US_MACRO_SERIES_IDS else "CN"
        metadata = {
            "seriesId": series_id,
            "country": country,
            "topic": _MACRO_TOPIC_BY_ID.get(series_id, "其他宏观"),
            "name": definition.name,
            "frequency": definition.frequency,
            "unit": definition.unit,
            "source": definition.source,
            "definition": definition.definition,
        }
        metadata["timeBasis"] = (
            "SOURCE_DATE" if series_id in _SOURCE_DATE_MACRO_IDS else "OBSERVATION_PERIOD"
        )
        return metadata
    raise ValueError(f"unknown registered macro series: {series_id}")


def _macro_availability(root: Path) -> dict[str, dict[str, str]]:
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return {}
    all_ids = [*_CN_IDS, *_US_IDS]
    placeholders = ", ".join("?" for _ in all_ids)
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute(
            f"""SELECT instrument_id, MAX(trading_date) AS latest_observation_period,
            MAX(timestamp) AS latest_fetched_at
            FROM gold_metrics
            WHERE instrument_id IN ({placeholders})
            GROUP BY instrument_id""",
            all_ids,
        ).fetchall()
    finally:
        connection.close()
    return {
        str(instrument_id): {
            "latestObservationPeriod": str(latest_observation_period),
            "latestFetchedAt": str(latest_fetched_at),
        }
        for instrument_id, latest_observation_period, latest_fetched_at in rows
    }


@router.get("/sections")
def data_sections(request: Request) -> dict[str, Any]:
    root = Path(request.app.state.data_root)
    availability = _macro_availability(root)
    a_share = _a_share_snapshot(root)
    northbound = _northbound_snapshot(root)
    # The HK aggregate is deliberately computed on first HK-tab access.  Do
    # not make the default A-share page wait for a multi-year HK daily scan.
    hk_points = _cached_hk_tdx_overview_rows(root)
    return {
        "sections": [
            {
                "id": "cn-equities",
                "title": "A股",
                "panels": [
                    _a_share_panel(
                        "cn-market-cap",
                        "A股市值",
                        a_share,
                        ("TOTAL_MARKET_CAP_YI",),
                        fallback_status="UNAVAILABLE",
                    ),
                    _a_share_panel(
                        "cn-turnover",
                        "A股成交额",
                        a_share,
                        ("TOTAL_AMOUNT_YI",),
                        fallback_status="UNAVAILABLE",
                    ),
                    _a_share_panel(
                        "cn-breadth",
                        "A股涨跌家数",
                        a_share,
                        ("ADVANCES", "UNCHANGED", "DECLINES"),
                        fallback_status="UNAVAILABLE",
                        partial=True,
                    ),
                    _a_share_panel(
                        "cn-limit-pool",
                        "涨跌停与一字板",
                        a_share,
                        ("涨停_COUNT", "跌停_COUNT"),
                        fallback_status="UNAVAILABLE",
                        partial=True,
                    ),
                    _unavailable_panel(
                        "cn-high-amplitude-low-return",
                        "高振幅且低涨幅家数",
                        "尚未锁定经真实分布审计的振幅和绝对涨幅阈值，不能以临时阈值统计。",
                    ),
                    _unavailable_panel(
                        "cn-risk-lists",
                        "ST 与退市风险警示名单",
                        "尚无带生效日期的交易所证券状态本地观测，不能按当前名称推断历史状态。",
                    ),
                    _northbound_panel(northbound),
                    _unavailable_panel(
                        "cn-margin",
                        "沪深京融资融券",
                        "融资融券派生项和两融余额占流通市值的同日分母尚未通过本地 Gold 质量门。",
                    ),
                    _unavailable_panel(
                        "cn-failed-limit-up-rate",
                        "涨停炸板率",
                        "缺少盘中触及合法涨停价且收盘未封住的可追溯事件池，未作阈值近似。",
                    ),
                    _unavailable_panel(
                        "cn-regulatory-suspension-lists",
                        "监管期与停牌期名单",
                        "尚无同时覆盖个股与 ETF、含生效区间和来源公告的本地状态名单。",
                    ),
                ],
            },
            {
                "id": "hk-equities",
                "title": "港股",
                "panels": [
                    _unavailable_panel(
                        "hk-market-cap",
                        "港股市值",
                        "本机未保存带上市状态与历史时点的港股市值分母，不能由当前 F10 值回填历史总市值。",
                    ),
                    _hk_overview_panel("hk-turnover", "港股成交额", hk_points, ("turnoverYi",)),
                    _hk_overview_panel(
                        "hk-breadth",
                        "港股上涨/持平/下跌家数",
                        hk_points,
                        ("advances", "unchanged", "declines"),
                    ),
                    _unavailable_panel(
                        "hk-status-lists",
                        "港股状态名单",
                        "港股停牌和扩展状态面板尚未形成经过来源、统计范围和历史长度验证的本地观测。",
                    ),
                ],
            },
            {
                "id": "macro",
                "title": "其他数据",
                "panels": [
                    {"id": "macro-cn", "title": "中国宏观", "status": "PARTIAL", "availableSeries": sum(item in availability for item in _CN_IDS)},
                    {"id": "macro-us", "title": "美国宏观", "status": "PARTIAL", "availableSeries": sum(item in availability for item in _US_IDS)},
                ],
            },
        ],
        "generatedAt": None,
    }


@router.get("/equities/{market}/overview")
def equity_overview(
    market: str,
    request: Request,
    start_day: str | None = Query(default=None, alias="startDay"),
    end_day: str | None = Query(default=None, alias="endDay"),
    segment: str | None = None,
) -> dict[str, Any]:
    """Expose audited A-share Gold and local-TDX Hong Kong daily aggregates."""

    normalized_market = market.strip().lower()
    if normalized_market not in {"cn", "hk"}:
        raise HTTPException(status_code=422, detail="market must be cn or hk")
    _validate_day(start_day, "startDay")
    _validate_day(end_day, "endDay")
    if start_day and end_day and start_day > end_day:
        raise HTTPException(status_code=422, detail="startDay must not be after endDay")
    normalized_segment = segment.strip().upper() if segment else "ALL"
    if normalized_market == "hk":
        if normalized_segment != "ALL":
            return _unavailable_equity_overview(
                market="HK",
                segment=normalized_segment,
                limitation="本机未建立港股主板、创业板等带历史生效日的板块主数据，不能将下载文件前缀当成完整板块范围。",
                currency="HKD",
            )
        points = _filter_equity_points(_hk_tdx_overview_rows(Path(request.app.state.data_root)), start_day, end_day)
        return {
            "available": bool(points),
            "market": "HK",
            "segment": "ALL",
            "currency": "HKD",
            "units": {"marketCap": "亿港元", "turnover": "亿港元", "breadth": "家", "limitPool": "家"},
            "points": points,
            "source": "通达信金融终端（本地）港股日线",
            "limitations": [
                "成交额和涨跌家数只统计本机已导入、通过 tdx-cn-v2 质量门的港股股票日线；每个交易日的覆盖数量见 coverage 字段，不能视为港股全市场完整口径。",
                "港股总市值、流通市值、涨跌停和状态名单没有带历史上市状态的权威本地观测，保持空值或不可用。",
            ] if points else ["尚无通过 tdx-cn-v2 质量门的港股股票日线。"],
        }
    if normalized_segment != "ALL":
        return _unavailable_equity_overview(
            market="CN",
            segment=normalized_segment,
            limitation="本地 A 股 Gold 目前只验证全市场汇总，尚未按沪深主板、北交所、创业板、科创板拆分。",
            currency="CNY",
        )
    points = _a_share_overview_rows(Path(request.app.state.data_root), start_day, end_day)
    return {
        "available": bool(points),
        "market": "CN",
        "segment": "ALL",
        "currency": "CNY",
        "units": {"marketCap": "亿元", "turnover": "亿元", "breadth": "家", "limitPool": "家"},
        "points": points,
        "limitations": [
            "当前仅为沪深京全市场汇总；四类板块、流通市值、一字板、炸板率和两融口径尚未完成本地 Gold 验证。",
        ] if points else ["尚无本地 A 股总览 Gold 观测。"],
    }


@router.get("/equities/{market}/lists")
def equity_status_list(
    market: str,
    list_type: str = Query(alias="type"),
    as_of_day: str | None = Query(default=None, alias="asOfDay"),
    segment: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, alias="pageSize", ge=1, le=200),
) -> dict[str, Any]:
    """Reserve a traceable, paginated status-list contract without inventing rows.

    Security statuses are point-in-time facts.  Until an official source with
    effective dates is persisted, returning an empty *available* table would
    misleadingly claim that there are no warned or suspended instruments.
    """

    normalized_market = market.strip().lower()
    if normalized_market not in {"cn", "hk"}:
        raise HTTPException(status_code=422, detail="market must be cn or hk")
    if list_type not in _EQUITY_LIST_TYPES:
        raise HTTPException(status_code=422, detail="unknown equity list type")
    _validate_day(as_of_day, "asOfDay")
    normalized_segment = segment.strip().upper() if segment else "ALL"
    market_label = "A 股" if normalized_market == "cn" else "港股"
    type_label = _EQUITY_LIST_TYPES[list_type]
    limitation = (
        f"{market_label} {type_label}尚无带生效日期、来源公告和抓取时间的本地权威观测；"
        "不会以当前名称、历史最后状态或空表代替事实。"
    )
    if normalized_segment != "ALL":
        limitation = f"{market_label}{normalized_segment} 分板块状态名单尚未完成 point-in-time 主数据映射。"
    return {
        "available": False,
        "market": normalized_market.upper(),
        "segment": normalized_segment,
        "listType": list_type,
        "listTitle": type_label,
        "asOfDay": as_of_day,
        "page": page,
        "pageSize": page_size,
        "total": None,
        "items": [],
        "limitations": [limitation],
    }


def _a_share_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    """Read only persisted A-share summary metrics; never derive from samples."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return {}
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute(
            """SELECT metric_id, trading_date, metric_name, value, definition, calculation_method, timestamp
            FROM gold_metrics
            WHERE metric_id LIKE 'A_SHARE_BREADTH:%' OR metric_id LIKE 'CN_ZT_POOL:CN.A_SHARE.%'
            ORDER BY trading_date DESC, timestamp DESC"""
        ).fetchall()
    finally:
        connection.close()
    result: dict[str, dict[str, Any]] = {}
    for metric_id, trading_date, metric_name, value, definition, calculation_method, timestamp in rows:
        key = str(metric_id).rsplit(":", 1)[-1]
        if key in result:
            continue
        result[key] = {
            "metricKey": key,
            "tradingDate": str(trading_date),
            "metricName": str(metric_name),
            "value": float(value),
            "definition": str(definition),
            "calculationMethod": str(calculation_method),
            "timestamp": str(timestamp),
        }
    return result


def _northbound_snapshot(root: Path) -> list[dict[str, Any]]:
    """Return persisted northbound fields without blending them into margin data."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute(
            """SELECT metric_id, trading_date, metric_name, value, definition, calculation_method, timestamp
            FROM gold_metrics
            WHERE instrument_id = 'CN.HSGT.北向'
            QUALIFY row_number() OVER (PARTITION BY metric_id ORDER BY trading_date DESC, timestamp DESC) = 1
            ORDER BY metric_id"""
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "metricKey": str(metric_id).rsplit(":", 1)[-1],
            "tradingDate": str(trading_date),
            "metricName": str(metric_name),
            "value": float(value),
            "definition": str(definition),
            "calculationMethod": str(calculation_method),
            "timestamp": str(timestamp),
        }
        for metric_id, trading_date, metric_name, value, definition, calculation_method, timestamp in rows
        if value is not None
    ]


def _a_share_overview_rows(
    root: Path,
    start_day: str | None,
    end_day: str | None,
) -> list[dict[str, Any]]:
    """Read the historical aggregate without interpreting unavailable keys as zero."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    clauses = ["(metric_id LIKE 'A_SHARE_BREADTH:%' OR metric_id LIKE 'CN_ZT_POOL:CN.A_SHARE.%')"]
    params: list[Any] = []
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute(
            f"""SELECT metric_id, trading_date, value, timestamp FROM gold_metrics
            WHERE {' AND '.join(clauses)}
            QUALIFY row_number() OVER (PARTITION BY metric_id, trading_date ORDER BY timestamp DESC) = 1
            ORDER BY trading_date, metric_id""",
            params,
        ).fetchall()
    finally:
        connection.close()
    fields = {
        "TOTAL_MARKET_CAP_YI": "totalMarketCapYi",
        "FLOAT_MARKET_CAP_YI": "floatMarketCapYi",
        "TOTAL_AMOUNT_YI": "turnoverYi",
        "ADVANCES": "advances",
        "UNCHANGED": "unchanged",
        "DECLINES": "declines",
        "涨停_COUNT": "limitUpCount",
        "跌停_COUNT": "limitDownCount",
        "LIMIT_UP": "limitUpCount",
        "LIMIT_DOWN": "limitDownCount",
    }
    by_day: dict[str, dict[str, Any]] = {}
    for metric_id, trading_day, value, timestamp in rows:
        key = str(metric_id).rsplit(":", 1)[-1]
        field = fields.get(key)
        if field is None or value is None:
            continue
        day = _normalized_trading_day(str(trading_day))
        point = by_day.setdefault(
            day,
            {
                "tradingDay": day,
                "totalMarketCapYi": None,
                "floatMarketCapYi": None,
                "turnoverYi": None,
                "advances": None,
                "unchanged": None,
                "declines": None,
                "limitUpCount": None,
                "limitDownCount": None,
                "updatedAt": str(timestamp),
            },
        )
        point[field] = float(value)
        if str(timestamp) > str(point["updatedAt"]):
            point["updatedAt"] = str(timestamp)
    return [
        by_day[day] for day in sorted(by_day)
        if (start_day is None or day >= start_day) and (end_day is None or day <= end_day)
    ]


def _hk_tdx_overview_rows(root: Path) -> list[dict[str, Any]]:
    """Aggregate only verified local TDX HK daily bars, with daily coverage.

    The source files provide reliable OHLC and turnover amounts after the
    ``tdx-cn-v2`` gate, but do not provide a complete point-in-time listed
    universe or historical market-cap denominator.  This routine therefore
    derives only turnover and close-to-close breadth and keeps market-cap
    fields null.  Results are cached by the Silver data revision rather than
    by a wall-clock TTL, so a completed local import automatically invalidates
    the aggregate.
    """

    root = Path(root)
    key = (str(root.resolve()), market_data_version(root))
    with _HK_OVERVIEW_LOCK:
        cached = _HK_OVERVIEW_CACHE.get(key)
        if cached is not None:
            return [dict(item) for item in cached]

        silver_root = root / "silver"
        if not silver_root.is_dir():
            return []
        # Reading ``silver/**/*.parquet`` makes DuckDB inspect every market
        # before it can apply a JSON-field filter.  Production writes use the
        # Hive path below, while the fallback retains compatibility with tiny
        # flat test/legacy stores.
        hk_daily_files = sorted(
            silver_root.glob("market=HK/asset_type=STOCK/period=1d/year=*/*.parquet")
        )
        parquet_source: str | list[str] = (
            [str(path) for path in hk_daily_files]
            if hk_daily_files
            else (silver_root / "**" / "*.parquet").as_posix()
        )
        connection = duckdb.connect(database=":memory:")
        try:
            rows = connection.execute(
                """WITH raw AS (
                    SELECT
                        coalesce(nullif(json_extract_string(bar_json, '$.canonical_instrument_id'), ''), instrument_id) AS canonical_id,
                        instrument_id AS physical_id,
                        substr(CAST(bar_open_time AS VARCHAR), 1, 10) AS trading_day,
                        try_cast(json_extract_string(bar_json, '$.close') AS DOUBLE) AS close,
                        try_cast(json_extract_string(bar_json, '$.amount') AS DOUBLE) AS amount,
                        json_extract_string(bar_json, '$.fetched_at') AS fetched_at
                    FROM read_parquet(?, union_by_name=true, hive_partitioning=true)
                    WHERE market = 'HK' AND asset_type = 'STOCK' AND period = '1d'
                      AND instrument_id LIKE 'HK.HKEX.STOCK.%.TDX_LOCAL'
                ), ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY canonical_id, trading_day
                        ORDER BY fetched_at DESC NULLS LAST, physical_id
                    ) AS source_rank
                    FROM raw
                ), daily AS (
                    SELECT canonical_id, trading_day, close, amount, fetched_at,
                        lag(close) OVER (PARTITION BY canonical_id ORDER BY trading_day) AS previous_close
                    FROM ranked
                    WHERE source_rank = 1
                )
                SELECT trading_day,
                    sum(CASE WHEN amount >= 0 THEN amount END) AS turnover,
                    count(*) FILTER (WHERE close IS NOT NULL) AS close_coverage,
                    count(*) FILTER (WHERE amount >= 0) AS turnover_coverage,
                    count(*) FILTER (WHERE close IS NOT NULL AND previous_close IS NOT NULL AND previous_close > 0) AS breadth_coverage,
                    count(*) FILTER (WHERE close > previous_close AND previous_close > 0) AS advances,
                    count(*) FILTER (WHERE close = previous_close AND previous_close > 0) AS unchanged,
                    count(*) FILTER (WHERE close < previous_close AND previous_close > 0) AS declines,
                    max(fetched_at) AS updated_at
                FROM daily
                GROUP BY trading_day
                ORDER BY trading_day""",
                [parquet_source],
            ).fetchall()
        except duckdb.Error:
            rows = []
        finally:
            connection.close()

        result = [
            {
                "tradingDay": str(trading_day),
                "totalMarketCapYi": None,
                "floatMarketCapYi": None,
                "turnoverYi": float(turnover) / 100_000_000 if turnover is not None else None,
                "advances": float(advances) if breadth_coverage else None,
                "unchanged": float(unchanged) if breadth_coverage else None,
                "declines": float(declines) if breadth_coverage else None,
                "limitUpCount": None,
                "limitDownCount": None,
                "coverage": int(close_coverage or 0),
                "turnoverCoverage": int(turnover_coverage or 0),
                "breadthCoverage": int(breadth_coverage or 0),
                "updatedAt": str(updated_at) if updated_at is not None else None,
            }
            for (
                trading_day,
                turnover,
                close_coverage,
                turnover_coverage,
                breadth_coverage,
                advances,
                unchanged,
                declines,
                updated_at,
            ) in rows
        ]
        _HK_OVERVIEW_CACHE.clear()
        _HK_OVERVIEW_CACHE[key] = result
        return [dict(item) for item in result]


def _cached_hk_tdx_overview_rows(root: Path) -> list[dict[str, Any]]:
    """Return an existing HK aggregate without triggering the on-demand scan."""

    key = (str(Path(root).resolve()), market_data_version(root))
    with _HK_OVERVIEW_LOCK:
        cached = _HK_OVERVIEW_CACHE.get(key)
        return [dict(item) for item in cached] if cached is not None else []


def _filter_equity_points(
    points: list[dict[str, Any]], start_day: str | None, end_day: str | None
) -> list[dict[str, Any]]:
    return [
        dict(point) for point in points
        if (start_day is None or str(point["tradingDay"]) >= start_day)
        and (end_day is None or str(point["tradingDay"]) <= end_day)
    ]


def _unavailable_equity_overview(
    *,
    market: str,
    segment: str,
    limitation: str,
    currency: str,
) -> dict[str, Any]:
    return {
        "available": False,
        "market": market,
        "segment": segment,
        "currency": currency,
        "units": {"marketCap": "亿元", "turnover": "亿元", "breadth": "家", "limitPool": "家"},
        "points": [],
        "limitations": [limitation],
    }


def _a_share_panel(
    panel_id: str,
    title: str,
    snapshot: dict[str, dict[str, Any]],
    metric_keys: tuple[str, ...],
    *,
    fallback_status: str,
    partial: bool = False,
) -> dict[str, Any]:
    values = [snapshot[key] for key in metric_keys if key in snapshot]
    return {
        "id": panel_id,
        "title": title,
        "status": "PARTIAL" if values and partial else "PASS" if values else fallback_status,
        "values": values,
        "limitations": (
            ["当前只具备全市场汇总，尚未按沪深主板、北交所、创业板、科创板分别验证。"]
            if panel_id == "cn-breadth" and values
            else ["现有权威池未提供一字涨停/一字跌停或炸板率，未作近似。"]
            if panel_id == "cn-limit-pool" and values
            else []
        ),
    }


def _hk_overview_panel(
    panel_id: str,
    title: str,
    points: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    """Expose the newest local-TDX HK aggregate without claiming full coverage."""

    latest = next(
        (
            point for point in reversed(points)
            if all(point.get(field) is not None for field in fields)
        ),
        None,
    )
    if latest is None:
        return _unavailable_panel(
            panel_id,
            title,
            "尚无足以计算该指标的本机通达信港股日线；不会把缺失值绘制为零。",
        )
    unit = "亿港元" if fields == ("turnoverYi",) else "家"
    labels = {
        "turnoverYi": "成交额",
        "advances": "上涨家数",
        "unchanged": "持平家数",
        "declines": "下跌家数",
    }
    return {
        "id": panel_id,
        "title": title,
        "status": "PARTIAL",
        "values": [
            {
                "metricKey": field,
                "tradingDate": latest["tradingDay"],
                "metricName": labels[field],
                "value": latest[field],
                "unit": unit,
                "coverage": latest.get("turnoverCoverage" if field == "turnoverYi" else "breadthCoverage"),
                "source": "通达信金融终端（本地）",
            }
            for field in fields
        ],
        "limitations": [
            "仅汇总通过 tdx-cn-v2 质量门的本地港股股票日线；覆盖数不是港股全市场上市数量。"
        ],
    }


def _northbound_panel(values: list[dict[str, Any]]) -> dict[str, Any]:
    required = {"北向_当日成交净买额", "北向_历史累计净买额", "北向_持股市值"}
    dates = {str(item["tradingDate"]) for item in values}
    if not required.issubset({str(item["metricKey"]) for item in values}) or len(dates) != 1:
        return _unavailable_panel(
            "cn-hsgt-flow",
            "北向资金",
            "本地北向记录未在同一交易日同时具备当日成交净买额、历史累计净买额和持股市值；"
            "不得以方向标签不一致、零值字段或跨日拼接替代缺失事实。",
        )
    return {
        "id": "cn-hsgt-flow",
        "title": "北向资金",
        "status": "PARTIAL",
        "values": values,
        "limitations": [
            "当前仅有本地已采集快照；已区分当日成交净买额、历史累计净买额和持股市值，"
            "但尚未通过连续历史、字段稳定性与全量口径验收。金额按来源记录的亿元单位解释。",
        ],
    }


def _unavailable_panel(panel_id: str, title: str, limitation: str) -> dict[str, Any]:
    return {"id": panel_id, "title": title, "status": "UNAVAILABLE", "limitations": [limitation]}


@router.get("/macro/catalog")
def macro_catalog(request: Request, country: str | None = None) -> dict[str, Any]:
    if country is not None and country not in {"CN", "US"}:
        raise HTTPException(status_code=422, detail="country must be CN or US")
    root = Path(request.app.state.data_root)
    availability = _macro_availability(root)
    ids = _CN_IDS if country in {None, "CN"} else _US_IDS
    if country is None:
        ids = (*_CN_IDS, *_US_IDS)
    return {
        "items": [
            {
                **_macro_metadata(series_id),
                "available": series_id in availability,
                "latestObservationPeriod": availability.get(series_id, {}).get("latestObservationPeriod"),
                "latestFetchedAt": availability.get(series_id, {}).get("latestFetchedAt"),
                "definitionVersion": "r4-v1",
            }
            for series_id in ids
        ],
    }


@router.get("/macro/series")
def macro_series(
    request: Request,
    series_id: str = Query(alias="seriesId"),
    start_period: str | None = Query(default=None, alias="startPeriod"),
    end_period: str | None = Query(default=None, alias="endPeriod"),
    view: str = "timeline",
) -> dict[str, Any]:
    all_ids = set(_CN_IDS) | set(_US_IDS)
    if series_id not in all_ids:
        raise HTTPException(status_code=422, detail="unknown seriesId")
    _validate_period(start_period, "startPeriod")
    _validate_period(end_period, "endPeriod")
    if start_period and end_period and start_period > end_period:
        raise HTTPException(status_code=422, detail="startPeriod must not be after endPeriod")
    if view not in {"timeline", "seasonal"}:
        raise HTTPException(status_code=422, detail="view must be timeline or seasonal")
    metadata = _macro_metadata(series_id)
    root = Path(request.app.state.data_root)
    rows = _macro_rows(root, series_id, start_period, end_period)
    if not rows:
        return {"available": False, "series": metadata, "view": view, "observations": [], "limitations": ["尚无本地 Gold 观测；不会以演示数据替代真实来源。"]}
    observations = [
        {
            "observationPeriod": item[0],
            "value": item[1],
            # The persisted metric timestamp records when this local collector
            # obtained the point.  It is not evidence of the authority's
            # publication time, so never expose it as a release date.
            "releasedAt": None,
            "fetchedAt": item[2],
            "source": metadata["source"],
            "definitionVersion": "r4-v1",
        }
        for item in rows
    ]
    return {
        "available": True,
        "series": metadata,
        "view": view,
        "observations": _seasonal(observations) if view == "seasonal" else observations,
        "limitations": [],
    }


def _macro_rows(root: Path, series_id: str, start_period: str | None, end_period: str | None) -> list[tuple[str, float, str]]:
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    clauses = ["instrument_id = ?"]
    params: list[Any] = [series_id]
    if start_period:
        clauses.append("trading_date >= ?")
        params.append(start_period)
    if end_period:
        clauses.append("trading_date <= ?")
        params.append(end_period)
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        return [
            (str(row[0]), float(row[1]), str(row[2]))
            for row in connection.execute(
                f"SELECT trading_date, value, timestamp FROM gold_metrics WHERE {' AND '.join(clauses)} ORDER BY trading_date, timestamp",
                params,
            ).fetchall()
            if row[1] is not None
        ]
    finally:
        connection.close()


def _seasonal(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    years: dict[str, dict[int, float]] = defaultdict(dict)
    for observation in observations:
        period = str(observation["observationPeriod"])
        if len(period) < 7:
            continue
        years[period[:4]][int(period[5:7])] = float(observation["value"])
    return [
        {"year": year, "months": [values.get(month) for month in range(1, 13)]}
        for year, values in sorted(years.items())
    ]


def _validate_period(value: str | None, field: str) -> None:
    if value is not None and not _PERIOD_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM or YYYY-MM-DD")


def _validate_day(value: str | None, field: str) -> None:
    if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD")


def _normalized_trading_day(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if re.fullmatch(r"\d{8}", value) else value


__all__ = ("router",)
