"""Materialize contract-count and official-turnover heat from isolated local facts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .futures_contract_turnover_heat import (
    FORMULA_VERSION,
    calculate_contract_turnover_heat,
)
from .futures_rule_sync import load_rule_book
from .futures_official_daily_quality import validated_daily_inputs
from .futures_weekly_heat_gold import rebuild_weekly_heat_gold
from .storage import MarketStore


def run_futures_contract_heat_pipeline(data_root: Path) -> dict[str, Any]:
    """Build v2 without changing the archived product/deposit v1 heat rows."""

    root = Path(data_root)
    store = MarketStore(root)
    try:
        official, coverage = validated_daily_inputs(
            store.list_futures_official_daily(), store.list_futures_official_daily_coverage(),
        )
        results = calculate_contract_turnover_heat(
            (), official, coverage, rule_book=load_rule_book(root)
        )
        calculated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in results:
            row["calculated_at"] = calculated_at
        written = store.replace_futures_contract_turnover_heat(
            results, formula_version=FORMULA_VERSION
        )
    finally:
        store.close()
    latest = results[-1] if results else None
    weekly = rebuild_weekly_heat_gold(root, as_of_day=latest["trade_date"]) if latest else {
        "status": "NO_DATA", "formulaVersion": None, "writtenDays": 0,
    }
    daily_status = "PASS" if latest and latest["data_quality_status"] == "PASS" else "PARTIAL" if results else "NO_DATA"
    return {
        "status": "PASS" if daily_status == weekly["status"] == "PASS"
        else "PARTIAL" if results else "NO_DATA",
        "dailyStatus": daily_status,
        "weekly": weekly,
        "formulaVersion": FORMULA_VERSION,
        "sourceRows": len(official),
        "preparedContractDays": len(official),
        "officialRows": len(official),
        "officialCoverageRows": len(coverage),
        "writtenDays": written,
        "firstDay": results[0]["trade_date"] if results else None,
        "latestDay": latest["trade_date"] if latest else None,
        "latestDirectionCoverage": latest["direction_coverage"] if latest else None,
        "latestTurnoverCoverage": latest["turnover_coverage"] if latest else None,
        "latestMissingExchanges": latest["missing_exchanges_json"] if latest else None,
        "calculatedAt": calculated_at,
    }


__all__ = ("run_futures_contract_heat_pipeline",)
