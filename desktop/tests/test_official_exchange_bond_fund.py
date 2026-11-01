from __future__ import annotations

import hashlib
import io
import json
from decimal import Decimal

import openpyxl
import pytest
import requests

from market_monitor import official_exchange_bond_fund as official


DAY = "2026-10-21"


def _sse_fund(day: str = DAY) -> bytes:
    rows = [("05", "971.40", "3525.57"), ("13", "968.70", "3518.75"),
            ("14", "1.95", "3.96"), ("16", "0.75", "2.86")]
    return json.dumps({"actionErrors": [], "result": [
        {"PRODUCT_CODE": code, "TRADE_DATE": day.replace("-", ""),
         "TRADE_VOL": volume, "TRADE_AMT": amount}
        for code, volume, amount in rows
    ]}).encode()


def _szse_fund() -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["基金品种", "成交量（万份）", "成交金额（万元）"])
    for row in (("      ETF", "6,386,685.73", "12,773,779.15"),
                ("      LOF", "52,828.64", "102,717.60"),
                ("      不动产基金", "4,002.70", "15,654.96"),
                ("合计", "6,443,517.07", "12,892,151.71")):
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    book.close()
    return out.getvalue()


def _sse_bonds(day: str = DAY) -> bytes:
    rows = [("10", "1", "10", "12"), ("20", "1", "20", "21"),
            ("30", "1", "30", "31"), ("40", "1", "40", "41"),
            ("50", "1", "50", "51"), ("60", "1", "60", "61"),
            ("80", "1", "80", "81"), ("90", "1", "90", "91"),
            ("9999", "8", "380", "389")]
    return json.dumps({"actionErrors": [], "result": [
        {"TYPE_CODE": code, "TRADE_DATE": day, "TOTAL_NUM": count,
         "FACE_VAL": face, "MARKET_VAL": value}
        for code, count, face, value in rows
    ]}).encode()


def _bse_month(month: str = "2026-09") -> bytes:
    row = {"rq": month, "zfzq_by": 6843, "zfzq_bnlj": 45428.1,
           "dfzq_by": 0, "dfzq_bnlj": 1080.69,
           "xyz_by": 18, "xyz_bnlj": 110.6,
           "gkfxgszq_by": 18, "gkfxgszq_bnlj": 92.6,
           "fgkfxgszq_by": 0, "fgkfxgszq_bnlj": 18}
    return f"{official._CALLBACK}({json.dumps([row])})".encode()


def test_sse_fund_parent_volume_is_not_added_to_children() -> None:
    values = official.parse_sse_fund_daily(_sse_fund(), DAY)
    assert values[("SSE", "FUND")]["volumeYi"] == Decimal("971.40")
    assert values[("SSE", "FUND_REIT")]["volumeYi"] == Decimal("0.75")
    assert values[("SSE", "FUND")]["turnoverYi"] == Decimal("3525.57")


def test_sse_fund_unmapped_zero_category_does_not_hide_new_nonzero_category() -> None:
    payload = json.loads(_sse_fund())
    payload["result"].append({"PRODUCT_CODE": "18", "TRADE_DATE": DAY.replace("-", ""),
                              "AVG_PE_RATE": "-", **{key: "0.0" for key in
                                                     official._SSE_FUND_NUMERIC_FIELDS}})
    values = official.parse_sse_fund_daily(json.dumps(payload).encode(), DAY)
    assert len(values) == 4
    assert values[("SSE", "FUND")]["volumeYi"] == Decimal("971.40")

    payload["result"][-1]["TRADE_NUM"] = "0.0002"
    with pytest.raises(ValueError, match="category changed"):
        official.parse_sse_fund_daily(json.dumps(payload).encode(), DAY)


def test_sse_fund_repo_is_reported_separately_from_cash_funds() -> None:
    payload = json.loads(_sse_fund())
    payload["result"].append({"PRODUCT_CODE": "12", "TRADE_DATE": DAY.replace("-", ""),
                              "TRADE_VOL": "0.01", "TRADE_AMT": "0.07", "TRADE_NUM": "0.0002"})
    values = official.parse_sse_fund_daily(json.dumps(payload).encode(), DAY)
    assert values[("SSE", "FUND_REPO")] == {"volumeYi": Decimal("0.01"), "turnoverYi": Decimal("0.07")}
    assert values[("SSE", "FUND")]["turnoverYi"] == Decimal("3525.57")


def test_sse_fund_before_cash_only_cutover_requires_separate_historical_scope() -> None:
    with pytest.raises(ValueError, match="different repo scope"):
        official.parse_sse_fund_daily(_sse_fund("2019-07-19"), "2019-07-19")
    assert official.parse_sse_fund_daily(_sse_fund("2019-07-22"), "2019-07-22")[("SSE", "FUND")]


def test_sse_money_fund_changes_parent_in_2022_without_double_counting() -> None:
    for day in ("2022-09-02", "2022-09-05"):
        payload = json.loads(_sse_fund(day))
        payload["result"].append({"PRODUCT_CODE": "15", "TRADE_DATE": day.replace("-", ""),
                                  "TRADE_VOL": "1.50", "TRADE_AMT": "20.00"})
        if day < "2022-09-05":
            payload["result"][0].update(TRADE_VOL="972.90", TRADE_AMT="3545.57")
        values = official.parse_sse_fund_daily(json.dumps(payload).encode(), day)
        assert values[("SSE", "FUND_MONEY")]["turnoverYi"] == Decimal("20.00")
        assert values[("SSE", "FUND")]["turnoverYi"] == Decimal(
            "3545.57" if day < "2022-09-05" else "3525.57"
        )
    payload["result"][-1]["TRADE_VOL"] = "9999"
    with pytest.raises(ValueError, match="exceeds ETF parent"):
        official.parse_sse_fund_daily(json.dumps(payload).encode(), "2022-09-05")


def test_szse_fund_wan_units_and_zero_are_preserved() -> None:
    values = official.parse_szse_fund_daily(_szse_fund())
    assert values[("SZSE", "FUND")]["volumeYi"] == Decimal("644.351707")
    assert values[("SZSE", "FUND")]["turnoverYi"] == Decimal("1289.215171")
    assert len(values) == 4


def test_sse_bond_custody_face_and_market_value_are_distinct() -> None:
    values = official.parse_sse_bond_custody_daily(_sse_bonds(), DAY)
    assert values[("SSE", "BOND_CUSTODY_ALL")] == {
        "custodyValueYi": Decimal("389"), "custodyFaceYi": Decimal("380"),
        "securityCount": Decimal("8"),
    }
    assert len(values) == 9


def test_bse_monthly_issuance_keeps_zero_and_period() -> None:
    values = official.parse_bse_bond_issuance_monthly(_bse_month(), "2026-09")
    assert values[("BSE", "BOND_TREASURY")]["issuanceYi"] == Decimal("6843")
    assert values[("BSE", "BOND_LOCAL_GOV")]["issuanceYi"] == Decimal("0")
    assert values[("BSE", "BOND_CORP")]["issuanceYtdYi"] == Decimal("110.6")
    with pytest.raises(ValueError, match="period mismatch"):
        official.parse_bse_bond_issuance_monthly(_bse_month(), "2026-08")
    with pytest.raises(ValueError, match="unpublished"):
        official.parse_bse_bond_issuance_monthly(f"{official._CALLBACK}([])".encode(), "2026-10")


def test_invalid_date_or_parent_relation_blocks_publication() -> None:
    with pytest.raises(ValueError, match="date"):
        official.parse_sse_fund_daily(_sse_fund("2026-10-20"), DAY)
    malformed = json.loads(_sse_fund())
    malformed["result"][0]["TRADE_VOL"] = "999"
    with pytest.raises(ValueError, match="disagree"):
        official.parse_sse_fund_daily(json.dumps(malformed).encode(), DAY)
    with pytest.raises(ValueError, match="JSONP"):
        official.parse_bse_bond_issuance_monthly(b"evil([])", "2026-09")
    malformed_month = _bse_month().replace(b'"xyz_by": 18', b'"xyz_by": 19')
    with pytest.raises(ValueError, match="disagree"):
        official.parse_bse_bond_issuance_monthly(malformed_month, "2026-09")


class _Response:
    def __init__(self, url: str, body: bytes) -> None:
        self.url = url
        self.content = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, url: str, *, params: dict, data: dict | None,
                headers: dict, timeout: int) -> _Response:
        self.calls.append((method, url, data))
        assert timeout == 20
        assert headers["Referer"].startswith("https://")
        prepared = requests.Request(method, url, params=params).prepare().url
        assert prepared is not None
        return _Response(prepared, self.body)


def test_fetch_keeps_sha_and_request_period_without_credentials() -> None:
    session = _Session(_sse_fund())
    batch = official.fetch_sse_fund_daily(DAY, session=session)
    assert batch.frequency == "1d" and batch.period == DAY
    assert batch.artifact.source == "sse_fund_daily"
    assert batch.artifact.sha256 == hashlib.sha256(_sse_fund()).hexdigest()
    assert "SEARCH_DATE=2026-10-21" in batch.artifact.requested_url
    assert session.calls[0][0] == "GET"

    month_session = _Session(_bse_month())
    monthly = official.fetch_bse_bond_issuance_monthly("2026-09", session=month_session)
    assert monthly.frequency == "1mo" and monthly.artifact.request_form == {"month": "2026-09"}
    assert month_session.calls[0][0] == "POST"
