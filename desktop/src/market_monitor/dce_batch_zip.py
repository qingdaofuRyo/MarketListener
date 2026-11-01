"""Validate and parse the DCE daily member-ranking batch ZIP format.

This parser accepts the UTF-8 TXT layout observed in the exchange's public
``batchDownload`` response. It does not make requests or write authoritative
facts; callers retain the original ZIP and provenance separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
import re
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

import pandas as pd


_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_ENTRIES = 1024
_MAX_ENTRY_BYTES = 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
_CONTRACT = r"[a-z]{1,3}\d{4}"
_INTEGER = re.compile(r"-?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)")
_HEADERS = (
    ("名次", "会员简称", "成交量", "增减"),
    ("名次", "会员简称", "持买单量", "增减"),
    ("名次", "会员简称", "持卖单量", "增减"),
)
_DIRECTIONS = (
    ("vol_party_name", "vol", "vol_chg"),
    ("long_party_name", "long_open_interest", "long_open_interest_chg"),
    ("short_party_name", "short_open_interest", "short_open_interest_chg"),
)
_COLUMNS = ("rank", *(column for direction in _DIRECTIONS for column in direction))


@dataclass
class DceBatchZipResult:
    """Dated ordinary-contract frames and audit counts from a validated ZIP."""

    trading_day: str
    frames: dict[str, pd.DataFrame]
    empty_contracts: tuple[str, ...]
    entry_count: int
    row_counts: tuple[int, int, int]
    archive_sha256: str
    excluded_contracts: tuple[str, ...] = ()

    @property
    def summary(self) -> dict[str, object]:
        """JSON-safe Bronze evidence; no raw report body or browser token."""
        summary = {
            "tradingDay": self.trading_day,
            "entryCount": self.entry_count,
            "rankedContractCount": len(self.frames),
            "emptyContractCount": len(self.empty_contracts),
            "rankRows": dict(zip(("volume", "long", "short"), self.row_counts)),
            "archiveSha256": self.archive_sha256,
        }
        if self.excluded_contracts:
            summary["excludedContractCount"] = len(self.excluded_contracts)
            summary["excludedContracts"] = list(self.excluded_contracts)
        return summary


def parse_dce_batch_zip(raw: bytes, *, trading_day: str, include_monthly_average: bool = False) -> DceBatchZipResult:
    """Require a complete dated ZIP and return frames for existing normalizers.

    Empty ordinary reports with three zero totals are represented only in
    ``empty_contracts``. Validated month-average F reports are listed in
    ``excluded_contracts``; neither becomes an ordinary-contract member fact.
    """
    if re.fullmatch(r"20\d{6}", trading_day) is None:
        raise ValueError("DCE batch trading_day must be YYYYMMDD")
    try:
        expected_day = date.fromisoformat(
            f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:]}"
        )
    except ValueError as error:
        raise ValueError("DCE batch trading_day is not a calendar date") from error
    if not raw or len(raw) > _MAX_ARCHIVE_BYTES:
        raise ValueError("DCE batch ZIP is empty or exceeds the archive size limit")

    filename_pattern = re.compile(
        rf"{trading_day}_({_CONTRACT}F?)_成交量_买持仓_卖持仓排名\.txt"
    )
    frames: dict[str, pd.DataFrame] = {}
    empty_contracts: list[str] = []
    excluded_contracts: list[str] = []
    counts = [0, 0, 0]
    seen_contracts: set[str] = set()
    try:
        with ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_ENTRIES:
                raise ValueError("DCE batch ZIP has no entries or too many entries")
            if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED_BYTES:
                raise ValueError("DCE batch ZIP exceeds the uncompressed size limit")
            for info in infos:
                match = filename_pattern.fullmatch(info.filename)
                if match is None or info.is_dir():
                    raise ValueError(f"DCE batch ZIP has an unexpected entry: {info.filename}")
                if info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                    raise ValueError(f"DCE batch ZIP has an unsupported entry: {info.filename}")
                if info.file_size > _MAX_ENTRY_BYTES:
                    raise ValueError(f"DCE batch ZIP entry exceeds the size limit: {info.filename}")
                source_contract = match.group(1)
                contract = source_contract.upper()
                if contract in seen_contracts:
                    raise ValueError(f"DCE batch ZIP has a duplicate contract: {contract}")
                seen_contracts.add(contract)
                with archive.open(info) as stream:
                    body = stream.read(_MAX_ENTRY_BYTES + 1)
                    if len(body) > _MAX_ENTRY_BYTES or stream.read(1):
                        raise ValueError(f"DCE batch ZIP entry exceeds the size limit: {info.filename}")
                if len(body) != info.file_size:
                    raise ValueError(f"DCE batch ZIP entry has a size mismatch: {info.filename}")
                frame, row_counts = _parse_entry(
                    body, contract, source_contract, expected_day.isoformat()
                )
                if source_contract.endswith("F") and (
                    not include_monthly_average or not re.fullmatch(r"(?:l|v|pp)\d{4}F", source_contract)
                ):
                    excluded_contracts.append(contract)
                    continue
                if source_contract.endswith("F"):
                    contract = re.sub(r"^([A-Z]+)(\d{4})F$", r"\1-F\2", contract)
                for index, count in enumerate(row_counts):
                    counts[index] += count
                if frame is None:
                    empty_contracts.append(contract)
                else:
                    frames[contract] = frame
    except BadZipFile as error:
        raise ValueError("DCE batch response is not a valid ZIP with intact CRC") from error
    if not frames:
        raise ValueError("DCE batch ZIP has no published member ranks")
    return DceBatchZipResult(
        trading_day=expected_day.isoformat(),
        frames=dict(sorted(frames.items())),
        empty_contracts=tuple(sorted(empty_contracts)),
        entry_count=len(seen_contracts),
        row_counts=tuple(counts),
        archive_sha256=sha256(raw).hexdigest(),
        excluded_contracts=tuple(sorted(excluded_contracts)),
    )


def _parse_entry(
    body: bytes, contract: str, source_contract: str, expected_day: str,
) -> tuple[pd.DataFrame | None, tuple[int, int, int]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"DCE batch report is not UTF-8: {contract}") from error
    lines = [tuple(cell.strip() for cell in line.split("\t") if cell.strip())
             for line in text.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 9 or lines[0] != ("大连商品交易所 成交量_买持仓_卖持仓排名",):
        raise ValueError(f"DCE batch report has an unexpected title: {contract}")
    if lines[1] != (f"合约代码：{source_contract}", f"Date：{expected_day}"):
        raise ValueError(f"DCE batch report date or contract differs from filename: {contract}")
    if lines[2] != ("期货公司会员",):
        raise ValueError(f"DCE batch report has an unexpected member heading: {contract}")

    records: dict[int, dict[str, object]] = {}
    section_counts: list[int] = []
    cursor = 3
    for section_index, header in enumerate(_HEADERS):
        if cursor >= len(lines) or lines[cursor] != header:
            raise ValueError(f"DCE batch report has a missing or unordered section: {contract}")
        cursor += 1
        section_rows: list[tuple[int, str, int, int]] = []
        while cursor < len(lines) and lines[cursor][0] != "合计":
            cells = lines[cursor]
            if len(cells) != 4:
                raise ValueError(f"DCE batch report has an invalid rank row: {contract}")
            rank = _parse_integer(cells[0], contract, "rank")
            if rank != len(section_rows) + 1 or cells[1] in ("", "-", "--"):
                raise ValueError(f"DCE batch report has a missing member or noncontiguous rank: {contract}")
            position = _parse_integer(cells[2], contract, "quantity")
            change = _parse_integer(cells[3], contract, "change")
            if position < 0:
                raise ValueError(f"DCE batch report has a negative quantity: {contract}")
            section_rows.append((rank, cells[1], position, change))
            if len(section_rows) > 20:
                raise ValueError(f"DCE batch report has more than 20 rank rows: {contract}")
            cursor += 1
        if cursor >= len(lines):
            raise ValueError(f"DCE batch report has an incomplete ranking section: {contract}")
        total = lines[cursor]
        if len(total) != 3 or total[0] != "合计":
            raise ValueError(f"DCE batch report has a missing total: {contract}")
        expected_total = (sum(row[2] for row in section_rows), sum(row[3] for row in section_rows))
        actual_total = (
            _parse_integer(total[1], contract, "total quantity"),
            _parse_integer(total[2], contract, "total change"),
        )
        if actual_total != expected_total:
            raise ValueError(f"DCE batch report has an incorrect total: {contract}")
        cursor += 1
        section_counts.append(len(section_rows))
        name_column, quantity_column, change_column = _DIRECTIONS[section_index]
        for rank, member, quantity, change in section_rows:
            record = records.setdefault(rank, {column: None for column in _COLUMNS})
            record["rank"] = rank
            record[name_column] = member
            record[quantity_column] = quantity
            record[change_column] = change
    if cursor != len(lines):
        raise ValueError(f"DCE batch report has unexpected trailing rows: {contract}")
    if not records:
        return None, tuple(section_counts)
    frame = pd.DataFrame([records[rank] for rank in sorted(records)], columns=list(_COLUMNS))
    return frame, tuple(section_counts)


def _parse_integer(value: str, contract: str, field: str) -> int:
    if _INTEGER.fullmatch(value) is None:
        raise ValueError(f"DCE batch report has an invalid {field}: {contract}")
    return int(value.replace(",", ""))


__all__ = ("DceBatchZipResult", "parse_dce_batch_zip")
