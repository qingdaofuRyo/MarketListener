from __future__ import annotations

import json

import pytest

from market_monitor.futures_official_multipliers import REGISTRY_PATH, load_official_multiplier_registry


_MATURE_UNITS = {
    "A": 10, "B": 10, "BB": 500, "C": 10, "CS": 10, "EB": 5, "EG": 10,
    "FB": 10, "I": 100, "J": 100, "JD": 10, "JM": 60, "L": 5, "LH": 16,
    "M": 10, "P": 10, "PG": 20, "PP": 5, "RR": 10, "V": 5, "Y": 10,
}


@pytest.mark.parametrize(("product", "multiplier"), _MATURE_UNITS.items())
def test_dce_2023_ordinary_units_use_historical_evidence(product: str, multiplier: int) -> None:
    registry = load_official_multiplier_registry()
    historical = registry.resolve("2023-01-03", "DCE", product)
    assert historical is not None and historical.contract_multiplier == multiplier
    assert historical.effective_from.isoformat() == "2023-01-01"
    assert historical.effective_to.isoformat() == "2025-09-23"
    current = registry.resolve("2025-09-24", "DCE", product)
    assert current is not None and current.contract_multiplier == multiplier
    assert current.effective_from.isoformat() == "2025-09-24"
    assert registry.resolve("2022-12-30", "DCE", product) is None


def test_dce_quote_denominators_keep_chicken_and_board_notional_units_distinct() -> None:
    registry = load_official_multiplier_registry()
    chicken = registry.resolve("2023-01-03", "DCE", "JD")
    plywood = registry.resolve("2023-01-03", "DCE", "BB")
    fibreboard = registry.resolve("2023-01-03", "DCE", "FB")
    assert chicken.quote_unit == "CNY/500kg"
    assert chicken.contract_multiplier == 5_000 / 500
    assert plywood.quote_unit == "CNY/sheet" and plywood.contract_multiplier == 500
    assert fibreboard.quote_unit == "CNY/cubic-metre" and fibreboard.contract_multiplier == 10


@pytest.mark.parametrize(("product", "day_before", "listing_day", "multiplier"), [
    ("LG", "2024-11-15", "2024-11-18", 90),
    ("BZ", "2025-07-07", "2025-07-08", 30),
])
def test_dce_new_products_start_at_verified_listing_date(
    product: str, day_before: str, listing_day: str, multiplier: int,
) -> None:
    registry = load_official_multiplier_registry()
    assert registry.resolve(day_before, "DCE", product) is None
    first = registry.resolve(listing_day, "DCE", product)
    assert first is not None and first.contract_multiplier == multiplier
    assert registry.resolve("2023-01-03", "DCE", product) is None


def test_dce_historical_records_retain_independent_source_versions_and_hashes() -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    for code in (*_MATURE_UNITS, "LG", "BZ"):
        history, current = payload["products"][f"DCE.{code}"]
        assert history["evidenceId"] != "DCE_CONTRACT_INFO_20261003"
        evidence = payload["evidence"][history["evidenceId"]]
        assert evidence["qualityStatus"] == "PASS" and len(evidence["payloadSha256"]) == 64
        assert "dce.com.cn/" in evidence["sourceUrl"]
        assert evidence["rawPath"].startswith("bronze/official_multiplier_history_dce/")
        assert current["evidenceId"] == "DCE_CONTRACT_INFO_20261003"
