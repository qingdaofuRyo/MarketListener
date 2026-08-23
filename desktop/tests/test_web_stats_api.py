"""API tests for /api/stats: Android-compatible ledger statistics."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.web_app import create_web_app
from web_fixtures import silver_row, write_silver


def _ledger_lines() -> list[dict[str, object]]:
    return [
        {"type": "header", "source_label": "desktop-test"},
        {"type": "cash", "kind": "DEPOSIT", "amount": 100000.0, "occurred_at": "2026-08-03T09:00:00+08:00"},
        {
            "type": "trade",
            "instrument_id": "CN.SSE.STOCK.600519",
            "side": "BUY",
            "quantity": 100,
            "price": 10.0,
            "executed_at": "2026-08-04T10:00:00+08:00",
            "fees": [{"kind": "COMMISSION", "amount": 5.0}],
            "strategy_id": "ma_cross_demo",
        },
        {
            "type": "trade",
            "instrument_id": "CN.SSE.STOCK.600519",
            "side": "SELL",
            "quantity": 100,
            "price": 12.0,
            "executed_at": "2026-08-05T10:00:00+08:00",
            "fees": [{"kind": "COMMISSION", "amount": 5.0}],
            "strategy_id": "ma_cross_demo",
        },
    ]


def _data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    ledger = data_root / "personal" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in _ledger_lines()),
        encoding="utf-8",
    )
    write_silver(
        data_root,
        [
            silver_row("CN.SSE.STOCK.600519", "2026-08-04", close=10.0),
            silver_row("CN.SSE.STOCK.600519", "2026-08-05", close=12.0),
        ],
    )
    return data_root


def _app(tmp_path: Path, *, with_ledger: bool = True) -> TestClient:
    data_root = _data_root(tmp_path) if with_ledger else tmp_path / "empty"
    return TestClient(create_web_app(data_root), client=("127.0.0.1", 50000))


def test_stats_summary_available_false_when_no_ledger(tmp_path: Path) -> None:
    client = _app(tmp_path, with_ledger=False)
    response = client.get("/api/stats/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["navCurve"] == []
    assert body["totalReturnPct"] is None
    assert body["realizedTotal"] is None
    assert "NaN" not in response.text
    assert "undefined" not in response.text


def test_stats_summary_matches_average_cost_accounting(tmp_path: Path) -> None:
    client = _app(tmp_path)
    body = client.get("/api/stats/summary").json()
    assert body["available"] is True
    assert body["feesTotal"] == 10.0
    assert body["realizedTotal"] == 190.0
    assert body["grossProfit"] == 190.0
    assert body["grossLoss"] == 0.0
    assert body["winRatePct"] == 100.0
    assert body["realizedByStrategy"] == {"ma_cross_demo": 190.0}
    assert body["realizedByInstrument"] == {"CN.SSE.STOCK.600519": 190.0}
    assert body["unrealizedTotal"] == 0.0

    curve = body["navCurve"]
    assert curve[0]["t"] == "2026-08-03"
    assert curve[0]["nav"] == 100000.0
    assert curve[1]["t"] == "2026-08-04"
    assert curve[1]["nav"] == 99995.0
    assert curve[-1]["t"] == "2026-08-05"
    assert curve[-1]["nav"] == 100190.0
    assert curve[-1]["positionValue"] == 0.0


def test_stats_trades_positions_and_pagination(tmp_path: Path) -> None:
    client = _app(tmp_path)
    trades = client.get("/api/stats/trades", params={"pageSize": 2})
    assert trades.status_code == 200
    body = trades.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    newest = body["items"][0]
    assert newest["side"] == "SELL"
    assert newest["fees"] == [{"kind": "COMMISSION", "amount": 5.0}]
    assert newest["strategyId"] == "ma_cross_demo"
    assert body["items"][1]["side"] == "BUY"

    page2 = client.get("/api/stats/trades", params={"page": 2, "pageSize": 2})
    assert page2.json()["items"] == []

    # A fresh open position uses cost fallback when no close is available.
    data_root = tmp_path / "data"
    ledger = data_root / "personal" / "ledger.jsonl"
    ledger.write_text(
        "".join(
            json.dumps(line, ensure_ascii=False) + "\n"
            for line in _ledger_lines()[:-1]
        ),
        encoding="utf-8",
    )
    positions = client.get("/api/stats/positions").json()
    assert positions["total"] == 1
    item = positions["items"][0]
    assert item["instrumentId"] == "CN.SSE.STOCK.600519"
    assert item["quantity"] == 100
    assert item["averageCost"] == 10.05
    assert item["marketValue"] == 1200.0
    assert item["unrealizedPnl"] == 195.0


def test_stats_import_skips_invalid_lines_and_export_roundtrip(tmp_path: Path) -> None:
    client = _app(tmp_path)
    imported = client.post(
        "/api/stats/import",
        json={
            "lines": [
                {
                    "type": "trade",
                    "instrument_id": "HK.HKEX.STOCK.00700",
                    "side": "buy",
                    "quantity": 10,
                    "price": 300.0,
                    "executed_at": "2026-08-06T10:00:00+08:00",
                },
                {"type": "trade", "instrument_id": "X", "side": "BUY", "quantity": 1},
                {"type": "mystery", "foo": 1},
            ]
        },
    )
    assert imported.status_code == 200
    assert imported.json() == {"imported": 1, "skipped": 2, "total": 3}

    exported = client.get("/api/stats/export")
    assert exported.status_code == 200
    text = exported.text
    assert "desktop-test" in text
    assert '"side": "BUY"' in text or '"side":"BUY"' in text

    summary = client.get("/api/stats/summary").json()
    assert summary["available"] is True
    assert "HK.HKEX.STOCK.00700" in summary["unrealizedByInstrument"]


def test_stats_mutations_are_loopback_only(tmp_path: Path) -> None:
    application = create_web_app(_data_root(tmp_path))
    remote = TestClient(application)
    assert remote.post("/api/stats/import", json={"lines": []}).status_code == 403
    assert remote.get("/api/stats/summary").status_code == 200


def test_strategy_ledger_crud_merges_legacy_and_calculates_long_short_performance(tmp_path: Path) -> None:
    client = _app(tmp_path)
    capital = client.put(
        "/api/stats/strategy-capital/ma_cross_demo",
        json={"initialCapital": 100000},
    )
    assert capital.status_code == 200
    created = client.post(
        "/api/stats/strategy-trades",
        json={
            "strategyId": "ma_cross_demo",
            "instrumentId": "CN.SSE.STOCK.600519",
            "direction": "SHORT",
            "entryAt": "2026-08-04T10:00:00+08:00",
            "entryPrice": 10,
            "exitAt": "2026-08-05T10:00:00+08:00",
            "exitPrice": 8,
            "quantity": 2,
            "contractMultiplier": 10,
            "entryFees": 1,
            "exitFees": 1,
        },
    )
    assert created.status_code == 201
    trade_id = created.json()["id"]
    ledger = client.get("/api/stats/strategy-ledger").json()
    assert ledger["capitalByStrategy"]["ma_cross_demo"] == 100000
    assert ledger["total"] == 3
    assert len(ledger["legacyItems"]) == 2
    assert ledger["legacyItems"][0]["editable"] is False

    performance = client.post(
        "/api/stats/strategy-performance",
        json={"strategyId": "ma_cross_demo", "period": "1d", "riskFreeRate": 0.02},
    )
    assert performance.status_code == 200, performance.text
    result = performance.json()
    assert result["available"] is True
    assert result["legacyTradeCount"] == 2
    assert result["editableTradeCount"] == 1
    assert result["curve"][-1]["nav"] == 100228.0
    assert result["valuationAt"] is not None

    updated_payload = dict(created.json())
    updated_payload.pop("id")
    updated_payload.pop("source")
    updated_payload["exitPrice"] = 9
    updated = client.put(f"/api/stats/strategy-trades/{trade_id}", json=updated_payload)
    assert updated.status_code == 200
    deleted = client.delete(f"/api/stats/strategy-trades/{trade_id}")
    assert deleted.status_code == 200
    assert client.get("/api/stats/strategy-ledger").json()["total"] == 2


def test_strategy_performance_requires_independent_capital(tmp_path: Path) -> None:
    client = _app(tmp_path)
    result = client.post(
        "/api/stats/strategy-performance",
        json={"strategyId": "ma_cross_demo", "period": "1d"},
    ).json()
    assert result["available"] is False
    assert "初始本金" in result["reason"]


def test_open_long_trade_uses_latest_local_close_and_reports_cutoff(tmp_path: Path) -> None:
    client = _app(tmp_path)
    assert client.put("/api/stats/strategy-capital/open_demo", json={"initialCapital": 1000}).status_code == 200
    assert client.post(
        "/api/stats/strategy-trades",
        json={
            "strategyId": "open_demo", "instrumentId": "CN.SSE.STOCK.600519", "direction": "LONG",
            "entryAt": "2026-08-04T10:00:00+08:00", "entryPrice": 10, "quantity": 2,
            "contractMultiplier": 5, "entryFees": 1, "exitFees": 0,
        },
    ).status_code == 201
    result = client.post(
        "/api/stats/strategy-performance",
        json={"strategyId": "open_demo", "period": "1d"},
    ).json()
    assert result["available"] is True
    assert result["curve"][-1]["nav"] == 1019.0
    assert result["valuationAt"].startswith("2026-08-05")


def test_account_analysis_crud_fifo_csv_and_recycle_bin(tmp_path: Path) -> None:
    """The new account store is isolated from the legacy Android ledger."""
    client = _app(tmp_path, with_ledger=False)
    created = client.post(
        "/api/stats/accounts",
        json={
            "name": "期货模拟账户", "startDate": "2026-08-03", "initialEquity": 100_000,
            "riskFreeRate": 0.02,
        },
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]

    first = client.post(
        f"/api/stats/accounts/{account_id}/snapshots",
        json={"day": "2026-08-04", "equity": 101_000, "cash": 90_000, "marketValue": 11_000},
    )
    assert first.status_code == 200
    # Updating an older row must return that row, not simply the last row by date.
    updated = client.post(
        f"/api/stats/accounts/{account_id}/snapshots",
        json={"id": first.json()["id"], "day": "2026-08-04", "equity": 101_100},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == first.json()["id"]
    assert updated.json()["equity"] == 101_100
    assert client.post(
        f"/api/stats/accounts/{account_id}/snapshots",
        json={"day": "2026-08-05", "equity": 100_900, "cash": 100_900, "marketValue": 0},
    ).status_code == 200
    assert client.post(
        f"/api/stats/accounts/{account_id}/cashflows",
        json={"occurredAt": "2026-08-04T12:00:00+08:00", "kind": "DEPOSIT", "amount": 10_000},
    ).status_code == 200

    fills = [
        {"instrumentId": "CN.SSE.STOCK.600519", "direction": "LONG", "positionEffect": "OPEN", "occurredAt": "2026-08-04T10:00:00+08:00", "quantity": 5, "price": 10, "contractMultiplier": 1, "fee": 1},
        {"instrumentId": "CN.SSE.STOCK.600519", "direction": "LONG", "positionEffect": "CLOSE", "occurredAt": "2026-08-05T10:00:00+08:00", "quantity": 2, "price": 12, "contractMultiplier": 1, "fee": 1},
        {"instrumentId": "CN.SSE.STOCK.600519", "direction": "LONG", "positionEffect": "CLOSE", "occurredAt": "2026-08-05T14:00:00+08:00", "quantity": 3, "price": 8, "contractMultiplier": 1, "fee": 1},
    ]
    for fill in fills:
        response = client.post(f"/api/stats/accounts/{account_id}/fills", json=fill)
        assert response.status_code == 200, response.text

    imported = client.post(
        f"/api/stats/accounts/{account_id}/csv-import",
        json={"kind": "strategyUses", "csvText": "strategyId,strategyName,startDate,endDate\nvalue_demo,示例,2026-08-04,2026-08-05\n"},
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1

    analysis = client.get(f"/api/stats/accounts/{account_id}/analysis")
    assert analysis.status_code == 200, analysis.text
    body = analysis.json()
    assert body["available"] is True
    assert body["metrics"]["cumulativeDeposits"]["value"] == 10_000
    assert body["metrics"]["winRate"]["value"] == 0.5
    assert body["metrics"]["profitLossRatio"]["value"] is not None
    assert len(body["strategyUses"]) == 1
    assert body["reconciliation"]["authoritative"] == "snapshots"

    assert client.delete(f"/api/stats/accounts/{account_id}").status_code == 200
    assert client.get("/api/stats/accounts").json()["total"] == 0
    assert client.get("/api/stats/accounts/trash").json()["total"] == 1
    assert client.post(f"/api/stats/accounts/{account_id}/restore").status_code == 200
    assert client.get("/api/stats/accounts").json()["total"] == 1
