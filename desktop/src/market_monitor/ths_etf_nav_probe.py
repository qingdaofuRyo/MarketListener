"""Private, bounded observations of THS ETF net-value growth rankings.

This is a net asset value list, not a traded-price ranking. A completed
download is research evidence only: it is never written to published Gold.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

URL = "https://fund.10jqka.com.cn/data/Net/info/ETF_rate_desc_0_0_{page}_200_0_0_0_jsonp_g.html"
PAGE_SIZE = 200
MAX_PAGES = 12
MAX_PAGE_BYTES = 512_000
_JSONP = re.compile(r"\A\s*g\s*\((.*)\)\s*;?\s*\Z", re.S)
_CODE = re.compile(r"\d{6}\Z")


def _parse(body: bytes, today: date) -> tuple[dict, list[dict]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("gb18030")
    match = _JSONP.fullmatch(text)
    if match is None:
        raise ValueError("invalid JSONP")
    value = json.loads(match.group(1), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict) or value["error"].get("id") != 0:
        raise ValueError("provider error")
    data = value.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict) or not isinstance(data.get("data"), dict):
        raise ValueError("invalid payload")
    info, items = data["info"], data["data"]
    if len(items) > PAGE_SIZE:
        raise ValueError("oversized page")
    rows = []
    for key, item in items.items():
        if not isinstance(item, dict):
            raise ValueError("invalid row")
        code = item.get("code")
        if not isinstance(code, str) or not _CODE.fullmatch(code) or key != "f" + code:
            raise ValueError("invalid code")
        raw_day = item.get("newdate")
        if raw_day in (None, "", "--", "-"):
            day = None
        elif isinstance(raw_day, str) and date.fromisoformat(raw_day).isoformat() == raw_day and date.fromisoformat(raw_day) <= today:
            day = raw_day
        else:
            raise ValueError("invalid date")
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 120:
            raise ValueError("invalid name")
        def number(field: str) -> float | None:
            raw = item.get(field)
            if raw in (None, "", "-", "--"):
                return None
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise ValueError("invalid number")
            result = float(raw)
            if not math.isfinite(result):
                raise ValueError("invalid number")
            return result
        net, rate = number("newnet"), number("rate")
        if net is not None and net <= 0:
            raise ValueError("invalid net value")
        rows.append({"code": code, "name": name, "navDate": day, "unitNav": net, "navGrowthPct": rate})
    return info, rows


def capture_etf_nav(root: Path, *, session: requests.Session | None = None) -> dict:
    """Read all pages once, fail closed on incomplete or inconsistent data."""
    root = Path(root)
    owned = session is None
    client = session or requests.Session()
    if owned:
        client.trust_env = False
    started = datetime.now(timezone.utc)
    today = started.astimezone(ZoneInfo("Asia/Shanghai")).date()
    pages: list[bytes] = []
    records: list[dict] = []
    info = None
    seen: set[str] = set()
    try:
        for page in range(1, MAX_PAGES + 1):
            response = client.get(URL.format(page=page), headers={"User-Agent": "Mozilla/5.0",
                "Referer": "https://fund.10jqka.com.cn/"}, timeout=(5, 10), allow_redirects=False, stream=True)
            try:
                if response.status_code != 200:
                    raise ValueError("provider unavailable")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES:
                        raise ValueError("oversized response")
                page_info, rows = _parse(bytes(body), today)
            finally:
                response.close()
            if info is None:
                info = page_info
            elif page_info != info:
                raise ValueError("provider snapshot changed")
            for row in rows:
                if row["code"] in seen:
                    raise ValueError("duplicate code")
                seen.add(row["code"])
            pages.append(bytes(body))
            records.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
        else:
            raise ValueError("pagination limit reached")
        if not records:
            raise ValueError("empty ranking")
        # The public page's count is the number of records updated on its
        # reported latest NAV date, not the total number of codes returned.
        # Check it to avoid committing a prematurely short pagination result.
        latest = info.get("star") if info is not None else None
        count = info.get("count") if info is not None else None
        if (not isinstance(latest, str) or type(count) is not int or count < 0
                or date.fromisoformat(latest).isoformat() != latest
                or date.fromisoformat(latest) > today
                or sum(row["navDate"] == latest for row in records) != count):
            raise ValueError("latest-date count mismatch")
        rates = [row["navGrowthPct"] for row in records if row["navGrowthPct"] is not None]
        if any(left < right for left, right in zip(rates, rates[1:])):
            raise ValueError("ranking order changed")
        fetched = datetime.now(timezone.utc)
        base = root / "bronze" / "ths_etf_nav"
        base.mkdir(parents=True, exist_ok=True)
        identifier = uuid4().hex
        stage, target = base / ("." + identifier + ".tmp"), base / identifier
        stage.mkdir()
        try:
            hashes = []
            for index, body in enumerate(pages, 1):
                (stage / f"page-{index:02}.raw").write_bytes(body)
                hashes.append(sha256(body).hexdigest())
            (stage / "manifest.json").write_text(json.dumps({"schema": "ths-etf-nav-bronze-v1",
                "requestedAt": started.isoformat(), "fetchedAt": fetched.isoformat(), "pageHashes": hashes,
                "pageCount": len(pages), "recordCount": len(records), "quality": "UNVERIFIED",
                "source": "THS_ETF_NAV_GROWTH", "records": records}, ensure_ascii=False), encoding="utf-8")
            os.replace(stage, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return {"status": "UNVERIFIED", "fetchedAt": fetched.isoformat(), "pageCount": len(pages),
                "recordCount": len(records), "missingDateCount": sum(row["navDate"] is None for row in records)}
    except (requests.RequestException, OSError, ValueError, TypeError, UnicodeError, OverflowError):
        return {"status": "FAILED", "fetchedAt": datetime.now(timezone.utc).isoformat()}
    finally:
        if owned:
            client.close()
