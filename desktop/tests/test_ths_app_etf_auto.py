from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor import ths_app_etf_auto as auto
from market_monitor.web_app import create_web_app


def test_schedule_limits_polls_and_does_not_repeat_after_close(tmp_path: Path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "ths_app_etf_settings.json").write_text('{"schema":1,"enabled":true}', encoding="utf-8")
    credential = tmp_path / "protected"
    credential.write_bytes(b"synthetic-protected")
    monkeypatch.setattr(auto, "credential_path", lambda: credential)
    monkeypatch.setattr(auto, "load_session", lambda: {"synthetic": True})
    monkeypatch.setattr(auto, "is_verified_a_share_trading_day", lambda _: True)
    calls = []
    def fetch(_root, _config):
        calls.append(1)
        return {"status": "UNVERIFIED", "count": 2, "captureEnd": "2026-10-30T06:00:01+00:00"}
    now = datetime(2026, 10, 30, 6, tzinfo=timezone.utc)
    assert auto.run_due(tmp_path, now, fetch=fetch)["status"] == "UNVERIFIED"
    assert auto.run_due(tmp_path, now + timedelta(minutes=14), fetch=fetch)["status"] == "NOT_DUE"
    assert auto.run_due(tmp_path, now + timedelta(minutes=15), fetch=fetch)["status"] == "UNVERIFIED"
    close = datetime(2026, 10, 30, 7, 30, tzinfo=timezone.utc)
    assert auto.run_due(tmp_path, close, fetch=fetch)["status"] == "UNVERIFIED"
    assert auto.run_due(tmp_path, close + timedelta(hours=1), fetch=fetch)["status"] == "NOT_DUE"
    assert len(calls) == 3


def test_expired_session_stops_even_force_until_new_credentials(tmp_path: Path, monkeypatch):
    credential = tmp_path / "protected"
    credential.write_bytes(b"synthetic-old")
    monkeypatch.setattr(auto, "credential_path", lambda: credential)
    monkeypatch.setattr(auto, "load_session", lambda: {"synthetic": True})
    now = datetime(2026, 10, 30, 6, tzinfo=timezone.utc)
    calls = []
    def rejected(_root, _config):
        calls.append(1)
        return {"status": "AUTH_EXPIRED"}
    assert auto.run_due(tmp_path, now, force=True, fetch=rejected)["status"] == "AUTH_EXPIRED"
    assert auto.run_due(tmp_path, now + timedelta(hours=1), force=True, fetch=rejected)["status"] == "AUTH_EXPIRED"
    assert len(calls) == 1
    credential.write_bytes(b"synthetic-new")
    assert auto.run_due(tmp_path, now + timedelta(hours=2), force=True, fetch=rejected)["status"] == "AUTH_EXPIRED"
    assert len(calls) == 2
    response = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 50000)).get("/api/data/ths-app-etf/status")
    assert response.status_code == 200 and response.json()["status"] == "AUTH_EXPIRED"
    assert "synthetic" not in response.text and "sessionRevision" not in response.text


def test_unverified_calendar_does_not_poll(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auto, "is_verified_a_share_trading_day", lambda _: False)
    assert auto.run_due(tmp_path, datetime(2026, 10, 30, 6, tzinfo=timezone.utc))["status"] == "DISABLED"
    assert not list(tmp_path.rglob("*"))


def test_enabled_but_unverified_calendar_does_not_poll(tmp_path: Path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "ths_app_etf_settings.json").write_text('{"schema":1,"enabled":true}', encoding="utf-8")
    monkeypatch.setattr(auto, "is_verified_a_share_trading_day", lambda _: False)
    assert auto.run_due(tmp_path, datetime(2026, 10, 30, 6, tzinfo=timezone.utc))["status"] == "NOT_DUE"
