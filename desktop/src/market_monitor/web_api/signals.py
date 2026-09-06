"""Recoverable signal definitions and explicitly invoked local monitoring scans."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from market_monitor.market_classification import UNCLASSIFIED_CATEGORY, classify_market, matches_market_category
from market_monitor.signal_monitor import advance_monitor, evaluate_signals, validate_signal_rule
from market_monitor.strategy_backtest import StrategyBacktestError
from market_monitor.strategy_definition import StrategyDefinitionError
from .market import _history_before, _logical_instruments, _normalize_future_name, _normalize_tdx_instrument

router = APIRouter(prefix='/api/signals', tags=['signals'])
_lock = RLock()
PERIODS = ('5m', '15m', '30m', '1h', '2h', '1d', '1w', '1mo', '3mo', '1y')


def read(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding='utf-8'))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        temp = stream.name
    os.replace(temp, path)


def directory(request: Request) -> Path:
    return Path(request.app.state.data_root) / 'strategies' / 'signals'


class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    displayName: str = Field(min_length=1, max_length=128)
    description: str = Field(default='', max_length=2000)
    action: Literal['open', 'add', 'reduce', 'close']
    direction: Literal['long', 'short'] = 'long'
    period: Literal['5m', '15m', '30m', '1h', '2h', '1d', '1w', '1mo', '3mo', '1y'] = '1d'
    enabled: bool = True
    rule: dict[str, Any]
    version: int = Field(default=0, ge=0)


class ScanBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategyIds: list[str] = Field(default_factory=list, max_length=100)
    categoryKeys: list[str] = Field(default_factory=list, max_length=100)
    offset: int = Field(default=0, ge=0)
    afterId: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=40, ge=1, le=100)
    monitoringOnly: bool = False


@router.get('/definitions')
def definitions(request: Request) -> dict[str, Any]:
    items = read(directory(request) / 'definitions.json', {})
    return {'items': [{key: value for key, value in item.items() if key != 'deleted'} for item in items.values() if not item.get('deleted')], 'periods': PERIODS}


def save(request: Request, body: DefinitionBody, strategy_id: str | None = None) -> dict[str, Any]:
    document = body.model_dump()
    try:
        validate_signal_rule(document)
    except StrategyDefinitionError as error:
        raise HTTPException(422, detail=error.to_dict()) from error
    with _lock:
        path = directory(request) / 'definitions.json'
        items = read(path, {})
        if strategy_id and (strategy_id not in items or items[strategy_id].get('deleted')):
            raise HTTPException(404, detail='策略不存在')
        old = items.get(strategy_id, {})
        if strategy_id and old['version'] != body.version:
            raise HTTPException(409, detail='策略已被更新，请刷新后编辑')
        identifier = strategy_id or f'signal_{uuid4().hex}'
        document.update(id=identifier, version=old.get('version', 0) + 1)
        items[identifier] = document
        write(path, items)
    return document


@router.post('/definitions', status_code=201)
def create(request: Request, body: DefinitionBody) -> dict[str, Any]:
    return save(request, body)


@router.put('/definitions/{strategy_id}')
def update(strategy_id: str, request: Request, body: DefinitionBody) -> dict[str, Any]:
    return save(request, body, strategy_id)


@router.delete('/definitions/{strategy_id}')
def delete(strategy_id: str, request: Request) -> dict[str, Any]:
    with _lock:
        path = directory(request) / 'definitions.json'
        items = read(path, {})
        if strategy_id not in items:
            raise HTTPException(404, detail='策略不存在')
        items[strategy_id]['deleted'] = True
        write(path, items)
    return {'deleted': True, 'recoverable': True}


@router.post('/definitions/{strategy_id}/restore')
def restore(strategy_id: str, request: Request) -> dict[str, Any]:
    with _lock:
        path = directory(request) / 'definitions.json'
        items = read(path, {})
        if strategy_id not in items:
            raise HTTPException(404, detail='策略不存在')
        items[strategy_id]['deleted'] = False
        write(path, items)
    return items[strategy_id]


def state_payload(request: Request, state: dict[str, Any]) -> dict[str, Any]:
    logical = _logical_instruments(Path(request.app.state.data_root))
    return {'items': [{**_normalize_tdx_instrument(_normalize_future_name(logical.get(item['instrumentId'], {}))), **item} for item in state.get('cycles', {}).values() if item['active']], 'events': state.get('events', [])[-100:]}


@router.get('/monitor')
def monitor(request: Request) -> dict[str, Any]:
    return state_payload(request, read(directory(request) / 'monitor.json', {}))


@router.post('/scan')
def scan(request: Request, body: ScanBody) -> dict[str, Any]:
    root = Path(request.app.state.data_root)
    with _lock:
        strategies = [item for item in read(directory(request) / 'definitions.json', {}).values() if item['enabled'] and not item.get('deleted')]
        state = read(directory(request) / 'monitor.json', {})
        logical = _logical_instruments(root)
        monitored = {cycle['instrumentId'] for cycle in state.get('cycles', {}).values() if cycle['active']}
        universe = sorted(identifier for identifier, item in logical.items() if classify_market(item) != UNCLASSIFIED_CATEGORY and (identifier in monitored or (not body.monitoringOnly and (not body.categoryKeys or any(matches_market_category(item, category) for category in body.categoryKeys)))))
        remaining = [identifier for identifier in universe if not body.afterId or identifier > body.afterId]
        batch = remaining[body.offset:body.offset + body.limit]
        issues = []
        for identifier in batch:
            evaluated = []
            windows: dict[str, list[dict[str, Any]]] = {}
            for strategy in strategies:
                if strategy['action'] == 'open' and (body.monitoringOnly or (body.strategyIds and strategy['id'] not in body.strategyIds)):
                    continue
                if strategy['action'] == 'open' and body.categoryKeys and not any(matches_market_category(logical[identifier], category) for category in body.categoryKeys):
                    continue
                try:
                    if strategy['period'] not in windows:
                        windows[strategy['period']] = _history_before(root, logical[identifier], strategy['period'], None, 500)[0]
                    bars = windows[strategy['period']]
                    points = evaluate_signals(strategy, bars)
                    cursor = state.get('cursors', {}).get(f"{identifier}|{strategy['id']}@{strategy['version']}")
                    if cursor and len(points) == 500 and cursor < points[0]['at']:
                        issues.append({'instrumentId': identifier, 'strategyId': strategy['id'], 'reason': '监控缺口超过当前 500 根窗口，需补充历史后继续，未跳过信号'})
                        continue
                    if not points or points[-1]['matched'] is None:
                        issues.append({'instrumentId': identifier, 'strategyId': strategy['id'], 'reason': '本地行情字段缺失或历史暖机不足'})
                    evaluated.append((strategy, points))
                except (StrategyBacktestError, ValueError, KeyError) as error:
                    issues.append({'instrumentId': identifier, 'strategyId': strategy['id'], 'reason': str(error)})
            advance_monitor(state, identifier, evaluated)
        write(directory(request) / 'monitor.json', state)
        has_more = body.offset + len(batch) < len(remaining)
        return {**state_payload(request, state), 'scanned': len(batch), 'total': len(universe), 'nextOffset': body.offset + len(batch) if has_more else None, 'nextAfterId': batch[-1] if has_more else None, 'issues': issues[:100], 'issueCount': len(issues)}
