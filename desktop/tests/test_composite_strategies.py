from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from market_monitor.composite_monitor import advance_composite, completed_bars, current_items, peer_groups
from market_monitor.composite_program import TEMPLATE, compile_program, evaluate_part
from market_monitor.web_api import signals
from market_monitor.web_app import create_web_app

SOURCE = '''def attention():
    return {"long": close > 10, "short": close < 5, "cancel": close == 6, "lookback": 1}
def position():
    return {"rewardRisk": 2, "winRate": observed_win_rate, "allocation": 0.5,
            "capitalUsage": 0.5 * margin_rate, "leverage": 0.5}
def timing():
    return {"open": close == 12 or close == 14, "add": close == 13,
            "reduce": close == 15, "close": close == 16}
'''
INSTRUMENT = {"instrumentId": "CN.SSE.STOCK.600000", "market": "CN", "assetType": "STOCK",
              "exchange": "SSE", "symbol": "600000", "name": "测试", "marginRate": 1}


def definition(source=SOURCE, identifier="combo"):
    return {"id": identifier, "displayName": "测试组合", "description": "", "source": source,
            "period": "1d", "enabled": True, "version": 1,
            "dependencies": compile_program(source).dependencies}


def bars(values):
    return [{"bar_open_time": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)).isoformat(),
             "bar_close_time": (datetime(2026, 1, 1, 7, tzinfo=timezone.utc) + timedelta(days=i)).isoformat(),
             "open": value, "high": value + 1, "low": value - 1, "close": value}
            for i, value in enumerate(values)]


def test_lifecycle_watch_open_add_reduce_close_reopen_unwatch_and_no_repeated_bars():
    state, strategy = {}, definition()
    values = [9, 11]
    advance_composite(state, strategy, INSTRUMENT, bars(values))
    assert [e["action"] for e in state["events"]] == ["watch"]
    for value in [12, 13, 15, 16, 14, 6, 13]:
        values.append(value)
        advance_composite(state, strategy, INSTRUMENT, bars(values), allow_attention=value != 13 or len(values) < 8)
    assert [e["action"] for e in state["events"]] == ["watch", "open", "add", "reduce", "close", "open", "unwatch"]
    saved = deepcopy(state)
    advance_composite(state, strategy, INSTRUMENT, bars(values))
    assert saved == state
    item = next(iter(state["observations"].values()))
    assert not item["watched"] and not item["positionOpen"] and item["position"] is None
    assert item["closedRounds"] == 1 and item["winningRounds"] == 1
    values.append(3)
    advance_composite(state, strategy, INSTRUMENT, bars(values))
    assert current_items(state, [strategy])[0]["direction"] == "short"


def test_unwatched_and_cancelled_never_evaluate_position_or_timing(monkeypatch):
    from market_monitor import composite_monitor as module
    original, calls = module.evaluate_part, []
    def capture(program, part, history, context):
        calls.append(part)
        return original(program, part, history, context)
    monkeypatch.setattr(module, "evaluate_part", capture)
    state, strategy = {}, definition()
    advance_composite(state, strategy, INSTRUMENT, bars([8, 9]))
    assert calls == ["attention"]
    advance_composite(state, strategy, INSTRUMENT, bars([8, 9, 11]))
    calls.clear()
    advance_composite(state, strategy, INSTRUMENT, bars([8, 9, 11, 6]))
    assert calls == ["attention"]


def test_close_priority_and_no_close_on_open_bar():
    source = SOURCE[:SOURCE.index("def timing")]+'''def timing():
    return {"open": True, "add": True, "reduce": True, "close": True}
'''
    strategy, state = definition(source), {}
    advance_composite(state, strategy, INSTRUMENT, bars([9, 12]))
    assert [event["action"] for event in state["events"]] == ["watch", "open"]
    advance_composite(state, strategy, INSTRUMENT, bars([9, 12, 13]))
    assert [event["action"] for event in state["events"]] == ["watch", "open", "close"]
    advance_composite(state, strategy, INSTRUMENT, bars([9, 12, 13, 14]))
    assert state["events"][-1]["action"] == "open"


def test_missing_margin_and_win_samples_stay_missing_and_part_errors_are_atomic():
    strategy, state = definition(), {}
    advance_composite(state, strategy, {**INSTRUMENT, "marginRate": None}, bars([9, 12]))
    position = current_items(state, [strategy])[0]["position"]
    assert position["winRate"] is None and position["capitalUsage"] is None and position["allocation"] == 0.5
    snapshot = deepcopy(state)
    bad = definition(SOURCE.replace('"allocation": 0.5', '"allocation": 2'))
    with pytest.raises(ValueError, match="allocation"):
        advance_composite(state, bad, INSTRUMENT, bars([9, 12, 13]))
    assert snapshot == state


def test_versions_isolate_and_peer_comparison_requires_exact_dates():
    state, strategy = {}, definition()
    advance_composite(state, strategy, INSTRUMENT, bars([9, 12]))
    advance_composite(state, strategy, {**INSTRUMENT, "instrumentId": "other"}, bars([10, 14]))
    items = peer_groups(current_items(state, [strategy]))
    assert len(items) == 2 and all(item["peerCount"] == 1 for item in items)
    assert items[0]["changePct"] != items[1]["changePct"]
    assert current_items(state, [{**strategy, "version": 2}]) == []
    items[1]["asOf"] = "different"
    assert all(item["peerCount"] == 0 for item in peer_groups(items))


@pytest.mark.parametrize("source", [
    "import os\n"+SOURCE, SOURCE.replace("close > 10", "__import__('os')"),
    SOURCE.replace("close > 10", "close[-1] > 0"), SOURCE.replace("close > 10", "close.__class__"),
    SOURCE.replace("close > 10", "2 ** 100000000"),
    SOURCE.replace("close > 10", "fn('technical.sma', 99, close, 5)"),
    SOURCE.replace("close > 10", "fn('technical.sma', 1, close)"),
    SOURCE.replace("def timing", "def missing"),
])
def test_reject_unsafe_or_incomplete_program(source):
    with pytest.raises(ValueError):
        compile_program(source)


def test_template_real_function_calls_and_missing_warmup():
    program = compile_program(TEMPLATE)
    result = evaluate_part(program, "attention", bars([8, 9]), {"direction": 0})
    assert result["long"] is None
    result = evaluate_part(program, "attention", bars(list(range(20, 50))), {"direction": 0})
    assert result["long"] is True and result["short"] is False
    assert all(item["hash"] and item["version"] for item in program.dependencies)


def test_partial_future_gap_and_timezone_are_not_silently_consumed():
    now = datetime.now(timezone.utc)
    assert completed_bars([{"bar_open_time": (now-timedelta(minutes=1)).isoformat(), "close": 12}], "5m") == []
    assert completed_bars([{"bar_open_time": (now+timedelta(days=1)).isoformat(), "close": 12}], "1d") == []
    assert len(completed_bars([{"bar_open_time": "2025-01-01T00:00:00+08:00", "close": 12}], "1d")) == 1
    with pytest.raises(ValueError, match="时区"):
        completed_bars([{"bar_open_time": "2026-01-01", "close": 12}])
    strategy = definition()
    key = "combo@1|" + INSTRUMENT["instrumentId"]
    state = {"observations": {key: {"cursor": "2020-01-01T00:00:00+00:00"}}}
    history = bars([12] * 500)
    # Move to historic dates; filtering one partial record must not hide raw window truncation.
    for i, bar in enumerate(history):
        bar["bar_open_time"] = (datetime(2023, 1, 1, tzinfo=timezone.utc)+timedelta(days=i)).isoformat()
        bar["bar_close_time"] = bar["bar_open_time"]
    history[-1]["is_partial"] = True
    with pytest.raises(ValueError, match="500"):
        advance_composite(state, strategy, INSTRUMENT, history)
    assert state["observations"][key]["cursor"].startswith("2020")


def test_unknown_daily_close_is_conservative_and_zero_allocation_cannot_open(monkeypatch):
    from market_monitor import composite_monitor as module
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 17, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", Clock)
    # A source timestamp at midnight is not an overseas session-close proof.
    daily = {"bar_open_time": "2026-09-16T00:00:00+08:00", "close": 12}
    assert completed_bars([daily]) == []
    assert completed_bars([{**daily, "bar_close_time": daily["bar_open_time"]}]) == []
    assert len(completed_bars([{**daily, "bar_close_time": "2026-09-16T21:00:00+00:00"}])) == 1
    state = {}
    advance_composite(state, definition(SOURCE.replace('"allocation": 0.5', '"allocation": 0')), INSTRUMENT, bars([9, 12]))
    assert [event["action"] for event in state["events"]] == ["watch"]


def test_api_python_edit_scan_restore_legacy_and_persistence(tmp_path, monkeypatch):
    api = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 1234))
    monkeypatch.setattr(signals, "_logical_instruments", lambda _: {INSTRUMENT["instrumentId"]: INSTRUMENT})
    history = bars([9, 12])
    monkeypatch.setattr(signals, "_history_before", lambda *args: (deepcopy(history), len(history), None, False, None, None))
    request = {"displayName": "组合", "source": SOURCE, "enabled": True}
    response = api.post("/api/composites/definitions", json=request)
    assert response.status_code == 201, response.text
    item = response.json()
    path = "/api/composites/definitions/" + item["id"]
    assert api.put(path, json={**request, "version": 0}).status_code == 409
    assert api.post("/api/composites/definitions", json={**request, "source": "def attention():\n return {}"}).status_code == 422
    assert api.post("/api/composites/scan", json={"categoryKeys": ["hk-stock"]}).json()["scanned"] == 0
    scanned = api.post("/api/composites/scan", json={}).json()
    assert len(scanned["items"]) == 1 and scanned["items"][0]["positionOpen"]
    assert api.get("/api/composites/monitor").json()["items"] == scanned["items"]
    assert len(api.post("/api/composites/scan", json={}).json()["events"]) == 2
    assert not (tmp_path / "strategies/signals/definitions.json").exists()
    assert api.delete(path).json()["recoverable"]
    assert api.get("/api/composites/monitor").json()["items"] == []
    restored = api.post(path+"/restore").json()
    assert not restored["enabled"]
    updated = api.put(path, json={**request, "version": 1}).json()
    assert updated["version"] == 2
    assert (tmp_path / "strategies/composites/versions" / f"{item['id']}.1.json").exists()
    assert api.get("/api/signals/definitions").json()["items"] == []
    assert TestClient(create_web_app(tmp_path)).post("/api/composites/scan", json={}).status_code == 403


def test_scanning_one_combination_never_starts_another(tmp_path, monkeypatch):
    api = TestClient(create_web_app(tmp_path), client=("127.0.0.1", 1234))
    monkeypatch.setattr(signals, "_logical_instruments", lambda _: {INSTRUMENT["instrumentId"]: INSTRUMENT})
    monkeypatch.setattr(signals, "_history_before", lambda *args: (bars([9, 12]), 2, None, False, None, None))
    first = api.post("/api/composites/definitions", json={"displayName": "A", "source": SOURCE, "enabled": True}).json()
    api.post("/api/composites/definitions", json={"displayName": "B", "source": SOURCE, "enabled": True})
    result = api.post("/api/composites/scan", json={"strategyIds": [first["id"]]}).json()
    assert {item["strategyId"] for item in result["items"]} == {first["id"]}
