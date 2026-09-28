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
from .futures_structure import _read_contract_aggregates
from .storage import MarketStore


def run_futures_contract_heat_pipeline(data_root: Path) -> dict[str, Any]:
    """Build v2 without changing the archived product/deposit v1 heat rows."""

    root = Path(data_root)
    files = sorted(
        (root / "silver" / "market=CN" / "asset_type=FUTURE" / "period=1d").rglob("*.parquet")
    )
    bars, source_rows, rejected_rows = _read_contract_aggregates(files, end_day=None)
    store = MarketStore(root)
    try:
        official = store.list_futures_official_daily()
        coverage = store.list_futures_official_daily_coverage()
        results = calculate_contract_turnover_heat(
            bars, official, coverage, rule_book=load_rule_book(root)
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
    return {
        "status": "PASS" if latest and latest["data_quality_status"] == "PASS"
        else "PARTIAL" if results else "NO_DATA",
        "formulaVersion": FORMULA_VERSION,
        "silverFiles": len(files),
        "sourceRows": source_rows,
        "preparedContractDays": len(bars),
        "rejectedRows": rejected_rows,
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
