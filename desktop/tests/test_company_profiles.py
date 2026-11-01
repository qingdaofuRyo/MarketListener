"""Regression coverage for the shared CompanySummary / CompanyDetail read model."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from market_monitor.control_center import make_handler
from market_monitor.industry_graph.f10 import CompanyRepository
from market_monitor.industry_graph.f10.repository import company_detail_from_record


def _write_f10(data_root: Path) -> None:
    directory = data_root / "industry" / "f10"
    directory.mkdir(parents=True)
    cn = {
        "code": "600519",
        "market": "CN",
        "name": "贵州茅台",
        "total_market_cap": 16366.32,
        "float_market_cap": 16366.32,
        "industry": "食品饮料-白酒",
        "csrc_industry": "酒、饮料和精制茶制造业",
        "profile": "白酒生产和销售企业",
        "main_business": "白酒业务",
        "products": ["茅台酒"],
        "revenue_breakdown": [{"item": "茅台酒", "income": 146499906480.49, "ratio": 0.867695,
                               "period": "2025-12-31", "type": "2", "currency": "CNY"}],
        "source": "eastmoney_f10",
        "fetched_at": "2026-08-09T10:00:00+08:00",
        "status": "ok",
    }
    hk = {
        "code": "600519",
        "market": "HK",
        "name": "港股样本",
        "profile": "港股资料",
        "source": "eastmoney_f10",
        "fetched_at": "2026-08-09T10:00:00+08:00",
        "status": "ok",
    }
    (directory / "cn_f10.jsonl").write_text(json.dumps(cn, ensure_ascii=False) + "\n", encoding="utf-8")
    (directory / "hk_f10.jsonl").write_text(json.dumps(hk, ensure_ascii=False) + "\n", encoding="utf-8")


def test_company_repository_uses_canonical_instrument_and_money_provenance(tmp_path: Path) -> None:
    _write_f10(tmp_path)
    repository = CompanyRepository(tmp_path)

    page = repository.list_companies(sort="code")

    assert page.total == 2
    assert {item.instrument_key for item in page.items} == {"CN.SSE.STOCK.600519", "HK.HKEX.STOCK.600519"}
    detail = repository.company("CN.SSE.STOCK.600519")
    assert detail is not None
    document = detail.to_dict()
    assert document["totalMarketCap"] == {
        "value": 1_636_632_000_000.0,
        "currency": "CNY",
        "asOf": "2026-08-09T10:00:00+08:00",
        "source": "tencent_quote",
    }
    # Historical cache records do not have a dedicated creation timestamp;
    # their first detail fetch remains the stable fallback.
    assert document["createdAt"] == "2026-08-09T10:00:00+08:00"
    assert document["updatedAt"] == "2026-08-09T10:00:00+08:00"
    assert document["topRevenueSegment"]["name"] == "茅台酒"
    assert document["revenueSegments"][0]["amountWithoutTimestamp"]["currency"] == "CNY"
    assert "amount" not in document["revenueSegments"][0]


def test_top_revenue_is_maximum_within_latest_period_and_comparable_classification() -> None:
    record = {"code": "600001", "market": "CN", "name": "合成公司", "fetched_at": "2026-10-31T00:00:00Z",
              "revenue_breakdown": [
                  {"item": "旧产品", "income": 9999, "type": "2", "period": "2025-12-31", "currency": "CNY"},
                  {"item": "地区", "income": 8000, "type": "3", "period": "2026-06-30", "currency": "CNY"},
                  {"item": "行业", "income": 5000, "type": "1", "period": "2026-06-30", "currency": "CNY"},
                  {"item": "最大产品", "income": 200, "type": "2", "period": "2026-06-30", "currency": "CNY"},
                  {"item": "较小产品", "income": 100, "type": "2", "period": "2026-06-30", "currency": "CNY"},
                  {"item": "日期未知", "income": 99999, "type": "2", "currency": "CNY"},
              ]}
    detail = company_detail_from_record(record)
    assert detail is not None
    result = detail.to_dict()
    assert result["topRevenueSegment"]["name"] == "最大产品"
    assert result["topRevenueSegment"]["period"] == "2026-06-30"
    assert result["revenueSegments"][1]["classification"] == "region"
    record["revenue_breakdown"] = [{"item": "未知期", "income": 100, "type": "2"}]
    detail = company_detail_from_record(record)
    assert detail is not None and "topRevenueSegment" not in detail.to_dict()
    record["revenue_breakdown"] = [{"item": "地区", "income": 100, "type": "3", "period": "2026-06-30"}]
    detail = company_detail_from_record(record)
    assert detail is not None and "topRevenueSegment" not in detail.to_dict()


def test_revenue_amount_keeps_zero_negative_and_rejects_nonfinite() -> None:
    record = {"code": "600001", "market": "CN", "name": "合成公司", "fetched_at": "2026-10-31T00:00:00Z",
              "revenue_breakdown": [{"item": "零值", "income": 0}, {"item": "负值", "income": -5},
                                    {"item": "无穷", "income": float("inf")}]}
    detail = company_detail_from_record(record)
    assert detail is not None
    rows = detail.to_dict()["revenueSegments"]
    assert rows[0]["amountWithoutTimestamp"]["value"] == 0 and rows[1]["amountWithoutTimestamp"]["value"] == -5
    assert "amount" not in rows[2] and "amountWithoutTimestamp" not in rows[2]


def test_revenue_timestamp_uses_own_provenance_and_never_company_timestamp() -> None:
    record = {"code": "600001", "market": "CN", "name": "合成公司", "fetched_at": "2026-10-31T00:00:00Z",
              "revenue_breakdown": [
                  {"item": "有采集时间", "income": 100, "period": "2026-06-30", "type": "2", "currency": "CNY",
                   "fetched_at": "2026-10-20T03:00:00+00:00", "source": "synthetic_revenue"},
                  {"item": "缺采集时间", "income": 200, "period": "2026-06-30", "type": "2", "currency": "CNY"},
                  {"item": "无时区", "income": 10, "fetched_at": "2026-10-21T03:00:00"},
              ]}
    detail = company_detail_from_record(record)
    assert detail is not None
    rows = detail.to_dict()["revenueSegments"]
    assert rows[0]["fetchedAt"] == "2026-10-20T03:00:00+00:00"
    assert rows[0]["amount"]["asOf"] == rows[0]["fetchedAt"]
    assert rows[0]["amount"]["source"] == "synthetic_revenue"
    assert "fetchedAt" not in rows[1] and "amount" not in rows[1]
    assert rows[1]["amountWithoutTimestamp"] == {"value": 200.0, "currency": "CNY"}
    assert "amount" not in rows[2] and "fetchedAt" not in rows[2]
    assert detail.to_dict()["topRevenueSegment"]["name"] == "缺采集时间"


def test_revenue_ratios_reject_nonfinite_and_preserve_canonical_percent():
    record = {"code": "600001", "market": "CN", "name": "合成公司", "revenue_breakdown": [
        {"item": "旧百分数", "ratio": 86.5, "revenue_share_pct": 86.5},
        {"item": "超百分百比率", "ratio": 1.2, "revenue_share_pct": 120.0},
        {"item": "异常", "ratio": float("nan"), "revenue_share_pct": float("inf"), "gross_margin_pct": True},
    ]}
    detail = company_detail_from_record(record)
    assert detail is not None
    rows = detail.to_dict()["revenueSegments"]
    assert rows[0]["revenueSharePct"] == 86.5
    assert rows[1]["ratio"] == 1.2 and rows[1]["revenueSharePct"] == 120.0
    assert not any(key in rows[2] for key in ("ratio", "revenueSharePct", "grossMarginPct"))


def test_revenue_currency_is_taken_from_evidence_and_mixed_currencies_are_not_ranked() -> None:
    record = {"code": "00700", "market": "HK", "name": "合成公司", "fetched_at": "2026-10-31T00:00:00Z",
              "revenue_breakdown": [
                  {"item": "人民币", "income": 100, "currency": "CNY", "period": "2026-06-30", "type": "2"},
                  {"item": "港元", "income": 200, "currency": "HKD", "period": "2026-06-30", "type": "2"},
                  {"item": "未知", "income": 300, "period": "2026-06-30", "type": "2"},
              ]}
    detail = company_detail_from_record(record)
    assert detail is not None
    data = detail.to_dict()
    assert [row["amountWithoutTimestamp"]["currency"] for row in data["revenueSegments"]] == ["CNY", "HKD", "UNKNOWN"]
    assert "topRevenueSegment" not in data
    record["revenue_breakdown"] = record["revenue_breakdown"][:1]
    detail = company_detail_from_record(record)
    assert detail is not None and detail.to_dict()["topRevenueSegment"]["name"] == "人民币"


def test_company_repository_filters_and_enforces_page_bounds(tmp_path: Path) -> None:
    _write_f10(tmp_path)
    repository = CompanyRepository(tmp_path)

    page = repository.list_companies(query="茅台", market="CN", page_size=1)

    assert page.total == 1
    assert page.items[0].name == "贵州茅台"
    with pytest.raises(ValueError, match="page_size"):
        repository.list_companies(page_size=501)
    with pytest.raises(ValueError, match="sort"):
        repository.list_companies(sort="sql")
    with pytest.raises(ValueError, match="market"):
        repository.list_companies(market="US")


class _Server:
    def __init__(self, data_root: Path) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(data_root, quiet=True))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_f10_http_api_is_paginated_local_and_has_no_placeholder_values(tmp_path: Path) -> None:
    _write_f10(tmp_path)
    server = _Server(tmp_path)
    try:
        with urlopen(f"{server.base_url}/api/f10/companies?q=%E8%8C%85%E5%8F%B0&page_size=1", timeout=5) as response:
            page = json.loads(response.read().decode("utf-8"))
        assert page["total"] == 1
        assert page["items"][0]["instrumentKey"] == "CN.SSE.STOCK.600519"

        with urlopen(f"{server.base_url}/api/f10/companies/CN.SSE.STOCK.600519", timeout=5) as response:
            detail = json.loads(response.read().decode("utf-8"))
        encoded = json.dumps(detail, ensure_ascii=False)
        assert "undefined" not in encoded
        assert "Invalid Date" not in encoded
        assert "null" not in encoded

        with pytest.raises(HTTPError) as error:
            urlopen(f"{server.base_url}/api/f10/companies?sort=sql", timeout=5)
        assert error.value.code == 400
    finally:
        server.close()
