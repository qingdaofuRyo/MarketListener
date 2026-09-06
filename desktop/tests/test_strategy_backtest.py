from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from market_monitor.builtin_strategies import build_builtin_strategy_definitions
from market_monitor.strategy_backtest import (
    StrategyBacktestError,
    evaluate_rule_series,
    run_strategy_backtest,
)
from market_monitor.strategy_definition import validate_strategy_definition
from market_monitor.strategy_functions import execute_strategy_function


def bars(closes: list[float]) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = []
    for index, close in enumerate(closes):
        opened = closes[index - 1] if index else close
        timestamp = start + timedelta(days=index)
        result.append(
            {
                "bar_open_time": timestamp.isoformat(),
                "bar_close_time": (timestamp + timedelta(hours=8)).isoformat(),
                "open": opened,
                "high": max(opened, close) + 0.5,
                "low": min(opened, close) - 0.5,
                "close": close,
                "volume": 1000 + index,
            }
        )
    return result


def ma_definition(*, direction: str = "long", fill_price: str = "next_open") -> dict:
    definition = next(item for item in build_builtin_strategy_definitions() if item["id"] == "strategy.ma_crossover")
    definition["direction"] = direction
    definition["execution"]["fill_price"] = fill_price
    definition["stop_loss"]["enabled"] = False
    definition["take_profit"]["enabled"] = False
    return definition


def signal_closes() -> list[float]:
    return [100.0] * 8 + [101.0, 103.0, 106.0, 108.0, 107.0, 104.0, 100.0, 96.0, 94.0]


def run(definition: dict, source_bars: list[dict], **kwargs) -> dict:
    parameters = kwargs.pop("parameters", {"fast": 2, "slow": 4})
    return run_strategy_backtest(
        definition,
        source_bars,
        instrument_id="CN.SSE.STOCK.600000",
        data_version="fixture-v1",
        parameters=parameters,
        **kwargs,
    )


def test_builtin_ma_and_donchian_atr_definitions_are_valid_and_versioned() -> None:
    definitions = build_builtin_strategy_definitions()
    assert {item["id"] for item in definitions} == {"strategy.ma_crossover", "strategy.donchian_atr"}
    evidence = {item["id"]: validate_strategy_definition(item) for item in definitions}
    assert {item["id"] for item in evidence["strategy.ma_crossover"]["function_references"]} >= {
        "technical.sma",
        "condition.crossover",
        "condition.crossunder",
    }

    donchian = next(item for item in definitions if item["id"] == "strategy.donchian_atr")
    result = run_strategy_backtest(
        donchian,
        bars([10, 10, 10, 10, 10, 11, 12, 13, 12, 10, 8, 7]),
        instrument_id="CN.SSE.STOCK.600000",
        data_version="fixture-v1",
        parameters={"entry_lookback": 3, "exit_lookback": 2, "atr_lookback": 2},
    )
    assert result["status"] == "ready"
    assert any(item["id"] == "technical.atr" for item in result["function_references"])
    assert result["order_intents"]


def test_historical_atr_v1_remains_exact_when_atr_v2_is_published() -> None:
    high = [11.0, 12.0, 14.0, 15.0]
    low = [9.0, 10.0, 11.0, 12.0]
    close = [10.0, 11.0, 12.0, 13.0]

    historical = execute_strategy_function("technical.atr", high, low, close, 2, version=1)
    current = execute_strategy_function("technical.atr", high, low, close, 2, version=2)

    assert historical == [None, 2.0, 2.5, 2.75]
    assert current == historical


def test_ma_backtest_delays_bar_close_signal_until_next_open_and_has_no_future_leak() -> None:
    definition = ma_definition()
    source = bars(signal_closes())
    entry_full, exit_full = evaluate_rule_series(definition, source, {"fast": 2, "slow": 4})
    for length in range(5, len(source) + 1):
        entry_prefix, exit_prefix = evaluate_rule_series(definition, source[:length], {"fast": 2, "slow": 4})
        assert entry_prefix == entry_full[:length]
        assert exit_prefix == exit_full[:length]

    result = run(definition, source)
    assert result["status"] == "ready"
    assert result["fills"]
    assert all(item["fill_index"] == item["signal_index"] + 1 for item in result["order_intents"])
    assert result["fills"][0]["filled_at"] == source[result["order_intents"][0]["fill_index"]]["bar_open_time"]
    assert all(item["risk_decision"] == "accepted" for item in result["order_intents"])
    assert all(item["adapter_id"] == "backtest-simulation-v1" for item in result["order_intents"])


def test_next_close_fills_at_next_bar_close_not_before_intrabar_stop() -> None:
    definition = ma_definition(fill_price="next_close")
    source = bars(signal_closes())
    result = run(definition, source)
    first = result["fills"][0]
    assert first["filled_at"] == source[first["bar_index"]]["bar_close_time"]
    assert result["order_intents"][0]["fill_index"] == result["order_intents"][0]["signal_index"] + 1


@pytest.mark.parametrize("direction", ["long", "short"])
def test_long_and_short_portfolio_accounting_includes_fees_and_slippage(direction: str) -> None:
    definition = ma_definition(direction=direction)
    definition["backtest"]["commission_rate"] = 0.001
    definition["backtest"]["slippage_rate"] = 0.01
    result = run(definition, bars(signal_closes()))

    assert result["fills"]
    assert all(fill["commission"] > 0 and fill["slippage_cost"] > 0 for fill in result["fills"])
    assert all(trade["direction"] == direction for trade in result["trades"])
    if result["trades"]:
        trade = result["trades"][0]
        expected = trade["gross_pnl"] - trade["commission"]
        assert trade["pnl"] == pytest.approx(expected)


def test_stop_loss_is_conservative_when_stop_and_take_profit_hit_same_bar() -> None:
    definition = ma_definition()
    definition["stop_loss"] = {"enabled": True, "kind": "percent", "value": 5}
    definition["take_profit"] = {"enabled": True, "kind": "percent", "value": 5}
    source = bars(signal_closes()[:12])
    baseline = run(definition, source)
    entry = baseline["fills"][0]
    hit_index = entry["bar_index"] + 1
    source[hit_index]["high"] = entry["price"] * 1.1
    source[hit_index]["low"] = entry["price"] * 0.9
    result = run(definition, source)
    exit_fill = next(fill for fill in result["fills"] if fill["position_effect"] == "close")
    assert exit_fill["reason"] == "stop_loss"


def test_take_profit_scale_out_closes_only_configured_position_ratio_once() -> None:
    source = bars(signal_closes()[:12])
    entry = run(ma_definition(), source)["fills"][0]
    hit_index = entry["bar_index"] + 1
    source[hit_index]["high"] = entry["price"] * 1.1

    definition = ma_definition()
    definition["take_profit"] = {"enabled": True, "kind": "percent", "value": 5}
    definition["scale_out"] = {"enabled": True, "ratio_percent": 40}
    result = run(definition, source)

    scale_out_fill = next(item for item in result["fills"] if item["reason"] == "take_profit_scale_out")
    assert scale_out_fill["position_effect"] == "close"
    assert scale_out_fill["quantity"] == pytest.approx(entry["quantity"] * 0.4)
    assert result["final_state"]["position_quantity"] == pytest.approx(entry["quantity"] * 0.6)
    assert result["final_state"]["scale_out_done"] is True
    assert [item["exit_reason"] for item in result["trades"]] == ["take_profit_scale_out"]
    assert not any(item["reason"] == "take_profit" for item in result["fills"])


def test_empty_invalid_and_unknown_parameter_inputs_are_explicit() -> None:
    definition = ma_definition()
    empty = run(definition, [])
    assert empty["status"] == "unavailable"
    assert empty["unavailable_reason"] == "NO_BARS"

    with pytest.raises(StrategyBacktestError) as caught:
        run(definition, bars([100, 101]), parameters={"unknown": 1})
    assert caught.value.code == "UNKNOWN_PARAMETER"

    invalid = bars([100, 101])
    invalid[1]["low"] = 200
    with pytest.raises(StrategyBacktestError) as caught:
        run(definition, invalid)
    assert caught.value.code == "INVALID_BAR"


def test_repeated_run_is_deterministic_and_partial_state_resumes_exactly() -> None:
    definition = ma_definition()
    source = bars(signal_closes())
    first = run(definition, source)
    second = run(definition, source)
    assert first == second

    partial = run(definition, source, stop_after_index=10)
    assert partial["status"] == "partial"
    resumed = run(definition, source, resume_state=partial["resume_state"])
    assert resumed["status"] == "ready"
    for key in ("run_id", "final_equity", "equity_points", "order_intents", "fills", "trades", "markers"):
        assert resumed[key] == first[key]

    changed = deepcopy(partial["resume_state"])
    changed["data_fingerprint"] = "different"
    with pytest.raises(StrategyBacktestError) as caught:
        run(definition, source, resume_state=changed)
    assert caught.value.code == "RESUME_MISMATCH"


def test_position_limit_rejection_is_recorded_without_a_fill() -> None:
    definition = ma_definition()
    definition["position_sizing"] = {"kind": "equity_percent", "value": 80}
    definition["risk"]["max_position_percent"] = 20
    result = run(definition, bars(signal_closes()))
    assert any(item["risk_decision"] == "rejected" for item in result["order_intents"])
    assert result["fills"] == []
