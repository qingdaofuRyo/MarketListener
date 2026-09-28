import json
from types import SimpleNamespace

import pytest

from market_monitor.tdx_download_ui import (
    FINANCIAL, FUTURES, DialogDriver, StopRun, completion_state, main,
    pending_keys, plain, run_terminal, selection_digest, terminal_completion,
)


@pytest.mark.parametrize(('message', 'button', 'seen', 'expected'), [
    ('正在下载日线', '取消下载', False, 'BUSY'),
    ('下载完毕', '开始下载', True, 'UI_DONE_UNVERIFIED'),
    ('下载完成', '开始下载', False, 'UNKNOWN'),
    ('下载完成，但部分失败', '开始下载', True, 'FAILED'),
    ('下载失败', '开始下载', False, 'FAILED'),
    ('', '开始下载', True, 'UNKNOWN'),
    ('已取消', '开始下载', True, 'FAILED'),
    ('下载完成', '取消下载', True, 'BUSY'),
])
def test_completion_needs_explicit_evidence(message, button, seen, expected):
    assert completion_state(message, button, seen) == expected


def test_saved_selection_does_not_depend_on_order():
    assert selection_digest(['CL00W', 'GC00W']) == selection_digest(['GC00W', 'CL00W'])
    with pytest.raises(StopRun, match='RETIRED_00Y'):
        selection_digest(['CL00Y'])


@pytest.mark.parametrize('codes', [[], [''], ['../bad'], ['name with space'], ['GC00Y']])
def test_unreadable_scope_stops(codes):
    with pytest.raises(StopRun):
        selection_digest(codes)


def test_six_jobs_preserve_saved_extended_scope():
    assert len({b.key for b in (*FINANCIAL, *FUTURES)}) == 6
    assert all(b.saved_selection for b in FINANCIAL[2:])
    assert all(b.all_checkbox for b in (*FINANCIAL[:2], *FUTURES))


def test_due_slot_idempotence():
    state = {FINANCIAL[0].key: {'slot': 'today', 'status': 'UI_DONE_UNVERIFIED'}}
    assert pending_keys(FINANCIAL, state, 'today') == list(FINANCIAL[1:])
    assert pending_keys(FINANCIAL, state, 'tomorrow') == list(FINANCIAL)


def test_terminal_completion_requires_every_batch_in_current_slot():
    state = {batch.key: {'slot': 'today', 'status': 'UI_DONE_UNVERIFIED'} for batch in FINANCIAL}
    assert terminal_completion(state, 'today', FINANCIAL) == 'UI_DONE_UNVERIFIED'
    assert terminal_completion(state, 'tomorrow', FINANCIAL) == 'NOT_DOWNLOADED'
    state[FINANCIAL[0].key]['status'] = 'RUNNING_UNKNOWN'
    assert terminal_completion(state, 'today', FINANCIAL) == 'PREVIOUS_RUN_NEEDS_RECONCILIATION'


class FakeDriver:
    def __init__(self, outcome='UI_DONE_UNVERIFIED', busy=False):
        self.calls, self.outcome, self.busy = [], outcome, busy

    def open(self):
        if self.busy:
            raise StopRun('EXISTING_DOWNLOAD_BUSY')
        self.calls.append('open')

    def prepare(self, batch):
        self.calls.append(batch.key)
        return {'selectionHash': 'synthetic-hash'}

    def download(self, timeout):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_busy_never_prepares_or_cancels():
    driver, state = FakeDriver(busy=True), {}
    with pytest.raises(StopRun):
        run_terminal(driver, FINANCIAL, state, 'today', lambda: None, 60)
    assert not state and not driver.calls


def test_each_batch_checkpoint_and_restart_skips_completed():
    state, saved = {}, []
    driver = FakeDriver()
    run_terminal(driver, FINANCIAL, state, 'today', lambda: saved.append(str(state)), 60)
    assert len(saved) == 8
    assert all(s['status'] == 'UI_DONE_UNVERIFIED' for s in state.values())
    second = FakeDriver()
    run_terminal(second, FINANCIAL, state, 'today', lambda: None, 60)
    assert not second.calls


def test_uncertain_download_never_retried_automatically():
    state = {}
    with pytest.raises(StopRun):
        run_terminal(FakeDriver(StopRun('timeout')), FINANCIAL, state, 'today', lambda: None, 60)
    assert state[FINANCIAL[0].key]['status'] == 'RUNNING_UNKNOWN'
    driver = FakeDriver()
    with pytest.raises(StopRun, match='RECONCILIATION'):
        run_terminal(driver, FINANCIAL, state, 'tomorrow', lambda: None, 60)
    assert not driver.calls


def test_failure_stops_remaining_batches():
    state, driver = {}, FakeDriver('FAILED')
    with pytest.raises(StopRun):
        run_terminal(driver, FINANCIAL, state, 'today', lambda: None, 60)
    assert len(state) == 1


def test_extended_period_scopes_must_match():
    class ChangedScope(FakeDriver):
        def prepare(self, batch):
            return {'selectionHash': batch.key}
    state = {}
    with pytest.raises(StopRun, match='SELECTION_MISMATCH'):
        run_terminal(ChangedScope(), FINANCIAL[2:], state, 'today', lambda: None, 60)
    assert 'extended-5m' not in state


def test_tab_names_excludes_window_caption():
    driver = object.__new__(DialogDriver)
    driver.control = lambda _: SimpleNamespace(tab_count=lambda: 2, get_tab_text=lambda i: ['沪深京日线*', '沪深京分钟线'][i])
    assert driver.tab_names() == ['沪深京日线*', '沪深京分钟线']
    assert plain('&沪深京日线 *') == '沪深京日线'


def test_default_plan_never_constructs_windows_driver(monkeypatch, capsys):
    monkeypatch.setattr(DialogDriver, '__init__', lambda *a: pytest.fail('UI invoked'))
    assert main([]) == 0
    assert 'PLAN_ONLY' in capsys.readouterr().out


def test_download_connect_never_launches_missing_terminal(tmp_path, monkeypatch):
    from pywinauto.application import ProcessNotFoundError
    from market_monitor import tdx_download_ui

    exe = tmp_path / 'tdxw.exe'
    exe.write_bytes(b'fixture')

    class FakeApplication:
        def connect(self, **_kwargs):
            raise ProcessNotFoundError('not running')

    driver = object.__new__(DialogDriver)
    driver.exe = exe
    driver.Application = lambda **_kwargs: FakeApplication()
    driver.unlocked = lambda: None
    monkeypatch.setattr(tdx_download_ui.subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('download must not launch'))

    with pytest.raises(StopRun, match='CLIENT_NOT_RUNNING'):
        driver.connect()


def test_launch_only_does_not_download_or_write_checkpoint(tmp_path, monkeypatch, capsys):
    from market_monitor import tdx_download_ui

    actions = []

    class FakeDriver:
        def __init__(self, _path):
            actions.append('driver')

        def start(self):
            actions.append('start')
            return 'LAUNCHED'

    monkeypatch.setattr(tdx_download_ui, 'DialogDriver', FakeDriver)
    assert main(['--launch-only', '--terminal', 'financial', '--state-dir', str(tmp_path)]) == 0
    assert actions == ['driver', 'start']
    assert not (tmp_path / 'latest.json').exists()
    assert json.loads(capsys.readouterr().out)['status'] == 'LAUNCHED'


def test_separate_downloads_never_launch_terminal_and_merge_completion(tmp_path, monkeypatch):
    from market_monitor import tdx_download_ui

    connections = []

    class RecordingDriver:
        def __init__(self, _path):
            self.dialog = None

        def connect(self):
            connections.append('connect')

        def open(self):
            pass

        def prepare(self, _batch):
            return {'selectionHash': 'synthetic-hash'}

        def download(self, _timeout):
            return 'UI_DONE_UNVERIFIED'

    monkeypatch.setattr(tdx_download_ui, 'DialogDriver', RecordingDriver)
    assert main(['--execute', '--terminal', 'financial', '--state-dir', str(tmp_path)]) == 0
    first = json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8'))
    assert first['status'] == 'ATTENTION_REQUIRED'
    assert first['terminals'] == {'financial': 'UI_DONE_UNVERIFIED', 'futures': 'NOT_DOWNLOADED'}
    assert main(['--execute', '--terminal', 'futures', '--state-dir', str(tmp_path)]) == 0
    second = json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8'))
    assert second['status'] == 'UI_DONE_UNVERIFIED'
    assert connections == ['connect', 'connect']


def test_custom_menu_waits_for_exact_download_dialog(monkeypatch, capsys):
    from market_monitor import tdx_download_ui

    driver = object.__new__(DialogDriver)
    main_window = SimpleNamespace(
        window_text=lambda: '通达信期货通 - [行情报价]',
        is_enabled=lambda: True,
        menu_select=lambda _path: (_ for _ in ()).throw(RuntimeError('custom menu')),
    )
    dialog = SimpleNamespace()
    observed = iter([None, None, dialog])
    driver.guard = lambda: None
    driver.windows = lambda: [main_window]
    driver.find_dialog = lambda: next(observed)
    driver.ensure_idle = lambda: None
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _seconds: None)

    driver.open()

    assert driver.dialog is dialog
    assert '手动打开该窗口' in capsys.readouterr().out
