"""Period routing uses one index lookup and preserves session-aware derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from market_monitor.market_query_cache import get_kline_query_store
from market_monitor.web_api import market
from web_fixtures import silver_row, write_silver


def test_period_routes_batch_candidates_without_decoding_minute_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "routed"
    canonical = "CN.SSE.STOCK.600519"
    daily = silver_row(f"{canonical}.DAILY", "2026-08-13", period="1d")
    daily.update(canonical_instrument_id=canonical, source="daily-local")
    minute = silver_row(f"{canonical}.MINUTE", "2026-08-14", period="5m")
    minute.update(canonical_instrument_id=canonical, source="minute-local")
    write_silver(root, [daily, minute])
    store = get_kline_query_store(root)
    original = store.periods_many
    calls: list[tuple[str, ...]] = []

    def capture(candidate_ids: list[str]) -> dict[str, list[str]]:
        calls.append(tuple(candidate_ids))
        return original(candidate_ids)

    monkeypatch.setattr(store, "periods_many", capture)
    monkeypatch.setattr(market, "_raw_periods_for_instrument", lambda *_args: pytest.fail("周期应批量查询"))
    monkeypatch.setattr(market, "read_bars", lambda *_args, **_kwargs: pytest.fail("有效元数据不应读取分钟K线"))

    minute_route, selected, available = market._resolve_period(root, canonical, "1h")
    assert selected == "1h"
    assert minute_route["storageInstrumentId"] == minute["instrument_id"]
    assert minute_route["actualSource"] == "minute-local"
    assert available == ["5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "1q", "3mo", "6mo", "1y"]
    assert len(calls) == 1
    assert set(calls[0]) == {daily["instrument_id"], minute["instrument_id"]}

    daily_route, selected, available = market._resolve_period(root, canonical, "1d")
    assert selected == "1d"
    assert daily_route["storageInstrumentId"] == daily["instrument_id"]
    assert daily_route["actualSource"] == "daily-local"
    assert "1h" in available
    assert len(calls) == 1


def test_period_route_sees_new_source_revision(tmp_path: Path) -> None:
    root = tmp_path / "new-period"
    instrument_id = "CN.SSE.STOCK.600519"
    daily = silver_row(instrument_id, "2026-08-13", period="1d")
    write_silver(root, [daily])
    _instrument, _selected, available = market._resolve_period(root, instrument_id, "1d")
    assert "1h" not in available

    minute = silver_row(instrument_id, "2026-08-14", period="5m")
    write_silver(root, [daily, minute])
    # The publisher must advance the query index before the new source
    # revision becomes chart-readable; a GET cannot use the stale snapshot.
    get_kline_query_store(root).rebuild()
    route, selected, available = market._resolve_period(root, instrument_id, "1h")
    assert selected == "1h"
    assert route["storageInstrumentId"] == instrument_id
    assert "1h" in available


def test_missing_inventory_identity_keeps_minute_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument_id = "legacy-minute"
    monkeypatch.setattr(market, "_raw_periods_for_instrument", lambda *_args: ["5m"])
    probes: list[str] = []

    def probe(_root: Path, _instrument_id: str, *, period: str, limit: int):
        probes.append(period)
        assert limit == 1
        return [{"market": "CN", "asset_type": "STOCK"}]

    monkeypatch.setattr(market, "read_bars", probe)
    available = market._available_periods(tmp_path, instrument_id, physical_item={})
    assert available == ["5m", "15m", "30m", "1h", "2h", "4h"]
    assert probes == ["5m"]


def test_newer_daily_identity_does_not_claim_unsupported_minute_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market, "_raw_periods_for_instrument", lambda *_args: ["5m", "1d"])
    probes: list[str] = []

    def probe(_root: Path, _instrument_id: str, *, period: str, limit: int):
        probes.append(period)
        assert limit == 1
        return [{"market": "XX", "asset_type": "UNKNOWN"}]

    monkeypatch.setattr(market, "read_bars", probe)
    available = market._available_periods(
        tmp_path, "old-minute", physical_item={"period": "1d", "market": "CN", "assetType": "STOCK"},
    )
    assert available == ["5m", "1d", "1w", "1mo", "1q", "3mo", "6mo", "1y"]
    assert probes == ["5m"]
