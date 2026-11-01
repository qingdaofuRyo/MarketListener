"""Unit tests for JoinQuant mapping; fixed SDK responses are not source acceptance."""

from __future__ import annotations

from datetime import date

import pytest

from market_monitor.providers import CapabilityStatus, ErrorCategory, ProbeRunner, ProviderError
from market_monitor.providers.joinquant import JoinQuantProvider, _provider_error


class FakeJoinQuantSdk:
    def __init__(self, price_error: Exception | None = None) -> None:
        self.price_error = price_error
        self.auth_calls: list[tuple[str, str]] = []
        self.price_calls: list[tuple[str, str]] = []

    def auth(self, username: str, password: str) -> None:
        self.auth_calls.append((username, password))

    def get_query_count(self) -> dict[str, int]:
        return {"total": 10000, "spare": 9999}

    def get_trade_days(self, **_: object) -> list[date]:
        return [date(2026, 8, 3)]

    def get_all_securities(self, types=None, **_: object):
        if types == ["futures"]:
            return [{"code": "IF2608.CCFX"}]
        return [{"code": "600519.XSHG"}]

    def get_price(self, symbol: str, *, frequency: str, **_: object):
        self.price_calls.append((symbol, frequency))
        if self.price_error:
            raise self.price_error
        return [{"open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5, "volume": 10.0}]

    def get_money_flow(self, *_: object, **__: object):
        return [{"net_amount_main": 1.0}]

    def get_extras(self, *_: object, **__: object):
        return [{"factor": 1.0}]


def test_joinquant_probe_uses_source_local_symbol_mapping_and_real_period_names() -> None:
    sdk = FakeJoinQuantSdk()
    provider = JoinQuantProvider(
        sdk=sdk,
        username="local-user",
        password="local-password",  # Synthetic test credential or marker.
        today=lambda: date(2026, 8, 3),
    )

    capabilities = provider.probe_capabilities()

    assert sdk.auth_calls == [("local-user", "local-password")]
    assert len(capabilities) == 21
    assert all(capability.status is CapabilityStatus.PASS for capability in capabilities)
    assert capabilities[1].registration.id == "jqdata-query-quota"
    assert "total=10000; spare=9999" in (capabilities[1].detail or "")
    assert ("600519.XSHG", "daily") in sdk.price_calls
    assert ("000001.XSHE", "30m") in sdk.price_calls
    assert ("510300.XSHG", "1m") in sdk.price_calls
    assert ("IF2608.CCFX", "daily") in sdk.price_calls


def test_joinquant_quota_failure_is_reported_without_suppressing_other_capabilities() -> None:
    sdk = FakeJoinQuantSdk()
    sdk.get_query_count = lambda: {"total": 10000}  # type: ignore[method-assign]
    provider = JoinQuantProvider(
        sdk=sdk,
        username="local-user",
        password="local-password",  # Synthetic test credential or marker.
        today=lambda: date(2026, 8, 3),
    )

    capabilities = provider.probe_capabilities()

    assert capabilities[0].status is CapabilityStatus.PASS
    assert capabilities[1].status is CapabilityStatus.FAILED
    assert "[PROVIDER]" in (capabilities[1].detail or "")
    assert all(capability.status is CapabilityStatus.PASS for capability in capabilities[2:])


def test_missing_local_credentials_is_a_configuration_failure() -> None:
    provider = JoinQuantProvider(sdk=FakeJoinQuantSdk(), username="", password="")

    with pytest.raises(ProviderError) as error:
        provider.probe_capabilities()

    assert error.value.category is ErrorCategory.CONFIGURATION
    assert "JQDATA_PASSWORD" in error.value.message


def test_runner_reports_missing_jqdata_credentials_as_independent_configuration_blocks() -> None:
    provider = JoinQuantProvider(sdk=FakeJoinQuantSdk(), username="", password="")

    result = ProbeRunner().run([provider]).results[0]

    assert [capability.registration.id for capability in result.capabilities] == [
        "configuration-jqdata-username",
        "configuration-jqdata-password",
    ]
    assert all(capability.status is CapabilityStatus.BLOCKED for capability in result.capabilities)
    assert all(capability.error and capability.error.category is ErrorCategory.CONFIGURATION for capability in result.capabilities)


def test_rate_limit_is_reported_per_capability_without_stopping_other_probes() -> None:
    provider = JoinQuantProvider(
        sdk=FakeJoinQuantSdk(RuntimeError("HTTP 429 too many requests")),
        username="local-user",
        password="local-password",  # Synthetic test credential or marker.
    )

    capabilities = provider.probe_capabilities()

    assert capabilities[0].status is CapabilityStatus.PASS
    assert any("[RATE_LIMIT]" in (capability.detail or "") for capability in capabilities[1:])


def test_localized_socket_error_is_classified_as_network() -> None:
    error = _provider_error(RuntimeError("网络接收错误 WinError 10057"))

    assert error.category is ErrorCategory.NETWORK
