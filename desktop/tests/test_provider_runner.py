"""Fixed provider responses exercise D0-010 failure isolation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event
from time import monotonic

import pytest

from market_monitor.contracts import validate_contract
from market_monitor.cli import _emit
from market_monitor.providers import (
    Capability,
    CapabilityRegistration,
    CapabilityStatus,
    ConfigurationRequirement,
    ErrorCategory,
    FetchResult,
    ProbeRunner,
    Provider,
    ProviderError,
    ProviderOperation,
    ProviderRequest,
    RetryPolicy,
    redact_secrets,
)


class StaticProvider(Provider):
    def __init__(self, name: str, response: object) -> None:
        self.name = name
        self.response = response

    def probe_capabilities(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def fetch_instruments(self) -> FetchResult:
        return FetchResult(records=[])

    def fetch_bars(self) -> FetchResult:
        return FetchResult(records=[])

    def fetch_indicators(self) -> FetchResult:
        return FetchResult(records=[])

    def fetch_calendar(self) -> FetchResult:
        return FetchResult(records=[])

    def health_check(self) -> FetchResult:
        return FetchResult(records=[])


class BlockingProvider(StaticProvider):
    def __init__(self, name: str) -> None:
        super().__init__(name, [])
        self.release = Event()
        self.invoked = Event()
        self.calls = 0

    def probe_capabilities(self):
        self.calls += 1
        self.invoked.set()
        self.release.wait()
        return []


class ConfiguredProvider(StaticProvider):
    def __init__(self, missing: tuple[ConfigurationRequirement, ...]) -> None:
        super().__init__("configured", [])
        self._missing = missing

    def missing_configuration_requirements(self):
        return self._missing


class RetryingProvider(StaticProvider):
    def __init__(self) -> None:
        super().__init__("rate-limited", [])
        self.calls = 0

    def probe_capabilities(self):
        self.calls += 1
        if self.calls < 3:
            raise ProviderError(ErrorCategory.QUOTA, "HTTP 429 token=visible-token")
        return [_static_capability("bars", CapabilityStatus.PASS, row_count=1)]


class AlwaysRateLimitedProvider(StaticProvider):
    def __init__(self) -> None:
        super().__init__("always-rate-limited", [])
        self.calls = 0

    def probe_capabilities(self):
        self.calls += 1
        raise ProviderError(ErrorCategory.QUOTA, "HTTP 429")


def _static_capability(name: str, status: CapabilityStatus, detail: str | None = None, row_count: int | None = None) -> Capability:
    return Capability(
        name,
        status,
        detail,
        row_count,
        registration=CapabilityRegistration(
            name, f"Static test capability: {name}", ProviderRequest(ProviderOperation.OTHER)
        ),
    )


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_category"),
    [
        ([_static_capability("normal", CapabilityStatus.PASS, row_count=2)], "PASS", None),
        (ProviderError(ErrorCategory.NO_COVERAGE, "empty response"), "FAILED", "NO_COVERAGE"),
        (ProviderError(ErrorCategory.NETWORK, "request timeout"), "FAILED", "NETWORK"),
        (ProviderError(ErrorCategory.QUOTA, "HTTP 429"), "FAILED", "RATE_LIMIT"),
        (ProviderError(ErrorCategory.FIELD_CHANGE, "required close field missing"), "FAILED", "PROVIDER"),
        (
            [
                _static_capability("partial", CapabilityStatus.PASS, row_count=1),
                _static_capability("missing", CapabilityStatus.FAILED, "field absent"),
            ],
            "FAILED",
            None,
        ),
    ],
)
def test_fixed_provider_responses_are_classified(
    response: object,
    expected_status: str,
    expected_category: str | None,
) -> None:
    result = ProbeRunner().run([StaticProvider("fixed", response)]).results[0]
    assert result.capabilities[-1].status.value == expected_status
    assert (result.capabilities[-1].error.category.value if result.capabilities[-1].error else None) == expected_category


def test_failure_does_not_stop_later_provider_and_reports_are_dual_format(tmp_path) -> None:
    report = ProbeRunner().run(
        [
            StaticProvider("broken", ProviderError(ErrorCategory.NETWORK, "token=secret-value")),
            StaticProvider("healthy", [_static_capability("bars", CapabilityStatus.PASS, row_count=2)]),
        ]
    )
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)

    payload = json.loads(machine_path.read_text(encoding="utf-8"))
    assert [item["source"]["id"] for item in payload["providers"]] == ["broken", "healthy"]
    assert payload["providers"][0]["capabilities"][0]["error"]["message"] == "[redacted sensitive text]"
    assert "healthy" in human_path.read_text(encoding="utf-8")


def test_timeout_is_reported_and_does_not_stop_later_provider() -> None:
    blocking = BlockingProvider("blocked-network")
    started = monotonic()
    try:
        report = ProbeRunner(timeout_seconds=0.01).run(
            [blocking, StaticProvider("healthy", [_static_capability("bars", CapabilityStatus.PASS, row_count=2)])]
        )
    finally:
        blocking.release.set()

    assert blocking.invoked.wait(0.1)
    assert blocking.calls == 1
    assert monotonic() - started < 1
    assert [result.capabilities[-1].status for result in report.results] == [CapabilityStatus.FAILED, CapabilityStatus.PASS]
    assert report.results[0].capabilities[0].error == ProviderError(ErrorCategory.NETWORK, "provider probe exceeded 0.01 seconds")


def test_missing_configuration_is_reported_per_requirement_without_calling_provider() -> None:
    requirements = (
        ConfigurationRequirement("JQDATA_USERNAME", "configuration-jqdata-username", "username is required"),
        ConfigurationRequirement("JQDATA_PASSWORD", "configuration-jqdata-password", "password is required"),
    )
    result = ProbeRunner().run([ConfiguredProvider(requirements)]).results[0]

    assert [capability.status for capability in result.capabilities] == [CapabilityStatus.BLOCKED, CapabilityStatus.BLOCKED]
    assert [capability.error.category for capability in result.capabilities if capability.error] == [
        ErrorCategory.CONFIGURATION,
        ErrorCategory.CONFIGURATION,
    ]


def test_rate_limit_retries_are_bounded_and_backed_off_without_leaking_tokens() -> None:
    provider = RetryingProvider()
    delays: list[float] = []
    result = ProbeRunner(
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.5),
        sleeper=delays.append,
        secret_values=("visible-token",),
    ).run([provider]).results[0]

    assert provider.calls == 3
    assert delays == [0.5, 1.0]
    assert result.capabilities[0].status is CapabilityStatus.PASS


def test_persistent_rate_limit_stops_at_the_retry_bound_and_uses_injected_clock() -> None:
    provider = AlwaysRateLimitedProvider()
    delays: list[float] = []
    fixed_time = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    report = ProbeRunner(
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.25),
        sleeper=delays.append,
        now=lambda: fixed_time,
    ).run([provider])

    assert provider.calls == 3
    assert delays == [0.25, 0.5]
    assert report.generated_at == "2026-08-05T09:00:00+00:00"
    assert report.results[0].capabilities[0].error.category is ErrorCategory.QUOTA


def test_secret_redaction_covers_common_error_shapes() -> None:
    assert redact_secrets("account=alice token=abc Bearer xyz") == "[redacted sensitive text]"
    assert redact_secrets("https://alice:pw@example.test/?api_key=x") == "[redacted sensitive text]"  # Synthetic test credential or marker.
    assert redact_secrets({"headers": {"Authorization": "Bearer short"}, "nested": ["password: pw"]}, secret_values=("pw",)) == {
        "headers": {"Authorization": "***"},
        "nested": ["[redacted sensitive text]"],
    }


def test_secret_redaction_normalizes_camel_snake_and_kebab_sensitive_names_without_mutating_short_plain_text() -> None:
    payload = {
        "accessToken": "LEAK_ACCESS",
        "nested": {"client_secret": "LEAK_SECRET", "private-key": "LEAK_PRIVATE", "apiKey": "xy"},
        "ordinaryKey": "unchanged",
    }
    redacted = redact_secrets(payload, secret_values=("xy",))

    assert redacted == {
        "accessToken": "***",
        "nested": {"client_secret": "***", "private-key": "***", "apiKey": "***"},
        "ordinaryKey": "unchanged",
    }
    assert redact_secrets('{"xy":"value","id":"xy","note":"proxy"}', secret_values=("xy",)) == '{"xy":"value","id":"***","note":"proxy"}'
    text = "accessToken=LEAK_ACCESS client-secret=LEAK_SECRET private_key=LEAK_PRIVATE apiKey=LEAK_API\nX-Client-Secret: LEAK_HEADER"
    assert "LEAK" not in redact_secrets(text)
    assert redact_secrets("https://user:pass@example.test/?accessToken=LEAK_QUERY&apiKey=LEAK_API") == "[redacted sensitive text]"  # Synthetic test credential or marker.


def test_secret_redaction_parses_complete_nested_json_and_preserves_valid_json() -> None:
    payload = {
        "accessToken": "REVIEW_JSON",
        "nested": {"clientSecret": "REVIEW_CLIENT", "privateKey": "REVIEW_PRIVATE", "safe": "ok"},
        "escaped": "line\\nquote\\\"value",
    }
    redacted = redact_secrets(json.dumps(payload))

    parsed = json.loads(redacted)
    assert parsed["accessToken"] == "***"
    assert parsed["nested"] == {"clientSecret": "***", "privateKey": "***", "safe": "ok"}
    assert parsed["escaped"] == payload["escaped"]
    assert not any(value in redacted for value in ("REVIEW_JSON", "REVIEW_CLIENT", "REVIEW_PRIVATE"))


def test_secret_redaction_handles_embedded_json_markdown_and_adjacent_fields_without_crossing_lines() -> None:
    text = (
        'SDK error: {"accessToken":"REVIEW_JSON","client_secret":"REVIEW_CLIENT","private-key":"REVIEW_PRIVATE","safe":"ok"} '
        '`accessToken`: `REVIEW_MARKDOWN`\n'
        'next line "apiKey": REVIEW_UNQUOTED, "safe": "still-safe"'
    )
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert not any(value in redacted for value in ("REVIEW_JSON", "REVIEW_CLIENT", "REVIEW_PRIVATE", "REVIEW_MARKDOWN", "REVIEW_UNQUOTED"))


def test_secret_redaction_structurally_scans_nested_embedded_json_arrays_multiple_fragments_and_crlf() -> None:
    text = (
        'prefix {"outer":{"accessToken":"REVIEW_EMBED_TOKEN","events":[{"clientSecret":"REVIEW_EMBED_SECRET"},{"safe":"first"}]},"safe":"outer"}\r\n'
        'middle [{"privateKey":"REVIEW_EMBED_PRIVATE","safe":"array"}] suffix {"safe":"second"}'
    )
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert not any(value in redacted for value in ("REVIEW_EMBED_TOKEN", "REVIEW_EMBED_SECRET", "REVIEW_EMBED_PRIVATE"))


def test_secret_redaction_recovers_valid_nested_object_from_malformed_outer_text_without_overmatching() -> None:
    text = 'broken {"outer": {"accessToken":"REVIEW_MALFORMED"} trailing, "safe":"kept"'
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_MALFORMED" not in redacted


def test_report_writer_redacts_nested_capability_error_values(tmp_path) -> None:
    report = ProbeRunner(secret_values=("pw", "xy")).run(
        [StaticProvider("unsafe", [_static_capability("bars", CapabilityStatus.FAILED, "password=pw")])]
    )
    machine_path, human_path = ProbeRunner(secret_values=("pw", "xy")).write_reports(report, tmp_path)

    assert "pw" not in machine_path.read_text(encoding="utf-8")
    assert "pw" not in human_path.read_text(encoding="utf-8")


def test_registered_short_secret_is_redacted_from_exception_and_both_reports_without_corrupting_json_key(tmp_path) -> None:
    runner = ProbeRunner(secret_values=("s3",))
    report = runner.run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, 'request failed s3 {"s3":"key"}'))])
    machine_path, human_path = runner.write_reports(report, tmp_path)

    assert '"s3"' in report.results[0].capabilities[0].error.message
    assert "request failed ***" in report.results[0].capabilities[0].error.message
    assert "request failed s3" not in machine_path.read_text(encoding="utf-8")
    assert "request failed s3" not in human_path.read_text(encoding="utf-8")


def test_json_exception_is_redacted_before_machine_and_human_report_output(tmp_path) -> None:
    error_text = '{"accessToken":"REVIEW_JSON","clientSecret":"REVIEW_CLIENT","privateKey":"REVIEW_PRIVATE"}'
    runner = ProbeRunner()
    report = runner.run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = runner.write_reports(report, tmp_path)

    machine = machine_path.read_text(encoding="utf-8")
    human = human_path.read_text(encoding="utf-8")
    report_error = json.loads(json.loads(machine)["providers"][0]["capabilities"][0]["error"]["message"])
    assert report_error == {"accessToken": "***", "clientSecret": "***", "privateKey": "***"}
    assert not any(value in machine or value in human for value in ("REVIEW_JSON", "REVIEW_CLIENT", "REVIEW_PRIVATE"))


def test_nested_json_network_error_is_redacted_through_result_reports_and_cli_stdout(tmp_path, capsys) -> None:
    error_text = 'SDK failed: {"outer":{"accessToken":"REVIEW_EMBED_TOKEN","clientSecret":"REVIEW_EMBED_SECRET"},"safe":"ok"}'
    runner = ProbeRunner()
    report = runner.run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = runner.write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    result_message = report.results[0].capabilities[0].error.message
    for text in (result_message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert "REVIEW_EMBED_TOKEN" not in text
        assert "REVIEW_EMBED_SECRET" not in text
    assert result_message == "[redacted sensitive text]"


def test_deep_complete_and_broken_json_never_escape_and_still_hide_direct_sensitive_pair() -> None:
    complete = '{"outer":' * 1000 + '"accessToken":"REVIEW_DEEP_COMPLETE"' + "}" * 1000
    broken = '{"outer":' * 1000 + '"accessToken":"REVIEW_DEEP_BROKEN"'

    complete_redacted = redact_secrets(complete)
    broken_redacted = redact_secrets(broken)

    assert "REVIEW_DEEP_COMPLETE" not in complete_redacted
    assert "REVIEW_DEEP_BROKEN" not in broken_redacted
    assert complete_redacted == "[redacted sensitive text]"
    assert broken_redacted == "[redacted sensitive text]"


def test_redaction_depth_limit_handles_manual_deep_mapping_without_recursion_error() -> None:
    value: dict[str, object] = {"accessToken": "REVIEW_MAPPING_SECRET"}
    for _ in range(1000):
        value = {"outer": value}

    redacted = redact_secrets(value)

    assert isinstance(redacted, dict)
    current: object = redacted
    for _ in range(64):
        assert isinstance(current, dict)
        current = current["outer"]
    assert current == "[redaction depth limit]"


def test_large_many_candidate_text_is_bounded_and_falls_back_to_direct_sensitive_field_redaction() -> None:
    text = "{\"outer\":" * 11_000 + '"accessToken":"REVIEW_LARGE_SECRET"'
    started = monotonic()
    redacted = redact_secrets(text)

    assert monotonic() - started < 3
    assert "REVIEW_LARGE_SECRET" not in redacted
    assert redacted == "[redacted sensitive text]"


def test_deep_provider_error_is_controlled_across_result_reports_and_cli(tmp_path, capsys) -> None:
    error_text = '{"outer":' * 1000 + '"accessToken":"REVIEW_PROVIDER_DEEP"'
    runner = ProbeRunner()
    report = runner.run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = runner.write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    for text in (report.results[0].capabilities[0].error.message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert "REVIEW_PROVIDER_DEEP" not in text
        assert "[redacted sensitive text]" in text


@pytest.mark.parametrize(
    "text, secret",
    [
        ('{"outer":' * 129 + '"accessToken":{"nested":"REVIEW_AFTER_BUDGET"}}', "REVIEW_AFTER_BUDGET"),
        ('prefix {"padding":"' + ("x" * 9_000) + '","clientSecret":["REVIEW_AFTER_SIZE"]}', "REVIEW_AFTER_SIZE"),
        ('accessToken={"nested":"REVIEW_OBJECT"}, clientSecret=["REVIEW_ARRAY"]', "REVIEW_OBJECT"),
        ('accessToken={"nested":"REVIEW_OBJECT"}, clientSecret=["REVIEW_ARRAY"]', "REVIEW_ARRAY"),
        ('detail="accessToken=REVIEW_DETAIL_DOUBLE" detail=\'client-secret: REVIEW_DETAIL_SINGLE\'', "REVIEW_DETAIL_DOUBLE"),
        ('prefix\r\naccessToken\r\n=\r\n{"nested":"REVIEW_CRLF"}\r\nclientSecret=REVIEW_SECOND', "REVIEW_CRLF"),
        ('prefix\r\naccessToken\r\n=\r\n{"nested":"REVIEW_CRLF"}\r\nclientSecret=REVIEW_SECOND', "REVIEW_SECOND"),
    ],
)
def test_fail_closed_residual_detection_hides_complex_or_budget_exhausted_sensitive_assignments(text: str, secret: str) -> None:
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert secret not in redacted


def test_complete_large_legal_json_keeps_its_structure_while_redacting_sensitive_values() -> None:
    source = json.dumps({"padding": "x" * 9_000, "accessToken": "REVIEW_COMPLETE_LARGE", "safe": [1, 2]})
    redacted = redact_secrets(source)

    assert json.loads(redacted) == {"padding": "x" * 9_000, "accessToken": "***", "safe": [1, 2]}
    assert "REVIEW_COMPLETE_LARGE" not in redacted


def test_registered_short_secret_remains_precisely_replaced_without_a_sensitive_key() -> None:
    assert redact_secrets("prefix xy proxy xylophone suffix", secret_values=("xy",)) == "prefix *** proxy xylophone suffix"


def test_fail_closed_detector_does_not_treat_token_count_or_plain_language_as_a_credential_assignment() -> None:
    assert redact_secrets("token_count=42; ordinary text about token counts") == "token_count=42; ordinary text about token counts"


def test_fail_closed_residual_detection_protects_provider_result_reports_and_cli(tmp_path, capsys) -> None:
    error_text = '{"outer":' * 129 + '"accessToken":{"nested":"REVIEW_FAIL_CLOSED"}}'
    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    for text in (report.results[0].capabilities[0].error.message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert "REVIEW_FAIL_CLOSED" not in text
        assert "[redacted sensitive text]" in text


@pytest.mark.parametrize(
    "text, secret",
    [
        ("accessToken\r\n:=\r\nREVIEW_CRLF_COLON_EQUALS", "REVIEW_CRLF_COLON_EQUALS"),
        ("accessToken\n: =\nREVIEW_LF_SPACED_EQUALS", "REVIEW_LF_SPACED_EQUALS"),
        ('"accessToken"\r\n:\r\nREVIEW_QUOTED_KEY', "REVIEW_QUOTED_KEY"),
        ("`accessToken`\n=\nREVIEW_BACKTICK_KEY", "REVIEW_BACKTICK_KEY"),
        ('accessToken := "REVIEW_QUOTED_VALUE"', "REVIEW_QUOTED_VALUE"),
        ('accessToken:= {"nested":"REVIEW_OBJECT_VALUE"}', "REVIEW_OBJECT_VALUE"),
        ('accessToken : = ["REVIEW_ARRAY_VALUE"]', "REVIEW_ARRAY_VALUE"),
        ("accessToken=***evil", "evil"),
        ('accessToken="***evil"', "evil"),
        ("safe=one accessToken:=REVIEW_FIRST clientSecret : = REVIEW_SECOND", "REVIEW_FIRST"),
        ("safe=one accessToken:=REVIEW_FIRST clientSecret : = REVIEW_SECOND", "REVIEW_SECOND"),
    ],
)
def test_preflight_fail_closed_detects_longest_assignment_separators_before_text_rewrites(text: str, secret: str) -> None:
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert secret not in redacted


def test_preflight_accepts_only_a_complete_safe_sentinel_and_ignores_key_words_without_assignments() -> None:
    assert redact_secrets('accessToken := "***"') != "[redacted sensitive text]"
    assert redact_secrets("accessToken is only a label; token_count=42") == "accessToken is only a label; token_count=42"


def test_crlf_colon_equals_preflight_protects_provider_result_reports_and_cli(tmp_path, capsys) -> None:
    error_text = 'accessToken\r\n: =\r\n{"nested":"REVIEW_PREFLIGHT_PROVIDER"}'
    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    for text in (report.results[0].capabilities[0].error.message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert text.find("REVIEW_PREFLIGHT_PROVIDER") == -1
        assert "[redacted sensitive text]" in text


def _escape_quotes_once(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'").replace("`", "\\`")


def _escape_quotes_with_backslash_run(text: str, run_length: int) -> str:
    return text.replace('"', ("\\" * run_length) + '"').replace("'", ("\\" * run_length) + "'").replace("`", ("\\" * run_length) + "`")


@pytest.mark.parametrize("layers", [1, 2, 3, 4])
def test_preflight_detects_one_to_four_layers_of_escaped_json_quotes(layers: int) -> None:
    escaped = '{"accessToken":"REVIEW_ESCAPED_LAYER"}'
    for _ in range(layers):
        escaped = _escape_quotes_once(escaped)

    redacted = redact_secrets(f"gateway payload {escaped} trailing")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_ESCAPED_LAYER" not in redacted


def test_preflight_decodes_printable_ascii_unicode_escapes_for_sensitive_key_and_value() -> None:
    text = r'gateway {\"access\u0054oken\":\"REVIEW_\u0056ALUE\"}'

    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW" not in redacted


def test_preflight_handles_mixed_native_and_escaped_fields_and_malformed_unicode_escape() -> None:
    mixed = r'native accessToken=REVIEW_NATIVE; gateway {\"clientSecret\":\"REVIEW_ESCAPED\"}'
    malformed = r'gateway {\"access\u12ZZToken\":\"REVIEW_MALFORMED\"}'

    for text, secret in ((mixed, "REVIEW_NATIVE"), (mixed, "REVIEW_ESCAPED"), (malformed, "REVIEW_MALFORMED")):
        redacted = redact_secrets(text)
        assert redacted == "[redacted sensitive text]"
        assert secret not in redacted


def test_escaped_complete_safe_sentinel_can_continue_without_rewriting_original_text() -> None:
    text = r'gateway {\"accessToken\":\"***\"} trailing'

    redacted = redact_secrets(text)

    assert redacted == text


def test_escaped_preflight_view_is_bounded_and_linear_for_large_text() -> None:
    text = ("x" * 100_000) + r' gateway {\"accessToken\":\"REVIEW_LARGE_ESCAPED\"}'
    started = monotonic()
    redacted = redact_secrets(text)

    assert monotonic() - started < 3
    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_LARGE_ESCAPED" not in redacted


def test_escaped_preflight_protects_provider_result_reports_and_cli(tmp_path, capsys) -> None:
    error_text = r'gateway {\"access\u0054oken\":\"REVIEW_ESCAPED_PROVIDER\"}'
    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    for text in (report.results[0].capabilities[0].error.message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert "REVIEW_ESCAPED_PROVIDER" not in text
        assert "[redacted sensitive text]" in text


@pytest.mark.parametrize("layers", [5, 6, 10])
def test_escape_budget_boundary_probe_fail_closes_deeper_supported_quote_layers(layers: int) -> None:
    escaped = '{"accessToken":"REVIEW_DEEP_ESCAPED"}'
    for _ in range(layers):
        escaped = _escape_quotes_once(escaped)

    redacted = redact_secrets(f"gateway {escaped} trailing")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_DEEP_ESCAPED" not in redacted


def test_escape_budget_boundary_probe_protects_provider_result_reports_and_cli(tmp_path, capsys) -> None:
    escaped = '{"accessToken":"REVIEW_DEEP_PROVIDER"}'
    for _ in range(10):
        escaped = _escape_quotes_once(escaped)
    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, f"gateway {escaped}"))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=f"gateway {escaped}")
    stdout = capsys.readouterr().out

    for text in (report.results[0].capabilities[0].error.message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert "REVIEW_DEEP_PROVIDER" not in text
        assert "[redacted sensitive text]" in text


def test_malformed_unicode_and_normal_backslash_text_stay_verbatim_without_sensitive_assignment() -> None:
    for text in (
        r"C:\users\synthetic-user",
        r"ordinary \u12ZZ log",
        r"normal \u12ZZ no credentials",
        r"\\server\share\logs",
        r"plain \\ backlog",
    ):
        assert redact_secrets(text) == text


def test_malformed_unicode_with_explicit_sensitive_assignment_or_candidate_fails_closed() -> None:
    for text in (r"accessToken=\u12ZZREVIEW_MALFORMED_VALUE", r'gateway {\"access\u12ZZToken\":\"REVIEW_MALFORMED_KEY\"}'):
        redacted = redact_secrets(text)
        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_malformed_unicode_at_fifth_escape_layer_fails_closed() -> None:
    escaped = r'{"access\u12ZZToken":"REVIEW_MALFORMED_LAYER5"}'
    for _ in range(5):
        escaped = _escape_quotes_once(escaped)

    redacted = redact_secrets(f"gateway {escaped} trailing")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_MALFORMED_LAYER5" not in redacted


def test_complete_json_double_escaped_unicode_key_is_redacted() -> None:
    text = '{"\\\\u0061\\\\u0063\\\\u0063\\\\u0065\\\\u0073\\\\u0073\\\\u0054\\\\u006f\\\\u006b\\\\u0065\\\\u006e":"REVIEW_DOUBLE_ESCAPED_KEY"}'

    redacted = redact_secrets(text)

    assert "REVIEW_DOUBLE_ESCAPED_KEY" not in redacted
    assert json.loads(redacted)["\\u0061\\u0063\\u0063\\u0065\\u0073\\u0073\\u0054\\u006f\\u006b\\u0065\\u006e"] == "***"


@pytest.mark.parametrize(
    "text",
    [
        "accessToken\uff1aREVIEW_FULLWIDTH_COLON",
        "accessToken\uff1dREVIEW_FULLWIDTH_EQUALS",
        "accessToken\u200b=REVIEW_ZWSP",
        "accessToken\ufeff=REVIEW_ZWNBSP",
        "\uff41\uff43\uff43\uff45\uff53\uff53\uff34\uff4f\uff4b\uff45\uff4e=REVIEW_FULLWIDTH_KEY",
        '" accessToken "=REVIEW_SPACED_QUOTED_KEY',
    ],
)
def test_fullwidth_zero_width_and_spaced_key_assignments_fail_closed(text: str) -> None:
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW" not in redacted


def test_xml_element_sensitive_text_is_redacted() -> None:
    for text in (
        "<password>REVIEW_XML_PASSWORD</password>",
        '<token type="x">REVIEW_XML_TOKEN</token>',
        "<ns:clientSecret>REVIEW_XML_NS_SECRET</ns:clientSecret>",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted
        assert "***" in redacted


def test_xml_element_non_sensitive_text_is_preserved() -> None:
    assert redact_secrets("<name>alice</name>") == "<name>alice</name>"


def test_literal_nonprintable_unicode_escapes_fail_closed() -> None:
    for text in (
        r"accessToken\u200b=REVIEW_LIT_ZWSP",
        r"accessToken\uFF1A=REVIEW_LIT_FWCOLON",
        r"accessToken\u000a=REVIEW_LIT_NL",
        r'{"message":"accessToken\\u200b=REVIEW_JSON_LIT"}',
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_common_credential_key_names_fail_closed() -> None:
    for text in (
        "secret_key=REVIEW_SECRET_KEY",
        "access_key=REVIEW_ACCESS_KEY",
        "passwd=REVIEW_PASSWD",
        "pwd=REVIEW_PWD",
        "password1=REVIEW_PW1",
        "AWS_SECRET_ACCESS_KEY=AKIA_REVIEW_AWS",
        "AWS_ACCESS_KEY_ID=AKIA_REVIEW_AWS_ID",
        "accessKey=REVIEW_ACCESSKEY",
        "bearer: REVIEW_BEARER",
    ):
        redacted = redact_secrets(text)

        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_xml_cdata_nested_and_unicode_tag_names_redacted() -> None:
    for text in (
        "<password><![CDATA[REVIEW_XML_CDATA]]></password>",
        "<password><value>REVIEW_XML_NESTED</value></password>",
        "<PASSWORD>REVIEW_UPPER_XML</PASSWORD>",
        "<\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44>REVIEW_FW_XML</\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44>",
        "<p\u0430ssword>REVIEW_CYR_XML</p\u0430ssword>",
        "<password>REVIEW_UNCLOSED",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted
        assert "***" in redacted


def test_extra_unicode_separators_and_control_chars_fail_closed() -> None:
    for text in (
        "accessToken\ufe66REVIEW_SMALL_EQ",
        "accessToken\u2236REVIEW_RATIO",
        "accessToken\ufe55REVIEW_SMALL_COLON",
        "accessToken\u2a75REVIEW_DBL_EQ",
        "accessToken\u180e=REVIEW_MVS",
        "accessToken\x00=REVIEW_NUL",
        "accessToken\x01=REVIEW_SOH",
    ):
        redacted = redact_secrets(text)

        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_quoted_key_with_inner_whitespace_fails_closed() -> None:
    for text in (
        '"access token" = REVIEW_SPACED_INNER',
        "'client secret'=REVIEW_SPACED_INNER_2",
    ):
        redacted = redact_secrets(text)

        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_non_sensitive_shaped_text_stays_usable() -> None:
    for text in (
        "token_count=42",
        "user_profile.name=alice",
        "<name>alice</name>",
        r"log C:\users\synthetic-user\normal\app.log plain \\server\share",
    ):
        assert redact_secrets(text) == text


def test_encoded_bearer_separators_fail_closed() -> None:
    for text in (
        r"Bearer\u0020REVIEW_BEARER_U",
        r"Bearer\u0009REVIEW_BEARER_TAB",
        "Bearer%20REVIEW_BEARER_PCT",
        r'{"message":"Bearer\\u0020REVIEW_BEARER_JSON"}',
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_compound_sensitive_keys_fail_closed() -> None:
    for text in (
        "user name=REVIEW_USER_NAME",
        "user.name=REVIEW_USER_DOT",
        "pass word=REVIEW_PASS_WORD",
        '"user.name"=REVIEW_QUOTED_DOT',
    ):
        redacted = redact_secrets(text)

        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_percent_html_and_extra_escaped_separators_fail_closed() -> None:
    for text in (
        "accessToken%3DREVIEW_PCT_EQ",
        "accessToken&equals;REVIEW_HTML_EQ",
        "accessToken&#61;REVIEW_HTML_NUM",
        r"access\x54oken=REVIEW_X_ESC",
        r"access\U00000054oken=REVIEW_U_ESC",
    ):
        redacted = redact_secrets(text)

        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_combined_encoded_separators_fail_closed() -> None:
    for text in (
        r"Bearer%5Cu0020REVIEW_COMBO_U",
        "Bearer&#92;u0020REVIEW_COMBO_ENTITY",
        r"Bearer%5Cx20REVIEW_COMBO_X",
        "Bearer%u0020REVIEW_COMBO_PCTU",
        r"accessToken%5Cu0020=REVIEW_COMBO_EQ",
        r"accessToken\U0020=REVIEW_UPPER_U4",
        r"accessToken\U00110000=REVIEW_OVERFLOW",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_encoded_url_userinfo_fails_closed() -> None:
    for text in (
        "https://user%3Apass@host/REVIEW_URL1",
        "https://user:pass%40host/REVIEW_URL2",
        "https://user&#58;pass@host/REVIEW_URL3",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_compound_auth_and_cookie_headers_fail_closed() -> None:
    for text in (
        "Proxy-Authorization: Basic REVIEW_PROXY",
        "X-Authorization: Basic REVIEW_XAUTH",
        "Set-Cookie: session=REVIEW_COOKIE",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_nested_encoded_separators_fail_closed() -> None:
    for text in (
        "Bearer%2525255Cu0020REVIEW_NESTED",
        "Bearer%252525255Cu0020REVIEW_DEEP",
        "https://user%252525253Apass%2525252540host/REVIEW_DEEP_URL",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_extended_html_entities_fail_closed() -> None:
    for text in (
        "Bearer&bsol;u0020REVIEW_BSOL",
        "accessToken&ratio;REVIEW_RATIO_ENTITY",
        "accessToken&Colon;REVIEW_COLON_ENTITY",
        "user&period;name=REVIEW_PERIOD_ENTITY",
        "`accessToken&grave;: REVIEW_GRAVE",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_extra_colon_lookalikes_fail_closed() -> None:
    for text in (
        "accessToken\ua789REVIEW_A789",
        "accessToken\u02d0REVIEW_02D0",
        "accessToken\u1361REVIEW_1361",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted


def test_extended_homoglyph_keys_fail_closed() -> None:
    for text in (
        "p\u0251ssword=REVIEW_LATIN_ALPHA",
        "p\u03b1ssword=REVIEW_GREEK_ALPHA",
        "tok\u03b5n=REVIEW_GREEK_EPS",
        "\u03c4oken=REVIEW_GREEK_TAU",
    ):
        redacted = redact_secrets(text)

        assert redacted == "[redacted sensitive text]"
        assert "REVIEW" not in redacted


def test_standalone_auth_scheme_values_are_redacted() -> None:
    for text in (
        "Basic dXNlcjpwYXNzREVIEW_BASIC",
        "Digest REVIEW_DIGEST",
    ):
        redacted = redact_secrets(text)

        assert "REVIEW" not in redacted
        assert "***" in redacted


@pytest.mark.parametrize("layers", [6, 10, 20, 100])
@pytest.mark.parametrize("key", [r"access\u0054oken", r"\u0061\u0063\u0063\u0065\u0073\u0073\u0054\u006f\u006b\u0065\u006e"])
def test_layer_independent_normalization_detects_partial_and_full_unicode_sensitive_keys(layers: int, key: str) -> None:
    escaped = f'{{"{key}":"REVIEW_LAYER_INDEPENDENT"}}'
    escaped = _escape_quotes_with_backslash_run(escaped, layers)

    redacted = redact_secrets(f"gateway {escaped} trailing")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_LAYER_INDEPENDENT" not in redacted


@pytest.mark.parametrize("layers", [5, 6, 10])
@pytest.mark.parametrize(
    "key",
    [r"access\u0054oken", r"\u0061\u0063\u0063\u0065\u0073\u0073\u0054\u006f\u006b\u0065\u006e"],
)
def test_iterative_quote_escaping_with_unicode_sensitive_key_fails_closed(layers: int, key: str) -> None:
    escaped = f'{{"{key}":"REVIEW_ITERATIVE_UNICODE"}}'
    for _ in range(layers):
        escaped = _escape_quotes_once(escaped)

    redacted = redact_secrets(f"gateway {escaped} trailing")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_ITERATIVE_UNICODE" not in redacted


@pytest.mark.parametrize("layers", [6, 10])
def test_iterative_quote_escaping_unicode_key_protects_four_boundaries(tmp_path, capsys, layers: int) -> None:
    escaped = r'{"access\u0054oken":"REVIEW_ITERATIVE_BOUNDARY"}'
    for _ in range(layers):
        escaped = _escape_quotes_once(escaped)
    error_text = f"gateway {escaped} trailing"

    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    for text in (
        report.results[0].capabilities[0].error.message,
        machine_path.read_text(encoding="utf-8"),
        human_path.read_text(encoding="utf-8"),
        stdout,
    ):
        assert "REVIEW_ITERATIVE_BOUNDARY" not in text
        assert "[redacted sensitive text]" in text


@pytest.mark.parametrize("separator", [r"\u003A", r"\u003D"])
def test_layer_independent_normalization_detects_unicode_colon_and_equal(separator: str) -> None:
    escaped = f"accessToken{separator}REVIEW_UNICODE_SEPARATOR"

    assert redact_secrets(f"gateway {escaped}") == "[redacted sensitive text]"


def test_layer_independent_normalization_handles_unicode_encoded_backslash_nesting_and_odd_even_runs() -> None:
    unicode_backslash = r'gateway {\u005c\u0022accessToken\u005c\u0022:\u005c\u0022REVIEW_UNICODE_BACKSLASH\u005c\u0022}'
    odd_run = 'gateway {' + ("\\" * 7) + '"accessToken' + ("\\" * 7) + '":' + ("\\" * 7) + '"REVIEW_ODD_RUN' + ("\\" * 7) + '"}'
    even_run = 'gateway {' + ("\\" * 8) + '"accessToken' + ("\\" * 8) + '":' + ("\\" * 8) + '"REVIEW_EVEN_RUN' + ("\\" * 8) + '"}'

    for text, secret in ((unicode_backslash, "REVIEW_UNICODE_BACKSLASH"), (odd_run, "REVIEW_ODD_RUN"), (even_run, "REVIEW_EVEN_RUN")):
        redacted = redact_secrets(text)
        assert redacted == "[redacted sensitive text]"
        assert secret not in redacted


def test_layer_independent_normalization_preserves_fully_escaped_safe_sentinel_but_rejects_evil_suffix() -> None:
    safe = '{"accessToken":"***"}'
    evil = '{"accessToken":"***evil"}'
    safe = _escape_quotes_with_backslash_run(safe, 100)
    evil = _escape_quotes_with_backslash_run(evil, 100)

    assert redact_secrets(f"gateway {safe}") == f"gateway {safe}"
    assert redact_secrets(f"gateway {evil}") == "[redacted sensitive text]"


def test_layer_independent_normalization_is_bounded_on_large_deeply_escaped_text_and_protects_four_boundaries(tmp_path, capsys) -> None:
    escaped = r'{"access\u0054oken":"REVIEW_DEEP_UNICODE_PROVIDER"}'
    escaped = _escape_quotes_with_backslash_run(escaped, 100)
    error_text = ("x" * 100_000) + f" gateway {escaped}"
    started = monotonic()
    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    assert monotonic() - started < 3
    for text in (report.results[0].capabilities[0].error.message, machine_path.read_text(encoding="utf-8"), human_path.read_text(encoding="utf-8"), stdout):
        assert "REVIEW_DEEP_UNICODE_PROVIDER" not in text
        assert "[redacted sensitive text]" in text


def test_run_result_matches_the_shared_contract() -> None:
    result = ProbeRunner().run(
        [StaticProvider("healthy", [_static_capability("bars", CapabilityStatus.PASS, row_count=2)])]
    ).results[0]
    validate_contract("provider-run-result.schema.json", result.to_dict())


@pytest.mark.parametrize(
    "text",
    [
        "https://user:pass@REVIEW_URL_ASCII/path",  # Synthetic test credential or marker.
        "https://user&ratio;pass@REVIEW_URL_RATIO/path",
        "https://user&#x2236;pass@REVIEW_URL_NUM2236/path",
        "https://user\uA789pass@REVIEW_URL_A789/path",
        "https://user\u02D0pass@REVIEW_URL_02D0/path",
        "https://user\u1361pass@REVIEW_URL_1361/path",
        "https://user\u2236pass@REVIEW_URL_2236/path",
        "https://user\uFF1Apass@REVIEW_URL_FF1A/path",
        "https://user\uFE55pass@REVIEW_URL_FE55/path",
    ],
)
def test_terra7_url_userinfo_homoglyph_colons_fail_closed(text: str) -> None:
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW" not in redacted


@pytest.mark.parametrize(
    "separator",
    [":", "&ratio;", "&#x2236;", "\uA789", "\u02D0", "\u1361", "\u2236", "\uFF1A", "\uFE55"],
)
def test_terra7_url_userinfo_homoglyph_colons_in_complete_json_fail_closed(separator: str) -> None:
    text = '{"message":"https://user' + separator + 'pass@REVIEW_URL_JSON/path"}'

    redacted = redact_secrets(text)

    assert "REVIEW_URL_JSON" not in redacted
    assert json.loads(redacted)["message"] == "[redacted sensitive text]"


@pytest.mark.parametrize("marker", ["\u200E", "\u00AD", "\u2061", "\u202E", "\u034F", "\u061C"])
def test_terra7_cf_format_char_between_sensitive_key_and_separator_fails_closed(marker: str) -> None:
    redacted = redact_secrets(f"accessToken{marker}=REVIEW_CF_SEP")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_CF_SEP" not in redacted


@pytest.mark.parametrize("marker", ["\u200E", "\u00AD", "\u034F", "\u202A"])
def test_terra7_cf_format_char_inside_sensitive_key_fails_closed(marker: str) -> None:
    redacted = redact_secrets(f"pass{marker}word=REVIEW_CF_KEY")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_CF_KEY" not in redacted


def test_terra7_cf_format_char_inside_complete_json_value_fails_closed() -> None:
    text = '{"message":"accessToken\u200e=REVIEW_JSON_LRM"}'

    redacted = redact_secrets(text)

    assert "REVIEW_JSON_LRM" not in redacted
    assert json.loads(redacted)["message"] == "[redacted sensitive text]"


@pytest.mark.parametrize(
    "key",
    [
        "passw\u00e9rd",
        "p\u00e4ssword",
        "pa\u0448word",
        "pas\U0001D421ord",
        "p@ssword",
        "passw0rd",
        "p4ssword",
        "t0ken",
        "password\u0661",
    ],
)
def test_terra7_accent_homoglyph_leet_and_unicode_digit_keys_fail_closed(key: str) -> None:
    redacted = redact_secrets(f"{key}=REVIEW_NORM_KEY")

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW_NORM_KEY" not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "auth_key=REVIEW_AUTH_KEY",
        "authkey=REVIEW_AUTHKEY",
        "consumer_key=REVIEW_CONSUMER",
        "session_key=REVIEW_SESSION",
        "master_key=REVIEW_MASTER",
        "signing_key=REVIEW_SIGNING",
        "encryption_key=REVIEW_ENC",
        "X-Auth-Key: REVIEW_XAUTHKEY",
        "X-Auth: REVIEW_XAUTH",
    ],
)
def test_terra7_auth_and_key_compound_assignments_fail_closed(text: str) -> None:
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW" not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "Bearer\u200BeyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9REVIEW_ZWSP",
        "Bearer\u200EReview_LRM",
        "Basic\u200BdXNlcjpwYXNzREVIEW_BASIC_ZWSP",
        "Digest\u00ADREVIEW_DIGEST",
        "Bearer\u034FREVIEW_CGJ",
    ],
)
def test_terra7_literal_invisible_separators_in_auth_schemes_are_redacted(text: str) -> None:
    redacted = redact_secrets(text)

    assert "REVIEW" not in redacted
    assert "***" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "\u5bc6\u7801=REVIEW_CHINESE_PASSWORD",
        "\u5bc6\u94a5\uff1aREVIEW_CHINESE_SECRET",
        "\u4ee4\u724c\uff1aREVIEW_CHINESE_TOKEN",
        "\u53e3\u4ee4=REVIEW_CHINESE_PASSPHRASE",
        "\u79c1\u94a5=REVIEW_CHINESE_PRIVATE_KEY",
    ],
)
def test_terra7_chinese_credential_keys_fail_closed(text: str) -> None:
    redacted = redact_secrets(text)

    assert redacted == "[redacted sensitive text]"
    assert "REVIEW" not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "\u8d26\u6237\uff1a\u666e\u901a\u6587\u672c",
        "token_count=42",
        "user_profile.name=alice",
        "<name>alice</name>",
    ],
)
def test_terra7_non_sensitive_controls_are_preserved(text: str) -> None:
    assert redact_secrets(text) == text


def test_terra7_safe_sentinel_and_auth_semantics_are_preserved() -> None:
    assert redact_secrets("accessToken: ***") == "accessToken: ***"
    assert redact_secrets("accessToken = ***") == "accessToken = ***"
    assert redact_secrets("accessToken=***") == "accessToken=***"
    assert redact_secrets("Bearer xyz") == "Bearer ***"
    assert redact_secrets("Authorization: Basic dXNlcjpwYXNz") == "[redacted sensitive text]"


def test_terra7_all_leak_surfaces_are_blocked_across_four_boundaries(tmp_path, capsys) -> None:
    error_text = (
        "https://user&ratio;pass@REVIEW_URL_RATIO/path "
        "https://user\uA789pass@REVIEW_URL_LITERAL/path "
        "accessToken\u200E=REVIEW_LRM_SEP "
        "passw0rd=REVIEW_LEET0 "
        "authkey=REVIEW_AUTHKEY "
        "Bearer\u200BeyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9REVIEW_ZWSP "
        "\u5bc6\u7801=REVIEW_CHINESE_PASSWORD"
    )

    report = ProbeRunner().run([StaticProvider("unsafe", ProviderError(ErrorCategory.NETWORK, error_text))])
    machine_path, human_path = ProbeRunner().write_reports(report, tmp_path)
    _emit("PARTIAL_FAILURE", 2, message=error_text)
    stdout = capsys.readouterr().out

    markers = (
        "REVIEW_URL_RATIO",
        "REVIEW_URL_LITERAL",
        "REVIEW_LRM_SEP",
        "REVIEW_LEET0",
        "REVIEW_AUTHKEY",
        "REVIEW_ZWSP",
        "REVIEW_CHINESE_PASSWORD",
    )
    for text in (
        report.results[0].capabilities[0].error.message,
        machine_path.read_text(encoding="utf-8"),
        human_path.read_text(encoding="utf-8"),
        stdout,
    ):
        for marker in markers:
            assert marker not in text
