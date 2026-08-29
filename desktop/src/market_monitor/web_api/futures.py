"""Read-only API for precomputed Chinese commodity-futures heat Gold rows."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request

from market_monitor.futures_heat import load_long_short_heat_config
from market_monitor.storage import MarketStore


router = APIRouter(prefix="/api/futures", tags=["futures"])


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


def _optional_day(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD") from error


__all__ = ("router",)
