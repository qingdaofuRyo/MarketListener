"""Server-side boundary between product APIs and internal platform operations."""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .local_session import LocalSessionManager, install_local_sessions


ADMIN_TOKEN_ENV = "MARKETLISTENER_ADMIN_TOKEN"
ADMIN_TOKEN_HEADER = "X-MarketListener-Admin-Token"
INTERNAL_PREFIXES = (
    "/api/operations", "/api/logs", "/api/sources", "/api/data-sources", "/api/tdx-afterhours",
    "/api/strategy/definition-resources/migrations",
    "/api/internal",
)
INTERNAL_EXACT = {"/api/health", "/api/datasets"}
INTERNAL_DATA_VIEWS = {"silver", "runs", "partitions", "quarantine", "package", "storage", "quality", "freshness"}
INTERNAL_DASHBOARDS = {"storage", "quality", "freshness", "runs", "partitions"}


def is_internal_api(path: str) -> bool:
    return (
        path in INTERNAL_EXACT
        or path in {f"/api/data/{view}" for view in INTERNAL_DATA_VIEWS}
        or path in {f"/api/dashboard/{view}" for view in INTERNAL_DASHBOARDS}
        or any(path == prefix or path.startswith(prefix + "/") for prefix in INTERNAL_PREFIXES)
    )


def install_admin_boundary(app: FastAPI, token: str | None = None, *, port: int = 8765, control_token: str | None = None) -> None:
    credential = token if token is not None else os.environ.get(ADMIN_TOKEN_ENV, "")
    app.state.customer_surface = True

    def internal_authorized(request: Request) -> bool:
        supplied = request.headers.get(ADMIN_TOKEN_HEADER, "")
        return (not request.headers.get("origin") and not request.headers.get("sec-fetch-site")
                and bool(credential) and hmac.compare_digest(supplied.encode("utf-8"), credential.encode("utf-8")))

    @app.middleware("http")
    async def internal_operations_only(request: Request, call_next):
        authorized = internal_authorized(request)
        request.state.internal_authorized = authorized
        internal_heatmap = request.url.path == "/api/metrics/heatmap" and request.query_params.get("category") == "storage"
        if is_internal_api(request.url.path) or internal_heatmap:
            if not authorized:
                return JSONResponse(status_code=403, content={"detail": "该接口仅供获授权的内部管理台使用"})
        return await call_next(request)

    manager = LocalSessionManager("product", port)
    install_local_sessions(app, manager, control_token=control_token, internal_authorizer=internal_authorized)

    @app.post("/api/internal/local-session/bootstrap", include_in_schema=False)
    async def issue_product_bootstrap(request: Request) -> dict:
        # The internal boundary has already required the supervisor credential.
        return {"bootstrap": manager.issue_bootstrap(request.headers["host"]), "audience": "product", "expiresIn": 120}


__all__ = ("ADMIN_TOKEN_ENV", "ADMIN_TOKEN_HEADER", "install_admin_boundary", "is_internal_api")
