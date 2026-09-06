from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from market_monitor.cli import main
from market_monitor.external_market import VIX_LOCAL_SERIES_PATH, load_vix_series
from market_monitor.external_market_sync import (
    ExternalMarketSyncError,
    build_cboe_vix_document,
    parse_cboe_vix_history_csv,
    sync_cboe_vix_history,
)


_CBOE_CSV = """DATE,OPEN,HIGH,LOW,CLOSE\n09/03/2026,17.1,18.1,16.5,17.2\n09/04/2026,17.3,18.5,17.0,18.4\n"""


def test_cboe_csv_parser_is_strict_and_stably_ordered() -> None:
    assert parse_cboe_vix_history_csv(_CBOE_CSV) == (
        ("2026-09-03", 17.2),
        ("2026-09-04", 18.4),
    )
    with pytest.raises(ExternalMarketSyncError, match="收盘价无效"):
        parse_cboe_vix_history_csv("DATE,CLOSE\n09/03/2026,not-a-number\n")
    with pytest.raises(ExternalMarketSyncError, match="重复交易日"):
        parse_cboe_vix_history_csv("DATE,CLOSE\n09/03/2026,17.2\n09/03/2026,17.3\n")


def test_sync_writes_only_a_complete_provenance_bearing_local_standard_series(tmp_path) -> None:
    summary = sync_cboe_vix_history(
        tmp_path,
        fetch_text=lambda _timeout: _CBOE_CSV,
        now=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc),
    )

    assert summary["status"] == "PASS"
    assert summary["pointCount"] == 2
    path = tmp_path / VIX_LOCAL_SERIES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["sourceStatus"] == "PASS"
    assert payload["asOfDate"] == "2026-09-04"
    assert payload["sourceSha256"]
    assert load_vix_series(tmp_path).status == "ready"


def test_document_hash_binds_the_exact_cboe_payload() -> None:
    first = build_cboe_vix_document(_CBOE_CSV, retrieved_at="2026-09-05T00:00:00+00:00")
    second = build_cboe_vix_document(_CBOE_CSV.replace("18.4", "18.5"), retrieved_at="2026-09-05T00:00:00+00:00")
    assert first["sourceSha256"] != second["sourceSha256"]


def test_vix_sync_cli_reports_a_controlled_success(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "market_monitor.cli.sync_cboe_vix_history",
        lambda data_root, *, timeout_seconds: {
            "status": "PASS",
            "pointCount": 2,
            "asOfDate": "2026-09-04",
            "path": str(data_root / VIX_LOCAL_SERIES_PATH),
            "source": "CBOE VIX_History.csv",
            "sourceUrl": "https://example.test/VIX_History.csv",
            "sourceSha256": "test",
            "retrievedAt": "2026-09-05T00:00:00+00:00",
        },
    )
    assert main(["vix-sync", "--data-root", str(tmp_path), "--timeout-seconds", "5"]) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["status"] == "SUCCESS"
    assert result["exit_code"] == 0


def test_vix_sync_cli_reports_source_validation_failure_without_writing(tmp_path, monkeypatch, capsys) -> None:
    def fail(_data_root, *, timeout_seconds: float) -> None:
        del timeout_seconds
        raise ExternalMarketSyncError("CBOE VIX CSV 缺少 DATE/CLOSE 列")

    monkeypatch.setattr("market_monitor.cli.sync_cboe_vix_history", fail)
    assert main(["vix-sync", "--data-root", str(tmp_path)]) == 2
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["exit_code"] == 2
    assert not (tmp_path / VIX_LOCAL_SERIES_PATH).exists()
