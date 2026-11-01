"""Date-effective official specification evidence, without live requests."""

from datetime import date

import pytest

from market_monitor.futures import futures_product_name
from market_monitor.futures_official_multipliers import load_official_multiplier_registry


@pytest.mark.parametrize("code", ["L-F", "V-F", "PP-F"])
def test_month_average_units_start_on_first_trading_day(code: str) -> None:
    registry = load_official_multiplier_registry()
    assert registry.resolve("2025-10-28", "DCE", code) is None
    spec = registry.resolve("2025-10-29", "DCE", code)
    assert spec is not None
    assert spec.contract_multiplier == 5
    assert spec.quote_unit == "CNY/ton"
    assert spec.last_trading_day_rule == "LAST_TRADING_DAY_OF_PREVIOUS_MONTH"
    assert "clientDf.do?id=" in spec.source_url


@pytest.mark.parametrize(
    ("code", "first_day", "multiplier"),
    [("SI", date(2022, 12, 22), 5), ("LC", date(2023, 7, 21), 1),
     ("PS", date(2024, 12, 26), 3)],
)
def test_gfex_historical_original_contract_units(
    code: str, first_day: date, multiplier: int,
) -> None:
    registry = load_official_multiplier_registry()
    spec = registry.resolve(first_day, "GFEX", code)
    assert spec is not None
    assert spec.contract_multiplier == multiplier
    assert spec.effective_from == first_day
    assert "contractInfo" not in spec.source_url
    assert "gfex.com.cn" in spec.source_url


def test_month_average_names_keep_distinct_product_identity() -> None:
    assert futures_product_name("L-F", "DCE") == "聚乙烯月均价"
    assert futures_product_name("V-F", "DCE") == "聚氯乙烯月均价"
    assert futures_product_name("PP-F", "DCE") == "聚丙烯月均价"
    assert futures_product_name("L", "DCE") != futures_product_name("L-F", "DCE")


@pytest.mark.parametrize("exchange,code,multiplier", [
    ("SHFE", "AL", 5), ("SHFE", "NI", 1), ("SHFE", "RB", 10),
    ("INE", "SC", 1000),
])
def test_shfe_dated_report_notes_supply_historical_units(
    exchange: str, code: str, multiplier: int,
) -> None:
    spec = load_official_multiplier_registry().resolve("2023-06-19", exchange, code)
    assert spec is not None
    assert spec.contract_multiplier == multiplier
    assert "shfe.com.cn/" in spec.source_url
    assert spec.effective_from <= date(2023, 6, 19)


def test_newly_listed_products_do_not_inherit_old_report_note_start() -> None:
    registry = load_official_multiplier_registry()
    assert registry.resolve("2023-06-19", "SHFE", "AD") is None
    assert registry.resolve("2023-06-19", "SHFE", "OP") is None
    assert registry.resolve("2023-06-19", "INE", "EC") is None


@pytest.mark.parametrize("exchange,code,multiplier", [
    ("SHFE", "CU", 5), ("SHFE", "AL", 5), ("SHFE", "ZN", 5),
    ("SHFE", "PB", 5), ("SHFE", "NI", 1), ("SHFE", "SN", 1),
    ("SHFE", "AG", 15), ("SHFE", "BU", 10), ("SHFE", "FU", 10),
    ("SHFE", "RU", 10), ("SHFE", "SP", 10), ("SHFE", "RB", 10),
    ("SHFE", "HC", 10), ("SHFE", "SS", 5), ("SHFE", "WR", 10),
    ("INE", "SC", 1000),
])
def test_pre_2023_official_handbook_contract_texts_cover_requested_start(
    exchange: str, code: str, multiplier: int,
) -> None:
    spec = load_official_multiplier_registry().resolve("2023-01-03", exchange, code)
    assert spec is not None
    assert spec.contract_multiplier == multiplier
    assert spec.source_url.endswith(".pdf")
    assert spec.effective_from == date(2023, 1, 1)
