"""Historical official CFFEX and SHFE member-rank source contracts."""

from __future__ import annotations

import json
from xml.etree import ElementTree

import pytest

from market_monitor.futures_official_member_sources import (
    cffex_rank_products,
    parse_cffex_rank_csv,
    parse_cffex_rank_xml,
    parse_shfe_rank_json,
)


def _legacy_cffex_treasury_csv(day: str = "20230103") -> bytes:
    lines = [
        "交易日,产品,会员类别,总成交量,比上交易日增减,总持买单量,比上交易日增减,总持卖单量,比上一交易日增减",
        f"{day},TS,非期货公司结算会员,0,0,0,0,0,0",
        "交易日,合约,排名,成交量排名,,,持买单量排名,,,持卖单量排名,,",
        ",,,会员简称,成交量,比上一交易日增减,会员简称,持买单量,比上一交易日增减,会员简称,持卖单量,比上一交易日增减",
        f"{day},TS2303,1,上海东证,26786,-6095,国投安信,5700,10,银河期货,9983,-11",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("gb18030")


def test_parse_legacy_cffex_treasury_csv_with_mixed_width_preamble() -> None:
    frames = parse_cffex_rank_csv(_legacy_cffex_treasury_csv(), trading_day="20230103")

    assert list(frames) == ["TS2303"]
    assert frames["TS2303"].to_dict("records") == [{
        "rank": 1,
        "long_party_name": "国投安信",
        "long_open_interest": "5700",
        "long_open_interest_chg": "10",
        "short_party_name": "银河期货",
        "short_open_interest": "9983",
        "short_open_interest_chg": "-11",
        "vol_party_name": "上海东证",
        "vol": "26786",
        "vol_chg": "-6095",
    }]


def test_parse_cffex_csv_rejects_wrong_report_date() -> None:
    with pytest.raises(ValueError, match="report date"):
        parse_cffex_rank_csv(_legacy_cffex_treasury_csv(), trading_day="20230104")


def test_parse_shfe_json_strips_fixed_width_contract_padding() -> None:
    payload = {
        "o_code": "0000",
        "report_date": "20230103",
        "o_cursor": [{
            "INSTRUMENTID": "cu2301                        ",
            "RANK": 1,
            "PARTICIPANTABBR1": "席位A",
            "CJ1": 10,
            "CJ1_CHG": -1,
            "PARTICIPANTABBR2": "席位B",
            "CJ2": 20,
            "CJ2_CHG": 2,
            "PARTICIPANTABBR3": "席位C",
            "CJ3": 30,
            "CJ3_CHG": 3,
        }],
    }
    frames = parse_shfe_rank_json(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    assert list(frames) == ["CU2301"]
    assert frames["CU2301"].iloc[0]["long_open_interest"] == 20


def test_cffex_product_roster_omits_tl_before_official_listing_day() -> None:
    assert "TL" not in cffex_rank_products("20230103")
    assert "TL" not in cffex_rank_products("20230420")
    assert cffex_rank_products("20230421")[-1] == "TL"


def _cffex_xml(*, day: str = "20260924", rank: int = 1) -> bytes:
    root = ElementTree.Element("positionRank")
    for data_type, member, quantity, change in (
        ("0", "成交会员", "100", "10"),
        ("1", "多头会员", "200", "20"),
        ("2", "空头会员", "300", "30"),
    ):
        item = ElementTree.SubElement(root, "data")
        for tag, value in (
            ("instrumentid", "IM2610"), ("tradingday", day), ("datatypeid", data_type),
            ("rank", str(rank)), ("shortname", member), ("volume", quantity),
            ("varvolume", change), ("productid", "IM"),
        ):
            ElementTree.SubElement(item, tag).text = value
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def test_parse_cffex_xml_merges_three_datatypes_by_rank() -> None:
    frames = parse_cffex_rank_xml(_cffex_xml(), trading_day="20260924", product="IM")

    assert list(frames) == ["IM2610"]
    assert frames["IM2610"].to_dict("records") == [{
        "rank": 1, "vol_party_name": "成交会员", "vol": "100", "vol_chg": "10",
        "long_party_name": "多头会员", "long_open_interest": "200",
        "long_open_interest_chg": "20", "short_party_name": "空头会员",
        "short_open_interest": "300", "short_open_interest_chg": "30",
    }]


def test_parse_cffex_xml_rejects_wrong_day_and_product() -> None:
    with pytest.raises(ValueError, match="invalid contract, date"):
        parse_cffex_rank_xml(_cffex_xml(), trading_day="20260929", product="IM")
    with pytest.raises(ValueError, match="invalid contract, date"):
        parse_cffex_rank_xml(_cffex_xml(), trading_day="20260924", product="IF")
