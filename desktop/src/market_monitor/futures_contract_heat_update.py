"""Refresh local month-contract bars, official daily turnover, and heat Gold."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .futures_calendar import load_futures_trading_calendar, sync_futures_trading_calendar
from .futures_bulk import run_bulk_futures
from .futures_contract_heat_pipeline import run_futures_contract_heat_pipeline
from .futures_official_backfill import (
    DEFAULT_EXCHANGES,
    HostRateLimiter,
    run_official_futures_backfill,
)
from .futures_contract_turnover_heat import EXCLUDED_EXCHANGES
from .futures_rule_sync import sync_futures_rule_snapshots
from .futures_structure import _read_contract_aggregates


def run_futures_contract_heat_update(data_root: Path) -> dict[str, Any]:
    """Import TDX Futures' latest local files, fetch same-day exchange reports, and rebuild heat."""

    root = Path(data_root)
    local_import = run_bulk_futures(
        root,
        include_domestic=True,
        include_global=False,
        include_akshare=False,
    )
    files = sorted(
        (root / "silver" / "market=CN" / "asset_type=FUTURE" / "period=1d").rglob("*.parquet")
    )
    bars, _source_rows, _rejected_rows = _read_contract_aggregates(files, end_day=None)
    if not bars:
        return {
            "status": "PARTIAL_FAILURE",
            "localImport": local_import,
            "officialTurnover": {"status": "NOT_RUN", "exchanges": []},
            "calculation": {"status": "NO_DATA", "latestDay": None, "latestTurnoverCoverage": None},
            "ruleSync": {"status": "NOT_RUN"},
        }

    latest_day = max(str(row["trade_date"]) for row in bars)
    heat_exchanges = tuple(exchange for exchange in DEFAULT_EXCHANGES if exchange not in EXCLUDED_EXCHANGES)
    calendar = load_futures_trading_calendar(root)
    if calendar is None:
        sync_futures_trading_calendar(root)
        calendar = load_futures_trading_calendar(root)
    if calendar is None or latest_day not in calendar.trading_days:
        official = {
            "status": "PARTIAL",
            "exchanges": [],
            "error": f"{latest_day} 不在已核验交易日历中，未请求交易所官网",
        }
    else:
        daily = run_official_futures_backfill(
            root, start_day=latest_day, end_day=latest_day, max_days=1,
            exchanges=heat_exchanges,
        )
        official = {
            "status": daily.get("status"),
            "exchanges": [
                {
                    "exchange": item.get("exchange"),
                    "status": item.get("status"),
                    "error": item.get("error"),
                }
                for item in daily.get("outcomes", [])
            ],
            "requestBudget": daily.get("rateLimit"),
        }
    try:
        # Rule snapshots use a provider adapter, so reserve a shared global
        # request lease before calling it as well.
        rule_limiter = HostRateLimiter(root)
        rule_host = "futures-rule-adapter"
        rule_limiter.acquire(rule_host)
        try:
            rules = sync_futures_rule_snapshots(root, target_days=[latest_day])
        finally:
            rule_limiter.complete(rule_host)
    except Exception as error:  # A rule provider outage leaves deposits explicitly unavailable.
        rules = {"status": "FAILED", "error": f"{type(error).__name__}: {error}"[:300]}
    calculation = run_futures_contract_heat_pipeline(root)
    local_errors = local_import.get("错误")
    local_ok = isinstance(local_errors, list) and not local_errors
    status = (
        "PASS"
        if local_ok and official.get("status") == "PASS"
        and rules.get("status") in {"UPDATED", "UNCHANGED"}
        and calculation.get("status") == "PASS"
        else "PARTIAL_FAILURE"
    )
    return {
        "status": status,
        "localImport": local_import,
        "officialTurnover": official,
        "ruleSync": rules,
        "calculation": calculation,
    }


__all__ = ("run_futures_contract_heat_update",)
