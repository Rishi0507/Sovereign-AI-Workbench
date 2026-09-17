"""FastAPI application: JSON API under ``/api`` and the server-rendered UI at ``/``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from workbench import __version__
from workbench.api import routes_files, routes_library, routes_system, routes_tasks
from workbench.core.errors import ApprovalRequired, NotFound, PolicyError, WorkbenchError
from workbench.runtime import Runtime
from workbench.security import egress_guard
from workbench.settings import get_settings

log = logging.getLogger("workbench.api")
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def create_app(rt: Runtime | None = None, install_guard: bool | None = None) -> FastAPI:
    owns_runtime = rt is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = app.state.rt
        guard = runtime.settings.egress_guard if install_guard is None else install_guard
        if guard:
            allow = egress_guard.load_allowlist(runtime.settings.config_dir / "egress_allowlist.yaml")
            egress_guard.install(allow, runtime.egress.report)
        health = runtime.health()
        for name in ("sandboxd", "egressd"):
            if health[name].get("status") != "ok":
                log.warning("%s is not available: %s", name, health[name].get("error"))
        runtime.audit.append({"type": "server.start", "version": __version__, "health": {
            k: v.get("status") if isinstance(v, dict) else v for k, v in health.items()}})
        yield
        if owns_runtime:
            runtime.close()

    app = FastAPI(title="Sovereign AI Workbench", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.rt = rt or Runtime(get_settings(), start_threads=True)
    for module in (routes_system, routes_files, routes_tasks, routes_library):
        app.include_router(module.router, prefix="/api")

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(PolicyError)
    async def refused(_: Request, exc: PolicyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(ApprovalRequired)
    async def conflict(_: Request, exc: ApprovalRequired) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(WorkbenchError)
    async def failed(_: Request, exc: WorkbenchError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    from workbench.ui.pages import router as ui_router

    app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")
    app.include_router(ui_router)
    return app
