from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from market_monitor.futures_official_daily_sources import parse_official_daily, PARSER_VERSION, TURNOVER_POLICIES
from market_monitor.storage import MarketStore


def _parse(raw, venue, day="2023-01-03"):
    return parse_official_daily(raw, exchange=venue, trading_day=day, source_url="https://example.invalid/report", retrieved_at="2026-10-03T00:00:00+00:00")


@pytest.mark.parametrize("venue", ["SHFE", "INE"])
def test_shfe_ine_signed_fields_empty_and_zero(venue):
    product = "cu" if venue == "SHFE" else "sc"
    raw = json.dumps({"report_date": "20230103", "o_curinstrument": [{"PRODUCTID": product+"_f", "DELIVERYMONTH": "2302", "PRESETTLEMENTPRICE": 100, "OPENPRICE": "", "HIGHESTPRICE": 102, "LOWESTPRICE": 95, "CLOSEPRICE": 97, "SETTLEMENTPRICE": 98, "ZD1_CHG": -3, "ZD2_CHG": -2, "VOLUME": 0, "TURNOVER": 0, "OPENINTEREST": 10, "OPENINTERESTCHG": -4}]}).encode()
    row = _parse(raw, venue)[0]
    assert row["previous_settlement"] == 100
    assert (row["change_close"], row["change_settlement"], row["open_interest_change"]) == (-3, -2, -4)
    assert row["open"] is None and row["volume"] == row["turnover_cny"] == 0


@pytest.mark.parametrize("venue", ["DCE", "GFEX"])
def test_dce_gfex_signed_mapping_and_monthly_identity(venue):
    item = {"contractId": "l2610F" if venue == "DCE" else "si2302", "varietyOrder": "si", "delivMonth": "2302", "lastClear": 100, "close": 97, "clearPrice": 98, "diff": -3, "diff1": -2, "diffI": -4, "volumn": 0, "turnover": 0, "openInterest": 10}
    raw = json.dumps({"code": 200, "success": True, "data": [item]} if venue == "DCE" else {"code": 0, "param": "20230103", "data": [item]}).encode()
    row = _parse(raw, venue, "2026-09-30" if venue == "DCE" else "2023-01-03")[0]
    assert row["change_close"] == -3 and row["open_interest_change"] == -4
    assert row["contract_code"] == ("L-F2610" if venue == "DCE" else "SI2302")


def test_czce_and_cffex_full_table_fields_and_atomic_idempotent_storage(tmp_path):
    header = "合约代码,昨结算,今开盘,最高价,最低价,今收盘,今结算,涨跌1,涨跌2,成交量,持仓量,持仓变化,成交金额\n"
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("20230103_1.csv", header + "IF2302,100,,102,95,97,98,-3,-2,0,10,-4,0\n")
    row = _parse(stream.getvalue(), "CFFEX")[0]
    assert row["open"] is None and row["change_close"] == -3 and row["previous_settlement"] == 100
    raw = ("郑州商品交易所期货每日行情表(2023-01-03)\n" + header.replace(",", "|").replace("成交金额", "成交额(万元)") + "TA305|100||102|95|97|98|-3|-2|0|10|-4|0\n").encode()
    czce = _parse(raw, "CZCE")[0]
    assert czce["contract_code"] == "TA2305" and czce["source_contract_code"] == "TA305"
    assert czce["open_interest_change"] == -4
    unverified = _parse(raw.replace(b"(\xe4\xb8\x87\xe5\x85\x83)", b"(\xe5\x85\x83)"), "CZCE")[0]
    assert unverified["turnover_raw"] == 0
    assert unverified["turnover_cny"] is None
    assert unverified["turnover_quality_status"] == "UNVERIFIED"
    store = MarketStore(tmp_path)
    coverage = {"trade_date": row["trade_date"], "exchange": "CFFEX", "adapter": row["adapter"], "status": "PASS", "record_count": 1, "usable_count": 1, "source_url": row["source_url"], "payload_sha256": row["payload_sha256"], "retrieved_at": row["retrieved_at"], "error": None}
    try:
        for _ in range(2):
            store.record_futures_official_daily([row], coverage)
        saved = store.list_futures_official_daily()
        assert len(saved) == 1 and saved[0]["change_settlement"] == -2
        assert store.list_futures_official_daily_coverage()[0]["parser_version"] == PARSER_VERSION
    finally:
        store.close()


def test_turnover_policies_have_source_proof_and_effective_interval():
    assert set(TURNOVER_POLICIES) == {"SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"}
    for policy in TURNOVER_POLICIES.values():
        assert policy["effective_from"] == "2023-01-01"
        assert policy["unit"] == "CNY_10K" and policy["factor"] == 10_000
        assert policy["evidence"] and policy["evidence_sha256"]


def test_shfe_wrong_or_missing_report_day_is_rejected():
    item = {"PRODUCTID": "cu_f", "DELIVERYMONTH": "2305", "PRESETTLEMENTPRICE": 100,
            "CLOSEPRICE": 101, "SETTLEMENTPRICE": 101, "VOLUME": 1, "OPENINTEREST": 1}
    wrong = json.dumps({"report_date": "20230104", "o_curinstrument": [item]}).encode()
    with pytest.raises(ValueError, match="date differs"):
        _parse(wrong, "SHFE")
    fallback = json.dumps({"o_year": "2023", "o_month": "01", "o_day": "04", "o_curinstrument": [item]}).encode()
    with pytest.raises(ValueError, match="date differs"):
        _parse(fallback, "SHFE")
    with pytest.raises(ValueError, match="explicit valid date"):
        _parse(json.dumps({"o_curinstrument": [item]}).encode(), "SHFE")
