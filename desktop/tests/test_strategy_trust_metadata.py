"""Read-only community/plugin metadata reservation tests (no network/service)."""

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from market_monitor.builtin_strategies import build_builtin_strategy_definitions
from market_monitor.strategy_definition import StrategyDefinitionError, validate_strategy_definition
from market_monitor.web_app import create_web_app


def community_fixture(*, trust_state: str = "untrusted") -> dict:
    definition = deepcopy(build_builtin_strategy_definitions()[0])
    definition.update(
        {
            "id": "strategy.community_fixture",
            "display_name": "社区只读样例",
            "origin": "community",
            "status": "disabled",
            "trust_metadata": {
                "publisher": {"id": "fixture.publisher", "display_name": "Fixture Publisher"},
                "signature": {"algorithm": "ed25519", "key_id": "a" * 64, "verified": trust_state == "trusted"},
                "trust_state": trust_state,
                "review_status": "approved" if trust_state == "trusted" else "pending",
            },
        }
    )
    return definition


def test_community_and_plugin_metadata_are_recognized_but_default_disabled() -> None:
    community = community_fixture()
    evidence = validate_strategy_definition(community)
    assert evidence["valid"] is True
    trusted_but_reserved = community_fixture(trust_state="trusted")
    assert validate_strategy_definition(trusted_but_reserved)["valid"] is True

    active = community_fixture()
    active["status"] = "active"
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(active)
    assert caught.value.code == "INVALID_DEFINITION"

    unknown = community_fixture()
    unknown["origin"] = "internet_marketplace"
    with pytest.raises(StrategyDefinitionError) as caught:
        validate_strategy_definition(unknown)
    assert caught.value.code == "INVALID_DEFINITION"


def test_community_fixture_lists_with_review_metadata_but_cannot_execute_or_upgrade(tmp_path) -> None:
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    document = community_fixture()
    saved = client.post("/api/strategy/definition-resources", json=document)
    assert saved.status_code == 201, saved.text

    listed = client.get("/api/strategy/definition-resources")
    assert listed.status_code == 200
    fixture = next(item for item in listed.json()["items"] if item["id"] == document["id"])
    assert fixture["origin"] == "community"
    assert fixture["status"] == "disabled"
    assert fixture["trustMetadata"]["reviewStatus"] == "pending"

    backtest = client.post(
        "/api/strategy/backtests",
        json={"strategyId": document["id"], "strategyVersion": 1, "instrumentId": "CN.SSE.STOCK.600000"},
    )
    assert backtest.status_code == 403
    assert backtest.json()["detail"]["code"] == "UNTRUSTED_ORIGIN"
    upgrade = client.patch(
        f"/api/strategy/definition-resources/{document['id']}/status",
        json={"status": "active"},
    )
    assert upgrade.status_code == 403
    assert upgrade.json()["detail"]["code"] == "UNTRUSTED_ORIGIN"
