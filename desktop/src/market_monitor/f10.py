"""A/H 股 F10 基础资料采集（腾讯批量行情 + 东财公司概况/主营构成）。

设计约束（用户要求）：
- 不能短时间高频请求东财 F10，容易被封 IP（用户已批准约 4 只/秒，
  并保留连续 8 次失败自动停机的看门狗兜底）。
- 支持断点续抓：只抓取本地尚未成功的证券，失败单独记录。
- 采集结果落成可查询的 JSONL 快照，并登记为 STOCK_F10 数据集。

该模块刻意使用 ``curl.exe`` 而非 requests/urllib，以绕开本机
Windows 系统代理（127.0.0.1:7897）下 Python 的 HTTPS 挂起问题；
不依赖 akshare/pandas 等重量级运行时。
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from threading import Lock
from functools import wraps
from typing import Callable
from uuid import uuid4

from .industry_graph.f10.market_caps import derive_market_caps
from .industry_graph.f10.segments import largest_revenue_segment, migrate_revenue_rows
from .storage import MarketStore

_CURL = "curl.exe"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_REFERER = "https://emweb.securities.eastmoney.com/"
_RATE_LOCK = Lock()
_LAST_F10_REQUEST_AT = 0.0
_HELD_MARKET_LOCKS: dict[tuple[str, str], tuple[int, Any]] = {}
_MARKET_LOCK_GUARD = Lock()
# 用户批准的提速档位：默认约 4 次请求/秒；可用环境变量覆盖。
_THROTTLE_SECONDS = float(os.environ.get("MARKET_MONITOR_F10_RATE_SECONDS", "0.25"))
_MIN_A_SHARE_UNIVERSE = 3000
_LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_compact() -> str:
    return date.today().isoformat().replace("-", "")


def _acquire_market_lock(data_root: Path, market: str) -> bool:
    """Try to acquire a file-based lock for one market's F10 fetch.
    Returns True if acquired, False if another process holds it."""
    lock_dir = Path(data_root) / "f10" / market.lower()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "fetch.lock"
    key = (str(Path(data_root).resolve()), market.upper())
    import threading

    guard = (lock_dir / "fetch.guard.lock").open("a+b")
    try:
        guard.seek(0, os.SEEK_END)
        if guard.tell() == 0:
            guard.write(b"0")
            guard.flush()
        guard.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        guard.close()
        return False
    if lock_file.is_file():
        try:
            old_pid = int(lock_file.read_text(encoding="utf-8").strip())
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, old_pid)
            if handle:
                kernel32.CloseHandle(handle)
                guard.close()
                return False
        except Exception:
            pass
    try:
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        with _MARKET_LOCK_GUARD:
            _HELD_MARKET_LOCKS[key] = (threading.get_ident(), guard)
        return True
    except Exception:
        guard.close()
        return False


def _release_market_lock(data_root: Path, market: str) -> None:
    """Remove the file-based lock for one market."""
    import threading

    key = (str(Path(data_root).resolve()), market.upper())
    with _MARKET_LOCK_GUARD:
        held = _HELD_MARKET_LOCKS.get(key)
        if held is None or held[0] != threading.get_ident():
            return
        del _HELD_MARKET_LOCKS[key]
    lock_file = Path(data_root) / "f10" / market.lower() / "fetch.lock"
    try:
        if lock_file.is_file() and lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_file.unlink()
    except OSError:
        pass
    finally:
        held[1].close()


def _release_after_fetch(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(function)
    def wrapped(data_root: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        import threading

        market = str(kwargs.get("market", "CN")).upper()
        key = (str(Path(data_root).resolve()), market)
        with _MARKET_LOCK_GUARD:
            held = _HELD_MARKET_LOCKS.get(key)
            previously_held = held is not None and held[0] == threading.get_ident()
        try:
            return function(data_root, *args, **kwargs)
        finally:
            if not previously_held:
                _release_market_lock(Path(data_root), market)
    return wrapped


def _num(value: Any, default: float | None = None) -> float | None:
    """Convert a Tencent/Eastmoney numeric field to float, ``None`` when empty."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "None", "nan", "NaN"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _get(
    url: str,
    *,
    timeout: float = 15.0,
    encoding: str = "utf-8",
    headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
) -> str:
    """Fetch a URL via curl.exe and return the decoded body."""
    extra_headers: list[str] = []
    if headers:
        if isinstance(headers, Mapping):
            extra_headers = [f"{key}: {value}" for key, value in headers.items()]
        else:
            extra_headers = [f"{key}: {value}" for key, value in headers]
    command = [
        _CURL,
        "-sS",
        "--compressed",
        "-m",
        str(int(timeout)),
        "-H",
        f"User-Agent: {_UA}",
        "-H",
        f"Referer: {_REFERER}",
    ]
    for header in extra_headers:
        command.extend(["-H", header])
    command.append(url)
    result = subprocess.run(command, capture_output=True, timeout=timeout + 10)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl {result.returncode}: {detail[:160]}")
    if not result.stdout:
        raise RuntimeError("empty HTTP response")
    return result.stdout.decode(encoding, errors="replace")


def _get_bytes(url: str, *, timeout: float = 90.0) -> bytes:
    """Fetch a binary payload (xlsx etc.) via curl.exe."""
    command = [
        _CURL,
        "-sS",
        "--compressed",
        "-m",
        str(int(timeout)),
        "-H",
        f"User-Agent: {_UA}",
        "-H",
        f"Referer: {_REFERER}",
        url,
    ]
    result = subprocess.run(command, capture_output=True, timeout=timeout + 15)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl {result.returncode}: {detail[:160]}")
    if not result.stdout:
        raise RuntimeError("empty HTTP response")
    return result.stdout


def _throttle_f10(min_interval_seconds: float = 0.9) -> None:
    """Global per-process throttle so parallel workers never hammer F10."""
    global _LAST_F10_REQUEST_AT
    with _RATE_LOCK:
        elapsed = time.monotonic() - _LAST_F10_REQUEST_AT
        if elapsed < min_interval_seconds:
            time.sleep(min_interval_seconds - elapsed)
        _LAST_F10_REQUEST_AT = time.monotonic()


def _retry_get(url: str, *, attempts: int = 3, timeout: float = 15.0, encoding: str = "utf-8") -> str:
    """Fetch with a small retry window; used for per-stock F10 detail calls."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return _get(url, timeout=timeout, encoding=encoding)
        except Exception as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {attempts} attempts: {last}")


def _governed_fetch(
    url: str,
    *,
    timeout: float = 18.0,
    encoding: str = "utf-8",
    provider: str = "eastmoney",
) -> str:
    """Fetch an F10 page through the shared provider governance limiter.

    The provider package is imported lazily so this module stays importable
    on its own (governance imports back into :mod:`market_monitor.f10`).
    """
    from .industry_graph.f10.providers.governance import governed_get

    return governed_get(url, provider=provider, timeout=timeout, encoding=encoding)


# ---------------------------------------------------------------------------
# A 股证券清单
# ---------------------------------------------------------------------------


def _validate_a_share_universe(items: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if (len(code) != 6 or not code.isascii() or not code.isdigit() or not name
                or name.lower() in {"nan", "none"} or code in seen):
            raise ValueError("invalid or duplicate A-share universe entry")
        seen.add(code)
        rows.append({"code": code, "name": name})
    if len(rows) < _MIN_A_SHARE_UNIVERSE:
        raise ValueError(f"A-share universe below minimum: {len(rows)}")
    return rows


def _read_a_share_universe_cache(path: Path) -> list[dict[str, str]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if any(not isinstance(item, dict) for item in rows):
            return []
        return _validate_a_share_universe(rows)
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def read_a_share_universe_cache_status(data_root: Path) -> dict[str, Any]:
    """Describe local cache freshness, which is not proof of source completeness."""
    path = Path(data_root) / "f10" / "cn" / "universe.jsonl"
    rows = _read_a_share_universe_cache(path)
    if not rows:
        return {"status": "INVALID_OR_MISSING", "count": None, "savedAt": None}
    try:
        modified = path.stat().st_mtime
        stamp = datetime.fromtimestamp(modified, tz=timezone.utc)
        is_recent = 0 <= time.time() - modified <= 7 * 86400
        return {"status": "RECENT_CACHE" if is_recent else "STALE_CACHE",
                "count": len(rows), "savedAt": stamp.isoformat()}
    except (OSError, ValueError, OverflowError):
        return {"status": "INVALID_OR_MISSING", "count": None, "savedAt": None}


def read_hk_universe_file_status(data_root: Path) -> dict[str, Any]:
    """Report local file presence and modification time, not list completeness."""
    path = Path(data_root) / "f10" / "hk" / "hkex_list.xlsx"
    try:
        if not path.is_file():
            raise FileNotFoundError
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return {"status": "LOCAL_FILE_PRESENT", "savedAt": stamp.isoformat()}
    except (OSError, ValueError, OverflowError):
        return {"status": "MISSING", "savedAt": None}


def load_a_share_universe(cache_dir: Path | None = None) -> list[dict[str, str]]:
    """Return all listed A-share securities (code + name).

    Uses a local ``universe.jsonl`` cache (refreshed weekly) so restarted
    fetch passes do not depend on the AkShare network call, then falls back
    to the bulk Eastmoney listing request.  A stale cache is still used when
    the network call fails, so a hung/blocked run cannot destroy progress.
    """
    cache_dir = Path(cache_dir or Path("data_control") / "f10" / "cn")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / "universe.jsonl"
    cache_rows = _read_a_share_universe_cache(cached)
    if cache_rows and 0 <= time.time() - cached.stat().st_mtime <= 7 * 86400:
        return cache_rows

    try:
        import akshare as ak  # only used for this one bulk listing call
    except Exception as error:  # pragma: no cover - environment dependent
        if cache_rows:
            _LOGGER.warning("A-share universe provider unavailable; using older validated cache")
            return cache_rows
        raise RuntimeError(f"akshare unavailable: {error}") from error
    try:
        frame = ak.stock_info_a_code_name()
    except Exception as error:
        if cache_rows:
            _LOGGER.warning("A-share universe fetch failed; using older validated cache")
            return cache_rows
        raise RuntimeError(f"A-share universe fetch failed: {error}") from error
    try:
        rows = _validate_a_share_universe([{"code": row.get("code"), "name": row.get("name")}
                                           for _, row in frame.iterrows()])
        if cache_rows and len(rows) < len(cache_rows) * 0.9:
            raise ValueError("A-share universe unexpectedly shrank by over 10%")
    except (ValueError, TypeError, AttributeError) as error:
        if cache_rows:
            _LOGGER.warning("A-share universe failed quality checks; using older validated cache")
            return cache_rows
        raise RuntimeError("A-share universe failed quality checks; no validated cache") from error
    temporary = cached.with_name(f".{cached.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")
        os.replace(temporary, cached)
    finally:
        temporary.unlink(missing_ok=True)
    return rows


def _tencent_symbol(code: str) -> str:
    """Convert a bare 6-digit A-share code into a Tencent quote symbol."""
    if len(code) == 5 and code.isdigit():
        return f"hk{code}"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"bj{code}"
    if code.startswith(("60", "68", "9")):
        return f"sh{code}"
    if code.startswith(("00", "30", "20")):
        return f"sz{code}"
    return f"sh{code}"


def _eastmoney_code(code: str) -> str:
    """A-share code -> Eastmoney F10 prefix (SH/SZ/BJ)."""
    if code.startswith("92"):
        return f"BJ{code}"
    if code.startswith(("60", "68", "9")):
        return f"SH{code}"
    if code.startswith(("00", "30", "20")):
        return f"SZ{code}"
    return f"BJ{code}"


def load_hk_universe(*, cache_dir: Path | None = None) -> list[dict[str, str]]:
    """Return HKEX listed equity codes (5-digit) from the official securities list.

    Uses an already-saved HKEX file. Automated access to the HKEX website is
    disabled; the local list can be stale and must not be called complete.
    """
    import pandas as pd

    cache_dir = Path(cache_dir or Path("data_control") / "f10" / "hk")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / "hkex_list.xlsx"
    if not cached.is_file():
        raise RuntimeError("HK universe unavailable: local securities list missing")
    frame = pd.read_excel(cached, sheet_name="ListOfSecurities", skiprows=3, header=None)
    if frame.empty or len(frame.columns) < 3:
        raise RuntimeError("unexpected HKEX list layout")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, row in frame.iterrows():
        raw_code = str(row.iloc[0] or "").strip()
        if not raw_code.isdigit():
            continue
        code = raw_code.zfill(5)
        name = str(row.iloc[1] or "").strip()
        category = str(row.iloc[2] or "").strip()
        if category.lower() != "equity" or not code.isdigit() or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": name})
    if not rows:
        raise RuntimeError("HK universe is empty")
    return rows


def _parse_hk_org_profile(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = (payload.get("result") or {}).get("data") or []
    if not rows:
        return None
    row = rows[0]
    return {
        "code": str(row.get("SECURITY_CODE") or "").strip(),
        "name": "",
        "org_name": str(row.get("ORG_NAME") or "").strip(),
        "org_name_en": str(row.get("ORG_EN_ABBR") or "").strip(),
        "industry_em": str(row.get("BELONG_INDUSTRY") or "").strip(),
        "industry_csrc": "",
        "province": str(row.get("REG_PLACE") or "").strip(),
        "address": str(row.get("ADDRESS") or "").strip(),
        "org_web": str(row.get("ORG_WEB") or "").strip(),
        "org_tel": str(row.get("ORG_TEL") or "").strip(),
        "chairman": str(row.get("CHAIRMAN") or "").strip(),
        "secretary": str(row.get("SECRETARY") or "").strip(),
        "emp_num": _num(row.get("EMP_NUM")),
        "org_profile": str(row.get("ORG_PROFILE") or "").strip(),
        "business_scope": "",
        "found_date": str(row.get("FOUND_DATE") or "").strip(),
        "listing_info": {},
    }


def fetch_hk_company_profile(code: str, *, quote: TencentQuote | None = None) -> dict[str, Any]:
    """Fetch and parse one HK company's Eastmoney F10 org profile."""
    import urllib.parse

    params = {
        "reportName": "RPT_HKF10_INFO_ORGPROFILE",
        "columns": (
            "SECUCODE,SECURITY_CODE,ORG_NAME,ORG_EN_ABBR,BELONG_INDUSTRY,FOUND_DATE,CHAIRMAN,"
            "SECRETARY,ACCOUNT_FIRM,REG_ADDRESS,ADDRESS,YEAR_SETTLE_DAY,EMP_NUM,ORG_TEL,ORG_FAX,"
            "ORG_EMAIL,ORG_WEB,ORG_PROFILE,REG_PLACE"
        ),
        "quoteColumns": "",
        "filter": f'(SECUCODE="{code}.HK")',
        "pageNumber": "1",
        "pageSize": "200",
        "sortTypes": "",
        "sortColumns": "",
        "source": "F10",
        "client": "PC",
        "v": "04748497219912483",
    }
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get?" + urllib.parse.urlencode(params)
    body = _governed_fetch(url, timeout=18.0, encoding="utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON for HK {code}: {body[:120]!r}") from error
    record = _parse_hk_org_profile(payload)
    if record is None:
        raise RuntimeError(f"empty HK org profile for {code}")
    record["code"] = code
    record["name"] = quote.name if quote and quote.name else record["org_name"]
    return record


# ---------------------------------------------------------------------------
# 腾讯批量行情解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TencentQuote:
    symbol: str
    name: str
    code: str
    price: float | None
    prev_close: float | None
    change_pct: float | None
    total_market_cap_yi: float | None
    float_market_cap_yi: float | None
    pe: float | None
    pb: float | None
    high: float | None
    low: float | None
    turnover_rate: float | None
    volume_ratio: float | None
    amount_yi: float | None
    quote_time: str


def parse_tencent_quotes(text: str) -> list[TencentQuote]:
    """Parse the ``v_sh600519="1~...";`` bulk quote payload (GBK decoded)."""
    quotes: list[TencentQuote] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("v_"):
            continue
        quote_body = line.split("=", 1)[-1]
        if not (quote_body.startswith('"') and quote_body.endswith('";')):
            continue
        fields = quote_body[1:-2].split("~")
        if len(fields) < 48:
            continue
        symbol = line[2:].split("=", 1)[0].strip()
        code = fields[2].strip()
        if not code:
            continue
        quotes.append(
            TencentQuote(
                symbol=symbol,
                name=fields[1].strip(),
                code=code,
                price=_num(fields[3]),
                prev_close=_num(fields[4]),
                change_pct=_num(fields[32]),
                total_market_cap_yi=_num(fields[45]),
                float_market_cap_yi=_num(fields[44]),
                pe=_num(fields[39]),
                pb=_num(fields[46]),
                high=_num(fields[33]),
                low=_num(fields[34]),
                turnover_rate=_num(fields[38]),
                volume_ratio=_num(fields[49]) if len(fields) > 49 else None,
                amount_yi=_num(fields[37]),
                quote_time=fields[30],
            )
        )
    return quotes


def fetch_tencent_quotes(codes: Sequence[str], *, batch_size: int = 60) -> dict[str, TencentQuote]:
    """Batch-fetch Tencent quotes for many codes (one request per batch)."""
    result: dict[str, TencentQuote] = {}
    for start in range(0, len(codes), batch_size):
        chunk = codes[start : start + batch_size]
        symbol_text = ",".join(_tencent_symbol(code) for code in chunk)
        text = _get(f"https://qt.gtimg.cn/q={symbol_text}", timeout=20.0, encoding="gbk")
        for quote in parse_tencent_quotes(text):
            result[quote.code] = quote
        time.sleep(0.35)
    return result


# ---------------------------------------------------------------------------
# 东财 F10 公司概况
# ---------------------------------------------------------------------------


def parse_company_survey(payload: Mapping[str, Any], *, code: str, quote: TencentQuote | None) -> dict[str, Any] | None:
    """Extract the F10 company-survey record from the PageAjax response."""
    basic = (payload.get("jbzl") or [{}])[0]
    if not basic:
        return None
    survey_name = str(basic.get("SECURITY_NAME_ABBR") or "").strip()
    name = survey_name or (quote.name if quote else "") or ""
    return {
        "code": code,
        "name": name,
        "org_name": basic.get("ORG_NAME") or "",
        "org_name_en": basic.get("ORG_NAME_EN") or "",
        "former_name": basic.get("FORMERNAME") or "",
        "security_type": basic.get("SECURITY_TYPE") or "",
        "trade_market": basic.get("TRADE_MARKET") or "",
        "industry_em": basic.get("EM2016") or "",
        "industry_csrc": basic.get("INDUSTRYCSRC1") or "",
        "province": basic.get("PROVINCE") or "",
        "address": basic.get("ADDRESS") or "",
        "org_web": basic.get("ORG_WEB") or "",
        "org_tel": basic.get("ORG_TEL") or "",
        "chairman": basic.get("CHAIRMAN") or "",
        "president": basic.get("PRESIDENT") or "",
        "legal_person": basic.get("LEGAL_PERSON") or "",
        "secretary": basic.get("SECRETARY") or "",
        "reg_capital_wan": _num(basic.get("REG_CAPITAL")),
        "emp_num": _num(basic.get("EMP_NUM")),
        "org_profile": str(basic.get("ORG_PROFILE") or "").strip(),
        "business_scope": str(basic.get("BUSINESS_SCOPE") or "").strip(),
        "listing_info": (payload.get("fxxg") or [{}])[0] if payload.get("fxxg") else {},
    }


def fetch_company_survey(
    code: str, *, market: str = "CN", quote: TencentQuote | None = None
) -> dict[str, Any]:
    """Fetch and parse one company's Eastmoney F10 survey (CN or HK)."""
    if market.upper() == "HK":
        return fetch_hk_company_profile(code, quote=quote)
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={_eastmoney_code(code)}"
    body = _governed_fetch(url, timeout=18.0, encoding="utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON for {code}: {body[:120]!r}") from error
    record = parse_company_survey(payload, code=code, quote=quote)
    if record is None:
        raise RuntimeError(f"empty CompanySurvey payload for {code}")
    return record


def _finite_field(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        number = _num(value)
        if number is not None and math.isfinite(number):
            return number
    return None


def _ratio_as_percentage(ratio: float | None) -> float | None:
    if ratio is None:
        return None
    value = ratio * 100.0
    return value if math.isfinite(value) else None


def parse_business_analysis(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract a structured revenue breakdown from the BusinessAnalysis payload.

    Every row keeps its report period, raw Eastmoney type and all available
    cost / gross-profit fields.  The canonical percentage is 0-100
    (``revenue_share_pct``); the original 0-1 ratio is preserved as ``ratio``
    so legacy consumers keep working.  No fixed ``[:20]`` truncation is
    applied: the full provider payload is retained.
    """
    rows = payload.get("zygcfx") or []
    breakdown: list[dict[str, Any]] = []
    for row in rows:
        item = str(row.get("ITEM_NAME") or row.get("item_name") or "").strip()
        if not item:
            continue
        ratio = _finite_field(row, "MBI_RATIO", "ratio")
        income = _finite_field(row, "MAIN_BUSINESS_INCOME", "revenue")
        cost = _finite_field(row, "MAIN_BUSINESS_COST", "cost")
        gross_profit = _finite_field(row, "MAIN_BUSINESS_RPOFIT", "gross_profit")
        gross_margin_ratio = _finite_field(row, "GROSS_RPOFIT_RATIO")
        gross_margin_pct = (_ratio_as_percentage(gross_margin_ratio) if gross_margin_ratio is not None
                            else _finite_field(row, "gross_margin_pct"))
        classification = _classification_from_type(str(row.get("MAINOP_TYPE") or "").strip())
        breakdown.append(
            {
                "type": str(row.get("MAINOP_TYPE") or "").strip(),
                "item": item,
                "item_name": item,
                "income": income,
                "revenue": income,
                "currency": "CNY",
                "revenue_share_pct": _ratio_as_percentage(ratio),
                "ratio": ratio,
                "cost": cost,
                "gross_profit": gross_profit,
                "gross_margin_pct": gross_margin_pct,
                "period": str(row.get("REPORT_DATE") or "").strip() or None,
                "source": "eastmoney_f10",
                "classification": classification[0] if classification else None,
                "classification_label": classification[1] if classification else None,
            }
        )
    return breakdown


def _classification_from_type(raw_type: str) -> tuple[str, str] | None:
    """Map the provider's own MAINOP_TYPE to a conservative classification.

    The Eastmoney taxonomy is factual: 1 = by industry, 2 = by product,
    3 = by region.  Region rows are retained for display/provenance but are
    never used as a company's largest business/product.
    """
    if raw_type == "1":
        return ("industry", "行业")
    if raw_type == "2":
        return ("product", "产品")
    if raw_type == "3":
        return ("region", "地区")
    return None


def fetch_business_analysis(code: str) -> dict[str, Any]:
    """Fetch and parse one A-share company's business/revenue breakdown."""
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?code={_eastmoney_code(code)}"
    body = _governed_fetch(url, timeout=18.0, encoding="utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON for {code} business: {body[:120]!r}") from error
    return {"code": code, "revenue_breakdown": parse_business_analysis(payload), "fetched_at": _now()}


def _revenue_period(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _latest_revenue_period(rows: Any, *, require_all: bool = False) -> date | None:
    if not isinstance(rows, list) or not rows:
        return None
    periods: list[date] = []
    for row in rows:
        period = _revenue_period(row.get("period")) if isinstance(row, Mapping) else None
        if period is None:
            if require_all:
                return None
            continue
        periods.append(period)
    return max(periods) if periods else None


def _retain_earlier_revenue_periods(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    """Keep verified periods older than the new response, preserving their provenance."""
    result = dict(current)
    fresh = list(current.get("revenue_breakdown") or [])
    fresh_dates = [_revenue_period(row.get("period")) for row in fresh if isinstance(row, Mapping)]
    if not fresh_dates or any(period is None for period in fresh_dates):
        return result
    earliest = min(period for period in fresh_dates if period is not None)
    older = []
    for row in _revenue_rows_with_fetched_at(previous):
        period = _revenue_period(row.get("period"))
        if period is not None and period < earliest:
            older.append(row)
    result["revenue_breakdown"] = fresh + older
    return result


def _recover_revenue_publication_locked(data_root: Path) -> bool:
    marker = Path(data_root) / "f10" / "cn" / "revenue_publication_pending.json"
    if not marker.exists():
        return False
    export_atlas_f10(data_root, markets=("CN",))
    marker.unlink()
    return True


@_release_after_fetch
def recover_revenue_publication(data_root: Path, *, market: str = "CN") -> dict[str, Any]:
    """Republish local data only; never contact an external provider."""
    if market.upper() != "CN":
        raise ValueError("revenue publication recovery is only implemented for CN")
    data_root = Path(data_root)
    if not _acquire_market_lock(data_root, "CN"):
        return {"status": "SKIPPED"}
    return {"status": "PUBLISHED" if _recover_revenue_publication_locked(data_root) else "NO_PENDING"}


@_release_after_fetch
def run_revenue_fetch(
    data_root: Path,
    *,
    market: str = "CN",
    limit: int = 200,
    detail_delay_seconds: float = 1.0,
    codes: Sequence[str] | None = None,
    refresh_after_days: int | None = 90,
) -> dict[str, Any]:
    """Fetch Eastmoney BusinessAnalysis for already-collected CN companies.

    Missing records are fetched first, then old observations in bounded batches.
    Fetch timestamps are not financial-report publication dates.
    """
    market_key = market.upper()
    if market_key != "CN":
        raise ValueError("revenue breakdown is only implemented for CN")
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if detail_delay_seconds < 0.02:
        raise ValueError("detail_delay_seconds must be at least 0.02")
    data_root = Path(data_root)
    started_at = _now()
    directory = data_root / "f10" / "cn"
    directory.mkdir(parents=True, exist_ok=True)
    if not _acquire_market_lock(data_root, market_key):
        return {
            "market": market_key,
            "status": "SKIPPED",
            "message": "another process holds the lock for CN",
        }
    existing = _load_existing_records(data_root, "CN", strict=True)
    if codes is None:
        codes = [str(record.get("code") or "") for record in existing.values() if record.get("code")]
    codes = [code for code in codes if code]
    done = _load_revenue_records(data_root, market_key)
    publication_marker = directory / "revenue_publication_pending.json"
    recovered_publication = _recover_revenue_publication_locked(data_root)
    usable = {code: record for code, record in done.items()
              if _latest_revenue_period(record.get("revenue_breakdown"), require_all=True) is not None}
    pending = _pending_detail_codes(codes, usable, now=datetime.now(timezone.utc),
                                    refresh_after_days=refresh_after_days)
    retry_path = directory / "revenue_retry.json"
    retry_queue = _load_retry_queue(retry_path)
    pending = _order_retry_queue(pending, retry_queue)
    already_done = len(done)
    fetched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, code in enumerate(pending[:limit], start=1):
        try:
            record = fetch_business_analysis(code)
            current_period = _latest_revenue_period(record.get("revenue_breakdown"), require_all=True)
            if current_period is None:
                raise ValueError("no usable revenue breakdown with verified report periods")
            previous_period = _latest_revenue_period((done.get(code) or {}).get("revenue_breakdown"))
            if previous_period is not None and current_period < previous_period:
                raise ValueError("revenue report period regressed; previous observation retained")
            record = _retain_earlier_revenue_periods(record, done.get(code) or {})
            if not publication_marker.exists():
                _write_summary(publication_marker, {"started_at": _now()})
            _append_jsonl(_record_path(data_root, "CN", "revenue"), record)
            done[code] = record
            fetched.append(record)
        except Exception as error:
            errors.append({"code": code, "error": f"{type(error).__name__}: {error}"[:300]})
            _move_retry_to_end(retry_queue, code)
            _write_summary(retry_path, {"retry_queue": retry_queue})
            if len(errors) >= 8:
                break
        else:
            if code in retry_queue:
                retry_queue.remove(code)
                _write_summary(retry_path, {"retry_queue": retry_queue})
        if index < len(pending[:limit]):
            time.sleep(detail_delay_seconds)
    if fetched or not recovered_publication:
        export_atlas_f10(data_root, markets=("CN",))
        publication_marker.unlink(missing_ok=True)
    summary = {
        "market": "CN",
        "started_at": started_at,
        "completed_at": _now(),
        "total_codes": len(codes),
        "already_done": already_done,
        "new_revenue": len(fetched),
        "pending_before": len(pending),
        "remaining_details": max(0, len(pending) - len(fetched)),
        "refresh_after_days": refresh_after_days,
        "total_revenue": len(done),
        "failed_codes": len(errors),
        "errors": errors[-10:],
        "status": "PARTIAL_FAILURE" if errors else "PARTIAL" if len(fetched) < len(pending) else "PASS",
    }
    _write_summary(directory / "revenue_summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# 断点状态与落盘
# ---------------------------------------------------------------------------


def _state_path(root: Path, market: str) -> Path:
    return root / "f10" / market.lower() / "state.json"


def _write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_f10_batch_summary(data_root: Path, kind: str) -> dict[str, Any] | None:
    """Only expose aggregate counters, never provider errors or company codes."""
    paths = {"F10_UPDATE_CN": ("cn", "summary.json"), "F10_UPDATE_HK": ("hk", "summary.json"),
             "REVENUE_UPDATE": ("cn", "revenue_summary.json")}
    if kind not in paths:
        return None
    market, filename = paths[kind]
    path = Path(data_root) / "f10" / market / filename
    try:
        if path.stat().st_size > 100_000:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        status = value.get("status")
        count = value.get("new_revenue" if kind == "REVENUE_UPDATE" else "new_details")
        remaining, failed = value.get("remaining_details"), value.get("failed_codes")
        completed = value.get("completed_at")
        if (status not in {"PASS", "PARTIAL", "PARTIAL_FAILURE"}
                or any(type(number) is not int or number < 0 for number in (count, remaining, failed))
                or not isinstance(completed, str)):
            return None
        parsed = datetime.fromisoformat(completed)
        if parsed.tzinfo is None:
            return None
        return {"status": status, "updated": count, "remaining": remaining,
                "failed": failed, "completedAt": parsed.astimezone(timezone.utc).isoformat()}
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def _load_state(root: Path, market: str) -> dict[str, Any]:
    path = _state_path(root, market)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(root: Path, market: str, state: Mapping[str, Any]) -> None:
    path = _state_path(root, market)
    _write_summary(path, state)


def _load_retry_queue(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_retry_queue(value.get("retry_queue", []))
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _normalize_retry_queue(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(code for code in value if isinstance(code, str) and code))


def _move_retry_to_end(queue: list[str], code: str) -> None:
    if code in queue:
        queue.remove(code)
    queue.append(code)


def _order_retry_queue(pending: Sequence[str], queue: Sequence[str]) -> list[str]:
    """Try fresh work first, then failures from least to most recently attempted."""
    deferred = set(queue)
    eligible = set(pending)
    return [code for code in pending if code not in deferred] + [code for code in queue if code in eligible]


def _record_path(root: Path, market: str, kind: str) -> Path:
    return root / "f10" / market.lower() / f"{kind}_{_today_compact()}.jsonl"


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_revenue_records(root: Path, market: str) -> dict[str, dict[str, Any]]:
    """Fail closed on incomplete or unreadable local revenue source files."""
    records: dict[str, dict[str, Any]] = {}
    directory = Path(root) / "f10" / market.lower()
    for path in sorted(directory.glob("revenue_*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict) or not str(record.get("code") or "").strip():
                        raise ValueError("invalid revenue record")
                    breakdown = record.get("revenue_breakdown")
                    if breakdown is not None and (not isinstance(breakdown, list)
                                                  or any(not isinstance(row, dict) for row in breakdown)):
                        raise ValueError("invalid revenue breakdown")
                    records[str(record["code"]).strip()] = record
        except (OSError, ValueError, UnicodeError):
            raise ValueError(f"{market.upper()} revenue records cannot be read safely") from None
    return records


def _revenue_rows_with_fetched_at(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Attach the parent revenue fetch time without replacing row-level provenance."""
    fetched_at = record.get("fetched_at")
    if isinstance(fetched_at, str):
        try:
            fetched_at = fetched_at if datetime.fromisoformat(fetched_at).tzinfo is not None else None
        except ValueError:
            fetched_at = None
    else:
        fetched_at = None
    rows = []
    for item in record.get("revenue_breakdown") or []:
        row = dict(item)
        if fetched_at and not row.get("fetched_at"):
            row["fetched_at"] = fetched_at
        rows.append(row)
    return rows


def _load_existing_records(root: Path, market: str, *, strict: bool = False) -> dict[str, dict[str, Any]]:
    """Load all previous detail records from JSONL files (by code)."""
    records: dict[str, dict[str, Any]] = {}
    directory = root / "f10" / market.lower()
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("details_*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if strict:
                        raise ValueError("F10 detail record is malformed") from None
                    continue
                if not isinstance(record, dict):
                    if strict:
                        raise ValueError("F10 detail record is malformed")
                    continue
                code = str(record.get("code") or "").strip()
                if code:
                    records[code] = record
        except OSError:
            if strict:
                raise
            continue
    return records


def _pending_detail_codes(codes: Sequence[str], existing: Mapping[str, Mapping[str, Any]], *,
                          now: datetime, refresh_after_days: int | None,
                          force: bool = False) -> list[str]:
    """Prioritize missing details, then oldest fetch times; never infer report dates."""
    if now.tzinfo is None or (refresh_after_days is not None and refresh_after_days < 1):
        raise ValueError("invalid F10 refresh policy")
    missing = [code for code in codes if code not in existing]
    if force:
        return list(codes)
    if refresh_after_days is None:
        return missing
    cutoff = now.astimezone(timezone.utc) - timedelta(days=refresh_after_days)
    stale: list[tuple[datetime, int, str]] = []
    for index, code in enumerate(codes):
        record = existing.get(code)
        if record is None:
            continue
        try:
            stamp = datetime.fromisoformat(str(record.get("detail_fetched_at") or record.get("fetched_at") or ""))
            if stamp.tzinfo is None:
                raise ValueError("naive timestamp")
            stamp = stamp.astimezone(timezone.utc)
            if stamp > now.astimezone(timezone.utc):
                raise ValueError("future timestamp")
        except (ValueError, TypeError, OverflowError):
            stamp = datetime.min.replace(tzinfo=timezone.utc)
        if stamp <= cutoff:
            stale.append((stamp, index, code))
    return missing + [code for _, _, code in sorted(stale)]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


@_release_after_fetch
def run_f10_fetch(
    data_root: Path,
    *,
    market: str = "CN",
    limit_details: int = 200,
   detail_delay_seconds: float = 1.2,
   skip_quotes: bool = False,
   force_details: bool = False,
   refresh_after_days: int | None = 90,
) -> dict[str, Any]:
   """Run one throttled F10 fetch pass and persist a snapshot.

   - Always refreshes the bulk quote snapshot (one request per 60 symbols).
   - Fetches missing details first, then oldest stale details, bounded by
     ``limit_details``. Fetch time is not a report publication date.
   """
   if market.upper() not in {"CN", "HK"}:
       raise ValueError("market must be CN or HK")
   if limit_details < 0:
       raise ValueError("limit_details must be non-negative")
   if detail_delay_seconds < 0.02:
       raise ValueError("detail_delay_seconds must be at least 0.02")
   data_root = Path(data_root)
   data_root.mkdir(parents=True, exist_ok=True)
   started_at = _now()

   market_key = market.upper()
   if not _acquire_market_lock(data_root, market_key):
       return {
           "market": market_key,
           "status": "SKIPPED",
           "message": f"another process holds the lock for {market_key}",
       }
   if market_key == "CN":
       universe = load_a_share_universe(cache_dir=data_root / "f10" / "cn")
   elif market_key == "HK":
       universe = load_hk_universe(cache_dir=data_root / "f10" / "hk")
   else:
       raise ValueError("market must be CN or HK")
   if not universe:
       raise RuntimeError(f"empty {market_key} universe")
   codes = [item["code"] for item in universe]

   quotes: dict[str, TencentQuote] = {}
   if not skip_quotes:
       quotes = fetch_tencent_quotes(codes)
       quote_rows = [
           {
               "code": quote.code,
               "name": quote.name,
               "price": quote.price,
               "prev_close": quote.prev_close,
               "change_pct": quote.change_pct,
               "total_market_cap_yi": quote.total_market_cap_yi,
               "float_market_cap_yi": quote.float_market_cap_yi,
               "pe": quote.pe,
               "pb": quote.pb,
               "high": quote.high,
               "low": quote.low,
               "turnover_rate": quote.turnover_rate,
               "volume_ratio": quote.volume_ratio,
               "amount_yi": quote.amount_yi,
               "quote_time": quote.quote_time,
               "fetched_at": _now(),
           }
           for quote in quotes.values()
       ]
       _append_jsonl(_record_path(data_root, market_key, "quotes"), {"fetched_at": _now(), "rows": quote_rows})

   state = _load_state(data_root, market_key)
   done_codes = set(str(item) for item in state.get("done", []))
   failed_codes = set(str(item) for item in state.get("failed", []))
   retry_queue = _normalize_retry_queue(state.get("retry_queue", []))
   for code in sorted(failed_codes - set(retry_queue)):
       retry_queue.append(code)
   existing = _load_existing_records(data_root, market_key, strict=True)
   pending = _pending_detail_codes(codes, existing, now=datetime.now(timezone.utc),
                                   refresh_after_days=refresh_after_days, force=force_details)
   if not force_details:
       pending = _order_retry_queue(pending, retry_queue)

   def save_retry_state() -> None:
       _save_state(data_root, market_key, {"done": sorted(done_codes), "failed": sorted(failed_codes),
                                          "retry_queue": retry_queue, "updated_at": _now()})

   fetched: list[dict[str, Any]] = []
   errors: list[dict[str, str]] = []
   for index, code in enumerate(pending[:limit_details], start=1):
       try:
           record = fetch_company_survey(code, market=market_key, quote=quotes.get(code))
           fetched_at = _now()
           previous = existing.get(code, {})
           for key in ("org_name", "org_profile", "business_scope", "main_business"):
               if previous.get(key) and not record.get(key):
                   raise ValueError("F10 response omits previously available company fields")
           record.update(
               {
                   "market": market_key,
                   "created_at": previous.get("created_at") or previous.get("detail_fetched_at") or fetched_at,
                   "detail_fetched_at": fetched_at,
                   "quote": (quotes.get(code) and {
                       "total_market_cap_yi": quotes[code].total_market_cap_yi,
                       "float_market_cap_yi": quotes[code].float_market_cap_yi,
                       "pe": quotes[code].pe,
                       "pb": quotes[code].pb,
                       "price": quotes[code].price,
                   }) or previous.get("quote"),
               }
           )
           _append_jsonl(_record_path(data_root, market_key, "details"), record)
           fetched.append(record)
           done_codes.add(code)
           failed_codes.discard(code)
       except Exception as error:
           errors.append({"code": code, "error": f"{type(error).__name__}: {error}"[:300]})
           failed_codes.add(code)
           _move_retry_to_end(retry_queue, code)
           save_retry_state()
           if len(errors) >= 8:
               break  # source is likely blocking us; stop before making it worse
       else:
           if code in retry_queue:
               retry_queue.remove(code)
               save_retry_state()
       if index < len(pending[:limit_details]):
           time.sleep(detail_delay_seconds)
       if index % 25 == 0:
           save_retry_state()

   save_retry_state()

   # Persist a queryable snapshot table in the catalog.
   combined_by_code = dict(existing)
   combined_by_code.update({str(record["code"]): record for record in fetched})
   combined = list(combined_by_code.values())
   if quotes:
       for record in combined:
           code = str(record.get("code") or "")
           quote = quotes.get(code)
           if quote is not None:
               record["quote"] = {
                   "total_market_cap_yi": quote.total_market_cap_yi,
                   "float_market_cap_yi": quote.float_market_cap_yi,
                   "pe": quote.pe,
                   "pb": quote.pb,
                   "price": quote.price,
                   "amount_yi": quote.amount_yi,
               }
   try:
       if not combined:
           raise ValueError("empty F10 snapshot; existing records retained")
       store = MarketStore(data_root)
       try:
           store.register_default_datasets()
           store.connection.execute(
               """
               CREATE TABLE IF NOT EXISTS f10_company (
                   code VARCHAR PRIMARY KEY,
                   market VARCHAR,
                   name VARCHAR,
                   org_name VARCHAR,
                   industry_em VARCHAR,
                   industry_csrc VARCHAR,
                   total_market_cap_yi DOUBLE,
                   float_market_cap_yi DOUBLE,
                   profile VARCHAR,
                   business_scope VARCHAR,
                   record JSON,
                   fetched_at VARCHAR
               )
               """
           )
           store.connection.execute("BEGIN TRANSACTION")
           store.connection.execute("DELETE FROM f10_company WHERE market = ?", [market_key])
           for record in combined:
               quote = record.get("quote") or {}
               store.connection.execute(
                   """
                   INSERT INTO f10_company
                   (code, market, name, org_name, industry_em, industry_csrc,
                    total_market_cap_yi, float_market_cap_yi, profile, business_scope,
                    record, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   """,
                   [
                       record.get("code"),
                       market_key,
                       record.get("name"),
                       record.get("org_name"),
                       record.get("industry_em"),
                       record.get("industry_csrc"),
                       quote.get("total_market_cap_yi"),
                       quote.get("float_market_cap_yi"),
                       record.get("org_profile"),
                       record.get("business_scope"),
                       json.dumps(record, ensure_ascii=False),
                       record.get("detail_fetched_at") or _now(),
                   ],
               )
           store.connection.commit()
       finally:
           store.close()
   except Exception as error:
       errors.append({"code": "__persist__", "error": f"{type(error).__name__}: {error}"[:300]})

   try:
       export_atlas_f10(data_root, markets=(market_key,))
   except Exception as error:
       errors.append({"code": "__atlas_export__", "error": f"{type(error).__name__}: {error}"[:300]})
   completed_at = _now()
   summary = {
       "market": market_key,
       "started_at": started_at,
       "completed_at": completed_at,
       "universe_count": len(codes),
       "universe_scope": "LOCAL_LIST_MAY_BE_STALE" if market_key == "HK" else "CACHED_OR_PUBLIC_LIST",
       "quote_count": len(quotes),
       "new_details": len(fetched),
       "pending_before": len(pending),
       "remaining_details": max(0, len(pending) - len(fetched)),
       "refresh_after_days": refresh_after_days,
       "total_details": len(combined),
       "failed_codes": len(errors),
       "errors": errors[-10:],
       "detail_delay_seconds": detail_delay_seconds,
       "status": "PARTIAL_FAILURE" if errors else "PARTIAL" if len(fetched) < len(pending) else "PASS",
   }
   target = data_root / "f10" / market_key.lower() / "summary.json"
   target.parent.mkdir(parents=True, exist_ok=True)
   _write_summary(target, summary)
   return summary


def _atlas_record(
    record: Mapping[str, Any],
    quote: Mapping[str, Any] | None,
    revenue: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize a raw F10 record to the industry-atlas compact contract.

    Market caps are emitted as canonical MoneySnapshot dicts (value in base
    currency units, currency, asOf, source) so no consumer can render a
    number without knowing when and where it came from.  Legacy Tencent
    ``*_yi`` scalars are converted with 1 yi = 1e8 units.  A missing float
    market cap may be derived from a same-day quote price and share counts
    (CN only); ``asOf`` is always the real quote time, never the detail-page
    fetch time.  Reasons for a missing cap are attached so callers can show
    ``暂无数据`` instead of a bare blank.
    """
    market = "HK" if str(record.get("market") or "").upper() == "HK" else "CN"
    fetched_at = str(record.get("detail_fetched_at") or record.get("fetched_at") or "").strip()
    revenue_rows, _ = migrate_revenue_rows(list(revenue or record.get("revenue_breakdown") or []))
    total_cap, float_cap, cap_reasons = derive_market_caps(record, quote, market=market)
    out: dict[str, Any] = {
        "code": record.get("code"),
        "market": market,
        "name": record.get("name") or "",
        "full_name": record.get("org_name") or "",
        "total_market_cap": total_cap,
        "float_market_cap": float_cap,
        "industry": record.get("industry_em") or record.get("industry") or "",
        "csrc_industry": record.get("industry_csrc") or record.get("csrc_industry") or "",
        "industry_tdx": record.get("industry_tdx") or "",
        "industry_sw": record.get("industry_sw") or "",
        "industry_em": record.get("industry_em") or "",
        "industry_hs": record.get("industry_hs") or "",
        "profile": record.get("org_profile") or record.get("profile") or "",
        "main_business": record.get("main_business") or "",
        "business_scope": record.get("business_scope") or "",
        "company_position": record.get("company_position") or record.get("position") or "",
        "company_highlight": record.get("company_highlight") or record.get("highlight") or "",
        "company_website": record.get("company_website") or record.get("org_web") or "",
        "total_shares": record.get("total_shares"),
        "float_shares": record.get("float_shares"),
        "largest_revenue_segment": largest_revenue_segment(revenue_rows),
        "revenue_breakdown": revenue_rows,
        "products": record.get("products") or [],
        "source": record.get("source") or "eastmoney_f10",
        "fetched_at": fetched_at,
        "status": record.get("status") or "ok",
        "provenance": record.get("provenance") or {},
    }
    if cap_reasons:
        out["market_cap_missing_reasons"] = cap_reasons
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def export_atlas_f10(data_root: Path, *, markets: Sequence[str] = ("CN", "HK")) -> dict[str, Any]:
    """Serialize cross-market snapshot publication across collector processes."""
    from .kpl_auto_refresh import _lock

    with _lock(Path(data_root) / "f10" / "export.guard.lock"):
        return _export_atlas_f10(data_root, markets=markets)


def _export_atlas_f10(data_root: Path, *, markets: Sequence[str]) -> dict[str, Any]:
    """Write ``industry/f10/{cn_f10,hk_f10}.jsonl`` + ``meta.json`` for the atlas."""
    root = Path(data_root)
    target_dir = root / "industry" / "f10"
    target_dir.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    active_markets = sorted({market.upper() for market in markets})
    if "CN" not in active_markets and (root / "f10" / "cn").is_dir():
        active_markets.append("CN")
    if "HK" not in active_markets and (root / "f10" / "hk").is_dir():
        active_markets.append("HK")
    for market_key in active_markets:
        filename = f"{market_key.lower()}_f10.jsonl"
        target = target_dir / filename
        records = list(_load_existing_records(root, market_key, strict=True).values())
        if not records and target.is_file():
            raise ValueError(f"{market_key} F10 source is empty; existing snapshot retained")
        quotes: dict[str, dict[str, Any]] = {}
        revenue_map = {code: _revenue_rows_with_fetched_at(payload)
                       for code, payload in _load_revenue_records(root, market_key).items()}
        quote_dir = root / "f10" / market_key.lower()
        for path in sorted(quote_dir.glob("quotes_*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    for row in payload.get("rows") or []:
                        code = str(row.get("code") or "")
                        if code:
                            quotes[code] = row
            except (OSError, json.JSONDecodeError):
                raise ValueError(f"{market_key} quote records cannot be read safely") from None
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                count = 0
                for record in records:
                    code = str(record.get("code") or "")
                    line = _atlas_record(record, quotes.get(code), revenue_map.get(code))
                    if not line.get("name") and not line.get("full_name"):
                        continue
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                    count += 1
            if records and not count:
                raise ValueError(f"{market_key} F10 export has no valid companies")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        totals[filename] = count
    meta = {
        "generated_at": _now(),
        "source": "eastmoney_f10 + tencent_quotes",
        "markets": {
            market_key: {"records": totals.get(f"{market_key.lower()}_f10.jsonl", 0)}
            for market_key in active_markets
        },
        "note": "raw per-stock payloads live under data_control/f10/{cn,hk}/details_*.jsonl",
    }
    (target_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"meta": meta, "totals": totals}


def f10_status(data_root: Path, market: str = "CN") -> dict[str, Any]:
    """Return a compact status snapshot for dashboards / control center."""
    root = Path(data_root)
    directory = root / "f10" / market.lower()
    summary_path = directory / "summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {}
    existing = _load_existing_records(root, market)
    records_path = directory / "records.json"
    try:
        records_path.write_text(
            json.dumps(list(existing.values()), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except OSError:
        pass
    return {
        "market": market.upper(),
        "summary": summary,
        "record_count": len(existing),
        "records_path": str(records_path),
    }


if __name__ == "__main__":  # pragma: no cover
    market = sys.argv[1] if len(sys.argv) > 1 else "CN"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    print(json.dumps(run_f10_fetch(Path("data_control"), market=market, limit_details=limit), ensure_ascii=False))
