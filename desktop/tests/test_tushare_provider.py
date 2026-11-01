"""Tushare mapping tests use fixed responses and do not represent live acceptance."""

from __future__ import annotations

from datetime import date

from market_monitor.providers import (
    CapabilityStatus,
    ErrorCategory,
    ProbeRunner,
)
from market_monitor.providers.tushare import TushareProvider, _provider_error


class FakeTusharePro:
    def __init__(
        self,
        *,
        minute_error: Exception | None = None,
        financial_error: Exception | None = None,
    ) -> None:
        self.minute_error = minute_error
        self.financial_error = financial_error
        self.calls: list[str] = []

    def trade_cal(self, **_: object):
        self.calls.append("trade_cal")
        return [{"exchange": "SSE", "cal_date": "20260803", "is_open": "1"}]

    def daily(self, **_: object):
        self.calls.append("daily")
        return [{"ts_code": "600519.SH", "trade_date": "20260803", "close": 100.0}]

    def stock_basic(self, **_: object):
        self.calls.append("stock_basic")
        return [{"ts_code": "600519.SH", "symbol": "600519", "name": "fixed"}]

    def income(self, **_: object):
        self.calls.append("income")
        if self.financial_error:
            raise self.financial_error
        return [{"ts_code": "600519.SH", "end_date": "20260630", "n_income": 100.0}]

    def stk_mins(self, **_: object):
        self.calls.append("stk_mins")
        if self.minute_error:
            raise self.minute_error
        return [{"ts_code": "600519.SH", "trade_time": "2026-08-03 10:30:00", "close": 100.0}]

    def user(self, **_: object):
        self.calls.append("user")
        return [{"email": "local@example.com", "point": 5000}]


class FakeTushareSdk:
    def __init__(self, pro: FakeTusharePro | None = None) -> None:
        self.pro = pro if pro is not None else FakeTusharePro()
        self.token: str | None = None

    def set_token(self, token: str) -> None:
        self.token = token

    def pro_api(self) -> FakeTusharePro:
        return self.pro


def test_tushare_probe_reports_all_capabilities_with_fixed_responses() -> None:
    sdk = FakeTushareSdk()
    provider = TushareProvider(
        sdk=sdk,
        token="local-token",  # Synthetic test credential or marker.
        today=lambda: date(2026, 8, 3),
    )

    capabilities = provider.probe_capabilities()

    assert sdk.token == "local-token"
    assert [capability.registration.id for capability in capabilities] == [
        "tushare-calendar",
        "tushare-daily",
        "tushare-stock-basic",
        "tushare-financial",
        "tushare-minute",
        "tushare-account",
    ]
    assert all(capability.status is CapabilityStatus.PASS for capability in capabilities)
    assert all(capability.registration.request.operation.value for capability in capabilities)
    assert capabilities[1].earliest == "2026-08-03T00:00:00+08:00"
    assert capabilities[4].latest == "2026-08-03T10:30:00+08:00"
    assert "points=5000" in (capabilities[5].detail or "")
    assert sdk.pro.calls == ["trade_cal", "daily", "stock_basic", "income", "stk_mins", "user"]


def test_missing_local_token_is_a_configuration_block_without_touching_the_sdk() -> None:
    sdk = FakeTushareSdk()
    provider = TushareProvider(sdk=sdk, token="")

    result = ProbeRunner().run([provider]).results[0]

    assert [capability.registration.id for capability in result.capabilities] == [
        "configuration-tushare-token"
    ]
    assert result.capabilities[0].status is CapabilityStatus.BLOCKED
    assert result.capabilities[0].error and result.capabilities[0].error.category is ErrorCategory.CONFIGURATION
    assert sdk.pro.calls == []


def test_permission_and_points_errors_are_reported_per_interface_without_stopping_other_probes() -> None:
    provider = TushareProvider(
        sdk=FakeTushareSdk(
            FakeTusharePro(
                minute_error=Exception("抱歉，您没有访问该接口的权限"),
                financial_error=Exception("您的积分不足"),
            )
        ),
        token="local-token",  # Synthetic test credential or marker.
        today=lambda: date(2026, 8, 3),
    )

    capabilities = provider.probe_capabilities()

    assert capabilities[0].status is CapabilityStatus.PASS
    assert capabilities[1].status is CapabilityStatus.PASS
    assert capabilities[2].status is CapabilityStatus.PASS
    assert capabilities[3].status is CapabilityStatus.FAILED
    assert "[RATE_LIMIT]" in (capabilities[3].detail or "")
    assert capabilities[4].status is CapabilityStatus.FAILED
    assert "[NO_COVERAGE]" in (capabilities[4].detail or "")
    assert capabilities[5].status is CapabilityStatus.PASS


def test_tushare_error_classification() -> None:
    assert _provider_error(RuntimeError("invalid token")).category is ErrorCategory.AUTHENTICATION
    assert _provider_error(RuntimeError("每分钟最多访问该接口X次")).category is ErrorCategory.QUOTA
    assert _provider_error(RuntimeError("系统繁忙，请稍后重试")).category is ErrorCategory.NETWORK
