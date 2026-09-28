"""FastAPI host for the local investment-research terminal.

The application is deliberately a thin presentation adapter.  It reads the
same local F10 and control-centre data already produced by the service layer;
it neither shells out to the CLI nor introduces another industry database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import socket
import threading
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from .control_center import build_control_center_report
from .dataset_catalog import dataset_index
from .event_log import EventLog
from .industry_graph.f10 import CompanyRepository
from .package_builder import latest_package_info
from .operations import OperationKind, OperationManager
from .web_api import dashboard as dashboard_api
from .web_api import data_sections as data_sections_api
from .web_api import futures as futures_api
from .web_api import market as market_api
from .web_api import sources as sources_api
from .web_api import stats as stats_api
from .web_api import strategy as strategy_api
from .web_api import signals as signals_api
from .web_api import composites as composites_api
from .web_api import watchlist as watchlist_api


_WEB_ROUTES = {
    "/",
    "/market/",
    "/market/all/",
    "/market/targets/",
    "/futures/",
    "/settings/",
    "/data/",
    "/strategy/",
    "/stats/",
    "/f10/",
    "/industry/",
    "/logs/",
    "/data-sources/",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


class _OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: OperationKind


def create_web_app(
    data_root: Path,
    *,
    web_dist: Path | None = None,
    operation_manager: OperationManager | None = None,
    auto_update_on_startup: bool = False,
) -> FastAPI:
    """Create the FastAPI surface, optionally queuing startup data refreshes."""

    root = Path(data_root)
    dist = web_dist or Path(__file__).with_name("web_dist")
    repository = CompanyRepository(root)
    events = EventLog(root)
    operations = operation_manager or OperationManager(root, _operation_handlers(root), event_sink=events.append)
    startup_operations = (
            OperationKind.MARKET_UPDATE,
            OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
            OperationKind.TDX_COVERAGE_CHECK,
            OperationKind.FUTURES_STRUCTURE_UPDATE,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if auto_update_on_startup:
            for kind in startup_operations:
                operations.submit(kind)
        yield

    app = FastAPI(
        title="MarketListener Local Research Terminal", docs_url=None, redoc_url=None,
        lifespan=lifespan,
    )
    app.state.data_root = root
    app.include_router(market_api.router)
    app.include_router(sources_api.router)
    app.include_router(watchlist_api.router)
    app.include_router(strategy_api.router)
    app.include_router(signals_api.router)
    app.include_router(composites_api.router)
    app.include_router(stats_api.router)
    app.include_router(futures_api.router)
    app.include_router(data_sections_api.router)
    app.include_router(dashboard_api.dashboard_router)
    app.include_router(dashboard_api.metrics_router)

    @app.middleware("http")
    async def loopback_mutations_only(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            host = request.client.host if request.client else None
            if host not in _LOOPBACK_HOSTS:
                return JSONResponse(status_code=403, content={"detail": "mutation APIs are loopback-only"})
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return build_control_center_report(root)

    @app.get("/api/ready")
    def ready() -> dict[str, str]:
        return {"status": "READY"}

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon_svg() -> Response:
        path = dist / "favicon.svg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="favicon is not built")
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    def legacy_favicon() -> RedirectResponse:
        return RedirectResponse("/favicon.svg", status_code=307)

    @app.get("/api/datasets")
    def datasets() -> list[dict[str, object]]:
        return [definition.to_dict() for definition in dataset_index().values()]

    @app.get("/api/data/{view}")
    def data_view(
        view: str,
        q: str = "",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=500),
        sort: str = "",
        direction: Literal["asc", "desc"] = "asc",
    ) -> dict[str, object]:
        rows = _data_rows(root, repository, view)
        keyword = q.strip().casefold()
        if keyword:
            rows = [row for row in rows if keyword in json.dumps(row, ensure_ascii=False).casefold()]
        if sort:
            if not all(sort in row for row in rows):
                raise HTTPException(status_code=400, detail="sort is not available for this data view")
            rows.sort(key=lambda row: str(row[sort] or ""), reverse=direction == "desc")
        total = len(rows)
        start = (page - 1) * page_size
        return {"view": view, "items": rows[start : start + page_size], "total": total, "page": page, "pageSize": page_size}

    @app.get("/api/operations")
    def list_operations() -> dict[str, object]:
        return {"items": [operation.to_dict() for operation in operations.list()]}

    @app.get("/api/logs")
    def logs(
        category: str | None = None,
        status: str | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        try:
            return events.page(category=category, status=status, page=page, page_size=page_size)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/operations", status_code=202)
    def create_operation(request: _OperationRequest) -> dict[str, object]:
        operation, created = operations.submit(request.kind)
        return {"created": created, "operation": operation.to_dict()}

    @app.post("/api/operations/{operation_id}/cancel")
    def cancel_operation(operation_id: str) -> dict[str, object]:
        operation = operations.cancel(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return {"operation": operation.to_dict()}

    @app.get("/api/tdx-afterhours/coverage")
    def tdx_afterhours_coverage() -> dict[str, object]:
        """Return the latest local raw-file coverage report, if one exists.

        The endpoint deliberately returns only aggregate/sampled evidence.  A
        coverage scan can touch tens of thousands of raw terminal files, so
        exposing its complete per-file report through the dashboard would make
        a read-only status page unexpectedly expensive.
        """

        return _tdx_coverage_payload(root)

    @app.get("/api/f10/companies")
    def companies(
        q: str = "",
        market: str | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=500),
        sort: Literal["name", "code", "market", "updatedAt", "totalMarketCap"] = "name",
        direction: Literal["asc", "desc"] = "asc",
    ) -> dict[str, object]:
        try:
            return repository.list_companies(
                query=q,
                market=market,
                page=page,
                page_size=page_size,
                sort=sort,
                descending=direction == "desc",
            ).to_dict()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/f10/companies/{instrument_key}")
    def company(instrument_key: str) -> dict[str, object]:
        detail = repository.company(instrument_key)
        if detail is None:
            raise HTTPException(status_code=404, detail="company not found")
        return _with_chain_locations(root, detail).to_dict()

    @app.get("/api/android-package-info")
    def android_package_info() -> dict[str, object]:
        info = latest_package_info(root)
        if info is None:
            raise HTTPException(status_code=404, detail="no active Android package")
        return info

    @app.get("/api/android-package")
    def android_package() -> Response:
        info = latest_package_info(root)
        if info is None:
            raise HTTPException(status_code=404, detail="no active Android package")
        package_path = root / "packages" / f"{info['package_id']}.zip"
        if not package_path.is_file():
            raise HTTPException(status_code=404, detail="active Android package file is missing")
        return FileResponse(package_path, media_type="application/zip", filename=package_path.name)

    @app.get("/api/industry/atlas")
    def industry_atlas() -> Response:
        for candidate in (root / "industry" / "industry-atlas.html", root.parent / "reports" / "industry" / "industry-atlas.html"):
            if candidate.is_file():
                return FileResponse(candidate, media_type="text/html; charset=utf-8")
        raise HTTPException(status_code=404, detail="industry atlas not built yet")

    @app.get("/industry-v2/")
    @app.get("/industry-v2")
    def legacy_industry_v2_redirect() -> RedirectResponse:
        return RedirectResponse("/industry/", status_code=307)

    if dist.is_dir() and (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    def shell() -> Response:
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html; charset=utf-8")
        return JSONResponse(
            status_code=503,
            content={"detail": "Vue terminal assets are not built; run npm run build in desktop/web."},
        )

    for route in sorted(_WEB_ROUTES):
        app.add_api_route(route, shell, methods=["GET"], include_in_schema=False)
    # Market detail is a client route rather than an API resource.  Serving the
    # shell here keeps a refreshed or shared detail URL inside the SPA.
    app.add_api_route("/market/instrument/{instrument_id}", shell, methods=["GET"], include_in_schema=False)
    app.add_api_route("/f10/company/{instrument_key}", shell, methods=["GET"], include_in_schema=False)
    return app


def _operation_handlers(data_root: Path) -> dict[OperationKind, object]:
    """Bind the finite operation enum directly to existing Python services."""

    from .collector import run_fetch_session
    from .f10 import run_f10_fetch, run_revenue_fetch
    from .industry_atlas import build_atlas
    from .package_builder import build_android_package
    from .report_pipeline import build_chain_index, process_report_batch, verify_report_batch
    from .futures_calendar import load_futures_trading_calendar, sync_futures_trading_calendar
    from .futures_official_backfill import default_backfill_range, run_official_futures_backfill
    from .futures_official_structure import run_official_product_structure_pipeline
    from .futures_structure import (
        run_member_open_interest_structure_pipeline,
        run_product_notional_structure_pipeline,
        run_product_open_interest_structure_pipeline,
    )
    from .tdx_local import run_tdx_local_import

    desktop_root = Path(__file__).resolve().parents[2]
    repository_root = desktop_root.parent
    report_root = repository_root / "行业产业链研报"
    output_root = repository_root / "reports" / "industry"
    private_key = desktop_root / "keys" / "market_package_private_key.pem"
    ecdsa_private_key = desktop_root / "keys" / "market_package_ecdsa_private.pem"

    def invoke(callable_: object) -> str:
        result = callable_()  # type: ignore[operator]
        return json.dumps(result, ensure_ascii=False, sort_keys=True) if isinstance(result, dict) else str(result or "PASS")

    def check_tdx_coverage() -> str:
        from .tdx_coverage import run_tdx_coverage_check

        report = run_tdx_coverage_check(data_root)
        summary = report.get("summary") if isinstance(report, dict) else None
        summary = summary if isinstance(summary, dict) else {}
        files = _nonnegative_int(summary.get("files"))
        gaps = _nonnegative_int(summary.get("gapFiles"))
        unreadable = _nonnegative_int(summary.get("unreadableFiles"))
        writing = _nonnegative_int(summary.get("writingFiles"))
        unknown = _nonnegative_int(summary.get("unknownCalendarFiles"))
        detail = (
            f"通达信本地K线检查：文件 {files}，缺口文件 {gaps}，"
            f"不可读 {unreadable}，写入中 {writing}，待日历/时段核验 {unknown}。"
        )
        if str(report.get("status") or "").upper() == "PASS" and not (gaps or unreadable or writing or unknown):
            return f"PASS: {detail}"
        return f"PARTIAL_FAILURE: {detail} 详情见仪表盘最近检查结果。"

    def download_tdx_afterhours() -> str:
        """Run only when explicitly queued; terminal launch/login stay manual."""

        from .tdx_download_ui import main as tdx_download_main

        state_dir = data_root / "state" / "tdx_download_ui"
        exit_code = tdx_download_main(["--execute", "--state-dir", str(state_dir)])
        latest = _read_json_object(state_dir / "latest.json")
        status = str(latest.get("status") or "UNKNOWN")
        terminals = latest.get("terminals")
        terminal_text = ", ".join(
            f"{key}={value}" for key, value in sorted(terminals.items())
        ) if isinstance(terminals, dict) else ""
        if exit_code == 0 and status == "UI_DONE_UNVERIFIED":
            suffix = f"（{terminal_text}）" if terminal_text else ""
            return (
                "PASS: 通达信界面下载已完成"
                f"{suffix}；这只证明界面完成，仍请运行“检查通达信盘后数据”核验文件覆盖。"
            )
        return (
            "PARTIAL_FAILURE: 通达信盘后下载未确认完成"
            f"（{status}{'；' + terminal_text if terminal_text else ''}）。"
            "请先在启动器中启动并登录两款终端，再重试下载；"
            "若期货通菜单无法自动打开，请手动打开“盘后数据下载”窗口。"
        )

    def update_futures_heat() -> str:
        from .futures_contract_heat_update import run_futures_contract_heat_update

        summary = run_futures_contract_heat_update(data_root)
        latest = summary.get("calculation", {})
        official = summary.get("officialTurnover", {})
        venues = official.get("exchanges", []) if isinstance(official, dict) else []
        passed = sum(isinstance(item, dict) and item.get("status") == "PASS" for item in venues)
        total = len(venues)
        missing = [str(item.get("exchange")) for item in venues if isinstance(item, dict) and item.get("status") != "PASS"]
        state = "PASS" if summary.get("status") == "PASS" else "PARTIAL_FAILURE"
        missing_text = f"；缺失/待核验交易所：{'、'.join(missing)}" if missing else ""
        return (
            f"{state}: 期货月份合约热度已刷新至 {latest.get('latestDay') or '暂无日期'}；"
            f"官方逐合约成交额 {passed}/{total} 个交易所通过，"
            f"成交额覆盖 {float(latest.get('latestTurnoverCoverage') or 0):.1%}"
            f"{missing_text}。"
        )

    def import_tdx_local_data() -> str:
        from datetime import datetime, timedelta

        from .tdx_afterhours import BEIJING

        download_state = _read_json_object(data_root / "state" / "tdx_download_ui" / "latest.json")
        try:
            slot_time = datetime.fromisoformat(str(download_state.get("slot") or ""))
            age = datetime.now(BEIJING) - slot_time.astimezone(BEIJING)
            fresh_slot = timedelta(0) <= age <= timedelta(hours=24)
        except ValueError:
            fresh_slot = False
        if (
            str(download_state.get("status") or "").upper() != "UI_DONE_UNVERIFIED"
            or not fresh_slot
        ):
            status = str(download_state.get("status") or "NO_CONFIRMED_DOWNLOAD")
            return (
                "PARTIAL_FAILURE: 通达信盘后下载未确认完成，跳过本地文件导入，"
                f"以免读取仍在写入的数据（{status}）。"
            )
        summary = run_tdx_local_import(data_root)
        state = str(summary.get("状态") or "")
        import_status = "PASS" if state == "完成" else "PARTIAL_FAILURE"
        heat_detail = update_futures_heat()
        heat_status = "PARTIAL_FAILURE" if heat_detail.startswith("PARTIAL_FAILURE") else "PASS"
        status = "PASS" if import_status == heat_status == "PASS" else "PARTIAL_FAILURE"
        return (
            f"{status}: 通达信本地数据导入扫描 {summary.get('扫描文件', 0)} 个文件、"
            f"写入 {summary.get('写入K线', 0)} 根K线（{state or '状态未知'}）；{heat_detail}"
        )

    def latest_completed_futures_day() -> str:
        if load_futures_trading_calendar(data_root) is None:
            sync_futures_trading_calendar(data_root)
        _start_day, end_day = default_backfill_range(data_root)
        return end_day

    def update_official_futures_daily() -> str:
        day = latest_completed_futures_day()
        daily = run_official_futures_backfill(
            data_root, start_day=day, end_day=day, max_days=1,
        )
        structures = [
            run_official_product_structure_pipeline(
                data_root, metric=metric, start_day=day, end_day=day,
            )
            for metric in ("open_interest", "notional", "volume")
        ]
        passed = sum(item.get("status") == "PASS" for item in structures)
        status = "PASS" if daily.get("status") == "PASS" and passed == len(structures) else "PARTIAL_FAILURE"
        return (
            f"{status}: 官方期货日行情截至 {day}；来源日 {daily.get('passed', 0)}/{daily.get('attempts', 0)} 通过，"
            f"官方品种结构 {passed}/{len(structures)} 项完整。"
        )

    def rebuild_futures_structures() -> str:
        day = latest_completed_futures_day()
        summaries = [
            run_product_open_interest_structure_pipeline(data_root, start_day=day, end_day=day),
            run_product_notional_structure_pipeline(data_root, start_day=day, end_day=day),
            run_member_open_interest_structure_pipeline(data_root, start_day=day, end_day=day),
        ]

        def complete(item: dict[str, Any]) -> bool:
            if item.get("status") == "PASS":
                return True
            directions = item.get("directions")
            return isinstance(directions, dict) and bool(directions) and all(
                isinstance(value, dict) and value.get("status") == "PASS"
                for value in directions.values()
            )

        passed = sum(complete(item) for item in summaries)
        status = "PASS" if passed == len(summaries) else "PARTIAL_FAILURE"
        return f"{status}: 期货本地持仓、市值及席位结构已重建至 {day}，{passed}/{len(summaries)} 项完整。"

    return {
        OperationKind.MARKET_UPDATE: lambda: invoke(lambda: run_fetch_session(data_root)),
        OperationKind.F10_UPDATE_CN: lambda: invoke(lambda: run_f10_fetch(data_root, market="CN")),
        OperationKind.F10_UPDATE_HK: lambda: invoke(lambda: run_f10_fetch(data_root, market="HK")),
        OperationKind.REVENUE_UPDATE: lambda: invoke(lambda: run_revenue_fetch(data_root, market="CN")),
        OperationKind.REPORT_PROCESS: lambda: invoke(lambda: process_report_batch(report_root, output_root)),
        OperationKind.REPORT_VERIFY: lambda: invoke(lambda: verify_report_batch(output_root)),
        OperationKind.CHAIN_REBUILD: lambda: invoke(lambda: build_chain_index(output_root)),
        OperationKind.ATLAS_BUILD: lambda: invoke(lambda: build_atlas(output_root, data_root=data_root)),
        OperationKind.ANDROID_PACKAGE_BUILD: lambda: invoke(
            lambda: build_android_package(data_root, private_key, ecdsa_private_key=ecdsa_private_key)
        ),
        OperationKind.STATUS_REFRESH: lambda: "PASS: status refresh is served from current local snapshots",
        OperationKind.TDX_COVERAGE_CHECK: check_tdx_coverage,
        OperationKind.TDX_AFTERHOURS_DOWNLOAD: download_tdx_afterhours,
        OperationKind.TDX_LOCAL_IMPORT: import_tdx_local_data,
        OperationKind.FUTURES_HEAT_UPDATE: update_futures_heat,
        OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE: update_official_futures_daily,
        OperationKind.FUTURES_STRUCTURE_UPDATE: rebuild_futures_structures,
    }


def _tdx_coverage_payload(data_root: Path) -> dict[str, object]:
    from .tdx_coverage import (
        coverage_groups,
        coverage_summary,
        financial_extended_groups,
        load_latest_tdx_coverage,
    )

    report = load_latest_tdx_coverage(data_root)
    if not isinstance(report, dict):
        return {"available": False}
    raw_files = report.get("files")
    native_files = (
        isinstance(raw_files, list) and bool(raw_files)
        and all(isinstance(item, dict) and "terminal" in item and "period" in item for item in raw_files)
    )
    raw_groups = coverage_groups(raw_files) if native_files else report.get("groups")
    groups: list[dict[str, object]] = []
    legacy_group_shape = not native_files and isinstance(raw_groups, list) and any(
        isinstance(item, dict) and ("earliestAt" in item or "latestAt" in item)
        for item in raw_groups
    )
    group_fields = (
        (
            "terminal", "period", "files", "readableFiles", "unreadableFiles", "writingFiles",
            "earliestAt", "latestAt", "records", "continuousFiles", "gapFiles",
            "unknownCalendarFiles", "gapCount", "sampleGaps",
        )
        if legacy_group_shape else (
            "terminal", "period", "files", "readableFiles", "unreadableFiles", "writingFiles",
            "commonStartDate", "commonStartFiles", "commonLatestDate", "commonLatestFiles",
            "records", "continuousFiles", "gapFiles", "unknownCalendarFiles", "gapCount", "sampleGaps",
        )
    )
    if isinstance(raw_groups, list):
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            groups.append(
                {
                    key: item.get(key)
                    for key in group_fields
                }
            )
    raw_summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    summary = coverage_summary(raw_files) if native_files else dict(raw_summary)
    for key in ("scannedFiles", "reusedFiles", "missingTerminals"):
        if key in raw_summary:
            summary[key] = raw_summary[key]
    raw_extended = report.get("extendedGroups")
    extended = raw_extended
    if not isinstance(extended, list) or any(
        isinstance(item, dict) and "commonStartDate" not in item for item in extended
    ):
        extended = financial_extended_groups(raw_files if native_files else [])
    return {
        "available": True,
        "generatedAt": report.get("generatedAt"),
        "status": report.get("status"),
        "summary": summary,
        "groups": groups,
        "extendedGroups": [
            {key: item.get(key) for key in (
                "prefix", "period", "files", "withRecords", "commonStartDate", "commonStartFiles",
                "commonLatestDate", "commonLatestFiles",
            )}
            for item in extended if isinstance(item, dict)
        ],
    }


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _data_rows(root: Path, repository: CompanyRepository, view: str) -> list[dict[str, Any]]:
    """Read one allow-listed monitoring view without exposing a SQL surface."""

    report = build_control_center_report(root)
    if view == "market":
        return list(report["coverage"].get("groups", []))
    if view == "silver":
        return list(report["partitions"])
    if view == "gold":
        return _gold_rows(root)
    if view == "f10":
        return [summary.to_dict() for summary in repository.list_companies(page_size=500).items]
    if view == "industry":
        return _industry_rows(root)
    if view == "runs":
        return list(report["runs"])
    if view == "partitions":
        return list(report["partitions"])
    if view == "quarantine":
        return list(report["quarantine"])
    if view == "package":
        return [report["android_package"]] if report["android_package"] else []
    if view == "storage":
        return [{"area": key, "bytes": value} for key, value in report["storage"].items()]
    if view == "quality":
        return [
            {"partition_id": row.get("partition_id"), "status": row.get("status"), "issue_count": 0, "stale": row.get("stale", False)}
            for row in report["partitions"]
        ] + list(report["quarantine"])
    if view == "freshness":
        return [
            {"partition_id": row.get("partition_id"), "updated_at": row.get("updated_at"), "data_cutoff": row.get("data_cutoff"), "stale": row.get("stale", False)}
            for row in report["partitions"]
        ]
    raise HTTPException(status_code=404, detail="unknown data view")


def _gold_rows(root: Path) -> list[dict[str, Any]]:
    catalog = root / "catalog.duckdb"
    if not catalog.is_file():
        return []
    try:
        import duckdb

        connection = duckdb.connect(str(catalog))
        try:
            cursor = connection.execute("SELECT * FROM gold_metrics ORDER BY timestamp DESC LIMIT 500")
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        finally:
            connection.close()
    except Exception:
        return []


def _industry_rows(root: Path) -> list[dict[str, Any]]:
    for candidate in (root / "industry" / "industry-atlas.json", root.parent / "reports" / "industry" / "industry-atlas.json"):
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        chains = document.get("chains") if isinstance(document, dict) else None
        if isinstance(chains, list):
            return [
                {"chain": chain.get("name"), "stages": len(chain.get("stages") or []), "subChains": len(chain.get("sub_chains") or [])}
                for chain in chains if isinstance(chain, dict)
            ]
    return []


def _with_chain_locations(root: Path, detail: Any) -> Any:
    """Add local Atlas locations to detail without copying F10 data into nodes."""

    locations: list[dict[str, str]] = []
    key = detail.summary.instrument_key
    for candidate in (root / "industry" / "industry-atlas.json", root.parent / "reports" / "industry" / "industry-atlas.json"):
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for chain in document.get("chains", []) if isinstance(document, dict) else []:
            if not isinstance(chain, dict):
                continue
            for stage in chain.get("stages", []) or []:
                if not isinstance(stage, dict):
                    continue
                for node in stage.get("cards", []) or []:
                    if isinstance(node, dict) and key in (node.get("companyRefs") or []):
                        locations.append(
                            {
                                "chain": str(chain.get("name") or ""),
                                "stage": str(stage.get("name") or ""),
                                "node": str(node.get("name") or ""),
                            }
                        )
        break
    return replace(detail, chain_locations=tuple(locations))


def serve_web_app(
    data_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    timeout_seconds: float | None = None,
    quiet: bool = False,
    auto_update_on_startup: bool = True,
) -> tuple[str, int]:
    """Serve the FastAPI terminal, retaining deterministic timeout support for CLI tests."""

    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(socket.SOMAXCONN)
    actual_port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        create_web_app(data_root, auto_update_on_startup=auto_update_on_startup),
        host=host,
        port=actual_port,
        log_level="warning" if quiet else "info",
        access_log=not quiet,
    )
    server = uvicorn.Server(config)
    timer: threading.Timer | None = None
    if timeout_seconds is not None:
        timer = threading.Timer(timeout_seconds, setattr, args=(server, "should_exit", True))
        timer.daemon = True
        timer.start()
    try:
        if not quiet:
            print(f"market-monitor local research terminal: http://{host}:{actual_port}/")
        server.run(sockets=[listener])
    finally:
        if timer is not None:
            timer.cancel()
        listener.close()
    return host, actual_port


__all__ = ("create_web_app", "serve_web_app")
