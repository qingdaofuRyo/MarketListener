"""Regression tests for the FastAPI host and fixed terminal routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.operations import OperationKind, OperationManager
from market_monitor.web_app import create_web_app


def _f10_fixture(data_root: Path) -> None:
    directory = data_root / "industry" / "f10"
    directory.mkdir(parents=True)
    row = {
        "code": "600519",
        "market": "CN",
        "name": "贵州茅台",
        "profile": "本地 F10 样本",
        "source": "eastmoney_f10",
        "fetched_at": "2026-08-09T10:00:00+08:00",
    }
    (directory / "cn_f10.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    (directory / "hk_f10.jsonl").write_text("", encoding="utf-8")


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<main id=app>MarketListener</main>", encoding="utf-8")
    return dist


def test_fastapi_shell_has_exact_routes_and_industry_v2_redirect(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data", web_dist=_dist(tmp_path)))

    for route in (
        "/",
        "/data/",
        "/f10/",
        "/industry/",
        "/logs/",
        "/market/all/",
        "/market/targets/",
        "/market/instrument/CN.CFFEX.FUTURE.IFMAIN",
        "/f10/company/CN.SSE.STOCK.600519",
    ):
        response = client.get(route)
        assert response.status_code == 200
        assert "MarketListener" in response.text

    redirect = client.get("/industry-v2/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/industry/"


def test_fastapi_f10_api_is_local_paginated_and_never_returns_placeholders(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _f10_fixture(data_root)
    (data_root / "industry").mkdir(exist_ok=True)
    (data_root / "industry" / "industry-atlas.json").write_text(
        json.dumps({"chains": [{"name": "白酒", "stages": [{"name": "消费", "cards": [{"name": "白酒", "companyRefs": ["CN.SSE.STOCK.600519"]}]}]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    listing = client.get("/api/f10/companies", params={"q": "茅台", "page_size": 1})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["instrumentKey"] == "CN.SSE.STOCK.600519"

    detail = client.get("/api/f10/companies/CN.SSE.STOCK.600519")
    assert detail.status_code == 200
    encoded = detail.text
    assert "undefined" not in encoded
    assert "Invalid Date" not in encoded
    assert "null" not in encoded
    assert detail.json()["chainLocations"] == [{"chain": "白酒", "stage": "消费", "node": "白酒"}]


def test_fastapi_rejects_unbounded_f10_pages_and_has_no_mutation_surface(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data", web_dist=_dist(tmp_path)), client=("127.0.0.1", 50000))

    assert client.get("/api/f10/companies", params={"page_size": 501}).status_code == 422
    assert client.post("/api/f10/companies").status_code == 405


def test_operations_only_accept_allowlisted_loopback_requests(tmp_path: Path) -> None:
    operations = OperationManager(tmp_path / "data", {OperationKind.STATUS_REFRESH: lambda: "PASS"})
    application = create_web_app(tmp_path / "data", web_dist=_dist(tmp_path), operation_manager=operations)

    remote = TestClient(application)
    assert remote.post("/api/operations", json={"kind": "STATUS_REFRESH"}).status_code == 403

    loopback = TestClient(application, client=("127.0.0.1", 50000))
    created = loopback.post("/api/operations", json={"kind": "STATUS_REFRESH"})
    assert created.status_code == 202
    assert created.json()["operation"]["kind"] == "STATUS_REFRESH"
    assert loopback.post("/api/operations", json={"kind": "__import__('os').system('x')"}).status_code == 422
    assert loopback.post("/api/operations", json={"kind": "STATUS_REFRESH", "sql": "delete from runs"}).status_code == 422
    assert loopback.get("/api/operations").status_code == 200


def test_data_views_are_allowlisted_paginated_and_read_only(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _f10_fixture(data_root)
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    response = client.get("/api/data/f10", params={"page_size": 500})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert client.get("/api/data/not_sql").status_code == 404
    assert client.post("/api/data/f10").status_code == 403


def test_logs_api_is_bounded_read_only_jsonl_view(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    logs = data_root / "logs"
    logs.mkdir(parents=True)
    (logs / "events-2026-08-09.jsonl").write_text('{"timestamp":"2026-08-09T00:00:00+00:00","category":"Operation","status":"PASS"}\n', encoding="utf-8")
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    assert client.get("/api/logs", params={"category": "Operation", "page_size": 1}).json()["total"] == 1
    assert client.post("/api/logs").status_code == 403
