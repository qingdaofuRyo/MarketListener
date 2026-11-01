import json
from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path

import pytest

from market_monitor.tdx_download_ui import (
    FINANCIAL, FUTURES, DialogDriver, StopRun, completion_state, main,
    pending_keys, plain, run_terminal, selection_digest, terminal_completion,
    terminal_failure_reason, extended_host_retry_prompt, market_host_retry_prompt,
)


@pytest.mark.parametrize(('message', 'button', 'seen', 'expected'), [
    ('正在下载日线', '取消下载', False, 'BUSY'),
    ('下载完毕', '开始下载', True, 'UI_DONE_UNVERIFIED'),
    ('下载完成', '开始下载', False, 'UNKNOWN'),
    ('下载完成，但部分失败', '开始下载', True, 'FAILED'),
    ('个别文件下载失败，仍在下载', '取消下载', True, 'BUSY'),
    ('下载失败', '开始下载', False, 'FAILED'),
    ('请先连接扩展市场行情主站!', '开始下载', False, 'FAILED'),
    ('请先连接期货市场行情主站!', '开始下载', False, 'FAILED'),
    ('', '开始下载', True, 'UNKNOWN'),
    ('已取消', '开始下载', True, 'FAILED'),
    ('下载完成', '取消下载', True, 'BUSY'),
])
def test_completion_needs_explicit_evidence(message, button, seen, expected):
    assert completion_state(message, button, seen) == expected


@pytest.mark.parametrize(('message', 'expected'), [
    ('下载被取消.', 'TERMINAL_DOWNLOAD_CANCELLED'),
    ('行情断开，下载失败', 'MARKET_CONNECTION_DROPPED'),
    ('连接失败，下载中断', 'MARKET_CONNECTION_DROPPED'),
    ('请先连接扩展市场行情主站!', 'EXTENDED_MARKET_HOST_DISCONNECTED'),
    ('请先连接期货市场行情主站!', 'FUTURES_MARKET_HOST_DISCONNECTED'),
    ('未知失败，账号12345', 'TERMINAL_DOWNLOAD_FAILED'),
])
def test_terminal_failure_reason_allowlists_ui_text(message, expected):
    assert terminal_failure_reason(message) == expected


def test_only_explicit_extended_connection_prompts_allow_one_retry():
    assert extended_host_retry_prompt('请先连接扩展市场行情主站!')
    assert extended_host_retry_prompt('连接失败，下载中断')
    assert extended_host_retry_prompt('行情断开，下载失败')
    assert not extended_host_retry_prompt('下载被取消')
    assert not extended_host_retry_prompt('未知失败')
    assert market_host_retry_prompt('请先连接期货市场行情主站!')
    assert not market_host_retry_prompt('下载被取消')


def _checkbox_driver(children, selected=lambda: 0, enabled=lambda: True):
    driver = object.__new__(DialogDriver)
    driver.guard = lambda: None
    driver.tab_names = lambda: ['扩展市场行情日线', '扩展市场行情分钟线']
    driver.control = lambda identifier: SimpleNamespace(get_selected_tab=selected)
    driver.dialog = SimpleNamespace(children=children, is_visible=lambda: True, is_enabled=enabled)
    return driver


def test_foreground_wait_requires_quiet_and_checks_desktop(monkeypatch):
    from market_monitor import tdx_download_ui as ui

    checks = []
    ages = iter([1.0, 9.9, 10.0])
    monkeypatch.setattr(ui.time, 'sleep', lambda _seconds: None)
    ui._wait_for_input_quiet(lambda: checks.append('check'), idle_seconds=lambda: next(ages))
    assert len(checks) == 4  # including a final check immediately before input


def test_foreground_wait_timeout_cancellation_and_locked_desktop(monkeypatch):
    from market_monitor import tdx_download_ui as ui

    ticks = iter([0, 60])
    monkeypatch.setattr(ui.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(StopRun, match='WAITING_FOR_INPUT_IDLE'):
        ui._wait_for_input_quiet(lambda: None, idle_seconds=lambda: 0)
    monkeypatch.setattr(ui.time, 'monotonic', lambda: 0)
    with pytest.raises(StopRun, match='INPUT_IDLE_WAIT_CANCELLED'):
        ui._wait_for_input_quiet(lambda: None, idle_seconds=lambda: 0, cancelled=lambda: True)
    with pytest.raises(StopRun, match='DESKTOP_LOCKED'):
        ui._wait_for_input_quiet(lambda: (_ for _ in ()).throw(StopRun('DESKTOP_LOCKED')),
                                 idle_seconds=lambda: 100)


def test_busy_download_monitor_does_not_wait_for_input_quiet(monkeypatch):
    from market_monitor import tdx_download_ui as ui

    driver = object.__new__(DialogDriver)
    events = iter([('正在下载', '取消下载'), ('下载完毕', '开始下载')])
    driver.status = lambda: next(events)
    monkeypatch.setattr(ui.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(ui, '_wait_for_input_quiet', lambda *_a, **_kw: pytest.fail('must not block BUSY'))
    assert driver.wait_download(60) == 'UI_DONE_UNVERIFIED'


def _checkbox(label='日线数据'):
    return SimpleNamespace(is_visible=lambda: True, is_enabled=lambda: True,
                           window_text=lambda: label, get_check_state=lambda: 1)


def test_checkbox_waits_for_missing_control_on_same_verified_tab(monkeypatch):
    from market_monitor import tdx_download_ui

    attempts = iter([[], [_checkbox()]])
    driver = _checkbox_driver(lambda: next(attempts))
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _seconds: None)
    driver.checkbox('日线数据', True, expected_tab='扩展市场行情日线')


def test_checkbox_refuses_tab_change_during_missing_control_wait(monkeypatch):
    from market_monitor import tdx_download_ui

    selected = iter([0, 1])
    driver = _checkbox_driver(lambda: [], selected=lambda: next(selected))
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _seconds: None)
    with pytest.raises(StopRun, match='CHECKBOX_TAB_CHANGED'):
        driver.checkbox('日线数据', True, expected_tab='扩展市场行情日线')


def test_checkbox_does_not_retry_ambiguous_controls(capsys):
    driver = _checkbox_driver(lambda: [_checkbox(), _checkbox()])
    with pytest.raises(StopRun, match='CHECKBOX_NOT_FOUND'):
        driver.checkbox('日线数据', True, expected_tab='扩展市场行情日线')
    assert '匹配数=2' in capsys.readouterr().out


def test_checkbox_diagnostics_do_not_echo_unrecognized_vendor_text(capsys):
    label = '敏感内容synthetic'
    driver = _checkbox_driver(lambda: [_checkbox(label), _checkbox(label)])
    with pytest.raises(StopRun, match='CHECKBOX_NOT_FOUND'):
        driver.checkbox(label, True, expected_tab='扩展市场行情日线')
    message = capsys.readouterr().out
    assert label not in message and '未识别控件' in message


def test_checkbox_stops_on_modal_or_missing_control_timeout(monkeypatch, capsys):
    from market_monitor import tdx_download_ui

    driver = _checkbox_driver(lambda: pytest.fail('must not inspect blocked dialog'), enabled=lambda: False)
    with pytest.raises(StopRun, match='CHECKBOX_DIALOG_NOT_READY'):
        driver.checkbox('日线数据', True, expected_tab='扩展市场行情日线')
    driver = _checkbox_driver(lambda: [])
    ticks = iter([0.0, 2.1])
    monkeypatch.setattr(tdx_download_ui.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(StopRun, match='CHECKBOX_NOT_FOUND'):
        driver.checkbox('日线数据', True, expected_tab='扩展市场行情日线')
    assert '匹配数=0' in capsys.readouterr().out


def test_wait_download_records_sanitized_cancel_reason():
    driver = object.__new__(DialogDriver)
    driver.status = lambda: ('下载被取消.', '开始下载')
    assert driver.wait_download(1) == 'FAILED'
    assert driver.last_failure_reason == 'TERMINAL_DOWNLOAD_CANCELLED'


def test_wait_download_stops_when_connected_terminal_exits(monkeypatch):
    from market_monitor import tdx_download_ui

    driver = object.__new__(DialogDriver)
    driver.app = SimpleNamespace(process=4242)
    driver.connected_start_token = 'windows-filetime:synthetic'
    driver.status = lambda: pytest.fail('exited terminal must not be polled')
    monkeypatch.setattr(tdx_download_ui, 'process_start_token', lambda _pid: None)
    with pytest.raises(StopRun, match='TERMINAL_EXITED_UNVERIFIED'):
        driver.wait_download(60)


@pytest.mark.parametrize(('prompt', 'reason'), [
    ('请先连接扩展市场行情主站!', 'EXTENDED_MARKET_HOST_DISCONNECTED'),
    ('请先连接期货市场行情主站!', 'FUTURES_MARKET_HOST_DISCONNECTED'),
])
def test_begin_download_classifies_explicit_host_prompt(prompt, reason):
    driver = object.__new__(DialogDriver)
    driver.ensure_idle = lambda: None
    driver.guard = lambda: None
    driver.control = lambda _identifier: SimpleNamespace(click=lambda: None)
    driver.status = lambda: (prompt, '开始下载')
    driver.connection_retries_remaining = 1
    with pytest.raises(StopRun, match=reason):
        driver.begin_download()
    assert driver.last_failure_reason == reason


@pytest.mark.parametrize('prompt', ['请先连接扩展市场行情主站!', '连接失败，下载中断',
                                   '请先连接期货市场行情主站!'])
def test_begin_download_retries_extended_host_prompt_once(monkeypatch, prompt):
    from market_monitor import tdx_download_ui

    clicks = []
    statuses = iter([
        (prompt, '开始下载'),
        (prompt, '开始下载'),
        ('正在下载扩展市场', '取消下载'),
    ])
    driver = object.__new__(DialogDriver)
    driver.ensure_idle = lambda: None
    driver.guard = lambda: None
    driver.control = lambda _identifier: SimpleNamespace(click=lambda: clicks.append(True))
    driver.status = lambda: next(statuses)
    driver.connection_retries_remaining = 1
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _seconds: None)
    driver.begin_download()
    assert clicks == [True, True]
    assert driver.last_failure_reason is None


def test_begin_download_does_not_retry_other_terminal_failure():
    clicks = []
    driver = object.__new__(DialogDriver)
    driver.ensure_idle = lambda: None
    driver.guard = lambda: None
    driver.control = lambda _identifier: SimpleNamespace(click=lambda: clicks.append(True))
    driver.status = lambda: ('下载被取消', '开始下载')
    with pytest.raises(StopRun, match='TERMINAL_DOWNLOAD_CANCELLED'):
        driver.begin_download()
    assert clicks == [True]


def test_futures_stability_wait_checks_identity_and_selections(monkeypatch):
    from market_monitor import tdx_download_ui

    driver = object.__new__(DialogDriver)
    driver.app = SimpleNamespace(process=4242)
    driver.dialog = SimpleNamespace(handle=20, process_id=lambda: 4242,
                                    class_name=lambda: '#32770', is_visible=lambda: True,
                                    is_enabled=lambda: True)
    checks = []
    driver.ensure_connected_process = lambda: checks.append('identity')
    driver.guard = lambda: None
    driver.find_dialog = lambda: driver.dialog
    driver.acknowledge_host_initializing = lambda: False
    driver.ensure_idle = lambda: None
    driver.tab_names = lambda: ['期货市场行情日线*', '期货市场行情分钟线*']
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _: None)
    driver.wait_futures_window_stable(FUTURES)
    assert len(checks) == 12
    connecting = iter([False, False] + [True] * 12)
    driver.dialog.is_enabled = lambda: next(connecting)
    checks.clear()
    driver.wait_futures_window_stable(FUTURES)
    assert len(checks) == 14
    driver.dialog.is_enabled = lambda: False
    with pytest.raises(StopRun, match='WAITING_FOR_MARKET_CONNECTION'):
        driver.wait_futures_window_stable(FUTURES)
    driver.dialog.is_enabled = lambda: True
    driver.tab_names = lambda: ['期货市场行情日线*', '期货市场行情分钟线']
    with pytest.raises(StopRun, match='UNEXPECTED_SELECTED_BATCHES'):
        driver.wait_futures_window_stable(FUTURES)
    driver.find_dialog = lambda: SimpleNamespace(handle=21, process_id=lambda: 4242,
        class_name=lambda: '#32770', is_visible=lambda: True)
    with pytest.raises(StopRun, match='DOWNLOAD_DIALOG_CHANGED'):
        driver.wait_futures_window_stable(FUTURES)


def test_futures_retry_does_not_ignore_new_failure(monkeypatch):
    from market_monitor import tdx_download_ui

    clicks = []
    driver = object.__new__(DialogDriver)
    driver.ensure_idle = lambda: None
    driver.guard = lambda: None
    driver.control = lambda _identifier: SimpleNamespace(click=lambda: clicks.append(True))
    messages = iter([('请先连接期货市场行情主站!', '开始下载'), ('下载被取消', '开始下载')])
    driver.status = lambda: next(messages)
    driver.connection_retries_remaining = 1
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _: None)
    with pytest.raises(StopRun, match='TERMINAL_DOWNLOAD_CANCELLED'):
        driver.begin_download()
    assert clicks == [True]


def test_wait_download_marks_connection_interruption_retryable():
    driver = object.__new__(DialogDriver)
    driver.status = lambda: ('连接失败，下载中断', '开始下载')
    assert driver.wait_download(1) == 'FAILED'
    assert driver.last_failure_reason == 'MARKET_CONNECTION_DROPPED'
    assert driver.last_retryable_connection_prompt is True


def test_wait_download_classifies_known_host_initializing_modal():
    driver = object.__new__(DialogDriver)
    driver.dialog = SimpleNamespace(is_enabled=lambda: False)
    driver.acknowledge_host_initializing = lambda: True
    driver.status = lambda: pytest.fail('disabled dialog must not be treated as download progress')
    assert driver.wait_download(1) == 'FAILED'
    assert driver.last_failure_reason == 'MARKET_HOST_INITIALIZING'
    assert driver.last_retryable_connection_prompt is False


def test_begin_download_stops_on_known_host_initializing_modal():
    driver = object.__new__(DialogDriver)
    driver.dialog = SimpleNamespace(is_enabled=lambda: False)
    driver.acknowledge_host_initializing = lambda: True
    driver.ensure_idle = lambda: None
    driver.guard = lambda: None
    driver.control = lambda _identifier: SimpleNamespace(click=lambda: None)
    driver.status = lambda: pytest.fail('known modal should stop before reading disabled dialog')
    with pytest.raises(StopRun, match='MARKET_HOST_INITIALIZING'):
        driver.begin_download()
    assert driver.last_failure_reason == 'MARKET_HOST_INITIALIZING'


def test_reconnect_extended_host_clicks_other_verified_tab():
    selected, clicks = [3], []
    rectangle = SimpleNamespace(left=40, right=120, top=2, bottom=24)

    def click_tab(*, coords):
        clicks.append(coords)
        selected[0] = 4

    tabs = SimpleNamespace(get_selected_tab=lambda: selected[0],
                           get_tab_rect=lambda index: rectangle if index == 4 else None,
                           click_input=click_tab)
    focus = []
    driver = object.__new__(DialogDriver)
    driver.guard = lambda: None
    driver.status = lambda: ('请先连接扩展市场行情主站!', '开始下载')
    driver.tab_names = lambda: ['沪深京日线', '沪深京分钟线', '沪深京分时图',
                                '扩展市场行情日线*', '扩展市场行情分钟线*']
    driver.control = lambda _identifier: tabs
    driver.dialog = SimpleNamespace(set_focus=lambda: focus.append(True), is_enabled=lambda: True)
    driver.unlocked = lambda: None
    assert driver.reconnect_extended_host() is True
    assert focus == [True]
    assert clicks == [(80, 13)]
    assert selected == [4]


def test_reconnect_futures_host_clicks_other_verified_tab():
    selected, clicks = [0], []
    rectangle = SimpleNamespace(left=80, right=160, top=2, bottom=24)

    def click_tab(*, coords):
        clicks.append(coords)
        selected[0] = 1

    tabs = SimpleNamespace(get_selected_tab=lambda: selected[0],
                           get_tab_rect=lambda index: rectangle if index == 1 else None,
                           click_input=click_tab)
    driver = object.__new__(DialogDriver)
    driver.guard = lambda: None
    driver.status = lambda: ('请先连接期货市场行情主站!', '开始下载')
    driver.tab_names = lambda: ['期货市场行情日线*', '期货市场行情分钟线*', '期货市场分时图',
                                '扩展市场行情日线', '扩展市场行情分钟线']
    driver.control = lambda _identifier: tabs
    driver.dialog = SimpleNamespace(set_focus=lambda: None, is_enabled=lambda: True)
    driver.unlocked = lambda: None
    assert driver.reconnect_futures_host() is True
    assert clicks == [(120, 13)]
    assert selected == [1]


def test_host_initializing_notice_acknowledges_only_exact_vendor_message():
    clicks = []
    message = ('行情连接被主站断开，\r\n原因：当前主站可能正在初始化今天的数据。'
               '系统几分钟后会自动重新进入，请稍等。')
    notice = SimpleNamespace(class_name=lambda: 'Static', control_id=lambda: 1726,
                             window_text=lambda: message)
    button = SimpleNamespace(class_name=lambda: 'Button', control_id=lambda: 1,
                             window_text=lambda: '确定', is_enabled=lambda: True,
                             click=lambda: clicks.append(True))
    modal = SimpleNamespace(class_name=lambda: '#32770', is_enabled=lambda: True,
                            process_id=lambda: 77, children=lambda: [notice, button])
    driver = object.__new__(DialogDriver)
    driver.app = SimpleNamespace(process=77)
    driver.windows = lambda: [modal]
    assert driver.acknowledge_host_initializing() is True
    assert clicks == [True]
    notice.window_text = lambda: '其他账户提示'
    assert driver.acknowledge_host_initializing() is False
    assert clicks == [True]


def test_open_stops_after_known_host_initializing_notice_without_menu_click():
    driver = object.__new__(DialogDriver)
    driver.guard = lambda: None
    driver.acknowledge_host_initializing = lambda: True
    driver.find_dialog = lambda: pytest.fail('must not open a download dialog while host initializes')
    with pytest.raises(StopRun, match='MARKET_HOST_INITIALIZING'):
        driver.open()


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
        self.download_count = 0
        self.download_timeouts = []

    def open(self):
        if self.busy:
            raise StopRun('EXISTING_DOWNLOAD_BUSY')
        self.calls.append('open')

    def prepare(self, batch):
        self.calls.append(batch.key)
        return {'selectionHash': 'synthetic-hash'}

    def prepare_many(self, batches):
        self.calls.append('combined:' + ','.join(batch.key for batch in batches))
        return {batch.key: {'selectionHash': 'synthetic-hash'} for batch in batches}

    def download(self, timeout):
        self.download_count += 1
        self.download_timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_busy_never_prepares_or_cancels():
    driver, state = FakeDriver(busy=True), {}
    with pytest.raises(StopRun):
        run_terminal(driver, FINANCIAL, state, 'today', lambda: None, 60)
    assert not state and not driver.calls


def test_financial_cn_then_extended_use_two_clicks_and_independent_checkpoints():
    state, saved = {}, []
    driver = FakeDriver()
    run_terminal(driver, FINANCIAL, state, 'today', lambda: saved.append(deepcopy(state)), 60)
    assert driver.calls == ['open', 'combined:cn-day,cn-5m',
                            'combined:extended-day,extended-5m']
    assert driver.download_count == 2
    assert driver.download_timeouts == [120, 120]
    assert len(saved) == 4
    assert {row['status'] for row in saved[0].values()} == {'RUNNING_UNKNOWN'}
    assert {row['status'] for row in saved[1].values()} == {'UI_DONE_UNVERIFIED'}
    assert {row['status'] for row in saved[2].values()} == {'UI_DONE_UNVERIFIED', 'RUNNING_UNKNOWN'}
    assert {row['status'] for row in saved[3].values()} == {'UI_DONE_UNVERIFIED'}
    assert all(s['status'] == 'UI_DONE_UNVERIFIED' for s in state.values())
    assert state['cn-day']['finishedAt'] == state['cn-5m']['finishedAt']
    assert state['extended-day']['finishedAt'] == state['extended-5m']['finishedAt']
    second = FakeDriver()
    run_terminal(second, FINANCIAL, state, 'today', lambda: None, 60)
    assert not second.calls


def test_financial_extended_failure_preserves_completed_cn_stage():
    class SecondStageFails(FakeDriver):
        def download(self, timeout):
            self.download_count += 1
            self.download_timeouts.append(timeout)
            return 'UI_DONE_UNVERIFIED' if self.download_count == 1 else 'FAILED'

    driver, state = SecondStageFails(), {}
    with pytest.raises(StopRun, match='BATCH_GROUP_FAILED'):
        run_terminal(driver, FINANCIAL, state, 'today', lambda: None, 60)
    assert driver.download_count == 2
    assert [state[key]['status'] for key in ('cn-day', 'cn-5m')] == [
        'UI_DONE_UNVERIFIED', 'UI_DONE_UNVERIFIED',
    ]
    assert [state[key]['status'] for key in ('extended-day', 'extended-5m')] == [
        'FAILED', 'FAILED',
    ]


@pytest.mark.parametrize('batches', [FINANCIAL, FUTURES])
def test_uncertain_combined_download_never_retried_automatically(batches):
    state, saved = {}, []
    with pytest.raises(StopRun):
        run_terminal(FakeDriver(StopRun('timeout')), batches, state, 'today',
                     lambda: saved.append(deepcopy(state)), 60)
    assert len(saved) == 1
    assert {row['status'] for row in saved[0].values()} == {'RUNNING_UNKNOWN'}
    first_phase = FINANCIAL[:2] if batches == FINANCIAL else FUTURES
    assert set(state) == {batch.key for batch in first_phase}
    assert {row['status'] for row in state.values()} == {'RUNNING_UNKNOWN'}
    driver = FakeDriver()
    with pytest.raises(StopRun, match='RECONCILIATION'):
        run_terminal(driver, batches, state, 'tomorrow', lambda: None, 60)
    assert not driver.calls


@pytest.mark.parametrize('batches', [FINANCIAL, FUTURES])
def test_combined_failure_marks_every_selected_batch_failed(batches):
    state, driver, saved = {}, FakeDriver('FAILED'), []
    with pytest.raises(StopRun, match='BATCH_GROUP_FAILED'):
        run_terminal(driver, batches, state, 'today', lambda: saved.append(deepcopy(state)), 60)
    assert len(saved) == 2
    assert {row['status'] for row in saved[0].values()} == {'RUNNING_UNKNOWN'}
    assert {row['status'] for row in saved[1].values()} == {'FAILED'}
    first_phase = FINANCIAL[:2] if batches == FINANCIAL else FUTURES
    assert set(state) == {batch.key for batch in first_phase}
    assert {row['status'] for row in state.values()} == {'FAILED'}
    assert driver.download_count == 1
    assert driver.download_timeouts == [120]


def test_terminal_cancel_persists_only_allowlisted_reason():
    class Cancelled(FakeDriver):
        last_failure_reason = 'TERMINAL_DOWNLOAD_CANCELLED'

    state = {}
    with pytest.raises(StopRun, match='TERMINAL_DOWNLOAD_CANCELLED'):
        run_terminal(Cancelled('FAILED'), FINANCIAL[2:], state, 'today', lambda: None, 60)
    assert {row['failureReason'] for row in state.values()} == {'TERMINAL_DOWNLOAD_CANCELLED'}
    assert {row['status'] for row in state.values()} == {'FAILED'}

    class Unsafe(FakeDriver):
        last_failure_reason = 'account-secret-untrusted-text'

    with pytest.raises(StopRun, match='BATCH_GROUP_FAILED'):
        run_terminal(Unsafe('FAILED'), FINANCIAL[2:], {}, 'today', lambda: None, 60)


@pytest.mark.parametrize(('batches', 'reason'), [
    (FINANCIAL[2:], 'EXTENDED_MARKET_HOST_DISCONNECTED'),
    (FUTURES, 'FUTURES_MARKET_HOST_DISCONNECTED'),
])
def test_explicit_start_failure_marks_batch_failed(batches, reason):
    class HostDisconnected(FakeDriver):
        def begin_download(self):
            raise StopRun(reason)

        def wait_download(self, _timeout):
            pytest.fail('explicit start failure must not wait for download')

    state = {}
    with pytest.raises(StopRun, match=reason):
        run_terminal(HostDisconnected(), batches, state, 'today', lambda: None, 60)
    assert {row['status'] for row in state.values()} == {'FAILED'}
    assert {row['failureReason'] for row in state.values()} == {reason}


def test_extended_reconnect_rechecks_saved_selection_before_start():
    class Reconnected(FakeDriver):
        def reconnect_extended_host(self):
            self.calls.append('reconnect')
            return True

    driver = Reconnected()
    run_terminal(driver, FINANCIAL[2:], {}, 'today', lambda: None, 60)
    assert driver.calls == ['open', 'combined:extended-day,extended-5m',
                            'reconnect', 'combined:extended-day,extended-5m']
    assert driver.download_count == 1


def test_futures_reconnect_rechecks_selected_batches_before_start():
    class Reconnected(FakeDriver):
        def reconnect_futures_host(self):
            self.calls.append('reconnect')
            return True

    driver = Reconnected()
    run_terminal(driver, FUTURES, {}, 'today', lambda: None, 60)
    assert driver.calls == ['open', 'combined:future-day,future-5m',
                            'reconnect', 'combined:future-day,future-5m']
    assert driver.download_count == 1


@pytest.mark.parametrize('batches', [FINANCIAL[2:], FUTURES])
def test_market_connection_interruption_restarts_once(batches):
    class Interrupted(FakeDriver):
        def __init__(self):
            super().__init__()
            self.begins = 0
            self.waits = 0

        def begin_download(self):
            self.begins += 1

        def wait_download(self, _timeout):
            self.waits += 1
            self.last_retryable_connection_prompt = self.waits == 1
            self.last_failure_reason = 'MARKET_CONNECTION_DROPPED' if self.waits == 1 else None
            return 'FAILED' if self.waits == 1 else 'UI_DONE_UNVERIFIED'

    driver, state = Interrupted(), {}
    run_terminal(driver, batches, state, 'today', lambda: None, 60)
    assert driver.begins == 2
    assert driver.waits == 2
    assert {row['status'] for row in state.values()} == {'UI_DONE_UNVERIFIED'}


@pytest.mark.parametrize('batches', [FINANCIAL[2:], FUTURES])
def test_market_connection_interruption_fails_after_single_restart(batches):
    class RepeatedInterruption(FakeDriver):
        def __init__(self):
            super().__init__()
            self.begins = 0

        def begin_download(self):
            self.begins += 1

        def wait_download(self, _timeout):
            self.last_retryable_connection_prompt = True
            self.last_failure_reason = 'MARKET_CONNECTION_DROPPED'
            return 'FAILED'

    driver, state = RepeatedInterruption(), {}
    with pytest.raises(StopRun, match='MARKET_CONNECTION_DROPPED'):
        run_terminal(driver, batches, state, 'today', lambda: None, 60)
    assert driver.begins == 2
    assert {row['status'] for row in state.values()} == {'FAILED'}


def test_futures_two_tabs_use_one_click_and_two_checkpoints():
    state, saved = {}, []
    driver = FakeDriver()
    run_terminal(driver, FUTURES, state, 'today', lambda: saved.append(deepcopy(state)), 60)
    assert driver.calls == ['open', 'combined:future-day,future-5m']
    assert driver.download_count == 1
    assert driver.download_timeouts == [120]
    assert len(saved) == 2
    assert {row['status'] for row in saved[0].values()} == {'RUNNING_UNKNOWN'}
    assert {row['status'] for row in saved[1].values()} == {'UI_DONE_UNVERIFIED'}
    assert {row['status'] for row in state.values()} == {'UI_DONE_UNVERIFIED'}
    assert state['future-day']['finishedAt'] == state['future-5m']['finishedAt']


def test_skip_current_is_distinct_from_ui_completion_and_never_replays():
    slot = '2026-10-25T23:00:00+08:00'
    state = {
        'cn-day': {'slot': slot, 'status': 'SKIPPED_RECENT_RAW', 'reason': 'RAW_MAJORITY_RECENT_SILVER_UNVERIFIED'},
        'cn-5m': {'slot': slot, 'status': 'SKIPPED_RECENT_RAW', 'reason': 'RAW_MAJORITY_RECENT_SILVER_UNVERIFIED'},
        'extended-day': {'slot': slot, 'status': 'UI_DONE_UNVERIFIED'},
        'extended-5m': {'slot': slot, 'status': 'UI_DONE_UNVERIFIED'},
    }
    assert not pending_keys(FINANCIAL, state, slot)
    assert terminal_completion(state, slot, FINANCIAL) == 'COMPLETE_WITH_SKIPS'
    assert terminal_completion(state, slot, FINANCIAL[:2]) == 'NO_GUI_RAW_RECENT'


def test_cli_skip_mainland_batches_downloads_only_extended_without_fake_ui_status(
    tmp_path: Path, monkeypatch,
):
    from market_monitor import tdx_download_ui
    from market_monitor import tdx_gui_auto_refresh

    calls: list[str] = []

    class RecordingDriver:
        def __init__(self, _path):
            self.dialog = None
        def connect(self): calls.append('connect')
        def open(self): calls.append('open')
        def prepare_many(self, batches):
            calls.append(','.join(batch.key for batch in batches))
            return {batch.key: {'selectionHash': 'same'} for batch in batches}
        def download(self, _timeout): return 'UI_DONE_UNVERIFIED'

    monkeypatch.setattr(tdx_download_ui, 'DialogDriver', RecordingDriver)
    monkeypatch.setattr(tdx_gui_auto_refresh, 'financial_cn_coverage',
                        lambda *_args: 'RAW_MAJORITY_RECENT_SILVER_UNVERIFIED')
    code = main(['--execute', '--terminal', 'financial', '--state-dir', str(tmp_path),
                 '--skip-batch', 'cn-day:RAW_MAJORITY_RECENT_SILVER_UNVERIFIED',
                 '--skip-batch', 'cn-5m:RAW_MAJORITY_RECENT_SILVER_UNVERIFIED'])
    assert code == 0
    state = json.loads((tmp_path / 'checkpoints.json').read_text(encoding='utf-8'))['financial']
    latest = json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8'))
    assert state['cn-day']['status'] == state['cn-5m']['status'] == 'SKIPPED_RECENT_RAW'
    assert state['extended-day']['status'] == state['extended-5m']['status'] == 'UI_DONE_UNVERIFIED'
    assert latest['terminals']['financial'] == 'COMPLETE_WITH_SKIPS'
    assert latest['status'] == 'ATTENTION_REQUIRED'  # Other terminal was not run.
    assert calls == ['connect', 'open', 'extended-day,extended-5m']


def test_cli_refuses_skip_without_current_source_evidence(tmp_path: Path, monkeypatch):
    from market_monitor import tdx_download_ui, tdx_gui_auto_refresh

    monkeypatch.setattr(tdx_gui_auto_refresh, 'financial_cn_coverage', lambda *_args: None)
    monkeypatch.setattr(tdx_download_ui, 'DialogDriver',
                        lambda *_args: pytest.fail('terminal must not open'))
    assert main(['--execute', '--terminal', 'financial', '--state-dir', str(tmp_path),
                 '--skip-batch', 'cn-day:RAW_MAJORITY_RECENT_SILVER_UNVERIFIED']) == 2
    assert not (tmp_path / 'checkpoints.json').exists()


@pytest.mark.parametrize('batches', [FINANCIAL, FUTURES])
def test_only_one_pending_batch_uses_single_selection(batches):
    state = {batch.key: {'slot': 'today', 'status': 'UI_DONE_UNVERIFIED'} for batch in batches[:-1]}
    if batches == FINANCIAL:
        state['extended-day']['minuteComparableHash'] = 'synthetic-hash'
    driver = FakeDriver()
    run_terminal(driver, batches, state, 'today', lambda: None, 60)
    assert driver.calls == ['open', batches[-1].key]
    assert driver.download_count == 1
    assert {row['status'] for row in state.values()} == {'UI_DONE_UNVERIFIED'}


def test_extended_period_scopes_must_match():
    class ChangedScope(FakeDriver):
        def prepare_many(self, batches):
            self.calls.append('combined')
            return {batch.key: {'selectionHash': batch.key if batch.key.startswith('extended-')
                                else 'synthetic-hash'} for batch in batches}
    state, driver, saved = {}, ChangedScope(), []
    with pytest.raises(StopRun, match='SELECTION_MISMATCH'):
        run_terminal(driver, FINANCIAL, state, 'today', lambda: saved.append(deepcopy(state)), 60)
    assert set(state) == {'cn-day', 'cn-5m'}
    assert {row['status'] for row in state.values()} == {'UI_DONE_UNVERIFIED'}
    assert len(saved) == 2
    assert driver.download_count == 1


def test_known_day_only_extended_code_does_not_block_other_minute_downloads():
    class DayOnlyScope(FakeDriver):
        def prepare_many(self, batches):
            self.calls.append('combined')
            evidence = {batch.key: {'selectionHash': 'synthetic-hash'} for batch in batches}
            if any(batch.key == 'extended-day' for batch in batches):
                evidence['extended-day'] = {'selectionHash': 'all-with-ZR00W',
                                            'minuteComparableHash': 'minute-available',
                                            'knownDayOnlyExcludedFromMinute': ['ZR00W']}
                evidence['extended-5m'] = {'selectionHash': 'minute-available'}
            return evidence

    state = {}
    run_terminal(DayOnlyScope(), FINANCIAL, state, 'today', lambda: None, 60)
    assert state['extended-day']['knownDayOnlyExcludedFromMinute'] == ['ZR00W']
    assert state['extended-5m']['status'] == 'UI_DONE_UNVERIFIED'


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

        def prepare_many(self, batches):
            return {batch.key: {'selectionHash': 'synthetic-hash'} for batch in batches}

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


def test_owned_process_closes_only_after_all_batches_complete(tmp_path: Path, monkeypatch):
    from market_monitor import tdx_download_ui

    class Window:
        def __init__(self, title: str, kind: str, handle: int):
            self.title, self.kind, self.closed = title, kind, False
            self.handle = handle

        def process_id(self): return 4242
        def class_name(self): return self.kind
        def window_text(self): return self.title
        def is_visible(self): return True
        def is_enabled(self): return True
        def children(self): return []
        def close(self): self.closed = True

    main_window = Window('模拟终端 - [行情]', 'TdxW_MainFrame_Class', 1)
    dialog = Window('盘后数据下载', '#32770', 2)
    driver = object.__new__(DialogDriver)
    driver.app = SimpleNamespace(process=4242)
    driver.exe = (tmp_path / 'tdxw.exe').resolve()
    driver.dialog = dialog
    driver.windows = lambda: [main_window, dialog]
    driver.ensure_idle = lambda: None
    monkeypatch.setattr(tdx_download_ui, '_process_image_path', lambda _pid: driver.exe)
    monkeypatch.setattr(tdx_download_ui, 'process_start_token',
                        lambda pid: 'windows-filetime:synthetic' if pid == 4242 else None)
    exits = iter([False, True])
    monkeypatch.setattr(tdx_download_ui, '_wait_process_exit', lambda _pid, _seconds: next(exits))
    slot = '2026-10-24T23:00:00+08:00'
    state = {batch.key: {'slot': slot, 'status': 'UI_DONE_UNVERIFIED'} for batch in FUTURES}
    assert driver.close_owned(4242, state, slot, FUTURES,
                              owned_token='windows-filetime:synthetic') == 'CLOSED'
    assert dialog.closed and main_window.closed

    main_window.closed = dialog.closed = False
    with pytest.raises(StopRun, match='IDENTITY'):
        driver.close_owned(99, state, slot, FUTURES,
                           owned_token='windows-filetime:synthetic')
    assert not main_window.closed and not dialog.closed
    state[FUTURES[0].key]['status'] = 'RUNNING_UNKNOWN'
    with pytest.raises(StopRun, match='INCOMPLETE'):
        driver.close_owned(4242, state, slot, FUTURES,
                           owned_token='windows-filetime:synthetic')
    assert not main_window.closed and not dialog.closed

    state[FUTURES[0].key]['status'] = 'UI_DONE_UNVERIFIED'
    other_modal = Window('交易确认', '#32770', 3)
    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _: None)
    ticks = iter([0, 6])
    monkeypatch.setattr(tdx_download_ui.time, 'monotonic', lambda: next(ticks))
    driver.windows = lambda: [main_window, dialog, other_modal]
    with pytest.raises(StopRun, match='OTHER_WINDOW_OPEN'):
        driver.close_owned(4242, state, slot, FUTURES,
                           owned_token='windows-filetime:synthetic')
    assert not main_window.closed and not dialog.closed and not other_modal.closed

    # An extra window that vanishes on its own may have been a transient
    # finishing screen. Recheck the identity and close only after it is gone.
    ticks = iter([0, 1])
    windows = iter([[main_window, dialog, other_modal], [main_window, dialog]])
    driver.windows = lambda: next(windows)
    driver.confirm_owned_exit = lambda *_args: False
    exits = iter([False, True])
    assert driver.close_owned(4242, state, slot, FUTURES,
                              owned_token='windows-filetime:synthetic') == 'CLOSED'
    assert main_window.closed and dialog.closed and not other_modal.closed


def test_completed_batches_retry_only_owned_close(tmp_path: Path, monkeypatch, capsys):
    from datetime import datetime
    from market_monitor import tdx_download_ui, tdx_process_ownership

    slot = tdx_download_ui.latest_slot(datetime.now(tdx_download_ui.BEIJING))
    state = {'financial': {batch.key: {'slot': slot, 'status': 'UI_DONE_UNVERIFIED'}
                           for batch in FINANCIAL}}
    (tmp_path / 'checkpoints.json').write_text(json.dumps(state), encoding='utf-8')
    events = []

    class Driver:
        def __init__(self, path):
            events.append('construct')
            self.exe = path
            self.app = SimpleNamespace(process=4242)
        def connect(self, *, auto_login_saved=False):
            assert not auto_login_saved
            events.append('connect')
        def find_dialog(self):
            events.append('find')
            return None
        def close_owned(self, pid, state, slot, batches, *, owned_token):
            assert pid == 4242 and owned_token == 'windows-filetime:12345'
            events.append('close')
            return 'CLOSED'

    monkeypatch.setattr(tdx_download_ui, 'DialogDriver', Driver)
    monkeypatch.setattr(tdx_process_ownership, 'load_auto_owned', lambda *_args: (4242, 'windows-filetime:12345'))
    args = ['--execute', '--terminal', 'financial', '--state-dir', str(tmp_path),
            '--owned-pid', '4242', '--owned-token', 'windows-filetime:12345']
    assert main(args) == 0
    assert events == ['construct', 'connect', 'find', 'close']
    assert json.loads(capsys.readouterr().out)['ownedProcessClose'] == 'CLOSED'

    events.clear()
    monkeypatch.setattr(tdx_process_ownership, 'load_auto_owned', lambda *_args: None)
    assert main(args) == 2
    assert events == []
    assert json.loads(capsys.readouterr().out)['terminals']['financial'] == 'OWNED_PROCESS_RECORD_MISMATCH'


def test_replaced_download_dialog_requires_busy_again_before_accepting_completion(monkeypatch):
    from market_monitor import tdx_download_ui

    class ElementNotFoundError(Exception):
        pass

    monkeypatch.setattr(tdx_download_ui.time, 'sleep', lambda _seconds: None)
    driver = object.__new__(DialogDriver)
    driver.find_dialog = lambda: SimpleNamespace(is_enabled=lambda: True)
    observations = iter([ElementNotFoundError(), ('下载完毕', '开始下载')])

    def status():
        value = next(observations)
        if isinstance(value, Exception):
            raise value
        return value

    driver.status = status
    with pytest.raises(StopRun, match='REATTACH_COMPLETION_UNVERIFIED'):
        driver.wait_download(5)


def test_owned_exit_confirmation_clicks_only_exact_same_process_dialog(monkeypatch):
    from market_monitor import tdx_download_ui

    clicked = []

    def child(kind, identifier, label):
        return SimpleNamespace(class_name=lambda: kind, control_id=lambda: identifier,
                               window_text=lambda: label, click=lambda: clicked.append(identifier))

    children = [child('Static', 1726, '您确定退出本系统吗?请您选择:'),
                child('Button', 961, '退出'), child('Button', 962, '重新登录'),
                child('Button', 1, '取消')]
    modal = SimpleNamespace(process_id=lambda: 4242, class_name=lambda: '#32770',
                            is_visible=lambda: True, is_enabled=lambda: True,
                            window_text=lambda: '退出确认', children=lambda: children)
    driver = object.__new__(DialogDriver)
    driver.windows = lambda: [modal]
    monkeypatch.setattr(tdx_download_ui, 'process_start_token',
                        lambda pid: 'windows-filetime:synthetic' if pid == 4242 else None)
    assert driver.confirm_owned_exit(4242, "windows-filetime:synthetic") is True
    assert clicked == [961]
    clicked.clear()
    with pytest.raises(StopRun, match='IDENTITY'):
        driver.confirm_owned_exit(9999, "windows-filetime:synthetic")
    children[1] = child('Button', 961, '未知按钮')
    with pytest.raises(StopRun, match='LAYOUT_CHANGED'):
        driver.confirm_owned_exit(4242, "windows-filetime:synthetic")
    assert clicked == []


def test_financial_exit_confirmation_requires_exact_three_buttons(monkeypatch):
    from market_monitor import tdx_download_ui

    clicked = []

    def button(identifier, label):
        return SimpleNamespace(class_name=lambda: 'Button', control_id=lambda: identifier,
                               window_text=lambda: label, click=lambda: clicked.append(identifier))

    children = [button(961, '退出'), button(962, '重新登录'), button(2, '取消')]
    modal = SimpleNamespace(process_id=lambda: 4242, class_name=lambda: '#32770',
                            is_visible=lambda: True, is_enabled=lambda: True,
                            window_text=lambda: '通达信金融终端', children=lambda: children)
    driver = object.__new__(DialogDriver)
    driver.windows = lambda: [modal]
    monkeypatch.setattr(tdx_download_ui, 'process_start_token',
                        lambda _pid: 'windows-filetime:synthetic')
    assert driver.confirm_owned_exit(4242, 'windows-filetime:synthetic') is True
    assert clicked == [961]
    children[2] = button(1, '取消')
    with pytest.raises(StopRun, match='LAYOUT_CHANGED'):
        driver.confirm_owned_exit(4242, 'windows-filetime:synthetic')


def test_owned_terminal_closes_exact_exit_modal_after_dialog_disappears(tmp_path, monkeypatch):
    from market_monitor import tdx_download_ui

    clicked = []
    main = SimpleNamespace(process_id=lambda: 4242, class_name=lambda: 'TdxW_MainFrame_Class',
                           window_text=lambda: '通达信金融终端 - [行情]', is_enabled=lambda: False,
                           handle=1)
    modal = SimpleNamespace(process_id=lambda: 4242, class_name=lambda: '#32770',
                            window_text=lambda: '通达信金融终端', is_visible=lambda: True,
                            is_enabled=lambda: True, handle=2,
                            children=lambda: [SimpleNamespace(
                                class_name=lambda: 'Button',
                                control_id=lambda identifier=identifier: identifier,
                                window_text=lambda label=label: label,
                                click=lambda identifier=identifier: clicked.append(identifier),
                            ) for identifier, label in ((961, '退出'), (962, '重新登录'), (2, '取消'))])
    driver = object.__new__(DialogDriver)
    driver.app = SimpleNamespace(process=4242)
    driver.exe = (tmp_path / 'tdxw.exe').resolve()
    driver.dialog = None
    driver.windows = lambda: [modal, main]
    monkeypatch.setattr(tdx_download_ui, '_process_image_path', lambda _pid: driver.exe)
    monkeypatch.setattr(tdx_download_ui, 'process_start_token',
                        lambda _pid: 'windows-filetime:synthetic')
    monkeypatch.setattr(tdx_download_ui, '_wait_process_exit', lambda *_args: True)
    slot = '2026-10-26T23:00:00+08:00'
    state = {batch.key: {'slot': slot, 'status': 'UI_DONE_UNVERIFIED'} for batch in FINANCIAL}
    assert driver.close_owned(4242, state, slot, FINANCIAL,
                              owned_token='windows-filetime:synthetic') == 'CLOSED'
    assert clicked == [961]


def test_owned_terminal_can_close_after_download_dialog_is_gone(tmp_path, monkeypatch):
    from market_monitor import tdx_download_ui

    closed = []
    main = SimpleNamespace(
        process_id=lambda: 4242, class_name=lambda: 'TdxW_MainFrame_Class',
        window_text=lambda: '模拟期货通 - [行情]', is_enabled=lambda: True,
        handle=1, close=lambda: closed.append(True),
    )
    driver = object.__new__(DialogDriver)
    driver.app = SimpleNamespace(process=4242)
    driver.exe = (tmp_path / 'tdxw.exe').resolve()
    driver.dialog = None
    driver.windows = lambda: [main]
    driver.ensure_idle = lambda: pytest.fail('missing dialog cannot be queried')
    driver.confirm_owned_exit = lambda *_args: False
    monkeypatch.setattr(tdx_download_ui, '_process_image_path', lambda _pid: driver.exe)
    monkeypatch.setattr(tdx_download_ui, 'process_start_token',
                        lambda _pid: 'windows-filetime:synthetic')
    monkeypatch.setattr(tdx_download_ui, '_wait_process_exit', lambda *_args: True)
    slot = '2026-10-26T23:00:00+08:00'
    state = {batch.key: {'slot': slot, 'status': 'UI_DONE_UNVERIFIED'} for batch in FUTURES}
    assert driver.close_owned(4242, state, slot, FUTURES,
                              owned_token='windows-filetime:synthetic') == 'CLOSED'
    assert closed == [True]


def test_foreground_input_lock_releases_while_both_downloads_wait(tmp_path: Path):
    entered = threading.Barrier(2)

    class PhasedDriver:
        def open(self): pass
        def prepare_many(self, batches):
            return {batch.key: {'selectionHash': 'synthetic-hash'} for batch in batches}
        def begin_download(self): pass
        def wait_download(self, _timeout):
            entered.wait(timeout=5)
            return 'UI_DONE_UNVERIFIED'

    def run(batches):
        state = {}
        run_terminal(PhasedDriver(), batches, state, 'slot', lambda: None, 60,
                     foreground_lock=tmp_path / 'foreground.lock')
        return {item['status'] for item in state.values()}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, FINANCIAL[:2]), pool.submit(run, FUTURES)]
        assert [future.result(timeout=8) for future in futures] == [
            {'UI_DONE_UNVERIFIED'}, {'UI_DONE_UNVERIFIED'},
        ]


def test_two_terminals_download_concurrently_without_losing_shared_checkpoints(tmp_path, monkeypatch):
    from market_monitor import tdx_download_ui

    both_running = threading.Barrier(2)
    observed = []

    class ConcurrentDriver:
        def __init__(self, path):
            self.terminal = 'financial' if 'tongdaxin' in str(path).lower() else 'futures'
            self.first_download = True

        def connect(self):
            pass

        def open(self):
            pass

        def prepare_many(self, batches):
            return {batch.key: {'selectionHash': 'same-saved-scope'} for batch in batches}

        def download(self, _timeout):
            if self.first_download:
                observed.append(self.terminal)
                both_running.wait(timeout=3)
                self.first_download = False
            return 'UI_DONE_UNVERIFIED'

    monkeypatch.setattr(tdx_download_ui, 'DialogDriver', ConcurrentDriver)
    def run(terminal):
        return main(['--execute', '--terminal', terminal, '--state-dir', str(tmp_path)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ('financial', 'futures')))
    assert results == [0, 0]
    assert set(observed) == {'financial', 'futures'}
    state = json.loads((tmp_path / 'checkpoints.json').read_text(encoding='utf-8'))
    assert {item['status'] for terminal in ('financial', 'futures')
            for item in state[terminal].values()} == {'UI_DONE_UNVERIFIED'}
    latest = json.loads((tmp_path / 'latest.json').read_text(encoding='utf-8'))
    assert latest['terminals'] == {'financial': 'UI_DONE_UNVERIFIED',
                                   'futures': 'UI_DONE_UNVERIFIED'}


def test_explicit_batch_starts_are_passed_to_each_terminal_without_changing_scope(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from market_monitor import tdx_download_ui
    from market_monitor.tdx_afterhours import BEIJING

    chosen = (datetime.now(BEIJING).date() - timedelta(days=1)).isoformat()
    observed = {}

    class RecordingDriver:
        def __init__(self, _path):
            pass

        def connect(self):
            pass

        def open(self):
            pass

        def prepare(self, batch):
            observed[batch.key] = self.batch_start_dates[batch.key].isoformat()
            return {'selectionHash': 'synthetic-saved-scope'}

        def prepare_many(self, batches):
            return {batch.key: self.prepare(batch) for batch in batches}

        def download(self, _timeout):
            return 'UI_DONE_UNVERIFIED'

    monkeypatch.setattr(tdx_download_ui, 'DialogDriver', RecordingDriver)
    for terminal, batches in [('financial', FINANCIAL), ('futures', FUTURES)]:
        flags = [flag for batch in batches for flag in ('--batch-start', f'{batch.key}:{chosen}')]
        assert main(['--execute', '--terminal', terminal, '--state-dir', str(tmp_path), *flags]) == 0
    assert observed == {batch.key: chosen for batch in (*FINANCIAL, *FUTURES)}
    with pytest.raises(SystemExit):
        main(['--execute', '--terminal', 'futures', '--state-dir', str(tmp_path),
              '--batch-start', f'extended-5m:{chosen}'])


def test_futures_native_menu_waits_for_exact_download_dialog():
    driver = object.__new__(DialogDriver)
    main_window = SimpleNamespace(
        window_text=lambda: '通达信期货通 - [行情报价]',
        is_enabled=lambda: True,
        menu_select=lambda _path: (_ for _ in ()).throw(RuntimeError('custom menu')),
    )
    dialog = SimpleNamespace()
    observed = iter([None, dialog])
    opened = []
    driver.guard = lambda: None
    driver.windows = lambda: [main_window]
    driver.find_dialog = lambda: next(observed)
    driver.ensure_idle = lambda: None
    driver.acknowledge_host_initializing = lambda: False
    driver.open_futures_native_menu = lambda main: opened.append(main)

    driver.open()

    assert driver.dialog is dialog
    assert opened == [main_window]


def test_futures_native_menu_waits_for_popup_creation():
    clicked = []
    control = SimpleNamespace(
        class_name=lambda: 'AfxWnd100', control_id=lambda: 1000,
        is_visible=lambda: True, click_input=lambda: clicked.append('system'),
    )
    item = SimpleNamespace(
        text=lambda: '盘后数据下载\tD', is_enabled=lambda: True,
        click_input=lambda: clicked.append('download'),
    )
    popup = SimpleNamespace(
        class_name=lambda: '#32768', process_id=lambda: 123,
        menu=lambda: SimpleNamespace(items=lambda: [item]),
    )
    polls = iter(([], [popup]))
    driver = object.__new__(DialogDriver)
    driver.guard = lambda: None
    driver.Desktop = lambda backend: SimpleNamespace(windows=lambda visible_only: next(polls))
    main_window = SimpleNamespace(descendants=lambda: [control], process_id=lambda: 123)

    driver.open_futures_native_menu(main_window)

    assert clicked == ['system', 'download']


def test_financial_native_menu_waits_for_popup_creation():
    clicked = []
    control = SimpleNamespace(
        class_name=lambda: 'AfxWnd100', control_id=lambda: 3019,
        is_visible=lambda: True, click_input=lambda: clicked.append('options'),
    )
    item = SimpleNamespace(
        text=lambda: '盘后数据下载\tD', is_enabled=lambda: True,
        click_input=lambda: clicked.append('download'),
    )
    popup = SimpleNamespace(
        class_name=lambda: '#32768', process_id=lambda: 123,
        menu=lambda: SimpleNamespace(items=lambda: [item]),
    )
    polls = iter(([], [popup]))
    driver = object.__new__(DialogDriver)
    driver.guard = lambda: None
    driver.Desktop = lambda backend: SimpleNamespace(windows=lambda visible_only: next(polls))
    main_window = SimpleNamespace(descendants=lambda: [control], process_id=lambda: 123)

    driver.open_financial_native_menu(main_window)

    assert clicked == ['options', 'download']


def test_guard_accepts_exact_download_dialog_when_main_is_temporarily_hidden():
    driver = object.__new__(DialogDriver)
    driver.unlocked = lambda: None
    dialog = SimpleNamespace(window_text=lambda: '盘后数据下载', class_name=lambda: '#32770')
    driver.windows = lambda: [dialog]
    driver.guard()
    login = SimpleNamespace(window_text=lambda: '通达信金融终端V7.71',
                            class_name=lambda: '#32770')
    driver.windows = lambda: [login]
    with pytest.raises(StopRun, match='WAITING_FOR_LOGIN'):
        driver.guard()
    driver.windows = lambda: []
    driver.dialog = SimpleNamespace(window_text=lambda: '盘后数据下载', is_visible=lambda: True)
    driver.guard()
    blank_dialog = SimpleNamespace(window_text=lambda: '', class_name=lambda: '#32770',
                                   is_visible=lambda: True)
    blank_main = SimpleNamespace(window_text=lambda: '', class_name=lambda: 'TdxW_MainFrame_Class')
    driver.dialog = blank_dialog
    driver.windows = lambda: [blank_dialog, blank_main]
    driver.guard()


def test_saved_account_login_clicks_only_known_layout_once():
    clicked = []
    window = SimpleNamespace(
        class_name=lambda: '#32770', window_text=lambda: '通达信金融终端V7.71',
        rectangle=lambda: SimpleNamespace(width=lambda: 564, height=lambda: 428),
        set_focus=lambda: None, click_input=lambda **kwargs: clicked.append(kwargs),
    )
    DialogDriver.login_saved_account(window)
    assert clicked == [{'coords': (196, 370)}]
    bad = SimpleNamespace(**{**window.__dict__, 'window_text': lambda: '未知登录窗口'})
    with pytest.raises(StopRun, match='UNRECOGNIZED'):
        DialogDriver.login_saved_account(bad)


def test_extended_minute_start_changes_only_start_picker():
    from datetime import date

    class Picker:
        def __init__(self, year, month, day, *, enabled=True):
            self.time = SimpleNamespace(wYear=year, wMonth=month, wDay=day)
            self.writes = []
            self.enabled = enabled

        def class_name(self):
            return 'SysDateTimePick32'

        def is_visible(self):
            return True

        def is_enabled(self):
            return self.enabled

        def get_time(self):
            return self.time

        def set_time(self, **kwargs):
            self.writes.append(kwargs)
            self.time = SimpleNamespace(wYear=kwargs['year'], wMonth=kwargs['month'], wDay=kwargs['day'])

    start, end = Picker(2025, 12, 25), Picker(2026, 10, 22, enabled=False)
    driver = object.__new__(DialogDriver)
    driver.control = lambda identifier: start if identifier == 1237 else end
    driver.set_extended_minute_start(date(2026, 10, 20))
    assert start.writes == [{'year': 2026, 'month': 10, 'day': 20}]
    assert end.writes == []
    with pytest.raises(StopRun, match='BATCH_START_AFTER_SAVED_END'):
        driver.set_batch_start(date(2026, 10, 23))
    assert end.writes == []
