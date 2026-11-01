"""Parse the exchange's dated HTML member ranking report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re

from bs4 import BeautifulSoup
import pandas as pd


_SECTION = re.compile(r"^(品种|合约)\s*[：:]\s*(.*?)\s*日期\s*[：:]\s*(\d{4}-\d{2}-\d{2})$")
_CONTRACT = re.compile(r"[A-Z]{1,3}\d{3,4}")
_INTEGER = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)")
_MISSING = frozenset({"", "-", "--", "—", "－"})
_COLUMNS = (
    "rank",
    "vol_party_name", "vol", "vol_chg",
    "long_party_name", "long_open_interest", "long_open_interest_chg",
    "short_party_name", "short_open_interest", "short_open_interest_chg",
)


@dataclass
class _RankingSection:
    kind: str
    key: str
    header_seen: bool = False
    total_seen: bool = False
    rows: list[dict[str, object]] = field(default_factory=list)
    ranks: set[int] = field(default_factory=set)


def parse_czce_rank_html(raw: bytes, *, trading_day: str) -> dict[str, pd.DataFrame]:
    """Return per-contract frames from a CZCE ``FutureDataHolding.htm`` body.

    The official report also has variety summaries. They are checked for a
    dated ranking layout but omitted from the contract frames so downstream
    member rankings do not count the same positions twice.
    """

    try:
        expected_day = date.fromisoformat(trading_day)
    except ValueError as error:
        raise ValueError("CZCE ranking trading_day must be YYYYMMDD") from error
    if expected_day.strftime("%Y%m%d") != trading_day:
        raise ValueError("CZCE ranking trading_day must be YYYYMMDD")

    if raw.startswith(b"HTTP/"):
        _, separator, raw = raw.partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("CZCE ranking HTTP capture has no response body")
    if not raw:
        raise ValueError("CZCE ranking response is empty")
    soup = BeautifulSoup(raw, "html.parser")
    title = _cell_text(soup.title) if soup.title is not None else ""
    if "持仓排名" not in title:
        raise ValueError("CZCE ranking response is not a holding report")
    tables = soup.find_all("table")
    if len(tables) != 1:
        raise ValueError("CZCE ranking report must contain one ranking table")

    frames: dict[str, pd.DataFrame] = {}
    section: _RankingSection | None = None
    after_note = False
    for table_row in tables[0].find_all("tr"):
        cells = table_row.find_all(["td", "th"], recursive=False)
        values = [_cell_text(cell) for cell in cells]
        if len(values) == 1:
            heading = values[0]
            if heading.startswith("注：") or heading.startswith("注:"):
                if section is not None:
                    _finish_section(section, frames)
                    section = None
                after_note = True
                continue
            if after_note:
                raise ValueError("CZCE ranking has a section after its note")
            match = _SECTION.fullmatch(heading)
            if match is None:
                raise ValueError("CZCE ranking has an invalid section heading")
            kind, key, report_day = match.groups()
            if report_day != expected_day.isoformat():
                raise ValueError(f"CZCE ranking report date {report_day} does not match {expected_day}")
            if section is not None:
                _finish_section(section, frames)
            key = key.strip().upper()
            if kind == "合约" and _CONTRACT.fullmatch(key) is None:
                raise ValueError("CZCE ranking has an invalid contract")
            if not key:
                raise ValueError("CZCE ranking has an empty section key")
            section = _RankingSection(kind=kind, key=key)
            continue
        if section is None or after_note or len(values) != 10:
            raise ValueError("CZCE ranking table has a row outside a valid section")
        if not section.header_seen:
            _validate_header(values)
            section.header_seen = True
            continue
        if values[0] == "合计":
            if section.total_seen or not section.rows:
                raise ValueError("CZCE ranking has a misplaced total row")
            section.total_seen = True
            continue
        if section.total_seen:
            raise ValueError("CZCE ranking has a rank after its total row")
        rank = _parse_integer(values[0], field="rank")
        if rank <= 0 or rank in section.ranks:
            raise ValueError(f"CZCE ranking has an invalid or duplicate rank: {rank}")
        section.ranks.add(rank)
        record: dict[str, object] = {"rank": rank}
        for offset, name_column, amount_column, change_column in (
            (1, "vol_party_name", "vol", "vol_chg"),
            (4, "long_party_name", "long_open_interest", "long_open_interest_chg"),
            (7, "short_party_name", "short_open_interest", "short_open_interest_chg"),
        ):
            member, amount, change = _parse_direction(values[offset:offset + 3])
            record[name_column] = member
            record[amount_column] = amount
            record[change_column] = change
        if all(record[column] is None for column in ("vol_party_name", "long_party_name", "short_party_name")):
            raise ValueError("CZCE ranking has a rank with no published direction")
        section.rows.append(record)
    if section is not None:
        _finish_section(section, frames)
    if not frames:
        raise ValueError("CZCE ranking report has no dated contract rows")
    return frames


def _cell_text(cell: object) -> str:
    get_text = getattr(cell, "get_text")
    return " ".join(get_text(" ", strip=True).split())


def _validate_header(values: list[str]) -> None:
    if not (
        values[0] == "名次"
        and values[1] == values[4] == values[7] == "会员简称"
        and ("交易量" in values[2] or "成交量" in values[2])
        and "持买仓量" in values[5]
        and "持卖仓量" in values[8]
        and values[3] == values[6] == values[9] == "增减量"
    ):
        raise ValueError("CZCE ranking table has an unexpected column header")


def _parse_integer(value: str, *, field: str) -> int:
    if _INTEGER.fullmatch(value) is None:
        raise ValueError(f"CZCE ranking has an invalid {field}")
    return int(value.replace(",", ""))


def _parse_direction(values: list[str]) -> tuple[str | None, int | None, int | None]:
    member, amount, change = values
    if all(value in _MISSING for value in values):
        return None, None, None
    if member in _MISSING or amount in _MISSING:
        raise ValueError("CZCE ranking has an incomplete member direction")
    quantity = _parse_integer(amount, field="quantity")
    if quantity < 0:
        raise ValueError("CZCE ranking has a negative member quantity")
    difference = None if change in _MISSING else _parse_integer(change, field="change")
    return member, quantity, difference


def _finish_section(section: _RankingSection, frames: dict[str, pd.DataFrame]) -> None:
    if not section.header_seen or not section.rows or not section.total_seen:
        raise ValueError(f"CZCE ranking section is incomplete: {section.key[:40]}")
    if section.kind == "合约":
        if section.key in frames:
            raise ValueError(f"CZCE ranking has a duplicate contract: {section.key}")
        frames[section.key] = pd.DataFrame(section.rows, columns=_COLUMNS, dtype=object)


__all__ = ("parse_czce_rank_html",)
