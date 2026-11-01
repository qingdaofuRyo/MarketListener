"""Read-only customer views of recorded disclosure leads and search-source gaps."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request

from market_monitor.cninfo_disclosure import KEYWORDS, PDF_ORIGIN, RISK_SIGNAL_TERMS, SCHEMA, STATE_FILE


router = APIRouter(prefix="/api/data", tags=["data-disclosure-watch"])


@router.get("/disclosure-watch")
def disclosure_watch(
    request: Request, keyword: str | None = None,
    start_day: str | None = Query(default=None, alias="startDay"),
    end_day: str | None = Query(default=None, alias="endDay"),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    if keyword is not None and keyword not in (*KEYWORDS, *RISK_SIGNAL_TERMS):
        raise HTTPException(422, "unknown disclosure keyword")
    try:
        start = date.fromisoformat(start_day).isoformat() if start_day else None
        end = date.fromisoformat(end_day).isoformat() if end_day else None
    except ValueError as error:
        raise HTTPException(422, "invalid disclosure date") from error
    if start and end and start > end:
        raise HTTPException(422, "startDay must not be after endDay")
    path = Path(request.app.state.data_root) / STATE_FILE
    snapshot_available = path.is_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"schema": SCHEMA, "items": []}
    except (OSError, ValueError) as error:
        raise HTTPException(503, "disclosure snapshot cannot be read") from error
    if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("items"), list)):
        raise HTTPException(503, "disclosure snapshot schema changed")
    selected = []
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        pdf = str(item.get("pdfUrl") or "")
        parsed = urlparse(pdf)
        if parsed.scheme != "https" or parsed.netloc != "static.cninfo.com.cn" or not pdf.startswith(PDF_ORIGIN):
            continue
        if item.get("matchStatus") != "KEYWORD_HIT_PENDING_REVIEW":
            continue
        if item.get("keyword") not in (*KEYWORDS, *RISK_SIGNAL_TERMS):
            continue
        day = str(item.get("publicationDay") or "")
        if (keyword and item.get("keyword") != keyword or start and day < start or end and day > end):
            continue
        selected.append({key: item.get(key) for key in (
            "announcementId", "securityCode", "securityName", "title", "publishedAt",
            "publicationDay", "pdfUrl", "pageColumn", "keyword", "matchStatus",
            "fetchedAt",
        )})
        selected[-1]["titleContainsKeyword"] = str(item["keyword"]) in str(item.get("title") or "")
        selected[-1]["reviewedRiskStatus"] = None
    selected.sort(key=lambda item: (str(item["publishedAt"]), str(item["announcementId"])), reverse=True)
    return {"available": snapshot_available, "items": selected[:limit], "total": len(selected),
            "keywords": list(KEYWORDS), "riskSignalTerms": list(RISK_SIGNAL_TERMS),
            "source": "巨潮资讯官方公告检索",
            "lastFetchedAt": max((str(item.get("fetchedAt") or "") for item in selected), default=None),
            "limitations": ["检索结果只用于待核查线索；titleContainsKeyword 指明标题是否直接包含该词。"
                            "检索可能命中正文，不表示公司当前停牌、交易异常或退市。"
                            "须阅读原公告及其生效日期，不能据此自动判定新规风险。"]}


@router.get("/search-interest-sources")
def search_interest_sources() -> dict[str, Any]:
    """Expose honest source capability instead of fabricating daily heat."""

    terms = ["炒股", "股票", "基金", "期货", "开户"]
    return {"terms": terms, "series": [], "sources": [
        {"id": "baidu-index", "name": "百度指数", "status": "UNAVAILABLE_FINANCIAL_TERMS",
         "reason": "官方帮助说明金融类关键词暂不提供，且未开放API；不记录伪每日值。",
         "url": "https://index.baidu.com/Helper"},
        {"id": "douyin-index", "name": "抖音指数（原巨量算数）", "status": "NO_VERIFIED_PUBLIC_DAILY_SERIES",
         "reason": "公开页未提供免登录、可重复取得的逐日数值；同一平台不重复计作两条来源。",
         "url": "https://trendinsight.oceanengine.com/arithmetic-index/hot"},
    ]}


@router.get("/risk-signal-categories")
def risk_signal_categories() -> dict[str, Any]:
    """Describe review queues without calculating legal status from keywords."""

    return {"status": "CANDIDATE_ONLY", "categories": [
        {"id": "profit-revenue", "name": "净利润与收入风险线索",
         "terms": ["退市风险警示", "可能被实施退市风险警示"],
         "requiredEvidence": "经审计年度财务数据、扣除后营业收入、上市板块和适用规则；公告标题不能代替财务判断。"},
        {"id": "net-assets", "name": "净资产风险线索", "terms": ["净资产为负"],
         "requiredEvidence": "经审计期末净资产、上市板块和适用规则。"},
        {"id": "investigation", "name": "立案调查与重大违法线索",
         "terms": ["立案告知书", "行政处罚事先告知书"],
         "requiredEvidence": "监管文书正文、送达和生效进度；立案不等于已实施退市风险警示。"},
        {"id": "market-cap", "name": "市值风险线索", "terms": ["市值低于", "终止上市"],
         "requiredEvidence": "对应板块逐交易日收盘总市值、连续天数、停牌日与适用规则。"},
    ], "ruleSources": [
        {"board": "沪市主板", "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml"},
        {"board": "科创板", "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/staripo/c/c_20260424_10816592.shtml"},
        {"board": "深市主板", "url": "https://docs.static.szse.cn/www/lawrules/rule/allrules/bussiness/W020260424747613955674.pdf"},
        {"board": "创业板", "url": "https://docs.static.szse.cn/www/lawrules/rule/stock/W020260424688875101057.pdf"},
    ], "limitations": ["不同上市板块的财务、市值阈值和连续天数不同；"
                        "现阶段只记录巨潮公告检索结果，不生成风险个股名单或法律结论。"]}


__all__ = ("router",)
