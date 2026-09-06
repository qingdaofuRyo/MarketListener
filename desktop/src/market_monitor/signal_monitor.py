"""Local observation signals; deliberately no account, order or fill capabilities."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from market_monitor.builtin_strategies import build_builtin_strategy_definitions
from market_monitor.strategy_backtest import evaluate_rule_series
from market_monitor.strategy_definition import validate_strategy_definition


def rule_adapter(strategy: dict[str, Any]) -> dict[str, Any]:
    """Reuse only the typed Rule AST validator/evaluator, never backtest execution."""
    definition = deepcopy(build_builtin_strategy_definitions()[0])
    definition.update(parameters={}, entry_rules=strategy['rule'], exit_rules=strategy['rule'])
    return definition


def validate_signal_rule(strategy: dict[str, Any]) -> None:
    validate_strategy_definition(rule_adapter(strategy), validate_assets=False)


def normalized_time(value: str) -> str:
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('行情时间缺少时区')
    return dt.astimezone(timezone.utc).isoformat()


def evaluate_signals(strategy: dict[str, Any], bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values, _ = evaluate_rule_series(rule_adapter(strategy), bars, {})
    result = []
    for bar, value in zip(bars, values, strict=True):
        if bar.get('is_partial') is True:
            continue
        stamp = str(bar.get('bar_close_time') or bar.get('bar_open_time') or '')
        result.append({'at': normalized_time(stamp), 'matched': value, 'price': bar.get('close'), 'barAt': str(bar.get('bar_open_time') or '')})
    return result


def advance_monitor(
    state: dict[str, Any], instrument_id: str, evaluated: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Consume each strategy/bar once; later bars only after opening; close wins ties."""
    cursors = state.setdefault('cursors', {})
    cycles = state.setdefault('cycles', {})
    events: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for strategy, points in evaluated:
        if not points:
            continue
        key = f"{instrument_id}|{strategy['id']}@{strategy['version']}"
        completed = [point for point in points if point['at'] <= now]
        if not completed:
            continue
        previous = cursors.get(key)
        candidates = [point for point in completed if point['at'] > previous] if previous else completed[-1:]
        for point in candidates:
            if point['matched'] is True:
                pending.append({**point, 'strategyId': strategy['id'], 'strategyName': strategy['displayName'], 'action': strategy['action'], 'direction': strategy['direction'], 'period': strategy['period']})
        cursors[key] = max(previous or '', completed[-1]['at'])
    # Process chronological cohorts, not strategy iteration order.
    for stamp in sorted({point['at'] for point in pending}):
        for direction in ('long', 'short'):
            group = [point for point in pending if point['at'] == stamp and point['direction'] == direction]
            key = f'{instrument_id}|{direction}'
            cycle = cycles.get(key)
            if cycle and cycle['active']:
                if stamp <= cycle['openedAt']:
                    continue
                closes = [point for point in group if point['action'] == 'close']
                selected = closes[:1] or [point for point in group if point['action'] in ('add', 'reduce')]
                if closes:
                    cycle.update(active=False, closedAt=stamp)
            else:
                selected = [point for point in group if point['action'] == 'open' and stamp > (cycle or {}).get('closedAt', '')][:1]
                if selected:
                    cycle = {'instrumentId': instrument_id, 'direction': direction, 'active': True, 'openedAt': stamp, 'openingStrategyId': selected[0]['strategyId']}
                    cycles[key] = cycle
            for point in selected:
                event = {**point, 'instrumentId': instrument_id, 'openedAt': cycle['openedAt']}
                events.append(event)
                cycle['latestSignal'] = event
    state.setdefault('events', []).extend(events)
    state['events'] = state['events'][-2000:]
    return events
