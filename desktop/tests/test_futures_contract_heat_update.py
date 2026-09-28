from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from market_monitor import futures_contract_heat_update as updater


def test_contract_heat_update_uses_shared_official_backfill_budget(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(updater, "run_bulk_futures", lambda *_args, **_kwargs: {"错误": []})
    monkeypatch.setattr(
        updater,
        "_read_contract_aggregates",
        lambda *_args, **_kwargs: ([{"trade_date": "2026-09-24"}], 1, 0),
    )
    monkeypatch.setattr(
        updater,
        "load_futures_trading_calendar",
        lambda _root: SimpleNamespace(trading_days=("2026-09-24",)),
    )

    def backfill(root, **kwargs):
        calls["data_root"] = root
        calls.update(kwargs)
        return {"status": "PASS", "outcomes": []}

    monkeypatch.setattr(updater, "run_official_futures_backfill", backfill)
    monkeypatch.setattr(updater, "sync_futures_rule_snapshots", lambda *_args, **_kwargs: {"status": "UPDATED"})
    monkeypatch.setattr(updater, "run_futures_contract_heat_pipeline", lambda _root: {"status": "PASS"})

    result = updater.run_futures_contract_heat_update(tmp_path)

    assert result["status"] == "PASS"
    assert calls["start_day"] == "2026-09-24"
    assert calls["end_day"] == "2026-09-24"
    assert calls["max_days"] == 1
    assert "CFFEX" not in calls["exchanges"]
