"""Synthetic examples of DCE's UTF-8 daily batch ranking ZIP."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pandas as pd
import pytest

from market_monitor import dce_batch_zip
from market_monitor.dce_batch_zip import parse_dce_batch_zip
from market_monitor.futures_member_positions import normalise_exchange_member_position_ranks
from market_monitor.futures_member_trade_ranks import normalise_exchange_member_trade_ranks


DAY = "20260930"
ISO_DAY = "2026-09-30"
HEADERS = ("成交量", "持买单量", "持卖单量")


def _report(
    contract: str, *, rows: int | tuple[int, int, int] = 20, day: str = ISO_DAY,
) -> bytes:
    direction_rows = (rows, rows, rows) if isinstance(rows, int) else rows
    lines = [
        "\t\t大连商品交易所 成交量_买持仓_卖持仓排名\t",
        f"合约代码：{contract}\t\tDate：{day}\t",
        "\t\t期货公司会员\t",
    ]
    for index, label in enumerate(HEADERS):
        lines.append(f"名次\t\t会员简称\t{label}\t\t增减\t")
        section_count = direction_rows[index]
        for rank in range(1, section_count + 1):
            change = -rank if index == 0 else rank
            lines.append(f"{rank}\t\t合成会员{rank}\t{1000 + rank:,}\t\t{change}\t")
        total_quantity = sum(1000 + rank for rank in range(1, section_count + 1))
        total_change = sum((-rank if index == 0 else rank) for rank in range(1, section_count + 1))
        lines.append(f"合计\t\t\t\t{total_quantity:,}\t\t{total_change:,}\t")
    return ("\r\r\n".join(lines) + "\r\r\n").encode("utf-8")


def _archive(entries: list[tuple[str, bytes]], *, compression: int = ZIP_DEFLATED) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", compression=compression) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return stream.getvalue()


def _name(contract: str, day: str = DAY) -> str:
    return f"{day}_{contract}_成交量_买持仓_卖持仓排名.txt"


def test_dce_batch_zip_feeds_both_member_normalizers_and_retains_empty_contracts() -> None:
    raw = _archive([(_name("a2611"), _report("a2611"))] + [
        (_name(f"bz{year:2d}{month:02d}"), _report(f"bz{year:2d}{month:02d}", rows=0))
        for year, month in ((26, 10), (26, 11), (26, 12), (27, 1), (27, 2),
                            (27, 3), (27, 4), (27, 5), (27, 6), (27, 7), (27, 8))
    ])
    result = parse_dce_batch_zip(raw, trading_day=DAY)

    assert list(result.frames) == ["A2611"]
    assert len(result.empty_contracts) == 11
    assert result.entry_count == 12
    assert result.row_counts == (20, 20, 20)
    assert result.summary == {
        "tradingDay": ISO_DAY,
        "entryCount": 12,
        "rankedContractCount": 1,
        "emptyContractCount": 11,
        "rankRows": {"volume": 20, "long": 20, "short": 20},
        "archiveSha256": sha256(raw).hexdigest(),
    }
    first = result.frames["A2611"].iloc[0].to_dict()
    assert first == {
        "rank": 1,
        "vol_party_name": "合成会员1", "vol": 1001, "vol_chg": -1,
        "long_party_name": "合成会员1", "long_open_interest": 1001,
        "long_open_interest_chg": 1,
        "short_party_name": "合成会员1", "short_open_interest": 1001,
        "short_open_interest_chg": 1,
    }
    positions = normalise_exchange_member_position_ranks(
        result.frames, exchange="DCE", trading_day=ISO_DAY,
        source="synthetic", collected_at="2026-10-02T10:00:00+08:00",
    )
    trades = normalise_exchange_member_trade_ranks(
        result.frames, exchange="DCE", trading_day=ISO_DAY,
        source="synthetic", collected_at="2026-10-02T10:00:00+08:00",
    )
    assert len(positions) == 40
    assert len(trades) == 20
    assert positions[0].contract_code == "A2611"
    assert trades[0].volume == 1001


def test_dce_batch_zip_accepts_unequal_contiguous_partial_directions() -> None:
    raw = _archive([(_name("cs2505"), _report("cs2505", rows=(14, 18, 13)))])
    result = parse_dce_batch_zip(raw, trading_day=DAY)

    assert result.entry_count == 1
    assert result.empty_contracts == ()
    assert result.row_counts == (14, 18, 13)
    frame = result.frames["CS2505"]
    assert len(frame) == 18
    assert frame.iloc[13]["vol_party_name"] == "合成会员14"
    assert pd.isna(frame.iloc[13]["short_party_name"])
    assert frame.iloc[17]["long_party_name"] == "合成会员18"
    assert pd.isna(frame.iloc[17]["vol_party_name"])
    positions = normalise_exchange_member_position_ranks(
        result.frames, exchange="DCE", trading_day=ISO_DAY,
        source="synthetic", collected_at="2026-10-02T10:00:00+08:00",
    )
    trades = normalise_exchange_member_trade_ranks(
        result.frames, exchange="DCE", trading_day=ISO_DAY,
        source="synthetic", collected_at="2026-10-02T10:00:00+08:00",
    )
    assert len(positions) == 31
    assert len(trades) == 14


def test_dce_batch_zip_validates_and_explicitly_excludes_multiple_f_contracts() -> None:
    raw = _archive([
        (_name("a2611"), _report("a2611")),
        (_name("v2609F"), _report("v2609F", rows=0)),
        (_name("a2611F"), _report("a2611F", rows=(3, 2, 1))),
    ])
    result = parse_dce_batch_zip(raw, trading_day=DAY)

    assert list(result.frames) == ["A2611"]
    assert result.empty_contracts == ()
    assert result.excluded_contracts == ("A2611F", "V2609F")
    assert result.entry_count == 3
    assert result.row_counts == (20, 20, 20)
    assert result.summary["excludedContractCount"] == 2
    assert result.summary["excludedContracts"] == ["A2611F", "V2609F"]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (_report("v2609f", rows=0), "date or contract"),
        (_report("v2609F", rows=0, day="2026-09-29"), "date or contract"),
        (_report("v2609F", rows=3).replace(
            "2\t\t合成会员2".encode(), "3\t\t合成会员2".encode(), 1,
        ), "noncontiguous rank"),
        (_report("v2609F", rows=2).replace(
            b"2,003", b"2,004", 1,
        ), "incorrect total"),
    ],
)
def test_dce_batch_zip_rejects_malformed_excluded_f_contract(body, error) -> None:
    raw = _archive([
        (_name("a2611"), _report("a2611")),
        (_name("v2609F"), body),
    ])
    with pytest.raises(ValueError, match=error):
        parse_dce_batch_zip(raw, trading_day=DAY)


def test_dce_batch_zip_rejects_f_only_archive_without_ordinary_member_ranks() -> None:
    raw = _archive([(_name("v2609F"), _report("v2609F", rows=0))])
    with pytest.raises(ValueError, match="no published member ranks"):
        parse_dce_batch_zip(raw, trading_day=DAY)


def test_dce_batch_zip_checks_excluded_f_contract_crc() -> None:
    raw = _archive(
        [(_name("a2611"), _report("a2611")),
         (_name("v2609F"), _report("v2609F", rows=0))],
        compression=ZIP_STORED,
    )
    altered = raw.replace("合约代码：v2609F".encode(), "合约代码：w2609F".encode(), 1)
    assert altered != raw
    with pytest.raises(ValueError, match="CRC"):
        parse_dce_batch_zip(altered, trading_day=DAY)


@pytest.mark.parametrize(
    ("entries", "error"),
    [
        ([("20260929_a2611_成交量_买持仓_卖持仓排名.txt", _report("a2611"))], "unexpected entry"),
        ([(_name("m2611"), _report("a2611"))], "date or contract"),
        ([(_name("a2611"), _report("a2611", day="2026-09-29"))], "date or contract"),
        ([(_name("a2611"), _report("a2611", rows=0))], "no published member ranks"),
        ([(_name("a2611"), _report("a2611") + b"\xff")], "not UTF-8"),
        ([(_name("a2611"), _report("a2611").replace(
            "持买单量".encode(), "持卖单量".encode(), 1))], "missing or unordered section"),
        ([(_name("a2611"), _report("a2611").replace(
            b"20,210", b"20,211", 1))], "incorrect total"),
        ([(_name("a2611"), _report("a2611").replace(
            "1\t\t合成会员1".encode(), "2\t\t合成会员1".encode(), 1))], "noncontiguous rank"),
        ([(_name("a2611"), _report("a2611")),
          (_name("a2611"), _report("a2611"))], "duplicate contract"),
        ([(_name("../a2611"), _report("a2611"))], "unexpected entry"),
    ],
)
def test_dce_batch_zip_rejects_wrong_or_malformed_report(entries, error) -> None:
    with pytest.raises(ValueError, match=error):
        parse_dce_batch_zip(_archive(entries), trading_day=DAY)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (_report("cs2505", rows=(14, 18, 13)).replace(
            "14\t\t合成会员14".encode(), "16\t\t合成会员14".encode(), 1,
        ), "noncontiguous rank"),
        (_report("cs2505", rows=(14, 18, 13)).replace(
            b"14,105", b"14,106", 1,
        ), "incorrect total"),
    ],
)
def test_dce_batch_zip_rejects_partial_rank_gap_or_bad_total(body, error) -> None:
    with pytest.raises(ValueError, match=error):
        parse_dce_batch_zip(_archive([(_name("cs2505"), body)]), trading_day=DAY)


@pytest.mark.parametrize("day", ["2026-09-30", "20260230", "20261301"])
def test_dce_batch_zip_rejects_invalid_requested_day(day: str) -> None:
    with pytest.raises(ValueError, match="trading_day"):
        parse_dce_batch_zip(_archive([(_name("a2611"), _report("a2611"))]), trading_day=day)


def test_dce_batch_zip_rejects_bad_archive_and_crc() -> None:
    with pytest.raises(ValueError, match="valid ZIP"):
        parse_dce_batch_zip(b"not a ZIP", trading_day=DAY)
    raw = _archive([(_name("a2611"), _report("a2611"))], compression=ZIP_STORED)
    altered = raw.replace("合成会员1".encode(), "合成会员2".encode(), 1)
    assert altered != raw
    with pytest.raises(ValueError, match="CRC"):
        parse_dce_batch_zip(altered, trading_day=DAY)


def test_dce_batch_zip_enforces_archive_entry_and_expanded_size_limits(monkeypatch) -> None:
    raw = _archive([(_name("a2611"), _report("a2611"))])
    monkeypatch.setattr(dce_batch_zip, "_MAX_ARCHIVE_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="archive size limit"):
        parse_dce_batch_zip(raw, trading_day=DAY)
    monkeypatch.setattr(dce_batch_zip, "_MAX_ARCHIVE_BYTES", len(raw))
    monkeypatch.setattr(dce_batch_zip, "_MAX_ENTRIES", 0)
    with pytest.raises(ValueError, match="too many entries"):
        parse_dce_batch_zip(raw, trading_day=DAY)
    monkeypatch.setattr(dce_batch_zip, "_MAX_ENTRIES", 1)
    monkeypatch.setattr(dce_batch_zip, "_MAX_UNCOMPRESSED_BYTES", len(_report("a2611")) - 1)
    with pytest.raises(ValueError, match="uncompressed size limit"):
        parse_dce_batch_zip(raw, trading_day=DAY)
    monkeypatch.setattr(dce_batch_zip, "_MAX_UNCOMPRESSED_BYTES", len(_report("a2611")))
    monkeypatch.setattr(dce_batch_zip, "_MAX_ENTRY_BYTES", len(_report("a2611")) - 1)
    with pytest.raises(ValueError, match="entry exceeds the size limit"):
        parse_dce_batch_zip(raw, trading_day=DAY)
