"""Dated Shanghai, Shenzhen and Beijing exchange market statistics.

Only explicitly identified A-share rows enter the A-share aggregate. Parent
fund/bond rows and their children remain separate observations.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import openpyxl
import requests

from .a_share_calendar import is_known_a_share_closed_day, is_verified_a_share_trading_day
from .collector import _metric
from . import official_exchange_bond_fund as extensions
from .storage import MarketStore


_SSE_URL = "https://query.sse.com.cn/commonQuery.do"
_SZSE_XLSX_URL = "https://www.szse.cn/api/report/ShowReport"
_BSE_STOCK_URL = "https://www.bseinfo.net/marketStatController/dailyReport.do"
_BSE_BOND_URL = "https://www.bseinfo.net/xyzStatController/xyzStatDailyReport.do"
_CALLBACK = "mlOfficialDaily"
_YUAN_PER_YI = Decimal("100000000")
_WAN_PER_YI = Decimal("10000")
_TOLERANCE_YI = Decimal("0.12")
_SOURCE_IDS = ("sse_market", "sse_stock", "sse_volume", "szse_categories",
               "szse_stock", "szse_main", "szse_chinext", "bse_stock", "bse_bond")
_SSE_CATEGORIES = {
    "主板A": "STOCK_MAIN_A", "主板B": "STOCK_MAIN_B", "科创板": "STOCK_STAR",
    "股票": "STOCK_ALL", "基金": "FUND", "ETF": "FUND_ETF", "LOF": "FUND_LOF",
    "公募REITs": "FUND_REIT", "债券": "BOND", "政府债": "BOND_GOV",
    "信用债": "BOND_CREDIT", "回购": "BOND_REPO", "优先股": "PREFERRED",
    "期权": "OPTION", "其他": "OTHER",
}
_SZSE_CATEGORIES = {
    "股票": "STOCK_ALL", "主板A股": "STOCK_MAIN_A", "主板B股": "STOCK_MAIN_B",
    "创业板A股": "STOCK_CHINEXT", "基金": "FUND", "ETF": "FUND_ETF",
    "LOF": "FUND_LOF", "封闭式基金": "FUND_CLOSED", "不动产基金": "FUND_REIT",
    "债券": "BOND", "债券现券": "BOND_CASH", "债券回购": "BOND_REPO",
    "ABS": "BOND_ABS", "期权": "OPTION",
}
_FIELDS = {
    "capYi": ("TOTAL_MARKET_CAP_YI", "总市值", "亿元"),
    "floatCapYi": ("FLOAT_MARKET_CAP_YI", "流通市值", "亿元"),
    "turnoverYi": ("TOTAL_AMOUNT_YI", "成交额", "亿元"),
    "companyCount": ("COMPANY_COUNT", "上市公司家数", "家"),
    "securityCount": ("SECURITY_COUNT", "证券数量", "只"),
    "sharesYi": ("TOTAL_SHARES_YI", "总股本", "亿股"),
    "floatSharesYi": ("FLOAT_SHARES_YI", "流通股本", "亿股"),
    "volumeYi": ("VOLUME_YI", "成交股数或份数", "亿股/亿份"),
    "tradeCount": ("TRADE_COUNT", "成交笔数", "笔"),
    "custodyValueYi": ("CUSTODY_MARKET_VALUE_YI", "托管市值", "亿元"),
    "custodyFaceYi": ("CUSTODY_FACE_VALUE_YI", "托管面值", "亿元"),
    "issuanceYi": ("ISSUANCE_YI", "当月发行额", "亿元"),
    "issuanceYtdYi": ("ISSUANCE_YTD_YI", "年内累计发行额", "亿元"),
}
_EXTENSION_NAMES = ("sse_fund_daily", "szse_fund_daily", "sse_bond_custody_daily")


@dataclass(frozen=True)
class _Artifact:
    source: str
    requested_url: str
    response_url: str
    fetched_at: str
    body: bytes


def _number(value: Any, label: str, *, zero: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or str(value).strip() in {"", "-", "—"}:
        raise ValueError(f"{label}: missing")
    try:
        result = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{label}: invalid number") from exc
    if not result.is_finite() or result < 0 or (result == 0 and not zero):
        raise ValueError(f"{label}: invalid range")
    return result


def _optional_number(value: Any, label: str) -> Decimal | None:
    return None if value is None or str(value).strip() in {"", "-", "—"} else _number(value, label, zero=True)


def _near(actual: Decimal, expected: Decimal, label: str, tolerance: Decimal = _TOLERANCE_YI) -> None:
    if abs(actual - expected) > tolerance:
        raise ValueError(f"{label}: components disagree")


def _sse_json(blob: bytes, day: str) -> list[dict[str, Any]]:
    try:
        response = json.loads(blob)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("SSE response is not JSON") from exc
    if (not isinstance(response, dict) or response.get("actionErrors") not in ([], None)
            or not isinstance(response.get("result"), list) or not response["result"]):
        raise ValueError("SSE market response has no validated rows")
    rows = response["result"]
    if any(not isinstance(row, dict) or row.get("TRADE_DATE") != day.replace("-", "") for row in rows):
        raise ValueError("SSE row date disagrees with requested day")
    return rows


def _workbook_rows(blob: bytes, headers: tuple[str, ...]) -> list[tuple[Any, ...]]:
    if not blob.startswith(b"PK") or len(blob) > 1_000_000:
        raise ValueError("SZSE response is not a bounded XLSX")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        book = openpyxl.load_workbook(io.BytesIO(blob), read_only=False, data_only=True)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("SZSE workbook has unexpected sheets")
        rows = list(book.active.values)
    finally:
        book.close()
    if not rows or tuple(str(cell).strip() for cell in rows[0][:len(headers)]) != headers:
        raise ValueError("SZSE workbook headers or units changed")
    return rows[1:]


def _szse_categories(blob: bytes) -> dict[str, dict[str, Decimal | None]]:
    rows = _workbook_rows(blob, ("证券类别", "数量(只)", "成交金额(元)", "总市值(元)", "流通市值(元)"))
    result: dict[str, dict[str, Decimal | None]] = {}
    for row in rows:
        label = str(row[0] or "").strip()
        key = _SZSE_CATEGORIES.get(label)
        if key is None:
            continue
        if key in result:
            raise ValueError("SZSE category repeated")
        result[key] = {
            "securityCount": _number(row[1], f"SZSE {label} securities", zero=True),
            "turnoverYi": _number(row[2], f"SZSE {label} turnover", zero=True) / _YUAN_PER_YI,
            "capYi": None if _optional_number(row[3], f"SZSE {label} cap") is None else
            _number(row[3], f"SZSE {label} cap", zero=True) / _YUAN_PER_YI,
            "floatCapYi": None if _optional_number(row[4], f"SZSE {label} float cap") is None else
            _number(row[4], f"SZSE {label} float cap", zero=True) / _YUAN_PER_YI,
        }
    if not {"STOCK_ALL", "STOCK_MAIN_A", "STOCK_MAIN_B", "STOCK_CHINEXT", "FUND", "BOND_CASH", "BOND_ABS", "BOND_REPO"} <= result.keys():
        raise ValueError("SZSE daily categories incomplete")
    for metric in ("capYi", "floatCapYi", "turnoverYi", "securityCount"):
        parts = sum((result[key][metric] or Decimal(0) for key in ("STOCK_MAIN_A", "STOCK_MAIN_B", "STOCK_CHINEXT")), Decimal(0))
        _near(result["STOCK_ALL"][metric] or Decimal(0), parts, f"SZSE stock {metric}",
              Decimal("0.01") if metric in {"capYi", "floatCapYi", "turnoverYi"} else Decimal(0))
    return result


def _szse_indicator(blob: bytes) -> dict[str, Decimal]:
    rows = _workbook_rows(blob, ("指标名称", "本日数值", "比上日增减", "幅度%"))
    result: dict[str, Decimal] = {}
    for row in rows:
        label = re.split(r"[（(]", str(row[0] or "").strip(), maxsplit=1)[0]
        if label and row[1] not in (None, "", "-", "—"):
            if label in result:
                raise ValueError("SZSE indicator repeated")
            result[label] = _number(row[1], f"SZSE indicator {label}", zero=True)
    return result


def _jsonp(blob: bytes, callback: str) -> list[dict[str, Any]]:
    try:
        text = blob.decode("utf-8").strip()
        match = re.fullmatch(re.escape(callback) + r"\((\[.*\])\);?", text, re.S)
        value = json.loads(match.group(1)) if match else None
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("BSE response is not valid JSONP") from exc
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise ValueError("BSE daily response is empty or malformed")
    return value


def _parse_day(day: str, bodies: dict[str, bytes]) -> dict[tuple[str, str], dict[str, Decimal | None]]:
    records: dict[tuple[str, str], dict[str, Decimal | None]] = {}
    sse_market = _sse_json(bodies["sse_market"], day)
    categories = {str(row.get("PRODUCT_NAME")): row for row in sse_market}
    if len(categories) != len(sse_market) or not {"主板A", "主板B", "科创板", "股票", "基金", "债券", "回购"} <= categories.keys():
        raise ValueError("SSE market categories incomplete or duplicated")
    for label, row in categories.items():
        category = _SSE_CATEGORIES.get(label)
        if category is None:
            # Retain new source rows in Bronze until their business meaning is reviewed.
            continue
        records[("SSE", category)] = {
            "securityCount": _number(row.get("SECURITY_NUM"), f"SSE {label} securities", zero=True),
            "capYi": _optional_number(row.get("TOTAL_VALUE"), f"SSE {label} cap"),
            "floatCapYi": _optional_number(row.get("NEGO_VALUE"), f"SSE {label} float cap"),
            "turnoverYi": _number(row.get("TOTAL_TRADE_AMT"), f"SSE {label} turnover", zero=True),
        }
    for field in ("securityCount", "capYi", "floatCapYi", "turnoverYi"):
        parts = sum((records[("SSE", key)][field] or Decimal(0)
                     for key in ("STOCK_MAIN_A", "STOCK_MAIN_B", "STOCK_STAR")), Decimal(0))
        _near(records[("SSE", "STOCK_ALL")][field] or Decimal(0), parts,
              f"SSE stock {field}", Decimal(0) if field == "securityCount" else _TOLERANCE_YI)
    for field in ("securityCount", "capYi", "floatCapYi", "turnoverYi"):
        parts = sum((records[("SSE", key)][field] or Decimal(0)
                     for key in ("FUND_ETF", "FUND_LOF", "FUND_REIT")), Decimal(0))
        _near(records[("SSE", "FUND")][field] or Decimal(0), parts,
              f"SSE fund {field}", Decimal(0) if field == "securityCount" else _TOLERANCE_YI)

    sse_stock = _sse_json(bodies["sse_stock"], day)
    stock_rows = {str(row.get("PRODUCT_NAME")): row for row in sse_stock}
    if set(stock_rows) != {"主板A", "科创板", "股票"} or len(stock_rows) != len(sse_stock):
        raise ValueError("SSE stock overview categories changed")
    for label, category in (("主板A", "STOCK_MAIN_A"), ("科创板", "STOCK_STAR"),
                            ("股票", "STOCK_ALL")):
        row = stock_rows[label]
        record = records[("SSE", category)]
        for key, source_key in (("capYi", "TOTAL_VALUE"), ("floatCapYi", "NEGO_VALUE"),
                                ("turnoverYi", "TOTAL_TRADE_AMT"), ("securityCount", "SECURITY_NUM")):
            _near(record[key] or Decimal(0), _number(row.get(source_key),
                  f"SSE stock {label} {source_key}", zero=True), f"SSE stock crosscheck {label} {key}")
        record["companyCount"] = _number(row.get("LIST_COM_NUM"), f"SSE {label} companies")
        record["sharesYi"] = _number(row.get("TOTAL_ISSUE_VOL"), f"SSE {label} shares")
        record["floatSharesYi"] = _number(row.get("NEGO_ISSUE_VOL"), f"SSE {label} float shares")
    sse_volume = _sse_json(bodies["sse_volume"], day)
    volume_rows = {str(row.get("PRODUCT_CODE")): row for row in sse_volume}
    if (not {"01", "02", "03", "17"} <= volume_rows.keys()
            or not volume_rows.keys() <= {"01", "02", "03", "11", "17"}
            or len(volume_rows) != len(sse_volume)):
        raise ValueError("SSE stock volume categories changed")
    for code, category in (("01", "STOCK_MAIN_A"), ("02", "STOCK_MAIN_B"),
                           ("03", "STOCK_STAR"), ("17", "STOCK_ALL")):
        row = volume_rows[code]
        records[("SSE", category)]["volumeYi"] = _number(row.get("TRADE_VOL"),
                                                            f"SSE {code} volume", zero=True)
        if code != "02":
            _near(records[("SSE", category)]["turnoverYi"] or Decimal(0),
                  _number(row.get("TRADE_AMT"), f"SSE {code} turnover", zero=True),
                  f"SSE {code} turnover crosscheck")
    _near(records[("SSE", "STOCK_ALL")]["volumeYi"] or Decimal(0),
          sum((records[("SSE", category)]["volumeYi"] or Decimal(0)
               for category in ("STOCK_MAIN_A", "STOCK_MAIN_B", "STOCK_STAR")), Decimal(0)),
          "SSE total stock volume")

    szse = _szse_categories(bodies["szse_categories"])
    for category, values in szse.items():
        records[("SZSE", category)] = values
    for field in ("securityCount", "capYi", "floatCapYi", "turnoverYi"):
        children = ("FUND_ETF", "FUND_LOF", "FUND_CLOSED", "FUND_REIT")
        if all(("SZSE", child) in records for child in children):
            total = sum((records[("SZSE", child)][field] or Decimal(0) for child in children), Decimal(0))
            _near(records[("SZSE", "FUND")][field] or Decimal(0), total,
                  f"SZSE fund {field}", Decimal(0) if field == "securityCount" else Decimal("0.02"))
    szse_all = _szse_indicator(bodies["szse_stock"])
    szse_main = _szse_indicator(bodies["szse_main"])
    szse_chinext = _szse_indicator(bodies["szse_chinext"])
    all_record = records[("SZSE", "STOCK_ALL")]
    all_record["companyCount"] = szse_all["上市公司数"]
    all_record["sharesYi"] = szse_all["股票总股本"] / _YUAN_PER_YI
    all_record["floatSharesYi"] = szse_all["股票流通股本"] / _YUAN_PER_YI
    for key, values in (("STOCK_MAIN_ALL", szse_main), ("STOCK_CHINEXT", szse_chinext)):
        record = records.get(("SZSE", key), {}).copy()
        record["companyCount"] = values["上市公司数"]
        record["sharesYi"] = values["总股本"] / _YUAN_PER_YI
        record["floatSharesYi"] = values["流通股本"] / _YUAN_PER_YI
        record["volumeYi"] = values["成交量"] / _YUAN_PER_YI
        records[("SZSE", key)] = record
    all_record["volumeYi"] = (records[("SZSE", "STOCK_MAIN_ALL")]["volumeYi"] or Decimal(0)) + (
        records[("SZSE", "STOCK_CHINEXT")]["volumeYi"] or Decimal(0))
    for key in ("companyCount", "sharesYi", "floatSharesYi"):
        _near(all_record[key] or Decimal(0),
              (records[("SZSE", "STOCK_MAIN_ALL")][key] or Decimal(0)) +
              (records[("SZSE", "STOCK_CHINEXT")][key] or Decimal(0)),
              f"SZSE stock {key}", Decimal(0) if key == "companyCount" else Decimal("0.00000002"))
    for key, label in (("capYi", "股票总市值"), ("floatCapYi", "股票流通市值"),
                       ("turnoverYi", "股票成交金额")):
        _near(all_record[key] or Decimal(0), szse_all[label] / _YUAN_PER_YI,
              f"SZSE stock summary {key}", Decimal("0.00000002"))

    bse_stock = _jsonp(bodies["bse_stock"], _CALLBACK)
    if any(row.get("rq") != day.replace("-", "") for row in bse_stock):
        raise ValueError("BSE stock date disagrees with requested day")
    stock_rows = [row for row in bse_stock if row.get("xxzrlx") == "2"]
    if len(stock_rows) != 1:
        raise ValueError("BSE official stock category is missing or duplicated")
    row = stock_rows[0]
    records[("BSE", "STOCK_A")] = {
        "companyCount": _number(row.get("gpgsjs"), "BSE listed companies"),
        "sharesYi": _number(row.get("xxzgb"), "BSE total shares") / _YUAN_PER_YI,
        "floatSharesYi": _number(row.get("xxfxsgb"), "BSE float shares") / _YUAN_PER_YI,
        "capYi": _number(row.get("zsz"), "BSE market cap") / _YUAN_PER_YI,
        "floatCapYi": _number(row.get("ltsz"), "BSE float cap") / _YUAN_PER_YI,
        "turnoverYi": _number(row.get("hqcjje"), "BSE turnover", zero=True) / _YUAN_PER_YI,
        "volumeYi": _number(row.get("hqcjsl"), "BSE volume", zero=True) / _YUAN_PER_YI,
    }

    if "bse_bond" in bodies:
        bse_bonds = _jsonp(bodies["bse_bond"], _CALLBACK)
        if any(bond.get("rq") != day for bond in bse_bonds):
            raise ValueError("BSE bond date disagrees with requested day")
        bonds = {str(bond.get("zqlb")): bond for bond in bse_bonds}
        if set(bonds) != {"20", "30", "sum"} or len(bonds) != len(bse_bonds):
            raise ValueError("BSE bond categories incomplete")
        for code, category in (("20", "BOND_PUBLIC_CORP"), ("30", "BOND_PRIVATE_CORP"),
                               ("sum", "BOND_CASH")):
            bond = bonds[code]
            records[("BSE", category)] = {
                "securityCount": _number(bond.get("tgzs"), f"BSE bond {code} count", zero=True),
                "turnoverYi": _number(bond.get("drcjje"), f"BSE bond {code} turnover", zero=True) / _WAN_PER_YI,
                "tradeCount": _number(bond.get("drcjbs"), f"BSE bond {code} trades", zero=True),
            }
        for metric in ("securityCount", "turnoverYi", "tradeCount"):
            _near(records[("BSE", "BOND_CASH")][metric] or Decimal(0),
                  (records[("BSE", "BOND_PUBLIC_CORP")][metric] or Decimal(0)) +
                  (records[("BSE", "BOND_PRIVATE_CORP")][metric] or Decimal(0)),
                  f"BSE bond {metric}", Decimal(0))

    records[("SSE", "STOCK_A")] = _sum_records(records, (("SSE", "STOCK_MAIN_A"),
                                                  ("SSE", "STOCK_STAR")))
    records[("SZSE", "STOCK_A")] = _sum_records(records, (("SZSE", "STOCK_MAIN_A"),
                                                   ("SZSE", "STOCK_CHINEXT")),
                                              fields=("securityCount", "capYi", "floatCapYi", "turnoverYi"))
    records[("ALL", "STOCK_A")] = _sum_records(records, (("SSE", "STOCK_A"),
                                                  ("SZSE", "STOCK_A"), ("BSE", "STOCK_A")),
                                             fields=("capYi", "floatCapYi", "turnoverYi"))
    records[("ALL", "STOCK_LISTED")] = _sum_records(records, (("SSE", "STOCK_ALL"),
                                                       ("SZSE", "STOCK_ALL"), ("BSE", "STOCK_A")),
                                                  fields=("companyCount", "sharesYi", "floatSharesYi", "volumeYi"))
    records[("SSE_SZSE", "FUND")] = _sum_records(records, (("SSE", "FUND"), ("SZSE", "FUND")),
                                                 fields=("securityCount", "capYi", "floatCapYi", "turnoverYi"))
    if ("BSE", "BOND_CASH") in records:
        records[("ALL", "BOND_CASH")] = {
            "turnoverYi": (records[("SSE", "BOND")]["turnoverYi"] or Decimal(0)) +
            (records[("SZSE", "BOND_CASH")]["turnoverYi"] or Decimal(0)) +
            (records[("SZSE", "BOND_ABS")]["turnoverYi"] or Decimal(0)) +
            (records[("BSE", "BOND_CASH")]["turnoverYi"] or Decimal(0)),
        }
    records[("SSE_SZSE", "BOND_REPO")] = {
        "turnoverYi": (records[("SSE", "BOND_REPO")]["turnoverYi"] or Decimal(0)) +
        (records[("SZSE", "BOND_REPO")]["turnoverYi"] or Decimal(0)),
    }
    return records


def _sum_records(
    records: dict[tuple[str, str], dict[str, Decimal | None]],
    members: tuple[tuple[str, str], ...],
    *, fields: tuple[str, ...] = ("companyCount", "securityCount", "sharesYi", "floatSharesYi",
                                  "capYi", "floatCapYi", "turnoverYi", "volumeYi"),
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for field in fields:
        values = [records[member].get(field) for member in members]
        if all(value is not None for value in values):
            result[field] = sum((value for value in values if value is not None), Decimal(0))
    return result


def _request(source: str, day: str) -> tuple[str, str, dict[str, str], dict[str, str]]:
    sse_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"}
    szse_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/market/overview/index.html"}
    bse_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseinfo.net/static/statisticdata.html",
                   "Origin": "https://www.bseinfo.net", "X-Requested-With": "XMLHttpRequest"}
    if source == "sse_market":
        return "GET", _SSE_URL, {"sqlId": "COMMON_SSE_SJ_SCGM_C", "isPagination": "false",
                                 "TRADE_DATE": day}, sse_headers
    if source == "sse_stock":
        return "GET", _SSE_URL, {"sqlId": "COMMON_SSE_SJ_GPSJ_GPSJZM_TJSJ_L",
                                 "PRODUCT_NAME": "股票,主板A,科创板", "type": "inParams", "TRADE_DATE": day}, sse_headers
    if source == "sse_volume":
        return "GET", _SSE_URL, {"sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
                                 "PRODUCT_CODE": "01,02,03,11,17", "type": "inParams",
                                 "SEARCH_DATE": day}, sse_headers
    if source in {"szse_categories", "szse_stock", "szse_main", "szse_chinext"}:
        catalog, tab = ("1803_sczm", "tab1") if source == "szse_categories" else (
            "1803_after", {"szse_stock": "tab1", "szse_main": "tab2", "szse_chinext": "tab4"}[source])
        return "GET", _SZSE_XLSX_URL, {"SHOWTYPE": "xlsx", "CATALOGID": catalog,
                                       "TABKEY": tab, "txtQueryDate": day}, szse_headers
    if source == "bse_stock":
        return "POST", _BSE_STOCK_URL, {"callback": _CALLBACK, "HQJSRQ": day.replace("-", "")}, bse_headers
    if source == "bse_bond":
        return "POST", _BSE_BOND_URL, {"callback": _CALLBACK, "date": day}, {
            **bse_headers, "Referer": "https://www.bseinfo.net/static/xyz_statisticdata.html"}
    raise ValueError("unknown official market source")


def _fetch(session: requests.Session, source: str, day: str) -> _Artifact:
    method, url, parameters, headers = _request(source, day)
    options: dict[str, Any] = {"headers": headers, "timeout": 20}
    options["params" if method == "GET" else "data"] = parameters
    if method == "POST":
        options["params"] = {"callback": parameters["callback"]}
        options["data"] = {key: value for key, value in parameters.items() if key != "callback"}
    response = session.request(method, url, **options)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError(f"{source}: HTTP status is not 200")
    expected_host = urlsplit(url).hostname
    if urlsplit(response.url).scheme != "https" or urlsplit(response.url).hostname != expected_host:
        raise ValueError(f"{source}: unexpected redirect")
    if not response.content or len(response.content) > 1_000_000:
        raise ValueError(f"{source}: response size invalid")
    requested = requests.Request(method, url, params=options.get("params"),
                                 data=options.get("data")).prepare().url
    if requested is None:
        raise ValueError(f"{source}: request URL invalid")
    return _Artifact(source, requested, response.url,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), response.content)


def _atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bronze_folder(root: Path, day: str) -> Path:
    return root / "bronze" / "official_exchange_daily" / f"day={day}"


def _backlog_path(root: Path, kind: str) -> Path:
    return root / "state" / f"official_exchange_{kind}_backlog.json"


def _load_backlog(root: Path, kind: str) -> dict[str, str]:
    try:
        document = json.loads(_backlog_path(root, kind).read_text(encoding="utf-8"))
        entries = document.get("failures")
        if not isinstance(entries, dict):
            return {}
        return {str(day): str(reason) for day, reason in entries.items()
                if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", str(day))}
    except (OSError, ValueError, TypeError):
        return {}


def _update_backlog(root: Path, kind: str, *, completed: list[str],
                    failed: list[dict[str, str]], key: str) -> None:
    backlog = _load_backlog(root, kind)
    for period in completed:
        backlog.pop(period, None)
    for item in failed:
        period = item.get(key)
        if period:
            backlog[period] = item.get("error", "UNKNOWN")
    document = {"schema": "official-exchange-backlog-v1", "kind": kind,
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "failures": dict(sorted(backlog.items()))}
    _atomic(_backlog_path(root, kind), json.dumps(document, ensure_ascii=False,
                                                 sort_keys=True).encode("utf-8"))


def _read_bronze(root: Path, day: str) -> dict[str, bytes] | None:
    folder = _bronze_folder(root, day)
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema") != "official-exchange-daily-v1" or manifest.get("day") != day:
            return None
        entries = manifest.get("sources")
        if not isinstance(entries, dict) or not entries or not set(entries) <= set(_SOURCE_IDS):
            return None
        blobs: dict[str, bytes] = {}
        for source, entry in entries.items():
            if source not in _SOURCE_IDS or not isinstance(entry, dict):
                return None
            method, url, parameters, _ = _request(source, day)
            query = parameters if method == "GET" else {"callback": parameters["callback"]}
            expected_url = requests.Request(method, url, params=query).prepare().url
            response_url = entry.get("responseUrl")
            if (entry.get("method") != method or entry.get("requestedUrl") != expected_url
                    or not isinstance(response_url, str) or urlsplit(response_url).scheme != "https"
                    or urlsplit(response_url).hostname != urlsplit(url).hostname):
                return None
            form = {key: value for key, value in parameters.items() if key != "callback"} if method == "POST" else None
            if entry.get("requestForm") != form:
                return None
            body = (folder / f"{source}.raw").read_bytes()
            if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
                return None
            if entry.get("requestedDay") != day or entry.get("status") != 200:
                return None
            blobs[source] = body
        return blobs
    except (OSError, ValueError, TypeError):
        return None


def _save_bronze(root: Path, day: str, artifacts: dict[str, _Artifact]) -> None:
    folder = _bronze_folder(root, day)
    existing = _read_bronze(root, day)
    if existing is not None:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    else:
        manifest = {"schema": "official-exchange-daily-v1", "day": day, "sources": {}}
    for source, item in artifacts.items():
        _atomic(folder / f"{source}.raw", item.body)
        manifest["sources"][source] = _manifest_entry(source, day, item)
    _atomic(folder / "manifest.json", json.dumps(manifest, ensure_ascii=False,
                                                   sort_keys=True).encode("utf-8"))


def _manifest_entry(source: str, day: str, item: _Artifact) -> dict[str, Any]:
    method, _, parameters, _ = _request(source, day)
    return {
        "requestedDay": day, "method": method,
        "requestedUrl": item.requested_url, "responseUrl": item.response_url,
        "requestForm": ({key: value for key, value in parameters.items() if key != "callback"}
                        if method == "POST" else None),
        "status": 200, "fetchedAt": item.fetched_at,
        "sha256": hashlib.sha256(item.body).hexdigest(),
    }


def _extension_folder(root: Path, day: str) -> Path:
    return root / "bronze" / "official_exchange_extensions" / f"day={day}"


def _extension_parser(source: str, body: bytes, day: str) -> dict[tuple[str, str], dict[str, Decimal]]:
    if source == "sse_fund_daily":
        return extensions.parse_sse_fund_daily(body, day)
    if source == "szse_fund_daily":
        return extensions.parse_szse_fund_daily(body)
    if source == "sse_bond_custody_daily":
        return extensions.parse_sse_bond_custody_daily(body, day)
    raise ValueError("unknown official extension")


def _read_extension(root: Path, day: str, source: str) -> tuple[dict[tuple[str, str], dict[str, Decimal]], bytes] | None:
    folder = _extension_folder(root, day)
    try:
        info = json.loads((folder / f"{source}.json").read_text(encoding="utf-8"))
        body = (folder / f"{source}.raw").read_bytes()
        if (info.get("source") != source or info.get("requestedPeriod") != day
                or hashlib.sha256(body).hexdigest() != info.get("sha256")
                or info.get("status") != 200):
            return None
        url = info.get("requestedUrl")
        if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            return None
        return _extension_parser(source, body, day), body
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save_extension(root: Path, batch: extensions.ExtensionBatch) -> None:
    artifact = batch.artifact
    folder = _extension_folder(root, batch.period)
    _atomic(folder / f"{artifact.source}.raw", artifact.body)
    entry = {"source": artifact.source, "requestedPeriod": batch.period,
             "requestedUrl": artifact.requested_url, "requestForm": artifact.request_form,
             "responseUrl": artifact.response_url, "fetchedAt": artifact.fetched_at,
             "status": 200, "sha256": artifact.sha256}
    _atomic(folder / f"{artifact.source}.json", json.dumps(entry, ensure_ascii=False,
                                                             sort_keys=True).encode("utf-8"))


def _merge_extensions(
    root: Path, day: str, session: requests.Session,
    records: dict[tuple[str, str], dict[str, Decimal | None]],
) -> tuple[dict[str, bytes], list[str]]:
    blobs: dict[str, bytes] = {}
    errors = []
    for source in _EXTENSION_NAMES:
        try:
            cached = _read_extension(root, day, source)
            if cached is None:
                batch = getattr(extensions, f"fetch_{source}")(day, session=session)
                fetched, body = batch.records, batch.artifact.body
            else:
                batch = None
                fetched, body = cached
            if source in {"sse_fund_daily", "szse_fund_daily"}:
                venue = "SSE" if source.startswith("sse_") else "SZSE"
                separate = {("SSE", "FUND_REPO"), ("SSE", "FUND_MONEY")}
                for key, values in fetched.items():
                    if key[0] != venue:
                        raise ValueError(f"{source}: unexpected venue")
                    baseline = records.get(key)
                    if baseline is None and key not in separate:
                        raise ValueError(f"{source}: category not in same-day market overview")
                    if baseline is not None and baseline.get("turnoverYi") is None and key not in separate:
                        raise ValueError(f"{source}: market overview turnover missing")
                    if baseline is not None and baseline.get("turnoverYi") is not None:
                        _near(baseline["turnoverYi"] or Decimal(0), values["turnoverYi"],
                              f"{source} turnover crosscheck", Decimal("0.03"))
                for key, values in fetched.items():
                    if key in separate:
                        records.setdefault(key, {}).update(values)
                    else:
                        records[key]["volumeYi"] = values["volumeYi"]
            else:
                _near(records[("SSE", "BOND")]["capYi"] or Decimal(0),
                      fetched[("SSE", "BOND_CUSTODY_ALL")]["custodyFaceYi"],
                      "SSE custody face versus market overview")
                for key, values in fetched.items():
                    records.setdefault(key, {}).update(values)
            if batch is not None:
                _save_extension(root, batch)
            blobs[source] = body
        except (OSError, ValueError, TypeError, KeyError, requests.RequestException) as exc:
            errors.append(f"{source}:{type(exc).__name__}")
    if all(source in blobs for source in ("sse_fund_daily", "szse_fund_daily")):
        records[("SSE_SZSE", "FUND")]["volumeYi"] = (
            (records[("SSE", "FUND")]["volumeYi"] or Decimal(0)) +
            (records[("SZSE", "FUND")]["volumeYi"] or Decimal(0)))
    return blobs, errors


def _extensions_complete(store: MarketStore, root: Path, day: str) -> bool:
    cached = {source: _read_extension(root, day, source) for source in _EXTENSION_NAMES}
    if any(value is None for value in cached.values()):
        return False
    identifiers = (
        f"OFFICIAL_EXCHANGE_DAILY:CN.SSE_SZSE.MARKET.FUND:{day}:1d:VOLUME_YI",
        f"OFFICIAL_EXCHANGE_DAILY:CN.SSE.MARKET.BOND_CUSTODY_ALL:{day}:1d:CUSTODY_MARKET_VALUE_YI",
        f"OFFICIAL_EXCHANGE_DAILY:CN.SSE.MARKET.BOND_CUSTODY_ALL:{day}:1d:CUSTODY_FACE_VALUE_YI",
    )
    if not all(store.connection.execute("SELECT 1 FROM gold_metrics WHERE metric_id=?", [identifier]).fetchone()
               for identifier in identifiers):
        return False
    fund_extension = cached.get("sse_fund_daily")
    if fund_extension is None:
        return False
    fund_values, fund_raw = fund_extension
    signature = f"sse_fund_daily:{hashlib.sha256(fund_raw).hexdigest()}"
    for category in ("FUND_REPO", "FUND_MONEY"):
        values = fund_values.get(("SSE", category))
        if values is None:
            continue
        for field, metric in (("volumeYi", "VOLUME_YI"), ("turnoverYi", "TOTAL_AMOUNT_YI")):
            identifier = f"OFFICIAL_EXCHANGE_DAILY:CN.SSE.MARKET.{category}:{day}:1d:{metric}"
            row = store.connection.execute("SELECT value, calculation_method FROM gold_metrics WHERE metric_id=?",
                                           [identifier]).fetchone()
            if row is None or row[0] is None or signature not in str(row[1]):
                return False
            try:
                actual = Decimal(str(row[0]))
            except InvalidOperation:
                return False
            if not actual.is_finite() or abs(actual - values[field]) > Decimal("0.000001"):
                return False
    return True


def _gold_rows(day: str, records: dict[tuple[str, str], dict[str, Decimal | None]],
               bodies: dict[str, bytes]) -> list[dict[str, Any]]:
    signatures = ",".join(f"{source}:{hashlib.sha256(body).hexdigest()}"
                          for source, body in sorted(bodies.items()))
    rows = []
    for (venue, category), values in records.items():
        instrument = f"CN.{venue}.MARKET.{category}"
        scope = ("沪深京A股" if (venue, category) == ("ALL", "STOCK_A") else
                 "沪深京上市股票（沪深含B股）" if (venue, category) == ("ALL", "STOCK_LISTED") else
                 "沪深上市基金" if (venue, category) == ("SSE_SZSE", "FUND") else
                 "三所债券及ABS现券" if (venue, category) == ("ALL", "BOND_CASH") else
                 "沪深债券回购" if (venue, category) == ("SSE_SZSE", "BOND_REPO") else
                 f"{venue} {category}")
        for field, value in values.items():
            if value is None or field not in _FIELDS:
                continue
            key, name, unit = _FIELDS[field]
            if field == "volumeYi":
                unit = "亿份" if category.startswith("FUND") else "亿股"
            method = f"source=official-exchanges-v1; venue={venue}; category={category}; field={field}; raw_sha256={signatures}"
            rows.append(_metric("OFFICIAL_EXCHANGE_DAILY", instrument, day, "1d",
                                f"{scope}{name}({unit})", float(value),
                                f"{scope}同日官方日报原值；父子类别不重复相加，单位={unit}",
                                method, metric_key=key))
    return rows


def _published_complete(store: MarketStore, day: str, bodies: dict[str, bytes]) -> bool:
    if set(bodies) != set(_SOURCE_IDS):
        return False
    try:
        expected = _parse_day(day, bodies)
    except (ValueError, TypeError, KeyError):
        return False
    checks = (("ALL", "STOCK_A", "capYi", "TOTAL_MARKET_CAP_YI"),
              ("ALL", "STOCK_A", "floatCapYi", "FLOAT_MARKET_CAP_YI"),
              ("ALL", "STOCK_A", "turnoverYi", "TOTAL_AMOUNT_YI"),
              ("ALL", "STOCK_LISTED", "companyCount", "COMPANY_COUNT"),
              ("SSE_SZSE", "FUND", "capYi", "TOTAL_MARKET_CAP_YI"),
              ("ALL", "BOND_CASH", "turnoverYi", "TOTAL_AMOUNT_YI"))
    for venue, category, field, metric_key in checks:
        identifier = f"OFFICIAL_EXCHANGE_DAILY:CN.{venue}.MARKET.{category}:{day}:1d:{metric_key}"
        row = store.connection.execute("SELECT value, calculation_method FROM gold_metrics WHERE metric_id=?",
                                       [identifier]).fetchone()
        value = expected.get((venue, category), {}).get(field)
        if row is None or value is None or not str(row[1]).startswith("source=official-exchanges-v1;"):
            return False
        if abs(Decimal(str(row[0])) - value) > Decimal("0.000001"):
            return False
    return True


def sync_official_exchange_daily(
    data_root: Path, *, start_day: str, end_day: str,
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Write complete daily official rows, retaining failures as dated gaps."""

    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    today = datetime.now(ZoneInfo("Asia/Shanghai"))
    if start > end or end > today.date() or (end == today.date() and
                                           (today.hour, today.minute) < (15, 30)):
        raise ValueError("invalid or unfinished official daily date range")
    root = Path(data_root)
    owned = session is None
    session = session or requests.Session()
    written: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    closed: list[str] = []
    metric_count = 0
    store = MarketStore(root)
    try:
        for offset in range((end - start).days + 1):
            trade_date = start + timedelta(days=offset)
            day = trade_date.isoformat()
            if not is_verified_a_share_trading_day(trade_date):
                if is_known_a_share_closed_day(trade_date):
                    closed.append(day)
                else:
                    failed.append({"day": day, "error": "CALENDAR_UNVERIFIED"})
                continue
            bodies = _read_bronze(root, day)
            if (bodies is not None and _published_complete(store, day, bodies)
                    and _extensions_complete(store, root, day)):
                skipped.append(day)
                continue
            try:
                bodies = dict(bodies or {})
                artifacts = {}
                source_errors = []
                for source in _SOURCE_IDS:
                    if source in bodies:
                        continue
                    try:
                        item = _fetch(session, source, day)
                        artifacts[source] = item
                        bodies[source] = item.body
                    except (OSError, ValueError, requests.RequestException) as exc:
                        source_errors.append(f"{source}:{type(exc).__name__}")
                if artifacts:
                    _save_bronze(root, day, artifacts)
                missing_core = set(_SOURCE_IDS[:-1]) - bodies.keys()
                if missing_core:
                    failed.append({"day": day, "error": ",".join(source_errors) or
                                   f"missing_core:{','.join(sorted(missing_core))}"})
                    continue
                records = _parse_day(day, bodies)
                extension_blobs, extension_errors = _merge_extensions(root, day, session, records)
                metrics = _gold_rows(day, records, {**bodies, **extension_blobs})
                run_id = store.begin_run("collector:official-exchange-daily")
                try:
                    store.upsert_gold_metrics(metrics)
                    store.finish_run(run_id, "COMPLETE", f"{day} 三所官网日统计 {len(metrics)} 项")
                except Exception:
                    store.finish_run(run_id, "FAILED", f"{day} 官网日统计入库失败")
                    raise
                written.append(day)
                metric_count += len(metrics)
                if source_errors or extension_errors:
                    failed.append({"day": day, "error": ",".join([*source_errors, *extension_errors])})
            except (OSError, ValueError, TypeError, KeyError, requests.RequestException) as exc:
                failed.append({"day": day, "error": f"{type(exc).__name__}:{str(exc)[:140]}"})
        _update_backlog(root, "daily", completed=[*written, *skipped, *closed],
                        failed=failed, key="day")
        return {"writtenDays": written, "skippedDays": skipped, "closedDays": closed,
                "failedDays": failed, "metricCount": metric_count}
    finally:
        store.close()
        if owned:
            session.close()


def _month_start(month: str) -> date:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("month must be YYYY-MM")
    return date.fromisoformat(f"{month}-01")


def _add_month(month: date) -> date:
    return date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def sync_official_monthly_issuance(
    data_root: Path, *, start_month: str, end_month: str,
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Store BSE monthly issuance apart from daily trading and outstanding bonds."""

    start, end = _month_start(start_month), _month_start(end_month)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if start > end or end >= today.replace(day=1):
        raise ValueError("monthly issuance requires completed calendar months")
    root = Path(data_root)
    owned = session is None
    session = session or requests.Session()
    written: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    store = MarketStore(root)
    try:
        current = start
        while current <= end:
            month = current.strftime("%Y-%m")
            current = _add_month(current)
            folder = root / "bronze" / "official_exchange_monthly" / f"month={month}"
            manifest_path = folder / "manifest.json"
            evidence: bytes | None = None
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                evidence = (folder / "bse_bond_issuance_monthly.raw").read_bytes()
                if (manifest.get("month") != month or manifest.get("source") != "bse_bond_issuance_monthly"
                        or hashlib.sha256(evidence).hexdigest() != manifest.get("sha256")
                        or manifest.get("requestForm") != {"month": month}):
                    evidence = None
            except (OSError, ValueError, TypeError):
                evidence = None
            existing = store.connection.execute(
                "SELECT count(*) FROM gold_metrics WHERE metric_id LIKE ?",
                [f"OFFICIAL_EXCHANGE_MONTHLY:CN.BSE.MARKET.%:{month}:1mo:%"],
            ).fetchone()[0]
            if evidence is not None and existing == 10:
                skipped.append(month)
                continue
            try:
                if evidence is None:
                    batch = extensions.fetch_bse_bond_issuance_monthly(month, session=session)
                    evidence = batch.artifact.body
                    artifact = batch.artifact
                    entry = {"schema": "official-exchange-monthly-v1", "month": month,
                             "source": artifact.source, "requestedUrl": artifact.requested_url,
                             "requestForm": artifact.request_form, "responseUrl": artifact.response_url,
                             "fetchedAt": artifact.fetched_at, "status": 200,
                             "sha256": artifact.sha256}
                    _atomic(folder / "bse_bond_issuance_monthly.raw", evidence)
                    _atomic(manifest_path, json.dumps(entry, ensure_ascii=False,
                                                      sort_keys=True).encode("utf-8"))
                    records = batch.records
                else:
                    records = extensions.parse_bse_bond_issuance_monthly(evidence, month)
                signature = hashlib.sha256(evidence).hexdigest()
                metrics = []
                for (venue, category), values in records.items():
                    for field, value in values.items():
                        key, name, unit = _FIELDS[field]
                        method = (f"source=official-exchanges-v1; venue={venue}; category={category}; "
                                  f"field={field}; raw_sha256=bse_bond_issuance_monthly:{signature}")
                        metrics.append(_metric("OFFICIAL_EXCHANGE_MONTHLY", f"CN.{venue}.MARKET.{category}",
                                               month, "1mo", f"北交所{category}{name}({unit})", float(value),
                                               f"北交所{month}发行额；单位={unit}；不表示债券余额或日成交",
                                               method, metric_key=key))
                run_id = store.begin_run("collector:official-exchange-monthly-issuance")
                try:
                    store.upsert_gold_metrics(metrics)
                    store.finish_run(run_id, "COMPLETE", f"{month} 北交所月度发行 {len(metrics)} 项")
                except Exception:
                    store.finish_run(run_id, "FAILED", f"{month} 月度发行入库失败")
                    raise
                written.append(month)
            except (OSError, ValueError, TypeError, KeyError, requests.RequestException) as exc:
                failed.append({"month": month, "error": f"{type(exc).__name__}:{str(exc)[:140]}"})
        _update_backlog(root, "monthly", completed=[*written, *skipped],
                        failed=failed, key="month")
        return {"writtenMonths": written, "skippedMonths": skipped, "failedMonths": failed,
                "metricCount": len(written) * 10}
    finally:
        store.close()
        if owned:
            session.close()


def sync_official_exchange_due(
    data_root: Path, *, start_day: str, end_day: str,
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Refresh recent days and a bounded set of older unresolved days."""

    root = Path(data_root)
    older = [day for day in sorted(_load_backlog(root, "daily"))
             if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and day < start_day and day <= end_day][:10]
    result = sync_official_exchange_daily(root, start_day=start_day, end_day=end_day,
                                          session=session)
    result["retriedBacklogDays"] = older
    for day in older:
        retry = sync_official_exchange_daily(root, start_day=day, end_day=day, session=session)
        for key in ("writtenDays", "skippedDays", "closedDays", "failedDays"):
            result[key].extend(retry[key])
        result["metricCount"] += retry["metricCount"]
    result["backlogRemaining"] = len(_load_backlog(root, "daily"))
    return result


def sync_official_monthly_due(
    data_root: Path, *, start_month: str, end_month: str,
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Refresh completed months and older monthly publication failures."""

    root = Path(data_root)
    older = [month for month in sorted(_load_backlog(root, "monthly"))
             if re.fullmatch(r"\d{4}-\d{2}", month) and month < start_month and month <= end_month][:6]
    result = sync_official_monthly_issuance(root, start_month=start_month, end_month=end_month,
                                            session=session)
    result["retriedBacklogMonths"] = older
    for month in older:
        retry = sync_official_monthly_issuance(root, start_month=month, end_month=month,
                                               session=session)
        for key in ("writtenMonths", "skippedMonths", "failedMonths"):
            result[key].extend(retry[key])
        result["metricCount"] += retry["metricCount"]
    result["backlogRemaining"] = len(_load_backlog(root, "monthly"))
    return result
