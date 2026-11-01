"""Unit tests for the A/H F10 collection module (no network)."""

from __future__ import annotations

from pathlib import Path

from market_monitor.f10 import (
    _atlas_record,
    _tencent_symbol,
    parse_business_analysis,
    parse_tencent_quotes,
)


def _tencent_line() -> str:
    fields = [""] * 50
    fields[0] = "1"
    fields[1] = "贵州茅台"
    fields[2] = "600519"
    fields[3] = "1309.22"
    fields[4] = "1313.99"
    fields[30] = "2026/08/09 10:00:00"
    fields[32] = "-0.36"
    fields[33] = "1320.00"
    fields[34] = "1300.00"
    fields[37] = "42.5"
    fields[38] = "0.23"
    fields[39] = "21.5"
    fields[44] = "16366.32"
    fields[45] = "16366.32"
    fields[46] = "7.8"
    fields[49] = "0.9"
    return 'v_sh600519="' + "~".join(fields) + '";'


def test_tencent_symbol_mapping() -> None:
    assert _tencent_symbol("600519") == "sh600519"
    assert _tencent_symbol("000001") == "sz000001"
    assert _tencent_symbol("688111") == "sh688111"
    assert _tencent_symbol("830001") == "bj830001"
    assert _tencent_symbol("00700") == "hk00700"


def test_parse_tencent_quotes() -> None:
    quotes = parse_tencent_quotes(_tencent_line())
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.code == "600519"
    assert quote.name == "贵州茅台"
    assert quote.total_market_cap_yi == 16366.32
    assert quote.float_market_cap_yi == 16366.32
    assert quote.change_pct == -0.36
    assert quote.pe == 21.5
    assert quote.amount_yi == 42.5


def test_atlas_record_contract(tmp_path: Path) -> None:
    record = {
        "code": "600519",
        "market": "CN",
        "name": "贵州茅台",
        "org_name": "贵州茅台酒股份有限公司",
        "industry_em": "食品饮料-饮料-白酒",
        "industry_csrc": "制造业-酒、饮料和精制茶制造业",
        "org_profile": "公司简介",
        "business_scope": "经营范围",
        "detail_fetched_at": "2026-08-09T00:00:00+00:00",
    }
    quote = {
        "total_market_cap_yi": 16366.32,
        "float_market_cap_yi": 16366.32,
        "quote_time": "2026/08/09 10:00:00",
        "price": 1309.22,
    }
    out = _atlas_record(record, quote)
    assert out["code"] == "600519"
    assert out["full_name"] == "贵州茅台酒股份有限公司"
    assert out["total_market_cap"] == {
        "value": 1_636_632_000_000.0,
        "currency": "CNY",
        "asOf": "2026/08/09 10:00:00",
        "source": "tencent_quote",
    }
    assert out["float_market_cap"]["value"] == 1_636_632_000_000.0
    assert out["source"] == "eastmoney_f10"
    assert out["status"] == "ok"
    assert out["profile"] == "公司简介"
    assert out["business_scope"] == "经营范围"
    assert "main_business" not in out


def test_atlas_record_never_uses_detail_fetch_time_for_caps() -> None:
    record = {
        "code": "600519",
        "market": "CN",
        "name": "贵州茅台",
        "detail_fetched_at": "2026-08-09T00:00:00+00:00",
    }
    quote = {"total_market_cap_yi": 16366.32, "float_market_cap_yi": 16366.32}
    out = _atlas_record(record, quote)
    assert "total_market_cap" not in out
    assert "float_market_cap" not in out
    assert out["market_cap_missing_reasons"]["float_market_cap"] == "missing_quote_time"


def test_parse_business_analysis() -> None:
    payload = {
        "zygcfx": [
            {"ITEM_NAME": "酒类", "MAINOP_TYPE": "1", "MAIN_BUSINESS_INCOME": 168774585187.65, "MBI_RATIO": 0.999624},
            {"ITEM_NAME": "茅台酒", "MAINOP_TYPE": "2", "MAIN_BUSINESS_INCOME": 146499906480.49, "MBI_RATIO": 0.867695},
            {"ITEM_NAME": "", "MAINOP_TYPE": "3", "MAIN_BUSINESS_INCOME": 1.0, "MBI_RATIO": 0.1},
        ]
    }
    rows = parse_business_analysis(payload)
    assert len(rows) == 2
    assert rows[0]["item"] == "酒类"
    assert rows[0]["ratio"] == 0.999624
    assert rows[1]["income"] == 146499906480.49


def test_business_analysis_preserves_zero_and_percentage_units() -> None:
    rows = parse_business_analysis({"zygcfx": [
        {"ITEM_NAME": "零值", "MAINOP_TYPE": "2", "MBI_RATIO": 0, "ratio": 0.8,
         "MAIN_BUSINESS_INCOME": 0, "revenue": 300, "MAIN_BUSINESS_COST": 0, "cost": 200,
         "MAIN_BUSINESS_RPOFIT": 0, "gross_profit": 100, "GROSS_RPOFIT_RATIO": 0, "gross_margin_pct": 25},
        {"ITEM_NAME": "百分数", "gross_margin_pct": 35.5, "revenue": -10, "ratio": 0.25},
        {"ITEM_NAME": "比率", "GROSS_RPOFIT_RATIO": 0.355},
        {"ITEM_NAME": "无效", "MAIN_BUSINESS_INCOME": float("nan"), "MAIN_BUSINESS_COST": "inf",
         "MAIN_BUSINESS_RPOFIT": True, "MBI_RATIO": "nan"},
        {"ITEM_NAME": "溢出", "MBI_RATIO": 1e308, "GROSS_RPOFIT_RATIO": 1e308},
    ]})
    assert [rows[0][key] for key in ("ratio", "revenue", "cost", "gross_profit", "gross_margin_pct")] == [0.0] * 5
    assert rows[0]["revenue_share_pct"] == 0.0
    assert rows[1]["gross_margin_pct"] == 35.5 and rows[1]["revenue"] == -10.0
    assert rows[1]["revenue_share_pct"] == 25.0
    assert rows[2]["gross_margin_pct"] == 35.5
    assert all(rows[3][key] is None for key in ("revenue", "cost", "gross_profit", "revenue_share_pct"))
    assert rows[4]["revenue_share_pct"] is None and rows[4]["gross_margin_pct"] is None


def test_atlas_record_includes_revenue() -> None:
    record = {"code": "600519", "market": "CN", "name": "贵州茅台"}
    revenue = [
        {
            "item": "茅台酒",
            "type": "2",
            "income": 146499906480.49,
            "ratio": 0.867695,
            "period": "2025-12-31",
        }
    ]
    out = _atlas_record(record, None, revenue)
    row = out["revenue_breakdown"][0]
    assert row["item"] == "茅台酒"
    assert row["item_name"] == "茅台酒"
    assert row["income"] == 146499906480.49
    assert row["revenue"] == 146499906480.49
    assert row["classification"] == "product"
    assert row["revenue_share_pct"] == 86.7695
    assert out["largest_revenue_segment"]["item"] == "茅台酒"
