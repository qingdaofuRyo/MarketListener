"""FastAPI host for the local investment-research terminal.

The application is deliberately a thin presentation adapter.  It reads the
same local F10 and control-centre data already produced by the service layer;
it neither shells out to the CLI nor introduces another industry database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import socket
import threading
import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from .control_center import build_control_center_report
from .data_page_auto_refresh import due_data_updates
from .futures_auto_refresh import (AUTO_KINDS, CHECK_INTERVAL_SECONDS,
                                   RANK_CONTINUATION_CHECK_INTERVAL_SECONDS,
                                   due_operations, due_rank_continuation, target_day)
from .market_auto_refresh import due_local_market_updates
from .tdx_gui_auto_refresh import (due_financial_download, due_futures_download,
                                   reconcile_due_unknown_downloads)
from .futures_calendar import load_futures_trading_calendar
from .dataset_catalog import dataset_index
from .event_log import EventLog
from .industry_graph.f10 import CompanyRepository
from .package_builder import latest_package_info
from .operations import ContextOperationHandler, OperationContext, OperationKind, OperationManager, OperationStatus
from .market_query_cache import KLineCacheUnavailable
from .service_startup_stage import StartupStageReporter
from .web_api import dashboard as dashboard_api
from .web_api import data_sections as data_sections_api
from .web_api import data_focus as data_focus_api
from .web_api import futures as futures_api
from .web_api import market as market_api
from .web_api import sources as sources_api
from .web_api import stats as stats_api
from .web_api import strategy as strategy_api
from .web_api import signals as signals_api
from .web_api import composites as composites_api
from .web_api import watchlist as watchlist_api
from .web_api import research as research_api
from .web_api import disclosure_watch as disclosure_watch_api


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
    "/quant/",
    "/logs/",
    "/data-sources/",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


class _OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: OperationKind


class _AccountPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmationDigest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _BackupVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^profile-\d{8}T\d{6}Z-[0-9a-f]{8}\.mlbackup$")


def create_web_app(
    data_root: Path,
    *,
    web_dist: Path | None = None,
    operation_manager: OperationManager | None = None,
    auto_update_on_startup: bool = False,
    auto_backup_on_startup: bool = False,
    auto_publish_on_startup: bool = False,
) -> FastAPI:
    """Create the FastAPI surface, optionally queuing startup data refreshes."""

    root = Path(data_root)
    startup_stage = StartupStageReporter(root)
    startup_stage.mark("APP_CONSTRUCTION")
    dist = web_dist or Path(__file__).with_name("web_dist")
    repository = CompanyRepository(root)
    events = EventLog(root)
    operations = operation_manager or OperationManager(root, _operation_handlers(root), event_sink=events.append)
    index_source_kinds = {
        OperationKind.MARKET_UPDATE, OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE,
        OperationKind.TDX_LOCAL_IMPORT, OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE,
        OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE,
        OperationKind.F10_UPDATE_CN, OperationKind.F10_UPDATE_HK,
    }

    def index_recovery_allowed() -> bool:
        return not any(
            item.kind in index_source_kinds and item.status in {
                OperationStatus.QUEUED, OperationStatus.RUNNING,
            }
            for item in operations.list()
        )

    @asynccontextmanager
    async def _service_lifespan(_app: FastAPI):
        publication_stop = threading.Event()
        publication_watch: threading.Thread | None = None
        startup_recovery: threading.Thread | None = None
        futures_watch: threading.Thread | None = None
        gui_watch: threading.Thread | None = None
        rank_watch: threading.Thread | None = None
        kpl_watch: threading.Thread | None = None
        ths_etf_watch: threading.Thread | None = None
        _app.state.silver_recovery_status = {"status": "NOT_NEEDED"}
        from .local_backup import recover_interrupted_profile_restore

        startup_stage.mark("RESTORE_RECOVERY")
        await asyncio.to_thread(recover_interrupted_profile_restore, root, offline=True)
        if (root / "catalog.duckdb").is_file():
            startup_stage.mark("CATALOG_MIGRATION")
            from .storage import MarketStore

            # Complete migration before readiness; a failed migration cannot
            # expose read APIs expecting columns that do not exist yet.
            def prepare_catalog() -> None:
                store = MarketStore(root)
                store.close()
                from .market_data_version import market_data_version, migrate_legacy_market_data_version

                migrate_legacy_market_data_version(root)
                market_data_version(root)

            await asyncio.to_thread(prepare_catalog)
        startup_stage.mark("QUERY_INDEX_SETUP")
        from .market_query_cache import get_kline_query_store

        query_store = get_kline_query_store(root)
        query_store.set_rebuild_allowed(index_recovery_allowed)
        if auto_publish_on_startup:
            startup_stage.mark("READMODEL_CHECK")
            from .market_readmodel import inspect_market_readmodel

            if (root / "catalog.duckdb").is_file() and (root / "silver").is_dir():
                # The initial job also asks the query index to rebuild when its
                # revision is missing; the watcher retries after it is ready.
                current_version = market_api._response_data_version(root)
                # The pointer only guides startup scheduling. Requests and
                # the watcher still verify payload SHA/schema before use.
                whole = inspect_market_readmodel(root, current_version=current_version, allow_stale=True)
                default = inspect_market_readmodel(root, current_version=current_version,
                                                   identity="category:cn-future-main", allow_stale=True)
                initial_needed = whole is None and default is None
                if initial_needed or (not auto_update_on_startup and (
                    not get_kline_query_store(root).status().ready or
                    whole is None or default is None or
                    whole.status != "CURRENT" or default.status != "CURRENT"
                )):
                    operations.submit(OperationKind.MARKET_READMODEL_PUBLISH)

            def watch_publication() -> None:
                from .market_query_cache import KLineCacheUnavailable, get_kline_query_store
                from .market_data_version import pending_market_writes, recover_abandoned_pending_write
                from .market_readmodel import load_market_readmodel

                next_recovery_attempt = 0.0
                while not publication_stop.wait(8):
                    try:
                        if not (root / "catalog.duckdb").is_file() or not (root / "silver").is_dir():
                            continue
                        if not index_recovery_allowed():
                            continue
                        if pending_market_writes(root)["status"] != "CLEAR":
                            if time.monotonic() >= next_recovery_attempt:
                                result = recover_abandoned_pending_write(root)
                                _app.state.silver_recovery_status = result
                                next_recovery_attempt = time.monotonic() + (
                                    8 if result["status"] in {"RECOVERED", "RETRY_LATER"} else 60
                                )
                            if pending_market_writes(root)["status"] != "CLEAR":
                                continue
                        query_store = get_kline_query_store(root)
                        if not query_store.status().ready:
                            try:
                                query_store.ensure_ready()
                            except KLineCacheUnavailable:
                                pass  # the index itself owns bounded rebuild/backoff state
                            continue
                        version = market_api._response_data_version(root)
                        if (load_market_readmodel(root, current_version=version, allow_stale=False) is not None and
                                load_market_readmodel(root, current_version=version,
                                                      identity="category:cn-future-main", allow_stale=False) is not None):
                            continue
                        recent = next((item for item in operations.list()
                                       if item.kind == OperationKind.MARKET_READMODEL_PUBLISH), None)
                        if recent is not None and recent.status in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
                            continue
                        if recent is not None and recent.status == OperationStatus.FAILED and recent.completed_at:
                            finished = datetime.fromisoformat(recent.completed_at)
                            if (datetime.now(timezone.utc) - finished).total_seconds() < 60:
                                continue
                        operations.submit(OperationKind.MARKET_READMODEL_PUBLISH)
                    except (OSError, ValueError, RuntimeError):
                        continue

            startup_stage.mark("BACKGROUND_WATCHER")
            publication_watch = threading.Thread(target=watch_publication,
                                                 name="marketlistener-publication-watch", daemon=True)
            publication_watch.start()
        startup_stage.mark("BACKUP_QUEUE")
        from .local_backup import profile_backup_due

        startup_backup_due = auto_backup_on_startup and profile_backup_due(root)
        backup_queued = False

        def gui_download_active() -> bool:
            return any(item.kind in {
                OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD,
                OperationKind.TDX_FUTURES_AUTO_DOWNLOAD,
            } and item.status in {OperationStatus.QUEUED, OperationStatus.RUNNING}
                       for item in operations.list())

        def submit_backup_if_gui_idle() -> None:
            nonlocal backup_queued
            if backup_queued or not startup_backup_due:
                return
            if gui_download_active():
                return
            operations.submit(OperationKind.BACKUP_CREATE)
            backup_queued = True

        if auto_update_on_startup:
            startup_stage.mark("UPDATE_QUEUE")
            from .market_data_version import pending_market_writes, recover_abandoned_pending_write
            from .kpl_auto_refresh import watch_kpl
            from .ths_app_etf_auto import watch as watch_ths_etf
            from .f10_auto_refresh import due_f10_updates, due_revenue_publication_recovery

            kpl_watch = threading.Thread(target=watch_kpl, args=(root, publication_stop),
                                         name="marketlistener-kpl-evidence-watch", daemon=True)
            kpl_watch.start()
            ths_etf_watch = threading.Thread(target=watch_ths_etf, args=(root, publication_stop),
                                             name="marketlistener-ths-etf-watch", daemon=True)
            ths_etf_watch.start()

            def submit_due_futures() -> None:
                calendar = load_futures_trading_calendar(root)
                for kind, _day in due_operations(calendar, operations.list(), datetime.now(timezone.utc)):
                    operations.submit(kind)

            def submit_due_data() -> None:
                for kind in due_data_updates(operations.list(), datetime.now(timezone.utc)):
                    operations.submit(kind)

            def submit_due_f10() -> None:
                from .f10 import read_f10_batch_summary

                items = operations.list()
                now = datetime.now(timezone.utc)
                pending_publication = (root / "f10" / "cn" / "revenue_publication_pending.json").exists()
                if due_revenue_publication_recovery(items, now, pending=pending_publication):
                    operations.submit(OperationKind.F10_REVENUE_REPUBLISH)
                for kind in due_f10_updates(items, now,
                                            revenue_summary=read_f10_batch_summary(root, "REVENUE_UPDATE")):
                    if pending_publication and kind == OperationKind.REVENUE_UPDATE:
                        continue
                    operations.submit(kind)

            def submit_due_gui_downloads() -> bool:
                # These workers only write vendor raw files and their own UI
                # checkpoints. Do not gate them on a Silver/Catalog repair.
                now = datetime.now(timezone.utc)
                reconcile_due_unknown_downloads(root, operations.list(), now)
                futures_due = due_futures_download(root, operations.list(), now)
                financial_due = due_financial_download(root, operations.list(), now)
                if futures_due:
                    operations.submit(OperationKind.TDX_FUTURES_AUTO_DOWNLOAD)
                if financial_due:
                    operations.submit(OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD)
                return futures_due or financial_due

            def submit_startup_updates() -> None:
                if publication_stop.is_set():
                    return
                gui_due = submit_due_gui_downloads()
                if pending_market_writes(root)["status"] != "CLEAR":
                    _app.state.silver_recovery_status = recover_abandoned_pending_write(root)
                if publication_stop.is_set():
                    return
                now = datetime.now(timezone.utc)
                submit_backup_if_gui_idle()
                local_due = due_local_market_updates(root, operations.list(), now)
                if (OperationKind.TDX_LOCAL_IMPORT in local_due and
                        not (gui_due or gui_download_active())):
                    operations.submit(OperationKind.TDX_LOCAL_IMPORT)
                submit_due_data()
                submit_due_futures()
                submit_due_f10()
                if OperationKind.MARKET_UPDATE in local_due:
                    operations.submit(OperationKind.MARKET_UPDATE)
                contract_state = root / "state" / "futures_contract_info_last_checked.json"
                if not contract_state.exists() or time.time() - contract_state.stat().st_mtime > 7 * 86400:
                    operations.submit(OperationKind.FUTURES_CONTRACT_INFO_UPDATE)
            if pending_market_writes(root)["status"] != "CLEAR":
                # Hashing an abandoned file may take seconds.  Keep READY
                # responsive while placing recovery ahead of source updates.
                startup_recovery = threading.Thread(
                    target=submit_startup_updates, name="marketlistener-silver-recovery", daemon=True,
                )
                startup_recovery.start()
            else:
                submit_startup_updates()

            def watch_gui_downloads() -> None:
                while not publication_stop.wait(60):
                    try:
                        submit_due_gui_downloads()
                    except (OSError, ValueError, RuntimeError):
                        continue

            gui_watch = threading.Thread(target=watch_gui_downloads,
                                         name="marketlistener-tdx-gui-auto-update", daemon=True)
            gui_watch.start()

            def watch_source_updates() -> None:
                while not publication_stop.wait(CHECK_INTERVAL_SECONDS):
                    try:
                        if pending_market_writes(root)["status"] == "CLEAR":
                            now = datetime.now(timezone.utc)
                            submit_backup_if_gui_idle()
                            local_due = due_local_market_updates(root, operations.list(), now)
                            if (OperationKind.TDX_LOCAL_IMPORT in local_due and
                                    not gui_download_active()):
                                operations.submit(OperationKind.TDX_LOCAL_IMPORT)
                            submit_due_futures()
                            submit_due_data()
                            submit_due_f10()
                            if OperationKind.MARKET_UPDATE in local_due:
                                operations.submit(OperationKind.MARKET_UPDATE)
                    except (OSError, ValueError, RuntimeError):
                        continue

            futures_watch = threading.Thread(target=watch_source_updates,
                                             name="marketlistener-source-auto-update", daemon=True)
            futures_watch.start()

            def watch_rank_continuations() -> None:
                while not publication_stop.wait(RANK_CONTINUATION_CHECK_INTERVAL_SECONDS):
                    try:
                        if pending_market_writes(root)["status"] != "CLEAR":
                            continue
                        calendar = load_futures_trading_calendar(root)
                        if due_rank_continuation(calendar, operations.list(), datetime.now(timezone.utc)):
                            operations.submit(OperationKind.FUTURES_MEMBER_RANK_UPDATE)
                    except (OSError, ValueError, RuntimeError):
                        continue

            rank_watch = threading.Thread(target=watch_rank_continuations,
                                          name="marketlistener-dce-rank-continuation", daemon=True)
            rank_watch.start()
        else:
            if startup_backup_due:
                operations.submit(OperationKind.BACKUP_CREATE)
        startup_stage.mark("READY")
        try:
            yield
        finally:
            publication_stop.set()
            query_store.set_rebuild_allowed(None)
            if publication_watch is not None:
                publication_watch.join(timeout=1)
            if startup_recovery is not None:
                startup_recovery.join(timeout=1)
            if futures_watch is not None:
                futures_watch.join(timeout=1)
            if gui_watch is not None:
                gui_watch.join(timeout=1)
            if rank_watch is not None:
                rank_watch.join(timeout=1)
            if kpl_watch is not None:
                kpl_watch.join(timeout=1)
            if ths_etf_watch is not None:
                ths_etf_watch.join(timeout=1)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with _service_lifespan(_app):
                yield
        except BaseException:
            if startup_stage.phase != "READY":
                startup_stage.mark("FAILED")
            raise

    app = FastAPI(
        title="MarketListener Local Research Terminal", docs_url=None, redoc_url=None,
        lifespan=lifespan,
    )
    app.state.data_root = root
    app.state.ths_etf_auto_enabled = auto_update_on_startup

    @app.exception_handler(KLineCacheUnavailable)
    async def query_index_not_ready(_request: Request, _error: KLineCacheUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503, headers={"Retry-After": "5"},
            content={"code": "KLINE_INDEX_REBUILDING", "detail": "K线索引正在核验，请稍后重试"},
        )

    app.include_router(market_api.router)
    app.include_router(sources_api.router)
    app.include_router(watchlist_api.router)
    app.include_router(strategy_api.router)
    app.include_router(signals_api.router)
    app.include_router(composites_api.router)
    app.include_router(stats_api.router)
    app.include_router(futures_api.router)
    app.include_router(data_sections_api.router)
    app.include_router(data_focus_api.router)
    app.include_router(dashboard_api.dashboard_router)
    app.include_router(dashboard_api.metrics_router)
    app.include_router(research_api.router)
    app.include_router(disclosure_watch_api.router)

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
    async def ready() -> dict[str, str]:
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

    @app.get("/api/data/update-status")
    def data_update_status() -> dict[str, object]:
        from .market_data_version import market_data_version

        kinds = (OperationKind.EQUITY_DASHBOARD_UPDATE, OperationKind.DISCLOSURES_UPDATE)
        revision_kinds = (*kinds, OperationKind.TDX_LOCAL_IMPORT, OperationKind.MARKET_UPDATE)
        history = operations.list()
        rows = []
        revision = [f"SILVER:{market_data_version(root)}"]
        for kind in revision_kinds:
            latest = next((item for item in history if item.kind == kind), None)
            completed = next((item for item in history if item.kind == kind and item.completed_at), None)
            if kind in kinds:
                rows.append({"kind": kind.value, "status": latest.status.value if latest else "NOT_RUN",
                             "completedAt": latest.completed_at if latest else None,
                             "phase": latest.phase if latest else None})
            if completed:
                revision.append(f"{kind.value}:{completed.operation_id}:{completed.status.value}")
        return {"autoEnabled": auto_update_on_startup, "jobs": rows,
                "completedRevision": "|".join(revision),
                "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}

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

    @app.get("/api/futures/update-status")
    def futures_update_status() -> dict[str, object]:
        """Report source scheduling and completed revisions without starting a fetch."""

        try:
            calendar = load_futures_trading_calendar(root)
        except (OSError, ValueError):
            calendar = None
        now = datetime.now(timezone.utc)
        watched = (*AUTO_KINDS, OperationKind.TDX_LOCAL_IMPORT,
                   OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE,
                   OperationKind.FUTURES_HEAT_UPDATE,
                   OperationKind.FUTURES_CONTRACT_INFO_UPDATE,
                   OperationKind.FUTURES_STRUCTURE_UPDATE)
        history = operations.list()
        jobs = []
        revision_parts = []
        for kind in watched:
            latest = next((item for item in history if item.kind == kind), None)
            completed = next((item for item in history if item.kind == kind and item.completed_at), None)
            expected = target_day(calendar, kind, now) if kind in AUTO_KINDS else None
            jobs.append({"kind": kind.value, "status": latest.status.value if latest else "NOT_RUN",
                         "phase": latest.phase if latest else None,
                         "expectedDay": expected[0] if expected else None,
                         "startedAt": latest.started_at if latest else None,
                         "completedAt": latest.completed_at if latest else None})
            if completed:
                revision_parts.append(f"{kind.value}:{completed.operation_id}:{completed.status.value}")
        coverage = _read_json_object(root / "state" / "futures_financial_coverage.json")
        return {"autoEnabled": auto_update_on_startup,
                "checkIntervalSeconds": CHECK_INTERVAL_SECONDS,
                "rankContinuationCheckIntervalSeconds": RANK_CONTINUATION_CHECK_INTERVAL_SECONDS,
                "latestObserved": {key: coverage.get(key) for key in (
                    "tdxLatestDay", "officialTurnoverLatestDay", "officialMarginLatestDay")},
                "localQuality": {key: coverage.get(key) for key in (
                    "queryIndexStatus", "contractCount", "tdxValidCount", "capitalCount")},
                "jobs": jobs, "completedRevision": "|".join(revision_parts),
                "checkedAt": now.isoformat(timespec="seconds")}

    @app.post("/api/futures/check-updates", status_code=202)
    def futures_check_updates() -> dict[str, object]:
        """Queue one bounded source check per kind; the serial manager deduplicates."""

        kinds = (OperationKind.TDX_LOCAL_IMPORT, *AUTO_KINDS)
        submitted = []
        for kind in kinds:
            operation, created = operations.submit(kind)
            submitted.append({"kind": kind.value, "operationId": operation.operation_id,
                              "queued": created})
        return {"items": submitted}

    @app.post("/api/data/check-updates", status_code=202)
    def data_check_updates() -> dict[str, object]:
        submitted = []
        for kind in (OperationKind.EQUITY_DASHBOARD_UPDATE, OperationKind.DISCLOSURES_UPDATE):
            operation, created = operations.submit(kind)
            submitted.append({"kind": kind.value, "operationId": operation.operation_id,
                              "queued": created})
        return {"items": submitted}

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
        if operation.status == OperationStatus.RUNNING and not operation.cancel_supported:
            raise HTTPException(status_code=409, detail="此任务尚不支持运行中取消")
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

    @app.get("/api/f10/update-status")
    def f10_update_status() -> dict[str, object]:
        from .f10 import read_a_share_universe_cache_status, read_f10_batch_summary, read_hk_universe_file_status

        kinds = (OperationKind.F10_UPDATE_CN, OperationKind.F10_UPDATE_HK, OperationKind.REVENUE_UPDATE)
        items = operations.list()
        jobs = []
        revisions = []
        for kind in kinds:
            rows = [item for item in items if item.kind == kind]
            latest = rows[0] if rows else None
            completed = next((item for item in rows if item.status not in {OperationStatus.RUNNING, OperationStatus.QUEUED}), None)
            batch = read_f10_batch_summary(root, kind.value)
            matches_latest = False
            if latest and latest.status not in {OperationStatus.RUNNING, OperationStatus.QUEUED} and batch:
                try:
                    times = [datetime.fromisoformat(value) for value in
                             (latest.started_at, batch["completedAt"], latest.completed_at)]
                    matches_latest = all(value.tzinfo is not None for value in times) and times[0] <= times[1] <= times[2]
                except (TypeError, ValueError, OverflowError):
                    pass
            jobs.append({"kind": kind.value, "status": latest.status.value if latest else "NOT_RUN",
                         "completedAt": latest.completed_at if latest else None,
                         "lastBatch": batch, "batchMatchesLatest": matches_latest})
            if completed:
                revisions.append(f"{kind.value}:{completed.operation_id}:{completed.status.value}")
        publication = next((item for item in items if item.kind == OperationKind.F10_REVENUE_REPUBLISH
                            and item.status not in {OperationStatus.RUNNING, OperationStatus.QUEUED}), None)
        if publication:
            revisions.append(f"{publication.kind.value}:{publication.operation_id}:{publication.status.value}")
        return {"autoEnabled": auto_update_on_startup, "jobs": jobs, "completedRevision": "|".join(revisions),
                "cnUniverseCache": read_a_share_universe_cache_status(root),
                "hkUniverseFile": read_hk_universe_file_status(root),
                "revenuePublicationPending": (root / "f10" / "cn" / "revenue_publication_pending.json").exists()}

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
        raise HTTPException(status_code=410, detail="旧版图谱已退出用户访问路径")

    @app.get("/api/internal/industry/atlas")
    def internal_industry_atlas() -> Response:
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


def _completed_tdx_slot(data_root: Path, terminal: str, now: datetime) -> bool:
    """Only unambiguous same-slot GUI completions can suppress a new launch."""
    from .tdx_afterhours import latest_slot
    from .tdx_download_ui import TERMINALS, _read_checkpoint_state

    state = _read_checkpoint_state(data_root / "state" / "tdx_download_ui" / "checkpoints.json")
    entries = state.get(terminal, {})
    if not isinstance(entries, dict):
        raise ValueError("invalid terminal checkpoints")
    for batch in TERMINALS[terminal][1]:
        entry = entries.get(batch.key)
        if entry is not None and not isinstance(entry, dict):
            raise ValueError("invalid batch checkpoint")
    slot = latest_slot(now)
    return all(isinstance(entries.get(batch.key), dict)
               and entries[batch.key].get("slot") == slot
               and entries[batch.key].get("status") == "UI_DONE_UNVERIFIED"
               for batch in TERMINALS[terminal][1])


def _operation_handlers(data_root: Path) -> dict[OperationKind, object]:
    """Bind the finite operation enum directly to existing Python services."""

    from .f10 import recover_revenue_publication, run_f10_fetch, run_revenue_fetch
    from .industry_atlas import build_atlas
    from .package_builder import build_android_package
    from .report_pipeline import build_chain_index, process_report_batch, verify_report_batch
    from .futures_calendar import load_futures_trading_calendar, sync_futures_trading_calendar
    from .futures_official_backfill import default_backfill_range
    from .futures_structure import (
        run_member_open_interest_structure_pipeline,
        run_product_notional_structure_pipeline,
        run_product_open_interest_structure_pipeline,
    )

    desktop_root = Path(__file__).resolve().parents[2]
    repository_root = desktop_root.parent
    report_root = repository_root / "行业产业链研报"
    output_root = repository_root / "reports" / "industry"
    private_key = desktop_root / "keys" / "market_package_private_key.pem"
    ecdsa_private_key = desktop_root / "keys" / "market_package_ecdsa_private.pem"

    def invoke(callable_: object) -> str:
        result = callable_()  # type: ignore[operator]
        return json.dumps(result, ensure_ascii=False, sort_keys=True) if isinstance(result, dict) else str(result or "PASS")

    def create_profile_backup_task() -> str:
        from .local_backup import create_profile_backup, rotate_profile_backups, verify_profile_backup

        path = create_profile_backup(data_root)
        report = verify_profile_backup(path)
        removed = rotate_profile_backups(data_root)
        return (f"PASS: 个人资料、配置和发布清单加密备份已校验；"
                f"文件 {report['fileCount']} 项，轮换清理 {removed} 份。"
                "大体积行情和签名私钥另行备份。")

    def publish_market_readmodel_task() -> str:
        market_api.prime_market_list_snapshots(data_root)
        return "PASS: 行情完整名单已按实际来源版本发布"

    def update_market_data() -> str:
        from .collector import build_collection_tasks, run_fetch_session

        tasks = [
            task for task in build_collection_tasks(limit_futures=15, limit_cn_stocks=5)
            if task.dataset_id not in {"FUTURES_OI_LEADERBOARD", "A_SHARE_BREADTH", "CN_ZT_POOL",
                                        "CN_ZT_OPEN_RETURN",
                                        "HKEX_MARKET_CAP", "THS_CPI", "BLS_PAYROLL_CHANGE"}
        ]
        summary = run_fetch_session(data_root, tasks=tasks, isolate_akshare=True)
        status = "PASS" if summary.get("status") == "PASS" else "PARTIAL_FAILURE"
        # The startup publication watcher queues a separate serial job once
        # the source queue and K-line index are ready. A slow index rebuild must
        # not turn already committed provider rows into a failed fetch session.
        return (
            f"{status}: 行情采集完成；来源通过 {summary.get('passed', 0)}/"
            f"{summary.get('task_count', 0)}，失败 {summary.get('failed', 0)}，"
            f"阻断 {summary.get('blocked', 0)}，新增 {summary.get('total_rows', 0)} 行；"
            "读取快照由独立发布任务更新，逐来源详情见采集会话。"
        )

    def update_equity_dashboard() -> str:
        from zoneinfo import ZoneInfo
        from .a_share_silver_recovery import recover_recent_a_share_breadth
        from .a_share_pool_recovery import (recover_recent_a_share_open_returns,
                                            recover_recent_a_share_pools)
        from .official_exchange_daily import (sync_official_exchange_due,
                                              sync_official_monthly_due)
        from .a_share_calendar import is_verified_a_share_trading_day
        from .collector import build_collection_tasks, run_fetch_session
        from .ths_indexflash import probe_indexflash

        # HKEX's website terms require written permission for scripted access.
        # Keep already saved observations visible, without a scheduled fetch.
        wanted = {"THS_CPI", "BLS_PAYROLL_CHANGE"}
        beijing_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        mainland_trading_day = is_verified_a_share_trading_day(beijing_now.date())
        indexflash_status = "NOT_DUE"
        if mainland_trading_day and (beijing_now.hour, beijing_now.minute) >= (15, 30):
            try:
                indexflash_status = str(probe_indexflash(data_root)["status"])
            except (OSError, ValueError, TypeError, KeyError):
                indexflash_status = "FAILED"
        if mainland_trading_day:
            wanted.update(("A_SHARE_BREADTH", "CN_ZT_POOL", "CN_ZT_OPEN_RETURN"))
        tasks = [task for task in build_collection_tasks(limit_futures=15, limit_cn_stocks=5)
                 if task.dataset_id in wanted]
        summary = run_fetch_session(data_root, tasks=tasks, max_workers=4,
                                    task_timeout_seconds=180, isolate_akshare=True)
        recovered = recover_recent_a_share_breadth(data_root)
        pools = recover_recent_a_share_pools(data_root, now=beijing_now)
        open_returns = recover_recent_a_share_open_returns(data_root, now=beijing_now)
        cap_end = (beijing_now.date() if mainland_trading_day and
                   (beijing_now.hour, beijing_now.minute) >= (15, 30)
                   else beijing_now.date() - timedelta(days=1))
        try:
            official = sync_official_exchange_due(
                data_root, start_day=(beijing_now.date() - timedelta(days=90)).isoformat(),
                end_day=cap_end.isoformat(),
            )
        except Exception as error:  # noqa: BLE001 - report source failure without dropping other updates
            official = {"writtenDays": [], "failedDays": [{"error": f"SOURCE:{type(error).__name__}"}]}
        last_month = (beijing_now.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        try:
            monthly = sync_official_monthly_due(
                data_root, start_month=(beijing_now.date() - timedelta(days=90)).strftime("%Y-%m"),
                end_month=last_month,
            )
        except Exception as error:  # noqa: BLE001 - independent monthly source
            monthly = {"writtenMonths": [], "failedMonths": [{"error": f"SOURCE:{type(error).__name__}"}]}
        source_states = {item["dataset_id"]: item["status"] for item in summary.get("tasks", [])}
        source_states["OFFICIAL_EXCHANGE_DAILY"] = (
            "PARTIAL_FAILURE" if official.get("failedDays") else "PASS"
        )
        source_states["OFFICIAL_EXCHANGE_MONTHLY"] = (
            "PARTIAL_FAILURE" if monthly.get("failedMonths") else "PASS"
        )
        source_states["HKEX_MARKET_CAP"] = "BLOCKED_SOURCE_PERMISSION"
        source_states["CN_ZT_POOL_RECOVERY"] = (
            "PARTIAL_FAILURE" if pools["failedDays"] else "PASS"
        )
        source_states["CN_ZT_OPEN_RETURN_RECOVERY"] = (
            "PARTIAL_FAILURE" if open_returns["failedDays"] else "PASS"
        )
        source_states["THS_INDEXFLASH"] = indexflash_status
        status = ("PASS" if summary.get("status") == "PASS" and not official.get("failedDays")
                  and not monthly.get("failedMonths") and not pools["failedDays"]
                  and not open_returns["failedDays"] and indexflash_status == "NOT_DUE"
                  else "PARTIAL_FAILURE")
        return (f"{status}: 数据页市场来源更新；"
                + "、".join(f"{key}={source_states.get(key, 'UNKNOWN')}"
                           for key in sorted((*wanted, "HKEX_MARKET_CAP", "OFFICIAL_EXCHANGE_DAILY", "OFFICIAL_EXCHANGE_MONTHLY", "THS_INDEXFLASH",
                                              "CN_ZT_POOL_RECOVERY", "CN_ZT_OPEN_RETURN_RECOVERY")))
                + ("；A股非已核验交易日，本轮不抓当日快照，仍检查历史官网缺口。"
                   if not mainland_trading_day else "；")
                + f"本机通达信日线补算 {len(recovered['recoveredDays'])} 个缺口日、{recovered['metricCount']} 项；"
                + f"涨跌停池复核 {len(pools['recoveredDays'])} 日、待补 {len(pools['failedDays'])} 日；"
                + f"接盘收益复核 {len(open_returns['recoveredDays'])} 日、"
                  f"待补 {len(open_returns['failedDays'])} 日；"
                + f"三所官网日统计新增 {len(official.get('writtenDays', []))} 日、"
                  f"待补 {len(official.get('failedDays', []))} 日；"
                + f"北交所月度发行新增 {len(monthly.get('writtenMonths', []))} 月、"
                  f"待补 {len(monthly.get('failedMonths', []))} 月；"
                + "同花顺CPI按月检查；港交所网页自动采集停用，已保存市值档案显示原观测日期。")

    def update_disclosure_watch() -> str:
        from .cninfo_disclosure import sync_due_cninfo_keyword_hits

        report = sync_due_cninfo_keyword_hits(data_root)
        status = "PASS" if report["status"] == "PASS" and not report["backlogRemaining"] else "PARTIAL_FAILURE"
        return (f"{status}: 巨潮资讯公告关键词检索 {report['startDay']}—{report['endDay']}；"
                f"新增待核线索 {report['newHits']} 条，本地累计 {report['storedHits']} 条；"
                f"已完整覆盖至 {report['completeThroughDay'] or '暂无'}，"
                f"历史待补={'是' if report['backlogRemaining'] else '否'}。"
                "关键词命中不等于当前风险状态。")

    def update_a_share_delisted() -> str:
        from .exchange_delisted import refresh_due

        report = refresh_due(data_root, datetime.now(timezone.utc))
        sources = "、".join(f"{item['exchange']}={item['status']}" for item in report["items"])
        return f"{report['status']}: 沪深交易所摘牌名单检查；{sources}；北交所仅有个别公告。"

    def update_f10(market: str) -> str:
        result = run_f10_fetch(data_root, market=market, limit_details=25, skip_quotes=True)
        status = "PASS" if result.get("status") == "PASS" else "PARTIAL_FAILURE"
        return (f"{status}: {market} F10 资料采集；本轮更新 {result.get('new_details', 0)} 家，"
                f"剩余待处理 {result.get('remaining_details', 0)} 家，失败 {result.get('failed_codes', 0)} 项；"
                f"状态 {result.get('status', 'UNKNOWN')}。")

    def update_revenue() -> str:
        result = run_revenue_fetch(data_root, market="CN", limit=25)
        status = "PASS" if result.get("status") == "PASS" else "PARTIAL_FAILURE"
        return (f"{status}: A股收入构成采集；本轮更新 {result.get('new_revenue', 0)} 家，"
                f"剩余待处理 {result.get('remaining_details', 0)} 家，失败 {result.get('failed_codes', 0)} 项；"
                f"状态 {result.get('status', 'UNKNOWN')}。")

    def republish_revenue() -> str:
        result = recover_revenue_publication(data_root)
        if result.get("status") == "SKIPPED":
            return "PARTIAL_FAILURE: 收入构成发布恢复等待其他采集释放锁。"
        return "PASS: 已检查收入构成本机快照发布状态。"

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
        """Legacy combined request uses each terminal's matching-bit GUI worker."""

        financial = download_financial_afterhours()
        futures = download_futures_afterhours()
        status = "PASS" if financial.startswith("PASS:") and futures.startswith("PASS:") else "PARTIAL_FAILURE"
        return f"{status}: 金融终端：{financial}；期货通：{futures}"

    def _auto_terminal_identity(driver, result: str, terminal: str,
                                enabled: bool) -> tuple[int | None, str | None]:
        if not enabled:
            return None, None
        from .tdx_process_ownership import load_auto_owned, remember_auto_owned

        if result == "LAUNCHED":
            pid = getattr(driver, "launched_pid", None)
            if isinstance(pid, int) and pid > 0:
                try:
                    return remember_auto_owned(data_root, terminal, pid, driver.exe)
                except (OSError, RuntimeError, ValueError):
                    pass  # Download can continue without closing an unverified process.
            return None, None
        return load_auto_owned(data_root, terminal, driver.exe) or (None, None)

    def _launch_financial_owned(*, auto_download: bool = False) -> tuple[str, int | None, str | None]:
        from .tdx_download_ui import DialogDriver, StopRun

        try:
            driver = DialogDriver(Path(r"C:\tongdaxin\tdxw.exe"))
            result = driver.start()
        except StopRun as error:
            return f"PARTIAL_FAILURE: 金融终端未能启动；{error}", None, None
        except Exception as error:
            return f"PARTIAL_FAILURE: 金融终端启动适配器错误；{type(error).__name__}", None, None
        owned_pid, owned_token = _auto_terminal_identity(driver, result, "financial", auto_download)
        return (f"PASS: 金融终端{result}；后续下载任务会核验已保存模拟账户的登录窗口。",
                owned_pid, owned_token)

    def launch_financial_terminal() -> str:
        return _launch_financial_owned()[0]

    def download_financial_afterhours() -> str:
        from .tdx_gui_auto_refresh import run_financial_download_worker
        from .tdx_process_ownership import load_auto_owned

        try:
            done = _completed_tdx_slot(data_root, "financial", datetime.now(timezone.utc))
        except (OSError, ValueError, TypeError):
            return "PARTIAL_FAILURE: 金融终端下载检查点无效，未启动软件。"
        if done:
            owned = load_auto_owned(data_root, "financial", Path(r"C:\tongdaxin\tdxw.exe"))
            if owned is None:
                return "PASS: 金融终端本次批次已完成，无可核验的自动启动进程；原文件质量仍待核验。"
            launch, (owned_pid, owned_token) = "PASS: 已完成，检查自动进程退出。", owned
        else:
            launch, owned_pid, owned_token = _launch_financial_owned(auto_download=True)
        if not launch.startswith("PASS:"):
            return launch
        state_dir = data_root / "state" / "tdx_download_ui"
        exit_code, worker_error = run_financial_download_worker(
            data_root, owned_pid=owned_pid, owned_token=owned_token)
        if owned_pid is not None and owned_token is not None:
            from .tdx_process_ownership import clear_exited_auto_owned

            clear_exited_auto_owned(data_root, "financial", owned_pid, owned_token)
        if worker_error:
            return f"PARTIAL_FAILURE: 金融终端盘后下载未完成；原因={worker_error}。"
        latest = _read_json_object(state_dir / "latest.json")
        terminals = latest.get("terminals") if isinstance(latest.get("terminals"), dict) else {}
        outcome = terminals.get("financial", "UNKNOWN")
        if exit_code == 0 and outcome in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}:
            return ("PASS: 金融终端已完成所需盘后下载或按原文件分组端点跳过当前批次；"
                    "逐标的原文件与入库质量继续独立核验。")
        return f"PARTIAL_FAILURE: 金融终端盘后下载未完成；原因={outcome}；未将界面状态当作数据覆盖。"

    def _launch_futures_owned(*, auto_download: bool = False) -> tuple[str, int | None, str | None]:
        from .tdx_download_ui import DialogDriver, StopRun

        try:
            driver = DialogDriver(Path(r"C:\new_tdxqh\tdxw.exe"))
            result = driver.start()
        except StopRun as error:
            return f"PARTIAL_FAILURE: 期货通未能启动；{error}", None, None
        except Exception as error:
            return f"PARTIAL_FAILURE: 期货通启动适配器错误；{type(error).__name__}", None, None
        owned_pid, owned_token = _auto_terminal_identity(driver, result, "futures", auto_download)
        return f"PASS: 期货通{result}；下载窗口仍逐项核验。", owned_pid, owned_token

    def launch_futures_terminal() -> str:
        return _launch_futures_owned()[0]

    def download_futures_afterhours() -> str:
        from .tdx_gui_auto_refresh import run_futures_download_worker
        from .tdx_process_ownership import load_auto_owned

        try:
            done = _completed_tdx_slot(data_root, "futures", datetime.now(timezone.utc))
        except (OSError, ValueError, TypeError):
            return "PARTIAL_FAILURE: 期货通下载检查点无效，未启动软件。"
        if done:
            owned = load_auto_owned(data_root, "futures", Path(r"C:\new_tdxqh\tdxw.exe"))
            if owned is None:
                return "PASS: 期货通本次批次已完成，无可核验的自动启动进程；原文件质量仍待核验。"
            launch, (owned_pid, owned_token) = "PASS: 已完成，检查自动进程退出。", owned
        else:
            launch, owned_pid, owned_token = _launch_futures_owned(auto_download=True)
        if not launch.startswith("PASS:"):
            return launch
        state_dir = data_root / "state" / "tdx_download_ui"
        exit_code, worker_error = run_futures_download_worker(
            data_root, owned_pid=owned_pid, owned_token=owned_token)
        if owned_pid is not None and owned_token is not None:
            from .tdx_process_ownership import clear_exited_auto_owned

            clear_exited_auto_owned(data_root, "futures", owned_pid, owned_token)
        if worker_error:
            return f"PARTIAL_FAILURE: 期货通盘后下载未完成；原因={worker_error}。"
        latest = _read_json_object(state_dir / "latest.json")
        terminals = latest.get("terminals") if isinstance(latest.get("terminals"), dict) else {}
        outcome = terminals.get("futures", "UNKNOWN")
        if exit_code == 0 and outcome in {"UI_DONE_UNVERIFIED", "COMPLETE_WITH_SKIPS", "NO_GUI_RAW_RECENT"}:
            return ("PASS: 期货通已完成所需盘后下载或按原文件分组端点跳过当前批次；"
                    "逐合约原文件与入库质量继续独立核验。")
        return f"PARTIAL_FAILURE: 期货通盘后下载未完成；原因={outcome}；未将界面状态当作数据覆盖。"

    def update_futures_heat() -> str:
        from .futures_contract_heat_update import run_futures_contract_heat_update

        summary = run_futures_contract_heat_update(data_root)
        latest = summary.get("calculation", {})
        weekly = latest.get("weekly", {}) if isinstance(latest, dict) else {}
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
            f"；周热度截至 {weekly.get('latestWeekEnd') or '暂无'}、质量 {weekly.get('latestQuality') or '未知'}，"
            f"历史待补日 {weekly.get('pendingDays', 0)}、部分质量日 {weekly.get('partialDays', 0)}{missing_text}。"
        )

    def import_tdx_local_data() -> str:
        from .tdx_import_workflow import run_local_terminal_imports

        summary = run_local_terminal_imports(data_root)
        details = []
        blocked = summary.get("blockedTerminals", {})
        for key, label in (("futures", "期货通"), ("financial", "金融终端")):
            if key in blocked:
                details.append(f"{label}=暂缓（{key}:{blocked[key]}），未读取该终端原始文件")
                continue
            item = summary.get("terminals", {}).get(key, {})
            details.append(f"{label}={item.get('status', '未知')}，新增 {item.get('rows', 0)} 根")
        from .market_tdx_futures_projection import refresh_financial_coverage

        if summary.get("selectedTerminals", summary.get("terminals")):
            refresh_financial_coverage(data_root)
        return f"{summary.get('status', 'PARTIAL_FAILURE')}: 本地文件增量导入；{'；'.join(details)}。质量隔离及断点见管理台；该状态不证明交易日全覆盖。"

    def latest_completed_futures_day() -> str:
        if load_futures_trading_calendar(data_root) is None:
            sync_futures_trading_calendar(data_root)
        _start_day, end_day = default_backfill_range(data_root)
        return end_day

    def update_official_futures_daily() -> str:
        from .futures_contract_heat_update import run_futures_contract_heat_update

        report = run_futures_contract_heat_update(data_root, recent_trading_days=10)
        from .market_tdx_futures_projection import refresh_financial_coverage

        refresh_financial_coverage(data_root)
        official = report.get("officialDaily", {})
        venues = official.get("exchanges", [])
        incomplete = [str(item.get("exchange")) for item in venues if item.get("status") != "PASS"]
        structures = report.get("structures", [])
        weekly = (report.get("calculation") or {}).get("weekly") or {}
        missing = f"；历史缺口来源：{'、'.join(incomplete)}" if incomplete else ""
        return (
            f"{report['refreshStatus']}: 官方期货日行情 {official.get('startDay', '2023-01-01')}"
            f" 至 {official.get('endDay', '未知')}；热度与品种结构已串行更新，"
            f"结构 {sum(item.get('status') == 'PASS' for item in structures)}/{len(structures)} 项通过；"
            f"周热度质量 {weekly.get('latestQuality') or '未知'}，历史待补日 {weekly.get('pendingDays', 0)}、"
            f"部分质量日 {weekly.get('partialDays', 0)}"
            f"{missing}。"
        )

    def update_futures_contract_info() -> str:
        from .futures_contract_info import collect_official_contract_info

        report = collect_official_contract_info(data_root)
        passed = report["quality"].get("PASS", 0)
        total = report["products"]
        status = "PASS" if passed == total else "PARTIAL_FAILURE"
        return f"{status}: 官网期货合约信息完整 {passed}/{total} 个品种；部分来源见任务报告。"

    def update_futures_settlement_parameters() -> str:
        from .dce_official_api import update_official_dce_api
        from .futures_settlement_parameters import sync_settlement_parameters
        from .futures_settlement_parameters import SettlementSourceError
        from .futures_dce_settlement_browser import DceSettlementBrowser
        from .market_official_futures_metrics import refresh_financial_metrics_version
        try:
            end_day = latest_completed_futures_day()
            calendar = load_futures_trading_calendar(data_root)
            recent_days = list(calendar.between("2023-01-01", end_day))[-10:] if calendar else []
            api = update_official_dce_api(
                data_root, recent_days,
            )
        except Exception as error:
            api = {"status": "FAILED", "error": type(error).__name__}
        browser = None
        if api.get("status") != "PASS" or api.get("pendingSettlementDays", 1):
            try:
                browser = DceSettlementBrowser()
            except SettlementSourceError:
                pass
        try:
            report = sync_settlement_parameters(
                data_root, bronze_only=False, dce_fetcher=browser.capture if browser else None,
                max_days=10,
            )
        finally:
            if browser is not None:
                browser.close()
        from .futures_settlement_backup import sync_current_backup
        from .storage import MarketStore

        backup = {"status": "UNAVAILABLE"}
        store = MarketStore(data_root)
        try:
            try:
                backup = sync_current_backup(data_root, store.connection, latest_completed_futures_day())
            except (ValueError, OSError) as error:
                backup = {"status": "UNAVAILABLE", "reason": type(error).__name__}
            except Exception as error:
                backup = {"status": "UNAVAILABLE", "reason": type(error).__name__}
        finally:
            store.close()
        from .market_tdx_futures_projection import refresh_financial_coverage

        refresh_financial_coverage(data_root)
        from .market_data_version import market_data_version
        from .market_tdx_futures_projection import SOURCE_POLICY_VERSION

        refresh_financial_metrics_version(data_root, versions={
            "marketRevision": market_data_version(data_root),
            "sourcePolicyVersion": SOURCE_POLICY_VERSION,
        })
        # The official margin evidence changes this financial digest only.
        # Silver K-line revisions and the large query index remain intact.
        status = "PASS" if report.get("status") == "PASS" else "PARTIAL_FAILURE"
        return (f"{status}: 官方期货结算参数已核对；逐所日期覆盖和缺口见任务报告。"
                f"大商所授权API：{api.get('status')}；"
                f"最新交易日公司披露的交易所投机标准备用观测：{backup.get('status')}，"
                f"{backup.get('record_count', 0)}个合约；备用不计为官方历史覆盖。")

    def update_futures_market_financials(context: OperationContext | None = None) -> str:
        """One manual/startup path with source updates in dependency order."""
        steps = []
        for index, (label, action) in enumerate((
            ("通达信导入", import_tdx_local_data),
            ("官网日行情", update_official_futures_daily),
            ("官网结算参数", update_futures_settlement_parameters),
        ), start=1):
            if context:
                context.check_cancelled()
                context.progress(label, index - 1, 4, checkpoint=f"step-{index}")
            try:
                detail = action()
                steps.append((label, detail))
            except Exception as error:
                steps.append((label, f"FAILED: {type(error).__name__}"))
            if context:
                context.progress(label, index, 4, checkpoint=f"step-{index}-done")
        from .market_tdx_futures_projection import refresh_financial_coverage

        if context:
            context.check_cancelled()
            context.progress("合约日核验与发布", 3, 4, checkpoint="publish")
        try:
            coverage = refresh_financial_coverage(data_root)
            if context:
                context.progress("合约日核验与发布", 4, 4,
                                 checkpoint="await-readmodel-publication")
        except Exception as error:
            steps.append(("合约日核验", f"FAILED: {type(error).__name__}"))
            coverage = {}
        passed = all(detail.startswith(("PASS:", "PASS ")) for _, detail in steps)
        status = ("PASS" if passed and not coverage.get("mismatchCount")
                  and coverage.get("queryIndexStatus") != "STALE" else "PARTIAL_FAILURE")
        summary = "；".join(f"{label}={detail.split(':', 1)[0]}" for label, detail in steps)
        return (f"{status}: 期货行情指标更新：{summary}；"
                "行情名单由独立串行任务按最新来源版本发布；"
                f"通达信截至 {coverage.get('tdxLatestDay') or '暂无'}，"
                f"官网金额截至 {coverage.get('officialTurnoverLatestDay') or '暂无'}，"
                f"官网保证金截至 {coverage.get('officialMarginLatestDay') or '暂无'}，"
                f"待核验合约 {coverage.get('mismatchCount', '未知')}。")

    def update_futures_member_ranks() -> str:
        from .futures_member_rank_update import run_latest_member_rank_update
        from .futures_auto_refresh import member_rank_continuation_marker

        report = run_latest_member_rank_update(data_root, recent_trading_days=10)
        day = str(report.get("tradingDay") or "未知交易日")
        if report.get("status") == "PASS":
            skipped = report.get("skippedSources")
            if isinstance(skipped, list) and skipped:
                return f"PASS: 日成交持仓排名已覆盖至 {day}；无缺失来源，本次跳过重复下载。"
            imported = [
                f"{source}={item.get('importStatus', '未知')}"
                for source, item in (report.get("sources") or {}).items()
            ]
            return (
                f"PASS: 日成交持仓排名已增量更新至 {day}；"
                f"来源状态：{'，'.join(imported) if imported else '覆盖核对通过'}。"
            )
        missing = report.get("missingVenues")
        missing_text = f"；尚缺交易所：{'、'.join(map(str, missing))}" if missing else ""
        details = []
        for source, item in (report.get("sources") or {}).items():
            status = item.get("captureStatus", item.get("status", "FAILED"))
            failure = item.get("detail") or item.get("importError")
            details.append(f"{source}={status}{'：' + str(failure) if failure else ''}")
        detail_text = f"；来源详情：{'；'.join(details)}" if details else ""
        dce_source = (report.get("sources") or {}).get("DCE", {})
        continuation = member_rank_continuation_marker(report)
        dce_note = ""
        if isinstance(missing, list) and "DCE" in missing:
            if dce_source.get("captureStatus") == "AUTHORIZED_API_READY":
                dce_note = ("大商所授权API本批已保存有效合约，15分钟后检查断点续跑。"
                            if continuation else "大商所授权API尚未完整核验，按来源状态退避重试。")
            else:
                dce_note = "大商所浏览器来源需保持已配置的官网排名页可用后重试。"
        return (
            f"PARTIAL_FAILURE: 日成交持仓排名未能完整更新至 {day}{missing_text}"
            f"{detail_text}。{dce_note}"
            + (f"；{continuation}" if continuation else "")
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
        OperationKind.MARKET_UPDATE: update_market_data,
        OperationKind.EQUITY_DASHBOARD_UPDATE: update_equity_dashboard,
        OperationKind.A_SHARE_DELISTED_UPDATE: update_a_share_delisted,
        OperationKind.DISCLOSURES_UPDATE: update_disclosure_watch,
        OperationKind.F10_UPDATE_CN: lambda: update_f10("CN"),
        OperationKind.F10_UPDATE_HK: lambda: update_f10("HK"),
        OperationKind.REVENUE_UPDATE: update_revenue,
        OperationKind.F10_REVENUE_REPUBLISH: republish_revenue,
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
        OperationKind.TDX_FINANCIAL_LAUNCH: launch_financial_terminal,
        OperationKind.TDX_FINANCIAL_AUTO_DOWNLOAD: download_financial_afterhours,
        OperationKind.TDX_FUTURES_LAUNCH: launch_futures_terminal,
        OperationKind.TDX_FUTURES_AUTO_DOWNLOAD: download_futures_afterhours,
        OperationKind.BACKUP_CREATE: create_profile_backup_task,
        OperationKind.MARKET_READMODEL_PUBLISH: publish_market_readmodel_task,
        OperationKind.TDX_LOCAL_IMPORT: import_tdx_local_data,
        OperationKind.FUTURES_HEAT_UPDATE: update_futures_heat,
        OperationKind.FUTURES_MEMBER_RANK_UPDATE: update_futures_member_ranks,
        OperationKind.FUTURES_OFFICIAL_DAILY_UPDATE: update_official_futures_daily,
        OperationKind.FUTURES_CONTRACT_INFO_UPDATE: update_futures_contract_info,
        OperationKind.FUTURES_SETTLEMENT_PARAMS_UPDATE: update_futures_settlement_parameters,
        OperationKind.FUTURES_MARKET_FINANCIAL_UPDATE: ContextOperationHandler(update_futures_market_financials),
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
    """Add published chain locations to F10 without copying company data into nodes."""

    key = detail.summary.instrument_key
    from .research_publication import read_publication

    company_index = read_publication(root, "companies")
    locations = [{"chain": item["chain"], "stage": "当前关系", "node": item["node"]}
                 for item in company_index.get("items", {}).get(key, [])]
    return replace(detail, chain_locations=tuple(locations))


def create_customer_web_app(
    data_root: Path, *, admin_token: str | None = None,
    session_port: int = 8765, local_control_token: str | None = None,
    **options: Any,
) -> FastAPI:
    """Production customer app; legacy create_web_app remains the test adapter."""

    from .admin_boundary import install_admin_boundary

    app = create_web_app(data_root, **options)
    install_admin_boundary(app, admin_token, port=session_port,
                           control_token=local_control_token)

    @app.middleware("http")
    async def market_timing(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        if (path == "/api/market/instruments" or
                (path.startswith("/api/market/instruments/") and
                 (path.endswith("/bars") or path.endswith("/bars/history")))):
            stages = getattr(request.state, "market_timings", "")
            total = f"app;dur={(time.perf_counter() - started) * 1000:.2f}"
            response.headers["Server-Timing"] = f"{stages}, {total}" if stages else total
        return response

    @app.get("/api/internal/runtime")
    def internal_runtime() -> dict:
        from .process_runtime import process_runtime

        return process_runtime()

    @app.get("/api/internal/tdx-import-inventory")
    def tdx_import_inventory() -> dict:
        from .market_query_cache import get_kline_query_store
        from .tdx_import_workflow import latest_import_runs

        return {**get_kline_query_store(Path(data_root)).tdx_import_inventory(),
                "lastRuns": latest_import_runs(Path(data_root))}

    @app.get("/api/internal/manual-data-gaps")
    def internal_manual_data_gaps(
        source: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int = Query(default=40, ge=1, le=200),
    ) -> dict[str, object]:
        from .manual_data_gaps import (collector_activity_items, delisted_snapshot_items, event_driven_items, exchange_stat_items,
                                       legacy_pytdx_intraday_item, macro_and_hk_items, official_gap_items)

        if start and end and start > end:
            raise HTTPException(status_code=422, detail="start must not be after end")
        root = Path(data_root)
        official = (official_gap_items(root, start=start.isoformat() if start else None,
                                       end=end.isoformat() if end else None, limit=limit, source=source)
                    if source is None or source.startswith("official.") else [])
        events = ([item for item in event_driven_items(root, limit=limit)
                   if source is None or item["key"] == source]
                  if source is None or not source.startswith("official.") else [])
        exchanges = ([item for item in exchange_stat_items(root, start=start.isoformat() if start else None,
                                                           end=end.isoformat() if end else None)
                      if source is None or item["key"] == source]
                     if source is None or source.startswith("exchange.") else [])
        delisted = ([item for item in delisted_snapshot_items(root, start=start.isoformat() if start else None,
                                                             end=end.isoformat() if end else None, limit=limit)
                     if source is None or item["key"] == source]
                    if source is None or source.startswith("exchange.") else [])
        macro = ([item for item in macro_and_hk_items(root) if source is None or item["key"] == source]
                 if source is None or source.startswith(("macro.", "hkex.")) else [])
        activity = ([item for item in collector_activity_items(root) if source is None or item["key"] == source]
                    if source is None or source.startswith("collector.") else [])
        quality = ([legacy_pytdx_intraday_item(root)]
                   if source is None or source == "quality.pytdx-30m" else [])
        return {"items": [*official, *exchanges, *delisted, *macro, *activity, *quality, *events]}

    @app.get("/api/internal/market-publication")
    def internal_market_publication(request: Request) -> dict:
        from .market_query_cache import get_kline_query_store
        from .market_data_version import pending_market_writes
        from .market_readmodel import load_market_readmodel, publication_manifest

        root = Path(data_root)
        current = market_api._response_data_version(root)
        pointer = publication_manifest(root) or {}
        models = {}
        for identity in ("category:cn-future-main", "category:cn-future-dce", "all"):
            model = load_market_readmodel(root, current_version=current,
                                          identity=identity, allow_stale=True)
            models[identity] = ({**model.metadata(), "rowCount": len(model.items)}
                                if model is not None else {"snapshotStatus": "MISSING_OR_INVALID"})
        return {"currentSourceVersion": current,
                "publicationVersion": pointer.get("publicationVersion"),
                "queryIndex": get_kline_query_store(root).status().to_dict(),
                "silverWrites": pending_market_writes(root),
                "silverRecovery": getattr(request.app.state, "silver_recovery_status", {"status": "NOT_RUN"}),
                "models": models}

    @app.get("/api/internal/accounts/trash/purge-preview")
    def internal_account_purge_preview() -> dict:
        from .account_analysis import preview_deleted_accounts

        return preview_deleted_accounts(Path(data_root))

    @app.post("/api/internal/accounts/trash/purge")
    def internal_account_purge(body: _AccountPurgeRequest) -> dict:
        from .account_analysis import AccountError, purge_deleted

        audit = EventLog(Path(data_root))
        audit.append({"category": "ACCOUNT_MAINTENANCE", "action": "PURGE_REQUESTED",
                      "confirmationDigest": body.confirmationDigest})
        try:
            count = purge_deleted(Path(data_root), expected_digest=body.confirmationDigest)
        except AccountError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        audit.append({"category": "ACCOUNT_MAINTENANCE", "action": "PURGE_COMPLETED",
                      "count": count, "confirmationDigest": body.confirmationDigest})
        return {"status": "COMPLETED", "purgedAccounts": count}

    @app.get("/api/internal/backups/profile")
    def internal_profile_backups() -> dict:
        from .local_backup import profile_backups

        items = profile_backups(Path(data_root))
        return {"items": items, "total": len(items)}

    @app.post("/api/internal/backups/profile/verify")
    def internal_verify_profile_backup(body: _BackupVerifyRequest) -> dict:
        from .local_backup import BackupError, verify_profile_backup

        path = Path(data_root) / "backups" / "profile" / body.name
        try:
            return verify_profile_backup(path)
        except BackupError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


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
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("本机服务只能监听回环地址")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(socket.SOMAXCONN)
    actual_port = int(listener.getsockname()[1])
    from .local_session import load_control_token

    try:
        config = uvicorn.Config(
            create_customer_web_app(
                data_root, auto_update_on_startup=auto_update_on_startup,
                auto_backup_on_startup=True,
                auto_publish_on_startup=True,
                session_port=actual_port,
                local_control_token=load_control_token("product", actual_port),
            ),
            host=host,
            port=actual_port,
            log_level="warning" if quiet else "info",
            access_log=not quiet,
            proxy_headers=False,
        )
    except Exception:
        listener.close()
        raise
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


__all__ = ("create_customer_web_app", "serve_web_app")
