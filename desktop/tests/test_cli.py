"""Stable, safe CLI outcomes without performing real provider probes."""

import json

from market_monitor.providers import (
    Capability,
    CapabilityRegistration,
    CapabilityStatus,
    ConfigurationRequirement,
    ErrorCategory,
    Provider,
    ProviderError,
    ProviderOperation,
    ProviderRequest,
)

from market_monitor import __version__
from market_monitor.cli import main
from market_monitor.collector import CollectionTask
from market_monitor.configuration import LocalConfiguration


class _Provider(Provider):
    name = "test-provider"

    def __init__(self, capabilities=(), missing=()):
        self._capabilities = capabilities
        self._missing = missing

    def probe_capabilities(self):
        return self._capabilities

    def missing_configuration_requirements(self):
        return self._missing

    def fetch_instruments(self):
        raise NotImplementedError

    def fetch_bars(self):
        raise NotImplementedError

    def fetch_indicators(self):
        raise NotImplementedError

    def fetch_calendar(self):
        raise NotImplementedError

    def health_check(self):
        raise NotImplementedError


def _capability(status: CapabilityStatus) -> Capability:
    return Capability(
        "bars",
        status,
        detail="safe test result",
        registration=CapabilityRegistration("bars", "test bars", ProviderRequest(ProviderOperation.OTHER)),
        error=ProviderError(ErrorCategory.NETWORK, "safe failure") if status is CapabilityStatus.FAILED else None,
    )


def test_package_version_is_pinned() -> None:
    assert __version__ == "0.1.0"


def test_main_returns_success(capsys) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["status"] == "SUCCESS"


def test_probe_exit_codes_are_machine_readable_without_real_network(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("market_monitor.cli.registered_providers", lambda values=None: (_Provider([_capability(CapabilityStatus.FAILED)]),))

    assert main(["probe", "--report-dir", str(tmp_path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 2
    assert payload["status"] == "PARTIAL_FAILURE"
    assert payload["reports"][0].endswith("provider-capabilities.json")
    assert payload["reports"][1].endswith("provider-capabilities.md")


def test_probe_configuration_and_argument_exit_codes(monkeypatch, tmp_path, capsys) -> None:
    missing = (ConfigurationRequirement("JQDATA_PASSWORD", "configuration-jqdata-password", "password required"),)
    monkeypatch.setattr("market_monitor.cli.registered_providers", lambda values=None: (_Provider(missing=missing),))

    assert main(["probe", "--report-dir", str(tmp_path)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "CONFIGURATION_BLOCKED"
    assert main(["probe", "--timeout-seconds", "0"]) == 64
    assert json.loads(capsys.readouterr().out)["exit_code"] == 64


def test_cli_redacts_sensitive_report_directory_from_machine_output(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("market_monitor.cli.registered_providers", lambda values=None: (_Provider([_capability(CapabilityStatus.PASS)]),))
    report_dir = tmp_path / "apiKey=CLI_LEAK"

    assert main(["probe", "--report-dir", str(report_dir)]) == 0
    output = capsys.readouterr().out

    assert "CLI_LEAK" not in output
    assert "[redacted sensitive text]" in output
    assert (report_dir / "provider-capabilities.json").is_file()
    assert (report_dir / "provider-capabilities.md").is_file()


def test_cli_redacts_registered_short_secret_when_it_appears_as_a_report_path_token(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("market_monitor.cli.registered_providers", lambda values=None: (_Provider([_capability(CapabilityStatus.PASS)]),))
    monkeypatch.setattr("market_monitor.cli.load_local_configuration", lambda **_: LocalConfiguration({"JQDATA_PASSWORD": "s3"}))
    report_dir = tmp_path / "s3"

    assert main(["probe", "--report-dir", str(report_dir)]) == 0
    output = capsys.readouterr().out

    assert "s3" not in output
    assert "***" in output


def test_cli_serve_emits_machine_readable_result(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        "market_monitor.cli.serve_web_app",
        lambda *args, **kwargs: ("127.0.0.1", 9876),
    )

    assert main(
        ["serve", "--data-root", str(tmp_path), "--host", "127.0.0.1", "--port", "0", "--timeout-seconds", "0.2", "--quiet"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCESS"
    assert "http://127.0.0.1:9876/" in payload["message"]


def test_cli_serve_rejects_non_positive_timeout(capsys) -> None:
    assert main(["serve", "--timeout-seconds", "0"]) == 64
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ARGUMENT_ERROR"
    assert "timeout-seconds" in payload["message"]


def test_cli_fetch_emits_machine_readable_session_summary(monkeypatch, tmp_path, capsys) -> None:
    def fake_run(data_root, **kwargs):
        assert data_root == tmp_path
        return {
            "session_id": "session-test",
            "status": "PARTIAL_FAILURE",
            "passed": 12,
            "failed": 3,
            "blocked": 2,
            "total_rows": 12345,
        }

    monkeypatch.setattr("market_monitor.cli.run_fetch_session", fake_run)
    assert main(["fetch", "--data-root", str(tmp_path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PARTIAL_FAILURE"
    assert payload["exit_code"] == 2
    assert "session-test" in payload["message"]
    assert "12345 rows" in payload["message"]


def test_cli_fetch_rejects_non_positive_limits(monkeypatch, capsys) -> None:
    monkeypatch.setattr("market_monitor.cli.run_fetch_session", lambda *a, **k: None)
    assert main(["fetch", "--limit-futures", "0"]) == 64
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ARGUMENT_ERROR"
    assert "limit-futures" in payload["message"]


def test_cli_fetch_can_select_only_one_dataset_without_starting_the_full_session(monkeypatch, tmp_path, capsys) -> None:
    requested: dict[str, object] = {}
    tasks = [
        CollectionTask("CN_STOCK_BAR", "股票", "fixture", lambda: None),
        CollectionTask("FUTURES_OI_LEADERBOARD", "席位排名", "fixture", lambda: None),
    ]

    monkeypatch.setattr("market_monitor.cli.build_collection_tasks", lambda **_: tasks)

    def fake_run(data_root, **kwargs):
        requested["data_root"] = data_root
        requested["tasks"] = kwargs["tasks"]
        return {
            "session_id": "session-one-task",
            "status": "PASS",
            "passed": 1,
            "failed": 0,
            "blocked": 0,
            "total_rows": 99,
        }

    monkeypatch.setattr("market_monitor.cli.run_fetch_session", fake_run)
    assert main(["fetch", "--data-root", str(tmp_path), "--dataset", "FUTURES_OI_LEADERBOARD"]) == 0

    assert requested["data_root"] == tmp_path
    assert [task.dataset_id for task in requested["tasks"]] == ["FUTURES_OI_LEADERBOARD"]
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_cli_fetch_rejects_an_unknown_dataset(capsys) -> None:
    assert main(["fetch", "--dataset", "NOT_A_DATASET"]) == 64
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ARGUMENT_ERROR"
    assert "NOT_A_DATASET" in payload["message"]
