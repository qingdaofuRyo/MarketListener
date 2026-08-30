from io import BytesIO

from openpyxl import Workbook
import pytest

from market_monitor import collector


def _bea_workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Table 1"
    sheet.append(["Period", None, None, None, None, None, None, None, " Imports", None])
    sheet.append([None, None, None, None, None, None, None, "Total", "Goods", "Services"])
    sheet.append(["Annual", None, None, None, None, None, None, None, None, None])
    sheet.append(["2026 May (R)", None, None, None, None, None, None, 395280, 316863, 78417])
    sheet.append(["2026 Jun", None, None, None, None, None, None, 387989, 309011, 78978])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_bea_trade_workbook_parser_reads_only_monthly_total_imports():
    assert collector._parse_bea_trade_imports_xlsx(_bea_workbook_bytes()) == [
        ("2026-05", 395280.0),
        ("2026-06", 387989.0),
    ]


def test_bea_trade_workbook_parser_rejects_unknown_layout():
    workbook = Workbook()
    output = BytesIO()
    workbook.save(output)

    with pytest.raises(ValueError, match="missing Table 1"):
        collector._parse_bea_trade_imports_xlsx(output.getvalue())


def test_bea_trade_release_link_is_discovered_from_official_page():
    page = '<a href="/sites/default/files/2026-08/trad0626-time-series.xlsx">xlsx</a>'

    assert collector._discover_bea_trade_xlsx_url(page) == (
        "https://www.bea.gov/sites/default/files/2026-08/trad0626-time-series.xlsx"
    )


def test_bea_core_pce_parser_uses_annualized_change_only_through_final_quarter():
    nipa_text = "\n".join([
        "%SeriesCode,Period,Value",
        "DPCCRG,2025Q4,\"127.533\"",
        "DPCCRG,2026Q1,\"128.920\"",
        "DPCCRG,2026Q2,\"130.055\"",
    ])

    rows = collector._parse_bea_core_pce_final_rows(nipa_text, final_through=(2026, 1))

    assert rows == [("2026-Q1", pytest.approx(4.42173, abs=0.00001))]


def test_bea_gdp_release_stage_sets_final_cutoff():
    assert collector._parse_bea_gdp_final_cutoff(
        "GDP (Second Estimate) and Corporate Profits, 2nd Quarter 2026"
    ) == (2026, 1)
    assert collector._parse_bea_gdp_final_cutoff(
        "GDP (Third Estimate), 4th Quarter 2026"
    ) == (2026, 4)
