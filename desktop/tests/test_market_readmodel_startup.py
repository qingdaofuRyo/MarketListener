"""Startup scheduling inspects references; data reads remain the integrity gate."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from market_monitor import market_readmodel
from market_monitor.operations import OperationKind
from market_monitor.web_app import create_web_app


@pytest.mark.parametrize("references_present", [True, False])
def test_ready_does_not_decode_published_rows_and_missing_references_queue_recovery(
    tmp_path: Path, monkeypatch, references_present: bool,
) -> None:
    root = tmp_path / "data"
    (root / "silver").mkdir(parents=True)
    (root / "catalog.duckdb").touch()
    version = "synthetic-publication-v1"
    if references_present:
        for identity in ("all", "category:cn-future-main"):
            market_readmodel.publish_market_readmodel(
                root, version=version, identity=identity,
                items=[{"instrumentId": "CN.DCE.FUTURE.A2611"}],
            )

    submitted: list[OperationKind] = []
    rebuild_guards: list[object] = []

    class QueryStore:
        def set_rebuild_allowed(self, callback) -> None:
            rebuild_guards.append(callback)

        def status(self):
            return SimpleNamespace(ready=True)

    class Operations:
        def submit(self, kind):
            submitted.append(kind)
            return None, True

        def list(self):
            return []

    class Catalog:
        def __init__(self, _root) -> None:
            pass

        def close(self) -> None:
            pass

    def forbidden_payload_read(*_args, **_kwargs):
        raise AssertionError("full payload validation must not block READY")

    query_store = QueryStore()
    monkeypatch.setattr("market_monitor.storage.MarketStore", Catalog)
    monkeypatch.setattr("market_monitor.market_data_version.market_data_version", lambda _root: "r1")
    monkeypatch.setattr("market_monitor.market_data_version.migrate_legacy_market_data_version",
                        lambda _root: None)
    monkeypatch.setattr("market_monitor.market_query_cache.get_kline_query_store", lambda _root: query_store)
    monkeypatch.setattr("market_monitor.web_api.market._response_data_version", lambda _root: version)
    monkeypatch.setattr(market_readmodel, "load_market_readmodel", forbidden_payload_read)
    monkeypatch.setenv("MARKETLISTENER_STARTUP_ID", "1" * 32)

    app = create_web_app(root, operation_manager=Operations(), auto_publish_on_startup=True)
    with TestClient(app) as client:
        assert client.get("/api/ready").json() == {"status": "READY"}
        stage = json.loads((root / "state" / "service_startup.json").read_text(encoding="utf-8"))
        assert stage["phase"] == "READY"
        assert "READMODEL_CHECK" in [item["phase"] for item in stage["phaseHistory"]]
        assert submitted == ([] if references_present else [OperationKind.MARKET_READMODEL_PUBLISH])
    assert callable(rebuild_guards[0])
    assert rebuild_guards[-1] is None
