"""TDX pytdx provider tests use a fake protocol client; live acceptance is separate."""

from __future__ import annotations

import pytest

from market_monitor.providers import AssetType, CapabilityStatus, ErrorCategory, Market, ProviderError
from market_monitor.providers.pytdx import TdxProvider, _iso_datetime


class FakeTdxApi:
    def __init__(
        self,
        *,
        bars: object | None = None,
        quotes: object | None = None,
        security_list: object | None = None,
        count: int = 100,
        connect_error: Exception | None = None,
    ) -> None:
        self.bars = bars if bars is not None else [
            {
                "open": 1.0,
                "close": 1.1,
                "high": 1.2,
                "low": 0.9,
                "vol": 100,
                "amount": 1000,
                "year": 2026,
                "month": 8,
                "day": 7,
                "hour": 15,
                "minute": 0,
                "datetime": "2026-08-07 15:00",
            }
        ]
        self.quotes = quotes if quotes is not None else [{"code": "600519", "price": 1309.22}]
        self.security_list = security_list if security_list is not None else [{"code": "600519", "name": "贵州茅台"}]
        self.count = count
        self.connect_error = connect_error
        self.disconnected = False

    def connect(self, host: str, port: int, time_out: float | None = None) -> "FakeTdxApi":
        if self.connect_error is not None:
            raise self.connect_error
        return self

    def disconnect(self) -> None:
        self.disconnected = True

    def get_security_count(self, market: int) -> int:
        return self.count

    def get_security_bars(self, category: int, market: int, code: str, start: int, count: int) -> object:
        return self.bars

    def get_security_quotes(self, pairs: object) -> object:
        return self.quotes

    def get_security_list(self, market: int, start: int) -> object:
        return self.security_list


def test_probe_reports_pass_for_working_protocol() -> None:
    capabilities = TdxProvider(api_factory=FakeTdxApi).probe_capabilities()

    assert [capability.name for capability in capabilities] == [
        "tdx-connect",
        "tdx-a-share-instruments",
        "tdx-spot-quotes",
        "tdx-stock-sh600519-1d",
        "tdx-stock-sh600519-30m",
        "tdx-index-sh000001-1d",
        "tdx-etf-sh510300-1d",
    ]
    assert all(capability.status is CapabilityStatus.PASS for capability in capabilities)
    stock = capabilities[3]
    assert stock.registration.request.period == "1d"
    assert stock.registration.request.market is Market.CN
    assert stock.registration.request.asset_type is AssetType.STOCK
    index = capabilities[5]
    assert index.registration.request.asset_type is AssetType.INDEX
    etf = capabilities[6]
    assert etf.registration.request.asset_type is AssetType.ETF


def test_probe_connect_failure_is_a_single_network_capability() -> None:
    provider = TdxProvider(api_factory=lambda: FakeTdxApi(connect_error=ConnectionError("refused")))

    capabilities = provider.probe_capabilities()

    assert len(capabilities) == 1
    assert capabilities[0].name == "tdx-connect"
    assert capabilities[0].status is CapabilityStatus.FAILED
    assert capabilities[0].error is not None
    assert capabilities[0].error.category is ErrorCategory.NETWORK


def test_bars_are_normalised() -> None:
    result = TdxProvider(api_factory=FakeTdxApi).fetch_bars()

    assert result.records == [
        {
            "date": "2026-08-07",
            "datetime": "2026-08-07 15:00",
            "code": "600519",
            "market": 1,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": 1.1,
            "volume": 100.0,
            "amount": 1000.0,
            "open_interest": None,
        }
    ]
    assert result.earliest == "2026-08-07T00:00:00+08:00"
    assert result.latest == "2026-08-07T00:00:00+08:00"


def test_empty_bars_raise_no_coverage() -> None:
    provider = TdxProvider(api_factory=lambda: FakeTdxApi(bars=[]))

    with pytest.raises(ProviderError) as exc_info:
        provider.fetch_bars()

    assert exc_info.value.category is ErrorCategory.NO_COVERAGE


def test_missing_security_list_is_a_failure_not_crash() -> None:
    provider = TdxProvider(api_factory=lambda: FakeTdxApi(security_list=[]))

    capabilities = provider.probe_capabilities()

    assert capabilities[1].name == "tdx-a-share-instruments"
    assert capabilities[1].status is CapabilityStatus.FAILED
    assert capabilities[1].error is not None
    assert capabilities[2].status is CapabilityStatus.PASS


def test_fetch_instruments_returns_market_tagged_rows() -> None:
    result = TdxProvider(api_factory=FakeTdxApi).fetch_instruments()

    assert result.records[0]["code"] == "600519"
    assert result.records[0]["market"] == 1
    assert result.records[1]["market"] == 0


def test_configure_overrides_servers() -> None:
    provider = TdxProvider(api_factory=FakeTdxApi)

    provider.configure({"TDX_SERVERS": "10.0.0.1:7709, 10.0.0.2"})

    assert ("10.0.0.1", 7709) in provider._servers
    assert ("10.0.0.2", 7709) in provider._servers


def test_corrupted_tdx_calendar_date_is_rejected_before_capability_persistence() -> None:
    with pytest.raises(ValueError):
        _iso_datetime("6427-90-45")


def test_corrupted_tdx_bar_is_skipped_without_losing_valid_index_window() -> None:
    valid = FakeTdxApi().bars[0]
    corrupt = {**valid, "month": 90, "day": 45}
    provider = TdxProvider(api_factory=lambda: FakeTdxApi(bars=[corrupt, valid]))

    capabilities = provider.probe_capabilities()

    index = next(item for item in capabilities if item.name == "tdx-index-sh000001-1d")
    assert index.status is CapabilityStatus.PASS
    assert index.row_count == 1
