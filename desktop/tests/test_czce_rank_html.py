"""Contract examples for CZCE's dated HTML member ranking report."""

from __future__ import annotations

import pytest

from market_monitor.czce_rank_html import parse_czce_rank_html
from market_monitor.futures_member_positions import normalise_exchange_member_position_ranks
from market_monitor.futures_member_trade_ranks import normalise_exchange_member_trade_ranks


_HEADER = (
    "<tr><td>名次</td><td>会员简称</td><td>交易量（手）</td><td>增减量</td>"
    "<td>会员简称</td><td>持买仓量</td><td>增减量</td>"
    "<td>会员简称</td><td>持卖仓量</td><td>增减量</td></tr>"
)


def _row(*cells: str) -> str:
    return "<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>"


def _section(kind: str, name: str, *rows: str, day: str = "2026-09-29") -> str:
    heading = f'<tr><td colspan="10">{kind}：{name}&nbsp;&nbsp;日期：{day}</td></tr>'
    total = _row("合计", "", "1,000", "0", "", "200", "0", "", "300", "0")
    return heading + _HEADER + "".join(rows) + total


def _report() -> str:
    variety = _section(
        "品种", "苹果AP",
        _row("1", "汇总会员", "1,000", "0", "汇总多头", "200", "0", "汇总空头", "300", "0"),
    )
    apple = _section(
        "合约", "AP610",
        _row("1", "甲期货", "1,234", "-5", "乙期货", "100", "+2", "丙期货", "70", "-3"),
        _row("2", "丁期货", "10", "0", "-", "-", "-", "-", "-", "-"),
    )
    cotton = _section(
        "合约", "CF701",
        _row("1", "-", "-", "-", "戊期货", "20", "-1", "己期货", "30", "2"),
    )
    return (
        "<html><head><meta charset='utf-8'><title>期货持仓排名表</title></head>"
        f"<body><table>{variety}{apple}{cotton}</table></body></html>"
    )


def test_czce_html_contract_frames_feed_member_normalizers_without_variety_duplication() -> None:
    capture = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + _report().encode("utf-8")
    frames = parse_czce_rank_html(capture, trading_day="20260929")

    assert list(frames) == ["AP610", "CF701"]
    assert len(frames["AP610"]) == 2
    first = frames["AP610"].iloc[0].to_dict()
    assert first == {
        "rank": 1,
        "vol_party_name": "甲期货", "vol": 1234, "vol_chg": -5,
        "long_party_name": "乙期货", "long_open_interest": 100,
        "long_open_interest_chg": 2,
        "short_party_name": "丙期货", "short_open_interest": 70,
        "short_open_interest_chg": -3,
    }
    assert frames["AP610"].iloc[1]["long_party_name"] is None
    assert frames["AP610"].iloc[1]["long_open_interest"] is None
    assert frames["CF701"].iloc[0]["vol"] is None

    positions = normalise_exchange_member_position_ranks(
        frames, exchange="CZCE", trading_day="2026-09-29",
        source="fixture", collected_at="2026-09-29T16:00:00+08:00",
    )
    trades = normalise_exchange_member_trade_ranks(
        frames, exchange="CZCE", trading_day="2026-09-29",
        source="fixture", collected_at="2026-09-29T16:00:00+08:00",
    )
    assert [(row.contract_code, row.side, row.rank) for row in positions] == [
        ("AP610", "LONG", 1), ("AP610", "SHORT", 1),
        ("CF701", "LONG", 1), ("CF701", "SHORT", 1),
    ]
    assert [(row.contract_code, row.rank, row.volume) for row in trades] == [
        ("AP610", 1, 1234), ("AP610", 2, 10),
    ]


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ("合约：CF701&nbsp;&nbsp;日期：2026-09-29", "合约：CF701&nbsp;&nbsp;日期：2026-09-28", "date"),
        ("合约：CF701", "合约：AP610", "duplicate contract"),
        ("合约：CF701", "合约：CF70X", "invalid contract"),
        ("<td>持卖仓量</td>", "<td>持卖量</td>", "column header"),
        ("<td>1,234</td>", "<td>1,2x4</td>", "invalid quantity"),
        ("<td>2</td><td>丁期货</td>", "<td>1</td><td>丁期货</td>", "duplicate rank"),
        ("<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>",
         "<td>-</td><td>2</td><td>-</td><td>-</td><td>-</td><td>-</td>", "incomplete member"),
    ],
)
def test_czce_html_rejects_mismatched_or_malformed_sections(old: str, new: str, error: str) -> None:
    report = _report()
    assert old in report
    with pytest.raises(ValueError, match=error):
        parse_czce_rank_html(report.replace(old, new, 1).encode("utf-8"), trading_day="20260929")


def test_czce_html_rejects_challenge_and_incomplete_section() -> None:
    challenge = b"<html><head><title>Verification</title></head><body>Access denied</body></html>"
    with pytest.raises(ValueError, match="not a holding report"):
        parse_czce_rank_html(challenge, trading_day="20260929")
    report = _report().replace(
        _row("合计", "", "1,000", "0", "", "200", "0", "", "300", "0"), "", 1,
    )
    with pytest.raises(ValueError, match="section is incomplete"):
        parse_czce_rank_html(report.encode("utf-8"), trading_day="20260929")
