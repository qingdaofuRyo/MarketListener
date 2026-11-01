"""Independent, fail-closed quantitative research diagnostics.

These tools describe local price series. They never stand in for a report's
strategy results or send an order, and they do not use the futures product
weighted series as a tradeable contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .research_publication import METHOD_CATEGORIES, read_publication
from .a_share_calendar import is_verified_a_share_trading_day
from .web_api.common import read_bars

ENGINE_VERSION = "quant-research-v1"
_BUILTINS = (
    {"id": "price-distribution", "version": 1, "name": "日度价格变动分布", "kind": "工具",
     "category": "统计与风险", "summary": "按所选区间计算本机日线相邻收盘价的变动分布、波动率和回撤。",
     "formula": "rₜ = closeₜ / closeₜ₋₁ − 1；σ = std(r) × √252",
     "requiredData": ["连续、质量合格的日线收盘价"], "assumptions": ["用户明确指定起止日期", "仅统计价格变化"],
     "limitations": ["未复权股票存在除权影响", "期货品种加权价格不代表可成交合约", "不含费用、股息或实盘收益"],
     "blockReasons": [], "implementationStatus": "RUNNABLE"},
    {"id": "rolling-volatility", "version": 1, "name": "滚动历史波动率", "kind": "工具",
     "category": "统计与风险", "summary": "用明确指定的交易日窗口观察本机日线价格变动的年化波动。",
     "formula": "σₜ(w) = std(rₜ₋w₊₁ … rₜ) × √252",
     "requiredData": ["连续、质量合格的日线收盘价", "用户指定窗口交易日数"],
     "assumptions": ["用户明确指定起止日期及窗口", "年化系数为252，仅作描述性统计"],
     "limitations": ["不预测未来波动", "未复权股票存在除权影响", "不构成期权隐含波动率"],
     "blockReasons": [], "implementationStatus": "RUNNABLE"},
)

_PUBLIC_METHODS = (
    {"id": "public-cross-sectional-momentum", "version": 1, "name": "横截面动量", "kind": "策略",
     "category": "因子选股", "summary": "按同一形成期的历史收益对标的排序，比较不同组在后续期间的表现。",
     "formula": "形成期收益 = P结束 / P起始 − 1；同日按形成期收益分组",
     "requiredData": ["带时点成员的证券池", "含退市与复权处理的历史价格", "调仓日可成交性及成本"],
     "assumptions": ["形成期、排序分组、持有期和调仓时点须事先定义", "信号生成后执行"],
     "limitations": ["动量可能出现急剧反转", "历史研究结果不代表未来或其他市场表现", "卖空及涨跌停约束因市场而异"],
     "blockReasons": ["未配置可核验的时点证券池、交易约束、成本和执行规则"],
     "implementationStatus": "EXPLAINED", "origin": "PUBLIC_RESEARCH_OVERVIEW"},
    {"id": "public-time-split-validation", "version": 1, "name": "按时间划分样本与样本外检验", "kind": "方法",
     "category": "回测评价与归因", "summary": "按时间先后划分研究、参数选择与最终检验区间，检查模型是否依赖历史拟合。",
     "formula": None, "requiredData": ["有时间戳的数据及其当时可用版本", "事先定义的训练、验证和测试区间"],
     "assumptions": ["测试区间在研究选择阶段保持独立", "所有输入满足信号时点可得"],
     "limitations": ["一次样本外通过不保证未来稳健", "反复查看测试集仍会产生选择偏差"],
     "blockReasons": ["尚未定义数据切分、候选选择和检验流程"],
     "implementationStatus": "EXPLAINED", "origin": "PUBLIC_RESEARCH_OVERVIEW"},
    {"id": "public-multiple-testing", "version": 1, "name": "多重检验与数据挖掘偏差", "kind": "方法",
     "category": "回测评价与归因", "summary": "同时尝试大量因子或参数时，记录全部尝试并修正对最佳结果的显著性判断。",
     "formula": None, "requiredData": ["完整候选集和试验记录", "定义明确的统计量及检验方案"],
     "assumptions": ["检验设计和筛选过程可追溯"],
     "limitations": ["显著性不等于可交易收益", "候选之间相关性影响检验方法选择"],
     "blockReasons": ["缺少完整候选历史和具体多重检验方案"],
     "implementationStatus": "EXPLAINED", "origin": "PUBLIC_RESEARCH_OVERVIEW"},
    {"id": "public-transaction-costs", "version": 1, "name": "交易成本与换手评估", "kind": "方法",
     "category": "回测评价与归因", "summary": "将调仓产生的费用、价差、滑点及市场冲击与毛收益分开评估。",
     "formula": "净收益 = 毛收益 − 费用 − 价差与滑点 − 其他执行成本",
     "requiredData": ["逐次调仓及成交假设", "与标的和日期对应的费用、价差和流动性数据"],
     "assumptions": ["成交时点、数量及价格规则在实验前确定"],
     "limitations": ["成本随成交规模和市场状态变化", "缺少成交明细时冲击估计存在较大不确定性"],
     "blockReasons": ["缺少逐笔执行规则和时点成本数据"],
     "implementationStatus": "EXPLAINED", "origin": "PUBLIC_RESEARCH_OVERVIEW"},
)


def methods(data_root: Path) -> dict[str, Any]:
    publication = read_publication(data_root, "methods")
    return {"categories": list(METHOD_CATEGORIES),
            "methods": [*list(_BUILTINS), *list(_PUBLIC_METHODS), *publication.get("methods", [])],
            "version": publication.get("version"), "status": publication.get("status", "PUBLISHED")}


def method(data_root: Path, method_id: str) -> dict[str, Any] | None:
    return next((item for item in methods(data_root)["methods"] if item["id"] == method_id), None)


def _day(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("请选择有效的起止日期")
    return value


def _price_bars(data_root: Path, instrument_id: str, start: str, end: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not instrument_id.strip() or len(instrument_id) > 160:
        raise ValueError("标的代码无效")
    start = _day(start)
    end = _day(end)
    if start >= end:
        raise ValueError("结束日期必须晚于开始日期")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if end >= today:
        raise ValueError("结束日期必须早于北京时间今天，避免使用未收盘行情")
    source = read_bars(data_root, instrument_id, period="1d", limit=100_000)
    bars = [bar for bar in source if start <= str(bar.get("bar_open_time") or "")[:10] <= end]
    reasons: list[str] = []
    a_share = instrument_id.startswith(("CN.SSE.STOCK.", "CN.SZSE.STOCK.", "CN.BSE.STOCK.",
                                         "CN.SSE.ETF.", "CN.SZSE.ETF."))
    if not a_share:
        reasons.append("当前价格统计只开放已核验交易日历的A股与ETF标的")
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first.year != 2026 or last.year != 2026:
        reasons.append("所选区间缺完整、逐年核验的A股交易日历")
    if not bars:
        reasons.append("所选区间没有本机日线数据")
        return [], reasons
    days = [str(bar.get("bar_open_time") or "")[:10] for bar in bars]
    if days != sorted(set(days)):
        reasons.append("日线日期重复或顺序错误")
    if a_share and first.year == last.year == 2026:
        expected = set()
        cursor = first
        while cursor <= last:
            if is_verified_a_share_trading_day(cursor):
                expected.add(cursor.isoformat())
            cursor += timedelta(days=1)
        if set(days) != expected:
            reasons.append("所选区间存在交易日缺口或额外日期")
    if any(str(bar.get("series_kind") or bar.get("seriesKind") or "").upper()
           in {"MAIN", "SECONDARY", "WEIGHTED"} for bar in bars):
        reasons.append("期货主连、次连或品种加权序列不是可成交的具体合约")
    if any(str(bar.get("quality_status") or "").upper() not in {"OK", "PASS"} for bar in bars):
        reasons.append("部分行情未通过 Silver 质量门禁")
    for bar in bars:
        close = bar.get("close")
        if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(close) or close <= 0:
            reasons.append("收盘价缺失或无效")
            break
    if len(bars) < 21:
        reasons.append("至少需要 21 个合格交易日")
    return bars, list(dict.fromkeys(reasons))


def readiness(data_root: Path, method_id: str, instrument_id: str, start: str, end: str,
              *, window: int | None = None) -> dict[str, Any]:
    definition = method(data_root, method_id)
    if definition is None:
        raise KeyError(method_id)
    if definition["implementationStatus"] != "RUNNABLE":
        return {"ready": False, "reasons": definition.get("blockReasons") or ["方法尚未通过公式、数据和实现核验"],
                "methodId": method_id, "instrumentId": instrument_id}
    bars, reasons = _price_bars(data_root, instrument_id, start, end)
    if method_id == "rolling-volatility":
        if window is None or window < 2 or window > 252:
            reasons.append("请明确指定 2–252 个交易日的窗口")
        elif len(bars) <= window:
            reasons.append("交易日数量不足以计算所选窗口")
    return {"ready": not reasons, "reasons": reasons, "methodId": method_id,
            "instrumentId": instrument_id, "barCount": len(bars),
            "firstDay": str(bars[0].get("bar_open_time"))[:10] if bars else None,
            "lastDay": str(bars[-1].get("bar_open_time"))[:10] if bars else None,
            "purpose": "PRICE_DIAGNOSTIC_ONLY"}


def _run_path(data_root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"qr_[0-9a-f]{24}", run_id):
        raise ValueError("Invalid research run identifier")
    return Path(data_root) / "research" / "runs" / f"{run_id}.json"


def run_diagnostic(data_root: Path, method_id: str, instrument_id: str, start: str, end: str,
                   *, window: int | None = None) -> dict[str, Any]:
    gate = readiness(data_root, method_id, instrument_id, start, end, window=window)
    if not gate["ready"]:
        raise ValueError("；".join(gate["reasons"]))
    bars, reasons = _price_bars(data_root, instrument_id, start, end)
    if reasons:
        raise ValueError("；".join(reasons))
    if method_id == "rolling-volatility" and (window is None or window < 2 or window > 252 or len(bars) <= window):
        raise ValueError("当前数据不足以计算所选窗口")
    if method_id not in {"price-distribution", "rolling-volatility"}:
        raise ValueError("该方法尚未实现本地计算")
    closes = [float(bar["close"]) for bar in bars]
    dates = [str(bar["bar_open_time"])[:10] for bar in bars]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    observations: list[dict[str, float | str]] = []
    if method_id == "rolling-volatility":
        assert window is not None
        for index in range(window - 1, len(returns)):
            sample = returns[index - window + 1:index + 1]
            observations.append({"day": dates[index + 1], "value": statistics.stdev(sample) * math.sqrt(252)})
        metrics = {"latestAnnualizedVolatility": observations[-1]["value"], "window": window}
    else:
        peak = closes[0]
        drawdown = 0.0
        for index, close in enumerate(closes[1:], 1):
            peak = max(peak, close)
            drawdown = min(drawdown, close / peak - 1)
            observations.append({"day": dates[index], "value": returns[index - 1]})
        metrics = {"priceChange": closes[-1] / closes[0] - 1,
                   "annualizedVolatility": statistics.stdev(returns) * math.sqrt(252),
                   "maximumPriceDrawdown": drawdown,
                   "positiveDays": sum(value > 0 for value in returns),
                   "negativeDays": sum(value < 0 for value in returns)}
    bar_json = json.dumps(bars, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data_hash = hashlib.sha256(bar_json.encode("utf-8")).hexdigest()
    parameters = {"instrumentId": instrument_id, "start": start, "end": end, "window": window}
    identity = json.dumps({"methodId": method_id, "parameters": parameters, "dataHash": data_hash,
                           "engineVersion": ENGINE_VERSION}, ensure_ascii=False, sort_keys=True)
    run_id = "qr_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    result = {"runId": run_id, "methodId": method_id, "methodVersion": 1,
              "name": method(data_root, method_id)["name"], "purpose": "PRICE_DIAGNOSTIC_ONLY",
              "instrumentId": instrument_id, "parameters": parameters, "barCount": len(bars),
              "firstDay": dates[0], "lastDay": dates[-1], "dataHash": data_hash,
              "engineVersion": ENGINE_VERSION, "metrics": metrics, "observations": observations,
              "limitations": method(data_root, method_id)["limitations"]}
    target = _run_path(data_root, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return result


def load_run(data_root: Path, run_id: str) -> dict[str, Any] | None:
    path = _run_path(data_root, run_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reproduce_run(data_root: Path, run_id: str) -> dict[str, Any]:
    original = load_run(data_root, run_id)
    if original is None:
        raise FileNotFoundError(run_id)
    if original["engineVersion"] != ENGINE_VERSION:
        raise ValueError("计算引擎版本已改变")
    params = original["parameters"]
    current = run_diagnostic(data_root, original["methodId"], params["instrumentId"],
                             params["start"], params["end"], window=params["window"])
    return {"reproducible": current["dataHash"] == original["dataHash"] and
            current["metrics"] == original["metrics"], "runId": run_id,
            "dataUnchanged": current["dataHash"] == original["dataHash"]}
