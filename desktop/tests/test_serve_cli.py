"""Compatibility checks for the real lightweight service command entry."""

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from market_monitor import serve_cli


@pytest.mark.parametrize("disable_updates", [False, True])
def test_light_serve_preserves_flags_and_machine_result(
    monkeypatch, tmp_path, capsys, disable_updates,
) -> None:
    received = {}
    web_app = ModuleType("market_monitor.web_app")

    def fake_serve(data_root, **kwargs):
        received.update(data_root=data_root, **kwargs)
        return "127.0.0.1", 9876

    web_app.serve_web_app = fake_serve
    monkeypatch.setitem(sys.modules, "market_monitor.web_app", web_app)
    arguments = [
        "serve", "--data-root", str(tmp_path), "--host", "127.0.0.1",
        "--port", "0", "--timeout-seconds", "0.2", "--quiet",
    ]
    if disable_updates:
        arguments.append("--no-auto-update-on-startup")

    assert serve_cli.main(arguments) == 0
    assert received == {
        "data_root": tmp_path, "host": "127.0.0.1", "port": 0,
        "timeout_seconds": 0.2, "quiet": True,
        "auto_update_on_startup": not disable_updates,
    }
    assert json.loads(capsys.readouterr().out) == {
        "status": "SUCCESS", "exit_code": 0,
        "message": "local research terminal served at http://127.0.0.1:9876/",
    }


@pytest.mark.parametrize("arguments", [
    ["serve", "--timeout-seconds", "0"],
    ["serve", "--port", "invalid"],
    ["serve", "--unknown=apiKey=SYNTHETIC_TEST_SECRET"],
])
def test_light_serve_rejects_arguments_before_loading_web_app(monkeypatch, capsys, arguments) -> None:
    # A malformed request must not construct the app, migrate a Catalog or bind.
    monkeypatch.setitem(sys.modules, "market_monitor.web_app", None)
    assert serve_cli.main(arguments) == 64
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "ARGUMENT_ERROR"
    assert payload["exit_code"] == 64
    assert "SYNTHETIC_TEST_SECRET" not in output


def test_light_serve_interrupt_keeps_success_contract(monkeypatch, capsys) -> None:
    web_app = ModuleType("market_monitor.web_app")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    web_app.serve_web_app = interrupted
    monkeypatch.setitem(sys.modules, "market_monitor.web_app", web_app)
    assert serve_cli.main(["serve"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "SUCCESS", "exit_code": 0,
        "message": "control center stopped by user",
    }


def test_module_serve_entry_avoids_full_cli_in_fresh_process() -> None:
    script = """
import runpy
import sys
import types

sys.modules['market_monitor.cli'] = None
web_app = types.ModuleType('market_monitor.web_app')
web_app.serve_web_app = lambda *args, **kwargs: ('127.0.0.1', 9876)
sys.modules['market_monitor.web_app'] = web_app
sys.argv = ['market_monitor', 'serve', '--quiet', '--no-auto-update-on-startup']
runpy.run_module('market_monitor', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "SUCCESS"


def test_app_binds_all_operations_without_loading_collector(tmp_path) -> None:
    script = """
import sys
import types
from pathlib import Path

# Fail on any eager import, including indirect imports while constructing routes.
sys.modules['market_monitor.collector'] = None
from market_monitor import web_app
from market_monitor.operations import OperationKind

original_manager = web_app.OperationManager
captured = []
def capture_manager(*args, **kwargs):
    manager = original_manager(*args, **kwargs)
    captured.append(manager)
    return manager
web_app.OperationManager = capture_manager
root = Path(sys.argv[1])
app = web_app.create_web_app(root)
assert len(captured) == 1
handlers = captured[0].handlers
assert set(handlers) == set(OperationKind)
assert sys.modules['market_monitor.collector'] is None
assert '/api/ready' in {getattr(route, 'path', None) for route in app.routes}

# The deferred binding must still execute the existing collection contract.
collector = types.ModuleType('market_monitor.collector')
def build_tasks(**kwargs):
    assert kwargs == {'limit_futures': 15, 'limit_cn_stocks': 5}
    return [types.SimpleNamespace(dataset_id='FUTURES_OI_LEADERBOARD'),
            types.SimpleNamespace(dataset_id='SYNTHETIC_SAFE_DATASET')]
def run_session(data_root, **kwargs):
    assert data_root == root
    assert kwargs['isolate_akshare'] is True
    assert [task.dataset_id for task in kwargs['tasks']] == ['SYNTHETIC_SAFE_DATASET']
    return {'status': 'PASS', 'passed': 1, 'task_count': 1,
            'failed': 0, 'blocked': 0, 'total_rows': 0}
collector.build_collection_tasks = build_tasks
collector.run_fetch_session = run_session
sys.modules['market_monitor.collector'] = collector
assert handlers[OperationKind.MARKET_UPDATE]().startswith('PASS:')
print('operation bindings and deferred invocation passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True,
        text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "operation bindings and deferred invocation passed" in result.stdout
