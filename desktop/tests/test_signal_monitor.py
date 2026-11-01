from copy import deepcopy
from contextlib import contextmanager
import threading

import pytest
from fastapi.testclient import TestClient

from market_monitor.signal_monitor import advance_monitor, evaluate_signals, validate_signal_rule
from market_monitor.web_app import create_web_app
from market_monitor.web_api import signals
from market_monitor.web_api.market import _normalize_tdx_instrument


def definition(action='open', direction='long'):
    return {'id': action, 'version': 1, 'displayName': action, 'action': action, 'direction': direction, 'period': '1d', 'enabled': True, 'rule': {'node_type': 'condition', 'left': {'function_id': 'technical.sma', 'version': 1, 'arguments': [{'kind': 'series', 'field': 'close'}, {'kind': 'literal', 'value': 2}]}, 'comparator': 'gt', 'right': {'kind': 'literal', 'value': 10}}}


def point(day, matched=True):
    return {'at': f'2026-01-{day:02d}T00:00:00+00:00', 'matched': matched, 'price': 12}


def test_signal_rules_validate_and_keep_warmup_missing():
    strategy = definition()
    validate_signal_rule(strategy)
    result = evaluate_signals(strategy, [{'bar_open_time': f'2026-01-0{i+1}T00:00:00+00:00', 'close': close} for i, close in enumerate([9, 13, 15])])
    assert [p['matched'] for p in result] == [None, True, True]


def test_open_then_add_reduce_close_and_dedupe():
    state = {}
    assert advance_monitor(state, 'x', [(definition('add'), [point(1)])]) == []
    assert [e['action'] for e in advance_monitor(state, 'x', [(definition(), [point(2)])])] == ['open']
    assert advance_monitor(state, 'x', [(definition(), [point(2)])]) == []
    events = advance_monitor(state, 'x', [(definition('add'), [point(1), point(3)]), (definition('reduce'), [point(3)])])
    assert [e['action'] for e in events] == ['add', 'reduce']
    assert [e['action'] for e in advance_monitor(state, 'x', [(definition('close'), [point(4)]), (definition('add'), [point(4)]), (definition(), [point(4)])])] == ['close']
    assert not state['cycles']['x|long']['active']
    assert advance_monitor(state, 'x', [(definition('add'), [point(5)])]) == []
    assert advance_monitor(state, 'x', [(definition(), [point(4)])]) == []
    assert advance_monitor(state, 'x', [(definition(), [point(6)])])[0]['action'] == 'open'


def test_direction_isolation_and_same_bar_does_not_close_new_cycle():
    state = {}
    short_open = {**definition('open', 'short'), 'id': 'short_open'}
    events = advance_monitor(state, 'x', [(definition(), [point(1)]), (definition('close'), [point(1)]), (short_open, [point(1)])])
    assert [event['action'] for event in events] == ['open', 'open']
    advance_monitor(state, 'x', [({**definition('close', 'short'), 'id': 'short_close'}, [point(2)])])
    assert state['cycles']['x|long']['active']
    assert not state['cycles']['x|short']['active']


def test_future_or_timezone_less_bars_not_accepted():
    state = {}
    future = {**point(1), 'at': '2099-01-01T00:00:00+00:00'}
    assert advance_monitor(state, 'x', [(definition(), [future])]) == []
    with pytest.raises(ValueError):
        evaluate_signals(definition(), [{'bar_open_time': '2026-01-01', 'close': 11}])
    assert evaluate_signals(definition(), [{'bar_open_time': '2026-01-01T00:00:00+00:00', 'close': 11, 'is_partial': True}]) == []


def client(tmp_path):
    return TestClient(create_web_app(tmp_path), client=('127.0.0.1', 1234))


def body(action='open'):
    return {key: value for key, value in definition(action).items() if key not in ('id', 'version')}


def test_api_create_edit_recoverable_delete_and_reject_account_fields(tmp_path):
    api = client(tmp_path)
    assert api.get('/api/signals/definitions').json()['items'] == []
    assert api.post('/api/signals/definitions', json={**body(), 'initialCash': 100}).status_code == 422
    created = api.post('/api/signals/definitions', json=body()).json()
    assert created['version'] == 1
    path = '/api/signals/definitions/' + created['id']
    assert api.put(path, json={**body(), 'version': 0}).status_code == 409
    assert api.put(path, json={**body('close'), 'version': 1}).json()['version'] == 2
    assert api.delete(path).json()['recoverable']
    assert api.get('/api/signals/definitions').json()['items'] == []
    assert api.post(path + '/restore').status_code == 200
    assert len(api.get('/api/signals/definitions').json()['items']) == 1


def test_monitor_persistence_new_bars_and_market_filter(tmp_path, monkeypatch):
    api = client(tmp_path)
    instrument = {'instrumentId': 'CN.SSE.STOCK.600000', 'storageInstrumentId': 'storage', 'name': '测试', 'market': 'CN', 'assetType': 'STOCK', 'exchange': 'SSE', 'symbol': '600000'}
    monkeypatch.setattr(signals, '_logical_instruments', lambda root: {instrument['instrumentId']: instrument})
    bars = [{'bar_open_time': '2026-01-01T00:00:00+00:00', 'close': 12}, {'bar_open_time': '2026-01-02T00:00:00+00:00', 'close': 13}]
    monkeypatch.setattr(signals, '_history_before', lambda *args, **kwargs: (deepcopy(bars), 2, None, False, None, None))
    api.post('/api/signals/definitions', json=body())
    assert api.post('/api/signals/scan', json={'categoryKeys': ['hk-stock']}).json()['scanned'] == 0
    opened = api.post('/api/signals/scan', json={}).json()
    assert len(opened['items']) == 1
    assert len(api.post('/api/signals/scan', json={}).json()['events']) == 1
    assert len(client(tmp_path).get('/api/signals/monitor').json()['items']) == 1
    api.post('/api/signals/definitions', json=body('close'))
    bars.append({'bar_open_time': '2026-01-03T00:00:00+00:00', 'close': 14})
    closed = api.post('/api/signals/scan', json={'monitoringOnly': True, 'categoryKeys': ['hk-stock']}).json()
    assert closed['items'] == []
    assert closed['events'][-1]['action'] == 'close'


def test_futures_index_names_are_source_scoped():
    sample = {'actualSource': '通达信期货通', 'exchange': 'TDX_OPTION_VOLATILITY', 'assetType': 'INDEX', 'seriesKind': 'OPTION_VOLATILITY_INDEX', 'symbol': 'V050C0', 'name': '期货波动率/期权指数 V050C0'}
    assert _normalize_tdx_instrument(sample)['name'] == 'V沪50ETF综合购'
    assert _normalize_tdx_instrument({**sample, 'exchange': 'UNKNOWN'})['name'] == sample['name']


def test_scan_gap_is_reported_without_advancing_cursor(tmp_path, monkeypatch):
    api = client(tmp_path)
    instrument = {'instrumentId': 'CN.SSE.STOCK.600000', 'storageInstrumentId': 'storage', 'market': 'CN', 'assetType': 'STOCK', 'exchange': 'SSE', 'symbol': '600000'}
    monkeypatch.setattr(signals, '_logical_instruments', lambda root: {instrument['instrumentId']: instrument})
    created = api.post('/api/signals/definitions', json=body()).json()
    from datetime import datetime, timedelta, timezone
    bars = [{'bar_open_time': (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)).isoformat(), 'close': 13} for index in range(500)]
    monkeypatch.setattr(signals, '_history_before', lambda *args, **kwargs: (bars, 500, None, True, None, None))
    cursor_key = instrument['instrumentId'] + '|' + created['id'] + '@1'
    state_path = tmp_path / 'strategies' / 'signals' / 'monitor.json'
    signals.write(state_path, {'cursors': {cursor_key: '2023-01-01T00:00:00+00:00'}})
    result = api.post('/api/signals/scan', json={}).json()
    assert '500' in result['issues'][0]['reason']
    assert signals.read(state_path, {})['cursors'][cursor_key] == '2023-01-01T00:00:00+00:00'


def test_scan_rejects_definition_change_before_monitor_commit(tmp_path, monkeypatch):
    api = client(tmp_path)
    instrument = {'instrumentId': 'CN.SSE.STOCK.600000', 'storageInstrumentId': 'storage',
                  'market': 'CN', 'assetType': 'STOCK', 'exchange': 'SSE', 'symbol': '600000'}
    monkeypatch.setattr(signals, '_logical_instruments', lambda _root: {instrument['instrumentId']: instrument})
    api.post('/api/signals/definitions', json=body())
    definitions_path = tmp_path / 'strategies' / 'signals' / 'definitions.json'
    old = signals.read(definitions_path, {})

    def changed_during_scan(*_args, **_kwargs):
        newer = deepcopy(old)
        next(iter(newer.values()))['displayName'] = '并发修改'
        signals.write(definitions_path, newer)
        return ([{'bar_open_time': '2026-01-01T00:00:00+00:00', 'close': 12}], 1, None, False, None, None)

    monkeypatch.setattr(signals, '_history_before', changed_during_scan)
    response = api.post('/api/signals/scan', json={})
    assert response.status_code == 409
    assert '重新扫描' in response.json()['detail']
    assert not (tmp_path / 'strategies' / 'signals' / 'monitor.json').exists()


def test_signal_definition_write_retries_when_profile_backup_is_busy(tmp_path, monkeypatch):
    from market_monitor.profile_gate import ProfileGateBusy

    @contextmanager
    def unavailable(_root):
        raise ProfileGateBusy('synthetic backup in progress')
        yield

    monkeypatch.setattr(signals, 'profile_write_gate', unavailable)
    response = client(tmp_path).post('/api/signals/definitions', json=body())
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '2'
    assert not (tmp_path / 'strategies' / 'signals' / 'definitions.json').exists()


def test_profile_capture_can_run_while_signal_scan_calculates(tmp_path, monkeypatch):
    from market_monitor.profile_gate import profile_read_gate

    api = client(tmp_path)
    instrument = {'instrumentId': 'CN.SSE.STOCK.600000', 'storageInstrumentId': 'storage',
                  'market': 'CN', 'assetType': 'STOCK', 'exchange': 'SSE', 'symbol': '600000'}
    monkeypatch.setattr(signals, '_logical_instruments', lambda _root: {instrument['instrumentId']: instrument})
    api.post('/api/signals/definitions', json=body())
    calculating = threading.Event()
    resume = threading.Event()
    response = []

    def slow_history(*_args, **_kwargs):
        calculating.set()
        assert resume.wait(3)
        return ([{'bar_open_time': '2026-01-01T00:00:00+00:00', 'close': 12}], 1, None, False, None, None)

    monkeypatch.setattr(signals, '_history_before', slow_history)
    worker = threading.Thread(target=lambda: response.append(api.post('/api/signals/scan', json={})))
    worker.start()
    try:
        assert calculating.wait(3)
        with profile_read_gate(tmp_path, timeout=0.1):
            assert not (tmp_path / 'strategies' / 'signals' / 'monitor.json').exists()
    finally:
        resume.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert response and response[0].status_code == 200


def test_signal_scan_rejects_silver_revision_change(tmp_path, monkeypatch):
    from market_monitor.market_data_version import advance_market_data_version

    api = client(tmp_path)
    instrument = {'instrumentId': 'CN.SSE.STOCK.600000', 'storageInstrumentId': 'storage',
                  'market': 'CN', 'assetType': 'STOCK', 'exchange': 'SSE', 'symbol': '600000'}
    monkeypatch.setattr(signals, '_logical_instruments', lambda _root: {instrument['instrumentId']: instrument})
    api.post('/api/signals/definitions', json=body())

    def changed_during_scan(*_args, **_kwargs):
        advance_market_data_version(tmp_path)
        return ([{'bar_open_time': '2026-01-01T00:00:00+00:00', 'close': 12}], 1, None, False, None, None)

    monkeypatch.setattr(signals, '_history_before', changed_during_scan)
    response = api.post('/api/signals/scan', json={})
    assert response.status_code == 409
    assert '行情已更新' in response.json()['detail']
    assert not (tmp_path / 'strategies' / 'signals' / 'monitor.json').exists()
