from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..db import init_db
from .routers import (
    audit,
    auth,
    clusters,
    dashboard,
    datastores,
    flr,
    inventory,
    jobs,
    points,
    repositories,
    tasks,
    users,
    vcenters,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "Strict-Transport-Security": "max-age=31536000",
}


def create_app(*, create_tables: bool = True) -> FastAPI:
    if create_tables:
        init_db()

    app = FastAPI(title="OpenBackup", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  redoc_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (auth, users, audit, vcenters, inventory, datastores, clusters, repositories, jobs,
              tasks, points, flr, dashboard):
        app.include_router(r.router)

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    index = WEB_DIR / "index.html"
    if (WEB_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (WEB_DIR / path).resolve()
        if path and candidate.is_file() and WEB_DIR in candidate.parents:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "Web UI not built"}, status_code=404)
