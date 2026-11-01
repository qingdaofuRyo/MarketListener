"""Historical official CZCE report units and actual new-product boundaries."""

from datetime import date, timedelta

import pytest

from market_monitor.futures_official_multipliers import load_official_multiplier_registry


@pytest.mark.parametrize("code,multiplier", [
    ("CF", 5), ("CJ", 5), ("CY", 5), ("PF", 5), ("PK", 5), ("SF", 5), ("SM", 5), ("TA", 5),
    ("AP", 10), ("MA", 10), ("OI", 10), ("RM", 10), ("RS", 10), ("SR", 10),
    ("FG", 20), ("JR", 20), ("LR", 20), ("RI", 20), ("SA", 20), ("UR", 20), ("WH", 20),
    ("PM", 50), ("ZC", 100),
])
def test_mature_czce_units_have_dated_report_evidence_from_backfill_start(code: str, multiplier: int) -> None:
    book = load_official_multiplier_registry()
    spec = book.resolve("2023-01-03", "CZCE", code)
    assert spec is not None
    assert spec.contract_multiplier == multiplier
    assert spec.source_url.endswith("/2023/02/13/1655835892573322-1655835892612145.pdf")
    assert book.resolve("2022-12-30", "CZCE", code) is None
    modern = book.resolve("2026-09-30", "CZCE", code)
    assert modern is not None and modern.contract_multiplier == multiplier


@pytest.mark.parametrize("code,first_day,multiplier,report_name", [
    ("PX", "2023-09-15", 5, "1697226838884489-1697226838904727.pdf"),
    ("SH", "2023-09-15", 30, "1697226838884489-1697226838904727.pdf"),
    ("PR", "2024-08-30", 15, "1726389999932285-1726389999951648.pdf"),
    ("PL", "2025-07-22", 20, "1750862830930040-1750862830943566.pdf"),
])
def test_new_czce_units_start_on_official_listing_dates(
    code: str, first_day: str, multiplier: int, report_name: str,
) -> None:
    book = load_official_multiplier_registry()
    first = date.fromisoformat(first_day)
    assert book.resolve(first - timedelta(days=1), "CZCE", code) is None
    historical = book.resolve(first, "CZCE", code)
    assert historical is not None
    assert historical.contract_multiplier == multiplier
    assert historical.source_url.endswith(report_name)
    modern = book.resolve("2026-01-01", "CZCE", code)
    assert modern is not None
    assert modern.contract_multiplier == historical.contract_multiplier
    assert modern.source_url != historical.source_url
