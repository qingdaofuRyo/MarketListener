"""API tests for /api/strategy: definitions, validation, run and history."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.web_app import create_web_app
from web_fixtures import silver_row, write_silver


_DSL_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "dsl" / "valid" / "ma-cross.json"


def _strategy_data(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    data_root = tmp_path / "data"
    document = json.loads(_DSL_FIXTURE.read_text(encoding="utf-8"))
    definitions = data_root / "strategies" / "definitions"
    definitions.mkdir(parents=True)
    (definitions / "ma_cross_demo.json").write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = []
    for index in range(40):
        day = f"2026-06-{index + 1:02d}"
        close_a = 30.0 - index * 0.7 if index < 25 else 12.5 + index * 0.8
        rows.append(silver_row("CN.SSE.STOCK.600000", day, close=close_a))
        rows.append(silver_row("CN.SSE.STOCK.600001", day, close=30.0 - index * 0.4))
    write_silver(data_root, rows)
    return data_root, document


def _app(tmp_path: Path) -> tuple[TestClient, dict[str, object]]:
    data_root, document = _strategy_data(tmp_path)
    application = create_web_app(data_root)
    return TestClient(application, client=("127.0.0.1", 50000)), document


def test_strategy_definitions_and_validate(tmp_path: Path) -> None:
    client, document = _app(tmp_path)
    definitions = client.get("/api/strategy/definitions")
    assert definitions.status_code == 200
    body = definitions.json()
    assert body["total"] == 1
    assert body["items"][0]["strategyId"] == "ma_cross_demo"
    assert body["items"][0]["inputs"] == ["close"]

    validated = client.post("/api/strategy/validate", json=document)
    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    assert validated.json()["strategyId"] == "ma_cross_demo"


def test_strategy_run_writes_record_and_returns_signals(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    response = client.post(
        "/api/strategy/run",
        json={
            "strategyId": "ma_cross_demo",
            "period": "1d",
            "limitInstruments": 10,
            "limitPerInstrument": 100,
            "timeoutSeconds": 5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["strategyId"] == "ma_cross_demo"
    assert body["report"]["status"] == "PASS"
    assert len(body["report"]["instruments"]) == 2
    assert len(body["signals"]) == 2
    assert any(scan["signalCount"] > 0 for scan in body["signals"])
    first_signal = next(scan for scan in body["signals"] if scan["signalCount"] > 0)
    assert first_signal["signals"][0]["instrumentId"]

    runs_dir = tmp_path / "data" / "strategies" / "runs"
    assert len(list(runs_dir.glob("*.json"))) == 1

    history = client.get("/api/strategy/history")
    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["strategyId"] == "ma_cross_demo"
    assert history.json()["items"][0]["status"] == "PASS"


def test_strategy_run_rejects_unknown_or_invalid_inputs(tmp_path: Path) -> None:
    client, document = _app(tmp_path)
    assert (
        client.post("/api/strategy/run", json={"strategyId": "missing"}).status_code == 404
    )
    assert (
        client.post(
            "/api/strategy/validate",
            json={"strategy_id": 123, "nodes": {}},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/strategy/run",
            json={"strategyId": "ma_cross_demo", "period": "5m"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/strategy/run",
            json={"strategyId": "ma_cross_demo", "sql": "delete"},
        ).status_code
        == 422
    )


def test_strategy_mutations_are_loopback_only(tmp_path: Path) -> None:
    data_root, document = _strategy_data(tmp_path)
    application = create_web_app(data_root)
    remote = TestClient(application)
    assert remote.post("/api/strategy/validate", json=document).status_code == 403
    assert remote.post("/api/strategy/run", json={"strategyId": "ma_cross_demo"}).status_code == 403
    assert remote.get("/api/strategy/definitions").status_code == 200


def test_strategy_crud_separates_display_name_and_stable_id(tmp_path: Path) -> None:
    client, document = _app(tmp_path)
    created = client.post(
        "/api/strategy/definitions",
        json={"displayName": "均线突破", "description": "收盘价上穿均线", "script": document},
    )
    assert created.status_code == 201
    body = created.json()
    strategy_id = body["strategyId"]
    assert strategy_id != "均线突破"
    assert body["displayName"] == "均线突破"
    assert body["script"]["strategy_id"] == strategy_id

    marked = client.patch(f"/api/strategy/definitions/{strategy_id}/mark", json={"markColorId": "strategy-mark-08"})
    assert marked.status_code == 200
    assert marked.json()["markColorId"] == "strategy-mark-08"

    updated = client.put(
        f"/api/strategy/definitions/{strategy_id}",
        json={"displayName": "均线突破 v2", "description": "更新后的自然语义", "script": document},
    )
    assert updated.status_code == 200
    assert updated.json()["strategyId"] == strategy_id
    assert updated.json()["displayName"] == "均线突破 v2"
    assert updated.json()["description"] == "更新后的自然语义"

    rejected = client.request("DELETE", f"/api/strategy/definitions/{strategy_id}", json={"confirmDisplayName": "均线突破"})
    assert rejected.status_code == 400
    deleted = client.request("DELETE", f"/api/strategy/definitions/{strategy_id}", json={"confirmDisplayName": "均线突破 v2"})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_strategy_names_are_unique_case_insensitively_and_marks_are_allowlisted(tmp_path: Path) -> None:
    client, document = _app(tmp_path)
    first = client.post(
        "/api/strategy/definitions",
        json={"displayName": "趋势观察", "description": "观察趋势", "script": document},
    )
    assert first.status_code == 201
    duplicate = client.post(
        "/api/strategy/definitions",
        json={"displayName": "趋势观察", "description": "另一个说明", "script": document},
    )
    assert duplicate.status_code == 409
    strategy_id = first.json()["strategyId"]
    invalid_mark = client.patch(
        f"/api/strategy/definitions/{strategy_id}/mark",
        json={"markColorId": "strategy-mark-21"},
    )
    assert invalid_mark.status_code == 422


def test_strategy_matches_respects_category_filter_and_returns_data_timestamp(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    response = client.post(
        "/api/strategy/matches",
        json={
            "allStrategies": True,
            "categoryKeys": ["cn-stock"],
            "page": 1,
            "pageSize": 20,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["pageSize"] == 20
    assert body["updatedAt"] is None or str(body["updatedAt"]).startswith("2026-06-")
    assert all(item["market"] == "CN" and item["assetType"] == "STOCK" for item in body["items"])


def test_strategy_matches_returns_deduplicated_local_instruments(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    response = client.post(
        "/api/strategy/matches",
        json={"allStrategies": True, "categoryKeys": ["cn-stock"], "page": 1, "pageSize": 20},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["updatedAt"]
    assert all("ma_cross_demo" in item["matchedStrategyIds"] for item in body["items"])
    assert all(item["market"] == "CN" for item in body["items"])


def test_formula_catalog_validation_create_and_run_preserve_dsl_v1(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    catalog = client.get("/api/strategy/indicators")
    assert catalog.status_code == 200
    assert {"hsar", "ma", "sd", "bollinger", "atr", "volume"}.issubset(
        {item["id"] for item in catalog.json()["items"]}
    )
    functions = client.get("/api/strategy/functions")
    assert functions.status_code == 200
    assert {"market_scope", "market_cap", "direction_ratio", "gann_rising_rate"}.issubset(
        {item["id"] for item in functions.json()["items"]}
    )
    conditions = client.get("/api/strategy/conditions")
    assert conditions.status_code == 200
    assert {"market_scope", "market_cap", "direction_ratio", "gann_falling_rate"}.issubset(
        {item["id"] for item in conditions.json()["items"]}
    )
    script = {
        "formula_version": 1,
        "expression": "value = ts_momentum(close, lookback)\nsignal = value > threshold",
        "period": "1d",
        "universe": {"market": "CN", "asset_type": "STOCK", "instrument_ids": ["CN.SSE.STOCK.600000"]},
        "parameters": {"lookback": {"default": 3}, "threshold": {"default": 0.0}},
    }
    assert client.post("/api/strategy/formula/validate", json=script).json()["valid"] is True
    created = client.post(
        "/api/strategy/definitions",
        json={"displayName": "三日动量", "description": "三日收益为正", "scriptKind": "formula_v1", "script": script},
    )
    assert created.status_code == 201
    strategy_id = created.json()["strategyId"]
    assert created.json()["scriptKind"] == "formula_v1"
    assert client.get("/api/strategy/definitions").json()["items"][0]["scriptKind"] in {"formula_v1", "dsl_v1"}

    run = client.post("/api/strategy/run", json={"strategyId": strategy_id, "limitPerInstrument": 100})
    assert run.status_code == 200, run.text
    assert run.json()["report"]["scriptKind"] == "formula_v1"
    assert run.json()["report"]["instrumentCount"] == 1
    assert run.json()["signals"][0]["latestValue"] is not None

    legacy = client.get("/api/strategy/definitions/ma_cross_demo").json()
    assert legacy["scriptKind"] == "dsl_v1"


def test_formula_validation_rejects_unsafe_code(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    response = client.post(
        "/api/strategy/formula/validate",
        json={
            "formula_version": 1,
            "expression": "value = close.__class__\nsignal = True",
            "period": "1d",
            "universe": {"market": "CN", "asset_type": "STOCK"},
            "parameters": {},
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "UNSAFE_SYNTAX"


def test_builder_and_safe_python_conditions_normalize_to_compatible_formula_runtime(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    builder = {
        "period": "1d",
        "universe": {"market_types": ["a_share"]},
        "conditionTree": {
            "operator": "and",
            "children": [
                {"functionId": "market_scope", "marketTypes": ["a_share"]},
                {"operator": "or", "children": [
                    {"functionId": "period_return", "args": [3], "operator": "gt", "value": -1},
                    {"functionId": "up_count", "args": [3], "operator": "ge", "value": 0},
                ]},
            ],
        },
    }
    checked = client.post("/api/strategy/condition/validate", json={"conditionKind": "builder_v1", "script": builder})
    assert checked.status_code == 200, checked.text
    created = client.post("/api/strategy/definitions", json={"displayName": "嵌套条件", "scriptKind": "builder_v1", "script": builder})
    assert created.status_code == 201, created.text
    strategy_id = created.json()["strategyId"]
    definition = client.get(f"/api/strategy/definitions/{strategy_id}").json()
    assert definition["scriptKind"] == "builder_v1"
    assert definition["script"]["script_kind"] == "formula_v1"
    assert client.post("/api/strategy/run", json={"strategyId": strategy_id, "limitPerInstrument": 100}).status_code == 200

    safe = {
        "period": "1d", "universe": {"market_types": ["a_share"]},
        "source": "score = period_return(close, 3)\nsignal = score > -1",
    }
    checked_safe = client.post("/api/strategy/condition/validate", json={"conditionKind": "python_safe_v1", "script": safe})
    assert checked_safe.status_code == 200, checked_safe.text
    unsafe = client.post("/api/strategy/condition/validate", json={"conditionKind": "python_safe_v1", "script": {**safe, "source": "import os\nsignal = True"}})
    assert unsafe.status_code == 400


def test_ohlc_formula_strategy_runs_against_local_high_and_low_series(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    script = {
        "formula_version": 1,
        "expression": "value = hsar_resistance(high, lookback, top_percent)\nsignal = close > value",
        "period": "1d",
        "universe": {"market": "CN", "asset_type": "STOCK", "instrument_ids": ["CN.SSE.STOCK.600000"]},
        "parameters": {"lookback": {"default": 3}, "top_percent": {"default": 20.0}},
    }
    created = client.post(
        "/api/strategy/definitions",
        json={"displayName": "横向阻力", "description": "近三根横向阻力", "scriptKind": "formula_v1", "script": script},
    )
    assert created.status_code == 201
    assert created.json()["inputs"] == ["open", "high", "low", "close", "volume"]
    run = client.post("/api/strategy/run", json={"strategyId": created.json()["strategyId"], "limitPerInstrument": 100})
    assert run.status_code == 200, run.text
    assert run.json()["signals"][0]["latestValue"] is not None


def test_formula_market_type_and_local_f10_market_cap_filters_are_applied(tmp_path: Path) -> None:
    client, _document = _app(tmp_path)
    f10 = tmp_path / "data" / "industry" / "f10" / "cn_f10.jsonl"
    f10.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"market": "CN", "code": "600000", "name": "浦发银行", "quote_time": "2026-08-16 15:00:00",
         "total_market_cap": 300.0, "float_market_cap": 250.0},
        {"market": "CN", "code": "600001", "name": "示例公司", "quote_time": "2026-08-16 15:00:00",
         "total_market_cap": 100.0, "float_market_cap": 80.0},
    ]
    f10.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    script = {
        "formula_version": 1,
        "expression": "value = ma(close, lookback)\nsignal = True",
        "period": "1d",
        "universe": {
            "market_types": ["main_board"], "exclude_st": True,
            "total_market_cap_yi": {"operator": "gt", "value": 200.0},
        },
        "parameters": {"lookback": {"default": 3}},
    }
    created = client.post(
        "/api/strategy/definitions",
        json={"displayName": "大市值主板", "description": "主板总市值大于200亿", "scriptKind": "formula_v1", "script": script},
    )
    assert created.status_code == 201, created.text
    run = client.post("/api/strategy/run", json={"strategyId": created.json()["strategyId"], "limitPerInstrument": 100})
    assert run.status_code == 200, run.text
    assert run.json()["report"]["instrumentCount"] == 1
    assert run.json()["signals"][0]["instrumentId"] == "CN.SSE.STOCK.600000"
