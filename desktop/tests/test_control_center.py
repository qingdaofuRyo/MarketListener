"""HTML 控制中心：筛选解析、同步计划、健康报告与本地 HTTP 端点。"""

from __future__ import annotations

import json
import threading
import zipfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from market_monitor.control_center import (
    SyncFilter,
    build_control_center_report,
    build_sync_plan,
    make_handler,
    parse_sync_filter,
    serve_control_center,
)
from market_monitor.dataset_catalog import dataset_index
from market_monitor.market_query_cache import KLineQueryStore
from market_monitor.package_builder import build_android_package
from market_monitor.signing import generate_development_key
from market_monitor.storage import MarketStore, PartitionKey


def _bar() -> dict[str, object]:
    return {
        "instrument_id": "CN.SSE.STOCK.600519",
        "bar_open_time": "2026-08-07T09:30:00+08:00",
        "close": 100.0,
    }


def _full_bar() -> dict[str, object]:
    return {
        "instrument_id": "CN.SSE.STOCK.600519",
        "period": "1d",
        "bar_open_time": "2026-08-07T09:30:00+08:00",
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 1000,
        "amount": 1000000.0,
    }


def _write_package_root(data_root: Path) -> None:
    store = MarketStore(data_root)
    try:
        run_ok = store.begin_run("pytdx")
        store.write_silver_bars(
            PartitionKey("CN", "STOCK", "1d", 2026, "CN-STOCK-1d-2026"),
            [_full_bar()],
            "2026-08-07T10:00:00+08:00",
            run_ok,
        )
        store.finish_run(run_ok, "COMPLETE", "package fixture")
        store.upsert_gold_metrics(
            [
                {
                    "metric_id": "CN_MARGIN:CN.SSE.MARGIN:20260807:1d:融资余额",
                    "instrument_id": "CN.SSE.MARGIN",
                    "trading_date": "20260807",
                    "period": "1d",
                    "metric_name": "沪市融资余额",
                    "value": 1266993136806.0,
                    "definition": "融资余额",
                    "calculation_method": "sum",
                    "timestamp": "2026-08-07T15:00:00+08:00",
                }
            ]
        )
    finally:
        store.close()
    private_key = data_root / "keys" / "test-private.pem"
    public_key = data_root / "keys" / "test-public.pem"
    generate_development_key(private_key, public_key)
    build_android_package(data_root, private_key)


def test_android_package_contains_only_new_industry_atlas(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_package_root(data_root)
    industry = data_root / "industry"
    industry.mkdir(parents=True)
    (industry / "industry-map.html").write_text("obsolete", encoding="utf-8")
    (industry / "industry-atlas.html").write_text("atlas", encoding="utf-8")
    private_key = data_root / "keys" / "test-private.pem"

    package = build_android_package(data_root, private_key)

    with zipfile.ZipFile(package["package_path"]) as archive:
        names = archive.namelist()
    assert "industry/industry-atlas.html" in names
    assert "industry/industry-map.html" not in names


def _write_store(data_root: Path) -> None:
    store = MarketStore(data_root)
    try:
        run_ok = store.begin_run("pytdx")
        store.write_silver_bars(
            PartitionKey("CN", "STOCK", "1d", 2026, "CN-STOCK-1d-2026"),
            [_bar()],
            "2026-08-07T10:00:00+08:00",
            run_ok,
        )
        store.finish_run(run_ok, "COMPLETE", "7/7 capabilities passed")
        run_bad = store.begin_run("akshare")
        store.finish_run(run_bad, "FAILED", "market-fund-flow remote disconnected")
    finally:
        store.close()
    quarantine = data_root / "quarantine" / "bad-partition"
    quarantine.mkdir(parents=True, exist_ok=True)
    (quarantine / "quality-report.json").write_text(
        json.dumps(
            {
                "partition_id": "bad-partition",
                "issues": [{"message": "close is negative"}],
                "blocking": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_coverage_store(data_root: Path) -> None:
    """Write two silver parquet partitions so /api/health coverage is real data."""

    store = MarketStore(data_root)
    try:
        run_ok = store.begin_run("pytdx")
        store.write_silver_bars(
            PartitionKey("CN", "STOCK", "1d", 2026, "CN-STOCK-1d-2026"),
            [_full_bar()],
            "2026-08-07T10:00:00+08:00",
            run_ok,
        )
        hk_bar = {**_full_bar(), "instrument_id": "HK.HKEX.STOCK.00700"}
        store.write_silver_bars(
            PartitionKey("HK", "STOCK", "1d", 2026, "HK-STOCK-1d-2026"),
            [hk_bar],
            "2026-08-07T10:00:00+08:00",
            run_ok,
        )
        store.finish_run(run_ok, "COMPLETE", "coverage fixture")
    finally:
        store.close()


def test_health_coverage_uses_compact_kline_manifest_when_ready(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_coverage_store(data_root)
    KLineQueryStore(data_root).rebuild()

    coverage = build_control_center_report(data_root)["coverage"]

    assert coverage["source"] == "kline-query-manifest"
    assert coverage["total_instruments"] == 2
    assert coverage["total_rows"] == 2


def test_parse_sync_filter_defaults() -> None:
    sync_filter = parse_sync_filter({})
    assert sync_filter.markets == ("CN", "HK", "GLOBAL")
    assert sync_filter.min_market_cap is None
    assert sync_filter.min_hk_market_cap is None
    assert sync_filter.min_amount is None
    assert sync_filter.amount_rank_top_n is None
    assert sync_filter.min_futures_capital is None
    assert sync_filter.datasets == ()


def test_parse_sync_filter_parses_positive_values_and_multi_selects() -> None:
    sync_filter = parse_sync_filter(
        {
            "markets": ["CN,HK"],
            "min_market_cap": ["200"],
            "min_hk_market_cap": ["200"],
            "min_amount": ["100"],
            "amount_rank_top_n": ["10"],
            "min_futures_capital": ["20"],
            "datasets": ["CN_STOCK_BAR,FUTURES_BREADTH"],
        }
    )
    assert sync_filter.markets == ("CN", "HK")
    assert sync_filter.min_market_cap == 200.0
    assert sync_filter.min_hk_market_cap == 200.0
    assert sync_filter.min_amount == 100.0
    assert sync_filter.amount_rank_top_n == 10
    assert sync_filter.min_futures_capital == 20.0
    assert sync_filter.datasets == ("CN_STOCK_BAR", "FUTURES_BREADTH")


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"markets": ["US"]}, "unknown markets"),
        ({"datasets": ["NOT_A_DATASET"]}, "unknown datasets"),
        ({"min_market_cap": ["abc"]}, "must be a number"),
        ({"min_market_cap": ["-1"]}, "must be positive"),
        ({"min_amount": ["0"]}, "must be positive"),
        ({"amount_rank_top_n": ["1.5"]}, "must be an integer"),
        ({"amount_rank_top_n": ["0"]}, "must be positive"),
    ],
)
def test_parse_sync_filter_rejects_invalid_input(params: dict[str, list[str]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_sync_filter(params)


def test_build_sync_plan_selects_all_or_explicit_datasets() -> None:
    registry = dataset_index()
    all_plan = build_sync_plan(SyncFilter(), now=datetime(2026, 8, 8, tzinfo=timezone.utc))
    assert all_plan["status"] == "PLANNED"
    assert all_plan["selected_datasets"] == list(registry)
    assert all_plan["selected_dataset_count"] == len(registry)
    assert all_plan["created_at"] == "2026-08-08T00:00:00+00:00"
    assert all_plan["filter"]["markets"] == ["CN", "HK", "GLOBAL"]

    filtered = build_sync_plan(
        SyncFilter(markets=("CN",), min_market_cap=200.0, datasets=("CN_STOCK_BAR",)),
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    assert filtered["selected_datasets"] == ["CN_STOCK_BAR"]
    assert filtered["filter"]["min_market_cap"] == 200.0
    assert filtered["filter"]["markets"] == ["CN"]


def test_build_control_center_report_reads_store_and_quarantine(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_store(data_root)
    report = build_control_center_report(data_root, now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc))

    assert report["stats"]["run_count"] == 2
    assert report["stats"]["partition_count"] == 1
    assert report["stats"]["total_rows"] == 1
    assert report["stats"]["quarantine_count"] == 1
    assert report["stats"]["failed_providers"] == ["akshare"]
    runs_by_provider = {run["provider"]: run for run in report["runs"]}
    assert set(runs_by_provider) == {"pytdx", "akshare"}
    assert runs_by_provider["pytdx"]["status"] == "COMPLETE"
    assert runs_by_provider["akshare"]["status"] == "FAILED"
    assert report["partitions"][0]["partition_id"] == "CN-STOCK-1d-2026"
    assert report["partitions"][0]["status"] == "COMPLETE"
    assert report["quarantine"][0]["issue_count"] == 1
    assert report["quarantine"][0]["blocking"] is True
    assert report["storage"]["total"] >= 0
    assert {item["dataset_id"] for item in report["datasets"]} == set(dataset_index())
    assert report["fetch_jobs"] == {"latest": None, "tasks": []}


def test_build_control_center_report_reads_fetch_session_summary(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_store(data_root)
    summary = {
        "session_id": "session-test",
        "started_at": "2026-08-08T12:00:00+00:00",
        "completed_at": "2026-08-08T12:01:00+00:00",
        "status": "PARTIAL_FAILURE",
        "task_count": 2,
        "passed": 1,
        "partial_failure": 1,
        "failed": 0,
        "blocked": 0,
        "total_rows": 42,
        "tasks": [
            {
                "dataset_id": "CN_STOCK_BAR",
                "dataset_name": "A股个股K线（样本）",
                "source": "pytdx",
                "status": "PASS",
                "rows": 40,
                "detail": "ok",
                "error": None,
                "started_at": "2026-08-08T12:00:00+00:00",
                "completed_at": "2026-08-08T12:00:30+00:00",
            },
            {
                "dataset_id": "USD_INDEX_VIX",
                "dataset_name": "美元指数/VIX",
                "source": "yahoo",
                "status": "PARTIAL_FAILURE",
                "rows": 2,
                "detail": "partial",
                "error": "VIX:timeout",
                "started_at": "2026-08-08T12:00:00+00:00",
                "completed_at": "2026-08-08T12:00:45+00:00",
            },
        ],
    }
    (data_root / "control_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False),
        encoding="utf-8",
    )

    report = build_control_center_report(data_root, now=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc))
    assert report["fetch_jobs"]["latest"]["session_id"] == "session-test"
    assert report["fetch_jobs"]["latest"]["status"] == "PARTIAL_FAILURE"
    assert report["fetch_jobs"]["latest"]["total_rows"] == 42
    assert [task["dataset_id"] for task in report["fetch_jobs"]["tasks"]] == ["CN_STOCK_BAR", "USD_INDEX_VIX"]


class _Server:
    def __init__(self, data_root: Path) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(data_root, quiet=True))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def http_server(tmp_path: Path):
    data_root = tmp_path / "data"
    _write_store(data_root)
    server = _Server(data_root)
    yield server
    server.close()


def test_http_health_datasets_and_index_page(http_server: _Server) -> None:
    with urlopen(f"{http_server.base_url}/", timeout=5) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"
        assert "MarketListener" in response.read().decode("utf-8")

    with urlopen(f"{http_server.base_url}/api/health", timeout=5) as response:
        report = json.loads(response.read().decode("utf-8"))
        assert report["stats"]["run_count"] == 2
        assert report["stats"]["quarantine_count"] == 1

    with urlopen(f"{http_server.base_url}/api/datasets", timeout=5) as response:
        datasets = json.loads(response.read().decode("utf-8"))
        assert {item["dataset_id"] for item in datasets} == set(dataset_index())


def test_http_health_reports_real_parquet_coverage(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_coverage_store(data_root)
    server = _Server(data_root)
    try:
        with urlopen(f"{server.base_url}/api/health", timeout=5) as response:
            report = json.loads(response.read().decode("utf-8"))
        coverage = report["coverage"]
        assert coverage["available"] is True
        assert coverage["total_instruments"] == 2
        assert coverage["total_rows"] == 2
        groups = {item["market"]: item for item in coverage["groups"]}
        assert groups["CN"]["asset_type"] == "STOCK"
        assert groups["CN"]["instruments"] == 1
        assert groups["HK"]["rows"] == 1
    finally:
        server.close()


def test_http_android_package_info_returns_404_without_package(tmp_path: Path) -> None:
    server = _Server(tmp_path / "data")
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(f"{server.base_url}/api/android-package-info", timeout=5)
        assert error.value.code == 404
        with pytest.raises(HTTPError) as error:
            urlopen(f"{server.base_url}/api/android-package", timeout=5)
        assert error.value.code == 404
    finally:
        server.close()


def test_http_android_package_download_returns_signed_zip(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_package_root(data_root)
    server = _Server(data_root)
    try:
        with urlopen(f"{server.base_url}/api/android-package-info", timeout=5) as response:
            info = json.loads(response.read().decode("utf-8"))
        assert info["package_id"].startswith("market-")
        assert info["status"] == "ACTIVE"
        assert info["package_bytes"] > 0

        with urlopen(f"{server.base_url}/api/android-package", timeout=5) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/zip"
            body = response.read()
        assert body.startswith(b"PK")
        assert len(body) == info["package_bytes"]
    finally:
        server.close()


def test_http_sync_plan_validates_and_rejects_bad_input(http_server: _Server) -> None:
    with urlopen(
        f"{http_server.base_url}/api/sync-plan?markets=CN&min_market_cap=200&datasets=CN_STOCK_BAR",
        timeout=5,
    ) as response:
        plan = json.loads(response.read().decode("utf-8"))
        assert plan["status"] == "PLANNED"
        assert plan["selected_datasets"] == ["CN_STOCK_BAR"]
        assert plan["filter"]["min_market_cap"] == 200.0

    with pytest.raises(HTTPError) as error:
        urlopen(f"{http_server.base_url}/api/sync-plan?markets=US", timeout=5)
    assert error.value.code == 400
    assert "unknown markets" in error.value.read().decode("utf-8")

    with pytest.raises(HTTPError) as error:
        urlopen(f"{http_server.base_url}/api/unknown", timeout=5)
    assert error.value.code == 404

    with pytest.raises(HTTPError) as error:
        urlopen(
            f"{http_server.base_url}/api/sync-plan",
            data=b"",
            timeout=5,
        )
    assert error.value.code == 405


def test_serve_control_center_honours_timeout_and_validates(tmp_path: Path) -> None:
    host, port = serve_control_center(tmp_path / "data", host="127.0.0.1", port=0, timeout_seconds=0.3, quiet=True)
    assert host == "127.0.0.1"
    assert isinstance(port, int) and port > 0
    with pytest.raises(ValueError, match="timeout_seconds"):
        serve_control_center(tmp_path / "data", timeout_seconds=0)
