"""Build and sign immutable Android sync packages from the local store."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .market_package import PackageLedger, build_market_package
from .profile_gate import profile_write_gate
from .quality import QualityReport
from .signing import sign_market_package
from .storage import MarketStore


RUN_STATUS_MAP = {
    "COMPLETE": "PASS",
    "FAILED": "FAILED",
    "PARTIAL_FAILURE": "PARTIAL_FAILURE",
    "BLOCKED": "BLOCKED",
    "RUNNING": "BLOCKED",
    "UNSUPPORTED": "UNSUPPORTED",
}


def load_all_bars(store: MarketStore) -> list[dict[str, Any]]:
    """Read every complete silver partition into normalized package bars."""

    rows = store.connection.execute(
        "SELECT file_path FROM partitions WHERE status='COMPLETE' ORDER BY partition_id"
    ).fetchall()
    bars: list[dict[str, Any]] = []
    for (relative_path,) in rows:
        parquet = store.root / relative_path
        if not parquet.is_file():
            continue
        for (bar_json,) in store.connection.execute(
            "SELECT bar_json FROM read_parquet(?) WHERE bar_json IS NOT NULL",
            [str(parquet)],
        ).fetchall():
            bar = json.loads(bar_json)
            bar["instrument_key"] = _instrument_key(bar)
            bars.append(bar)
    return bars


def load_all_metrics(store: MarketStore) -> list[dict[str, Any]]:
    """Read every gold-layer metric row for the Android data page."""

    return [
        {
            "metric_id": row[0],
            "instrument_id": row[1],
            "trading_date": row[2],
            "period": row[3],
            "metric_name": row[4],
            "value": row[5],
            "definition": row[6],
            "calculation_method": row[7],
            "timestamp": row[8],
        }
        for row in store.connection.execute(
            "SELECT metric_id, instrument_id, trading_date, period, metric_name, value, "
            "definition, calculation_method, timestamp FROM gold_metrics ORDER BY metric_id"
        ).fetchall()
    ]


def load_source_run_summaries(store: MarketStore) -> list[dict[str, str]]:
    """Build manifest source summaries from the store run log."""

    summaries: list[dict[str, str]] = []
    for run_id, provider, status in store.connection.execute(
        "SELECT run_id, provider, status FROM runs ORDER BY started_at"
    ).fetchall():
        summaries.append(
            {
                "run_id": run_id,
                "provider": provider,
                "status": RUN_STATUS_MAP.get(status, "UNSUPPORTED"),
            }
        )
    return summaries


def build_android_package(
    data_root: Path,
    private_key: Path,
    *,
    ecdsa_private_key: Path | None = None,
    minimum_app_version: str = "0.1.0",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build, sign, and activate one immutable package for Android sync."""

    if not private_key.is_file():
        raise FileNotFoundError(f"signing private key not found: {private_key}")
    data_root = Path(data_root)
    store = MarketStore(data_root)
    try:
        bars = load_all_bars(store)
        metrics = load_all_metrics(store)
        source_run_summaries = load_source_run_summaries(store)
        cutoff_rows = store.connection.execute(
            "SELECT data_cutoff FROM partitions WHERE status='COMPLETE'"
        ).fetchall()
        data_cutoff = max((row[0] for row in cutoff_rows), default="")
        if not data_cutoff and metrics:
            latest_date = max(metric["trading_date"] for metric in metrics)
            data_cutoff = f"{latest_date}T15:00:00+08:00"
        if not data_cutoff:
            data_cutoff = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    finally:
        store.close()

    packages_dir = data_root / "packages"
    ledger_path = packages_dir / "ledger.sqlite"
    package_id = f"market-{(now or datetime.now(timezone.utc)).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    report = QualityReport("ANDROID-SYNC-ALL", [])
    extra_files: list[tuple[str, Path]] = []
    industry_atlas = data_root / "industry" / "industry-atlas.html"
    if industry_atlas.is_file():
        extra_files.append(("industry/industry-atlas.html", industry_atlas))
    package_path = build_market_package(
        packages_dir,
        package_id,
        bars,
        report,
        data_cutoff,
        source_run_summaries,
        minimum_app_version=minimum_app_version,
        gold_metrics=metrics,
        extra_files=extra_files,
    )
    sign_market_package(package_path, private_key, ecdsa_private_key)
    # The backup includes the ledger, so registration and activation must be
    # one capture-visible logical operation even though SQLite commits twice.
    with profile_write_gate(data_root):
        ledger = PackageLedger(ledger_path)
        try:
            ledger.register(package_id, "FULL")
            ledger.activate(package_id)
        finally:
            ledger.close()
    return {
        "package_id": package_id,
        "package_path": str(package_path),
        "package_bytes": package_path.stat().st_size,
        "bars": len(bars),
        "gold_metrics": len(metrics),
        "data_cutoff": data_cutoff,
        "source_runs": len(source_run_summaries),
        "signature": "ed25519+ecdsa" if ecdsa_private_key is not None else "ed25519",
        "ledger": str(ledger_path),
        "industry_atlas": str(industry_atlas) if industry_atlas.is_file() else None,
    }


def latest_package_info(data_root: Path) -> dict[str, Any] | None:
    """Return metadata for the active package, or None when none exists."""

    ledger_path = Path(data_root) / "packages" / "ledger.sqlite"
    if not ledger_path.is_file():
        return None
    ledger = PackageLedger(ledger_path)
    try:
        active = ledger.active()
        if active is None:
            return None
        package_path = ledger_path.parent / f"{active.package_id}.zip"
        if not package_path.is_file():
            return None
        return {
            "package_id": active.package_id,
            "package_type": active.package_type,
            "base_package_id": active.base_package_id,
            "built_at": active.built_at,
            "status": active.status,
            "package_bytes": package_path.stat().st_size,
            "download_url": "/api/android-package",
        }
    finally:
        ledger.close()


def _instrument_key(bar: Mapping[str, Any]) -> dict[str, str]:
    """Derive a 4-part instrument key from a stored instrument_id."""

    instrument_id = str(bar.get("instrument_id") or bar.get("symbol") or "")
    parts = instrument_id.split(".")
    return {
        "country_or_market": parts[0] if len(parts) > 0 else "",
        "exchange": parts[1] if len(parts) > 1 else "",
        "asset_type": parts[2] if len(parts) > 2 else "",
        "code": ".".join(parts[3:]) if len(parts) > 3 else "",
    }
