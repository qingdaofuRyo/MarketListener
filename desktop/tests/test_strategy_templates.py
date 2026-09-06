"""Tests for immutable built-in Strategy templates and custom copies."""

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from market_monitor import strategy_templates
from market_monitor.strategy_templates import StrategyTemplateError, build_strategy_templates, create_from_template
from market_monitor.web_app import create_web_app


def test_builtin_template_catalog_has_required_starts_and_exact_dependencies() -> None:
    templates = {item["template_id"]: item for item in build_strategy_templates()}

    assert set(templates) == {"template.ma_crossover", "template.donchian_atr", "template.blank"}
    assert templates["template.ma_crossover"]["source_strategy_id"] == "strategy.ma_crossover"
    assert templates["template.donchian_atr"]["source_strategy_version"] == 1
    assert templates["template.blank"]["source_strategy_id"] is None
    assert {item["id"] for item in templates["template.ma_crossover"]["dependencies"]} == {
        "condition.crossover", "condition.crossunder", "technical.sma",
    }
    assert "收益承诺" in templates["template.blank"]["disclaimer"]


def test_template_copy_is_isolated_custom_v1_with_provenance() -> None:
    created = create_from_template(
        "template.donchian_atr",
        display_name="我的 Donchian",
        strategy_id="strategy.my_donchian",
    )
    changed = deepcopy(created)
    changed["parameters"]["entry_lookback"]["default"] = 99

    again = create_from_template("template.donchian_atr", strategy_id="strategy.second_donchian")
    assert created["origin"] == "custom"
    assert created["version"] == 1
    assert created["template_source"] == {
        "template_id": "template.donchian_atr",
        "template_version": 1,
        "source_strategy_id": "strategy.donchian_atr",
        "source_strategy_version": 1,
        "default_overrides": {"parameters": {"entry_lookback": 20, "exit_lookback": 10, "atr_lookback": 14}},
    }
    assert again["parameters"]["entry_lookback"]["default"] == 20
    assert changed["parameters"]["entry_lookback"]["default"] == 99


def test_template_refuses_missing_builtin_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_templates, "build_builtin_strategy_definitions", lambda: ())

    with pytest.raises(StrategyTemplateError) as caught:
        build_strategy_templates()
    assert caught.value.code == "MISSING_TEMPLATE_SOURCE"


def test_template_api_creates_custom_resource_and_never_mutates_builtin(tmp_path) -> None:
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    catalog = client.get("/api/strategy/templates")
    assert catalog.status_code == 200
    assert catalog.json()["total"] == 3

    created = client.post(
        "/api/strategy/templates/template.ma_crossover/create",
        json={"displayName": "模板生成均线"},
    )
    assert created.status_code == 201, created.text
    definition = created.json()["definition"]
    assert definition["origin"] == "custom"
    assert definition["version"] == 1
    assert definition["templateSource"]["templateId"] == "template.ma_crossover"

    listed = client.get("/api/strategy/definition-resources").json()["items"]
    builtin = next(item for item in listed if item["id"] == "strategy.ma_crossover")
    generated = next(item for item in listed if item["id"] == definition["id"])
    assert builtin["origin"] == "builtin"
    assert generated["origin"] == "custom"
    missing = client.post("/api/strategy/templates/template.missing/create", json={})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "TEMPLATE_NOT_FOUND"
