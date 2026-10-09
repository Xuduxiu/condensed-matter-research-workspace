from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.desktop_control import router as desktop_control_router
from backend.api.library_routes import router as library_router
from backend.api.routes import router
from backend.api.ui_v2_routes import router as ui_v2_router
from backend.api.workbench_routes import router as workbench_router
from backend.config import PROJECT_ROOT
from backend.db.database import connect, init_db
from backend.db.strict_condmat import apply_strict_condmat_policy_migrations
from backend.library.workbench import ensure_workbench_schema
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.live_scanner import start_live_scanner, stop_live_scanner



def frontend_dist_path() -> Path | None:
    override = os.getenv("CONDMAT_RADAR_FRONTEND_DIST", "").strip()
    bundle_root = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    candidates = [
        Path(override).expanduser() if override else None,
        bundle_root / "frontend_dist",
        PROJECT_ROOT / "frontend" / "dist",
    ]
    for candidate in candidates:
        if candidate and (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Condensed Matter Trend Radar",
        description="Local-first metadata analytics for condensed matter trend tracking.",
        version="2.4.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(desktop_control_router)
    app.include_router(library_router)
    app.include_router(ui_v2_router)
    app.include_router(workbench_router)
    frontend = frontend_dist_path()
    if frontend:
        app.mount("/", StaticFiles(directory=str(frontend), html=True), name="radar-web")
    return app


app = create_app()


def main() -> None:
    host = os.getenv("CONDMAT_RADAR_API_HOST", "127.0.0.1")
    port = int(os.getenv("CONDMAT_RADAR_API_PORT", "8000"))
    with connect() as connection:
        init_db(connection)
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        apply_strict_condmat_policy_migrations(connection)
    start_live_scanner()
    try:
        uvicorn.run(app, host=host, port=port, reload=False)
    finally:
        stop_live_scanner()


if __name__ == "__main__":
    main()
