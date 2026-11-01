"""Validate and parse dated CZCE XLS and XLSX member ranking workbooks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from html import escape
from io import BytesIO
import re
import zipfile

from openpyxl import load_workbook
import pandas as pd
import xlrd

from .czce_rank_html import parse_czce_rank_html

_TITLE_DATE = re.compile(r"(\d{4}[-/]\d{2}[-/]\d{2})")


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


@contextmanager
def _rows(raw: bytes):
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("CZCE ranking workbook exceeds size limits")
    if raw.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        workbook = xlrd.open_workbook(file_contents=raw, on_demand=True)
        try:
            if workbook.nsheets != 1:
                raise ValueError("CZCE ranking workbook has unexpected worksheets")
            sheet = workbook.sheet_by_index(0)
            yield (sheet.row_values(index) for index in range(sheet.nrows)), sheet.ncols
        finally:
            workbook.release_resources()
        return
    if not zipfile.is_zipfile(BytesIO(raw)):
        raise ValueError("CZCE ranking workbook is not a supported XLS/XLSX file")
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(info.file_size for info in infos) > 64 * 1024 * 1024:
            raise ValueError("CZCE ranking workbook exceeds size limits")
        if archive.testzip() is not None or "xl/workbook.xml" not in archive.namelist():
            raise ValueError("CZCE ranking workbook failed ZIP/XLSX checks")
    workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    try:
        if len(workbook.worksheets) != 1:
            raise ValueError("CZCE ranking workbook has unexpected worksheets")
        sheet = workbook.worksheets[0]
        yield sheet.iter_rows(values_only=True), sheet.max_column
    finally:
        workbook.close()


def parse_czce_rank_workbook_checked(raw: bytes, *, trading_day: str) -> dict[str, pd.DataFrame]:
    """Apply the HTML report gate to either published ten-column workbook format."""
    with _rows(raw) as (rows, columns):
        if columns < 10:
            raise ValueError("CZCE ranking workbook has fewer than ten columns")
        title_row = next(rows, None)
        title = _text(title_row[0]) if title_row else ""
        if "持仓排名" not in title:
            raise ValueError("CZCE ranking workbook lacks report title")
        match = _TITLE_DATE.search(title)
        if match is None:
            raise ValueError("CZCE ranking workbook title lacks a date")
        title_day = match.group(1).replace("/", "-")
        if title_day != date.fromisoformat(trading_day).isoformat():
            raise ValueError("CZCE ranking workbook title date differs from requested date")
        html_rows: list[str] = []
        for row in rows:
            values = [_text(value) for value in row]
            if not any(values):
                continue
            if any(values[10:]):
                raise ValueError("CZCE ranking workbook has extra populated columns")
            if values[0].startswith(("品种：", "品种:", "合约：", "合约:", "注：", "注:")):
                if any(values[1:10]):
                    raise ValueError("CZCE ranking workbook has a malformed section title")
                html_rows.append(f'<tr><td colspan="10">{escape(values[0])}</td></tr>')
            else:
                html_rows.append("<tr>" + "".join(f"<td>{escape(value)}</td>" for value in values[:10]) + "</tr>")
        page = "<html><head><title>期货持仓排名表</title></head><body><table>"
        page += "".join(html_rows) + "</table></body></html>"
        return parse_czce_rank_html(page.encode("utf-8"), trading_day=trading_day)


__all__ = ("parse_czce_rank_workbook_checked",)
