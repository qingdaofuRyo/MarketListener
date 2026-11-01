"""Direct parsers for exchange-published dated-contract daily reports.

The transport returns the *original* response bytes.  This module never
interprets a listing page, a challenge page, or a source summary row as a
contract observation.
"""

from __future__ import annotations

import csv
from datetime import date
from hashlib import sha256
from io import BytesIO, StringIO
import json
import math
import re
from typing import Any, Mapping
import zipfile

from .futures import contract_delivery_month


PARSER_VERSION = "official-futures-daily-v4"
_DATED_CODE = re.compile(r"^([A-Z]{1,3})(\d{3,4})$")
_CFFEX_FUTURES_PRODUCTS = frozenset({"IF", "IH", "IC", "IM", "TS", "TF", "T", "TL"})
_INE_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})
_ONE_SIDED_QUANTITY_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"})
# The raw daily fields use ten-thousand CNY in the requested 2023+ period.
# Dated report headers and historical turnover/price cross-checks complement
# current official page notes where a historical page revision is unavailable.
TURNOVER_POLICIES = {
    "SHFE": {"effective_from": "2023-01-01", "unit": "CNY_10K", "factor": 10_000,
             "evidence": "https://www.shfe.com.cn/data/config/report_config_cn.dat",
             "evidence_sha256": "92d3c4527c37a250a11d0773e7ae1fc06305e833799b586b53505625c134f49c"},
    "INE": {"effective_from": "2023-01-01", "unit": "CNY_10K", "factor": 10_000,
            "evidence": "https://www.shfe.com.cn/data/config/report_config_cn.dat",
            "evidence_sha256": "92d3c4527c37a250a11d0773e7ae1fc06305e833799b586b53505625c134f49c"},
    "DCE": {"effective_from": "2023-01-01", "unit": "CNY_10K", "factor": 10_000,
            "evidence": "http://www.dce.com.cn/dce/channel/list/168.html",
            "evidence_sha256": "3ca7515cc0a9cbcff734d8fe093423bddde309453dc0002649f056e6c0bb3c23"},
    "CZCE": {"effective_from": "2023-01-01", "unit": "CNY_10K", "factor": 10_000,
             "evidence": "DATED_REPORT_HEADER", "evidence_sha256": "DATED_REPORT_SHA"},
    "GFEX": {"effective_from": "2023-01-01", "unit": "CNY_10K", "factor": 10_000,
             "evidence": "http://www.gfex.com.cn/gfex/xhtml/js/hqsj_tjsj.js",
             "evidence_sha256": "5d80fad436f01511718dd25e5c73437bc80146d1a8f8df49b23cf87de168f323"},
    "CFFEX": {"effective_from": "2023-01-01", "unit": "CNY_10K", "factor": 10_000,
              "evidence": "http://www.cffex.com.cn/cn/rtj.html",
              "evidence_sha256": "1b4e560815272ea196475f662d308de7b9bc5d2d0c47be098abac21a77a44b43"},
}
_MAX_ARCHIVE_BYTES = 20_000_000
_MAX_ENTRY_BYTES = 5_000_000
_DIRECT_URLS = {
    "SHFE": "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{day}.dat",
    "INE": "https://www.ine.cn/data/tradedata/future/dailydata/kx{day}.dat",
    "CZCE": "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataDaily.txt",
    "DCE": "http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
    "GFEX": "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList",
    "CFFEX": "http://www.cffex.com.cn/sj/historysj/{month}/zip/{month}.zip",
}
_GFEX_DAILY_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "http://www.gfex.com.cn",
    "Referer": "http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
    "X-Requested-With": "XMLHttpRequest",
}


def official_daily_request(exchange: str, trading_day: str) -> dict[str, Any]:
    """Describe one public source request without issuing it."""

    venue = exchange.upper()
    if venue not in _DIRECT_URLS:
        raise ValueError(f"unsupported official daily exchange: {exchange}")
    day = date.fromisoformat(trading_day).strftime("%Y%m%d")
    url = _DIRECT_URLS[venue].format(day=day, year=day[:4], month=day[:6])
    if venue == "DCE":
        return {
            "method": "POST", "url": url,
            "json": {
                "contractId": "", "lang": "zh", "optionSeries": "",
                "statisticsType": "0", "tradeDate": day,
                "tradeType": "1", "varietyId": "all",
            },
        }
    if venue == "GFEX":
        return {
            "method": "POST", "url": url,
            "data": {"trade_date": day, "trade_type": "0"},
            "headers": _GFEX_DAILY_HEADERS,
        }
    return {"method": "GET", "url": url}


def parse_official_daily(
    raw: bytes, *, exchange: str, trading_day: str, source_url: str,
    retrieved_at: str,
) -> list[dict[str, Any]]:
    """Normalize independently usable quote, quantity, and amount fields."""

    venue = exchange.upper()
    if venue not in _DIRECT_URLS:
        raise ValueError(f"unsupported official daily exchange: {exchange}")
    day = date.fromisoformat(trading_day)
    if not raw or len(raw) > _MAX_ARCHIVE_BYTES:
        raise ValueError("official daily response is empty or too large")
    if venue in {"SHFE", "INE"}:
        shfe_payload = _json_object(raw)
        reported_day = shfe_payload.get("report_date")
        if reported_day is not None and str(reported_day) != day.strftime("%Y%m%d"):
            raise ValueError("SHFE/INE daily report date differs from request")
        if reported_day is None:
            try:
                reported_day = date(int(shfe_payload["o_year"]), int(shfe_payload["o_month"]), int(shfe_payload["o_day"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("SHFE/INE daily report lacks an explicit valid date") from error
            if reported_day != day:
                raise ValueError("SHFE/INE daily report date differs from request")
        source_rows = _parse_shfe_family(raw)
    elif venue == "CZCE":
        _validate_czce_report_day(raw, day)
        source_rows = _parse_czce(raw)
    elif venue == "CFFEX":
        source_rows = _parse_cffex(raw, day.strftime("%Y%m%d"))
    elif venue == "DCE":
        source_rows = _parse_dce(raw)
    else:
        source_rows = _parse_gfex(raw, day.strftime("%Y%m%d"))
    digest = sha256(raw).hexdigest()
    selected: dict[str, dict[str, Any]] = {}
    for source in source_rows:
        source_code = str(source.get("code") or "").strip().upper()
        code = re.sub(r"^([A-Z]{1,3})(\d{4})F$", r"\1-F\2", source_code) if venue == "DCE" else source_code
        average = re.fullmatch(r"(L|V|PP)-F(\d{4})", code) if venue == "DCE" else None
        match = _DATED_CODE.fullmatch(code)
        if match is None and average is None:
            continue
        product = average.group(1) + "-F" if average else match.group(1)
        if (venue == "SHFE" and product in _INE_PRODUCTS) or (venue == "INE" and product not in _INE_PRODUCTS):
            continue
        delivery = contract_delivery_month(code, venue, reference_day=day, last_trading_day=day)
        if delivery is None or delivery < day.replace(day=1):
            continue
        if average and (day < date(2025, 10, 29) or delivery <= day.replace(day=1)):
            continue
        contract = f"{product}{delivery.year % 100:02d}{delivery.month:02d}"
        if contract in selected:
            raise ValueError(f"duplicate official daily contract: {venue}.{contract} on {trading_day}")
        volume = _number(source.get("volume"))
        open_interest = _number(source.get("open_interest"))
        settlement = _number(source.get("settlement"))
        close = _number(source.get("close"))
        if volume is None and open_interest is None and settlement is None:
            continue
        raw_turnover = _number(source.get("turnover"))
        policy = TURNOVER_POLICIES.get(venue)
        amount_audited = bool(
            policy and trading_day >= policy["effective_from"] and raw_turnover is not None
            and (venue != "CZCE" or source.get("turnover_unit") == policy["unit"])
        )
        selected[contract] = {
            "trade_date": trading_day, "exchange": venue,
            "contract_code": contract, "product_code": product,
            "close": close, "settlement": settlement,
            "volume": volume, "open_interest": open_interest,
            "turnover_raw": raw_turnover,
            "turnover_cny": raw_turnover * policy["factor"] if amount_audited else None,
            "turnover_unit": policy["unit"] if amount_audited else None,
            "turnover_sidedness": "ONE_SIDED" if amount_audited else None,
            "source_url": source_url, "adapter": f"official-{venue.lower()}-daily-v2",
            "payload_sha256": digest, "retrieved_at": retrieved_at,
            "quality_status": "PASS",
            "volume_sidedness": "ONE_SIDED" if venue in _ONE_SIDED_QUANTITY_EXCHANGES else "UNVERIFIED",
            "open_interest_sidedness": "ONE_SIDED" if venue in _ONE_SIDED_QUANTITY_EXCHANGES else "UNVERIFIED",
            "turnover_quality_status": "PASS" if amount_audited else "UNVERIFIED",
            "parser_version": PARSER_VERSION,
            "source_contract_code": str(source.get("code") or "").strip(),
            "source_product_name": source.get("name"),
            "delivery_month": delivery.strftime("%Y-%m"),
            "previous_settlement": _number(source.get("previous_settlement")),
            "open": _number(source.get("open")),
            "high": _number(source.get("high")),
            "low": _number(source.get("low")),
            "change_close": _signed_number(source.get("change_close")),
            "change_settlement": _signed_number(source.get("change_settlement")),
            "open_interest_change": _signed_number(source.get("open_interest_change")),
        }
    if not selected:
        raise ValueError(f"{venue} daily response has no valid dated futures contracts")
    return [selected[code] for code in sorted(selected)]


def _parse_shfe_family(raw: bytes) -> list[dict[str, Any]]:
    payload = _json_object(raw)
    items = payload.get("o_curinstrument")
    if not isinstance(items, list):
        raise ValueError("SHFE/INE daily JSON lacks o_curinstrument")
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        product_id = str(item.get("PRODUCTID") or "").upper().strip()
        product_class = str(item.get("PRODUCTCLASS") or "").strip()
        if product_id.endswith("_TAS") or (product_class and product_class != "1"):
            continue
        product = str(item.get("PRODUCTGROUPID") or product_id).split("_", 1)[0].upper().strip()
        month = str(item.get("DELIVERYMONTH") or "").strip()
        rows.append({
            "code": product + month, "close": item.get("CLOSEPRICE"),
            "settlement": item.get("SETTLEMENTPRICE"), "volume": item.get("VOLUME"),
            "open_interest": item.get("OPENINTEREST"), "turnover": item.get("TURNOVER"),
            "name": item.get("PRODUCTNAME"), "previous_settlement": item.get("PRESETTLEMENTPRICE"),
            "open": item.get("OPENPRICE"), "high": item.get("HIGHESTPRICE"), "low": item.get("LOWESTPRICE"),
            "change_close": item.get("ZD1_CHG"), "change_settlement": item.get("ZD2_CHG"),
            "open_interest_change": item.get("OPENINTERESTCHG"),
        })
    return rows


def _parse_czce(raw: bytes) -> list[dict[str, Any]]:
    text = _decode_text(raw)
    records = [[cell.strip() for cell in line.split("|")] for line in text.splitlines() if "|" in line]
    header_index = next((i for i, row in enumerate(records) if row and row[0] in {"合约代码", "品种月份", "品种代码"}), None)
    if header_index is None:
        raise ValueError("CZCE daily table lacks contract header")
    header = records[header_index]

    def cell(row: list[str], *prefixes: str) -> str | None:
        for index, name in enumerate(header):
            if any(name.startswith(prefix) for prefix in prefixes):
                return row[index] if index < len(row) else None
        return None

    return [{
        "code": row[0], "close": cell(row, "今收盘"),
        "settlement": cell(row, "今结算"), "volume": cell(row, "成交量"),
        "open_interest": cell(row, "持仓量", "空盘量"),
        "turnover": cell(row, "成交额"),
        "turnover_unit": "CNY_10K" if any(name.startswith("成交额") and "万元" in name for name in header) else None,
        "previous_settlement": cell(row, "昨结算", "前结算"),
        "open": cell(row, "今开盘", "开盘价"), "high": cell(row, "最高价"), "low": cell(row, "最低价"),
        "change_close": cell(row, "涨跌1"), "change_settlement": cell(row, "涨跌2"),
        "open_interest_change": cell(row, "增减量", "持仓变化", "持仓量增减"),
    } for row in records[header_index + 1:] if row]


def _validate_czce_report_day(raw: bytes, expected_day: date) -> None:
    text = _decode_text(raw)
    match = re.search(r"(?<!\d)(20\d{2})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})日?(?!\d)", text)
    if match is None:
        raise ValueError("CZCE daily report has no explicit report date")
    actual = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if actual != expected_day:
        raise ValueError(f"CZCE report date {actual.isoformat()} does not match request {expected_day.isoformat()}")


def _parse_cffex(raw: bytes, day: str) -> list[dict[str, Any]]:
    if not zipfile.is_zipfile(BytesIO(raw)):
        raise ValueError("CFFEX monthly response is not a ZIP archive")
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        name = f"{day}_1.csv"
        if name not in archive.namelist() or archive.getinfo(name).file_size > _MAX_ENTRY_BYTES:
            raise ValueError(f"CFFEX monthly archive lacks safe {name}")
        text = _decode_text(archive.read(name))
    rows = csv.DictReader(StringIO(text))
    output: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("合约代码") or "").strip().upper()
        match = _DATED_CODE.fullmatch(code)
        if match is None or match.group(1) not in _CFFEX_FUTURES_PRODUCTS:
            continue
        output.append({
            "code": code,
            "close": row.get("今收盘") or row.get("收盘价"),
            "settlement": row.get("今结算") or row.get("结算价"),
            "volume": row.get("成交量"),
            "open_interest": row.get("持仓量"),
            "turnover": row.get("成交金额") or row.get("成交额"),
            "previous_settlement": row.get("前结算") or row.get("昨结算"),
            "open": row.get("今开盘") or row.get("开盘价"),
            "high": row.get("最高价"), "low": row.get("最低价"),
            "change_close": row.get("涨跌1"), "change_settlement": row.get("涨跌2"),
            "open_interest_change": row.get("持仓变化") or row.get("持仓增减"),
        })
    return output


def _parse_dce(raw: bytes) -> list[dict[str, Any]]:
    payload = _json_object(raw)
    if payload.get("code") != 200 or payload.get("success") is not True:
        raise ValueError("DCE daily JSON business status is not successful")
    items = payload.get("data")
    if not isinstance(items, list):
        raise ValueError("DCE daily JSON lacks data array")
    return [{
        "code": item.get("contractId"), "close": item.get("close"),
        "settlement": item.get("clearPrice"), "volume": item.get("volumn"),
        "open_interest": item.get("openInterest"), "turnover": item.get("turnover"),
        "name": item.get("variety"), "previous_settlement": item.get("lastClear"),
        "open": item.get("open"), "high": item.get("high"), "low": item.get("low"),
        "change_close": item.get("diff"), "change_settlement": item.get("diff1"),
        "open_interest_change": item.get("diffI"),
    } for item in items if isinstance(item, Mapping)]


def _parse_gfex(raw: bytes, trading_day: str) -> list[dict[str, Any]]:
    payload = _json_object(raw)
    if str(payload.get("code")) != "0":
        raise ValueError("GFEX daily JSON business status is not successful")
    parameters = payload.get("param")
    if isinstance(parameters, Mapping) and parameters.get("trade_date") != [trading_day]:
        raise ValueError("GFEX daily JSON date differs from request")
    items = payload.get("data")
    if not isinstance(items, list):
        raise ValueError("GFEX daily JSON lacks data array")
    return [{
        "code": str(item.get("varietyOrder") or "") + str(item.get("delivMonth") or ""),
        "close": item.get("close"), "settlement": item.get("clearPrice"),
        "volume": item.get("volumn"), "open_interest": item.get("openInterest"),
        "turnover": item.get("turnover"),
        "name": item.get("variety"), "previous_settlement": item.get("lastClear"),
        "open": item.get("open"), "high": item.get("high"), "low": item.get("low"),
        "change_close": item.get("diff"), "change_settlement": item.get("diff1"),
        "open_interest_change": item.get("diffI"),
    } for item in items if isinstance(item, Mapping)]


def _json_object(raw: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("official daily response is not JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("official daily response is not a JSON object")
    return payload


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("official daily text encoding is unsupported")


def _number(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None


def _signed_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = ("PARSER_VERSION", "official_daily_request", "parse_official_daily")
