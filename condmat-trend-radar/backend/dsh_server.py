"""Serve the paper workspace for one local DSH process, on an OS-assigned port."""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import Request
from fastapi.responses import Response

from backend.api.main import create_app, frontend_dist_path, initialize_database
from backend.api.translation_routes import set_worker
from backend.config import db_path
from backend.library.translations import TranslationWorker, automatic_enabled
from backend.llm.dsh_translation_bridge import DshTranslationBridge
from backend.scheduler.live_scanner import start_live_scanner, stop_live_scanner


def resume_enabled(expected: Path, actual: Path) -> bool:
    if expected.resolve() != actual or not actual.is_file():
        return False
    with closing(sqlite3.connect(actual.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return automatic_enabled(connection)


def serve(parent_origin: str, resume_database: Path | None = None) -> None:
    parent = urlsplit(parent_origin)
    if (parent.scheme != "http" or parent.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parent.username is not None or parent.password is not None
            or parent_origin != f"http://{parent.netloc}" or not parent.port):
        raise ValueError("Expected a local DSH origin with a port")
    database = db_path().resolve()
    if resume_database is not None and not resume_enabled(resume_database, database):
        print(json.dumps({"type": "paper-desk:translation-auto", "enabled": False,
                          "database": str(resume_database)}), flush=True)
        return
    if frontend_dist_path() is None:
        raise RuntimeError("Build the Radar frontend before starting Paper Desk")
    initialize_database()
    app = create_app()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
        origin = f"http://{address}"

        @app.middleware("http")
        async def local_only(request: Request, call_next):
            if request.headers.get("host") != address:
                return Response(status_code=403)
            if request.method not in {"GET", "HEAD"} and (
                request.headers.get("origin", origin) != origin
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return Response(status_code=403)
            response = await call_next(request)
            # Desktop uses its registered Electron scheme; Web uses the authenticated HTTP host.
            response.headers["Content-Security-Policy"] = f"frame-ancestors {parent_origin} dsh-app://app 'self'"
            return response

        class DeskServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                if self.started:
                    bridge.send({"origin": origin})
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                        conn.row_factory = sqlite3.Row
                        notify_auto(automatic_enabled(conn))
                    worker.start()
                    if resume_database is None:
                        activate_ui()

        server = DeskServer(uvicorn.Config(app, log_level="warning", access_log=False))

        def owner_disconnected() -> None:
            worker.request_stop()
            server.should_exit = True

        scanner_lock = threading.Lock()

        def activate_ui() -> None:
            # Background translation alone does not start discovery or download jobs.
            with scanner_lock:
                if not server.should_exit:
                    start_live_scanner()

        bridge = DshTranslationBridge(sys.stdin.buffer, sys.stdout.buffer, owner_disconnected, activate_ui)

        def notify_auto(enabled: bool) -> None:
            bridge.send({"type": "paper-desk:translation-auto", "enabled": enabled, "database": str(database)})

        worker = TranslationWorker(database, bridge.translate, available=bridge.available,
                                   provider_status=bridge.status, on_auto_change=notify_auto)
        set_worker(worker)
        bridge.start()
        try:
            server.run(sockets=[listener])
        finally:
            set_worker(None)
            worker.request_stop()
            bridge.close()
            worker.close()
            stop_live_scanner()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("parent_origin")
    parser.add_argument("--resume-translation-db", type=Path)
    options = parser.parse_args()
    serve(options.parent_origin, options.resume_translation_db)
