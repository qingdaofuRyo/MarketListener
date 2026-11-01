"""Independent official fund, bond custody and bond issuance observations.

Daily fund volume and SSE bond custody are source-specific facts. BSE bond
issuance is monthly; it must never be displayed as a daily outstanding amount.
The caller owns Gold publication and can keep these optional sources isolated
from the main three-exchange stock update.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import openpyxl
import requests


SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_REPORT_URL = "https://www.szse.cn/api/report/ShowReport"
BSE_ISSUANCE_URL = "https://www.bseinfo.net/bondStatController/bondStatMonthlyReport.do"
_CALLBACK = "mlOfficialBondFund"
_SSE_BOND_TYPES = {
    "10": "BOND_TREASURY",
    "20": "BOND_LOCAL_GOV",
    "30": "BOND_FINANCIAL",
    "40": "BOND_PUBLIC_CORP",
    "50": "BOND_PRIVATE_CORP",
    "60": "BOND_EXCHANGEABLE",
    "80": "BOND_CONVERTIBLE",
    "90": "BOND_ABS",
    "9999": "BOND_CUSTODY_ALL",
}
_SSE_FUND_TYPES = {"05": "FUND", "13": "FUND_ETF", "14": "FUND_LOF", "16": "FUND_REIT"}
# The official daily page identifies 12 as fund repo and 15 as traded money
# market funds. Since 2022-09-05 the latter is included in ETF, not a peer to
# be added to the ETF total. Fund repo remains outside the cash-fund total.
_SSE_FUND_ADDITIONAL_TYPES = {"12": "FUND_REPO", "15": "FUND_MONEY"}
_SSE_MONEY_ETF_INCLUSION_DAY = date(2022, 9, 5)
_SSE_CASH_FUND_TOTAL_START_DAY = date(2019, 7, 22)
_SSE_FUND_NUMERIC_FIELDS = ("TOTAL_VALUE", "TRADE_VOL", "TOTAL_TO_RATE", "TRADE_AMT",
                            "TRADE_NUM", "NEGO_VALUE", "NEGO_TO_RATE", "LIST_NUM")
_SZSE_FUND_TYPES = {"ETF": "FUND_ETF", "LOF": "FUND_LOF",
                    "不动产基金": "FUND_REIT", "合计": "FUND"}
_BSE_ISSUANCE_TYPES = {
    "BOND_TREASURY": ("zfzq_by", "zfzq_bnlj"),
    "BOND_LOCAL_GOV": ("dfzq_by", "dfzq_bnlj"),
    "BOND_CORP": ("xyz_by", "xyz_bnlj"),
    "BOND_PUBLIC_CORP": ("gkfxgszq_by", "gkfxgszq_bnlj"),
    "BOND_PRIVATE_CORP": ("fgkfxgszq_by", "fgkfxgszq_bnlj"),
}


@dataclass(frozen=True)
class ExtensionArtifact:
    source: str
    requested_period: str
    requested_url: str
    request_form: dict[str, str] | None
    response_url: str
    fetched_at: str
    body: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True)
class ExtensionBatch:
    period: str
    frequency: str
    records: dict[tuple[str, str], dict[str, Decimal]]
    artifact: ExtensionArtifact


def _number(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None or str(value).strip() in {"", "-", "—"}:
        raise ValueError(f"{label}: missing")
    try:
        result = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{label}: invalid number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label}: invalid range")
    return result


def _same(actual: Decimal, expected: Decimal, label: str, *, tolerance: str = "0.03") -> None:
    if abs(actual - expected) > Decimal(tolerance):
        raise ValueError(f"{label}: parent and children disagree")


def _validate_day(day: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("day must be ISO YYYY-MM-DD")
    return day


def _validate_month(month: str) -> str:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("month must be YYYY-MM")
    return month


def _sse_rows(blob: bytes, day: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(blob)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("SSE response is not JSON") from exc
    if (not isinstance(payload, dict) or payload.get("actionErrors") not in (None, [])
            or not isinstance(payload.get("result"), list) or not payload["result"]):
        raise ValueError("SSE response has no verified result")
    rows = payload["result"]
    if any(not isinstance(row, dict) or row.get("TRADE_DATE") not in {day, day.replace("-", "")}
           for row in rows):
        raise ValueError("SSE row date disagrees with requested day")
    return rows


def parse_sse_fund_daily(blob: bytes, day: str) -> dict[tuple[str, str], dict[str, Decimal]]:
    """Read the official fund daily overview; TRADE_VOL is already 亿份."""

    day = _validate_day(day)
    if date.fromisoformat(day) < _SSE_CASH_FUND_TOTAL_START_DAY:
        raise ValueError("SSE fund total before 2019-07-22 has a different repo scope")
    rows = _sse_rows(blob, day)
    by_code = {str(row.get("PRODUCT_CODE")): row for row in rows}
    if len(by_code) != len(rows) or "05" not in by_code:
        raise ValueError("SSE fund category changed or total is absent")
    known_types = {**_SSE_FUND_TYPES, **_SSE_FUND_ADDITIONAL_TYPES}
    for code in set(by_code) - set(known_types):
        row = by_code[code]
        if (not re.fullmatch(r"\d{2}", code)
                or set(row) - {"PRODUCT_CODE", "TRADE_DATE", "AVG_PE_RATE",
                               *_SSE_FUND_NUMERIC_FIELDS}
                or any(_number(row.get(field), f"SSE fund {code} {field}") != 0
                       for field in _SSE_FUND_NUMERIC_FIELDS)
                or (row.get("AVG_PE_RATE") not in (None, "", "-", "—")
                    and _number(row["AVG_PE_RATE"], f"SSE fund {code} AVG_PE_RATE") != 0)):
            # A newly emitted empty category carries no value to classify yet.
            # Nonzero unknown rows need an explicit, reviewed parent/child mapping.
            raise ValueError("SSE fund category changed or total is absent")
    result = {
        ("SSE", known_types[code]): {
            "volumeYi": _number(row.get("TRADE_VOL"), f"SSE fund {code} volume"),
            "turnoverYi": _number(row.get("TRADE_AMT"), f"SSE fund {code} turnover"),
        }
        for code, row in by_code.items() if code in known_types
    }
    for code in set(by_code) & set(_SSE_FUND_ADDITIONAL_TYPES):
        # The page's displayed amount and volume are rounded. Validate other
        # supplied numeric fields, but do not infer the unit of TRADE_NUM.
        for field in _SSE_FUND_NUMERIC_FIELDS:
            if field in by_code[code]:
                _number(by_code[code][field], f"SSE fund {code} {field}")
    money = result.get(("SSE", "FUND_MONEY"))
    if money is not None and date.fromisoformat(day) >= _SSE_MONEY_ETF_INCLUSION_DAY:
        etf = by_code.get("13")
        if etf is None:
            raise ValueError("SSE traded money fund requires ETF parent")
        for field in ("LIST_NUM", "TOTAL_VALUE", "NEGO_VALUE", "TRADE_VOL", "TRADE_AMT"):
            if field not in by_code["15"] or field not in etf:
                continue
            child = _number(by_code["15"][field], f"SSE money fund {field}")
            parent = _number(etf[field], f"SSE ETF {field}")
            if child > parent + (Decimal(0) if field == "LIST_NUM" else Decimal("0.03")):
                raise ValueError(f"SSE money fund {field}: exceeds ETF parent")
    if set(_SSE_FUND_TYPES) <= set(by_code):
        for metric in ("volumeYi", "turnoverYi"):
            prior_money = (money[metric] if money is not None and
                           date.fromisoformat(day) < _SSE_MONEY_ETF_INCLUSION_DAY else Decimal(0))
            _same(result[("SSE", "FUND")][metric],
                  sum((result[("SSE", category)][metric] for category in
                       ("FUND_ETF", "FUND_LOF", "FUND_REIT")), Decimal(0)) + prior_money,
                  f"SSE fund {metric}")
    return result


def parse_szse_fund_daily(blob: bytes) -> dict[tuple[str, str], dict[str, Decimal]]:
    """Read XLSX values labelled 万份 and 万元; this report has no embedded date.

    The caller must compare the fund parent turnover with its separately dated
    SZSE market-overview response for the requested day before publication.
    """

    if not blob.startswith(b"PK") or len(blob) > 1_000_000:
        raise ValueError("SZSE fund response is not a bounded XLSX")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        book = openpyxl.load_workbook(io.BytesIO(blob), read_only=False, data_only=True)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("SZSE fund workbook has unexpected sheets")
        rows = list(book.active.values)
    finally:
        book.close()
    if not rows or tuple(str(value).strip() for value in rows[0][:3]) != (
        "基金品种", "成交量（万份）", "成交金额（万元）"
    ):
        raise ValueError("SZSE fund labels or units changed")
    result: dict[tuple[str, str], dict[str, Decimal]] = {}
    for row in rows[1:]:
        if not row or row[0] is None:
            continue
        label = str(row[0]).strip()
        if label not in _SZSE_FUND_TYPES:
            raise ValueError(f"SZSE fund category changed: {label}")
        key = ("SZSE", _SZSE_FUND_TYPES[label])
        if key in result:
            raise ValueError("SZSE fund category duplicated")
        result[key] = {
            "volumeYi": _number(row[1], f"SZSE {label} volume") / Decimal("10000"),
            "turnoverYi": _number(row[2], f"SZSE {label} turnover") / Decimal("10000"),
        }
    if set(result) != {("SZSE", key) for key in _SZSE_FUND_TYPES.values()}:
        raise ValueError("SZSE fund categories incomplete")
    for metric in ("volumeYi", "turnoverYi"):
        _same(result[("SZSE", "FUND")][metric],
              sum((result[("SZSE", category)][metric] for category in
                   ("FUND_ETF", "FUND_LOF", "FUND_REIT")), Decimal(0)),
              f"SZSE fund {metric}", tolerance="0.00001")
    return result


def parse_sse_bond_custody_daily(blob: bytes, day: str) -> dict[tuple[str, str], dict[str, Decimal]]:
    """Return market value and face value separately; both are in 亿元."""

    rows = _sse_rows(blob, _validate_day(day))
    by_code = {str(row.get("TYPE_CODE")): row for row in rows}
    if len(by_code) != len(rows) or set(by_code) != set(_SSE_BOND_TYPES):
        raise ValueError("SSE bond custody categories incomplete")
    result = {
        ("SSE", category): {
            "custodyValueYi": _number(by_code[code].get("MARKET_VAL"), f"SSE bond {code} value"),
            "custodyFaceYi": _number(by_code[code].get("FACE_VAL"), f"SSE bond {code} face"),
            "securityCount": _number(by_code[code].get("TOTAL_NUM"), f"SSE bond {code} count"),
        }
        for code, category in _SSE_BOND_TYPES.items()
    }
    for metric in ("custodyValueYi", "custodyFaceYi", "securityCount"):
        _same(result[("SSE", "BOND_CUSTODY_ALL")][metric],
              sum((result[("SSE", category)][metric] for code, category in
                   _SSE_BOND_TYPES.items() if code != "9999"), Decimal(0)),
              f"SSE bond custody {metric}", tolerance="0" if metric == "securityCount" else "0.05")
    return result


def parse_bse_bond_issuance_monthly(blob: bytes, month: str) -> dict[tuple[str, str], dict[str, Decimal]]:
    """Read monthly issuance (亿元), never a daily balance or trade value."""

    _validate_month(month)
    try:
        payload = blob.decode("utf-8").strip()
        match = re.fullmatch(re.escape(_CALLBACK) + r"\((\[.*\])\);?", payload, re.S)
        if match is None:
            raise ValueError("BSE monthly response is not JSONP")
        rows = json.loads(match.group(1))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("BSE monthly response is not JSONP") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("BSE monthly issuance is unpublished or malformed")
    row = rows[0]
    if row.get("rq") != month:
        raise ValueError("BSE monthly issuance period mismatch")
    result = {
        ("BSE", category): {
            "issuanceYi": _number(row.get(current), f"BSE {category} current issuance"),
            "issuanceYtdYi": _number(row.get(cumulative), f"BSE {category} YTD issuance"),
        }
        for category, (current, cumulative) in _BSE_ISSUANCE_TYPES.items()
    }
    for metric in ("issuanceYi", "issuanceYtdYi"):
        _same(result[("BSE", "BOND_CORP")][metric],
              result[("BSE", "BOND_PUBLIC_CORP")][metric] +
              result[("BSE", "BOND_PRIVATE_CORP")][metric],
              f"BSE corporation bond {metric}", tolerance="0.02")
    return result


def _fetch(
    session: requests.Session, source: str, period: str, *, url: str,
    params: dict[str, str], form: dict[str, str] | None,
    referer: str,
) -> ExtensionArtifact:
    method = "POST" if form is not None else "GET"
    response = session.request(method, url, params=params, data=form,
                               headers={"User-Agent": "Mozilla/5.0", "Referer": referer}, timeout=20)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError(f"{source}: unexpected HTTP status")
    if (urlsplit(response.url).scheme != "https"
            or urlsplit(response.url).hostname != urlsplit(url).hostname):
        raise ValueError(f"{source}: response left official host")
    body = response.content
    if not body or len(body) > 1_000_000:
        raise ValueError(f"{source}: unexpected response size")
    requested_url = requests.Request(method, url, params=params).prepare().url
    if requested_url is None:
        raise ValueError(f"{source}: invalid request URL")
    return ExtensionArtifact(source, period, requested_url, form, response.url,
                             datetime.now(timezone.utc).isoformat(timespec="seconds"), body)


def fetch_sse_fund_daily(day: str, session: requests.Session | None = None) -> ExtensionBatch:
    _validate_day(day)
    owned = session is None
    session = session or requests.Session()
    try:
        artifact = _fetch(session, "sse_fund_daily", day, url=SSE_QUERY_URL,
                          params={"sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
                                  "PRODUCT_CODE": "05,13,16,14,15,12", "type": "inParams",
                                  "SEARCH_DATE": day}, form=None, referer="https://www.sse.com.cn/")
        return ExtensionBatch(day, "1d", parse_sse_fund_daily(artifact.body, day), artifact)
    finally:
        if owned:
            session.close()


def fetch_szse_fund_daily(day: str, session: requests.Session | None = None) -> ExtensionBatch:
    _validate_day(day)
    owned = session is None
    session = session or requests.Session()
    try:
        artifact = _fetch(session, "szse_fund_daily", day, url=SZSE_REPORT_URL,
                          params={"SHOWTYPE": "xlsx", "CATALOGID": "scsj_jjrdgk",
                                  "TABKEY": "tab1", "txtQueryDate": day}, form=None,
                          referer="https://www.szse.cn/market/fund/dealSurvey/daily/index.html")
        return ExtensionBatch(day, "1d", parse_szse_fund_daily(artifact.body), artifact)
    finally:
        if owned:
            session.close()


def fetch_sse_bond_custody_daily(day: str, session: requests.Session | None = None) -> ExtensionBatch:
    _validate_day(day)
    owned = session is None
    session = session or requests.Session()
    try:
        artifact = _fetch(session, "sse_bond_custody_daily", day, url=SSE_QUERY_URL,
                          params={"sqlId": "COMMON_SSEBOND_SCSJ_SCTJ_SCGL_ZQXQSCGL_CX_L",
                                  "TRADE_DATE": day}, form=None,
                          referer="https://bond.sse.com.cn/")
        return ExtensionBatch(day, "1d", parse_sse_bond_custody_daily(artifact.body, day), artifact)
    finally:
        if owned:
            session.close()


def fetch_bse_bond_issuance_monthly(month: str, session: requests.Session | None = None) -> ExtensionBatch:
    _validate_month(month)
    owned = session is None
    session = session or requests.Session()
    try:
        artifact = _fetch(session, "bse_bond_issuance_monthly", month, url=BSE_ISSUANCE_URL,
                          params={"callback": _CALLBACK}, form={"month": month},
                          referer="https://www.bseinfo.net/static/bond_statisticdata.html")
        return ExtensionBatch(month, "1mo", parse_bse_bond_issuance_monthly(artifact.body, month), artifact)
    finally:
        if owned:
            session.close()


__all__ = (
    "ExtensionArtifact", "ExtensionBatch", "fetch_sse_fund_daily", "fetch_szse_fund_daily",
    "fetch_sse_bond_custody_daily", "fetch_bse_bond_issuance_monthly",
    "parse_sse_fund_daily", "parse_szse_fund_daily", "parse_sse_bond_custody_daily",
    "parse_bse_bond_issuance_monthly",
)
