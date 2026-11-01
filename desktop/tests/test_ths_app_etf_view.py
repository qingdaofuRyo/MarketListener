from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.ths_app_etf_view import read_ths_app_etf
from market_monitor.web_app import create_web_app


def _snapshot(root: Path, **overrides):
    folder = root / "bronze" / "ths_app_etf"
    folder.mkdir(parents=True)
    at = "2026-10-30T00:00:00+00:00"
    data = {"schema": "ths-app-etf-observation-v1", "scope": "APP_TRACKING_INDEX_FILTER",
            "quality": "UNVERIFIED", "count": 1,
            "capturedPages": [{"pageBegin": 0, "responseSha256": "a" * 64, "observedAt": at}],
            "rows": [{"code": "20:515880", "name": "样本ETF", "price": 1.0,
                      "changePct": 0.0, "turnover": 0, "observedAt": at, "token": "synthetic-secret"}]}
    data.update(overrides)
    (folder / "snapshot.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_local_observation_is_source_isolated_and_preserves_real_zero(tmp_path: Path):
    _snapshot(tmp_path)
    result = read_ths_app_etf(tmp_path, now=datetime(2026, 10, 30, 1, tzinfo=timezone.utc))
    assert result["status"] == "UNVERIFIED" and result["quoteTime"] is None
    assert result["rows"][0]["changePct"] == 0 and result["rows"][0]["turnover"] == 0
    assert "synthetic-secret" not in json.dumps(result)


def test_duplicate_and_future_capture_are_not_published(tmp_path: Path):
    data = _snapshot(tmp_path)
    data["rows"].append(dict(data["rows"][0]))
    data["count"] = 2
    path = tmp_path / "bronze" / "ths_app_etf" / "snapshot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not read_ths_app_etf(tmp_path, now=datetime(2026, 10, 30, 1, tzinfo=timezone.utc))["available"]
    data["rows"].pop()
    data["count"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not read_ths_app_etf(tmp_path, now=datetime(2026, 10, 29, tzinfo=timezone.utc))["available"]


def test_newest_capture_wins_over_newer_file_mtime(tmp_path: Path):
    data = _snapshot(tmp_path)
    folder = tmp_path / "bronze" / "ths_app_etf"
    recent = folder / "recent.json"
    data["capturedPages"][0]["observedAt"] = "2026-10-30T00:30:00+00:00"
    data["rows"][0]["observedAt"] = "2026-10-30T00:30:00+00:00"
    data["rows"][0]["price"] = 2.0
    recent.write_text(json.dumps(data), encoding="utf-8")
    os.utime(recent, (1, 1))
    result = read_ths_app_etf(tmp_path, now=datetime(2026, 10, 30, 1, tzinfo=timezone.utc))
    assert result["rows"][0]["price"] == 2.0
    assert result["captureEnd"] == "2026-10-30T00:30:00+00:00"


def test_rows_must_match_their_own_capture_page(tmp_path: Path):
    data = _snapshot(tmp_path)
    first_time = data["capturedPages"][0]["observedAt"]
    second_time = "2026-10-30T00:01:00+00:00"
    data["capturedPages"].append({"pageBegin": 40, "responseSha256": "b" * 64,
                                  "observedAt": second_time})
    data["rows"] = [{**data["rows"][0], "code": f"20:{i:06d}"} for i in range(41)]
    data["count"] = len(data["rows"])
    data["rows"][0]["observedAt"] = second_time
    data["rows"][-1]["observedAt"] = first_time
    (tmp_path / "bronze" / "ths_app_etf" / "snapshot.json").write_text(json.dumps(data), encoding="utf-8")
    assert not read_ths_app_etf(tmp_path, now=datetime(2026, 10, 30, 1, tzinfo=timezone.utc))["available"]


def test_status_api_reads_only_local_observation(tmp_path: Path):
    _snapshot(tmp_path)
    client = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 50000))
    response = client.get("/api/data/ths-app-etf/observation")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert "synthetic-secret" not in response.text
