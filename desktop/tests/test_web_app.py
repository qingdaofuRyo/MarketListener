"""Regression tests for the FastAPI host and fixed terminal routes."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from market_monitor.operations import OperationKind, OperationManager
from market_monitor.tdx_afterhours import BEIJING, latest_slot
from market_monitor.web_app import _operation_handlers, create_web_app


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
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
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


def test_readiness_and_favicon_routes(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path / "data", web_dist=_dist(tmp_path)))

    assert client.get("/api/ready").json() == {"status": "READY"}
    icon = client.get("/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    legacy = client.get("/favicon.ico", follow_redirects=False)
    assert legacy.status_code == 307
    assert legacy.headers["location"] == "/favicon.svg"


def test_startup_auto_update_queues_market_and_non_f10_data_only(tmp_path: Path) -> None:
    class RecordingOperations:
        def __init__(self) -> None:
            self.submitted: list[OperationKind] = []

        def submit(self, kind: OperationKind):
            self.submitted.append(kind)
            return None, True

        def list(self):
            return []

    operations = RecordingOperations()
    app = create_web_app(
        tmp_path / "data",
        web_dist=_dist(tmp_path),
        operation_manager=operations,  # type: ignore[arg-type]
        auto_update_on_startup=True,
    )
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 200

    assert operations.submitted == [
        OperationKind.MARKET_UPDATE,
        OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationKind.TDX_COVERAGE_CHECK,
        OperationKind.FUTURES_STRUCTURE_UPDATE,
    ]
    assert not {
        OperationKind.F10_UPDATE_CN,
        OperationKind.F10_UPDATE_HK,
        OperationKind.REVENUE_UPDATE,
    }.intersection(operations.submitted)


def test_startup_local_import_requires_recent_confirmed_tdx_download(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    state = data_root / "state" / "tdx_download_ui"
    state.mkdir(parents=True)
    (state / "latest.json").write_text(
        json.dumps({"status": "UI_DONE_UNVERIFIED", "slot": latest_slot(datetime.now(BEIJING))}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "market_monitor.tdx_local.run_tdx_local_import",
        lambda _root: {"状态": "完成", "扫描文件": 4, "写入K线": 10, "错误数": 0},
    )
    monkeypatch.setattr(
        "market_monitor.futures_contract_heat_update.run_futures_contract_heat_update",
        lambda _root: {
            "status": "PASS",
            "calculation": {"latestDay": "2026-09-24", "latestTurnoverCoverage": 1.0},
            "officialTurnover": {"exchanges": []},
        },
    )
    handlers = _operation_handlers(data_root)

    result = handlers[OperationKind.TDX_LOCAL_IMPORT]()  # type: ignore[operator]

    assert str(result).startswith("PASS:")
    (state / "latest.json").write_text(
        json.dumps({"status": "ATTENTION_REQUIRED", "slot": latest_slot(datetime.now(BEIJING))}),
        encoding="utf-8",
    )
    skipped = handlers[OperationKind.TDX_LOCAL_IMPORT]()  # type: ignore[operator]
    assert str(skipped).startswith("PARTIAL_FAILURE:")
    assert "跳过本地文件导入" in str(skipped)


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


def test_tdx_operations_are_allowlisted_for_loopback_custom_queue(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def safe_coverage_handler() -> str:
        entered.set()
        assert release.wait(3)
        return "PASS: synthetic coverage check"

    data_root = tmp_path / "data"
    operations = OperationManager(
        data_root,
        {
            OperationKind.TDX_COVERAGE_CHECK: safe_coverage_handler,
            OperationKind.TDX_AFTERHOURS_DOWNLOAD: lambda: "PASS: synthetic download",
        },
    )
    application = create_web_app(data_root, web_dist=_dist(tmp_path), operation_manager=operations)
    remote = TestClient(application)
    loopback = TestClient(application, client=("127.0.0.1", 50000))

    assert remote.post("/api/operations", json={"kind": "TDX_COVERAGE_CHECK"}).status_code == 403
    try:
        coverage = loopback.post("/api/operations", json={"kind": "TDX_COVERAGE_CHECK"})
        assert coverage.status_code == 202
        assert coverage.json()["operation"]["kind"] == "TDX_COVERAGE_CHECK"
        assert entered.wait(3)

        download = loopback.post("/api/operations", json={"kind": "TDX_AFTERHOURS_DOWNLOAD"})
        assert download.status_code == 202
        assert download.json()["operation"]["kind"] == "TDX_AFTERHOURS_DOWNLOAD"
        assert download.json()["operation"]["status"] == "QUEUED"
    finally:
        release.set()


def test_tdx_afterhours_coverage_api_returns_safe_aggregates(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    client = TestClient(create_web_app(data_root, web_dist=_dist(tmp_path)))

    missing = client.get("/api/tdx-afterhours/coverage")
    assert missing.status_code == 200
    assert missing.json() == {"available": False}

    absolute_raw_path = tmp_path / "terminals" / "vipdoc" / "sh" / "lday" / "sh600519.day"
    report = {
        "schema": "tdx-raw-coverage-v1",
        "generatedAt": "2026-09-20T23:05:00+08:00",
        "status": "PARTIAL_FAILURE",
        "summary": {"files": 4, "gapFiles": 1, "unreadableFiles": 0, "writingFiles": 0, "unknownCalendarFiles": 1},
        "groups": [
            {
                "terminal": "FINANCIAL",
                "period": "1d",
                "files": 2,
                "readableFiles": 2,
                "unreadableFiles": 0,
                "writingFiles": 0,
                "earliestAt": "2026-09-01",
                "latestAt": "2026-09-19",
                "records": 30,
                "continuousFiles": 1,
                "gapFiles": 1,
                "unknownCalendarFiles": 0,
                "gapCount": 1,
                "sampleGaps": [{"instrument": "CN.SSE.STOCK.600519", "date": "2026-09-12", "reason": "missing trading day"}],
                "absolutePath": str(absolute_raw_path),
                "fileDetails": [{"path": str(absolute_raw_path), "records": 15}],
            }
        ],
        "files": [{"path": str(absolute_raw_path), "period": "1d", "records": 15}],
    }
    report_path = data_root / "reports" / "tdx-coverage" / "latest.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    response = client.get("/api/tdx-afterhours/coverage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["generatedAt"] == report["generatedAt"]
    assert payload["status"] == report["status"]
    assert payload["summary"] == report["summary"]
    assert payload["groups"] == [
        {
            "terminal": "FINANCIAL",
            "period": "1d",
            "files": 2,
            "readableFiles": 2,
            "unreadableFiles": 0,
            "writingFiles": 0,
            "earliestAt": "2026-09-01",
            "latestAt": "2026-09-19",
            "records": 30,
            "continuousFiles": 1,
            "gapFiles": 1,
            "unknownCalendarFiles": 0,
            "gapCount": 1,
            "sampleGaps": [{"instrument": "CN.SSE.STOCK.600519", "date": "2026-09-12", "reason": "missing trading day"}],
        }
    ]
    assert str(absolute_raw_path) not in response.text


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
