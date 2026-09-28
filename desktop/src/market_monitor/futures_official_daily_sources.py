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
from .futures_exchange_daily import AUDITED_TURNOVER_EXCHANGES


PARSER_VERSION = "official-futures-daily-v2"
_DATED_CODE = re.compile(r"^([A-Z]{1,3})(\d{3,4})$")
_CFFEX_FUTURES_PRODUCTS = frozenset({"IF", "IH", "IC", "IM", "TS", "TF", "T", "TL"})
_INE_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})
_ONE_SIDED_QUANTITY_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "CFFEX"})
_MAX_ARCHIVE_BYTES = 20_000_000
_MAX_ENTRY_BYTES = 5_000_000
_DIRECT_URLS = {
    "SHFE": "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{day}.dat",
    "INE": "https://www.ine.cn/data/tradedata/future/dailydata/kx{day}.dat",
    "CZCE": "http://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataDaily.txt",
    "DCE": "http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
    "GFEX": "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList",
    "CFFEX": "http://www.cffex.com.cn/sj/historysj/{month}/zip/{month}.zip",
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
        return {"method": "POST", "url": url, "data": {"trade_date": day, "trade_type": "0"}}
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
        source_rows = _parse_shfe_family(raw)
    elif venue == "CZCE":
        _validate_czce_report_day(raw, day)
        source_rows = _parse_czce(raw)
    elif venue == "CFFEX":
        source_rows = _parse_cffex(raw, day.strftime("%Y%m%d"))
    elif venue == "DCE":
        source_rows = _parse_dce(raw)
    else:
        source_rows = _parse_gfex(raw)
    digest = sha256(raw).hexdigest()
    selected: dict[str, dict[str, Any]] = {}
    for source in source_rows:
        code = str(source.get("code") or "").strip().upper()
        match = _DATED_CODE.fullmatch(code)
        if match is None:
            continue
        product = match.group(1)
        if (venue == "SHFE" and product in _INE_PRODUCTS) or (venue == "INE" and product not in _INE_PRODUCTS):
            continue
        delivery = contract_delivery_month(code, venue, reference_day=day, last_trading_day=day)
        if delivery is None or delivery < day.replace(day=1):
            continue
        contract = f"{product}{delivery.year % 100:02d}{delivery.month:02d}"
        if contract in selected:
            raise ValueError(f"duplicate official daily contract: {venue}.{contract} on {trading_day}")
        volume = _number(source.get("volume"))
        open_interest = _number(source.get("open_interest"))
        settlement = _number(source.get("settlement"), positive=True)
        close = _number(source.get("close"), positive=True)
        if volume is None and open_interest is None and settlement is None:
            continue
        raw_turnover = _number(source.get("turnover"))
        amount_audited = (
            day >= date(2026, 1, 1)
            and venue in AUDITED_TURNOVER_EXCHANGES
            and raw_turnover is not None
        )
        selected[contract] = {
            "trade_date": trading_day, "exchange": venue,
            "contract_code": contract, "product_code": product,
            "close": close, "settlement": settlement,
            "volume": volume, "open_interest": open_interest,
            "turnover_raw": raw_turnover,
            "turnover_cny": raw_turnover * 10_000 if amount_audited else None,
            "turnover_unit": "CNY_10K" if amount_audited else None,
            "turnover_sidedness": "ONE_SIDED" if amount_audited else None,
            "source_url": source_url, "adapter": f"official-{venue.lower()}-daily-v2",
            "payload_sha256": digest, "retrieved_at": retrieved_at,
            "quality_status": "PASS",
            "volume_sidedness": "ONE_SIDED" if venue in _ONE_SIDED_QUANTITY_EXCHANGES else "UNVERIFIED",
            "open_interest_sidedness": "ONE_SIDED" if venue in _ONE_SIDED_QUANTITY_EXCHANGES else "UNVERIFIED",
            "turnover_quality_status": "PASS" if amount_audited else "UNVERIFIED",
            "parser_version": PARSER_VERSION,
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
            "close": row.get("收盘价"),
            "settlement": row.get("结算价"),
            "volume": row.get("成交量"),
            "open_interest": row.get("持仓量"),
            "turnover": row.get("成交额"),
        })
    return output


def _parse_dce(raw: bytes) -> list[dict[str, Any]]:
    items = _json_object(raw).get("data")
    if not isinstance(items, list):
        raise ValueError("DCE daily JSON lacks data array")
    return [{
        "code": item.get("contractId"), "close": item.get("close"),
        "settlement": item.get("clearPrice"), "volume": item.get("volumn"),
        "open_interest": item.get("openInterest"), "turnover": item.get("turnover"),
    } for item in items if isinstance(item, Mapping)]


def _parse_gfex(raw: bytes) -> list[dict[str, Any]]:
    items = _json_object(raw).get("data")
    if not isinstance(items, list):
        raise ValueError("GFEX daily JSON lacks data array")
    return [{
        "code": str(item.get("varietyOrder") or "") + str(item.get("delivMonth") or ""),
        "close": item.get("close"), "settlement": item.get("clearPrice"),
        "volume": item.get("volumn"), "open_interest": item.get("openInterest"),
        "turnover": item.get("turnover"),
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


__all__ = ("PARSER_VERSION", "official_daily_request", "parse_official_daily")
