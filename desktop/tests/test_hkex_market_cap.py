"""Official HKEX market capitalisation needs two same-day audited boards."""

from __future__ import annotations

from datetime import date

import pytest

from market_monitor.hkex_market_cap import _parse_archive
from market_monitor.web_api.data_sections import _merge_hk_cap_points, _screen_hk_tdx_coverage
from market_monitor.collector import build_collection_tasks


def _archive(day: str = "2026/09/30", amount: str = "45,407,005,985,138") -> dict:
    body = [{"row": index, "col": 0, "text": f"2026/09/{index:02}"}
            for index in range(1, 30)]
    body += [{"row": index, "col": 1, "text": "45,000,000,000,000"}
             for index in range(1, 30)]
    body += [{"row": 30, "col": 0, "text": day},
             {"row": 30, "col": 1, "text": amount}]
    return {"tables": [{"header": [{"col": 0, "text": "Year/Month/Day"},
                                   {"col": 1, "text": "Total market capitalisation<br/>(HKD)"}],
                        "body": body, "footer": [{"text": "Updated 30 Sep 2026"}]}]}


def test_hkex_parser_rejects_currency_future_day_and_duplicate_cells() -> None:
    result = _parse_archive(_archive(), today=date(2026, 10, 21))
    assert result["2026-09-30"] == 45_407_005_985_138
    wrong_currency = _archive()
    wrong_currency["tables"][0]["header"][1]["text"] = "Total market capitalisation (USD)"
    with pytest.raises(ValueError, match="currency"):
        _parse_archive(wrong_currency, today=date(2026, 10, 21))
    future = _archive(day="2026/10/30")
    with pytest.raises(ValueError, match="future"):
        _parse_archive(future, today=date(2026, 10, 21))
    duplicate = _archive()
    duplicate["tables"][0]["body"].append({"row": 30, "col": 1, "text": "45,407,005,985,138"})
    with pytest.raises(ValueError, match="duplicate"):
        _parse_archive(duplicate, today=date(2026, 10, 21))


def test_hkex_website_collector_is_not_in_default_automatic_tasks() -> None:
    assert "HKEX_MARKET_CAP" not in {task.dataset_id for task in
                                     build_collection_tasks(limit_futures=0, limit_cn_stocks=0)}


def test_official_cap_does_not_fill_missing_hk_tdx_turnover_or_breadth() -> None:
    points = _merge_hk_cap_points(
        [{"tradingDay": "2026-08-18", "totalMarketCapYi": None,
          "turnoverYi": 100.0, "advances": 3.0, "declines": 2.0}],
        [{"tradingDay": "2026-09-30", "totalMarketCapYi": 454_803.88552796,
          "updatedAt": "2026-10-20T00:00:00Z"}],
    )
    assert points[-1]["totalMarketCapYi"] == pytest.approx(454_803.88552796)
    assert points[-1]["turnoverYi"] is None
    assert points[-1]["advances"] is None
    assert points[0]["turnoverYi"] == 100.0


def test_local_hk_coverage_collapse_breaks_turnover_and_breadth() -> None:
    points = _screen_hk_tdx_coverage([
        {"tradingDay": "2026-08-14", "turnoverYi": 1989.33, "turnoverCoverage": 2724,
         "advances": 1000.0, "unchanged": 200.0, "declines": 1000.0},
        {"tradingDay": "2026-08-17", "turnoverYi": 11.8190048, "turnoverCoverage": 706,
         "advances": 210.0, "unchanged": 100.0, "declines": 396.0},
        {"tradingDay": "2026-08-18", "turnoverYi": 1943.27, "turnoverCoverage": 2763,
         "advances": 1200.0, "unchanged": 200.0, "declines": 1000.0},
    ])
    assert points[1]["turnoverYi"] is None
    assert points[1]["advances"] is None
    assert points[1]["coverageStatus"] == "INSUFFICIENT_LOCAL_FILES"
    assert points[1]["turnoverCoverage"] == 706
    assert points[1]["referenceCoverage"] == 2763
    assert points[0]["turnoverYi"] == 1989.33
    assert points[2]["turnoverYi"] == 1943.27


def test_week_of_low_hk_coverage_stays_broken() -> None:
    points = _screen_hk_tdx_coverage([
        {"tradingDay": f"2026-08-{day:02}", "turnoverYi": 1000.0,
         "turnoverCoverage": 2700, "advances": 1000.0}
        for day in range(3, 8)
    ] + [
        {"tradingDay": f"2026-08-{day:02}", "turnoverYi": 10.0,
         "turnoverCoverage": 700, "advances": 200.0}
        for day in range(10, 17)
    ] + [
        {"tradingDay": "2026-08-17", "turnoverYi": 900.0,
         "turnoverCoverage": 2690, "advances": 1000.0},
    ])
    assert all(point["turnoverYi"] is None and point["coverageStatus"] == "INSUFFICIENT_LOCAL_FILES"
               for point in points[5:12])
    assert points[-1]["turnoverYi"] == 900.0
