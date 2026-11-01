from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.kpl_history_view import read_kpl_history
from market_monitor.kpl_market_probe import HISTORY_URL
from market_monitor.web_app import create_web_app


def _sample(root: Path, rows: list, fetched: str) -> Path:
    raw = json.dumps({"info": rows, "errcode": "0", "ttag": 0.01}).encode()
    digest = sha256(raw).hexdigest()
    folder = root / "bronze" / "kpl_market_history" / digest
    (folder / "observations").mkdir(parents=True, exist_ok=True)
    (folder / "response.raw").write_bytes(raw)
    (folder / "observations" / (fetched.replace(":", "-") + ".json")).write_text(json.dumps({
        "schema": "kpl-market-bronze-v1", "sha256": digest, "observedDate": rows[0][-1],
        "fetchedAt": fetched, "httpStatus": 200, "quality": "UNVERIFIED", "token": "private",
        "requestedUrl": HISTORY_URL,
    }))
    return folder


def test_history_dates_gaps_and_intraday(tmp_path: Path) -> None:
    rows = [[72, 6, 70, 13, 49.2857, 69, "2026-10-28"],
            [59, 26, 51, 8, 29.4872, 23, "2026-10-26"]]
    _sample(tmp_path, rows, "2026-10-28T06:00:00+00:00")
    result = read_kpl_history(tmp_path, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert result["available"] and result["latestPhase"] == "INTRADAY"
    assert result["points"][1] == {"day": "2026-10-27", "limitUp": None, "limitDown": None,
        "naturalLimitUp": None, "everLimitDown": None, "brokenRatePct": None, "brokenCount": None,
        "observedAt": None}
    assert result["points"][0]["limitUp"] == 59
    assert "private" not in json.dumps(result)


def test_newest_valid_observation_and_tamper(tmp_path: Path) -> None:
    old = [[0, 0, 0, 0, 0, 0, "2026-10-26"]]
    latest = [[42, 22, 39, 7, 40.5797, 28, "2026-10-27"]]
    _sample(tmp_path, old, "2026-10-26T08:00:00+00:00")
    folder = _sample(tmp_path, latest, "2026-10-27T08:00:00+00:00")
    (folder / "observations" / "broken.json").write_text("bad")
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    assert read_kpl_history(tmp_path, now=now)["points"][-1]["limitUp"] == 42
    (folder / "response.raw").write_bytes(b"bad")
    fallback = read_kpl_history(tmp_path, now=now)
    assert fallback["points"][0]["limitUp"] == 0
    assert fallback["latestPhase"] == "AFTER_CLOSE"


def test_readonly_api_absence(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 50000))
    result = client.get("/api/data/kpl-market/history")
    assert result.status_code == 200 and result.json()["points"] == []


def test_overlapping_captures_keep_older_dates_and_update_same_day(tmp_path: Path) -> None:
    _sample(tmp_path, [[42, 22, 39, 7, 40.5797, 28, "2026-10-27"],
                       [59, 26, 51, 8, 29.4872, 23, "2026-10-26"]], "2026-10-27T08:00:00+00:00")
    _sample(tmp_path, [[72, 6, 70, 13, 49.2857, 69, "2026-10-28"],
                       [43, 22, 39, 7, 40.5797, 28, "2026-10-27"]], "2026-10-28T08:00:00+00:00")
    result = read_kpl_history(tmp_path, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert [p["day"] for p in result["points"]] == ["2026-10-26", "2026-10-27", "2026-10-28"]
    assert result["points"][0]["limitUp"] == 59
    assert result["points"][1]["limitUp"] == 43
    assert result["points"][0]["observedAt"] == "2026-10-27T08:00:00+00:00"
    assert result["points"][1]["observedAt"] == "2026-10-28T08:00:00+00:00"


def test_observation_cannot_claim_a_later_trading_day(tmp_path: Path) -> None:
    _sample(tmp_path, [[72, 6, 70, 13, 49.2857, 69, "2026-10-28"]], "2026-10-27T08:00:00+00:00")
    assert not read_kpl_history(tmp_path, now=datetime(2026, 10, 29, tzinfo=timezone.utc))["available"]


def test_known_closed_day_is_not_rendered(tmp_path: Path) -> None:
    _sample(tmp_path, [[1, 1, 1, 1, 1, 1, "2026-10-25"],
                       [42, 22, 39, 7, 40.5797, 28, "2026-10-23"]], "2026-10-25T08:00:00+00:00")
    result = read_kpl_history(tmp_path, now=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert [point["day"] for point in result["points"]] == ["2026-10-23"]
