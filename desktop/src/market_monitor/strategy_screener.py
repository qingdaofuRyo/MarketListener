"""Bounded, current-day A-share screening using explicit source evidence."""
from __future__ import annotations

import ast
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import threading
import time as clock
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import requests
import duckdb

from market_monitor.a_share_calendar import has_verified_a_share_calendar, is_verified_a_share_trading_day
from market_monitor.f10 import parse_tencent_quotes
from market_monitor.exchange_delisted import DelistedSourceError, load_exchange
from market_monitor.formula_engine import (
    FormulaError, evaluate_formula, no_limit_down, no_limit_up, parse_formula, period_return, sum_daily_returns, up_days,
)
from market_monitor.market_data_version import market_data_version, revision_is_settled
from market_monitor.market_query_cache import KLineCacheUnavailable, get_kline_query_store
from market_monitor.storage import connect_catalog
from market_monitor.tdx_action_evidence import action_snapshot_is_current, load_action_snapshot
from market_monitor.web_api.common import read_bars
from market_monitor.web_api.market import _logical_instruments

BEIJING = ZoneInfo("Asia/Shanghai")
_FUNCTION_FIELDS = {"up_days": "close", "period_return": "close", "sum_daily_returns": "close",
                    "no_limit_up": "limit_up", "no_limit_down": "limit_down"}
_POOL_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, tuple[set[str], set[str]]], list[dict[str, str]]]] = {}
_POOL_LOCK = threading.Lock()
_FQ_BLOCKED_UNTIL: dict[str, float] = {}
_FQ_LOCK = threading.Lock()
_DELISTED_PATH = Path(__file__).with_name("config") / "a_share_delisted_evidence.json"
_SCREEN_POLICY_VERSION = "a-share-screen-v5-market-cap-close-check"


class AdjustmentSourceBlocked(ValueError):
    """The public adjustment source requested a pause or returned a challenge."""


def _fq_host_available(host: str) -> bool:
    with _FQ_LOCK:
        return _FQ_BLOCKED_UNTIL.get(host, 0.0) <= clock.monotonic()


def _note_fq_http_error(host: str, error: requests.HTTPError) -> bool:
    status = error.response.status_code if error.response is not None else None
    if status not in {403, 429, 501, 502, 503, 504}:
        return False
    with _FQ_LOCK:
        _FQ_BLOCKED_UNTIL[host] = clock.monotonic() + 300
    return True


def validate_screen_source(source: str) -> tuple[Any, int, set[str], int]:
    """Restrict scripts to documented pure functions and constant lookbacks."""
    program = parse_formula(source)
    lookback, requested, limit_lookback = 1, set(), 0
    for assignment in program.assignments.values():
        for node in ast.walk(assignment):
            if (isinstance(node, ast.Name) and node.id not in set(program.assignments)
                    and node.id not in {"close", "limit_up", "limit_down", *_FUNCTION_FIELDS}):
                raise FormulaError("UNSUPPORTED_INPUT", f"选股工作台不支持输入：{node.id}")
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name not in _FUNCTION_FIELDS or len(node.args) != 2:
                raise FormulaError("UNSUPPORTED_FUNCTION", "选股工作台只支持页面列出的五种函数")
            series, window = node.args
            if not isinstance(series, ast.Name) or series.id != _FUNCTION_FIELDS[name]:
                raise FormulaError("INVALID_SERIES", f"{name} 的第一个参数须为 {_FUNCTION_FIELDS[name]}")
            if (not isinstance(window, ast.Constant) or type(window.value) is not int
                    or not 1 <= window.value <= 60):
                raise FormulaError("INVALID_LOOKBACK", "回看期必须是 1 至 60 的整数常量")
            lookback = max(lookback, window.value)
            if name in {"no_limit_up", "no_limit_down"}:
                limit_lookback = max(limit_lookback, window.value)
            requested.add(name)
    return program, lookback, requested, limit_lookback


def verified_days(now: datetime, count: int) -> list[str]:
    local = now.astimezone(BEIJING)
    current = local.date()
    if local.time() < time(15, 30):
        current -= timedelta(days=1)
    result = []
    while len(result) < count:
        if not has_verified_a_share_calendar(current.year):
            raise FormulaError("CALENDAR_UNVERIFIED", f"{current.year} 年 A 股交易日历尚未核验")
        if is_verified_a_share_trading_day(current):
            result.append(current.isoformat())
        current -= timedelta(days=1)
    return list(reversed(result))


def _a_share_symbol(instrument_id: str) -> str | None:
    match = re.fullmatch(r"CN\.(SSE|SZSE|BSE)\.STOCK\.(\d{6})", instrument_id)
    if not match:
        return None
    exchange, code = match.groups()
    allowed = {"SSE": ("600", "601", "603", "605", "688", "689"),
               "SZSE": ("000", "001", "002", "003", "300", "301"), "BSE": ("4", "8", "92")}
    return {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}[exchange] + code if code.startswith(allowed[exchange]) else None


def _same_day_closing_quote(value: str, day: str, now: datetime) -> bool:
    try:
        timestamp = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=BEIJING)
    except (TypeError, ValueError):
        return False
    return timestamp.date().isoformat() == day and timestamp.time() >= time(15) and timestamp <= now.astimezone(BEIJING)


def _delisted_evidence(as_of: str, root: Path | None = None) -> tuple[dict[str, dict[str, str]], str]:
    """Return only individually evidenced delistings known by the target day."""
    try:
        raw = _DELISTED_PATH.read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("oversize")
        payload = json.loads(raw)
        if payload.get("schemaVersion") != 1 or not isinstance(payload.get("items"), list):
            raise ValueError("invalid evidence schema")
        result: dict[str, dict[str, str]] = {}
        seen: set[str] = set()
        target = date.fromisoformat(as_of)
        for item in payload["items"]:
            if not isinstance(item, dict):
                raise ValueError("invalid event")
            instrument_id, name = item.get("instrumentId"), item.get("name")
            announced, delisted, url = item.get("announcedAt"), item.get("delistedAt"), item.get("sourceUrl")
            if (not isinstance(instrument_id, str) or _a_share_symbol(instrument_id) is None or instrument_id in seen
                    or not isinstance(name, str) or not name.strip() or not isinstance(url, str)
                    or not re.match(r"^https://(?:www\.sse\.com\.cn|big5\.sse\.com\.cn|disc\.static\.szse\.cn|www\.nesc\.cn|www\.bse\.cn)/", url)):
                raise ValueError("invalid identity or source")
            seen.add(instrument_id)
            if not isinstance(announced, str) or not isinstance(delisted, str):
                raise ValueError("invalid dates")
            if date.fromisoformat(announced) <= target and date.fromisoformat(delisted) <= target:
                result[instrument_id] = {"instrumentId": instrument_id, "name": name, "delistedAt": delisted,
                                         "announcedAt": announced, "sourceUrl": url}
        digests = [sha256(raw).hexdigest()]
        if root is not None:
            for exchange in ("SSE", "SZSE"):
                rows, status = load_exchange(root, exchange)
                digests.append(str(status.get("sha256") or "UNAVAILABLE"))
                review = status.get("review")
                digests.append(str(review.get("sha256")) if isinstance(review, dict) else "NO_REVIEW")
                for row in rows:
                    if date.fromisoformat(row["delistedAt"]) > target:
                        continue
                    previous = result.get(row["instrumentId"])
                    if previous is not None and previous["delistedAt"] != row["delistedAt"]:
                        raise FormulaError("LISTING_EVIDENCE_CONFLICT", "交易所名单与已核验公告日期不一致，请检查来源")
                    if previous is None:
                        result[row["instrumentId"]] = {**row, "sourceObservedAt": str(status["fetchedAt"]),
                                                       "sourceType": "EXCHANGE_LIST"}
                if isinstance(review, dict):
                    for identifier in review.get("affectedIds", []):
                        if identifier in result:
                            result[identifier]["reviewStatus"] = "REVIEW_REQUIRED"
        return result, sha256("|".join(digests).encode()).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError, DelistedSourceError) as error:
        if isinstance(error, FormulaError):
            raise
        raise FormulaError("LISTING_EVIDENCE_INVALID", "已核验摘牌资料不可读取，请检查本机配置") from error


def _quote_issue(quote: Any, symbol: str, day: str, now: datetime) -> str | None:
    if quote is None:
        return "腾讯未返回可解析的报价"
    if quote.symbol != symbol or quote.code != symbol[2:]:
        return "腾讯报价代码与标的不一致，无法核对"
    try:
        if not isinstance(quote.quote_time, str) or not re.fullmatch(r"\d{14}", quote.quote_time):
            raise ValueError("invalid timestamp")
        stamp = datetime.strptime(quote.quote_time, "%Y%m%d%H%M%S").replace(tzinfo=BEIJING)
    except (TypeError, ValueError):
        return "腾讯报价时间缺失或格式无效"
    if stamp > now.astimezone(BEIJING):
        return "腾讯报价时间晚于当前时间，无法核对"
    if stamp.date().isoformat() != day:
        return "腾讯报价不是目标交易日的数据，无法核对当日市值"
    if stamp.time() < time(15):
        return "腾讯报价时间早于收盘，无法核对收盘市值"
    if (quote.total_market_cap_yi is None or quote.price is None
            or not math.isfinite(quote.total_market_cap_yi) or not math.isfinite(quote.price)
            or quote.total_market_cap_yi <= 0 or quote.price <= 0):
        return "腾讯收盘报价或总市值缺失、无效"
    return None


def parse_limit_pool(payload: Mapping[str, Any], day: str, expected_count: int) -> set[str]:
    """An empty or truncated provider result cannot prove non-membership."""
    data = payload.get("data")
    if payload.get("rc") != 0 or not isinstance(data, Mapping):
        raise FormulaError("LIMIT_POOL_UNAVAILABLE", f"{day} 涨跌停池响应不可用")
    rows, total = data.get("pool"), data.get("tc")
    # qdate describes the provider's latest market date, not the requested
    # historical date. Compare to the separately stored daily aggregate.
    if (type(total) is not int or total != expected_count
            or not isinstance(rows, list) or total <= 0 or total != len(rows) or total >= 10000):
        raise FormulaError("LIMIT_POOL_UNAVAILABLE", f"{day} 涨跌停池日期、数量或完整性待核")
    codes: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise FormulaError("LIMIT_POOL_UNAVAILABLE", f"{day} 涨跌停池格式无效")
        code = row.get("c")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or code in codes:
            raise FormulaError("LIMIT_POOL_UNAVAILABLE", f"{day} 涨跌停池代码无效或重复")
        codes.add(code)
    return codes


def _recorded_limit_counts(root: Path, days: list[str]) -> dict[tuple[str, str], int]:
    if not days:
        return {}
    try:
        with connect_catalog(root) as connection:
            rows = connection.execute(
                "SELECT trading_date, calculation_method, value FROM gold_metrics "
                "WHERE metric_id LIKE 'CN_ZT_POOL:%_COUNT' "
                "AND calculation_method IN ('source=stock_zt_pool_em','source=stock_zt_pool_dtgc_em') "
                "AND REPLACE(trading_date, '-', '') BETWEEN ? AND ?",
                [days[0].replace("-", ""), days[-1].replace("-", "")],
            ).fetchall()
    except (duckdb.Error, OSError) as error:
        raise FormulaError("LIMIT_POOL_UNAVAILABLE", "无法读取已存涨跌停池汇总") from error
    methods = {"source=stock_zt_pool_em": "getTopicZTPool", "source=stock_zt_pool_dtgc_em": "getTopicDTPool"}
    result: dict[tuple[str, str], int] = {}
    for day, method, value in rows:
        if value is None or not math.isfinite(float(value)) or float(value) <= 0 or not float(value).is_integer():
            continue
        key = (str(day).replace("-", ""), methods[str(method)])
        if key in result and result[key] != int(value):
            raise FormulaError("LIMIT_POOL_UNAVAILABLE", "已存涨跌停池汇总出现冲突，无法核验")
        result[key] = int(value)
    return result


def fetch_limit_pools(session: requests.Session, days: list[str], counts: Mapping[tuple[str, str], int]) -> tuple[dict[str, tuple[set[str], set[str]]], list[dict[str, str]]]:
    pools, evidence = {}, []
    for day in days:
        sides = []
        for endpoint, sort in (("getTopicZTPool", "fbt:asc"), ("getTopicDTPool", "fund:asc")):
            expected = counts.get((day.replace("-", ""), endpoint))
            if expected is None:
                raise FormulaError("LIMIT_POOL_UNAVAILABLE", f"{day} 已存涨跌停池汇总缺失，不能确认名单完整")
            for attempt in range(2):
                try:
                    response = session.get(f"https://push2ex.eastmoney.com/{endpoint}",
                                           params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                                                   "Pageindex": "0", "pagesize": "10000", "sort": sort,
                                                   "date": day.replace("-", "")}, timeout=12)
                    response.raise_for_status()
                    codes = parse_limit_pool(response.json(), day, expected)
                    break
                except (requests.RequestException, FormulaError, ValueError):
                    if attempt == 1:
                        raise
                    clock.sleep(.25)
            sides.append(codes)
            semantic = json.dumps(sorted(codes), separators=(",", ":")).encode("utf-8")
            evidence.append({"day": day, "kind": endpoint, "sha256": sha256(response.content).hexdigest(),
                             "contentSha256": sha256(semantic).hexdigest()})
        pools[day] = (sides[0], sides[1])
    return pools, evidence


def _cached_limit_pools(root: Path, session: requests.Session, days: list[str],
                        counts: Mapping[tuple[str, str], int]) -> tuple[dict[str, tuple[set[str], set[str]]], list[dict[str, str]]]:
    if not days:
        return {}, []
    key = (str(root.resolve()), tuple(days), tuple(sorted(counts.items())))
    with _POOL_LOCK:
        cached = _POOL_CACHE.get(key)
        if cached and clock.monotonic() - cached[0] < 300:
            return cached[1], cached[2]
        pools, evidence = fetch_limit_pools(session, days, counts)
        if len(_POOL_CACHE) >= 8:
            _POOL_CACHE.clear()
        _POOL_CACHE[key] = (clock.monotonic(), pools, evidence)
        return pools, evidence


def _adjusted_closes(session: requests.Session, symbol: str, days: list[str],
                     closes: list[float], events: list[str]) -> tuple[list[float], dict[str, Any]]:
    """Accept Tencent qfq only after its raw closes agree with local Silver."""
    values: dict[str, dict[str, float]] = {}
    hashes: dict[str, str] = {}
    selected_host = ""
    blocked = False
    for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
        if not _fq_host_available(host):
            blocked = True
            continue
        candidate: dict[str, dict[str, float]] = {}
        candidate_hashes: dict[str, str] = {}
        try:
            for kind, adjustment in (("raw", ""), ("qfq", "qfq")):
                response = session.get(f"https://{host}/appstock/app/fqkline/get",
                                       params={"param": f"{symbol},day,{days[0]},{days[-1]},100,{adjustment}"}, timeout=12)
                response.raise_for_status()
                data = response.json()
                rows = data["data"][symbol]["day" if kind == "raw" else "qfqday"]
                if not isinstance(rows, list):
                    raise ValueError("invalid adjusted quote response")
                indexed: dict[str, float] = {}
                for row in rows:
                    if not isinstance(row, list) or len(row) < 3 or row[0] not in days:
                        continue
                    day = row[0]
                    if day in indexed or isinstance(row[2], bool):
                        raise ValueError("duplicate or invalid adjusted quote")
                    number = float(row[2])
                    if not math.isfinite(number) or number <= 0:
                        raise ValueError("invalid adjusted close")
                    indexed[day] = number
                if set(indexed) != set(days):
                    raise ValueError("incomplete adjusted close history")
                candidate[kind] = indexed
                candidate_hashes[kind] = sha256(response.content).hexdigest()
        except requests.HTTPError as error:
            blocked = _note_fq_http_error(host, error) or blocked
            continue
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
            continue
        values, hashes, selected_host = candidate, candidate_hashes, host
        break
    if not selected_host:
        if blocked:
            raise AdjustmentSourceBlocked("adjusted source temporarily blocked")
        raise ValueError("adjusted quote source unavailable")
    if any(abs(values["raw"][day] - local) > .011 for day, local in zip(days, closes, strict=True)):
        raise ValueError("raw close disagreement")
    if abs(values["qfq"][days[-1]] - closes[-1]) > .011:
        raise ValueError("adjusted latest close mismatch")
    for event in events:
        if event not in days or days.index(event) == 0:
            raise ValueError("action date is not a comparable trading day")
        prior = days[days.index(event) - 1]
        prior_delta = values["qfq"][prior] - values["raw"][prior]
        current_delta = values["qfq"][event] - values["raw"][event]
        if abs(prior_delta - current_delta) <= .005:
            raise ValueError("action adjustment not observable")
    return [values["qfq"][day] for day in days], {"actionDays": events, "sourceHost": selected_host,
                                                  "rawSha256": hashes["raw"],
                                                  "adjustedSha256": hashes["qfq"]}


def screen_batch(root: Path, source: str, cap_yi: float, offset: int, limit: int,
                 *, instrument_ids: list[str] | None = None, now: datetime | None = None,
                 session: requests.Session | None = None) -> dict[str, Any]:
    program, lookback, _functions, limit_lookback = validate_screen_source(source)
    if not math.isfinite(cap_yi) or cap_yi < 0:
        raise FormulaError("INVALID_MARKET_CAP", "市值阈值必须是非负有限数值")
    if offset < 0 or not 1 <= limit <= 100:
        raise FormulaError("INVALID_PAGE", "批次范围无效")
    current_time = now or datetime.now(BEIJING)
    days = verified_days(current_time, lookback + 1)
    delisted, listing_digest = _delisted_evidence(days[-1], root)
    revision = market_data_version(root)
    if not revision_is_settled(root, revision):
        raise FormulaError("DATA_UPDATING", "行情数据正在更新，请稍后重试")
    logical = _logical_instruments(root)
    candidates = sorted((item for item in logical.values()
                         if str(item.get("market", "")).upper() == "CN"
                         and str(item.get("assetType", "")).upper() == "STOCK"
                         and _a_share_symbol(str(item.get("instrumentId", ""))) is not None
                         and item.get("storageInstrumentId")), key=lambda item: str(item["instrumentId"]))
    universe_bytes = json.dumps([_SCREEN_POLICY_VERSION, listing_digest,
                                 [(item["instrumentId"], item["storageInstrumentId"],
                                   sorted(str(value) for value in (item.get("sourceCandidates") or []) if value))
                                  for item in candidates]],
                                ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    universe_hash = sha256(universe_bytes).hexdigest()
    if instrument_ids is None:
        selected = candidates[offset:offset + limit]
    else:
        if (offset != 0 or not 1 <= len(instrument_ids) <= limit
                or len(set(instrument_ids)) != len(instrument_ids)
                or any(not isinstance(value, str) or _a_share_symbol(value) is None for value in instrument_ids)):
            raise FormulaError("INVALID_SELECTION", "待核标的须为不重复的有效 A 股，且数量不能超过批次上限")
        by_id = {str(item["instrumentId"]): item for item in candidates}
        if any(value not in by_id for value in instrument_ids):
            raise FormulaError("UNIVERSE_CHANGED", "部分待核标的已不在当前本机目录，请重新扫描")
        selected = [by_id[value] for value in instrument_ids]
    action_snapshot = load_action_snapshot()
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    limit_days = days[-limit_lookback:] if limit_lookback else []
    counts = _recorded_limit_counts(root, limit_days)
    pools, pool_evidence = (_cached_limit_pools(root, client, limit_days, counts) if session is None
                            else fetch_limit_pools(client, limit_days, counts))
    excluded, identity_unavailable = [], []
    for item in selected:
        identifier = str(item["instrumentId"])
        event = delisted.get(identifier)
        if event is None:
            continue
        if event.get("reviewStatus") == "REVIEW_REQUIRED":
            identity_unavailable.append({"instrumentId": identifier, "name": item.get("name"),
                                         "reason": "交易所历史摘牌记录发生变动，证券身份待核"})
            continue
        try:
            source_ids = list(dict.fromkeys([str(item["storageInstrumentId"]),
                                            *(str(value) for value in (item.get("sourceCandidates") or []) if value)]))
            latest_days = []
            store = get_kline_query_store(root)
            for source_id in source_ids:
                count, _earliest, latest = store.bounds(source_id, "1d")
                if count and not latest:
                    raise ValueError("missing daily boundary")
                if count:
                    latest_days.append(date.fromisoformat(str(latest)[:10]))
            if not latest_days:
                raise ValueError("no daily evidence")
            local_day = max(latest_days)
            if local_day >= date.fromisoformat(event["delistedAt"]):
                identity_unavailable.append({"instrumentId": identifier, "name": item.get("name"),
                                             "reason": "摘牌当日或之后仍有本地日线，证券身份或数据时序待核",
                                             "lastLocalDay": local_day.isoformat()})
                continue
            excluded.append(event)
        except (KLineCacheUnavailable, OSError, ValueError, RuntimeError, duckdb.Error):
            identity_unavailable.append({"instrumentId": identifier, "name": item.get("name"),
                                         "reason": "无法核对摘牌标的本地日线范围，证券身份待核"})
    active = [item for item in selected if str(item["instrumentId"]) not in delisted]
    symbols = [_a_share_symbol(str(item["instrumentId"])) for item in active]
    if any(symbol is None for symbol in symbols):
        raise FormulaError("INVALID_UNIVERSE", "标的分类发生变化，请重试")
    symbols = [symbol for symbol in symbols if symbol is not None]
    quotes: dict[str, Any] = {}
    for start in range(0, len(symbols), 60):
        response = client.get("https://qt.gtimg.cn/q=" + ",".join(symbols[start:start + 60]), timeout=12)
        response.raise_for_status()
        response.encoding = "gbk"
        quotes.update({quote.symbol: quote for quote in parse_tencent_quotes(response.text)})
    matched, unavailable = [], identity_unavailable
    below_cap = evaluated = 0
    for item, symbol in zip(active, symbols, strict=True):
        instrument_id = str(item["instrumentId"])
        code = instrument_id.rsplit(".", 1)[-1]
        quote = quotes.get(symbol)
        reason = _quote_issue(quote, symbol, days[-1], current_time)
        bars = read_bars(root, str(item["storageInstrumentId"]), period="1d", limit=lookback + 5)
        keyed = {str(bar.get("trading_date") or bar.get("bar_open_time") or "")[:10]: bar for bar in bars}
        window = [keyed.get(day) for day in days]
        latest = window[-1]
        if not reason and latest is None:
            reason = "目标交易日本地日线缺失，无法核对收盘市值"
        if not reason and (latest.get("source") != "通达信金融终端（本地）"
                           or latest.get("quality_status") != "PASS"
                           or latest.get("normalization_version") != "tdx-cn-v2"):
            reason = "日线未通过通达信证券归一化质量门"
        latest_close = latest.get("close") if latest else None
        if not reason and (not isinstance(latest_close, (float, int)) or isinstance(latest_close, bool)
                           or not math.isfinite(latest_close) or latest_close <= 0):
            reason = "收盘价缺失或无效"
        if not reason and abs(float(latest_close) - float(quote.price)) > .011:
            reason = "本地收盘价与腾讯同日报价不一致"
        if not reason and quote.total_market_cap_yi <= cap_yi:
            below_cap += 1
            continue
        if not reason and any(bar is None for bar in window):
            reason = "连续交易日行情不足或停牌，无法确认窗口"
        if not reason and any(bar.get("source") != "通达信金融终端（本地）"
                              or bar.get("quality_status") != "PASS" or bar.get("normalization_version") != "tdx-cn-v2"
                              for bar in window if bar is not None):
            reason = "日线未通过通达信证券归一化质量门"
        closes = [bar.get("close") if bar else None for bar in window]
        if not reason and any(not isinstance(value, (float, int)) or isinstance(value, bool)
                              or not math.isfinite(value) or value <= 0 for value in closes):
            reason = "收盘价缺失或无效"
        if reason:
            trusted_identity = quote is not None and quote.symbol == symbol and quote.code == code
            unavailable.append({"instrumentId": instrument_id, "name": item.get("name"), "reason": reason,
                                "quoteName": quote.name if trusted_identity else None,
                                "quoteTime": quote.quote_time if trusted_identity else None,
                                "lastLocalDay": max((day for day in keyed if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)), default=None)})
            continue
        action_days = action_snapshot.window_events(instrument_id, days[0], days[-1])
        price_basis = "TDX_RAW"
        adjustment_evidence = None
        if action_days:
            try:
                closes, adjustment_evidence = _adjusted_closes(client, symbol, days, closes, action_days)
                price_basis = "TENCENT_QFQ_VERIFIED_AGAINST_TDX_RAW"
            except AdjustmentSourceBlocked:
                unavailable.append({"instrumentId": instrument_id, "name": item.get("name"),
                                    "reason": "窗口内有除权记录，腾讯前复权接口暂受限制，请稍后重新扫描"})
                continue
            except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
                unavailable.append({"instrumentId": instrument_id, "name": item.get("name"),
                                    "reason": "窗口内存在本机除权记录，前复权价格缺失或未通过原价核对"})
                continue
        flags = [pools.get(day) for day in days]
        context = {"close": closes,
                   "limit_up": [code in value[0] if value else None for value in flags],
                   "limit_down": [code in value[1] if value else None for value in flags]}
        result = evaluate_formula(program, context, {"up_days": up_days, "period_return": period_return,
                                  "sum_daily_returns": sum_daily_returns, "no_limit_up": no_limit_up,
                                  "no_limit_down": no_limit_down})
        signal = result["signal"][-1] if isinstance(result["signal"], list) else result["signal"]
        evaluated += 1
        if signal:
            matched.append({"instrumentId": instrument_id, "name": item.get("name"),
                            "upDays20": up_days(closes, 20)[-1] if lookback >= 20 else None,
                            "marketCapYi": quote.total_market_cap_yi,
                            "return5d": period_return(closes, 5)[-1] if lookback >= 5 else None,
                            "quoteTime": quote.quote_time, "close": closes[-1], "priceBasis": price_basis,
                            "adjustmentEvidence": adjustment_evidence})
    try:
        evidence_unchanged = _delisted_evidence(days[-1], root)[1] == listing_digest
    except FormulaError:
        evidence_unchanged = False
    if not revision_is_settled(root, revision) or not action_snapshot_is_current(action_snapshot) or not evidence_unchanged:
        raise FormulaError("DATA_UPDATING", "计算时行情版本变化，请重新运行")
    return {"asOf": days[-1], "silverRevision": revision, "universeHash": universe_hash,
            "screenPolicyVersion": _SCREEN_POLICY_VERSION,
            "offset": offset, "processed": len(selected),
            "universeCount": len(candidates), "hasMore": instrument_ids is None and offset + len(selected) < len(candidates),
            "requestedInstrumentIds": instrument_ids,
            "belowMarketCap": below_cap, "evaluated": evaluated,
            "matches": matched, "unavailable": unavailable, "excludedDelisted": excluded, "poolEvidence": pool_evidence,
            "corporateActionEvidence": action_snapshot.public(),
            "scope": "本机行情目录中的沪深京 A 股；逐批扫描，停牌及缺少可核验数据者列为待核"}
