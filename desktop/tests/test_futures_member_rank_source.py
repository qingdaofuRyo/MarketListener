"""One published member-ranking source is selected per exchange and day."""

from __future__ import annotations

import pytest

from market_monitor.futures_member_rank_source import (
    CFFEX_PUBLISHED_SOURCE,
    CZCE_WORKBOOK_SOURCE,
    DCE_BATCH_SOURCE,
    GFEX_BROWSER_SOURCE,
    SHFE_INE_DAT_SOURCE,
    select_member_position_source_rows,
)


@pytest.mark.parametrize(
    ("exchange", "preferred"),
    [
        ("CFFEX", CFFEX_PUBLISHED_SOURCE),
        ("CZCE", CZCE_WORKBOOK_SOURCE),
        ("DCE", DCE_BATCH_SOURCE),
        ("GFEX", GFEX_BROWSER_SOURCE),
        ("SHFE", SHFE_INE_DAT_SOURCE),
        ("INE", SHFE_INE_DAT_SOURCE),
    ],
)
def test_verified_official_bronze_source_wins_without_double_counting(
    exchange: str, preferred: str,
) -> None:
    day = "2026-09-30"
    old = {"trading_day": day, "exchange": exchange,
           "source": f"official-{exchange.lower()}-rank-v1", "position": 50,
           "quality_status": "PASS"}
    new = {**old, "source": preferred, "position": 50}
    coverage = [
        {"trading_day": day, "exchange": exchange, "source": old["source"],
         "status": "PASS"},
        {"trading_day": day, "exchange": exchange, "source": preferred,
         "status": "PASS"},
    ]

    assert select_member_position_source_rows([old, new], coverage) == [new]
    coverage[1]["status"] = "FAILED"
    assert select_member_position_source_rows([old, new], coverage) == [old]


def test_unmatched_nonofficial_coverage_does_not_hide_published_rows() -> None:
    day = "2026-09-30"
    row = {"trading_day": day, "exchange": "SHFE",
           "source": "akshare-shfe-member-ranking", "quality_status": "LEGACY_WRAPPED"}
    unrelated_coverage = [{"trading_day": day, "exchange": "SHFE",
                           "source": "fixture-shfe-member-ranking", "status": "PASS"}]

    assert select_member_position_source_rows([row], unrelated_coverage) == [row]
