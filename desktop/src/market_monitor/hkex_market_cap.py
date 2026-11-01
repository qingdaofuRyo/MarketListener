"""Parse HKEX's public Main Board and GEM daily capitalisation archives."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests


MAIN_URL = ("https://www.hkex.com.hk/eng/stat/smstat/mthbull/"
            "rpt_data_statistics_archive_market_cap_2025_2029.json")
GEM_URL = ("https://www.hkex.com.hk/eng/stat/smstat/mthbull/"
           "rpt_data_statistics_archive_market_cap_gem_2024_2028.json")
_DATE_RE = re.compile(r"\d{4}/\d{2}/\d{2}")
_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})+")


@dataclass(frozen=True)
class MarketCapArchive:
    points: tuple[tuple[str, int], ...]
    main_sha256: str
    gem_sha256: str
    latest_main_day: str
    latest_gem_day: str


def _parse_archive(payload: Any, *, today: date) -> dict[str, int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), list) or len(payload["tables"]) != 1:
        raise ValueError("HKEX market-cap archive has no single data table")
    table = payload["tables"][0]
    if not isinstance(table, dict) or not isinstance(table.get("header"), list):
        raise ValueError("HKEX market-cap archive has no header")
    header = {item.get("col"): str(item.get("text") or "") for item in table["header"]
              if isinstance(item, dict)}
    if "Year/Month/Day" not in header.get(0, "") or not (
        "market capitalisation" in header.get(1, "").lower() and "HKD" in header.get(1, "")
    ):
        raise ValueError("HKEX market-cap header or currency changed")
    cells: dict[int, dict[int, str]] = {}
    for item in table.get("body", []):
        if not isinstance(item, dict) or type(item.get("row")) is not int or item.get("col") not in {0, 1}:
            raise ValueError("HKEX market-cap cell shape changed")
        row, col = item["row"], item["col"]
        if row < 1 or col in cells.setdefault(row, {}):
            raise ValueError("HKEX market-cap archive has duplicate cells")
        cells[row][col] = str(item.get("text") or "")
    points: dict[str, int] = {}
    for values in cells.values():
        raw_day, raw_amount = values.get(0, ""), values.get(1, "")
        if not _DATE_RE.fullmatch(raw_day) or not _AMOUNT_RE.fullmatch(raw_amount):
            raise ValueError("HKEX market-cap date or amount is malformed")
        day = date.fromisoformat(raw_day.replace("/", "-"))
        amount = int(raw_amount.replace(",", ""))
        if day > today or day.isoformat() in points or amount <= 0:
            raise ValueError("HKEX market-cap row is future, duplicate or non-positive")
        points[day.isoformat()] = amount
    if len(points) < 20:
        raise ValueError("HKEX market-cap archive is unexpectedly short")
    footer = table.get("footer")
    if not isinstance(footer, list) or not footer:
        raise ValueError("HKEX market-cap publication date is missing")
    footer_text = str(footer[0].get("text") or "") if isinstance(footer[0], dict) else ""
    publication = date.fromisoformat(max(points))
    if publication.strftime("%d %b %Y") not in footer_text:
        raise ValueError("HKEX market-cap footer disagrees with its latest row")
    return points


def _download(url: str, session: requests.Session) -> tuple[Any, str]:
    response = session.get(url, timeout=25, headers={"User-Agent": "MarketListener/1.0"})
    response.raise_for_status()
    if response.url.split("/", 3)[:3] != ["https:", "", "www.hkex.com.hk"]:
        raise ValueError("HKEX archive redirected outside the official HTTPS host")
    if len(response.content) > 2_000_000:
        raise ValueError("HKEX archive exceeds the expected size")
    return response.json(), hashlib.sha256(response.content).hexdigest()


def fetch_hkex_market_cap(*, session: requests.Session | None = None,
                          today: date | None = None) -> MarketCapArchive:
    own_session = session is None
    session = session or requests.Session()
    try:
        main_payload, main_sha = _download(MAIN_URL, session)
        gem_payload, gem_sha = _download(GEM_URL, session)
    finally:
        if own_session:
            session.close()
    checked_day = today or datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    main = _parse_archive(main_payload, today=checked_day)
    gem = _parse_archive(gem_payload, today=checked_day)
    common = sorted(main.keys() & gem.keys())
    if not common or common[-1] != max(main) or common[-1] != max(gem):
        raise ValueError("HKEX Main Board and GEM have no matching latest day")
    return MarketCapArchive(tuple((day, main[day] + gem[day]) for day in common),
                            main_sha, gem_sha, max(main), max(gem))


__all__ = ("GEM_URL", "MAIN_URL", "MarketCapArchive", "fetch_hkex_market_cap")
