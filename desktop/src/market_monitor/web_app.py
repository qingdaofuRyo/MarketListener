"""FastAPI host for the local investment-research terminal.

The application is deliberately a thin presentation adapter.  It reads the
same local F10 and control-centre data already produced by the service layer;
it neither shells out to the CLI nor introduces another industry database.
"""

from __future__ import annotations

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
from .web_api import watchlist as watchlist_api


_WEB_ROUTES = {
    "/",
    "/market/",
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
) -> FastAPI:
    """Create the read-only FastAPI surface for the Vue terminal shell."""

    root = Path(data_root)
    dist = web_dist or Path(__file__).with_name("web_dist")
    repository = CompanyRepository(root)
    events = EventLog(root)
    operations = operation_manager or OperationManager(root, _operation_handlers(root), event_sink=events.append)
    app = FastAPI(title="MarketListener Local Research Terminal", docs_url=None, redoc_url=None)
    app.state.data_root = root
    app.include_router(market_api.router)
    app.include_router(sources_api.router)
    app.include_router(watchlist_api.router)
    app.include_router(strategy_api.router)
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
    app.add_api_route("/f10/company/{instrument_key}", shell, methods=["GET"], include_in_schema=False)
    return app


def _operation_handlers(data_root: Path) -> dict[OperationKind, object]:
    """Bind the finite operation enum directly to existing Python services."""

    from .collector import run_fetch_session
    from .f10 import run_f10_fetch, run_revenue_fetch
    from .industry_atlas import build_atlas
    from .package_builder import build_android_package
    from .report_pipeline import build_chain_index, process_report_batch, verify_report_batch

    desktop_root = Path(__file__).resolve().parents[2]
    repository_root = desktop_root.parent
    report_root = repository_root / "行业产业链研报"
    output_root = repository_root / "reports" / "industry"
    private_key = desktop_root / "keys" / "market_package_private_key.pem"
    ecdsa_private_key = desktop_root / "keys" / "market_package_ecdsa_private.pem"

    def invoke(callable_: object) -> str:
        result = callable_()  # type: ignore[operator]
        return json.dumps(result, ensure_ascii=False, sort_keys=True) if isinstance(result, dict) else str(result or "PASS")

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
    }


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
        create_web_app(data_root),
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
