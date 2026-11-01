"""Audited historical Shanghai, Shenzhen and Beijing A-share capitalisation.

Eastmoney's "沪深两市" aggregate includes Shenzhen B shares. This module uses
Eastmoney Shanghai Main Board, STAR and BSE rows, plus the SZSE's published
Shenzhen Main Board A and ChiNext A rows. Every daily Gold value retains the
exact public responses and their SHA-256 digests in local Bronze.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import warnings
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zipfile import BadZipFile
from uuid import uuid4
from zoneinfo import ZoneInfo

import openpyxl
import requests

from .a_share_calendar import is_known_a_share_closed_day, is_verified_a_share_trading_day
from .collector import _metric
from .storage import MarketStore


EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport"
SERIES_ID = "A_SHARE_MARKET_CAP_HISTORY"
INSTRUMENT_ID = "CN.A_SHARE.MARKET_CAP"
METRIC_KEY = "TOTAL_MARKET_CAP_YI"
_METHOD_PREFIX = "source=eastmoney-RPT_VALUEMARKET+szse-official-daily;"
_FORMULA = "em_main+em_star+em_bse+szse_main_a+szse_chinext_a"
_CODES = frozenset({"000001", "000300", "000688", "399001", "399006", "500001", "899050"})
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ONE_YI = Decimal("100000000")
_EM_RAW_PER_YI = Decimal("10000")
_AGGREGATE_TOLERANCE_YI = Decimal("0.1")


def _beijing_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _beijing_today() -> date:
    return _beijing_now().date()


def _day(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is missing or not numeric")
    try:
        result = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


def _close(actual: Decimal, expected: Decimal, *, label: str) -> None:
    if abs(actual - expected) > _AGGREGATE_TOLERANCE_YI:
        raise ValueError(f"{label} disagrees by more than 0.1 亿元")


def _parse_eastmoney(blob: bytes, day: str) -> dict[str, Decimal]:
    try:
        payload = json.loads(blob, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Eastmoney response is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Eastmoney response reports failure")
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("count") != len(_CODES) or result.get("pages") != 1:
        raise ValueError("Eastmoney daily market coverage is incomplete")
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) != len(_CODES):
        raise ValueError("Eastmoney daily market rows are incomplete")
    caps: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Eastmoney daily market row is malformed")
        code = row.get("TRADE_MARKET_CODE")
        if code not in _CODES or code in caps:
            raise ValueError("Eastmoney daily market code is unknown or duplicated")
        if row.get("TRADE_DATE") not in {day, f"{day} 00:00:00"}:
            raise ValueError("Eastmoney market date disagrees with the requested day")
        count = row.get("LISTING_ORG_NUM")
        if type(count) is not int or count <= 0:
            raise ValueError("Eastmoney listing count is missing")
        caps[code] = _decimal(row.get("TOTAL_MARKET_CAP"), field=f"Eastmoney {code} cap") / _EM_RAW_PER_YI
    if set(caps) != _CODES:
        raise ValueError("Eastmoney daily market coverage is incomplete")
    _close(caps["000300"],
           caps["000001"] + caps["000688"] + caps["399001"] + caps["399006"],
           label="Eastmoney Shanghai/Shenzhen aggregate")
    _close(caps["500001"], caps["399001"] + caps["399006"],
           label="Eastmoney Shenzhen aggregate")
    if caps["899050"] >= caps["000300"] * Decimal("0.2"):
        raise ValueError("Eastmoney BSE cap scale is implausible")
    return caps


def _parse_szse(blob: bytes) -> dict[str, Decimal]:
    if not blob.startswith(b"PK") or len(blob) > 1_000_000:
        raise ValueError("SZSE daily response is not an expected XLSX")
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style")
            workbook = openpyxl.load_workbook(io.BytesIO(blob), read_only=False, data_only=True)
        try:
            if len(workbook.worksheets) != 1:
                raise ValueError("SZSE daily workbook has unexpected sheets")
            rows = list(workbook.active.values)
        finally:
            workbook.close()
    except (OSError, ValueError, KeyError, BadZipFile) as exc:
        raise ValueError("SZSE daily workbook cannot be read") from exc
    if not rows or tuple(str(x).strip() for x in rows[0][:5]) != (
        "证券类别", "数量(只)", "成交金额(元)", "总市值(元)", "流通市值(元)"
    ):
        raise ValueError("SZSE daily workbook headers or units changed")
    wanted = {"股票", "主板A股", "主板B股", "创业板A股"}
    caps: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for row in rows[1:]:
        if not row:
            continue
        label = str(row[0] or "").strip()
        if label not in wanted:
            continue
        if label in caps:
            raise ValueError("SZSE daily workbook has duplicate categories")
        raw_count = row[1]
        if type(raw_count) is int:
            count = raw_count
        elif isinstance(raw_count, str) and re.fullmatch(r"\d+", raw_count.strip()):
            count = int(raw_count.strip())
        else:
            raise ValueError(f"SZSE {label} count is missing")
        if count <= 0:
            raise ValueError(f"SZSE {label} count is missing")
        counts[label] = count
        caps[label] = _decimal(row[3], field=f"SZSE {label} total cap")
    if set(caps) != wanted:
        raise ValueError("SZSE daily A/B/ChiNext categories are incomplete")
    if counts["股票"] != counts["主板A股"] + counts["主板B股"] + counts["创业板A股"]:
        raise ValueError("SZSE stock counts disagree with category counts")
    if abs(caps["股票"] - sum((caps[key] for key in wanted - {"股票"}), Decimal(0))) > Decimal("0.01"):
        raise ValueError("SZSE stock cap disagrees with A/B/ChiNext categories")
    return {key: value / _ONE_YI for key, value in caps.items()}


def _compose_cap(em: dict[str, Decimal], szse: dict[str, Decimal]) -> Decimal:
    _close(em["399001"], szse["主板A股"] + szse["主板B股"],
           label="Eastmoney/SZSE Shenzhen Main Board")
    _close(em["399006"], szse["创业板A股"],
           label="Eastmoney/SZSE ChiNext")
    _close(em["500001"], szse["股票"],
           label="Eastmoney/SZSE Shenzhen total")
    value = (em["000001"] + em["000688"] + em["899050"]
             + szse["主板A股"] + szse["创业板A股"])
    if value <= 0 or value >= Decimal("10000000"):
        raise ValueError("A-share market cap scale is implausible")
    return value


def _atomic_bytes(path: Path, blob: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_bytes(blob)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _evidence_dir(root: Path, day: str) -> Path:
    return root / "bronze" / "a_share_market_cap_history" / f"day={day}"


def _request_spec(source: str, day: str) -> tuple[str, dict[str, str], str]:
    if source == "eastmoney":
        return EASTMONEY_URL, {
            "reportName": "RPT_VALUEMARKET", "columns": "ALL", "pageNumber": "1",
            "pageSize": "20", "source": "WEB", "client": "WEB",
            "filter": f"(TRADE_DATE='{day}')",
        }, "datacenter-web.eastmoney.com"
    if source == "szse":
        return SZSE_URL, {
            "SHOWTYPE": "xlsx", "CATALOGID": "1803_sczm", "TABKEY": "tab1",
            "txtQueryDate": day,
        }, "www.szse.cn"
    raise ValueError("unknown market-cap source")


def _request_url(source: str, day: str) -> str:
    url, params, _ = _request_spec(source, day)
    prepared = requests.Request("GET", url, params=params).prepare().url
    if prepared is None:
        raise ValueError("market-cap request URL could not be prepared")
    return prepared


def _legacy_request_metadata(day: str) -> dict[str, dict[str, Any]]:
    reconstructed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        source: {
            "requestedDay": day,
            "requestedUrl": _request_url(source, day),
            "responseUrl": None,
            "httpStatus": None,
            "fetchedAt": None,
            "metadataOrigin": "LEGACY_RECONSTRUCTED",
            "reconstructedAt": reconstructed_at,
        }
        for source in ("eastmoney", "szse")
    }


def _valid_request_metadata(metadata: object, source: str, day: str) -> bool:
    if not isinstance(metadata, dict):
        return False
    if metadata.get("requestedDay") != day or metadata.get("requestedUrl") != _request_url(source, day):
        return False
    origin = metadata.get("metadataOrigin")
    if origin == "LEGACY_RECONSTRUCTED":
        if any(metadata.get(key) is not None for key in ("responseUrl", "httpStatus", "fetchedAt")):
            return False
        timestamp = metadata.get("reconstructedAt")
    elif origin == "OBSERVED":
        if metadata.get("httpStatus") != 200:
            return False
        final_url = metadata.get("responseUrl")
        _, _, expected_host = _request_spec(source, day)
        if (not isinstance(final_url, str) or urlsplit(final_url).scheme != "https"
                or urlsplit(final_url).hostname != expected_host):
            return False
        query = parse_qs(urlsplit(final_url).query)
        if source == "eastmoney" and query.get("filter") != [f"(TRADE_DATE='{day}')"]:
            return False
        if source == "szse" and query.get("txtQueryDate") != [day]:
            return False
        timestamp = metadata.get("fetchedAt")
    else:
        return False
    if not isinstance(timestamp, str):
        return False
    try:
        return datetime.fromisoformat(timestamp).tzinfo is not None
    except ValueError:
        return False


def _read_evidence(root: Path, day: str) -> tuple[bytes, bytes, dict[str, Any]] | None:
    directory = _evidence_dir(root, day)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("day") != day:
            return None
        em_blob = (directory / "eastmoney.json").read_bytes()
        szse_blob = (directory / "szse.xlsx").read_bytes()
        if (hashlib.sha256(em_blob).hexdigest() != manifest.get("emSha256")
                or hashlib.sha256(szse_blob).hexdigest() != manifest.get("szseSha256")):
            return None
        if not _SHA_RE.fullmatch(str(manifest.get("emSha256", ""))):
            return None
        if not _SHA_RE.fullmatch(str(manifest.get("szseSha256", ""))):
            return None
        request_metadata = manifest.get("requests")
        if request_metadata is None:
            if manifest.get("schemaVersion") is not None:
                return None
            # V1 manifests contain verified raw bytes and digests, but never
            # recorded the HTTP response URL/status/time. Reconstruct only the
            # deterministic request, and preserve the other fields as unknown.
            em = _parse_eastmoney(em_blob, day)
            szse = _parse_szse(szse_blob)
            if _compose_cap(em, szse) != Decimal(str(manifest.get("valueYi"))):
                return None
            request_metadata = _legacy_request_metadata(day)
            manifest = {**manifest, "schemaVersion": 2, "requests": request_metadata}
            _atomic_bytes(directory / "manifest.json", json.dumps(
                manifest, ensure_ascii=False, sort_keys=True,
            ).encode("utf-8"))
        elif manifest.get("schemaVersion") is None:
            # A previously upgraded V1 manifest can gain the version marker
            # without any provider request or change to the published value.
            manifest = {**manifest, "schemaVersion": 2}
            _atomic_bytes(directory / "manifest.json", json.dumps(
                manifest, ensure_ascii=False, sort_keys=True,
            ).encode("utf-8"))
        elif manifest.get("schemaVersion") != 2:
            return None
        if (not isinstance(request_metadata, dict)
                or set(request_metadata) != {"eastmoney", "szse"}
                or any(not _valid_request_metadata(request_metadata[source], source, day)
                       for source in ("eastmoney", "szse"))):
            return None
        return em_blob, szse_blob, manifest
    except (OSError, ValueError, TypeError, InvalidOperation):
        return None


def _get_response(session: requests.Session, url: str, *, params: dict[str, str],
                  host: str, day: str, max_bytes: int) -> tuple[bytes, dict[str, Any]]:
    response = session.get(
        url, params=params, headers={"User-Agent": "Mozilla/5.0 MarketListener/1.0",
                                    "Referer": "https://data.eastmoney.com/gzfx/scgk.html"
                                    if host == "datacenter-web.eastmoney.com" else
                                    "https://www.szse.cn/market/overview/index.html"},
        timeout=25,
    )
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError("market-cap response did not return HTTP 200")
    if urlsplit(response.url).scheme != "https" or urlsplit(response.url).hostname != host:
        raise ValueError("market-cap response redirected outside the expected HTTPS host")
    blob = response.content
    if not blob or len(blob) > max_bytes:
        raise ValueError("market-cap response size is outside the expected range")
    return blob, {
        "requestedDay": day,
        "requestedUrl": _request_url("eastmoney" if host == "datacenter-web.eastmoney.com" else "szse", day),
        "responseUrl": response.url,
        "httpStatus": response.status_code,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metadataOrigin": "OBSERVED",
    }


def _fetch_evidence(
    session: requests.Session, root: Path, day: str,
) -> tuple[bytes, bytes, dict[str, dict[str, Any]]]:
    em_url, em_params, em_host = _request_spec("eastmoney", day)
    em_blob, em_metadata = _get_response(
        session, em_url, params=em_params, host=em_host, day=day, max_bytes=300_000,
    )
    directory = _evidence_dir(root, day)
    _atomic_bytes(directory / "eastmoney.json", em_blob)
    szse_url, szse_params, szse_host = _request_spec("szse", day)
    szse_blob, szse_metadata = _get_response(
        session, szse_url, params=szse_params, host=szse_host, day=day, max_bytes=1_000_000,
    )
    _atomic_bytes(directory / "szse.xlsx", szse_blob)
    return em_blob, szse_blob, {"eastmoney": em_metadata, "szse": szse_metadata}


def _already_published(store: MarketStore, root: Path, day: str) -> bool:
    metric_id = f"{SERIES_ID}:{INSTRUMENT_ID}:{day}:1d:{METRIC_KEY}"
    row = store.connection.execute(
        "SELECT value, calculation_method FROM gold_metrics WHERE metric_id=?", [metric_id]
    ).fetchone()
    if (row is None or not str(row[1]).startswith(_METHOD_PREFIX)
            or f"formula={_FORMULA}" not in row[1] or "unit=亿元" not in row[1]):
        return False
    evidence = _read_evidence(root, day)
    if evidence is None:
        return False
    _, _, manifest = evidence
    if f"em_sha256={manifest['emSha256']}" not in row[1]:
        return False
    if f"szse_sha256={manifest['szseSha256']}" not in row[1]:
        return False
    try:
        return math.isclose(float(row[0]), float(manifest["valueYi"]), rel_tol=0, abs_tol=1e-6)
    except (ValueError, TypeError, OverflowError):
        return False


def sync_a_share_market_cap_history(
    data_root: Path, *, start_day: str, end_day: str,
    session: requests.Session | None = None,
) -> dict[str, object]:
    """Fetch, verify and checkpoint dated pure-A market capitalisation in 亿元.

    Completed dates with matching Bronze SHA-256 evidence and Gold values are
    skipped. Failed dates remain absent from Gold and are returned for retry.
    """

    start = _day(start_day)
    end = _day(end_day)
    if end < start:
        raise ValueError("end_day precedes start_day")
    today = _beijing_today()
    if end > today:
        raise ValueError("market-cap sync cannot include a future day")
    if end == today:
        now = _beijing_now()
        if now.date() != today or (now.hour, now.minute) < (15, 30):
            raise ValueError("same-day market-cap sync requires the completed A-share session")
    root = Path(data_root)
    own_session = session is None
    session = session or requests.Session()
    written: list[str] = []
    skipped: list[str] = []
    non_trading: list[str] = []
    calendar_unverified: list[str] = []
    failed: list[dict[str, str]] = []
    store = MarketStore(root)
    try:
        current = start
        while current <= end:
            day = current.isoformat()
            current += timedelta(days=1)
            trade_date = date.fromisoformat(day)
            if not is_verified_a_share_trading_day(trade_date):
                if is_known_a_share_closed_day(trade_date):
                    non_trading.append(day)
                else:
                    calendar_unverified.append(day)
                    failed.append({"day": day, "error": "CALENDAR_UNVERIFIED"})
                continue
            if _already_published(store, root, day):
                skipped.append(day)
                continue
            try:
                evidence = _read_evidence(root, day)
                if evidence is None:
                    em_blob, szse_blob, request_metadata = _fetch_evidence(session, root, day)
                else:
                    em_blob, szse_blob, prior_manifest = evidence
                    request_metadata = prior_manifest["requests"]
                em = _parse_eastmoney(em_blob, day)
                szse = _parse_szse(szse_blob)
                value_yi = _compose_cap(em, szse)
                em_sha = hashlib.sha256(em_blob).hexdigest()
                szse_sha = hashlib.sha256(szse_blob).hexdigest()
                method = (f"{_METHOD_PREFIX} formula={_FORMULA}; unit=亿元; "
                          f"em_sha256={em_sha}; szse_sha256={szse_sha}; "
                          f"em_date={day}; szse_request_date={day}")
                manifest = {
                    "schemaVersion": 2,
                    "day": day,
                    "valueYi": str(value_yi),
                    "emSha256": em_sha,
                    "szseSha256": szse_sha,
                    "componentsYi": {
                        "emMain": str(em["000001"]), "emStar": str(em["000688"]),
                        "emBse": str(em["899050"]),
                        "szseMainA": str(szse["主板A股"]),
                        "szseChiNextA": str(szse["创业板A股"]),
                        "szseMainBExcluded": str(szse["主板B股"]),
                    },
                    "sourceUrls": [EASTMONEY_URL, SZSE_URL],
                    "requests": request_metadata,
                }
                _atomic_bytes(
                    _evidence_dir(root, day) / "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                )
                metric = _metric(
                    SERIES_ID, INSTRUMENT_ID, day, "1d", "沪深京A股总市值(亿元)",
                    float(value_yi),
                    "沪市主板A股、科创板、深市主板A股、创业板A股与北证A股市值之和；"
                    "东方财富原值万元转亿元，深交所日报原值元转亿元。",
                    method, metric_key=METRIC_KEY,
                )
                run_id = store.begin_run("collector:a-share-market-cap-history")
                try:
                    store.upsert_gold_metrics([metric])
                    store.finish_run(run_id, "COMPLETE", f"{day} 沪深京A股总市值核验入库")
                except Exception:
                    store.finish_run(run_id, "FAILED", f"{day} 市值入库失败")
                    raise
                written.append(day)
            except (OSError, ValueError, TypeError, requests.RequestException) as exc:
                failed.append({"day": day, "error": str(exc)[:240]})
        return {
            "writtenDays": written,
            "skippedDays": skipped,
            "nonTradingDays": non_trading,
            "calendarUnverifiedDays": calendar_unverified,
            "failedDays": failed,
            "metricCount": len(written),
        }
    finally:
        store.close()
        if own_session:
            session.close()


__all__ = (
    "EASTMONEY_URL", "INSTRUMENT_ID", "METRIC_KEY", "SERIES_ID", "SZSE_URL",
    "sync_a_share_market_cap_history",
)
