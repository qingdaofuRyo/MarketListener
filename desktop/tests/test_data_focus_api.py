"""TDX economic observations and source-isolated commodity-ratio API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.storage import MarketStore, PartitionKey
from market_monitor.web_app import create_web_app


def _macro(code: str, day: str, close: float, *, quality: str = "PASS", normalization: str = "PASS") -> dict:
    return {
        "instrument_id": f"CN.TDX_MACRO.MACRO.{code}.TDX_LOCAL",
        "market": "CN", "asset_type": "MACRO", "period": "1d",
        "bar_open_time": f"{day}T00:00:00+08:00", "trading_date": day,
        "close": close, "fetched_at": f"{day}T12:00:00+08:00",
        "quality_status": quality, "normalization_status": normalization,
        "source": "通达信金融终端（本地）",
    }


def _future(symbol: str, day: str, close: float, *, source: str = "akshare-futures_foreign_hist") -> dict:
    exchange = "NYMEX" if symbol == "CL" else "COMEX"
    return {
        "instrument_id": f"US.{exchange}.FUTURE.{symbol}",
        "market": "GLOBAL", "asset_type": "FUTURE", "period": "1d",
        "bar_open_time": f"{day}T00:00:00+08:00", "trading_date": day,
        "close": close, "fetched_at": f"{day}T12:00:00+08:00",
        "quality_status": "PASS", "currency": "USD", "source": source,
    }


def _client(tmp_path: Path, macros: list[dict], futures: list[dict]) -> TestClient:
    root = tmp_path / "data"
    store = MarketStore(root)
    try:
        if macros:
            store.write_silver_bars(
                PartitionKey("CN", "MACRO", "1d", 2026, "TDX-LOCAL-CN-MACRO-1d-2026-test"),
                macros, "2026-10-16", "fixture", update_query_cache=False,
            )
        if futures:
            store.write_silver_bars(
                PartitionKey("GLOBAL", "FUTURE", "1d", 2026, "GLOBAL-FUTURE-1d-2026-test"),
                futures, "2026-10-16", "fixture", update_query_cache=False,
            )
    finally:
        store.close()
    return TestClient(create_web_app(root), client=("127.0.0.1", 50000))


def test_catalog_exposes_only_pass_tdx_economy_observations(tmp_path: Path) -> None:
    client = _client(tmp_path, [
        _macro("3_PMI", "2026-08-31", 49.4),
        _macro("3_PMI", "2026-09-30", 50.1),
        _macro("2_CPI", "2026-09-30", 100.2),
        _macro("8_NONAG", "2026-09-30", 15904.4),
        _macro("7_AAA", "2026-09-30", 1000),
        _macro("9_FUND", "2026-09-30", 500),
        _macro("4_TRADE", "2026/09/30", 8000),
        _macro("5_M2", "2026-09-30", 3_000_000, normalization="FAILED"),
        _macro("5_M1", "2026-09-30", 400_000, quality="PARTIAL"),
    ], [])
    response = client.get("/api/data/tdx-economy/catalog")
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    assert {key for key in items if key.startswith("TDX_MACRO.")} == {
        "TDX_MACRO.3_PMI", "TDX_MACRO.2_CPI", "TDX_MACRO.8_NONAG",
    }
    assert items["TDX_MACRO.3_PMI"]["latestDay"] == "2026-09-30"
    assert items["TDX_MACRO.3_PMI"]["latestValue"] == 50.1
    assert items["TDX_MACRO.3_PMI"]["source"].startswith("通达信金融终端")
    assert items["GOLD_COPPER_RATIO"]["available"] is False


def test_catalog_reads_while_market_store_has_a_writer_connection(tmp_path: Path) -> None:
    client = _client(tmp_path, [_macro("3_PMI", "2026-09-30", 50.1)], [])
    store = MarketStore(tmp_path / "data")
    try:
        response = client.get("/api/data/tdx-economy/catalog")
        assert response.status_code == 200
        assert any(item["id"] == "TDX_MACRO.3_PMI" for item in response.json()["items"])
    finally:
        store.close()


def test_macro_series_preserves_observation_dates_and_bound(tmp_path: Path) -> None:
    client = _client(tmp_path, [
        _macro("3_PMI", "2026-08-31", 49.4),
        _macro("3_PMI", "2026-09-30", 50.1),
        _macro("3_PMI", "2026-10-31", 50.4, quality="FAILED"),
    ], [])
    body = client.get("/api/data/tdx-economy/series", params={"id": "TDX_MACRO.3_PMI"}).json()
    assert body["points"] == [
        {"day": "2026-08-31", "value": 49.4},
        {"day": "2026-09-30", "value": 50.1},
    ]
    assert "观测期" in body["limitations"][0]
    limited = client.get("/api/data/tdx-economy/series", params={"id": "TDX_MACRO.3_PMI", "limit": 1}).json()
    assert limited["points"] == [{"day": "2026-09-30", "value": 50.1}]
    assert client.get("/api/data/tdx-economy/series", params={"id": "TDX_MACRO.7_AAA"}).status_code == 404


def test_ratios_require_same_day_source_and_convert_copper_cents(tmp_path: Path) -> None:
    client = _client(tmp_path, [], [
        _future("GC", "2026-10-14", 3000),
        _future("SI", "2026-10-14", 30),
        _future("HG", "2026-10-14", 500),
        _future("CL", "2026-10-14", 75),
        _future("GC", "2026-10-15", 3100),
        _future("SI", "2026-10-15", 31),
        _future("CL", "2026-10-15", -75),
        _future("HG", "2026-10-15", 510, source="other-provider"),
    ])
    expected = {
        "GOLD_SILVER_RATIO": [
            {"day": "2026-10-14", "value": 100.0},
            {"day": "2026-10-15", "value": 100.0},
        ],
        "GOLD_COPPER_RATIO": [{"day": "2026-10-14", "value": 600.0}],
        "GOLD_OIL_RATIO": [{"day": "2026-10-14", "value": 40.0}],
    }
    for ratio_id, points in expected.items():
        response = client.get("/api/data/tdx-economy/series", params={"id": ratio_id})
        assert response.status_code == 200
        assert response.json()["points"] == points
        assert response.json()["available"] is True
    items = {item["id"]: item for item in client.get("/api/data/tdx-economy/catalog").json()["items"]}
    assert items["GOLD_COPPER_RATIO"]["latestDay"] == "2026-10-14"
    assert items["GOLD_COPPER_RATIO"]["unit"] == "磅铜/盎司黄金"


def test_previews_include_every_catalog_item_with_recent_sorted_points(tmp_path: Path) -> None:
    client = _client(tmp_path, [
        _macro("3_PMI", "2026-08-31", 49.4),
        _macro("3_PMI", "2026-09-30", 50.1),
        _macro("3_PMI", "2026-10-31", 50.4),
        _macro("2_CPI", "2026-09-30", 100.2),
        _macro("8_NONAG", "2026-09-30", 15904.4),
        _macro("7_AAA", "2026-09-30", 1000),
        _macro("9_FUND", "2026-09-30", 500),
        _macro("5_M2", "2026-09-30", 3_000_000, normalization="FAILED"),
        _macro("5_M1", "2026-09-30", 400_000, quality="PARTIAL"),
    ], [
        _future("GC", "2026-10-14", 3000),
        _future("SI", "2026-10-14", 30),
        _future("HG", "2026-10-14", 500),
        _future("CL", "2026-10-14", 75),
        _future("GC", "2026-10-15", 3100),
        _future("SI", "2026-10-15", 31),
        _future("HG", "2026-10-15", 510, source="other-provider"),
    ])
    catalog = client.get("/api/data/tdx-economy/catalog").json()["items"]
    response = client.get("/api/data/tdx-economy/previews", params={"limit": 2})
    assert response.status_code == 200
    items = {item["id"]: item["points"] for item in response.json()["items"]}
    assert len(items) == len(catalog)
    assert set(items) == {item["id"] for item in catalog}
    assert items["TDX_MACRO.3_PMI"] == [
        {"day": "2026-09-30", "value": 50.1},
        {"day": "2026-10-31", "value": 50.4},
    ]
    assert items["GOLD_SILVER_RATIO"] == [
        {"day": "2026-10-14", "value": 100.0},
        {"day": "2026-10-15", "value": 100.0},
    ]
    assert items["GOLD_COPPER_RATIO"] == [{"day": "2026-10-14", "value": 600.0}]
    assert items["GOLD_OIL_RATIO"] == [{"day": "2026-10-14", "value": 40.0}]
    assert "TDX_MACRO.5_M1" not in items
    assert "TDX_MACRO.5_M2" not in items
    assert "TDX_MACRO.7_AAA" not in items
    assert "TDX_MACRO.9_FUND" not in items
    assert "TDX_MACRO.4_TRADE" not in items
    assert client.get("/api/data/tdx-economy/previews", params={"limit": 0}).status_code == 422
    assert client.get("/api/data/tdx-economy/previews", params={"limit": 121}).status_code == 422
