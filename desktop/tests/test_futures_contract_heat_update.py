from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_monitor import futures_contract_heat_update as updater
from market_monitor.futures_auto_refresh import due_operations
from market_monitor.operations import Operation, OperationKind, OperationStatus
from market_monitor.web_app import _operation_handlers


@pytest.fixture(autouse=True)
def no_user_credentials(monkeypatch):
    monkeypatch.setattr("market_monitor.dce_official_api.load_accounts", lambda: [])


def test_update_catches_official_calendar_gaps_without_tdx(tmp_path: Path, monkeypatch):
    days = ["2023-01-03", "2026-09-24", "2026-09-29", "2026-09-30"]
    calls = []
    monkeypatch.setattr(updater, "load_futures_trading_calendar", lambda root: SimpleNamespace(trading_days=days, between=lambda start, end: [day for day in days if start <= day <= end]))
    monkeypatch.setattr(updater, "sync_futures_trading_calendar", lambda root: None)
    monkeypatch.setattr(updater, "run_official_futures_backfill", lambda root, **kwargs: calls.append(("daily", kwargs)) or {"status": "PASS", "rateLimit": {}})
    monkeypatch.setattr(updater, "_update_dce_browser", lambda root, days: calls.append(("browser", days)) or {"status": "CACHED"})
    monkeypatch.setattr(updater, "_covered_official_days", lambda root, days: {(day, venue) for day in days for venue in updater.DEFAULT_EXCHANGES})
    monkeypatch.setattr(updater, "run_futures_contract_heat_pipeline", lambda root: calls.append(("heat", None)) or {"status": "PASS", "dailyStatus": "PASS"})
    monkeypatch.setattr(updater, "run_official_product_structure_pipeline", lambda root, **kwargs: calls.append((kwargs["metric"], kwargs)) or {"status": "PASS"})
    result = updater.run_futures_contract_heat_update(tmp_path, today=date(2026, 10, 3))
    assert result["status"] == "PASS"
    assert [item[0] for item in calls] == ["daily", "browser", "heat", "open_interest", "notional", "volume"]
    assert calls[0][1]["start_day"] == "2023-01-01" and calls[0][1]["end_day"] == "2026-09-30"
    assert "CFFEX" in calls[0][1]["exchanges"]
    assert "run_bulk_futures" not in vars(updater) and "_read_contract_aggregates" not in vars(updater)
    calls.clear()
    recent = updater.run_futures_contract_heat_update(tmp_path, today=date(2026, 10, 3),
                                                       recent_trading_days=2)
    assert recent["status"] == "PASS"
    assert calls[0][1]["start_day"] == "2026-09-29"
    assert calls[1][1] == ["2026-09-29", "2026-09-30"]
    assert all(item[1]["start_day"] == "2026-09-29" for item in calls[3:])


def test_recent_daily_refresh_ignores_legacy_weekly_quality_but_not_source_gaps(
    tmp_path: Path, monkeypatch,
):
    day = "2026-09-30"
    calendar = SimpleNamespace(trading_days=[day], between=lambda start, end: [day])
    monkeypatch.setattr(updater, "load_futures_trading_calendar", lambda root: calendar)
    monkeypatch.setattr(updater, "run_official_futures_backfill", lambda root, **kwargs: {"status": "PASS"})
    monkeypatch.setattr(updater, "_update_dce_browser", lambda root, days: {"status": "CACHED"})
    covered = {(day, venue) for venue in updater.DEFAULT_EXCHANGES}
    monkeypatch.setattr(updater, "_covered_official_days", lambda root, days: covered)
    monkeypatch.setattr(updater, "run_futures_contract_heat_pipeline", lambda root: {
        "status": "PARTIAL", "dailyStatus": "PASS",
        "weekly": {"status": "PARTIAL", "partialDays": 480, "pendingDays": 0},
    })
    monkeypatch.setattr(updater, "run_official_product_structure_pipeline", lambda root, **kwargs: {"status": "PASS"})

    result = updater.run_futures_contract_heat_update(tmp_path, today=date(2026, 10, 1), recent_trading_days=1)
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["refreshStatus"] == "PASS"
    assert result["calculation"]["weekly"]["partialDays"] == 480

    covered.remove((day, "DCE"))
    result = updater.run_futures_contract_heat_update(tmp_path, today=date(2026, 10, 1), recent_trading_days=1)
    assert result["refreshStatus"] == "PARTIAL_FAILURE"


def test_recent_daily_refresh_requires_latest_daily_heat_and_structures(tmp_path: Path, monkeypatch):
    day = "2026-09-30"
    calendar = SimpleNamespace(trading_days=[day], between=lambda start, end: [day])
    monkeypatch.setattr(updater, "load_futures_trading_calendar", lambda root: calendar)
    monkeypatch.setattr(updater, "run_official_futures_backfill", lambda root, **kwargs: {"status": "PASS"})
    monkeypatch.setattr(updater, "_update_dce_browser", lambda root, days: {"status": "CACHED"})
    monkeypatch.setattr(updater, "_covered_official_days", lambda root, days: {
        (day, venue) for venue in updater.DEFAULT_EXCHANGES
    })
    daily_status = {"status": "PARTIAL", "dailyStatus": "PARTIAL"}
    structure_status = {"status": "PASS"}
    monkeypatch.setattr(updater, "run_futures_contract_heat_pipeline", lambda root: daily_status)
    monkeypatch.setattr(updater, "run_official_product_structure_pipeline", lambda root, **kwargs: structure_status)

    result = updater.run_futures_contract_heat_update(tmp_path, today=date(2026, 10, 1), recent_trading_days=1)
    assert result["refreshStatus"] == "PARTIAL_FAILURE"
    daily_status.update(status="PASS", dailyStatus="PASS")
    structure_status["status"] = "PARTIAL"
    result = updater.run_futures_contract_heat_update(tmp_path, today=date(2026, 10, 1), recent_trading_days=1)
    assert result["refreshStatus"] == "PARTIAL_FAILURE"


def test_scheduled_daily_handler_passes_with_historical_weekly_partial(tmp_path: Path, monkeypatch):
    from market_monitor import market_tdx_futures_projection

    monkeypatch.setattr(updater, "run_futures_contract_heat_update", lambda root, **kwargs: {
        "status": "PARTIAL_FAILURE", "refreshStatus": "PASS",
        "officialDaily": {"startDay": "2026-09-30", "endDay": "2026-09-30", "exchanges": []},
        "structures": [{"status": "PASS"}] * 3,
        "calculation": {"weekly": {"latestQuality": "PARTIAL", "pendingDays": 0, "partialDays": 480}},
    })
    monkeypatch.setattr(market_tdx_futures_projection, "refresh_financial_coverage", lambda root: None)
    detail = _operation_handlers(tmp_path)[OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE]()
    assert detail.startswith("PASS:")
    assert "部分质量日 480" in detail

    completed = Operation("daily", OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
                          OperationStatus.PASS, "2026-09-30T12:00:00+00:00")
    calendar = SimpleNamespace(trading_days=["2026-09-30"])
    now = datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)
    assert (OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE, "2026-09-30") not in due_operations(
        calendar, [completed], now,
    )


def test_missing_calendar_prevents_requests(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(updater, "load_futures_trading_calendar", lambda root: None)
    monkeypatch.setattr(updater, "sync_futures_trading_calendar", lambda root: None)
    monkeypatch.setattr(updater, "run_official_futures_backfill", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not request")))
    assert updater.run_futures_contract_heat_update(tmp_path)["status"] == "PARTIAL_FAILURE"


def test_dce_child_only_captures_bronze_and_parent_imports_twenty_gaps_per_batch(tmp_path: Path, monkeypatch):
    days = [(date(2026, 8, 1) + timedelta(days=index)).isoformat() for index in range(23)]
    remaining = list(days)
    calls = []
    monkeypatch.setattr(updater, "_missing_dce_days", lambda root, dates: list(remaining))

    def capture(command, **kwargs):
        assert Path(command[1]).name == "capture_official_daily_history.py"
        assert "--write" not in command and "--max-days" not in command
        assert command[command.index("--exchange") + 1] == "DCE"
        assert kwargs["timeout"] == 180
        calls.append(("capture", command))
        return SimpleNamespace(returncode=0)

    def import_cached(root, **kwargs):
        assert kwargs["exchanges"] == ("DCE",) and kwargs["retry_failed"] is True
        with pytest.raises(updater.HostPaused, match="只导入"):
            kwargs["session"].request("POST", "http://example.invalid")
        calls.append(("import", kwargs))
        remaining[:] = [day for day in remaining if not kwargs["start_day"] <= day <= kwargs["end_day"]]
        return {"status": "PASS"}

    monkeypatch.setattr(updater.subprocess, "run", capture)
    monkeypatch.setattr(updater, "run_official_futures_backfill", import_cached)
    result = updater._update_dce_browser(tmp_path, days)
    assert result == {"status": "PASS", "pendingDays": 0, "updatedDays": 23, "batches": 2}
    assert [call[0] for call in calls] == ["capture", "import", "capture", "import"]
    assert calls[1][1]["start_day"] == days[0] and calls[1][1]["end_day"] == days[19]
    assert calls[3][1]["start_day"] == days[20] and calls[3][1]["end_day"] == days[-1]


def test_dce_capture_exit_zero_without_new_verified_facts_keeps_gap(tmp_path: Path, monkeypatch):
    days = ["2026-09-30"]
    monkeypatch.setattr(updater, "_missing_dce_days", lambda root, dates: list(days))
    monkeypatch.setattr(updater.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    imports = []
    monkeypatch.setattr(updater, "run_official_futures_backfill", lambda root, **kwargs: imports.append(kwargs) or {"status": "PARTIAL"})
    result = updater._update_dce_browser(tmp_path, days)
    assert result["status"] == "PARTIAL" and result["pendingDays"] == 1
    assert "日行情" in result["error"] and "调试连接" in result["error"]
    assert "排名" not in result["error"]
    assert len(imports) == 1 and isinstance(imports[0]["session"], updater._BronzeOnlySession)


def test_dce_capture_failure_still_imports_available_originals_in_parent(tmp_path: Path, monkeypatch):
    days = ["2026-09-29", "2026-09-30"]
    remaining = list(days)
    monkeypatch.setattr(updater, "_missing_dce_days", lambda root, dates: list(remaining))
    monkeypatch.setattr(updater.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))

    def partial_import(root, **kwargs):
        assert isinstance(kwargs["session"], updater._BronzeOnlySession)
        remaining.remove(days[0])
        return {"status": "PARTIAL"}

    monkeypatch.setattr(updater, "run_official_futures_backfill", partial_import)
    result = updater._update_dce_browser(tmp_path, days)
    assert result["status"] == "PARTIAL" and result["pendingDays"] == 1
