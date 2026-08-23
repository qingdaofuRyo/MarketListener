"""Real 通达信 (TDX) provider built on the pytdx protocol client.

pytdx speaks the public TDX market-data protocol directly, so the third-party
tdx_quant wrapper is not required for A-share/ETF/index bars and quotes.
Servers are tried in order and can be overridden with the TDX_SERVERS local
setting (comma-separated host:port pairs).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Callable, Mapping, Sequence

from .base import (
    AssetType,
    Capability,
    CapabilityRegistration,
    CapabilityStatus,
    ErrorCategory,
    FetchResult,
    Market,
    Provider,
    ProviderError,
    ProviderOperation,
    ProviderRequest,
    SourceDescription,
)
from .joinquant import _error_detail, _provider_error

_DEFAULT_SERVERS: tuple[tuple[str, int], ...] = (
    ("60.191.117.167", 7709),
    ("218.75.126.9", 7709),
    ("115.238.56.198", 7709),
)

_BAR_CATEGORIES = {
    "1m": 7,
    "5m": 0,
    "15m": 1,
    "30m": 2,
    "1h": 3,
    "1d": 4,
    "1w": 5,
    "1mo": 6,
}


def _parse_servers(value: str | None) -> tuple[tuple[str, int], ...] | None:
    if not value:
        return None
    servers: list[tuple[str, int]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port_text = item.partition(":")
        try:
            port = int(port_text) if port_text else 7709
        except ValueError:
            continue
        servers.append((host, port))
    return tuple(servers) or None


class TdxProvider(Provider):
    name = "pytdx"
    source_description = SourceDescription(
        "pytdx",
        "通达信 pytdx",
        "TDX public market protocol: A-share, ETF and index bars plus quotes",
    )

    def __init__(
        self,
        *,
        api_factory: Callable[[], Any] | None = None,
        servers: Sequence[tuple[str, int]] | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._api_factory = api_factory
        self._servers = list(servers or _parse_servers(os.getenv("TDX_SERVERS")) or _DEFAULT_SERVERS)
        self._timeout_seconds = timeout_seconds

    def configure(self, values: Mapping[str, str]) -> None:
        configured = _parse_servers(values.get("TDX_SERVERS"))
        if configured:
            self._servers = list(configured)

    def configuration_requirements(self) -> Sequence[Any]:
        return ()

    def probe_capabilities(self) -> Sequence[Capability]:
        try:
            api, host = self._connect()
        except ProviderError as error:
            return (self._capability("tdx-connect", "connect to a TDX market server", CapabilityStatus.FAILED, error=error),)
        try:
            return (
                self._probe_health(api, host),
                self._probe_instruments(api),
                self._probe_quotes(api),
                self._probe_bars(api, "tdx-stock-sh600519-1d", "600519", 1, "1d", AssetType.STOCK),
                self._probe_bars(api, "tdx-stock-sh600519-30m", "600519", 1, "30m", AssetType.STOCK),
                self._probe_bars(api, "tdx-index-sh000001-1d", "000001", 1, "1d", AssetType.INDEX),
                self._probe_bars(api, "tdx-etf-sh510300-1d", "510300", 1, "1d", AssetType.ETF),
            )
        finally:
            api.disconnect()

    def fetch_instruments(self) -> FetchResult:
        api, host = self._connect()
        try:
            rows: list[Mapping[str, Any]] = []
            for market in (1, 0):
                rows.extend(self._security_list(api, market))
            if not rows:
                raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX server returned no security-list rows")
            return FetchResult(records=rows, detail=f"TDX {host} A-share security list")
        finally:
            api.disconnect()

    def fetch_bars(self) -> FetchResult:
        api, host = self._connect()
        try:
            records = self._bars(api, "600519", 1, "1d", 250)
            dates = [str(row["date"]) for row in records]
            return FetchResult(
                records=records,
                earliest=_iso_datetime(min(dates)),
                latest=_iso_datetime(max(dates)),
                detail=f"TDX {host} 600519 daily bars",
            )
        finally:
            api.disconnect()

    def fetch_indicators(self) -> FetchResult:
        raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX protocol does not expose standalone indicator datasets")

    def fetch_calendar(self) -> FetchResult:
        raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX protocol has no standalone calendar; derive it from bars")

    def health_check(self) -> FetchResult:
        api, host = self._connect()
        try:
            count = api.get_security_count(1)
            if not count:
                raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX server returned zero SH securities")
            return FetchResult(
                records=[{"market": 1, "security_count": count}],
                detail=f"TDX {host} quote protocol responded",
            )
        finally:
            api.disconnect()

    def _connect(self) -> tuple[Any, str]:
        failures: list[str] = []
        for host, port in self._servers:
            api = self._new_api()
            try:
                connected = api.connect(host, port, time_out=self._timeout_seconds)
            except Exception as error:
                failures.append(f"{host}:{port} {type(error).__name__}")
                continue
            if not connected:
                failures.append(f"{host}:{port} refused")
                continue
            return api, f"{host}:{port}"
        raise ProviderError(ErrorCategory.NETWORK, "; ".join(failures) or "no TDX servers configured")

    def _new_api(self) -> Any:
        if self._api_factory is not None:
            return self._api_factory()
        from pytdx.hq import TdxHq_API

        return TdxHq_API()

    def _security_list(self, api: Any, market: int) -> list[Mapping[str, Any]]:
        rows = api.get_security_list(market, 0)
        if not rows:
            return []
        return [
            {"market": market, "code": str(row.get("code", "")), "name": row.get("name")}
            for row in rows
        ]

    def _bars(self, api: Any, code: str, market: int, period: str, count: int) -> list[Mapping[str, Any]]:
        category = _BAR_CATEGORIES[period]
        rows = api.get_security_bars(category, market, code, 0, count)
        if not rows:
            raise ProviderError(ErrorCategory.NO_COVERAGE, f"TDX returned zero {period} bars for {code}")
        records: list[Mapping[str, Any]] = []
        for row in rows:
            try:
                records.append(_normalise_bar(row, market, code))
            except (KeyError, TypeError, ValueError):
                # Individual public TDX nodes occasionally mix a corrupt
                # calendar tuple into otherwise valid index pages.  One bad
                # record must not make the entire chart window unavailable.
                continue
        if not records:
            raise ProviderError(
                ErrorCategory.FIELD_CHANGE,
                f"TDX returned no valid {period} bars for {code}",
            )
        return records

    def _probe_health(self, api: Any, host: str) -> Capability:
        try:
            count = api.get_security_count(1)
            if not count:
                raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX server returned zero SH securities")
            return self._capability(
                "tdx-connect",
                "connect and count securities",
                CapabilityStatus.PASS,
                operation=ProviderOperation.HEALTH_CHECK,
                row_count=1,
            )
        except Exception as error:
            wrapped = error if isinstance(error, ProviderError) else _provider_error(error)
            return self._capability(
                "tdx-connect",
                "connect and count securities",
                CapabilityStatus.FAILED,
                operation=ProviderOperation.HEALTH_CHECK,
                error=wrapped,
            )

    def _probe_instruments(self, api: Any) -> Capability:
        try:
            rows = self._security_list(api, 1) + self._security_list(api, 0)
            if not rows:
                raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX server returned no security-list rows")
            return self._capability(
                "tdx-a-share-instruments",
                "A-share security list",
                CapabilityStatus.PASS,
                operation=ProviderOperation.INSTRUMENTS,
                row_count=len(rows),
            )
        except Exception as error:
            wrapped = error if isinstance(error, ProviderError) else _provider_error(error)
            return self._capability(
                "tdx-a-share-instruments",
                "A-share security list",
                CapabilityStatus.FAILED,
                operation=ProviderOperation.INSTRUMENTS,
                error=wrapped,
            )

    def _probe_quotes(self, api: Any) -> Capability:
        try:
            rows = api.get_security_quotes([(1, "600519"), (0, "000001")])
            if not rows:
                raise ProviderError(ErrorCategory.NO_COVERAGE, "TDX returned zero quotes")
            return self._capability(
                "tdx-spot-quotes",
                "A-share spot quotes",
                CapabilityStatus.PASS,
                row_count=len(rows),
            )
        except Exception as error:
            wrapped = error if isinstance(error, ProviderError) else _provider_error(error)
            return self._capability(
                "tdx-spot-quotes",
                "A-share spot quotes",
                CapabilityStatus.FAILED,
                error=wrapped,
            )

    def _probe_bars(
        self,
        api: Any,
        capability_id: str,
        code: str,
        market: int,
        period: str,
        asset_type: AssetType,
    ) -> Capability:
        try:
            records = self._bars(api, code, market, period, 60)
            return self._capability(
                capability_id,
                f"{code} {period} bars",
                CapabilityStatus.PASS,
                operation=ProviderOperation.BARS,
                asset_type=asset_type,
                period=period,
                instrument=code,
                row_count=len(records),
                earliest=_iso_datetime(records[0]["date"]),
                latest=_iso_datetime(records[-1]["date"]),
            )
        except Exception as error:
            wrapped = error if isinstance(error, ProviderError) else _provider_error(error)
            return self._capability(
                capability_id,
                f"{code} {period} bars",
                CapabilityStatus.FAILED,
                operation=ProviderOperation.BARS,
                asset_type=asset_type,
                period=period,
                instrument=code,
                error=wrapped,
            )

    def _capability(
        self,
        capability_id: str,
        description: str,
        status: CapabilityStatus,
        *,
        operation: ProviderOperation = ProviderOperation.OTHER,
        market: Market = Market.CN,
        asset_type: AssetType = AssetType.STOCK,
        period: str | None = None,
        instrument: str | None = None,
        row_count: int | None = None,
        earliest: str | None = None,
        latest: str | None = None,
        error: ProviderError | None = None,
    ) -> Capability:
        if operation in (ProviderOperation.HEALTH_CHECK, ProviderOperation.CALENDAR):
            asset_type = AssetType.GENERAL
            period = None
            instrument = None
        request = ProviderRequest(
            operation,
            market=market,
            asset_type=asset_type,
            period=period,
            instrument=instrument,
        )
        return Capability(
            capability_id,
            status,
            detail=_error_detail(error) if error is not None else description,
            row_count=row_count,
            earliest=earliest,
            latest=latest,
            registration=CapabilityRegistration(capability_id, description, request),
            error=error,
        )


def _normalise_bar(row: Mapping[str, Any], market: int, code: str) -> Mapping[str, Any]:
    calendar_date = date(int(row["year"]), int(row["month"]), int(row["day"]))
    bar_date = calendar_date.isoformat()
    normalised: dict[str, Any] = {
        "date": bar_date,
        "datetime": row.get("datetime"),
        "code": code,
        "market": market,
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row.get("vol", 0)),
        "amount": float(row.get("amount", 0)),
        "open_interest": row.get("position") if row.get("position") is not None else None,
    }
    if "up_count" in row:
        normalised["up_count"] = row["up_count"]
        normalised["down_count"] = row["down_count"]
    return normalised


def _iso_datetime(bar_date: str) -> str:
    """TDX bars only carry a calendar date; treat it as an Asia/Shanghai midnight."""
    # Some public TDX hosts occasionally return a corrupted calendar tuple.
    # Reject it here so invalid evidence cannot enter a capability report.
    value = date.fromisoformat(bar_date)
    return f"{value.isoformat()}T00:00:00+08:00"
