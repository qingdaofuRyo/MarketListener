"""Read-only API for precomputed Chinese commodity-futures heat Gold rows."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
import re
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request

from market_monitor.futures import is_expired_futures_contract
from market_monitor.futures_heat import load_long_short_heat_config
from market_monitor.futures_structure import (
    MEMBER_OPEN_INTEREST_CHART_ID,
    MEMBER_STRUCTURE_FORMULA_VERSION,
    PRODUCT_OPEN_INTEREST_CHART_ID,
    STRUCTURE_DIRECTION_GROSS,
    STRUCTURE_FORMULA_VERSION,
)
from market_monitor.futures_member_positions import ALL_EXCHANGES, COMMODITY_EXCHANGES
from market_monitor.storage import MarketStore

from .common import read_bars
from .market import _logical_instruments


router = APIRouter(prefix="/api/futures", tags=["futures"])
_STRUCTURE_CHARTS = frozenset({PRODUCT_OPEN_INTEREST_CHART_ID, "product-notional", "member-notional", MEMBER_OPEN_INTEREST_CHART_ID})
_MEMBER_STRUCTURE_DIRECTIONS = frozenset({"long", "short", STRUCTURE_DIRECTION_GROSS, "net-long", "net-short"})
_CONTRACT_SERIES_KINDS = frozenset({"CONTRACT", "WEIGHTED"})

# These labels deliberately live next to the API contract.  The frontend must
# not infer a zero-valued line merely because a dependent Gold dataset has not
# been produced yet.
_NOTIONAL_UNAVAILABLE = "名义持仓规模等待全轮统一锁定结算价或收盘价 priceBasis；当前不会用收盘价静默代替。"
_BASIS_UNAVAILABLE = "基差等待现货规格、方向、单位换算和可追溯现货来源通过探针；当前不会以不同规格价格相减。"


@router.get("/heat")
def futures_heat(
    request: Request,
    start_day: str | None = None,
    end_day: str | None = None,
) -> dict[str, Any]:
    """Return stored Gold history; changing UI weights never invokes this API again."""

    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start_day must not be after end_day")
    root = Path(request.app.state.data_root)
    config = load_long_short_heat_config()
    catalog = root / "catalog.duckdb"
    rows: list[dict[str, Any]] = []
    if catalog.is_file():
        store = MarketStore(root)
        try:
            rows = store.list_futures_long_short_heat(
                start_day=start.isoformat() if start else None,
                end_day=end.isoformat() if end else None,
                formula_version=config.formula_version,
            )
        finally:
            store.close()
    points = [_point(row) for row in rows]
    latest = points[-1] if points else None
    limitations: list[str] = []
    if not points:
        limitations.append("尚未构建商品期货多空热度 Gold 数据，请先运行 futures-heat 离线任务。")
    elif latest and latest["fundScore10"] is None:
        limitations.append("沉淀资金必要字段覆盖率未达阈值，资金热度及用户组合热度保持不可用。")
    elif latest and (
        latest["coverage"]["missingVarietyCount"] > 0
        or latest["coverage"]["fundMissingVarietyCount"] > 0
    ):
        coverage = latest["coverage"]
        limitations.append(
            "最新交易日仍有资金字段缺口："
            f"{coverage['fundValidVarietyCount']}/{latest['validVarietyCount']} 个有效方向品种可计算沉淀资金。"
        )
    elif latest and latest["isWarmup"]:
        limitations.append("最新交易日的可用热度历史不足完整回看窗口，当前结果仍处于预热期。")
    fund_history = [point for point in points if point["fundScore10"] is not None]
    if points and len(fund_history) < len(points):
        first_day = fund_history[0]["tradeDate"] if fund_history else "无"
        limitations.append(
            "所选区间资金/总热度历史覆盖 "
            f"{len(fund_history)}/{len(points)} 个交易日，最早可用日为 {first_day}；"
            "缺失月份合约或精确规则的日期保持 null。"
        )
    return {
        "available": bool(points),
        "config": {
            "defaultUserWeight": {
                "breadthWeight": config.breadth_weight,
                "fundWeight": config.fund_weight,
            },
            "userWeight": {
                "min": config.user_weight_min,
                "max": config.user_weight_max,
                "step": config.user_weight_step,
            },
            "score": {"min": config.score_min, "max": config.score_max},
            "stateBands": [
                {"min": minimum, "max": maximum, "label": label}
                for minimum, maximum, label in config.state_bands
            ],
            "divergenceThreshold": config.divergence_threshold,
            "fundUnit": config.fund_unit,
            "lookbackTradingDays": config.lookback_trading_days,
            "halfLife": config.half_life_trading_days,
            "neutralThreshold": config.neutral_threshold,
            "minFundCoverage": config.min_fund_coverage,
            "excludeExchanges": sorted(config.exclude_exchanges),
        },
        "points": points,
        "latest": latest,
        "generatedAt": rows[-1]["calculated_at"] if rows else None,
        "source": "FUTURES_LONG_SHORT_HEAT",
        "formulaVersion": rows[-1]["formula_version"] if rows else config.formula_version,
        "limitations": limitations,
    }


@router.get("/contracts")
def futures_contracts(
    request: Request,
    trading_day: str | None = None,
    exchange: str | None = None,
    product: str | None = None,
    series_kind: str | None = None,
) -> dict[str, Any]:
    """List locally available commodity contract series for linked selectors.

    ``trading_day`` is a read-only availability probe.  It is intentionally
    evaluated only after the caller narrows the list by exchange/product,
    avoiding an unbounded historical scan at page bootstrap.
    """

    selected_day = _optional_day(trading_day, "trading_day")
    selected_exchange = _optional_commodity_exchange(exchange)
    selected_product = _optional_contract_code(product, "product")
    selected_kind = _optional_contract_series_kind(series_kind)
    root = Path(request.app.state.data_root)
    instruments = _contract_instruments(
        root,
        exchange=selected_exchange,
        product=selected_product,
        series_kind=selected_kind,
        include_expired=False,
    )
    if selected_day is not None and not (selected_exchange and selected_product):
        raise HTTPException(
            status_code=422,
            detail="trading_day availability probe requires both exchange and product",
        )
    if selected_day is not None:
        requested = selected_day.isoformat()
        instruments = [
            item for item in instruments
            if any(str(bar.get("trading_date") or "")[:10] == requested for bar in read_bars(
                root, str(item["storageInstrumentId"]), period="1d", limit=5_000
            ))
        ]
    items = [_contract_option(item) for item in instruments]
    return {
        "available": bool(items),
        "tradingDay": selected_day.isoformat() if selected_day else None,
        "filters": {
            "exchange": selected_exchange,
            "product": selected_product,
            "seriesKind": selected_kind,
        },
        "items": items,
        "limitations": [
            "默认仅列出国内商品期货的具体月份合约和本地已存在的加权合约；中金所金融期货、主连、次连和商品指数不在此接口范围。",
            *( ["指定交易日时请同时选择交易所和品种，系统才会逐合约核验该日真实日线。"] if selected_day is None else []),
        ],
    }


@router.get("/contract-series")
def futures_contract_series(
    request: Request,
    exchange: str,
    product: str,
    contract: str | None = None,
    series_kind: str = "CONTRACT",
    start_day: str | None = None,
    end_day: str | None = None,
) -> dict[str, Any]:
    """Return price/OI plus explicit, non-fabricated notional and basis slots.

    A series is kept source-isolated.  This endpoint never builds a weighted
    contract from month contracts: a weighted row must already exist as a
    locally stored, independently sourced series with composition evidence.
    """

    selected_exchange = _required_commodity_exchange(exchange)
    selected_product = _required_contract_code(product, "product")
    selected_kind = _required_contract_series_kind(series_kind)
    selected_contract = _optional_contract_code(contract, "contract")
    if selected_kind == "CONTRACT" and selected_contract is None:
        raise HTTPException(status_code=422, detail="contract is required when series_kind=CONTRACT")
    if selected_kind == "WEIGHTED" and selected_contract is not None:
        raise HTTPException(status_code=422, detail="contract must be omitted when series_kind=WEIGHTED")
    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start_day must not be after end_day")
    root = Path(request.app.state.data_root)
    matches = _contract_instruments(
        root,
        exchange=selected_exchange,
        product=selected_product,
        series_kind=selected_kind,
        contract=selected_contract,
    )
    if not matches:
        raise HTTPException(status_code=404, detail="local futures contract series not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="more than one local series matches; choose an exact contract or resolve duplicate source metadata",
        )
    instrument = matches[0]
    bars = read_bars(root, str(instrument["storageInstrumentId"]), period="1d", limit=5_000)
    points = [_contract_series_point(bar) for bar in bars]
    if start is not None:
        points = [point for point in points if point["tradingDay"] >= start.isoformat()]
    if end is not None:
        points = [point for point in points if point["tradingDay"] <= end.isoformat()]
    has_open_interest = any(point["openInterest"] is not None for point in points)
    return {
        "available": bool(points),
        "instrument": _contract_option(instrument),
        "seriesKind": selected_kind,
        "priceBasis": None,
        "units": {
            "price": "来源报价单位（待合约规格审计）",
            "openInterest": "contracts",
            "notional": "CNY",
            "basis": None,
        },
        "availability": {
            "price": {"available": bool(points), "render": "candlestick", "reason": None if points else "本地没有日线 OHLC。"},
            "openInterest": {
                "available": has_open_interest,
                "render": "line",
                "reason": None if has_open_interest else "本地日线没有交易所单边持仓量。",
            },
            "notional": {"available": False, "render": "line", "reason": _NOTIONAL_UNAVAILABLE},
            "basis": {"available": False, "render": "line", "reason": _BASIS_UNAVAILABLE},
        },
        "points": points,
        "source": str(instrument.get("actualSource") or instrument.get("source") or ""),
        "updatedAt": instrument.get("lastBarAt"),
        "limitations": [
            "价格序列保留本地来源的 OHLC；持仓量为交易所公布的单边持仓量。",
            _NOTIONAL_UNAVAILABLE,
            _BASIS_UNAVAILABLE,
            *( ["加权合约不会由接口临时合成；需先导入具有成分合约和权重证据的独立加权序列。"] if selected_kind == "WEIGHTED" else []),
        ],
    }


@router.get("/structures/{chart_id}")
def futures_structure(
    request: Request,
    chart_id: str,
    direction: str = STRUCTURE_DIRECTION_GROSS,
    range: str = "1y",
    start_day: str | None = None,
    end_day: str | None = None,
    level: str = "main",
) -> dict[str, Any]:
    """Return only materialised, fixed-baseline futures structures.

    Unimplemented chart IDs intentionally return an explicit unavailable
    payload rather than deriving a plausible-looking series from Silver.
    """

    if chart_id not in _STRUCTURE_CHARTS:
        raise HTTPException(status_code=404, detail="unknown futures structure chart_id")
    if level not in {"main", "other"}:
        raise HTTPException(status_code=422, detail="level must be main or other")
    if range not in {"1y", "3y", "5y", "all"}:
        raise HTTPException(status_code=422, detail="range must be 1y, 3y, 5y or all")
    if chart_id == PRODUCT_OPEN_INTEREST_CHART_ID and direction == STRUCTURE_DIRECTION_GROSS:
        formula_version = STRUCTURE_FORMULA_VERSION
    elif chart_id == MEMBER_OPEN_INTEREST_CHART_ID and direction in _MEMBER_STRUCTURE_DIRECTIONS:
        formula_version = MEMBER_STRUCTURE_FORMULA_VERSION
    else:
        return _unavailable_structure(chart_id, direction, "该结构尚未建立可审计的 Gold 数据与固定基准。")
    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start_day must not be after end_day")
    root = Path(request.app.state.data_root)
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return _unavailable_structure(chart_id, direction, "尚未构建期货结构 Gold 数据，请先运行 futures-structure 离线任务。")
    store = MarketStore(root)
    try:
        baseline = store.get_futures_structure_baseline(
            chart_id=chart_id,
            direction=direction,
            formula_version=formula_version,
        )
        if baseline is None:
            return _unavailable_structure(chart_id, direction, "尚未建立固定堆叠基准，请先运行 futures-structure 离线任务。")
        all_rows = store.list_futures_structure_daily(
            chart_id=chart_id,
            direction=direction,
            formula_version=formula_version,
        )
    finally:
        store.close()
    if not all_rows:
        return _unavailable_structure(chart_id, direction, "期货结构 Gold 数据为空，请先运行 futures-structure 离线任务。")
    latest_day = all_rows[-1]["trade_date"]
    effective_start = max(
        [item for item in (_range_start(latest_day, range), start.isoformat() if start else None) if item],
        default=None,
    )
    effective_end = end.isoformat() if end else None
    rows = [
        row for row in all_rows
        if (effective_start is None or row["trade_date"] >= effective_start)
        and (effective_end is None or row["trade_date"] <= effective_end)
    ]
    return _structure_payload(baseline, rows, level)


@router.get("/member-positions")
def futures_member_positions(
    request: Request,
    trading_day: str | None = None,
    exchange: str | None = None,
    contract_code: str | None = None,
    product_code: str | None = None,
    commodity_only: bool = True,
) -> dict[str, Any]:
    """Return published member ranking coverage for one futures trading day.

    The result intentionally represents an unpublished direction as ``null``:
    absence from a top-ranked list is not evidence of a zero position.
    """

    selected_exchange = _optional_exchange(exchange)
    selected_contract = _optional_contract_code(contract_code, "contract_code")
    selected_product = _optional_contract_code(product_code, "product_code")
    selected_day = _optional_day(trading_day, "trading_day")
    root = Path(request.app.state.data_root)
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return _unavailable_member_positions("尚未采集交易所会员持仓排名。")
    store = MarketStore(root)
    try:
        effective_day = selected_day.isoformat() if selected_day else store.latest_futures_member_position_day(
            commodity_only=commodity_only
        )
        if effective_day is None:
            return _unavailable_member_positions("尚未采集交易所会员持仓排名。")
        ranks = store.list_futures_member_position_ranks(
            trading_day=effective_day,
            exchange=selected_exchange,
            contract_code=selected_contract,
            product_code=selected_product,
            commodity_only=commodity_only,
        )
    finally:
        store.close()
    return _member_position_payload(
        ranks,
        trading_day=effective_day,
        exchange=selected_exchange,
        contract_code=selected_contract,
        product_code=selected_product,
        commodity_only=commodity_only,
        include_rows=selected_contract is not None or selected_product is not None,
    )


def _point(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tradeDate": row["trade_date"],
        "totalVarietyCount": row["total_variety_count"],
        "validVarietyCount": row["valid_variety_count"],
        "missingVarietyCount": row["missing_variety_count"],
        "upVarietyCount": row["up_variety_count"],
        "downVarietyCount": row["down_variety_count"],
        "flatVarietyCount": row["flat_variety_count"],
        "fundValidVarietyCount": row["fund_valid_variety_count"],
        "fundMissingVarietyCount": row["fund_missing_variety_count"],
        "upFund": row["up_fund"],
        "downFund": row["down_fund"],
        "flatFund": row["flat_fund"],
        "breadthScoreDaily": row["breadth_score_daily"],
        "fundScoreDaily": row["fund_score_daily"],
        "breadthScore10": row["breadth_score_10d"],
        "fundScore10": row["fund_score_10d"],
        "divergence": row["divergence"],
        "isWarmup": row["is_warmup"],
        "dataQualityStatus": row["data_quality_status"],
        "coverage": {
            "variety": row["return_coverage"],
            "fund": row["fund_coverage"],
            "validVarietyCount": row["valid_variety_count"],
            "missingVarietyCount": row["missing_variety_count"],
            "fundValidVarietyCount": row["fund_valid_variety_count"],
            "fundMissingVarietyCount": row["fund_missing_variety_count"],
        },
        "formulaVersion": row["formula_version"],
        "sourceCutoff": row["source_cutoff"],
        "calculationMethod": row["calculation_method"],
        "calculatedAt": row["calculated_at"],
    }


def _structure_payload(
    baseline: Mapping[str, Any], rows: list[Mapping[str, Any]], level: str
) -> dict[str, Any]:
    by_day: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_day[str(row["trade_date"])][str(row["member_key"])] = row
    dates = sorted(by_day)
    primary = list(baseline["primary_members"])
    other = list(baseline["other_members"])
    known_keys = {
        str(member["memberKey"])
        for member in [*primary, *other]
    }
    all_members: dict[str, str] = {}
    for row in rows:
        all_members[str(row["member_key"])] = str(row["member_name"])
    unclassified = [
        {"memberKey": key, "memberName": all_members[key]}
        for key in sorted(set(all_members) - known_keys)
    ]
    totals = [sum(float(row["value"]) for row in by_day[day].values()) for day in dates]
    unclassified_totals = [
        sum(float(row["value"]) for key, row in by_day[day].items() if key not in known_keys)
        for day in dates
    ]
    main_series = [_member_series(member, dates, by_day) for member in primary]
    if other:
        main_series.append(_other_series(other, dates, by_day))
    series = [_member_series(member, dates, by_day) for member in other] if level == "other" else main_series
    coverage = [
        {
            "tradeDate": day,
            "inputRowCount": max(int(row["input_row_count"]) for row in by_day[day].values()),
            "missingRowCount": max(int(row["missing_row_count"]) for row in by_day[day].values()),
            "dataQualityStatus": "PARTIAL" if any(
                row["data_quality_status"] == "PARTIAL" for row in by_day[day].values()
            ) else "PASS",
        }
        for day in dates
    ]
    return {
        "available": bool(dates),
        "chartId": baseline["chart_id"],
        "metric": "openInterest",
        "direction": baseline["direction"],
        "unit": "contracts",
        "baselineDay": baseline["baseline_day"],
        "baselineVersion": baseline["baseline_version"],
        "threshold": baseline["threshold"],
        "stackOrder": baseline["stack_order"],
        "primaryMembers": primary,
        "otherMembers": other,
        "unclassifiedMembers": unclassified,
        "dates": dates,
        "series": series,
        "totals": totals,
        "unclassifiedTotals": unclassified_totals,
        "coverage": coverage,
        "formulaVersion": baseline["formula_version"],
        "priceBasis": baseline["price_basis"],
        "source": baseline["source"],
        "updatedAt": max((str(row["calculated_at"]) for row in rows), default=baseline["created_at"]),
        "limitations": [
            "仅统计国内商品期货有效月份合约的交易所单边持仓量；已排除中金所金融期货。",
            "“其他”成员集合和堆叠顺序固定于基准日；基准日后出现的新成员单列为未分类，不会静默并入其他。",
        ],
    }


def _member_series(
    member: Mapping[str, Any], dates: list[str], by_day: Mapping[str, Mapping[str, Mapping[str, Any]]]
) -> dict[str, Any]:
    key = str(member["memberKey"])
    return {
        "memberKey": key,
        "memberName": member["memberName"],
        "values": [float(by_day[day][key]["value"]) if key in by_day[day] else None for day in dates],
    }


def _other_series(
    members: list[Mapping[str, Any]], dates: list[str], by_day: Mapping[str, Mapping[str, Mapping[str, Any]]]
) -> dict[str, Any]:
    keys = {str(member["memberKey"]) for member in members}
    return {
        "memberKey": "OTHER",
        "memberName": "其他",
        "memberCount": len(keys),
        "values": [
            sum(float(by_day[day][key]["value"]) for key in keys if key in by_day[day])
            for day in dates
        ],
    }


def _range_start(latest_day: str, range: str) -> str | None:
    if range == "all":
        return None
    years = {"1y": 1, "3y": 3, "5y": 5}[range]
    return (date.fromisoformat(latest_day) - timedelta(days=365 * years)).isoformat()


def _unavailable_structure(chart_id: str, direction: str, limitation: str) -> dict[str, Any]:
    return {
        "available": False,
        "chartId": chart_id,
        "metric": None,
        "direction": direction,
        "unit": None,
        "baselineDay": None,
        "baselineVersion": None,
        "threshold": None,
        "stackOrder": [],
        "primaryMembers": [],
        "otherMembers": [],
        "unclassifiedMembers": [],
        "dates": [],
        "series": [],
        "totals": [],
        "unclassifiedTotals": [],
        "coverage": [],
        "formulaVersion": None,
        "priceBasis": None,
        "source": None,
        "updatedAt": None,
        "limitations": [limitation],
    }


def _member_position_payload(
    ranks: list[Mapping[str, Any]],
    *,
    trading_day: str,
    exchange: str | None,
    contract_code: str | None,
    product_code: str | None,
    commodity_only: bool,
    include_rows: bool,
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    contracts: set[tuple[str, str, str]] = set()
    for item in ranks:
        current_exchange = str(item["exchange"])
        current_contract = str(item["contract_code"])
        current_product = str(item["product_code"])
        contracts.add((current_exchange, current_product, current_contract))
        key = (current_exchange, current_contract, str(item["member_key"]))
        row = groups.setdefault(
            key,
            {
                "exchange": current_exchange,
                "contractCode": current_contract,
                "productCode": current_product,
                "memberKey": str(item["member_key"]),
                "memberName": str(item["member_name"]),
                "longPosition": None,
                "longPositionChange": None,
                "longRank": None,
                "shortPosition": None,
                "shortPositionChange": None,
                "shortRank": None,
                "sources": set(),
            },
        )
        prefix = "long" if item["side"] == "LONG" else "short"
        row[f"{prefix}Position"] = float(item["position"])
        row[f"{prefix}PositionChange"] = (
            float(item["position_change"]) if item["position_change"] is not None else None
        )
        row[f"{prefix}Rank"] = int(item["rank"])
        row["sources"].add(str(item["source"]))
    rows: list[dict[str, Any]] = []
    for row in groups.values():
        long_position = row["longPosition"]
        short_position = row["shortPosition"]
        net = long_position - short_position if long_position is not None and short_position is not None else None
        rows.append(
            {
                **{key: value for key, value in row.items() if key != "sources"},
                "netPosition": net,
                "netLongPosition": max(net, 0) if net is not None else None,
                "netShortPosition": max(-net, 0) if net is not None else None,
                "sources": sorted(row["sources"]),
            }
        )
    rows.sort(
        key=lambda row: (
            row["exchange"], row["contractCode"],
            min(value for value in (row["longRank"], row["shortRank"]) if value is not None),
            row["memberKey"],
        )
    )
    contract_options = [
        {"exchange": item[0], "productCode": item[1], "contractCode": item[2]}
        for item in sorted(contracts)
    ]
    exchanges = sorted({str(item["exchange"]) for item in ranks})
    missing_exchanges = sorted(COMMODITY_EXCHANGES - set(exchanges)) if commodity_only else []
    return {
        "available": bool(ranks),
        "tradingDay": trading_day,
        "filters": {
            "exchange": exchange,
            "contractCode": contract_code,
            "productCode": product_code,
            "commodityOnly": commodity_only,
        },
        "contracts": contract_options,
        "rows": rows if include_rows else [],
        "coverage": {
            "publishedDirectionRankCount": len(ranks),
            "memberCount": len(rows),
            "exchangeCount": len(exchanges),
            "exchanges": exchanges,
            "missingExchanges": missing_exchanges,
            "isComplete": not missing_exchanges,
        },
        "limitations": [
            "仅统计交易所实际公布的会员方向排名，覆盖总计不是全市场会员全部持仓。",
            "会员未出现在另一方向排名时以 null 表示未公布，不能据此推断持仓为 0 或计算净持仓。",
            "默认排除中金所金融期货；可通过 commodity_only=false 查看已采集的全部交易所。",
            *( [] if include_rows else ["请选择交易所、品种或具体月份合约后再读取席位明细，避免默认传输全市场大字段。"]),
        ],
    }


def _unavailable_member_positions(limitation: str) -> dict[str, Any]:
    return {
        "available": False,
        "tradingDay": None,
        "filters": {"exchange": None, "contractCode": None, "productCode": None, "commodityOnly": True},
        "contracts": [],
        "rows": [],
        "coverage": {
            "publishedDirectionRankCount": 0,
            "memberCount": 0,
            "exchangeCount": 0,
            "exchanges": [],
            "missingExchanges": sorted(COMMODITY_EXCHANGES),
            "isComplete": False,
        },
        "limitations": [limitation],
    }


def _contract_instruments(
    root: Path,
    *,
    exchange: str | None = None,
    product: str | None = None,
    series_kind: str | None = None,
    contract: str | None = None,
    include_expired: bool = True,
) -> list[dict[str, Any]]:
    """Return canonical, source-selected local commodity contract series."""

    rows: list[dict[str, Any]] = []
    for item in _logical_instruments(root).values():
        if str(item.get("assetType") or "").upper() != "FUTURE":
            continue
        current_exchange = str(item.get("exchange") or "").upper()
        current_product = str(item.get("productCode") or "").upper()
        current_kind = str(item.get("seriesKind") or "").upper()
        if current_exchange not in COMMODITY_EXCHANGES or current_kind not in _CONTRACT_SERIES_KINDS:
            continue
        if not item.get("storageInstrumentId"):
            continue
        if exchange is not None and current_exchange != exchange:
            continue
        if product is not None and current_product != product:
            continue
        if series_kind is not None and current_kind != series_kind:
            continue
        if (
            not include_expired
            and current_kind == "CONTRACT"
            and is_expired_futures_contract(
                str(item.get("symbol") or ""), current_exchange, reference_day=date.today()
            )
        ):
            continue
        symbols = {
            str(item.get("symbol") or "").upper(),
            str(item.get("sourceSymbol") or "").upper(),
        }
        if contract is not None and contract not in symbols:
            continue
        rows.append(dict(item))
    rows.sort(key=lambda item: (
        str(item.get("exchange") or ""),
        str(item.get("productCode") or ""),
        str(item.get("seriesKind") or ""),
        str(item.get("symbol") or ""),
        str(item.get("instrumentId") or ""),
    ))
    return rows


def _contract_option(item: Mapping[str, Any]) -> dict[str, Any]:
    """Public selector shape; storage IDs remain an API implementation detail."""

    return {
        "instrumentId": str(item.get("instrumentId") or ""),
        "exchange": str(item.get("exchange") or "").upper(),
        "productCode": str(item.get("productCode") or "").upper(),
        "contractCode": str(item.get("symbol") or item.get("sourceSymbol") or "").upper(),
        "name": str(item.get("name") or item.get("symbol") or ""),
        "seriesKind": str(item.get("seriesKind") or "").upper(),
        "lastTradingDay": str(item.get("lastBarAt") or "")[:10] or None,
        "actualSource": str(item.get("actualSource") or item.get("source") or "") or None,
        "qualityStatus": str(item.get("qualityStatus") or "") or None,
    }


def _contract_series_point(bar: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one local daily bar into the fixed four-series response shape."""

    trading_day = str(bar.get("trading_day") or bar.get("trading_date") or bar.get("bar_open_time") or "")[:10]
    return {
        "tradingDay": trading_day,
        "open": _finite_number(bar.get("open")),
        "high": _finite_number(_first_present(bar, "high", "highest")),
        "low": _finite_number(_first_present(bar, "low", "lowest")),
        "close": _finite_number(bar.get("close")),
        "settlement": _finite_number(bar.get("settlement")),
        "openInterest": _finite_number(_first_present(bar, "open_interest", "openInterest")),
        "notionalRmb": None,
        "basisRmb": None,
        "basisPercent": None,
        "unavailable": {"notional": _NOTIONAL_UNAVAILABLE, "basis": _BASIS_UNAVAILABLE},
    }


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in {float("inf"), float("-inf")} else None


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _optional_day(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD") from error


def _optional_exchange(value: str | None) -> str | None:
    if value is None:
        return None
    exchange = value.strip().upper()
    if exchange not in ALL_EXCHANGES:
        raise HTTPException(status_code=422, detail="exchange must be a supported domestic futures exchange")
    return exchange


def _optional_commodity_exchange(value: str | None) -> str | None:
    if value is None:
        return None
    exchange = value.strip().upper()
    if exchange not in COMMODITY_EXCHANGES:
        raise HTTPException(status_code=422, detail="exchange must be a supported commodity futures exchange")
    return exchange


def _required_commodity_exchange(value: str) -> str:
    exchange = _optional_commodity_exchange(value)
    if exchange is None:
        raise HTTPException(status_code=422, detail="exchange is required")
    return exchange


def _optional_contract_code(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]+[0-9]*", code):
        raise HTTPException(status_code=422, detail=f"{field} must contain only an uppercase product code and digits")
    return code


def _required_contract_code(value: str, field: str) -> str:
    code = _optional_contract_code(value, field)
    if code is None:
        raise HTTPException(status_code=422, detail=f"{field} is required")
    return code


def _optional_contract_series_kind(value: str | None) -> str | None:
    if value is None:
        return None
    kind = value.strip().upper()
    if kind not in _CONTRACT_SERIES_KINDS:
        raise HTTPException(status_code=422, detail="series_kind must be CONTRACT or WEIGHTED")
    return kind


def _required_contract_series_kind(value: str) -> str:
    kind = _optional_contract_series_kind(value)
    if kind is None:
        raise HTTPException(status_code=422, detail="series_kind is required")
    return kind


__all__ = ("router",)
