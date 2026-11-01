"""Source-isolated, exact-day official speculative futures margin parameters.

Contract-specification minimum margins and broker rule summaries are deliberately
excluded. The scalar used by the market list is max(long, short), not a two-sided
capital estimate. A missing side leaves that scalar unavailable.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from io import StringIO
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import duckdb
import requests
from bs4 import BeautifulSoup

from .atomic_files import replace_with_retry
from .futures import contract_delivery_month
from .futures_calendar import load_futures_trading_calendar


PARSER_VERSION = "official-futures-settlement-v1"
MARGIN_POLICY = "MAX_SPEC"
EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX")
BRONZE_ROOT = Path("bronze") / "official_futures_settlement"
_INE_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})
_CFFEX_PRODUCTS = frozenset({"IF", "IH", "IC", "IM", "TS", "TF", "T", "TL"})
_HOSTS = {"SHFE": "www.shfe.com.cn", "INE": "www.ine.cn", "DCE": "www.dce.com.cn",
          "CZCE": "www.czce.com.cn", "GFEX": "www.gfex.com.cn", "CFFEX": "www.cffex.com.cn"}
_COLUMNS = (
    "trade_date", "exchange", "contract_code", "product_code", "source_contract_code",
    "spec_long_rate", "spec_short_rate", "standard_margin_rate", "margin_policy",
    "source_margin_unit", "spec_long_raw", "spec_short_raw", "settlement",
    "source_url", "payload_sha256", "retrieved_at", "parser_version", "quality_status",
    "effective_from", "effective_to", "interval_source_url", "interval_payload_sha256",
    "source_date_basis", "request_payload_json", "request_payload_sha256",
    "published_on",
)
_COVERAGE_COLUMNS = ("trade_date", "exchange", "status", "record_count", "usable_count",
                     "source_url", "payload_sha256", "retrieved_at", "parser_version", "error")
_MAX_BYTES = 20_000_000
_SOURCE_COLUMNS = ", ".join(_COLUMNS)


class SettlementSourceError(RuntimeError):
    """A sanitized source result that cannot be confused with margin facts."""

    def __init__(self, code: str, *, status: str = "FAILED") -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def ensure_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS futures_settlement_parameter_official (
            trade_date VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            contract_code VARCHAR NOT NULL, product_code VARCHAR NOT NULL,
            source_contract_code VARCHAR NOT NULL,
            spec_long_rate DOUBLE, spec_short_rate DOUBLE, standard_margin_rate DOUBLE,
            margin_policy VARCHAR NOT NULL, source_margin_unit VARCHAR NOT NULL,
            spec_long_raw VARCHAR, spec_short_raw VARCHAR, settlement DOUBLE,
            source_url VARCHAR NOT NULL, payload_sha256 VARCHAR NOT NULL,
            retrieved_at VARCHAR NOT NULL, parser_version VARCHAR NOT NULL,
            quality_status VARCHAR NOT NULL,
            effective_from VARCHAR NOT NULL, effective_to VARCHAR NOT NULL,
            interval_source_url VARCHAR, interval_payload_sha256 VARCHAR,
            source_date_basis VARCHAR, request_payload_json VARCHAR, request_payload_sha256 VARCHAR,
            published_on VARCHAR,
            PRIMARY KEY (trade_date, exchange, contract_code, source_url)
        )
    """)
    columns = {str(item[1]) for item in connection.execute("PRAGMA table_info('futures_settlement_parameter_official')").fetchall()}
    missing = [column for column in ("source_date_basis", "request_payload_json", "request_payload_sha256", "published_on")
               if column not in columns]
    if missing:
        # DuckDB blocks ALTER TABLE while a secondary index depends on it.
        # Recreate this module's index after the compatible column additions.
        connection.execute("DROP INDEX IF EXISTS settlement_parameter_day_idx")
        for column in missing:
            connection.execute(f"ALTER TABLE futures_settlement_parameter_official ADD COLUMN {column} VARCHAR")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS futures_settlement_parameter_coverage (
            trade_date VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            status VARCHAR NOT NULL, record_count INTEGER NOT NULL,
            usable_count INTEGER NOT NULL, source_url VARCHAR NOT NULL,
            payload_sha256 VARCHAR, retrieved_at VARCHAR NOT NULL,
            parser_version VARCHAR NOT NULL, error VARCHAR,
            PRIMARY KEY (trade_date, exchange, source_url)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS settlement_parameter_day_idx ON "
                       "futures_settlement_parameter_official(trade_date, exchange, contract_code)")


def _safe_url(value: str, exchange: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or parts.hostname != _HOSTS[exchange] or parts.username:
        raise ValueError("settlement source must use the exchange official host")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() in {"", "-", "--", "null"}:
        return None
    try:
        result = float(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        raise ValueError("invalid numeric settlement parameter") from None
    if not math.isfinite(result):
        raise ValueError("non-finite settlement parameter")
    return result


def _rate(value: Any, unit: str) -> float | None:
    if unit not in {"DECIMAL", "PERCENT"}:
        raise ValueError("margin unit must be explicitly DECIMAL or PERCENT")
    text = str(value).strip() if value is not None else ""
    if unit == "DECIMAL" and ("%" in text or "％" in text):
        raise ValueError("percent margin conflicts with decimal source policy")
    numeric = _number(text.replace("%", "").replace("％", ""))
    if numeric is None:
        return None
    result = numeric / 100 if unit == "PERCENT" else numeric
    if not 0 <= result <= 1:
        raise ValueError("margin rate must be in [0, 1]")
    return result


def _text(raw: bytes, required: str) -> str:
    if not raw or len(raw) > _MAX_BYTES:
        raise ValueError("settlement response is empty or too large")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if required in text:
            return text
    raise ValueError("settlement response lacks verified official headings")


def _json(raw: bytes) -> Mapping[str, Any]:
    if not raw or len(raw) > _MAX_BYTES or raw.lstrip().startswith(b"<"):
        raise ValueError("settlement response is not JSON")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("settlement response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("settlement response must be an object")
    return payload


def _day_text(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    text = str(value or "").strip()
    if re.fullmatch(r"20\d{6}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _payload_day(payload: Mapping[str, Any]) -> str | None:
    for key in ("report_date", "tradeDate", "trade_date", "date"):
        if result := _day_text(payload.get(key)):
            return result
    param = payload.get("param")
    if isinstance(param, Mapping):
        return _payload_day(param)
    return _day_text(param)


def _normalize_code(source: Any, exchange: str, day: str) -> tuple[str, str] | None:
    code = str(source or "").strip().upper()
    if exchange == "DCE":
        code = re.sub(r"^(L|V|PP)(\d{4})F$", r"\1-F\2", code)
    matched = re.fullmatch(r"([A-Z]{1,3})(\d{3,4})", code)
    average = re.fullmatch(r"(L|V|PP)-F(\d{4})", code) if exchange == "DCE" else None
    if matched is None and average is None:
        return None
    product = average.group(1) + "-F" if average else matched.group(1)
    if exchange == "CFFEX" and product not in _CFFEX_PRODUCTS:
        return None
    if (exchange == "SHFE" and product in _INE_PRODUCTS) or (exchange == "INE" and product not in _INE_PRODUCTS):
        return None
    trading = date.fromisoformat(day)
    delivery = contract_delivery_month(code, exchange, reference_day=trading)
    if delivery is None:
        return None
    return f"{product}{delivery.year % 100:02d}{delivery.month:02d}", product


@lru_cache(maxsize=16)
def _payload_digest(raw: bytes) -> str:
    # Each original is shared by hundreds of contract rows. Hash it once,
    # retaining only a small bounded number of concurrently parsed originals.
    return sha256(raw).hexdigest()


def _row(*, source_code: Any, long: Any, short: Any, unit: str, exchange: str,
         trading_day: str, raw: bytes, source_url: str, retrieved_at: str,
         settlement: Any = None) -> dict[str, Any] | None:
    identity = _normalize_code(source_code, exchange, trading_day)
    if identity is None:
        return None
    long_rate, short_rate = _rate(long, unit), _rate(short, unit)
    scalar = max(long_rate, short_rate) if long_rate is not None and short_rate is not None else None
    return {
        "trade_date": trading_day, "exchange": exchange, "contract_code": identity[0],
        "product_code": identity[1], "source_contract_code": str(source_code).strip(),
        "spec_long_rate": long_rate, "spec_short_rate": short_rate,
        "standard_margin_rate": scalar, "margin_policy": MARGIN_POLICY,
        "source_margin_unit": unit, "spec_long_raw": str(long) if long is not None else None,
        "spec_short_raw": str(short) if short is not None else None,
        "settlement": _number(settlement), "source_url": _safe_url(source_url, exchange),
        "payload_sha256": _payload_digest(raw), "retrieved_at": retrieved_at,
        "parser_version": PARSER_VERSION, "quality_status": "PASS" if scalar is not None else "PARTIAL",
        "effective_from": trading_day, "effective_to": trading_day,
        "interval_source_url": None, "interval_payload_sha256": None,
        "published_on": None,
        "source_date_basis": "REPORTED_DATE", "request_payload_json": None,
        "request_payload_sha256": None,
    }


def _finish(rows: Sequence[dict[str, Any] | None]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row is None:
            continue
        if row["contract_code"] in selected:
            raise ValueError("duplicate normalized settlement contract")
        selected[row["contract_code"]] = row
    if not selected:
        raise ValueError("no dated futures settlement parameters")
    return [selected[key] for key in sorted(selected)]


def parse_shfe(raw: bytes, *, trading_day: str, source_url: str, retrieved_at: str,
               exchange: str = "SHFE") -> list[dict[str, Any]]:
    if exchange not in {"SHFE", "INE"}:
        raise ValueError("SHFE parser supports SHFE and INE")
    payload = _json(raw)
    if _payload_day(payload) != trading_day:
        raise ValueError("settlement report date differs from request")
    rows = payload.get("o_cursor")
    if not isinstance(rows, list):
        raise ValueError("missing SHFE settlement rows")
    return _finish([_row(source_code=item.get("INSTRUMENTID"),
                        long=item.get("SPECLONGMARGINRATIO"), short=item.get("SPECSHORTMARGINRATIO"),
                        unit="DECIMAL", exchange=exchange, trading_day=trading_day,
                        raw=raw, source_url=source_url, retrieved_at=retrieved_at,
                        settlement=item.get("SETTLEMENTPRICE"))
                    for item in rows if isinstance(item, Mapping)])


def parse_gfex(raw: bytes, *, trading_day: str, source_url: str,
               retrieved_at: str) -> list[dict[str, Any]]:
    payload = _json(raw)
    if str(payload.get("code")) != "0" or _payload_day(payload) != trading_day:
        raise ValueError("GFEX settlement status or reported date differs")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("missing GFEX settlement rows")
    return _finish([_row(source_code=item.get("contractId"), long=item.get("specBuyRate"),
                        short=item.get("specSellRate"), unit="DECIMAL", exchange="GFEX",
                        trading_day=trading_day, raw=raw, source_url=source_url,
                        retrieved_at=retrieved_at, settlement=item.get("clearPrice"))
                    for item in rows if isinstance(item, Mapping)])


def parse_czce(raw: bytes, *, trading_day: str, source_url: str,
               retrieved_at: str) -> list[dict[str, Any]]:
    text = _text(raw, "交易保证金率")
    day_match = re.search(r"期货结算参数表\s*[（(](20\d{2}-\d{2}-\d{2})[）)]", text)
    if day_match is None or day_match.group(1) != trading_day:
        raise ValueError("CZCE settlement report date differs from request")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header_index = next((i for i, line in enumerate(lines) if "合约代码" in line and "交易保证金率(%)" in line), None)
    if header_index is None:
        raise ValueError("CZCE settlement percentage heading missing")
    headers = [cell.strip() for cell in lines[header_index].split("|")]
    rows: list[dict[str, Any] | None] = []
    for line in lines[header_index + 1:]:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < len(headers):
            continue
        item = dict(zip(headers, cells))
        rate = item.get("交易保证金率(%)")
        rows.append(_row(source_code=item.get("合约代码"), long=rate, short=rate, unit="PERCENT",
                         exchange="CZCE", trading_day=trading_day, raw=raw,
                         source_url=source_url, retrieved_at=retrieved_at,
                         settlement=item.get("当日结算价")))
    return _finish(rows)


def parse_cffex(raw: bytes, *, trading_day: str, source_url: str,
                retrieved_at: str, interval_evidence: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    text = _text(raw, "合约多头保证金标准")
    if text.lstrip().startswith("<"):
        raise ValueError("CFFEX returned HTML instead of CSV")
    match = re.search(r"期货合约结算业务参数表\s*[（(](20\d{6})[）)]", text)
    source_day = _day_text(match.group(1)) if match else None
    if source_day is None:
        raise ValueError("CFFEX CSV lacks publication date")
    start, end = source_day, source_day
    interval_url = interval_sha = None
    if interval_evidence is not None:
        # An application-reviewed official declaration is required. A previous
        # CSV or a next-publication date alone cannot establish an interval.
        evidence_raw = interval_evidence.get("raw")
        start = _day_text(interval_evidence.get("effective_from"))
        end = _day_text(interval_evidence.get("effective_to"))
        if (not isinstance(evidence_raw, bytes) or not start or not end or start > end
                or interval_evidence.get("explicit_declaration_verified") is not True):
            raise ValueError("CFFEX interval requires verified explicit official declaration")
        interval_url = _safe_url(str(interval_evidence.get("source_url") or ""), "CFFEX")
        interval_sha = sha256(evidence_raw).hexdigest()
        if interval_evidence.get("payload_sha256") != interval_sha:
            raise ValueError("CFFEX interval evidence SHA mismatch")
    if not start <= trading_day <= end:
        raise ValueError("CFFEX publication has no verified effective interval for request")
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if "合约多头保证金标准" in line)
    reader = csv.DictReader(StringIO("\n".join(lines[index:])))
    rows = _finish([_row(source_code=item.get("期货合约"), long=item.get("合约多头保证金标准"),
                         short=item.get("合约空头保证金标准"), unit="PERCENT", exchange="CFFEX",
                         trading_day=trading_day, raw=raw, source_url=source_url, retrieved_at=retrieved_at)
                    for item in reader])
    for row in rows:
        row.update(effective_from=start, effective_to=end,
                   interval_source_url=interval_url, interval_payload_sha256=interval_sha)
    return rows


def parse_dce(raw: bytes, *, trading_day: str, source_url: str,
              retrieved_at: str, margin_unit: str | None = None,
              request_evidence: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read only observed official settlement field names and declared units.

    DCE's transport is injected until the 181-page request has been verified;
    the daily quotation API must not be mislabeled as settlement parameters.
    """
    if raw.lstrip().startswith(b"{"):
        payload = _json(raw)
        if str(payload.get("code")) != "200" or payload.get("success") is not True:
            raise ValueError("DCE settlement response status is not successful")
        reported_day = _payload_day(payload)
        submitted_raw = None
        if reported_day != trading_day:
            if reported_day is not None:
                raise ValueError("DCE settlement reported date differs from request")
            submitted_raw = _verified_dce_request(request_evidence, trading_day, source_url)
        if margin_unit not in {"DECIMAL", "PERCENT"}:
            raise ValueError("DCE JSON requires verified margin field unit")
        source_rows = payload.get("data")
        if not isinstance(source_rows, list):
            raise ValueError("DCE settlement row array missing")
        rows = _finish([_row(source_code=item.get("contractId"), long=item.get("specBuyRate"),
                            short=item.get("specSellRate"), unit=margin_unit, exchange="DCE",
                            trading_day=trading_day, raw=raw, source_url=source_url,
                            retrieved_at=retrieved_at, settlement=item.get("clearPrice"))
                        for item in source_rows if isinstance(item, Mapping)])
        if submitted_raw is not None:
            for row in rows:
                row.update(source_date_basis=("AUTHORIZED_API_REQUEST_BODY" if
                           (request_evidence or {}).get("transport_kind") == "AUTHORIZED_OFFICIAL_API"
                           else "VERIFIED_REQUEST_BODY"), request_payload_json=submitted_raw,
                           request_payload_sha256=sha256(submitted_raw.encode("utf-8")).hexdigest())
        return rows
    text = _text(raw, "保证金")
    if "交易保证金率(%)" not in text:
        raise ValueError("DCE export lacks verified speculative percentage heading")
    dates = re.findall(r"20\d{2}-\d{2}-\d{2}|20\d{6}", text[:2000])
    if trading_day not in {_day_text(value) for value in dates}:
        raise ValueError("DCE export date differs from request")
    lines = text.splitlines()
    index = next((i for i, line in enumerate(lines) if "合约代码" in line and "交易保证金率(%)" in line), None)
    if index is None:
        raise ValueError("DCE export header missing")
    delimiter = "\t" if "\t" in lines[index] else "|" if "|" in lines[index] else ","
    reader = csv.DictReader(StringIO("\n".join(lines[index:])), delimiter=delimiter)
    return _finish([_row(source_code=item.get("合约代码"), long=item.get("交易保证金率(%)"),
                        short=item.get("交易保证金率(%)"), unit="PERCENT", exchange="DCE",
                        trading_day=trading_day, raw=raw, source_url=source_url,
                        retrieved_at=retrieved_at, settlement=item.get("结算价")) for item in reader])


def _verified_dce_request(evidence: Mapping[str, Any] | None, trading_day: str,
                          source_url: str) -> str:
    if not isinstance(evidence, Mapping) or evidence.get("trade_date_verified") is not True:
        raise ValueError("DCE settlement without date requires verified Network request evidence")
    url = _safe_url(source_url, "DCE")
    path = urlsplit(url).path
    authorized = (path == "/dceapi/forward/publicweb/tradepara/futAndOptSettle"
                  and evidence.get("transport_kind") == "AUTHORIZED_OFFICIAL_API")
    if (path != "/dcereport/publicweb/tradepara/futAndOptSettle" and not authorized
            or evidence.get("method") != "POST"
            or _safe_url(str(evidence.get("source_url") or ""), "DCE") != url):
        raise ValueError("DCE settlement request URL/method evidence differs")
    post_data = evidence.get("post_data")
    if not isinstance(post_data, str) or sha256(post_data.encode("utf-8")).hexdigest() != evidence.get("post_data_sha256"):
        raise ValueError("DCE Network request body SHA mismatch")
    expected = {"varietyId": "all", "tradeDate": trading_day.replace("-", ""), "tradeType": "1", "lang": "zh"}
    if json.loads(post_data) != expected:
        raise ValueError("DCE Network request body does not identify requested settlement day")
    return post_data


def parse_settlement_parameters(raw: bytes, *, exchange: str, trading_day: str,
                                source_url: str, retrieved_at: str,
                                margin_unit: str | None = None,
                                request_evidence: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    venue = exchange.upper()
    kwargs = dict(trading_day=trading_day, source_url=source_url, retrieved_at=retrieved_at)
    if venue in {"SHFE", "INE"}:
        return parse_shfe(raw, exchange=venue, **kwargs)
    if venue == "GFEX":
        return parse_gfex(raw, **kwargs)
    if venue == "CZCE":
        return parse_czce(raw, **kwargs)
    if venue == "CFFEX":
        return parse_cffex(raw, **kwargs)
    if venue == "DCE":
        return parse_dce(raw, margin_unit=margin_unit, request_evidence=request_evidence, **kwargs)
    raise ValueError("unsupported settlement exchange")


def import_partition(connection: Any, rows: Sequence[Mapping[str, Any]], *, exchange: str,
                     trade_date: str, source_url: str, payload_sha256: str | None,
                     retrieved_at: str, status: str | None = None, error: str | None = None) -> int:
    """Atomically replace one exact official source partition, never a broker row."""
    conn = getattr(connection, "connection", connection)
    ensure_schema(conn)
    source_url = _safe_url(source_url, exchange)
    if status is None:
        status = "PASS" if rows and all(row.get("quality_status") == "PASS" for row in rows) else "PARTIAL" if rows else "FAILED"
    if status not in {"PASS", "PARTIAL", "FAILED", "DEFERRED", "NO_PUBLICATION"}:
        raise ValueError("invalid settlement coverage status")
    if rows and status not in {"PASS", "PARTIAL"}:
        raise ValueError("failed source cannot import margin rows")
    if status == "PASS" and not rows:
        raise ValueError("PASS settlement coverage requires rows")
    if rows and (not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)):
        raise ValueError("settlement partition requires original SHA")
    for row in rows:
        if (row.get("trade_date") != trade_date or row.get("exchange") != exchange
                or row.get("source_url") != source_url or row.get("payload_sha256") != payload_sha256
                or row.get("parser_version") != PARSER_VERSION or row.get("margin_policy") != MARGIN_POLICY):
            raise ValueError("settlement rows differ from source partition")
        long = _rate(row.get("spec_long_rate"), "DECIMAL")
        short = _rate(row.get("spec_short_rate"), "DECIMAL")
        expected = max(long, short) if long is not None and short is not None else None
        if row.get("standard_margin_rate") != expected:
            raise ValueError("standard margin must follow MAX_SPEC")
        start, end = _day_text(row.get("effective_from")), _day_text(row.get("effective_to"))
        if not start or not end or start > end:
            raise ValueError("invalid settlement effective interval")
        if start != trade_date or end != trade_date:
            if exchange != "CFFEX" or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("interval_payload_sha256") or "")):
                raise ValueError("non-exact settlement interval requires official CFFEX evidence")
            _safe_url(str(row.get("interval_source_url") or ""), "CFFEX")
        if row.get("published_on") is not None and _day_text(row["published_on"]) != row["published_on"]:
            raise ValueError("invalid official publication date")
        if row.get("source_date_basis") in {"VERIFIED_REQUEST_BODY", "AUTHORIZED_API_REQUEST_BODY"}:
            _verified_dce_request({"trade_date_verified": True, "method": "POST", "source_url": source_url,
                                   "post_data": row.get("request_payload_json"),
                                   "post_data_sha256": row.get("request_payload_sha256"),
                                   "transport_kind": "AUTHORIZED_OFFICIAL_API" if
                                   row.get("source_date_basis") == "AUTHORIZED_API_REQUEST_BODY" else None}, trade_date, source_url)
    coverage = dict(trade_date=trade_date, exchange=exchange, status=status,
                    record_count=len(rows), usable_count=sum(row.get("quality_status") == "PASS" for row in rows),
                    source_url=source_url, payload_sha256=payload_sha256, retrieved_at=retrieved_at,
                    parser_version=PARSER_VERSION, error=error)
    if exchange == "DCE" and rows:
        tables = {item[0] for item in conn.execute("SHOW TABLES").fetchall()}
        if "futures_contract_daily_official" in tables:
            daily_prices = dict(conn.execute(
                "SELECT contract_code,max(settlement) FROM futures_contract_daily_official "
                "WHERE trade_date=? AND exchange='DCE' AND quality_status='PASS' "
                "AND settlement>0 GROUP BY contract_code", [trade_date],
            ).fetchall())
            for row in rows:
                reported_price = row.get("settlement")
                official_price = daily_prices.get(row["contract_code"])
                if reported_price is not None and reported_price > 0 and official_price is not None:
                    if not math.isclose(reported_price, official_price, rel_tol=1e-10, abs_tol=1e-8):
                        raise ValueError("DCE settlement date price differs from official daily report")
    prior = conn.execute(
        "SELECT status,record_count,usable_count,payload_sha256,parser_version,error "
        "FROM futures_settlement_parameter_coverage WHERE trade_date=? AND exchange=? AND source_url=?",
        [trade_date, exchange, source_url],
    ).fetchone()
    expected_coverage = (status, len(rows), coverage["usable_count"], payload_sha256, PARSER_VERSION, error)
    if prior == expected_coverage:
        saved_count = conn.execute(
            "SELECT count(*) FROM futures_settlement_parameter_official "
            "WHERE trade_date=? AND exchange=? AND source_url=? AND parser_version=?",
            [trade_date, exchange, source_url, PARSER_VERSION],
        ).fetchone()[0]
        if not rows or saved_count == len(rows):
            return 0
    try:
        conn.execute("BEGIN TRANSACTION")
        if rows:
            import pandas as pd

            conn.execute("DELETE FROM futures_settlement_parameter_official WHERE trade_date=? AND exchange=? AND source_url=?",
                         [trade_date, exchange, source_url])
            # One registered batch avoids one Python-to-DuckDB insert per
            # contract while retaining the exact source-day transaction.
            conn.register("_official_settlement_stage", pd.DataFrame(
                [{key: row.get(key) for key in _COLUMNS} for row in rows], columns=list(_COLUMNS),
            ))
            try:
                conn.execute(f"INSERT INTO futures_settlement_parameter_official ({_SOURCE_COLUMNS}) "
                             f"SELECT {_SOURCE_COLUMNS} FROM _official_settlement_stage")
            finally:
                conn.unregister("_official_settlement_stage")
        conn.execute("INSERT INTO futures_settlement_parameter_coverage (" + ", ".join(_COVERAGE_COLUMNS)
                     + ") VALUES (" + ", ".join("?" for _ in _COVERAGE_COLUMNS) + ") "
                     "ON CONFLICT(trade_date, exchange, source_url) DO UPDATE SET "
                     + ", ".join(f"{key}=excluded.{key}" for key in _COVERAGE_COLUMNS
                                 if key not in {"trade_date", "exchange", "source_url"}),
                     [coverage[key] for key in _COVERAGE_COLUMNS])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def read_settlement_parameters(data_root: Path, trade_date: str, exchange: str | None = None,
                               contract_code: str | None = None) -> dict[str, Any]:
    path = Path(data_root) / "catalog.duckdb"
    result: dict[str, Any] = {"tradeDate": trade_date, "marginPolicy": MARGIN_POLICY,
                              "parserVersion": PARSER_VERSION, "rows": [], "coverage": []}
    if not path.is_file():
        return result
    # Use the same DuckDB configuration as the serving MarketStore connection;
    # read_only=True conflicts with that writer in the same process.
    connection = duckdb.connect(str(path))
    try:
        available = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "futures_settlement_parameter_official" not in available:
            return result
        clauses = ["exchange<>'CFFEX'", "effective_from<=? AND effective_to>=?",
                   "trade_date=?", "parser_version=?"]
        params: list[Any] = [trade_date, trade_date, trade_date, PARSER_VERSION]
        if exchange:
            clauses.append("exchange=?")
            params.append(exchange.upper())
        if contract_code:
            clauses.append("contract_code=?")
            params.append(contract_code.upper())
        rows = connection.execute(f"SELECT {_SOURCE_COLUMNS} FROM futures_settlement_parameter_official WHERE "
                                  + " AND ".join(clauses) + " ORDER BY exchange,contract_code,source_url", params).fetchall()
        result["rows"] = [dict(zip(_COLUMNS, row, strict=True)) for row in rows]
        if exchange is None or exchange.upper() == "CFFEX":
            from .cffex_margin_asof import (
                applicable_publication_on_or_before,
                known_publication_days,
                verified_publications,
            )

            publications = verified_publications(connection, PARSER_VERSION)
            selected_day = applicable_publication_on_or_before(
                sorted({day for day, _, _ in publications}),
                known_publication_days(connection, PARSER_VERSION), trade_date,
            )
            result["cffexPublication"] = {
                "selection": "LATEST_VERIFIED_PUBLICATION",
                "publishedOn": selected_day,
                "carriedForward": selected_day is not None and selected_day < trade_date,
            }
            if selected_day is not None:
                accepted = {(url, digest) for day, url, digest in publications if day == selected_day}
                cffex_clauses = ["trade_date=?", "exchange='CFFEX'", "parser_version=?",
                                 "quality_status='PASS'"]
                cffex_params: list[Any] = [selected_day, PARSER_VERSION]
                if contract_code:
                    cffex_clauses.append("contract_code=?")
                    cffex_params.append(contract_code.upper())
                cffex_rows = connection.execute(
                    f"SELECT {_SOURCE_COLUMNS} FROM futures_settlement_parameter_official WHERE "
                    + " AND ".join(cffex_clauses), cffex_params,
                ).fetchall()
                result["rows"].extend(
                    dict(zip(_COLUMNS, row, strict=True)) for row in cffex_rows
                    if (row[_COLUMNS.index("source_url")], row[_COLUMNS.index("payload_sha256")]) in accepted
                    and (row[_COLUMNS.index("interval_source_url")] is None or (
                        row[_COLUMNS.index("effective_from")] <= trade_date
                        <= row[_COLUMNS.index("effective_to")]
                        and len(str(row[_COLUMNS.index("interval_payload_sha256")] or "")) == 64
                    ))
                )
                result["rows"].sort(key=lambda row: (row["exchange"], row["contract_code"], row["source_url"]))
        cov_clauses = ["trade_date=?"] + (["exchange=?"] if exchange else [])
        cov_params = [trade_date] + ([exchange.upper()] if exchange else [])
        coverage = connection.execute("SELECT " + ", ".join(_COVERAGE_COLUMNS)
                                      + " FROM futures_settlement_parameter_coverage WHERE " + " AND ".join(cov_clauses),
                                      cov_params).fetchall()
        result["coverage"] = [dict(zip(_COVERAGE_COLUMNS, row, strict=True)) for row in coverage]
        return result
    finally:
        connection.close()


def settlement_request(exchange: str, trading_day: str) -> dict[str, Any]:
    day = date.fromisoformat(trading_day).strftime("%Y%m%d")
    if exchange in {"SHFE", "INE"}:
        return {"method": "GET", "url": f"https://{_HOSTS[exchange]}/data/tradedata/future/dailydata/js{day}.dat"}
    if exchange == "GFEX":
        return {"method": "POST", "url": "http://www.gfex.com.cn/u/interfacesWebTiFutAndOptSettle/loadList",
                "data": {"trade_date": day, "variety": ""}}
    if exchange == "CZCE":
        return {"method": "GET", "url": f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day[:4]}/{day}/FutureDataClearParams.txt"}
    if exchange == "CFFEX":
        return {"method": "GET", "url": f"http://www.cffex.com.cn/sj/jscs/{day[:6]}/{day[6:]}/{day}_1.csv"}
    if exchange == "DCE":
        return {"method": "BROWSER_OBSERVED", "url": "http://www.dce.com.cn/dce/channel/list/181.html"}
    raise ValueError("unsupported settlement exchange")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".settlement-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _save_meta(path: Path, meta: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def fetch_settlement_parameters(data_root: Path, *, exchange: str, trading_day: str,
                                session: Any = None, timeout: float = 20,
                                dce_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
                                force: bool = False) -> dict[str, Any]:
    """Download and reparse one original; all network failures are sanitized."""
    exchange = exchange.upper()
    request = settlement_request(exchange, trading_day)
    source_url = request["url"]
    directory = Path(data_root) / BRONZE_ROOT / exchange / trading_day
    meta_path = directory / "result.json"
    timestamp = _utc()
    if not force and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            original = directory / str(meta.get("raw_file") or "")
            raw = original.read_bytes() if original.is_file() else b""
            if raw and sha256(raw).hexdigest() == meta.get("payload_sha256"):
                rows = parse_settlement_parameters(raw, exchange=exchange, trading_day=trading_day,
                                                   source_url=meta["source_url"], retrieved_at=meta["retrieved_at"],
                                                   margin_unit=meta.get("margin_unit"), request_evidence=meta.get("request_evidence"))
                return {**meta, "status": "PASS" if all(row["quality_status"] == "PASS" for row in rows) else "PARTIAL",
                        "cached": True, "rows": rows}
        except (OSError, ValueError, KeyError):
            pass
    margin_unit = None
    request_evidence = None
    try:
        if exchange == "DCE":
            if dce_fetcher is None:
                raise SettlementSourceError("DCE_181_VERIFIED_BROWSER_REQUEST_REQUIRED", status="DEFERRED")
            captured = dce_fetcher(trading_day)
            raw = captured.get("raw")
            if not isinstance(raw, bytes):
                raise SettlementSourceError("DCE_BROWSER_ORIGINAL_MISSING")
            source_url = _safe_url(str(captured.get("source_url") or ""), "DCE")
            margin_unit = captured.get("margin_unit")
            request_evidence = captured.get("request_evidence")
        else:
            client = session or requests.Session()
            headers = {"User-Agent": "Mozilla/5.0", "Referer": {
                "SHFE": "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/",
                "INE": "https://www.ine.cn/reports/tradedata/dailyandweeklydata/",
                "GFEX": "http://www.gfex.com.cn/gfex/rjscs/ywcs.shtml",
                "CZCE": "https://www.czce.com.cn/cn/jysj/jscs/H077003003index_1.htm",
                "CFFEX": "http://www.cffex.com.cn/cn/jscs.html",
            }[exchange]}
            if exchange == "GFEX":
                headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
                    "Origin": "http://www.gfex.com.cn", "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                })
            response = client.request(request["method"], request["url"], data=request.get("data"), headers=headers, timeout=timeout)
            if response.status_code != 200:
                status = "DEFERRED" if response.status_code in {403, 412, 429} else "NO_PUBLICATION" if response.status_code == 404 else "FAILED"
                raise SettlementSourceError(f"HTTP_{response.status_code}", status=status)
            raw = response.content
            if raw.lstrip().startswith((b"<", b"<!")):
                raise SettlementSourceError("HTML_INSTEAD_OF_SETTLEMENT", status="NO_PUBLICATION" if exchange == "CFFEX" else "DEFERRED")
        rows = parse_settlement_parameters(raw, exchange=exchange, trading_day=trading_day,
                                           source_url=source_url, retrieved_at=timestamp, margin_unit=margin_unit,
                                           request_evidence=request_evidence)
        digest = sha256(raw).hexdigest()
        raw_name = digest + ".raw"
        _atomic_bytes(directory / raw_name, raw)
        meta = {"schema": "official-futures-settlement-bronze/v1", "trade_date": trading_day,
                "exchange": exchange, "status": "PASS" if all(row["quality_status"] == "PASS" for row in rows) else "PARTIAL",
                "record_count": len(rows), "usable_count": sum(row["quality_status"] == "PASS" for row in rows),
                "source_url": _safe_url(source_url, exchange), "payload_sha256": digest,
                "retrieved_at": timestamp, "parser_version": PARSER_VERSION,
                "raw_file": raw_name, "margin_unit": margin_unit, "request_evidence": request_evidence, "error": None}
        _save_meta(meta_path, meta)
        return {**meta, "cached": False, "rows": rows}
    except SettlementSourceError as error:
        code, status = error.code, error.status
    except (OSError, requests.RequestException) as error:
        code, status = type(error).__name__, "FAILED"
    except (ValueError, TypeError, KeyError) as error:
        code, status = f"PARSE_{type(error).__name__}", "FAILED"
    meta = {"schema": "official-futures-settlement-bronze/v1", "trade_date": trading_day,
            "exchange": exchange, "status": status, "record_count": 0, "usable_count": 0,
            "source_url": _safe_url(source_url, exchange), "payload_sha256": None,
            "retrieved_at": timestamp, "parser_version": PARSER_VERSION, "error": code}
    _save_meta(directory / "last_failure.json", meta)
    return {**meta, "rows": []}


def discover_cffex_settlement_catalog(session: Any, *, start_day: str,
                                      timeout: float = 20) -> dict[str, str]:
    """Enumerate published CSV links; absence is never inferred from HTTP 200."""
    found: dict[str, str] = {}
    for page in range(1, 41):
        suffix = "" if page == 1 else f"_{page}"
        response = session.get(f"http://www.cffex.com.cn/cn/jscs{suffix}.html",
                               headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if response.status_code != 200:
            raise SettlementSourceError(f"CFFEX_CATALOG_HTTP_{response.status_code}")
        soup = BeautifulSoup(response.content, "html.parser")
        page_days: list[str] = []
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "")
            match = re.fullmatch(r"/sj/jscs/(20\d{4})/(\d{2})/(20\d{6})_1\.csv", href)
            if match is None:
                continue
            day = _day_text(match.group(3))
            if day is None or match.group(1) + match.group(2) != match.group(3):
                raise SettlementSourceError("CFFEX_CATALOG_DATE_MISMATCH")
            page_days.append(day)
            found[day] = "http://www.cffex.com.cn" + href
        if not page_days:
            if not found:
                raise SettlementSourceError("CFFEX_CATALOG_NO_VERIFIED_CSV_LINKS")
            break
        if min(page_days) < start_day:
            break
    return found


def import_settlement_bronze(data_root: Path, connection: Any, *, start_day: str = "2023-01-01",
                             end_day: str | None = None) -> dict[str, Any]:
    """Serially import SHA-verified originals without any network calls."""
    root = Path(data_root)
    imported = failures = 0
    outcomes: list[dict[str, Any]] = []
    for meta_path in sorted((root / BRONZE_ROOT).glob("*/*/result.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            day, venue = str(meta["trade_date"]), str(meta["exchange"])
            if day < start_day or (end_day and day > end_day):
                continue
            raw_name = str(meta.get("raw_file") or "")
            if not re.fullmatch(r"[0-9a-f]{64}\.raw", raw_name):
                raise ValueError("Bronze original name invalid")
            raw = (meta_path.parent / raw_name).read_bytes()
            if sha256(raw).hexdigest() != meta.get("payload_sha256"):
                raise ValueError("Bronze original SHA mismatch")
            rows = parse_settlement_parameters(raw, exchange=venue, trading_day=day,
                                               source_url=meta["source_url"], retrieved_at=meta["retrieved_at"],
                                               margin_unit=meta.get("margin_unit"), request_evidence=meta.get("request_evidence"))
            imported += import_partition(connection, rows, exchange=venue, trade_date=day,
                                          source_url=meta["source_url"], payload_sha256=meta["payload_sha256"],
                                          retrieved_at=meta["retrieved_at"])
            outcomes.append({"exchange": venue, "tradeDate": day, "status": "IMPORTED", "count": len(rows)})
        except (OSError, ValueError, KeyError) as error:
            failures += 1
            outcomes.append({"status": "FAILED", "reason": type(error).__name__, "file": str(meta_path.relative_to(root))})
    return {"status": "PASS" if outcomes and not failures else "PARTIAL_FAILURE" if failures else "NO_DATA",
            "recordCount": imported, "failed": failures, "outcomes": outcomes}


def capture_current_broker_observation(data_root: Path, *, trading_day: str,
                                        session: Any = None, timeout: float = 20) -> dict[str, Any]:
    """Archive the broker page, but grant no date to an undated current table.

    This observation remains separate from official settlement partitions.
    A company-default margin column is never interpreted as exchange margin.
    """
    source_url = "https://www.hsqh.net/col170/index"
    client = session or requests.Session()
    captured_at = _utc()
    directory = Path(data_root) / "bronze" / "futures_current_broker_margin" / "HSQH"
    report: dict[str, Any] = {
        "sourceKind": "THIRD_PARTY_CURRENT_EXCHANGE_STANDARD", "sourceUrl": source_url,
        "retrievedAt": captured_at, "requestedTradingDay": trading_day,
        "effectiveTradingDay": None, "qualityStatus": "UNVERIFIED", "rows": [],
    }
    try:
        response = client.get(source_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if response.status_code != 200:
            raise SettlementSourceError(f"HTTP_{response.status_code}")
        raw = response.content
        if not raw or len(raw) > _MAX_BYTES:
            raise SettlementSourceError("BROKER_PAGE_BODY_INVALID")
        soup = BeautifulSoup(raw, "html.parser")
        text = soup.get_text(" ", strip=True)
        report["payloadSha256"] = sha256(raw).hexdigest()
        report["hasExchangeStandardColumn"] = "交易所标准保证金" in text
        report["hasCompanyDefaultColumn"] = "公司默认标准" in text or "普标保证金" in text
        report["candidateDates"] = sorted({day for value in re.findall(r"20\d{2}-\d{2}-\d{2}", text)
                                           if (day := _day_text(value))})
        # Dates elsewhere in navigation/news are not table-effective evidence.
        # No row may be normalized until the table caption expressly identifies
        # its current trade date and the exchange-standard cells are audited.
        report["reason"] = "CURRENT_TABLE_EFFECTIVE_DATE_AND_EXCHANGE_COLUMN_NOT_VERIFIED"
        _atomic_bytes(directory / (report["payloadSha256"] + ".html"), raw)
    except (SettlementSourceError, requests.RequestException, OSError) as error:
        report["reason"] = error.code if isinstance(error, SettlementSourceError) else type(error).__name__
    _save_meta(directory / "last_observation.json", report)
    return report


def sync_settlement_parameters(data_root: Path, *, start_day: str = "2023-01-01",
                               end_day: str | None = None, exchanges: Sequence[str] | None = None,
                               workers: int = 6, max_days: int | None = None,
                               bronze_only: bool = True, connection: Any = None,
                               timeout: float = 20, min_interval_seconds: float = 0,
                               session_factory: Callable[[], Any] | None = None,
                               dce_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
                               progress: Callable[[Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """Collect source days concurrently; import only on the caller's one writer."""
    root = Path(data_root)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        raise ValueError("verified futures trading calendar is required")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    completed_through = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    available = [day for day in calendar.trading_days if day <= completed_through.isoformat()]
    if not available:
        raise ValueError("trading calendar has no completed day")
    end = min(end_day or available[-1], available[-1])
    days = calendar.between(start_day, end)
    if max_days is not None:
        if max_days < 1:
            raise ValueError("max_days must be positive")
        days = days[-max_days:]
    venues = tuple(dict.fromkeys(value.upper() for value in (exchanges or EXCHANGES)))
    if any(value not in EXCHANGES for value in venues) or workers < 1 or min_interval_seconds < 0:
        raise ValueError("invalid settlement sync configuration")
    locks = {venue: threading.Lock() for venue in venues}
    paused: dict[str, str] = {}
    existing_pass: dict[tuple[str, str], dict[str, Any]] = {}
    database = root / "catalog.duckdb"
    if database.is_file():
        reader = connection or duckdb.connect(str(database))
        try:
            tables = {row[0] for row in reader.execute("SHOW TABLES").fetchall()}
            if {"futures_settlement_parameter_official", "futures_settlement_parameter_coverage"} <= tables:
                cursor = reader.execute(
                    """WITH facts AS (
                         SELECT trade_date,exchange,source_url,count(*) n,
                                count(DISTINCT payload_sha256) sha_n,min(payload_sha256) sha
                         FROM futures_settlement_parameter_official
                         WHERE parser_version=? AND quality_status='PASS' GROUP BY 1,2,3
                       )
                       SELECT c.* FROM futures_settlement_parameter_coverage c
                       JOIN facts f USING(trade_date,exchange,source_url)
                       WHERE c.status='PASS' AND c.parser_version=? AND c.record_count=f.n
                         AND c.usable_count=f.n AND f.sha_n=1 AND c.payload_sha256=f.sha
                         AND length(c.payload_sha256)=64""", [PARSER_VERSION, PARSER_VERSION],
                )
                names = [column[0] for column in cursor.description]
                records = (dict(zip(names, values, strict=True)) for values in cursor.fetchall())
                existing_pass = {(str(record["exchange"]), str(record["trade_date"])): record
                                 for record in records}
        finally:
            if connection is None:
                reader.close()

    def reusable_partition(venue: str, day: str) -> dict[str, Any] | None:
        verified = existing_pass.get((venue, day))
        if verified is None:
            return None
        directory = root / BRONZE_ROOT / venue / day
        try:
            meta = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            raw_name = str(meta.get("raw_file") or "")
            if not raw_name or Path(raw_name).name != raw_name:
                return None
            raw = (directory / raw_name).read_bytes()
            if (meta.get("payload_sha256") == verified["payload_sha256"]
                    and sha256(raw).hexdigest() == verified["payload_sha256"]):
                return {**meta, "trade_date": day, "exchange": venue, "status": "PASS",
                        "cached": True, "already_imported": True, "rows": []}
        except (OSError, ValueError, TypeError):
            pass
        return None

    def collect_venue(venue: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        session = session_factory() if session_factory else requests.Session()
        interval = min_interval_seconds
        consecutive_failures = 0
        consecutive_passes = 0
        catalog: dict[str, str] | None = None
        if venue == "CFFEX":
            try:
                catalog = discover_cffex_settlement_catalog(session, start_day=start_day, timeout=timeout)
            except (SettlementSourceError, requests.RequestException):
                paused[venue] = "CFFEX_CATALOG_NOT_VERIFIED"
        for day in reversed(days):
            reused = reusable_partition(venue, day)
            if reused is not None:
                results.append({key: value for key, value in reused.items() if key != "rows"})
                if progress:
                    progress(results[-1])
                continue
            if venue in paused:
                results.append({"trade_date": day, "exchange": venue, "status": "DEFERRED", "rows": [],
                                "source_url": settlement_request(venue, day)["url"], "payload_sha256": None,
                                "retrieved_at": _utc(), "error": paused[venue], "parser_version": PARSER_VERSION,
                                "preserved_existing": (venue, day) in existing_pass})
                continue
            if catalog is not None and day not in catalog:
                results.append({"trade_date": day, "exchange": venue, "status": "NO_PUBLICATION", "rows": [],
                                "source_url": settlement_request(venue, day)["url"], "payload_sha256": None,
                                "retrieved_at": _utc(), "error": "NO_EXACT_DAILY_PUBLICATION_OR_VERIFIED_INTERVAL",
                                "parser_version": PARSER_VERSION,
                                "preserved_existing": (venue, day) in existing_pass})
                continue
            with locks[venue]:
                if interval:
                    time.sleep(interval)
                result = fetch_settlement_parameters(root, exchange=venue, trading_day=day,
                                                      session=session, timeout=timeout, dce_fetcher=dce_fetcher)
            results.append({key: value for key, value in result.items() if key != "rows"})
            if result["status"] not in {"PASS", "PARTIAL"} and (venue, day) in existing_pass:
                results[-1]["preserved_existing"] = True
            if result["status"] == "DEFERRED":
                paused[venue] = str(result.get("error") or "SOURCE_PAUSED")
            elif result["status"] == "FAILED":
                consecutive_passes = 0
                interval = min(max(interval * 2, 0.5), 8)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    paused[venue] = "REPEATED_SOURCE_FAILURE_PAUSED"
            else:
                consecutive_failures = 0
                consecutive_passes += 1
                if consecutive_passes >= 15 and interval > max(min_interval_seconds, 0.5):
                    interval = max(interval * 0.75, min_interval_seconds, 0.5)
                    consecutive_passes = 0
            if progress:
                progress({key: value for key, value in result.items() if key != "rows"})
        session.close()
        return results

    outcomes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(venues) or 1)) as pool:
        futures = [pool.submit(collect_venue, venue) for venue in venues]
        for future in as_completed(futures):
            outcomes.extend(future.result())
    if not bronze_only:
        # Opening the writer only now keeps all network work outside DuckDB's
        # cross-process single-writer window.
        own_connection = connection is None
        writer = connection or duckdb.connect(str(root / "catalog.duckdb"))
        try:
            for result in sorted(outcomes, key=lambda item: (item["trade_date"], item["exchange"])):
                if result.get("already_imported") or result.get("preserved_existing"):
                    continue
                rows: list[dict[str, Any]] = []
                if result["status"] in {"PASS", "PARTIAL"}:
                    directory = root / BRONZE_ROOT / result["exchange"] / result["trade_date"]
                    raw = (directory / result["raw_file"]).read_bytes()
                    if sha256(raw).hexdigest() != result["payload_sha256"]:
                        raise ValueError("Bronze changed before settlement import")
                    rows = parse_settlement_parameters(raw, exchange=result["exchange"], trading_day=result["trade_date"],
                                                       source_url=result["source_url"], retrieved_at=result["retrieved_at"],
                                                       margin_unit=result.get("margin_unit"), request_evidence=result.get("request_evidence"))
                import_partition(writer, rows, exchange=result["exchange"], trade_date=result["trade_date"],
                                 source_url=result["source_url"], payload_sha256=result.get("payload_sha256"),
                                 retrieved_at=result["retrieved_at"], status=result["status"], error=result.get("error"))
        finally:
            if own_connection:
                writer.close()
    summary = {venue: {state: sum(item["exchange"] == venue and item["status"] == state for item in outcomes)
                       for state in ("PASS", "PARTIAL", "FAILED", "DEFERRED", "NO_PUBLICATION")} for venue in venues}
    report = {"status": "PASS" if outcomes and all(item["status"] == "PASS" for item in outcomes) else "PARTIAL_FAILURE",
              "startedDay": start_day, "completedAt": _utc(),
              "startDay": days[0] if days else None, "endDay": days[-1] if days else None,
              "tradingDayCount": len(days), "bronzeOnly": bronze_only,
              "recordCount": sum(int(item.get("record_count") or 0) for item in outcomes), "exchanges": summary,
              "reusedPartitions": sum(bool(item.get("already_imported")) for item in outcomes),
              "preservedPartitions": sum(bool(item.get("preserved_existing")) for item in outcomes),
              "outcomes": [{key: value for key, value in item.items() if key != "rows"} for item in outcomes]}
    _save_meta(root / "state" / "futures_settlement_last_sync.json", report)
    return report


__all__ = ("PARSER_VERSION", "MARGIN_POLICY", "EXCHANGES", "ensure_schema", "parse_shfe", "parse_gfex",
           "parse_czce", "parse_cffex", "parse_dce", "parse_settlement_parameters", "import_partition",
           "read_settlement_parameters", "settlement_request", "fetch_settlement_parameters", "sync_settlement_parameters",
           "import_settlement_bronze", "discover_cffex_settlement_catalog", "capture_current_broker_observation")
