from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_monitor import kpl_auto_refresh as auto


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(auto, "is_verified_a_share_trading_day", lambda day: day.weekday() < 5)
    def live(_root: Path, *, expected_date):
        calls.append(("live", expected_date.isoformat()))
        return {"status": "UNVERIFIED"}
    def history(_root: Path, *, expected_date):
        calls.append(("history", expected_date.isoformat()))
        return {"status": "UNVERIFIED"}
    monkeypatch.setattr(auto, "probe_kpl_market", live)
    monkeypatch.setattr(auto, "probe_kpl_history", history)
    return calls


def _at(hour: int, minute: int, *, day: int = 28) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_market_sessions_and_persistent_limit(tmp_path: Path, probes) -> None:
    assert auto.run_due_kpl_probes(tmp_path, _at(9, 29)) == ["history"]
    assert auto.run_due_kpl_probes(tmp_path, _at(9, 30)) == ["live"]
    assert auto.run_due_kpl_probes(tmp_path, _at(9, 34)) == []
    assert auto.run_due_kpl_probes(tmp_path, _at(9, 35)) == ["live"]
    assert auto.run_due_kpl_probes(tmp_path, _at(11, 31)) == []
    assert auto.run_due_kpl_probes(tmp_path, _at(13, 0)) == ["live"]
    assert auto.run_due_kpl_probes(tmp_path, _at(13, 0, day=31)) == ["history"]
    assert len(probes) == 5


def test_after_close_once_and_retry(tmp_path: Path, probes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto, "probe_kpl_history", lambda *_a, **_kw: {"status": "BLOCKED"})
    assert auto.run_due_kpl_probes(tmp_path, _at(15, 0)) == []
    assert auto.run_due_kpl_probes(tmp_path, _at(15, 30)) == ["live", "history"]
    assert auto.run_due_kpl_probes(tmp_path, _at(16, 29)) == []
    monkeypatch.setattr(auto, "probe_kpl_history", lambda *_a, **_kw: {"status": "UNVERIFIED"})
    assert auto.run_due_kpl_probes(tmp_path, _at(16, 30)) == ["history"]
    assert auto.run_due_kpl_probes(tmp_path, _at(23, 0)) == []


def test_previous_attempt_and_corrupt_state_fail_closed(tmp_path: Path, probes) -> None:
    assert auto.run_due_kpl_probes(tmp_path, _at(14, 59)) == ["live"]
    assert auto.run_due_kpl_probes(tmp_path, _at(15, 0)) == []
    assert auto.run_due_kpl_probes(tmp_path, _at(15, 30)) == ["live", "history"]
    path = tmp_path / "state" / "kpl_auto_schedule.json"
    state = json.loads(path.read_text())
    state["live"]["attemptAt"] = "bad"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        auto.run_due_kpl_probes(tmp_path, _at(14, 0) + timedelta(days=1))


@pytest.mark.parametrize(("when", "expected"), [
    (datetime(2026, 10, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")), "2026-10-23"),
    (datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Asia/Shanghai")), "2026-10-23"),
    (datetime(2026, 10, 24, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")), "2026-10-23"),
    (datetime(2026, 10, 5, 18, tzinfo=ZoneInfo("Asia/Shanghai")), "2026-09-30"),
    (datetime(2026, 10, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")), "2026-10-23"),
])
def test_catch_up_only_verified_previous_history(tmp_path: Path, monkeypatch, when, expected) -> None:
    calls = []
    monkeypatch.setattr(auto, "probe_kpl_history", lambda _root, *, expected_date:
                        calls.append(expected_date.isoformat()) or {"status": "UNVERIFIED"})
    monkeypatch.setattr(auto, "probe_kpl_market", lambda *_a, **_kw: pytest.fail("unexpected live"))
    assert auto.run_due_kpl_probes(tmp_path, when) == ["history"]
    assert calls == [expected]
    # A fresh call after restart does not repeatedly download the same day.
    assert auto.run_due_kpl_probes(tmp_path, when + timedelta(minutes=5)) == []


def test_unknown_calendar_and_stale_retry(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(auto, "probe_kpl_history", lambda _root, *, expected_date:
                        calls.append(expected_date.isoformat()) or {"status": "STALE"})
    assert auto.run_due_kpl_probes(tmp_path, datetime(2027, 1, 2, 9, tzinfo=ZoneInfo("Asia/Shanghai"))) == []
    assert auto.run_due_kpl_probes(tmp_path, datetime(2026, 1, 1, 9, tzinfo=ZoneInfo("Asia/Shanghai"))) == []
    when = datetime(2026, 10, 25, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert auto.run_due_kpl_probes(tmp_path, when) == ["history"]
    assert auto.run_due_kpl_probes(tmp_path, when + timedelta(minutes=29)) == []
    assert auto.run_due_kpl_probes(tmp_path, when + timedelta(minutes=30)) == ["history"]
    assert calls == ["2026-10-23", "2026-10-23"]
