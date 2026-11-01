"""Read the public, monthly Tonghuashun CPI table without mixing TDX data."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


CPI_URL = "https://data.10jqka.com.cn/macro/cpi/"
_MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")
_PERCENT = re.compile(r"[+-]?\d+(?:\.\d+)?%\Z")
_FIELDS = (
    "national_index", "national_yoy_pct", "national_mom_pct", "national_ytd_index",
    "urban_index", "urban_yoy_pct", "urban_ytd_index",
    "rural_index", "rural_yoy_pct", "rural_ytd_index",
)
_PERCENT_FIELDS = {"national_yoy_pct", "national_mom_pct", "urban_yoy_pct", "rural_yoy_pct"}


@dataclass(frozen=True)
class CPIMonth:
    period: str
    national_index: float | None
    national_yoy_pct: float | None
    national_mom_pct: float | None
    national_ytd_index: float | None
    urban_index: float | None
    urban_yoy_pct: float | None
    urban_ytd_index: float | None
    rural_index: float | None
    rural_yoy_pct: float | None
    rural_ytd_index: float | None


@dataclass(frozen=True)
class CPIArchive:
    observations: tuple[CPIMonth, ...]
    source_url: str
    payload_sha256: str
    fetched_at: str


def _numeric(raw: str, *, percent: bool) -> float | None:
    if raw in {"", "-", "--", "—"}:
        return None
    if not (_PERCENT if percent else _NUMBER).fullmatch(raw):
        raise ValueError("Tonghuashun CPI contains malformed numeric fields")
    value = float(raw[:-1] if percent else raw)
    if not isfinite(value) or not percent and value <= 0:
        raise ValueError("Tonghuashun CPI contains an invalid numeric value")
    return value


def parse_cpi_table(html: str, *, as_of: datetime | None = None) -> tuple[CPIMonth, ...]:
    """Validate the full 11-column table and retain missing cells as null."""

    document = BeautifulSoup(html, "html.parser")
    candidates = [table for table in document.find_all("table") if table.select_one("thead")]
    if len(candidates) != 1:
        raise ValueError("Tonghuashun CPI table is missing or ambiguous")
    table = candidates[0]
    heads = [[cell.get_text(" ", strip=True) for cell in tr.find_all("th")]
             for tr in table.select("thead tr")]
    if heads != [["月份", "全国", "城市", "农村"],
                 ["当月", "同比增长", "环比增长", "累计", "当月", "同比增长", "累计", "当月", "同比增长", "累计"]]:
        raise ValueError("Tonghuashun CPI table headers changed")
    current = (as_of or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(
        ZoneInfo("Asia/Shanghai")
    ).strftime("%Y-%m")
    observed: set[str] = set()
    result: list[CPIMonth] = []
    for tr in table.select("tbody tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all("td")]
        if len(cells) != 11 or not _MONTH.fullmatch(cells[0]) or cells[0] > current or cells[0] in observed:
            raise ValueError("Tonghuashun CPI month or row shape changed")
        if result and cells[0] >= result[-1].period:
            raise ValueError("Tonghuashun CPI months are not descending")
        observed.add(cells[0])
        values = {field: _numeric(raw, percent=field in _PERCENT_FIELDS)
                  for field, raw in zip(_FIELDS, cells[1:], strict=True)}
        if values["national_index"] is None:
            raise ValueError("Tonghuashun CPI national index is absent")
        result.append(CPIMonth(cells[0], **values))
    if not result:
        raise ValueError("Tonghuashun CPI table contains no observations")
    return tuple(result)


def fetch_cpi_archive(*, session: requests.Session | None = None,
                      as_of: datetime | None = None) -> CPIArchive:
    """Fetch only the public page; caller decides whether to publish values."""

    own_session = session is None
    session = session or requests.Session()
    try:
        response = session.get(CPI_URL, timeout=20,
                               headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.10jqka.com.cn/"})
        response.raise_for_status()
        parsed = urlparse(response.url)
        if parsed.scheme != "https" or parsed.hostname != "data.10jqka.com.cn":
            raise ValueError("Tonghuashun CPI redirected outside its official HTTPS host")
        if len(response.content) > 2_000_000:
            raise ValueError("Tonghuashun CPI response is unexpectedly large")
        observations = parse_cpi_table(response.content.decode("gbk"), as_of=as_of)
        return CPIArchive(observations, CPI_URL, hashlib.sha256(response.content).hexdigest(),
                          datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finally:
        if own_session:
            session.close()


__all__ = ("CPI_URL", "CPIArchive", "CPIMonth", "fetch_cpi_archive", "parse_cpi_table")
