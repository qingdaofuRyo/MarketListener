"""Read-only API for precomputed Chinese commodity-futures heat Gold rows."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request

from market_monitor.futures import contract_delivery_month, futures_product_name, is_expired_futures_contract
from market_monitor.futures_contract_turnover_heat import (
    FORMULA_VERSION as CONTRACT_HEAT_FORMULA_VERSION,
    HALF_LIFE_DAYS as CONTRACT_HEAT_HALF_LIFE,
    LOOKBACK_DAYS as CONTRACT_HEAT_LOOKBACK,
    MIN_TURNOVER_COVERAGE,
)
from market_monitor.futures_heat import load_long_short_heat_config
from market_monitor.futures_structure import (
    MEMBER_OPEN_INTEREST_CHART_ID,
    MEMBER_STRUCTURE_FORMULA_VERSION,
    PRODUCT_NOTIONAL_CHART_ID,
    PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION,
    PRODUCT_NOTIONAL_FORMULA_VERSION,
    PRODUCT_OPEN_INTEREST_CHART_ID,
    PRODUCT_VOLUME_CHART_ID,
    PRODUCT_STRUCTURE_RANK_LIMIT,
    SETTLEMENT_PRICE_BASIS,
    STRUCTURE_DIRECTION_GROSS,
    STRUCTURE_FORMULA_VERSION,
)
from market_monitor.futures_official_structure import (
    OFFICIAL_PRODUCT_NOTIONAL_FORMULA_VERSION,
    OFFICIAL_PRODUCT_OI_FORMULA_VERSION,
    OFFICIAL_PRODUCT_VOLUME_FORMULA_VERSION,
)
from market_monitor.futures_rule_sync import RuleBook, load_rule_book
from market_monitor.futures_member_positions import ALL_EXCHANGES, COMMODITY_EXCHANGES
from market_monitor.storage import MarketStore

from .common import read_bars
from .market import _logical_instruments


router = APIRouter(prefix="/api/futures", tags=["futures"])
_STRUCTURE_CHARTS = frozenset({PRODUCT_OPEN_INTEREST_CHART_ID, PRODUCT_NOTIONAL_CHART_ID, PRODUCT_VOLUME_CHART_ID, "member-notional", MEMBER_OPEN_INTEREST_CHART_ID})
_MEMBER_STRUCTURE_DIRECTIONS = frozenset({"long", "short", STRUCTURE_DIRECTION_GROSS, "net-long", "net-short"})
_CONTRACT_SERIES_KINDS = frozenset({"CONTRACT", "WEIGHTED"})
_MEMBER_SCOPE_EXCHANGES = {
    "commodity": frozenset(COMMODITY_EXCHANGES),
    "all": frozenset(ALL_EXCHANGES),
}

# These labels deliberately live next to the API contract.  The frontend must
# not infer a zero-valued line merely because a dependent Gold dataset has not
# been produced yet.
_NOTIONAL_UNAVAILABLE = "名义持仓规模缺少同交易日的结算价、交易乘数或交易所单边持仓量。"
_WEIGHTED_NOTIONAL_UNAVAILABLE = "加权合约的持仓与名义规模须由有效月份合约汇总；当前独立加权序列不以自身持仓量代替。"
_BASIS_UNAVAILABLE = "基差等待现货规格、方向、单位换算和可追溯现货来源通过探针；当前不会以不同规格价格相减。"


@router.get("/contract-heat")
def futures_contract_heat(
    request: Request,
    start_day: str | None = None,
    end_day: str | None = None,
) -> dict[str, Any]:
    """Return the contract-level turnover formula without reusing v1 deposits."""

    start = _optional_day(start_day, "start_day")
    end = _optional_day(end_day, "end_day")
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start_day must not be after end_day")
    root = Path(request.app.state.data_root)
    rows: list[dict[str, Any]] = []
    if (root / "catalog.duckdb").is_file():
        store = MarketStore(root)
        try:
            rows = store.list_futures_contract_turnover_heat(
                formula_version=CONTRACT_HEAT_FORMULA_VERSION,
                start_day=start.isoformat() if start else None,
                end_day=end.isoformat() if end else None,
            )
        finally:
            store.close()
    config = load_long_short_heat_config()
    points = [
        {
            "tradeDate": row["trade_date"],
            "totalContractCount": row["total_contract_count"],
            "validContractCount": row["valid_contract_count"],
            "missingDirectionCount": row["missing_direction_count"],
            "upContractCount": row["up_contract_count"],
            "downContractCount": row["down_contract_count"],
            "flatContractCount": row["flat_contract_count"],
            "turnoverValidContractCount": row["turnover_valid_contract_count"],
            "turnoverMissingContractCount": row["turnover_missing_contract_count"],
            "upTurnoverCny": row["up_turnover_cny"],
            "downTurnoverCny": row["down_turnover_cny"],
            "flatTurnoverCny": row["flat_turnover_cny"],
            "totalDepositCny": row["total_deposit_cny"],
            "countScore10": row["count_score_10d"],
            "turnoverScore10": row["turnover_score_10d"],
            "divergence": row["divergence"],
            "isWarmup": row["is_warmup"],
            "dataQualityStatus": row["data_quality_status"],
            "coverage": {
                "direction": row["direction_coverage"],
                "turnover": row["turnover_coverage"],
                "deposit": row["deposit_coverage"],
                "missingExchanges": json.loads(row["missing_exchanges_json"]),
            },
        }
        for row in rows
    ]
    latest = points[-1] if points else None
    limitations = [] if points else ["尚未构建月份合约成交额热度，请先同步交易所日行情并运行 futures-contract-heat。"]
    if latest and latest["turnoverScore10"] is None:
        limitations.append(
            "最新交易日没有可计算的官方逐合约成交额，成交额热度暂不可用；"
            f"当前为{latest['turnoverValidContractCount']}/{latest['validContractCount']}个有效方向合约，"
            f"缺失交易所：{'、'.join(latest['coverage']['missingExchanges']) or '无'}。"
        )
    elif latest and latest["coverage"]["turnover"] < MIN_TURNOVER_COVERAGE:
        limitations.append(
            "成交额热度仅反映已核验的交易所官方成交额样本，不代表全市场完整结果；"
            f"覆盖 {latest['turnoverValidContractCount']}/{latest['validContractCount']} 个有效方向合约"
            f"（{latest['coverage']['turnover']:.1%}），缺失交易所："
            f"{'、'.join(latest['coverage']['missingExchanges']) or '无'}。"
        )
    return {
        "available": bool(points),
        "config": {
            "defaultUserWeight": {"contractWeight": config.breadth_weight, "turnoverWeight": config.fund_weight},
            "userWeight": {"min": config.user_weight_min, "max": config.user_weight_max, "step": config.user_weight_step},
            "score": {"min": config.score_min, "max": config.score_max},
            "stateBands": [
                {"min": minimum, "max": maximum, "label": label}
                for minimum, maximum, label in config.state_bands
            ],
            "lookbackTradingDays": CONTRACT_HEAT_LOOKBACK,
            "halfLife": CONTRACT_HEAT_HALF_LIFE,
            "divergenceThreshold": config.divergence_threshold,
            "minTurnoverCoverage": MIN_TURNOVER_COVERAGE,
            "turnoverUnit": "CNY",
            "depositUnit": "CNY",
        },
        "points": points,
        "latest": latest,
        "generatedAt": rows[-1]["calculated_at"] if rows else None,
        "source": "CN_FUTURE_SILVER+EXCHANGE_OFFICIAL_DAILY",
        "formulaVersion": CONTRACT_HEAT_FORMULA_VERSION,
        "limitations": limitations,
    }


@router.get("/official-daily")
def futures_official_daily(
    request: Request,
    trading_day: str,
    exchange: str | None = None,
    contract_code: str | None = None,
) -> dict[str, Any]:
    """Expose source-isolated exchange daily values and their fetch quality."""

    day = _optional_day(trading_day, "trading_day")
    if day is None:
        raise HTTPException(status_code=422, detail="trading_day is required")
    venue = _optional_exchange(exchange)
    contract = _optional_contract_code(contract_code, "contract_code")
    root = Path(request.app.state.data_root)
    if not (root / "catalog.duckdb").is_file():
        return {"available": False, "tradingDay": day.isoformat(), "rows": [], "coverage": []}
    store = MarketStore(root)
    try:
        source_rows = store.list_futures_official_daily(
            start_day=day.isoformat(), end_day=day.isoformat(),
        )
        source_coverage = store.list_futures_official_daily_coverage(
            start_day=day.isoformat(), end_day=day.isoformat(),
        )
    finally:
        store.close()
    rows = [
        {
            "exchange": row["exchange"], "contractCode": row["contract_code"],
            "productCode": row["product_code"], "close": row["close"],
            "settlement": row["settlement"], "volume": row["volume"],
            "openInterest": row["open_interest"],
            "turnoverRaw": row["turnover_raw"], "turnoverCny": row["turnover_cny"],
            "turnoverUnit": row["turnover_unit"],
            "turnoverSidedness": row["turnover_sidedness"],
            "volumeSidedness": row["volume_sidedness"],
            "openInterestSidedness": row["open_interest_sidedness"],
            "turnoverQualityStatus": row["turnover_quality_status"],
            "parserVersion": row["parser_version"],
            "sourceUrl": row["source_url"], "adapter": row["adapter"],
            "retrievedAt": row["retrieved_at"], "qualityStatus": row["quality_status"],
        }
        for row in source_rows
        if (venue is None or row["exchange"] == venue)
        and (contract is None or row["contract_code"] == contract)
    ]
    coverage = [
        {
            "exchange": row["exchange"], "status": row["status"],
            "usableCount": row["usable_count"], "recordCount": row["record_count"],
            "sourceUrl": row["source_url"], "adapter": row["adapter"],
            "error": row["error"], "retrievedAt": row["retrieved_at"],
        }
        for row in source_coverage if venue is None or row["exchange"] == venue
    ]
    return {
        "available": bool(rows), "tradingDay": day.isoformat(),
        "rows": rows, "coverage": coverage,
    }


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
        include_expired=selected_day is not None,
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
            "默认仅列出当前未到期的国内商品期货月份合约和本地加权合约；指定历史交易日时保留当日有真实日线的已到期合约。中金所金融期货、主连、次连和商品指数不在此接口范围。",
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
    rule_book = load_rule_book(root)
    points = [
        _contract_series_point(
            bar,
            exchange=selected_exchange,
            product=selected_product,
            series_kind=selected_kind,
            rule_book=rule_book,
        )
        for bar in bars
    ]
    if start is not None:
        points = [point for point in points if point["tradingDay"] >= start.isoformat()]
    if end is not None:
        points = [point for point in points if point["tradingDay"] <= end.isoformat()]
    has_open_interest = any(point["openInterest"] is not None for point in points)
    has_notional = any(point["notionalRmb"] is not None for point in points)
    notional_reason = (
        _WEIGHTED_NOTIONAL_UNAVAILABLE
        if selected_kind == "WEIGHTED"
        else None if has_notional else _NOTIONAL_UNAVAILABLE
    )
    return {
        "available": bool(points),
        "instrument": _contract_option(instrument),
        "seriesKind": selected_kind,
        "priceBasis": SETTLEMENT_PRICE_BASIS,
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
            "notional": {"available": has_notional, "render": "line", "reason": notional_reason},
            "basis": {"available": False, "render": "line", "reason": _BASIS_UNAVAILABLE},
        },
        "points": points,
        "source": str(instrument.get("actualSource") or instrument.get("source") or ""),
        "updatedAt": instrument.get("lastBarAt"),
        "limitations": [
            "价格序列保留本地来源的 OHLC；持仓量为交易所公布的单边持仓量。",
            "名义持仓规模 = 交易所单边持仓量 × 结算价 × 同交易日交易乘数；不乘保证金率或双边系数。",
            *( [notional_reason] if notional_reason else []),
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
    source: str = "tdx",
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
    if source not in {"tdx", "official"}:
        raise HTTPException(status_code=422, detail="source must be tdx or official")
    if chart_id == PRODUCT_OPEN_INTEREST_CHART_ID and direction == STRUCTURE_DIRECTION_GROSS:
        formula_version = OFFICIAL_PRODUCT_OI_FORMULA_VERSION if source == "official" else STRUCTURE_FORMULA_VERSION
    elif chart_id == PRODUCT_NOTIONAL_CHART_ID and direction == STRUCTURE_DIRECTION_GROSS:
        formula_version = OFFICIAL_PRODUCT_NOTIONAL_FORMULA_VERSION if source == "official" else PRODUCT_NOTIONAL_FORMULA_VERSION
    elif chart_id == PRODUCT_VOLUME_CHART_ID and direction == STRUCTURE_DIRECTION_GROSS:
        formula_version = OFFICIAL_PRODUCT_VOLUME_FORMULA_VERSION
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
        coverage_formula_version = (
            formula_version if source == "official" and chart_id in {
                PRODUCT_OPEN_INTEREST_CHART_ID, PRODUCT_NOTIONAL_CHART_ID, PRODUCT_VOLUME_CHART_ID,
            }
            else PRODUCT_NOTIONAL_OFFICIAL_FORMULA_VERSION
        )
        official_input_coverage = (
            store.list_futures_structure_input_coverage(
                chart_id=chart_id,
                formula_version=coverage_formula_version,
            )
            if direction == STRUCTURE_DIRECTION_GROSS and (
                source == "official" and chart_id in {
                    PRODUCT_OPEN_INTEREST_CHART_ID, PRODUCT_NOTIONAL_CHART_ID, PRODUCT_VOLUME_CHART_ID,
                }
                or chart_id == PRODUCT_NOTIONAL_CHART_ID
            )
            else []
        )
    finally:
        store.close()
    if not all_rows:
        return _unavailable_structure(chart_id, direction, "期货结构 Gold 数据为空，请先运行 futures-structure 离线任务。")
    latest_day = (
        max([str(all_rows[-1]["trade_date"]), *(str(item["trade_date"]) for item in official_input_coverage)])
        if source == "official" else str(all_rows[-1]["trade_date"])
    )
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
    selected_coverage = [
        row for row in official_input_coverage
        if (effective_start is None or str(row["trade_date"]) >= effective_start)
        and (effective_end is None or str(row["trade_date"]) <= effective_end)
    ]
    return _structure_payload(baseline, rows, level, selected_coverage)


@router.get("/member-positions/overview")
def futures_member_position_overview(
    request: Request,
    trading_day: str | None = None,
    scope: str = "all",
) -> dict[str, Any]:
    """Rank all disclosed domestic futures products and members on one common day."""

    selected_day = _optional_day(trading_day, "trading_day")
    selected_scope = _member_scope(scope)
    commodity_only = selected_scope == "commodity"
    root = Path(request.app.state.data_root)
    if not (root / "catalog.duckdb").is_file():
        return _unavailable_member_overview("尚未采集交易所会员持仓排名。", selected_scope)
    store = MarketStore(root)
    try:
        latest_published_day = store.latest_futures_member_position_day(commodity_only=commodity_only)
        latest_observation_day = store.latest_futures_member_position_observation_day(commodity_only=commodity_only)
        day = selected_day.isoformat() if selected_day else latest_published_day
        if day is None:
            return _unavailable_member_overview("尚未采集国内期货会员持仓排名。", selected_scope)
        ranks = store.list_futures_member_position_ranks(trading_day=day, commodity_only=commodity_only)
        coverage_rows = store.list_futures_member_position_coverage(
            trading_day=day, commodity_only=commodity_only,
        )
        latest_observation_coverage = store.list_futures_member_position_coverage(
            trading_day=latest_observation_day, commodity_only=commodity_only,
        ) if latest_observation_day else []
    finally:
        store.close()
    ranks = _unique_published_ranks(ranks)
    product_values: dict[str, float] = defaultdict(float)
    member_values: dict[str, float] = defaultdict(float)
    member_names: dict[str, str] = {}
    member_exchanges: dict[str, set[str]] = defaultdict(set)
    product_contracts: dict[str, set[str]] = defaultdict(set)
    for row in ranks:
        product_key = f"{row['exchange']}.{row['product_code']}"
        member_key = str(row["member_key"])
        position = float(row["position"])
        product_values[product_key] += position
        member_values[member_key] += position
        member_names.setdefault(member_key, str(row["member_name"]))
        member_exchanges[member_key].add(str(row["exchange"]))
        product_contracts[product_key].add(str(row["contract_code"]))
    products = [
        {
            "productKey": key,
            "exchange": key.split(".", 1)[0],
            "productCode": key.split(".", 1)[1],
            "displayName": _product_display_name(key),
            "grossPublishedPosition": product_values[key],
            "contracts": sorted(product_contracts[key]),
        }
        for key in sorted(product_values, key=lambda key: (-product_values[key], key))
    ]
    members = [
        {
            "memberKey": key, "memberName": member_names[key],
            "grossPublishedPosition": member_values[key],
            "exchanges": sorted(member_exchanges[key]),
        }
        for key in sorted(member_values, key=lambda key: (-member_values[key], key))
    ]
    return {
        "available": bool(ranks), "scope": selected_scope, "tradingDay": day,
        "latestPublishedDay": latest_published_day,
        "latestObservationDay": latest_observation_day,
        "latestObservationCoverage": _member_position_coverage_payload(
            [], latest_observation_coverage, commodity_only=commodity_only,
        ),
        "defaultProductKey": products[0]["productKey"] if products else None,
        "defaultMemberKey": members[0]["memberKey"] if members else None,
        "products": products, "members": members,
        "coverage": _member_position_coverage_payload(
            ranks, coverage_rows, commodity_only=commodity_only,
        ),
        "limitations": [
            "排行仅按该交易日各交易所已公布的多头与空头持仓张数计算；未上榜席位不代表零持仓。",
        ],
    }


@router.get("/member-positions/history")
def futures_member_position_history(
    request: Request,
    exchange: str | None = None,
    product_key: str | None = None,
    product_code: str | None = None,
    contract_code: str | None = None,
    member_key: str | None = None,
    scope: str = "commodity",
) -> dict[str, Any]:
    """Published ranking history for one product identity or exact member name."""

    selected_scope = _member_scope(scope)
    scope_exchanges = _MEMBER_SCOPE_EXCHANGES[selected_scope]
    commodity_only = selected_scope == "commodity"
    selected_exchange = _optional_exchange(exchange)
    selected_product = _optional_contract_code(product_code, "product_code")
    selected_contract = _optional_contract_code(contract_code, "contract_code")
    selected_member = _optional_member_key(member_key)
    if product_key is not None:
        parts = product_key.strip().upper().split(".")
        if len(parts) != 2 or parts[0] not in scope_exchanges:
            raise HTTPException(status_code=422, detail=f"product_key exchange is outside scope={selected_scope}")
        parsed_product = _optional_contract_code(parts[1], "product_key")
        if selected_exchange is not None and selected_exchange != parts[0]:
            raise HTTPException(status_code=422, detail="exchange conflicts with product_key")
        if selected_product is not None and selected_product != parsed_product:
            raise HTTPException(status_code=422, detail="product_code conflicts with product_key")
        selected_exchange, selected_product = parts[0], parsed_product
    if selected_exchange is not None and selected_exchange not in scope_exchanges:
        raise HTTPException(status_code=422, detail=f"exchange is outside scope={selected_scope}")
    if selected_product is not None and selected_exchange is None:
        raise HTTPException(status_code=422, detail="product_code requires exchange or product_key")
    if (selected_product is None) == (selected_member is None):
        raise HTTPException(status_code=422, detail="select exactly one product_code or member_key")
    if selected_contract is not None and selected_product is None:
        raise HTTPException(status_code=422, detail="contract_code requires product_code")
    root = Path(request.app.state.data_root)
    if not (root / "catalog.duckdb").is_file():
        return {
            "available": False, "exchange": selected_exchange, "scope": selected_scope, "dates": [], "rows": [],
            "filters": {"productCode": selected_product, "contractCode": selected_contract, "memberKey": selected_member},
            "productKey": product_key,
            "sourceStatusByDay": {}, "limitations": ["尚未采集交易所会员持仓排名。"],
        }
    store = MarketStore(root)
    try:
        ranks = store.list_futures_member_position_ranks(
            exchange=selected_exchange,
            product_code=selected_product,
            contract_code=selected_contract,
            member_key=selected_member,
            commodity_only=commodity_only,
        )
        coverage = store.list_futures_member_position_coverage(
            exchange=selected_exchange, commodity_only=commodity_only,
        )
    finally:
        store.close()
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in _unique_published_ranks(ranks):
        by_day[str(row["trading_day"])].append(row)
    source_status: dict[str, str] = {}
    source_status_by_exchange: dict[str, dict[str, str]] = defaultdict(dict)
    source_is_direct: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in coverage:
        day = str(row["trading_day"])
        status = str(row["status"])
        venue = str(row["exchange"])
        previous = source_status_by_exchange[day].get(venue)
        direct = str(row.get("adapter") or row.get("source") or "").startswith("official-")
        if (
            previous is None
            or (direct and not source_is_direct[day].get(venue, False))
            or (direct == source_is_direct[day].get(venue, False) and status == "PASS")
        ):
            source_status_by_exchange[day][venue] = status
            source_is_direct[day][venue] = direct
    for day in by_day:
        source_status_by_exchange.setdefault(day, {})
    for day, venues in source_status_by_exchange.items():
        statuses = [venues.get(exchange) for exchange in (
            [selected_exchange] if selected_exchange else sorted(scope_exchanges)
        )]
        usable_statuses = {"PASS", "NO_PUBLICATION"}
        source_status[day] = (
            "NO_PUBLICATION" if statuses and all(status == "NO_PUBLICATION" for status in statuses)
            else "PASS" if statuses and all(status in usable_statuses for status in statuses)
            else "PARTIAL" if any(status in usable_statuses | {"PARTIAL"} for status in statuses)
            else "FAILED" if any(status == "FAILED" for status in statuses)
            else "LEGACY_UNVERIFIED" if by_day.get(day) else "NOT_COLLECTED"
        )
    dates = sorted(source_status)
    history_rows: list[dict[str, Any]] = []
    for day in sorted(by_day):
        payload = _member_position_payload(
            by_day[day], trading_day=day, latest_published_day=None,
            exchange=selected_exchange, contract_code=selected_contract,
            product_code=selected_product, member_key=selected_member,
            commodity_only=commodity_only, include_rows=True, coverage_records=[],
        )
        history_rows.extend({
            **row, "tradingDay": day,
            "productKey": f"{row['exchange']}.{row['productCode']}",
            "productName": futures_product_name(row["productCode"], row["exchange"])
            or row["productCode"],
            "productDisplayName": _product_display_name(
                f"{row['exchange']}.{row['productCode']}"
            ),
        } for row in payload["rows"])
    return {
        "available": bool(history_rows),
        "scope": selected_scope,
        "exchange": selected_exchange,
        "productKey": f"{selected_exchange}.{selected_product}" if selected_product else None,
        "filters": {
            "productCode": selected_product, "contractCode": selected_contract,
            "memberKey": selected_member,
        },
        "dates": dates,
        "rows": history_rows,
        "sourceStatusByDay": source_status,
        "sourceStatusByExchange": source_status_by_exchange,
        "limitations": [
            "仅包含交易所已公布的会员方向排名；未上榜、采集失败或未公布的方向保持缺失。",
            "多头净持仓与空头净持仓仅在同一合约席位的多空双方都已公布时计算。",
        ],
    }


@router.get("/member-positions")
def futures_member_positions(
    request: Request,
    trading_day: str | None = None,
    exchange: str | None = None,
    product_key: str | None = None,
    contract_code: str | None = None,
    product_code: str | None = None,
    member_key: str | None = None,
    commodity_only: bool = True,
    scope: str | None = None,
) -> dict[str, Any]:
    """Return published member ranking coverage for one futures trading day.

    The result intentionally represents an unpublished direction as ``null``:
    absence from a top-ranked list is not evidence of a zero position.
    """

    selected_scope = _member_scope(scope) if scope is not None else ("commodity" if commodity_only else "all")
    commodity_only = selected_scope == "commodity"
    scope_exchanges = _MEMBER_SCOPE_EXCHANGES[selected_scope]
    selected_exchange = _optional_exchange(exchange)
    if selected_exchange is not None and selected_exchange not in scope_exchanges:
        raise HTTPException(status_code=422, detail=f"exchange is outside scope={selected_scope}")
    selected_contract = _optional_contract_code(contract_code, "contract_code")
    selected_product = _optional_contract_code(product_code, "product_code")
    if product_key is not None:
        parts = product_key.strip().upper().split(".")
        if len(parts) != 2 or parts[0] not in scope_exchanges:
            raise HTTPException(status_code=422, detail=f"product_key exchange is outside scope={selected_scope}")
        keyed_product = _optional_contract_code(parts[1], "product_key")
        if selected_exchange is not None and selected_exchange != parts[0]:
            raise HTTPException(status_code=422, detail="exchange conflicts with product_key")
        if selected_product is not None and selected_product != keyed_product:
            raise HTTPException(status_code=422, detail="product_code conflicts with product_key")
        selected_exchange, selected_product = parts[0], keyed_product
    selected_member = _optional_member_key(member_key)
    if selected_member is not None and selected_exchange is None:
        raise HTTPException(status_code=422, detail="exchange is required with member_key")
    selected_day = _optional_day(trading_day, "trading_day")
    root = Path(request.app.state.data_root)
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return _unavailable_member_positions("尚未采集交易所会员持仓排名。", commodity_only=commodity_only)
    store = MarketStore(root)
    try:
        effective_day = selected_day.isoformat() if selected_day else store.latest_futures_member_position_observation_day(
            commodity_only=commodity_only
        )
        if effective_day is None:
            return _unavailable_member_positions("尚未采集交易所会员持仓排名。", commodity_only=commodity_only)
        latest_published_day = store.latest_futures_member_position_day(commodity_only=commodity_only)
        ranks = store.list_futures_member_position_ranks(
            trading_day=effective_day,
            exchange=selected_exchange,
            contract_code=selected_contract,
            product_code=selected_product,
            member_key=selected_member,
            commodity_only=commodity_only,
        )
        coverage_records = store.list_futures_member_position_coverage(
            trading_day=effective_day,
            commodity_only=commodity_only,
        )
    finally:
        store.close()
    return _member_position_payload(
        ranks,
        trading_day=effective_day,
        latest_published_day=latest_published_day,
        exchange=selected_exchange,
        contract_code=selected_contract,
        product_code=selected_product,
        member_key=selected_member,
        commodity_only=commodity_only,
        include_rows=selected_contract is not None or selected_product is not None or selected_member is not None,
        coverage_records=coverage_records,
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
    baseline: Mapping[str, Any], rows: list[Mapping[str, Any]], level: str,
    official_input_coverage: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    by_day: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_day[str(row["trade_date"])][str(row["member_key"])] = row
    official_by_day = {
        str(item["trade_date"]): item for item in (official_input_coverage or [])
    }
    dates = sorted(set(by_day) | set(official_by_day))
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
    coverage = []
    for day in dates:
        day_rows = by_day.get(day, {})
        official_row = official_by_day.get(day)
        input_count = (
            max(int(row["input_row_count"]) for row in day_rows.values())
            if day_rows else int(official_row.get("input_contract_count") or 0) if official_row else 0
        )
        missing_count = (
            max(int(row["missing_row_count"]) for row in day_rows.values())
            if day_rows else 0
        )
        coverage_payload = _official_input_coverage_payload(official_row)
        if official_row is not None:
            missing_count = max(missing_count, int(official_row.get("missing_exchange_count") or 0))
            quality_status = str(official_row.get("quality_status") or "PARTIAL")
        else:
            quality_status = "PARTIAL" if any(
                row["data_quality_status"] == "PARTIAL" for row in day_rows.values()
            ) else "PASS"
        coverage.append({
            "tradeDate": day, "inputRowCount": input_count,
            "missingRowCount": min(input_count, missing_count),
            "dataQualityStatus": quality_status, **coverage_payload,
        })
    is_ranked_product = str(baseline["chart_id"]) in {
        PRODUCT_OPEN_INTEREST_CHART_ID, PRODUCT_NOTIONAL_CHART_ID, PRODUCT_VOLUME_CHART_ID,
    }


    incomplete_days = {
        item["tradeDate"] for item in coverage if item["dataQualityStatus"] == "PARTIAL"
    } if is_ranked_product else set()
    totals = [
        None if day in incomplete_days else sum(float(row["value"]) for row in by_day[day].values())
        for day in dates
    ]
    unclassified_totals = [
        None if day in incomplete_days else sum(
            float(row["value"]) for key, row in by_day[day].items() if key not in known_keys
        )
        for day in dates
    ]
    keep_partial_lines = "official" in str(baseline["formula_version"])
    baseline_coverage = next(
        (item for item in coverage if item["tradeDate"] == str(baseline["baseline_day"])),
        None,
    )
    main_series = [_member_series(member, dates, by_day, incomplete_days, keep_partial_lines=keep_partial_lines) for member in primary]
    if other:
        main_series.append(_other_series(other, dates, by_day, incomplete_days, keep_partial_lines=keep_partial_lines))
    series = (
        [_member_series(member, dates, by_day, incomplete_days, keep_partial_lines=keep_partial_lines) for member in other]
        if level == "other" else main_series
    )
    chart_metadata = _structure_metadata(str(baseline["chart_id"]))
    return {
        "available": bool(dates),
        "chartId": baseline["chart_id"],
        "metric": chart_metadata["metric"],
        "direction": baseline["direction"],
        "unit": chart_metadata["unit"],
        "baselineDay": baseline["baseline_day"],
        "baselineQualityStatus": baseline_coverage["dataQualityStatus"] if baseline_coverage else None,
        "baselineVersion": baseline["baseline_version"],
        "threshold": None if is_ranked_product else baseline["threshold"],
        "rankLimit": PRODUCT_STRUCTURE_RANK_LIMIT if is_ranked_product else None,
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
            chart_metadata["formula"],
            "主图成员和堆叠顺序固定于基准日；基准日后出现的新成员单列为未分类。",
            *(["合约输入不完整的交易日保留为图表缺口，避免部分合计造成虚假断崖。"] if is_ranked_product else []),
            *([f"没有完整官方日；前40名暂按部分基准日 {baseline['baseline_day']} 的已计算品种排序，不能将该日解释为全市场总量。"] if keep_partial_lines and baseline_coverage and baseline_coverage["dataQualityStatus"] == "PARTIAL" else []),
        ],
    }


def _official_input_coverage_payload(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose v4's audited missing-input reasons beside the active v3 chart."""

    if row is None:
        return {}
    try:
        missing_products = json.loads(str(row.get("missing_products_json") or "[]"))
        missing_contracts = json.loads(str(row.get("missing_contracts_json") or "[]"))
        missing_exchanges = json.loads(str(row.get("missing_exchanges_json") or "[]"))
    except (TypeError, json.JSONDecodeError):
        missing_products, missing_contracts, missing_exchanges = [], [], []
    product_keys = [str(key) for key in missing_products if isinstance(key, str)]
    contract_keys = [str(key) for key in missing_contracts if isinstance(key, str)]
    return {
        "officialInputRowCount": int(row.get("input_contract_count") or 0),
        "missingSettlementCount": int(row.get("missing_settlement_count") or 0),
        "missingOpenInterestCount": int(row.get("missing_open_interest_count") or 0),
        "missingMultiplierCount": int(row.get("missing_multiplier_count") or 0),
        "missingVolumeCount": int(row.get("missing_volume_count") or 0),
        "missingExchangeCount": int(row.get("missing_exchange_count") or 0),
        "missingExchanges": [str(item) for item in missing_exchanges if isinstance(item, str)],
        "unverifiedVolumeCount": int(row.get("unverified_volume_count") or 0),
        "unverifiedOpenInterestCount": int(row.get("unverified_open_interest_count") or 0),
        "missingProducts": [_product_display_name(key) for key in product_keys],
        "missingContracts": contract_keys,
    }


def _structure_metadata(chart_id: str) -> dict[str, str]:
    if chart_id == PRODUCT_NOTIONAL_CHART_ID:
        return {
            "metric": "notionalOpenInterest",
            "unit": "CNY",
            "formula": "统计国内期货（含中金所）有效月份合约的单边名义持仓规模：结算价 × 交易乘数 × 交易所单边持仓量；不乘保证金率或双边系数。",
        }
    if chart_id == MEMBER_OPEN_INTEREST_CHART_ID:
        return {
            "metric": "memberOpenInterest",
            "unit": "contracts",
            "formula": "仅统计交易所已公布席位排名覆盖范围内的商品期货单边持仓；不是会员完整持仓。",
        }
    if chart_id == PRODUCT_VOLUME_CHART_ID:
        return {
            "metric": "tradedVolume",
            "unit": "contracts",
            "formula": "统计国内期货（含中金所）交易所官方逐合约日行情成交量（手）；仅累计单边口径已核验的数据，缺源或未核验来源保持缺口。",
        }
    return {
        "metric": "openInterest",
        "unit": "contracts",
        "formula": "统计国内期货（含中金所）有效月份合约的交易所单边持仓量。",
    }


def _member_series(
    member: Mapping[str, Any], dates: list[str], by_day: Mapping[str, Mapping[str, Mapping[str, Any]]],
    incomplete_days: set[str] | None = None, *, keep_partial_lines: bool = False,
) -> dict[str, Any]:
    key = str(member["memberKey"])
    raw_values = [float(by_day[day][key]["value"]) if key in by_day.get(day, {}) else None for day in dates]
    return {
        "memberKey": key,
        "memberName": member["memberName"],
        "values": [None if day in (incomplete_days or set()) else value for day, value in zip(dates, raw_values, strict=True)],
        "lineValues": raw_values if keep_partial_lines else [None if day in (incomplete_days or set()) else value for day, value in zip(dates, raw_values, strict=True)],
    }


def _other_series(
    members: list[Mapping[str, Any]], dates: list[str], by_day: Mapping[str, Mapping[str, Mapping[str, Any]]],
    incomplete_days: set[str] | None = None, *, keep_partial_lines: bool = False,
) -> dict[str, Any]:
    keys = {str(member["memberKey"]) for member in members}
    raw_values = [
        sum(float(by_day.get(day, {})[key]["value"]) for key in keys if key in by_day.get(day, {}))
        if any(key in by_day.get(day, {}) for key in keys) else None
        for day in dates
    ]
    return {
        "memberKey": "OTHER",
        "memberName": "其他",
        "memberCount": len(keys),
        "values": [None if day in (incomplete_days or set()) else value for day, value in zip(dates, raw_values, strict=True)],
        "lineValues": raw_values if keep_partial_lines else [None if day in (incomplete_days or set()) else value for day, value in zip(dates, raw_values, strict=True)],
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
        "rankLimit": None,
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
    latest_published_day: str | None,
    exchange: str | None,
    contract_code: str | None,
    product_code: str | None,
    member_key: str | None,
    commodity_only: bool,
    include_rows: bool,
    coverage_records: list[Mapping[str, Any]],
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
                "publishers": set(),
                "sourceUrls": set(),
                "adapters": set(),
                "legacyAdapters": set(),
                "payloadSha256s": set(),
                "qualityStatuses": set(),
                "retrievedAt": set(),
            },
        )
        prefix = "long" if item["side"] == "LONG" else "short"
        row[f"{prefix}Position"] = float(item["position"])
        row[f"{prefix}PositionChange"] = (
            float(item["position_change"]) if item["position_change"] is not None else None
        )
        row[f"{prefix}Rank"] = int(item["rank"])
        row["sources"].add(str(item["source"]))
        row["publishers"].add(str(item.get("publisher") or current_exchange))
        if item.get("source_url"):
            row["sourceUrls"].add(str(item["source_url"]))
        row["adapters"].add(str(item.get("adapter") or item["source"]))
        if item.get("payload_sha256"):
            row["payloadSha256s"].add(str(item["payload_sha256"]))
        if item.get("quality_status"):
            row["qualityStatuses"].add(str(item["quality_status"]))
        if item.get("retrieved_at"):
            row["retrievedAt"].add(str(item["retrieved_at"]))
        if item.get("legacy_adapter") or str(item["source"]).startswith("akshare-"):
            row["legacyAdapters"].add(str(item.get("legacy_adapter") or item["source"]))
    rows: list[dict[str, Any]] = []
    for row in groups.values():
        long_position = row["longPosition"]
        short_position = row["shortPosition"]
        net = long_position - short_position if long_position is not None and short_position is not None else None
        long_change = row["longPositionChange"]
        short_change = row["shortPositionChange"]
        prior_long = long_position - long_change if long_position is not None and long_change is not None else None
        prior_short = short_position - short_change if short_position is not None and short_change is not None else None
        prior_net = (
            prior_long - prior_short
            if prior_long is not None and prior_short is not None
            and prior_long >= 0 and prior_short >= 0 else None
        )
        rows.append(
            {
                **{key: value for key, value in row.items() if key not in {
                    "sources", "publishers", "sourceUrls", "adapters", "legacyAdapters",
                    "payloadSha256s", "qualityStatuses", "retrievedAt",
                }},
                "netPosition": net,
                "netLongPosition": max(net, 0) if net is not None else None,
                "netShortPosition": max(-net, 0) if net is not None else None,
                "netLongPositionChange": (
                    max(net, 0) - max(prior_net, 0)
                    if net is not None and prior_net is not None else None
                ),
                "netShortPositionChange": (
                    max(-net, 0) - max(-prior_net, 0)
                    if net is not None and prior_net is not None else None
                ),
                "sources": sorted(row["sources"]),
                "publishers": sorted(row["publishers"]),
                "sourceUrls": sorted(row["sourceUrls"]),
                "adapters": sorted(row["adapters"]),
                "legacyAdapters": sorted(row["legacyAdapters"]),
                "payloadSha256s": sorted(row["payloadSha256s"]),
                "qualityStatuses": sorted(row["qualityStatuses"]),
                "retrievedAt": max(row["retrievedAt"], default=None),
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
    member_options = [
        {"exchange": item[0], "memberKey": item[1], "memberName": item[2]}
        for item in sorted({
            (str(row["exchange"]), str(row["member_key"]), str(row["member_name"]))
            for row in ranks
        })
    ]
    exchanges = sorted({str(item["exchange"]) for item in ranks})
    coverage = _member_position_coverage_payload(
        ranks,
        coverage_records,
        commodity_only=commodity_only,
    )
    return {
        "available": bool(ranks),
        "tradingDay": trading_day,
        "latestPublishedDay": latest_published_day,
        "filters": {
            "exchange": exchange,
            "contractCode": contract_code,
            "productCode": product_code,
            "memberKey": member_key,
            "commodityOnly": commodity_only,
        },
        "contracts": contract_options,
        "members": member_options,
        "rows": rows if include_rows else [],
        "coverage": {
            "publishedDirectionRankCount": len(ranks),
            "memberCount": len(rows),
            "exchangeCount": len(exchanges),
            "exchanges": exchanges,
            **coverage,
        },
        "limitations": [
            "仅统计交易所实际公布的会员方向排名，覆盖总计不是全市场会员全部持仓。",
            "会员未出现在另一方向排名时以 null 表示未公布，不能据此推断持仓为 0 或计算净持仓。",
            "默认排除中金所金融期货；可通过 commodity_only=false 查看已采集的全部交易所。",
            *( ["逐交易所采集覆盖未完整通过，失败详情保留在“来源采集覆盖”中；不得将已公布排名外推为全市场会员持仓。"] if not coverage["isComplete"] else []),
            *( [] if include_rows else ["请选择品种、具体月份合约或交易所席位后再读取排名明细，避免默认传输全市场大字段。"]),
        ],
    }


def _unique_published_ranks(ranks: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Prefer a verified direct observation over a legacy wrapper duplicate."""

    selected: dict[tuple[str, str, str, str, str], Mapping[str, Any]] = {}
    for row in ranks:
        try:
            day = date.fromisoformat(str(row["trading_day"]))
        except ValueError:
            continue
        delivery = contract_delivery_month(
            str(row["contract_code"]), str(row["exchange"]),
            reference_day=day, last_trading_day=day,
        )
        if delivery is None or delivery < day.replace(day=1):
            continue
        key = (
            str(row["trading_day"]), str(row["exchange"]),
            str(row["contract_code"]), str(row["side"]), str(row["member_key"]),
        )
        previous = selected.get(key)
        priority = (
            str(row.get("quality_status") or "") == "PASS",
            not str(row.get("source") or "").startswith("akshare-"),
            str(row.get("retrieved_at") or row.get("collected_at") or ""),
        )
        previous_priority = (
            str(previous.get("quality_status") or "") == "PASS",
            not str(previous.get("source") or "").startswith("akshare-"),
            str(previous.get("retrieved_at") or previous.get("collected_at") or ""),
        ) if previous is not None else None
        if previous_priority is None or priority > previous_priority:
            selected[key] = row
    return [selected[key] for key in sorted(selected)]


def _product_display_name(product_key: str) -> str:
    exchange, product = product_key.split(".", 1)
    return f"{futures_product_name(product, exchange) or product}（{product}）"


def _unavailable_member_overview(limitation: str, scope: str = "all") -> dict[str, Any]:
    exchanges = _MEMBER_SCOPE_EXCHANGES[_member_scope(scope)]
    return {
        "available": False, "scope": scope, "tradingDay": None, "latestPublishedDay": None,
        "latestObservationDay": None, "latestObservationCoverage": None,
        "defaultProductKey": None, "defaultMemberKey": None,
        "products": [], "members": [],
        "coverage": {
            "exchangeCoverage": [], "missingExchanges": sorted(exchanges),
            "isComplete": False, "dataQualityStatus": "UNAVAILABLE",
        },
        "limitations": [limitation],
    }


def _member_position_coverage_payload(
    ranks: list[Mapping[str, Any]],
    coverage_records: list[Mapping[str, Any]],
    *,
    commodity_only: bool,
) -> dict[str, Any]:
    """Expose source results independently from the rank rows they produced."""

    expected = tuple(sorted(COMMODITY_EXCHANGES if commodity_only else ALL_EXCHANGES))
    rank_exchanges = {str(item["exchange"]).upper() for item in ranks}
    by_exchange: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in coverage_records:
        exchange = str(row.get("exchange") or "").upper()
        if exchange in expected:
            by_exchange[exchange].append(row)
    exchange_coverage: list[dict[str, Any]] = []
    for exchange in expected:
        records = by_exchange[exchange]
        if records:
            direct_records = [
                item for item in records
                if str(item.get("adapter") or item.get("source") or "").startswith("official-")
            ]
            effective_records = direct_records or records
            statuses = {str(item.get("status") or "").upper() for item in effective_records}
            if "PASS" in statuses:
                status = "PARTIAL" if statuses & {"PARTIAL", "FAILED", "UNSUPPORTED"} else "PASS"
            elif statuses and statuses <= {"NO_PUBLICATION"}:
                status = "NO_PUBLICATION"
            elif statuses == {"UNSUPPORTED"}:
                status = "UNSUPPORTED"
            elif "PARTIAL" in statuses:
                status = "PARTIAL"
            else:
                status = "FAILED"
            errors = sorted({str(item["error"]) for item in effective_records if item.get("error")})
            quality_statuses = sorted({
                str(item.get("quality_status") or "UNKNOWN") for item in effective_records
            })
            exchange_coverage.append(
                {
                    "exchange": exchange,
                    "status": status,
                    "contractCount": max(int(item["contract_count"]) for item in effective_records),
                    "recordCount": max(int(item["record_count"]) for item in effective_records),
                    "sources": sorted({str(item["source"]) for item in records}),
                    "publishers": sorted({str(item.get("publisher") or exchange) for item in records}),
                    "adapters": sorted({str(item.get("adapter") or item["source"]) for item in records}),
                    "sourceUrls": sorted({str(item["source_url"]) for item in records if item.get("source_url")}),
                    "payloadSha256s": sorted({str(item["payload_sha256"]) for item in records if item.get("payload_sha256")}),
                    "qualityStatuses": quality_statuses,
                    "retrievedAt": max((str(item.get("retrieved_at") or "") for item in effective_records), default=None),
                    "qualityStatus": "；".join(quality_statuses),
                    "error": "；".join(errors) or None,
                    "collectedAt": max(str(item["collected_at"]) for item in records),
                }
            )
            continue
        # Old databases may contain normalised ranking facts without the
        # probe result.  Preserve the facts but never retroactively call their
        # exchange coverage PASS.
        exchange_coverage.append(
            {
                "exchange": exchange,
                "status": "LEGACY_UNVERIFIED" if exchange in rank_exchanges else "NOT_COLLECTED",
                "contractCount": 0,
                "recordCount": 0,
                "sources": [],
                "publishers": [exchange],
                "adapters": [],
                "sourceUrls": [],
                "payloadSha256s": [],
                "qualityStatuses": [],
                "retrievedAt": None,
                "qualityStatus": "LEGACY_UNVERIFIED" if exchange in rank_exchanges else "NOT_COLLECTED",
                "error": "历史排名未保留逐交易所采集覆盖。" if exchange in rank_exchanges else "本交易日未记录该交易所采集结果。",
                "collectedAt": None,
            }
        )
    missing_exchanges = [
        item["exchange"] for item in exchange_coverage
        if item["status"] not in {"PASS", "NO_PUBLICATION"}
    ]
    return {
        "exchangeCoverage": exchange_coverage,
        "missingExchanges": missing_exchanges,
        "isComplete": not missing_exchanges,
        "dataQualityStatus": "PASS" if not missing_exchanges else "PARTIAL" if ranks or coverage_records else "UNAVAILABLE",
    }


def _unavailable_member_positions(limitation: str, *, commodity_only: bool = True) -> dict[str, Any]:
    return {
        "available": False,
        "tradingDay": None,
        "latestPublishedDay": None,
        "filters": {"exchange": None, "contractCode": None, "productCode": None, "memberKey": None, "commodityOnly": True},
        "contracts": [],
        "members": [],
        "rows": [],
        "coverage": {
            "publishedDirectionRankCount": 0,
            "memberCount": 0,
            "exchangeCount": 0,
            "exchanges": [],
            "exchangeCoverage": [],
            "missingExchanges": sorted(COMMODITY_EXCHANGES if commodity_only else ALL_EXCHANGES),
            "isComplete": False,
            "dataQualityStatus": "UNAVAILABLE",
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


def _contract_series_point(
    bar: Mapping[str, Any],
    *,
    exchange: str,
    product: str,
    series_kind: str,
    rule_book: RuleBook,
) -> dict[str, Any]:
    """Turn one local daily bar into the fixed four-series response shape."""

    trading_day = str(bar.get("trading_day") or bar.get("trading_date") or bar.get("bar_open_time") or "")[:10]
    settlement = _finite_number(bar.get("settlement"))
    open_interest = _finite_number(_first_present(bar, "open_interest", "openInterest"))
    multiplier = rule_book.resolve_multiplier(trading_day, exchange, product) if series_kind == "CONTRACT" else None
    notional = (
        settlement * multiplier * open_interest
        if settlement is not None and multiplier is not None and open_interest is not None and open_interest >= 0
        else None
    )
    notional_reason = (
        None if notional is not None
        else _WEIGHTED_NOTIONAL_UNAVAILABLE if series_kind == "WEIGHTED"
        else _NOTIONAL_UNAVAILABLE
    )
    return {
        "tradingDay": trading_day,
        "open": _finite_number(bar.get("open")),
        "high": _finite_number(_first_present(bar, "high", "highest")),
        "low": _finite_number(_first_present(bar, "low", "lowest")),
        "close": _finite_number(bar.get("close")),
        "settlement": settlement,
        "openInterest": open_interest,
        "contractMultiplier": multiplier,
        "notionalRmb": notional,
        "basisRmb": None,
        "basisPercent": None,
        "unavailable": {"notional": notional_reason, "basis": _BASIS_UNAVAILABLE},
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


def _member_scope(value: str) -> str:
    scope = str(value or "").strip().lower()
    if scope not in _MEMBER_SCOPE_EXCHANGES:
        raise HTTPException(status_code=422, detail="scope must be 'commodity' or 'all'")
    return scope


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


def _optional_member_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip().casefold()
    if not key or len(key) > 120 or any(ord(char) < 32 for char in key):
        raise HTTPException(status_code=422, detail="member_key must be a non-empty published member key")
    return key


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
