"""Read-only R4 data-section metadata and macro-series endpoints."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
import json
import math
import os
import re
from pathlib import Path
import threading
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request

from market_monitor.a_share_calendar import (has_verified_a_share_calendar, is_known_a_share_closed_day,
                                             is_verified_a_share_trading_day)
from market_monitor.market_data_version import market_data_version
from market_monitor.macro_series import macro_series_index
from market_monitor.storage import connect_catalog


router = APIRouter(prefix="/api/data", tags=["data-sections"])
_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])(?:-\d{2})?$")


@router.get("/ths-indexflash/status")
def ths_indexflash_status(request: Request) -> dict[str, str | None]:
    """Only expose bounded probe status, never unverified payload values."""
    from market_monitor.ths_indexflash import read_indexflash_status

    return read_indexflash_status(Path(request.app.state.data_root))


@router.get("/ths-app-etf/observation")
def ths_app_etf_observation(request: Request) -> dict:
    """Read local observed values only; never issue upstream requests."""
    from market_monitor.ths_app_etf_view import read_ths_app_etf

    return read_ths_app_etf(Path(request.app.state.data_root))


@router.get("/ths-app-etf/status")
def ths_app_etf_status(request: Request) -> dict:
    """Read schedule status only, without exposing the session or fetching."""
    from market_monitor.ths_app_etf_auto import read_status

    return read_status(Path(request.app.state.data_root),
                       auto_enabled=bool(getattr(request.app.state, "ths_etf_auto_enabled", False)))


@router.get("/kpl-market/status")
def kpl_market_status(request: Request) -> dict[str, str | None]:
    """Expose metadata only; no captured traffic or unverified values."""
    from market_monitor.kpl_market_probe import read_kpl_status

    status = read_kpl_status(Path(request.app.state.data_root))
    return _kpl_status_projection(status, datetime.now(ZoneInfo("Asia/Shanghai")))


@router.get("/kpl-market/history")
def kpl_market_history(request: Request) -> dict:
    """Return only KPL public market fields from intact local evidence."""
    from market_monitor.kpl_history_view import read_kpl_history

    return read_kpl_history(Path(request.app.state.data_root))


def _kpl_status_projection(status: dict[str, str | None], now: datetime) -> dict[str, str | None]:
    """Keep calendar coverage and check age separate from the source result."""
    checked = status.get("checkedAt")
    age = "UNKNOWN"
    local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    clock = local_now.time().replace(tzinfo=None)
    active = (is_verified_a_share_trading_day(local_now.date()) and
              (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock < time(15)))
    if checked:
        try:
            stamp = datetime.fromisoformat(checked)
            if stamp.tzinfo is not None:
                elapsed = now - stamp
                if timedelta(0) <= elapsed:
                    age = "OLD" if elapsed > (timedelta(minutes=15) if active else timedelta(days=7)) else "RECENT"
        except (ValueError, TypeError, OverflowError):
            pass
    calendar = ("VERIFIED" if has_verified_a_share_calendar(local_now.year)
                else "UNKNOWN")
    return {"status": status.get("status"), "checkedAt": checked,
            "observedDate": status.get("observedDate"), "calendarStatus": calendar, "checkAge": age}

# Every requested R4 macro series is registered in ``macro_series``.  The
# display catalogue derives availability only from persisted Gold rows.
_CN_IDS = (
    "M2_MONEY_SUPPLY", "M1_MONEY_SUPPLY", "M0_MONEY_SUPPLY", "CN_IMPORT_USD_YOY",
    "CN_EXPORT_USD_YOY", "CN_TRADE_BALANCE_USD", "CN_RETAIL_SALES_YOY", "CN_RETAIL_SALES_MOM",
    "PPI", "PMI_MANUFACTURING", "CPI", "CN_FOREX_RESERVES", "CN_ELECTRICITY_CONSUMPTION",
    "DR007", "CN10Y_YIELD",
)
_US_IDS = ("US_CORE_PCE_QOQ_SAAR_FINAL", "US_IMPORTS_SA", "US_NONFARM_PAYROLLS_SA", "US_NONFARM_BLS_LATEST", "FED_FUNDS_RATE")
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
    "US_NONFARM_BLS_LATEST": "就业",
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
_HK_DISK_CACHE_SCHEMA = "market-listener/hk-tdx-daily-overview/v1"
_LEGACY_OPEN_RETURN_COVERAGE_RE = re.compile(r"昨日涨停池(\d+)只中(\d+)只有当日K线")
_OPEN_RETURN_COVERAGE_RE = re.compile(r"pool_count=(\d+); priced_count=(\d+)")
_A_SHARE_CAP_HISTORY_PREFIX = "source=eastmoney-RPT_VALUEMARKET+szse-official-daily;"
_SHA256_EVIDENCE_RE = re.compile(r"(?:^|; )(?P<label>em|szse)_sha256=[0-9a-f]{64}(?=;|$)")
_TDX_BREADTH_COVERAGE_RE = re.compile(r"(?:^|; )coverage=(\d+); paired=(\d+)(?=;|$)")
_OFFICIAL_PREFIX = "OFFICIAL_EXCHANGE_DAILY:"
_OFFICIAL_METHOD = "source=official-exchanges-v1;"
_OFFICIAL_INSTRUMENT_RE = re.compile(r"^CN\.(ALL|SSE|SZSE|BSE|SSE_SZSE)\.MARKET\.([A-Z_]+)$")
_OFFICIAL_METRICS = {
    "TOTAL_MARKET_CAP_YI": ("capYi", "亿元"),
    "FLOAT_MARKET_CAP_YI": ("floatCapYi", "亿元"),
    "TOTAL_AMOUNT_YI": ("turnoverYi", "亿元"),
    "COMPANY_COUNT": ("companyCount", "家"),
    "SECURITY_COUNT": ("securityCount", "只"),
    "TOTAL_SHARES_YI": ("sharesYi", "亿股"),
    "FLOAT_SHARES_YI": ("floatSharesYi", "亿股"),
    "VOLUME_YI": ("volumeYi", "亿股/亿份"),
    "TRADE_COUNT": ("tradeCount", "笔"),
    "CUSTODY_MARKET_VALUE_YI": ("custodyValueYi", "亿元"),
    "CUSTODY_FACE_VALUE_YI": ("custodyFaceYi", "亿元"),
}
_OFFICIAL_MONTHLY_METRICS = {
    "ISSUANCE_YI": ("issuanceYi", "亿元"),
    "ISSUANCE_YTD_YI": ("issuanceYtdYi", "亿元"),
}
_OFFICIAL_SOURCE_LABEL = "沪深京证券交易所官网日报"
_OFFICIAL_DIGEST_RE = re.compile(r"(?:^|[,=])[a-z0-9_]+:[0-9a-f]{64}(?=,|$)")
_OFFICIAL_SCOPE = {
    ("ALL", "STOCK_A"): "沪深京A股",
    ("ALL", "STOCK_LISTED"): "三所挂牌股票（沪深含B股）",
    ("SSE_SZSE", "FUND"): "沪深上市基金（北交所未覆盖）",
    ("SSE", "FUND_MONEY"): "沪市交易型货币基金（2022年9月5日起包含在ETF内）",
    ("SSE", "FUND_REPO"): "沪市基金回购（2019年7月22日起不计入基金现货合计，按官网显示精度）",
    ("ALL", "BOND_CASH"): "三所债券及ABS现券成交额",
    ("SSE_SZSE", "BOND_REPO"): "沪深债券回购成交额",
}
_OFFICIAL_MONTHLY_SCOPE = {
    "BOND_TREASURY": "北交所国债月度发行",
    "BOND_LOCAL_GOV": "北交所地方政府债月度发行",
    "BOND_CORP": "北交所公司债月度发行",
    "BOND_PUBLIC_CORP": "北交所公开发行公司债月度发行",
    "BOND_PRIVATE_CORP": "北交所非公开发行公司债月度发行",
}
_OFFICIAL_COVERAGE_FIELDS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "STOCK_A": {venue: {"STOCK_A": ("TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI", "TOTAL_AMOUNT_YI")}
                for venue in ("SSE", "SZSE", "BSE")},
    "STOCK_LISTED": {
        "SSE": {"STOCK_ALL": ("COMPANY_COUNT", "TOTAL_SHARES_YI", "FLOAT_SHARES_YI", "VOLUME_YI")},
        "SZSE": {"STOCK_ALL": ("COMPANY_COUNT", "TOTAL_SHARES_YI", "FLOAT_SHARES_YI", "VOLUME_YI")},
        "BSE": {"STOCK_A": ("COMPANY_COUNT", "TOTAL_SHARES_YI", "FLOAT_SHARES_YI", "VOLUME_YI")},
    },
    "FUND": {
        "SSE": {"FUND": ("SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI",
                         "TOTAL_AMOUNT_YI", "VOLUME_YI")},
        "SZSE": {"FUND": ("SECURITY_COUNT", "TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI",
                          "TOTAL_AMOUNT_YI", "VOLUME_YI")},
    },
    "BOND_CASH": {
        "SSE": {"BOND": ("TOTAL_AMOUNT_YI",)},
        "SZSE": {"BOND_CASH": ("TOTAL_AMOUNT_YI",), "BOND_ABS": ("TOTAL_AMOUNT_YI",)},
        "BSE": {"BOND_CASH": ("TOTAL_AMOUNT_YI",)},
    },
    "BOND_REPO": {
        "SSE": {"BOND_REPO": ("TOTAL_AMOUNT_YI",)},
        "SZSE": {"BOND_REPO": ("TOTAL_AMOUNT_YI",)},
    },
}
_OFFICIAL_FAILED_SOURCE_ASSETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sse_market": ("SSE", ("STOCK_A", "STOCK_LISTED", "FUND", "BOND_CASH", "BOND_REPO")),
    "sse_stock": ("SSE", ("STOCK_A", "STOCK_LISTED")),
    "sse_volume": ("SSE", ("STOCK_A", "STOCK_LISTED")),
    "sse_fund_daily": ("SSE", ("FUND",)),
    "szse_categories": ("SZSE", ("STOCK_A", "STOCK_LISTED", "FUND", "BOND_CASH", "BOND_REPO")),
    "szse_stock": ("SZSE", ("STOCK_A", "STOCK_LISTED")),
    "szse_main": ("SZSE", ("STOCK_A", "STOCK_LISTED")),
    "szse_chinext": ("SZSE", ("STOCK_A", "STOCK_LISTED")),
    "szse_fund_daily": ("SZSE", ("FUND",)),
    "bse_stock": ("BSE", ("STOCK_A", "STOCK_LISTED")),
    "bse_bond": ("BSE", ("BOND_CASH",)),
    # sse_bond_custody_daily has no headline coverage asset; it is surfaced by
    # the independent source-update report and its own observed series.
}


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


def _has_gold_metrics(connection: duckdb.DuckDBPyConnection) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='gold_metrics' LIMIT 1"
    ).fetchone())


def _read_catalog(root: Path) -> duckdb.DuckDBPyConnection:
    """Bound transient writer locks, without treating unread data as absent."""

    try:
        return connect_catalog(root)
    except (duckdb.IOException, duckdb.ConnectionException) as error:
        message = str(error).lower()
        if any(marker in message for marker in (
            "lock on file", "could not set lock", "another process",
            "used by another process", "另一个程序正在使用", "被另一进程使用",
        )):
            raise HTTPException(
                status_code=503,
                detail={"code": "CATALOG_BUSY", "message": "本机数据正在入库，目录暂被占用；请稍后重试。"},
                headers={"Retry-After": "2", "X-MarketListener-Error-Code": "CATALOG_BUSY"},
            ) from None
        raise


def _macro_availability(root: Path) -> dict[str, dict[str, str]]:
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return {}
    all_ids = [*_CN_IDS, *_US_IDS]
    placeholders = ", ".join("?" for _ in all_ids)
    # The same runtime also has MarketStore writer connections. DuckDB requires
    # a consistent connection mode in one process; the SQL here is SELECT-only.
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return {}
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
    # The HK aggregate is deliberately computed on first HK-tab access.  Do
    # not make the default A-share page wait for a multi-year HK daily scan.
    hk_points = _screen_hk_tdx_coverage(_cached_hk_tdx_overview_rows(root))
    hk_cap = _hk_market_cap_rows(root)
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
                        "cn-float-market-cap",
                        "A股流通市值",
                        a_share,
                        ("FLOAT_MARKET_CAP_YI",),
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
                        "A股涨跌停家数",
                        a_share,
                        ("涨停_COUNT", "跌停_COUNT"),
                        fallback_status="UNAVAILABLE",
                        partial=True,
                    ),
                    _a_share_panel("cn-broken-board", "A股炸板数", a_share,
                                   ("炸板_COUNT",), fallback_status="UNAVAILABLE", partial=True),
                    _a_share_sealed_rate_panel(a_share),
                    _a_share_panel(
                        "cn-limit-height",
                        "A股涨跌停高度",
                        a_share,
                        ("涨停_MAX_LIANBAN", "跌停_MAX_LIANBAN"),
                        fallback_status="UNAVAILABLE",
                        partial=True,
                    ),
                    _a_share_open_return_panel(a_share),
                ],
            },
            {
                "id": "hk-equities",
                "title": "港股",
                "panels": [
                    _hk_market_cap_panel(hk_cap),
                    _hk_overview_panel("hk-turnover", "港股股票成交额（本机样本）", hk_points,
                                       ("turnoverYi",)),
                    _hk_overview_panel(
                        "hk-breadth",
                        "港股涨跌家数",
                        hk_points,
                        ("advances", "unchanged", "declines"),
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
        root = Path(request.app.state.data_root)
        cap_points = _hk_market_cap_rows(root)
        points = _filter_equity_points(_merge_hk_cap_points(
            _screen_hk_tdx_coverage(_hk_tdx_overview_rows(root)), cap_points),
                                       start_day, end_day)
        return {
            "available": bool(points),
            "market": "HK",
            "segment": "ALL",
            "currency": "HKD",
            "units": {"marketCap": "亿港元", "turnover": "亿港元", "breadth": "家", "limitPool": "家"},
            "points": points,
            "source": ("港交所主板及GEM市价总值档案；" if cap_points else "")
            + "通达信金融终端（本地）港股股票日线样本",
            "limitations": [
                "成交额与涨跌家数只统计本机已导入、通过 tdx-cn-v2 质量门的港股股票日线；"
                "不含全部上市证券，不能视为港交所全市场成交额。样本数低于相邻交易日参考覆盖的85%时成交额留空。",
                ("总市值来自已保存的港交所主板及GEM按日档案；官网自动网页采集已暂停，"
                 "较晚观测需取得授权来源，缺日保持空值。"
                 if cap_points else "尚未导入港交所市价总值档案；总市值保持空值。")
                + "不以当前F10回填；流通市值、涨跌停和状态名单仍不可用。",
            ] if points else ["尚无港交所主板及GEM市值档案或通过质量门的港股日线。"],
        }
    if normalized_segment != "ALL":
        return _unavailable_equity_overview(
            market="CN",
            segment=normalized_segment,
            limitation="本地 A 股 Gold 目前只验证全市场汇总，尚未按沪深主板、北交所、创业板、科创板拆分。",
            currency="CNY",
        )
    points = _a_share_overview_rows(Path(request.app.state.data_root), start_day, end_day)
    has_official = any("officialCoverage" in point for point in points)
    return {
        "available": bool(points),
        "market": "CN",
        "segment": "ALL",
        "currency": "CNY",
        "units": {"marketCap": "亿元", "turnover": "亿元", "breadth": "家", "limitPool": "家",
                  "brokenBoard": "家", "sealedRate": "%", "limitHeight": "板", "limitUpOpenReturn": "%"},
        "points": points,
        "limitations": [
            ("A股总市值、流通市值及成交额只采用同日沪深京三所完整官网日报；单所缺报时当日保持空值，"
             "officialCoverage 逐日列出缺失交易所。上市公司家数、总股本及成交股数的三所合计另属沪深含B股的挂牌股票范围。"
             "涨跌家数仍来自腾讯快照或通过通达信 tdx-cn-v2 质量门的本机日线。"
             if has_official else
             "当前仅为沪深京全市场汇总；成交额和涨跌家数的历史缺口由通过 tdx-cn-v2 质量门的本机日线补算，覆盖口径与腾讯当日快照略有差异。A股总市值按东方财富沪市主板、科创板、北证A股与深交所主板A股、创业板A股同日数据计算，整段采用同一口径；来源缺日保持空值。")
            + "炸板数来自东方财富盘中触板且收盘未封板池，缺日不补零；接盘收益来自昨日涨停池和同日开收盘价，口径不保证与同花顺完全相同。",
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


def _official_identity(metric_id: object, method: object, value: object) -> tuple[str, str, str] | None:
    """Accept only source-isolated, finite Gold rows from the official pipeline."""

    parts = str(metric_id).split(":")
    if len(parts) != 5 or parts[0] != "OFFICIAL_EXCHANGE_DAILY" or parts[3] != "1d":
        return None
    instrument = _OFFICIAL_INSTRUMENT_RE.fullmatch(parts[1])
    if not instrument or parts[4] not in _OFFICIAL_METRICS:
        return None
    venue, category = instrument.groups()
    field = _OFFICIAL_METRICS[parts[4]][0]
    method_text = str(method)
    if (not method_text.startswith(_OFFICIAL_METHOD)
            or f"venue={venue}; category={category}; field={field}; raw_sha256=" not in method_text
            or not _OFFICIAL_DIGEST_RE.search(method_text)):
        return None
    if (venue, category) == ("ALL", "STOCK_A") and any(
        re.search(rf"(?:^|[,=]){source}:[0-9a-f]{{64}}(?=,|$)", method_text) is None
        for source in ("sse_market", "szse_categories", "bse_stock")
    ):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return venue, category, parts[4]


def _official_monthly_identity(metric_id: object, method: object, value: object) -> tuple[str, str, str] | None:
    parts = str(metric_id).split(":")
    if (len(parts) != 5 or parts[0] != "OFFICIAL_EXCHANGE_MONTHLY" or parts[3] != "1mo"
            or not re.fullmatch(r"\d{4}-\d{2}", parts[2])):
        return None
    instrument = _OFFICIAL_INSTRUMENT_RE.fullmatch(parts[1])
    if not instrument or instrument.group(1) != "BSE" or parts[4] not in _OFFICIAL_MONTHLY_METRICS:
        return None
    venue, category = instrument.groups()
    field = _OFFICIAL_MONTHLY_METRICS[parts[4]][0]
    method_text = str(method)
    if (not method_text.startswith(_OFFICIAL_METHOD)
            or f"venue={venue}; category={category}; field={field}; raw_sha256=" not in method_text
            or not _OFFICIAL_DIGEST_RE.search(method_text)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return (venue, category, parts[4]) if math.isfinite(number) and number >= 0 else None


def _official_records(root: Path, start_day: str | None = None,
                      end_day: str | None = None) -> list[tuple[str, str, float, str, str]]:
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return []
        clauses = ["metric_id LIKE 'OFFICIAL_EXCHANGE_DAILY:%'"]
        params: list[str] = []
        if start_day:
            clauses.append("trading_date >= ?")
            params.append(start_day)
        if end_day:
            clauses.append("trading_date <= ?")
            params.append(end_day)
        rows = connection.execute(f"""SELECT metric_id, trading_date, value, calculation_method, timestamp
            FROM gold_metrics WHERE {' AND '.join(clauses)} ORDER BY trading_date, metric_id""", params).fetchall()
    finally:
        connection.close()
    return [(str(metric_id), str(day), float(value), str(method), str(stamp))
            for metric_id, day, value, method, stamp in rows
            if _official_identity(metric_id, method, value) is not None]


def _official_monthly_records(root: Path, start_month: str,
                              end_month: str) -> list[tuple[str, str, float, str, str]]:
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return []
        rows = connection.execute("""SELECT metric_id, trading_date, value, calculation_method, timestamp
            FROM gold_metrics WHERE metric_id LIKE 'OFFICIAL_EXCHANGE_MONTHLY:%'
            AND trading_date BETWEEN ? AND ? ORDER BY trading_date, metric_id""",
            [start_month, end_month],
        ).fetchall()
    finally:
        connection.close()
    return [(str(metric_id), str(month), float(value), str(method), str(stamp))
            for metric_id, month, value, method, stamp in rows
            if _official_monthly_identity(metric_id, method, value) is not None]


def _official_coverage(
    observations: dict[tuple[str, str, str, str], float], day: str, asset: str,
) -> dict[str, Any]:
    requirements = _OFFICIAL_COVERAGE_FIELDS[asset]
    present = [venue for venue, categories in requirements.items()
               if all((day, venue, category, metric) in observations
                      for category, metrics in categories.items() for metric in metrics)]
    required = list(requirements)
    missing = [venue for venue in required if venue not in present]
    return {"day": day, "asset": asset, "requiredVenues": required,
            "presentVenues": present, "missingVenues": missing,
            "status": "COMPLETE" if not missing else "PARTIAL" if present else "MISSING"}


def _official_failed_venues(root: Path) -> dict[str, dict[str, list[str]]]:
    """Map explicit failed source tokens to the assets they can supply."""

    path = root / "state" / "official_exchange_daily_backlog.json"
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("schema") != "official-exchange-backlog-v1":
            return {}
        failures = ledger.get("failures")
        if not isinstance(failures, dict):
            return {}
    except (OSError, ValueError, TypeError):
        return {}
    result: dict[str, dict[str, list[str]]] = {}
    for day, reason in failures.items():
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        by_asset: dict[str, list[str]] = {}
        for failure in str(reason).split(","):
            source = failure.split(":", 1)[0].strip().lower()
            mapping = _OFFICIAL_FAILED_SOURCE_ASSETS.get(source)
            if mapping is None:
                continue
            venue, assets = mapping
            for asset in assets:
                venues = by_asset.setdefault(asset, [])
                if venue not in venues:
                    venues.append(venue)
        if by_asset:
            result[day] = by_asset
    return result


@router.get("/exchanges/overview")
def exchange_overview(
    request: Request,
    start_day: str | None = Query(default=None, alias="startDay"),
    end_day: str | None = Query(default=None, alias="endDay"),
) -> dict[str, Any]:
    """Batch the bounded, dated source-isolated exchange statistics for charts."""

    _validate_day(start_day, "startDay")
    _validate_day(end_day, "endDay")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date()
    try:
        end = date.fromisoformat(end_day) if end_day else _exchange_default_end(now)
        start = date.fromisoformat(start_day) if start_day else end - timedelta(days=119)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid trading day") from exc
    if end < start or (end - start).days > 365:
        raise HTTPException(status_code=422, detail="range must contain 1..366 calendar days")
    root = Path(request.app.state.data_root)
    rows = _official_records(root, start.isoformat(), end.isoformat())
    observations: dict[tuple[str, str, str, str], float] = {}
    times: dict[tuple[str, str, str, str], str] = {}
    for metric_id, day, value, method, timestamp in rows:
        identity = _official_identity(metric_id, method, value)
        if identity is None or _is_a_share_closed_day(day):
            continue
        venue, category, metric = identity
        key = (day, venue, category, metric)
        if key not in times or timestamp > times[key]:
            observations[key] = value
            times[key] = timestamp
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for (day, venue, category, metric), value in sorted(observations.items()):
        grouped[(venue, category, metric)].append({"day": day, "value": value})
    series = [{"venue": venue, "category": category, "metric": metric,
               "unit": ("亿份" if metric == "VOLUME_YI" and category.startswith("FUND") else
                        _OFFICIAL_METRICS[metric][1]),
               "frequency": "DAILY", "scope": _OFFICIAL_SCOPE.get((venue, category),
                                                               f"{venue} {category}"),
               "points": points}
              for (venue, category, metric), points in sorted(grouped.items())]
    monthly_grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for metric_id, month, value, method, _ in _official_monthly_records(
        Path(request.app.state.data_root), start.isoformat()[:7], end.isoformat()[:7],
    ):
        identity = _official_monthly_identity(metric_id, method, value)
        if identity:
            monthly_grouped[identity].append({"day": month, "value": value})
    series.extend({"venue": venue, "category": category, "metric": metric,
                   "unit": _OFFICIAL_MONTHLY_METRICS[metric][1], "frequency": "MONTHLY",
                   "scope": _OFFICIAL_MONTHLY_SCOPE.get(category, f"北交所 {category} 月度发行"),
                   "points": sorted(points, key=lambda item: item["day"])}
                  for (venue, category, metric), points in sorted(monthly_grouped.items()))
    coverage = []
    failed_venues = _official_failed_venues(root)
    current = start
    while current <= end:
        if is_verified_a_share_trading_day(current):
            day = current.isoformat()
            for asset in _OFFICIAL_COVERAGE_FIELDS:
                item = _official_coverage(observations, day, asset)
                if current > today or (current == today and now.time() < time(19)):
                    item.update(status="NOT_DUE", missingVenues=[], failedVenues=[])
                else:
                    item["failedVenues"] = [venue for venue in failed_venues.get(day, {}).get(asset, [])
                                            if venue in item["missingVenues"]]
                coverage.append(item)
        current += timedelta(days=1)
    return {"available": bool(series), "periods": {"startDay": start.isoformat(),
                                                  "endDay": end.isoformat()},
            "series": series, "coverage": coverage,
            "limitations": ["A股与三所挂牌股票是不同范围；后者在沪深两所含B股。",
                            "沪深基金合计仅覆盖上交所和深交所；北交所基金日值未核验。",
                            "债券及ABS现券成交额不含债券回购；债券托管面值与市价总值不合计。"]}


def _exchange_default_end(now: datetime) -> date:
    """Do not label a still-open domestic trading day as an exchange failure."""

    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.date() if local.time() >= time(19) else local.date() - timedelta(days=1)


def _a_share_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    """Read only persisted A-share summary metrics; never derive from samples."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return {}
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return {}
        rows = connection.execute(
            """SELECT metric_id, trading_date, metric_name, value, definition, calculation_method, timestamp
            FROM gold_metrics
            WHERE metric_id LIKE 'A_SHARE_BREADTH:%'
               OR metric_id LIKE 'A_SHARE_MARKET_CAP_HISTORY:%'
               OR (metric_id LIKE 'OFFICIAL_EXCHANGE_DAILY:%' AND instrument_id IN
                   ('CN.ALL.MARKET.STOCK_A', 'CN.SSE.MARKET.STOCK_A',
                    'CN.SZSE.MARKET.STOCK_A', 'CN.BSE.MARKET.STOCK_A'))
               OR metric_id LIKE 'CN_ZT_POOL:CN.A_SHARE.%'
               OR metric_id LIKE 'CN_ZT_POOL_OPEN_RETURN_V2:CN.A_SHARE.%'
            ORDER BY trading_date DESC, timestamp DESC"""
        ).fetchall()
    finally:
        connection.close()
    official_available = any(_official_identity(metric_id, method, value) is not None
                             for metric_id, _, _, value, _, method, _ in rows)
    history_cap_available = any(_trusted_a_share_market_cap_history(metric_id, method, value)
                                for metric_id, _, _, value, _, method, _ in rows)
    result: dict[str, dict[str, Any]] = {}
    for metric_id, trading_date, metric_name, value, definition, calculation_method, timestamp in rows:
        if _is_a_share_closed_day(str(trading_date)):
            continue
        key = str(metric_id).rsplit(":", 1)[-1]
        official = _official_identity(metric_id, calculation_method, value)
        if str(metric_id).startswith(_OFFICIAL_PREFIX):
            if official is None or official[:2] != ("ALL", "STOCK_A"):
                continue
        elif str(metric_id).startswith("A_SHARE_MARKET_CAP_HISTORY:"):
            if official_available:
                continue
            if not _trusted_a_share_market_cap_history(metric_id, calculation_method, value):
                continue
        elif key in {"TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"} and official_available:
            continue
        elif key == "TOTAL_MARKET_CAP_YI" and history_cap_available:
            continue
        if str(metric_id).startswith("A_SHARE_BREADTH:") and not _trusted_a_share_snapshot_time(
            str(trading_date), str(timestamp), str(calculation_method)
        ):
            continue
        if key in {"涨停_COUNT", "跌停_COUNT", "炸板_COUNT"} and float(value) <= 0:
            # Legacy empty/error pools were persisted as zero; current collector
            # only publishes a positive count from a non-empty dated pool.
            continue
        if key == "YESTERDAY_LIMIT_UP_OPEN_RETURN" and not _trusted_open_return_method(str(calculation_method)):
            continue
        if key in {"涨停_MAX_LIANBAN", "跌停_MAX_LIANBAN"} and (
            not math.isfinite(float(value)) or float(value) < 1
        ):
            # Older collection runs could turn an absent streak column into 0.
            continue
        if key in result:
            continue
        result[key] = {
            "metricKey": key,
            "tradingDate": str(trading_date),
            "metricName": str(metric_name),
            "value": float(value),
            "unit": "板" if key in {"涨停_MAX_LIANBAN", "跌停_MAX_LIANBAN"} else "家" if key == "炸板_COUNT" else None,
            "source": (_OFFICIAL_SOURCE_LABEL if official is not None
                       else "东方财富估值分析＋深交所市场总貌"
                       if str(metric_id).startswith("A_SHARE_MARKET_CAP_HISTORY:")
                       else "通达信本机质检日线"
                       if str(calculation_method).startswith("source=tdx-cn-v2; historical_silver_recovery;")
                       else "腾讯行情快照"
                       if str(metric_id).startswith("A_SHARE_BREADTH:")
                       else "东方财富炸板池" if key == "炸板_COUNT" else None),
            "definition": str(definition),
            "calculationMethod": str(calculation_method),
            "timestamp": str(timestamp),
        }
    return result


def _trusted_open_return_method(method: str) -> bool:
    """Exclude legacy close-to-close fallbacks written under the open-return key."""

    coverage = _open_return_coverage(method)
    if coverage is None or "回退" in method:
        return False
    pool_count = coverage["poolCount"]
    priced_count = coverage["pricedCount"]
    if pool_count <= 0 or priced_count > pool_count or priced_count < max(3, pool_count // 2):
        return False
    if method.startswith("source=eastmoney-previous-pool+tencent-dated-daily;"):
        return (priced_count >= max(3, math.ceil(pool_count * .8))
                and "formula=(close-open)/open*100" in method
                and "same_day_close_checked=true" in method)
    if method.startswith("source=pytdx; metric=open_to_close_mean;"):
        return "formula=(close-open)/open*100" in method and _OPEN_RETURN_COVERAGE_RE.search(method) is not None
    return (
        method.startswith("pytdx 昨日涨停池")
        and "按(收盘-开盘)/开盘取均值" in method
        and _LEGACY_OPEN_RETURN_COVERAGE_RE.search(method) is not None
    )


def _open_return_coverage(method: str) -> dict[str, int] | None:
    match = _OPEN_RETURN_COVERAGE_RE.search(method) or _LEGACY_OPEN_RETURN_COVERAGE_RE.search(method)
    if match is None:
        return None
    return {"poolCount": int(match.group(1)), "pricedCount": int(match.group(2))}


def _northbound_snapshot(root: Path) -> list[dict[str, Any]]:
    """Return persisted northbound fields without blending them into margin data."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return []
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


def _empty_a_share_point(day: str, timestamp: str) -> dict[str, Any]:
    return {
        "tradingDay": day,
        "totalMarketCapYi": None,
        "floatMarketCapYi": None,
        "turnoverYi": None,
        "advances": None,
        "unchanged": None,
        "declines": None,
        "limitUpCount": None,
        "limitDownCount": None,
        "failedLimitUpCount": None,
        "sealedLimitUpRatePct": None,
        "limitUpHeight": None,
        "limitDownHeight": None,
        "yesterdayLimitUpOpenReturnPct": None,
        "updatedAt": timestamp,
    }


def _a_share_overview_rows(
    root: Path,
    start_day: str | None,
    end_day: str | None,
) -> list[dict[str, Any]]:
    """Read the historical aggregate without interpreting unavailable keys as zero."""

    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    clauses = [
        "(metric_id LIKE 'A_SHARE_BREADTH:%' "
        "OR metric_id LIKE 'A_SHARE_MARKET_CAP_HISTORY:%' "
        "OR (metric_id LIKE 'OFFICIAL_EXCHANGE_DAILY:%' AND instrument_id IN "
        "('CN.ALL.MARKET.STOCK_A', 'CN.SSE.MARKET.STOCK_A', "
        "'CN.SZSE.MARKET.STOCK_A', 'CN.BSE.MARKET.STOCK_A')) "
        "OR metric_id LIKE 'CN_ZT_POOL:CN.A_SHARE.%' "
        "OR metric_id LIKE 'CN_ZT_POOL_OPEN_RETURN_V2:CN.A_SHARE.%')"
    ]
    params: list[Any] = []
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return []
        rows = connection.execute(
            f"""SELECT metric_id, trading_date, value, calculation_method, timestamp FROM gold_metrics
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
        "炸板_COUNT": "failedLimitUpCount",
        "涨停_MAX_LIANBAN": "limitUpHeight",
        "跌停_MAX_LIANBAN": "limitDownHeight",
        "LIMIT_UP": "limitUpCount",
        "LIMIT_DOWN": "limitDownCount",
        "YESTERDAY_LIMIT_UP_OPEN_RETURN": "yesterdayLimitUpOpenReturnPct",
    }
    official_available = any(_official_identity(metric_id, method, value) is not None
                             for metric_id, _, value, method, _ in rows)
    history_cap_available = any(_trusted_a_share_market_cap_history(metric_id, method, value)
                                for metric_id, _, value, method, _ in rows)
    by_day: dict[str, dict[str, Any]] = {}
    official_observations: dict[tuple[str, str, str, str], float] = {}
    for metric_id, trading_day, value, calculation_method, timestamp in rows:
        if _is_a_share_closed_day(str(trading_day)):
            continue
        key = str(metric_id).rsplit(":", 1)[-1]
        official = _official_identity(metric_id, calculation_method, value)
        if str(metric_id).startswith(_OFFICIAL_PREFIX):
            if official is None or official[1] != "STOCK_A":
                continue
            day = _normalized_trading_day(str(trading_day))
            venue, category, _ = official
            official_observations[(day, venue, category, key)] = float(value)
            by_day.setdefault(day, _empty_a_share_point(day, str(timestamp)))
            if venue != "ALL":
                continue
        elif str(metric_id).startswith("A_SHARE_MARKET_CAP_HISTORY:"):
            if official_available:
                continue
            if not _trusted_a_share_market_cap_history(metric_id, calculation_method, value):
                continue
        elif key in {"TOTAL_MARKET_CAP_YI", "FLOAT_MARKET_CAP_YI", "TOTAL_AMOUNT_YI"} and official_available:
            continue
        elif key == "TOTAL_MARKET_CAP_YI" and history_cap_available:
            continue
        if str(metric_id).startswith("A_SHARE_BREADTH:") and not _trusted_a_share_snapshot_time(
            str(trading_day), str(timestamp), str(calculation_method)
        ):
            continue
        if key in {"涨停_COUNT", "跌停_COUNT", "炸板_COUNT"} and (value is None or float(value) <= 0):
            continue
        if key == "YESTERDAY_LIMIT_UP_OPEN_RETURN" and not _trusted_open_return_method(str(calculation_method)):
            continue
        field = fields.get(key)
        if field is None or value is None:
            continue
        if field in {"limitUpHeight", "limitDownHeight"} and (
            not math.isfinite(float(value)) or float(value) < 1
        ):
            # A non-empty limit pool must contain at least one sealed board.
            # Older collection runs could persist 0 when the streak column was absent.
            continue
        day = _normalized_trading_day(str(trading_day))
        point = by_day.setdefault(day, _empty_a_share_point(day, str(timestamp)))
        point[field] = float(value)
        provenance = _a_share_point_provenance(metric_id, calculation_method, field)
        if provenance is not None:
            point.setdefault("sources", {})[field] = provenance
        if str(timestamp) > str(point["updatedAt"]):
            point["updatedAt"] = str(timestamp)
    failed_venues = _official_failed_venues(root)
    for point in by_day.values():
        sealed, broken = point["limitUpCount"], point["failedLimitUpCount"]
        if sealed is not None and broken is not None and sealed + broken > 0:
            point["sealedLimitUpRatePct"] = sealed / (sealed + broken) * 100
        if official_available:
            point["officialCoverage"] = _official_coverage(
                official_observations, point["tradingDay"], "STOCK_A",
            )
            point["officialCoverage"]["failedVenues"] = [
                venue for venue in failed_venues.get(point["tradingDay"], {}).get("STOCK_A", [])
                if venue in point["officialCoverage"]["missingVenues"]
            ]
    return [
        by_day[day] for day in sorted(by_day)
        if (start_day is None or day >= start_day) and (end_day is None or day <= end_day)
    ]


def _is_a_share_closed_day(raw_day: str) -> bool:
    try:
        return is_known_a_share_closed_day(date.fromisoformat(_normalized_trading_day(raw_day)))
    except ValueError:
        return True


def _trusted_a_share_snapshot_time(raw_day: str, timestamp: str, method: str) -> bool:
    """Reject old pre-close snapshots mislabelled with the current trading day."""

    if method.startswith("source=tdx-cn-v2; historical_silver_recovery;"):
        return True
    try:
        observed = datetime.fromisoformat(timestamp)
        if observed.tzinfo is None:
            return False
        beijing = observed.astimezone(ZoneInfo("Asia/Shanghai"))
        return (beijing.date().isoformat() == _normalized_trading_day(raw_day)
                and (beijing.hour, beijing.minute) >= (15, 30))
    except ValueError:
        return False


def _trusted_a_share_market_cap_history(metric_id: object, method: object, value: object) -> bool:
    """Only a complete, audited historical A-share valuation may replace the old spot series."""

    if (not str(metric_id).startswith("A_SHARE_MARKET_CAP_HISTORY:")
            or not str(metric_id).endswith(":TOTAL_MARKET_CAP_YI")
            or not str(method).startswith(_A_SHARE_CAP_HISTORY_PREFIX)):
        return False
    if ("formula=em_main+em_star+em_bse+szse_main_a+szse_chinext_a;" not in str(method)
            or "unit=亿元;" not in str(method)
            or set(_SHA256_EVIDENCE_RE.findall(str(method))) != {"em", "szse"}):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _a_share_point_provenance(metric_id: object, method: object, field: str) -> dict[str, Any] | None:
    if field not in {"totalMarketCapYi", "floatMarketCapYi", "turnoverYi", "advances", "unchanged", "declines"}:
        return None
    if str(metric_id).startswith(_OFFICIAL_PREFIX):
        return {"label": _OFFICIAL_SOURCE_LABEL, "venues": ["SSE", "SZSE", "BSE"]}
    if str(metric_id).startswith("A_SHARE_MARKET_CAP_HISTORY:"):
        return {"label": "东方财富估值分析＋深交所市场总貌"}
    if not str(metric_id).startswith("A_SHARE_BREADTH:"):
        return None
    method_text = str(method)
    if method_text.startswith("source=tdx-cn-v2; historical_silver_recovery;"):
        result: dict[str, Any] = {"label": "通达信本机质检日线"}
        coverage = _TDX_BREADTH_COVERAGE_RE.search(method_text)
        if coverage:
            result["coverage"] = int(coverage.group(1))
            result["pairedCoverage"] = int(coverage.group(2))
        return result
    if method_text.startswith("source=stock_zh_a_spot_tx"):
        return {"label": "腾讯行情收盘快照"}
    return None


def _hk_market_cap_rows(root: Path) -> list[dict[str, Any]]:
    catalog = Path(root) / "catalog.duckdb"
    if not catalog.is_file():
        return []
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return []
        rows = connection.execute("""
            SELECT trading_date,value,timestamp FROM gold_metrics
            WHERE metric_id LIKE 'HKEX_MARKET_CAP:HK.HKEX.EQUITY.ALL:%:1d:TOTAL_MARKET_CAP_YI'
              AND calculation_method LIKE 'source=HKEX Securities Statistics Archive;%'
            QUALIFY row_number() OVER (PARTITION BY trading_date ORDER BY timestamp DESC)=1
            ORDER BY trading_date
        """).fetchall()
    finally:
        connection.close()
    return [{"tradingDay": _normalized_trading_day(str(day)), "totalMarketCapYi": float(value),
             "updatedAt": str(timestamp)} for day, value, timestamp in rows
            if value is not None and math.isfinite(float(value)) and float(value) > 0]


def _merge_hk_cap_points(tdx_points: list[dict[str, Any]],
                         cap_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {point["tradingDay"]: dict(point) for point in tdx_points}
    for cap in cap_points:
        day = cap["tradingDay"]
        point = merged.setdefault(day, {
            "tradingDay": day, "totalMarketCapYi": None, "floatMarketCapYi": None,
            "turnoverYi": None, "advances": None, "unchanged": None, "declines": None,
            "limitUpCount": None, "limitDownCount": None, "coverage": 0,
            "turnoverCoverage": 0, "breadthCoverage": 0, "updatedAt": cap["updatedAt"],
        })
        point["totalMarketCapYi"] = cap["totalMarketCapYi"]
    return [merged[day] for day in sorted(merged)]


def _hk_market_cap_panel(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return _unavailable_panel("hk-market-cap", "港股市值",
                                  "港交所主板及GEM每日市价总值档案尚未导入；不由当前F10值回填历史。")
    latest = points[-1]
    return {"id": "hk-market-cap", "title": "港股市值", "status": "PARTIAL",
            "values": [{"metricKey": "totalMarketCapYi", "tradingDate": latest["tradingDay"],
                        "metricName": "主板及GEM总市值", "value": latest["totalMarketCapYi"],
                        "unit": "亿港元", "source": "港交所主板及GEM市价总值档案"}],
            "stale": latest["tradingDay"] < datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat(),
            "limitations": [f"已保存港交所档案最新观测日为{latest['tradingDay']}；"
                            "官网自动网页采集已暂停，较晚日期保持缺报。"]}


def _hk_disk_cache_path(root: Path) -> Path:
    return Path(root) / "state" / "hk_tdx_daily_overview.json"


def _read_hk_disk_cache(root: Path, revision: str) -> list[dict[str, Any]] | None:
    path = _hk_disk_cache_path(root)
    try:
        if path.stat().st_size > 4_000_000:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if (not isinstance(payload, dict) or payload.get("schema") != _HK_DISK_CACHE_SCHEMA
            or payload.get("revision") != revision or not isinstance(payload.get("points"), list)
            or len(payload["points"]) > 10_000):
        return None
    points = payload["points"]
    if any(not isinstance(item, dict) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",
           str(item.get("tradingDay", ""))) or
           type(item.get("turnoverCoverage")) is not int or item["turnoverCoverage"] < 0 or
           (item.get("turnoverYi") is not None and
            (not isinstance(item["turnoverYi"], (int, float)) or
             not math.isfinite(item["turnoverYi"]) or item["turnoverYi"] < 0))
           for item in points):
        return None
    days = [item["tradingDay"] for item in points]
    return points if days == sorted(set(days)) else None


def _write_hk_disk_cache(root: Path, revision: str, points: list[dict[str, Any]]) -> None:
    path = _hk_disk_cache_path(root)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"schema": _HK_DISK_CACHE_SCHEMA,
                                         "revision": revision, "points": points},
                                        ensure_ascii=False, separators=(",", ":")) + "\n",
                             encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass  # The in-memory result remains valid for this request.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


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

        disk_cached = _read_hk_disk_cache(root, key[1])
        if disk_cached is not None:
            _HK_OVERVIEW_CACHE.clear()
            _HK_OVERVIEW_CACHE[key] = disk_cached
            return [dict(item) for item in disk_cached]

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
        _write_hk_disk_cache(root, key[1], result)
        return [dict(item) for item in result]


def _cached_hk_tdx_overview_rows(root: Path) -> list[dict[str, Any]]:
    """Return an existing HK aggregate without triggering the on-demand scan."""

    key = (str(Path(root).resolve()), market_data_version(root))
    with _HK_OVERVIEW_LOCK:
        cached = _HK_OVERVIEW_CACHE.get(key)
        return [dict(item) for item in cached] if cached is not None else []


def _screen_hk_tdx_coverage(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Break a local-file coverage collapse, including a multi-day gap."""

    screened = [dict(point) for point in points]
    for index, point in enumerate(screened):
        nearby = screened[max(0, index - 20):index] + screened[index + 1:index + 21]
        reference = max((int(item.get("turnoverCoverage") or 0) for item in nearby), default=0)
        observed = int(point.get("turnoverCoverage") or 0)
        if reference >= 100 and observed < reference * .85:
            point["turnoverYi"] = None
            point["advances"] = None
            point["unchanged"] = None
            point["declines"] = None
            point["coverageStatus"] = "INSUFFICIENT_LOCAL_FILES"
            point["referenceCoverage"] = reference
    return screened


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
        "units": {"marketCap": "亿元", "turnover": "亿元", "breadth": "家", "limitPool": "家", "limitHeight": "板"},
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
    latest_market_day = max(
        (_normalized_trading_day(str(item["tradingDate"])) for key, item in snapshot.items()
         if key in {"TOTAL_AMOUNT_YI", "ADVANCES", "DECLINES"}),
        default=None,
    )
    stale = (panel_id in {"cn-market-cap", "cn-float-market-cap", "cn-turnover"}
             and bool(values) and latest_market_day is not None
             and _normalized_trading_day(str(values[0]["tradingDate"])) < latest_market_day)
    return {
        "id": panel_id,
        "title": title,
        "status": "PARTIAL" if values and (partial or stale) else "PASS" if values else fallback_status,
        "values": values,
        "stale": stale,
        "limitations": (
            ["市值、流通市值及成交额仅在沪深京三所相同交易日全部核验后发布；缺报不沿用旧快照。"]
            if panel_id in {"cn-market-cap", "cn-float-market-cap", "cn-turnover"}
            and values and values[0]["source"] == _OFFICIAL_SOURCE_LABEL
            else
            ["沪市主板、科创板、北证A股取东方财富指定交易日估值；深市主板A股、创业板A股取深交所同日日报。仅在同日分量核对通过后合计，缺日不沿用旧值。"]
            if panel_id == "cn-market-cap" and values and values[0]["source"] == "东方财富估值分析＋深交所市场总貌"
            else
            ["当前只具备全市场汇总，尚未按沪深主板、北交所、创业板、科创板分别验证。"]
            if panel_id == "cn-breadth" and values
            else ["涨跌停家数来自已落库涨跌停池；缺少某日来源时不以零补齐。"]
            if panel_id == "cn-limit-pool" and values
            else ["炸板数按当日炸板池中不同股票代码计数，逐只核对涨停价、收盘价和炸板次数；缺报不作零。"]
            if panel_id == "cn-broken-board" and values
            else ["每日最高连板数来自已落库的东方财富涨跌停池；两侧分别按真实观测日期展示，缺报不补零。"]
            if panel_id == "cn-limit-height" and values
            else []
        ),
    }


def _a_share_sealed_rate_panel(snapshot: dict[str, dict[str, Any]]) -> dict[str, Any]:
    closed, broken = snapshot.get("涨停_COUNT"), snapshot.get("炸板_COUNT")
    if closed is None or broken is None or (
        _normalized_trading_day(str(closed["tradingDate"])) !=
        _normalized_trading_day(str(broken["tradingDate"]))
    ):
        return _unavailable_panel("cn-sealed-rate", "A股封板率",
                                  "同一交易日的封住涨停数或炸板数缺报，封板率保持空值。")
    denominator = float(closed["value"]) + float(broken["value"])
    if denominator <= 0:
        return _unavailable_panel("cn-sealed-rate", "A股封板率", "当日触板股票分母为零，比例无定义。")
    day = _normalized_trading_day(str(closed["tradingDate"]))
    return {"id": "cn-sealed-rate", "title": "A股封板率", "status": "PARTIAL",
            "values": [{"metricKey": "sealedLimitUpRatePct", "metricName": "封板率",
                        "tradingDate": day, "value": float(closed["value"]) / denominator * 100,
                        "unit": "%", "source": "东方财富涨停池＋炸板池"}],
            "limitations": ["仅比较同一交易日的收盘封住涨停股票和盘中触板但收盘未封股票；"
                            "分母为零或任一池缺报时不计算。"]}


def _a_share_open_return_panel(snapshot: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = "YESTERDAY_LIMIT_UP_OPEN_RETURN"
    item = snapshot.get(key)
    if item is None:
        return _unavailable_panel(
            "cn-limit-up-open-return",
            "A股涨停接盘收益",
            "没有可证明按今日开盘买入、今日收盘卖出的昨日涨停池均值；历史收盘对昨收的涨跌幅不作替代。",
        )
    coverage = _open_return_coverage(item["calculationMethod"])
    latest_other_observation_day = max(
        (_normalized_trading_day(str(value["tradingDate"])) for metric, value in snapshot.items() if metric != key),
        default=None,
    )
    return_day = _normalized_trading_day(str(item["tradingDate"]))
    stale = latest_other_observation_day is not None and return_day < latest_other_observation_day
    limitations = [
        "昨日涨停池中仅对有当日开盘价和收盘价的样本计算等权平均；"
        "不足最低覆盖门时不发布该日收益，且不以今收相对昨收的涨跌幅代替。"
        "同花顺网页指标的成分和加权规则未公开核验，两者可能不同。"
    ]
    if stale:
        limitations.append(
            f"最近可信接盘收益日期为{return_day}，其它A股本地观测最新记载日期为{latest_other_observation_day}；"
            "较晚日期暂无可信接盘收益，不应将旧值解释为最新观测日数据。"
        )
    return {
        "id": "cn-limit-up-open-return",
        "title": "A股涨停接盘收益",
        "status": "PARTIAL",
        "values": [{**item, "coverage": coverage, "unit": "%",
                    "source": ("东方财富昨日涨停池＋腾讯指定交易日日线"
                               if item["calculationMethod"].startswith("source=eastmoney-previous-pool+tencent-dated-daily;")
                               else "东方财富昨日涨停池＋通达信日线")}],
        "latestOtherObservationDay": latest_other_observation_day,
        "stale": stale,
        "limitations": limitations,
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
            "仅汇总通过 tdx-cn-v2 质量门的本地港股股票日线；"
            "覆盖数不是港股全市场上市数量，成交额也不包含其他证券。"
            "样本覆盖低于相邻交易日参考值85%的日期不发布数值。"
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


@router.get("/ths-cpi")
def ths_cpi(request: Request) -> dict[str, Any]:
    """Read independent public Tonghuashun monthly CPI observations."""

    root = Path(request.app.state.data_root)
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return {"available": False, "points": [], "source": "同花顺CPI公开表",
                "limitations": ["尚未导入同花顺公开月度CPI。"]}
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            rows = []
        else:
            rows = connection.execute("""
                SELECT trading_date,metric_id,value,timestamp FROM gold_metrics
                WHERE metric_id LIKE 'THS_CPI:CN.CPI.THS:%:MONTHLY:%'
                  AND calculation_method LIKE 'source=https://data.10jqka.com.cn/macro/cpi/;%'
                QUALIFY row_number() OVER (PARTITION BY metric_id ORDER BY timestamp DESC)=1
                ORDER BY trading_date,metric_id
            """).fetchall()
    finally:
        connection.close()
    keys = {"NATIONAL_INDEX": "index", "NATIONAL_YOY_PCT": "yoyPct",
            "NATIONAL_MOM_PCT": "momPct"}
    by_month: dict[str, dict[str, Any]] = {}
    fetched_at = None
    for month, metric_id, value, timestamp in rows:
        month = str(month)
        key = str(metric_id).rsplit(":", 1)[-1]
        if key not in keys or not _PERIOD_RE.fullmatch(month) or value is None or not math.isfinite(float(value)):
            continue
        item = by_month.setdefault(month, {"month": month, "index": None, "yoyPct": None, "momPct": None})
        item[keys[key]] = float(value)
        fetched_at = max(str(timestamp), fetched_at or "")
    points = [by_month[month] for month in sorted(by_month)]
    return {"available": bool(points), "points": points, "source": "同花顺CPI公开表",
            "fetchedAt": fetched_at,
            "limitations": ["该来源按月观测，独立于通达信金融终端38# CPI；"
                            "最新观测月份以源表为准，不向每日价格图填充。"] if points
            else ["尚未导入同花顺公开月度CPI。"]}


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
    connection = _read_catalog(root)
    try:
        if not _has_gold_metrics(connection):
            return []
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
