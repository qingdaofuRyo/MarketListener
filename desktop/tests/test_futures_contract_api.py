"""Contract selector and four-series API tests for the R4 futures page."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.storage import MarketStore, PartitionKey
from market_monitor.web_app import create_web_app


def _bar(
    day: str,
    *,
    exchange: str,
    product: str,
    symbol: str,
    close: float,
    open_interest: float | None,
    series_kind: str = "CONTRACT",
) -> dict[str, object]:
    instrument_id = f"CN.{exchange}.FUTURE.{symbol}.{series_kind}.TDX"
    return {
        "instrument_id": instrument_id,
        "canonical_instrument_id": f"CN.{exchange}.FUTURE.{symbol}.{series_kind}",
        "market": "CN",
        "asset_type": "FUTURE",
        "period": "1d",
        "trading_date": day,
        "trading_day": day,
        "bar_open_time": f"{day}T00:00:00+08:00",
        "symbol": symbol,
        "name": f"{product} fixture",
        "series_kind": series_kind,
        "product_code": product,
        "exchange": exchange,
        "open": close - 2,
        "high": close + 3,
        "low": close - 4,
        "close": close,
        "settlement": close - 0.5,
        "open_interest": open_interest,
        "source": "fixture-tdx",
        "actual_source": "fixture-tdx",
        "fetched_at": "2026-08-29T00:00:00+00:00",
        "quality_status": "PASS",
    }


def _write(data_root: Path, rows: list[dict[str, object]]) -> None:
    store = MarketStore(data_root)
    try:
        store.write_silver_bars(
            PartitionKey("CN", "FUTURE", "1d", 2026, "contract-api-fixture"),
            rows,
            "2026-08-29T00:00:00+00:00",
            "contract-api-fixture-run",
        )
    finally:
        store.close()


def test_contract_selector_and_series_keep_blocked_metrics_explicit(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write(
        data_root,
        [
            _bar("2026-08-26", exchange="SHFE", product="RB", symbol="RB2610", close=3200, open_interest=1234),
            _bar("2026-08-27", exchange="SHFE", product="RB", symbol="RB2610", close=3210, open_interest=0),
            _bar("2026-08-27", exchange="SHFE", product="RB", symbol="RB2701", close=3220, open_interest=456),
            _bar("2025-05-06", exchange="SHFE", product="RB", symbol="RB2505", close=3000, open_interest=999),
            _bar("2026-08-27", exchange="CFFEX", product="IF", symbol="IF2609", close=4000, open_interest=789),
        ],
    )
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    listing = client.get("/api/futures/contracts", params={"exchange": "SHFE", "product": "RB"})
    assert listing.status_code == 200
    assert [item["contractCode"] for item in listing.json()["items"]] == ["RB2610", "RB2701"]
    assert client.get("/api/futures/contracts").json()["items"]
    assert all(item["exchange"] != "CFFEX" for item in client.get("/api/futures/contracts").json()["items"])
    assert client.get("/api/futures/contracts", params={"trading_day": "2026-08-27"}).status_code == 422
    by_day = client.get(
        "/api/futures/contracts",
        params={"exchange": "SHFE", "product": "RB", "trading_day": "2026-08-26"},
    )
    assert [item["contractCode"] for item in by_day.json()["items"]] == ["RB2610"]

    response = client.get(
        "/api/futures/contract-series",
        params={"exchange": "SHFE", "product": "RB", "contract": "RB2610"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["instrument"]["contractCode"] == "RB2610"
    assert payload["availability"]["price"] == {"available": True, "render": "candlestick", "reason": None}
    assert payload["availability"]["openInterest"]["available"] is True
    assert payload["availability"]["notional"]["available"] is False
    assert payload["availability"]["basis"]["available"] is False
    assert payload["points"][1]["openInterest"] == 0
    assert payload["points"][0]["notionalRmb"] is None
    assert payload["points"][0]["basisRmb"] is None
    assert "priceBasis" in payload["availability"]["notional"]["reason"]
    historical = client.get(
        "/api/futures/contract-series",
        params={"exchange": "SHFE", "product": "RB", "contract": "RB2505"},
    )
    assert historical.status_code == 200 and historical.json()["instrument"]["contractCode"] == "RB2505"


def test_contract_series_requires_an_exact_supported_contract(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write(data_root, [_bar("2026-08-27", exchange="DCE", product="JM", symbol="JM2610", close=1000, open_interest=20)])
    client = TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))

    assert client.get("/api/futures/contract-series", params={"exchange": "DCE", "product": "JM"}).status_code == 422
    assert client.get(
        "/api/futures/contract-series",
        params={"exchange": "CFFEX", "product": "IF", "contract": "IF2609"},
    ).status_code == 422
    assert client.get(
        "/api/futures/contract-series",
        params={"exchange": "DCE", "product": "JM", "contract": "JM2610", "series_kind": "MAIN"},
    ).status_code == 422
