from __future__ import annotations

import hmac
import threading
from collections.abc import Callable

from fastapi import APIRouter, Header, HTTPException


router = APIRouter(tags=["desktop-control"])
_shutdown_callback: Callable[[], None] | None = None
_shutdown_token = ""
_lock = threading.Lock()


def configure_shutdown(callback: Callable[[], None], token: str) -> None:
    global _shutdown_callback, _shutdown_token
    with _lock:
        _shutdown_callback = callback
        _shutdown_token = token


@router.post("/api/desktop/shutdown", include_in_schema=False)
def desktop_shutdown(x_condmat_stop_token: str = Header(default="")) -> dict[str, bool]:
    with _lock:
        callback = _shutdown_callback
        token = _shutdown_token
    if callback is None or not token or not hmac.compare_digest(x_condmat_stop_token, token):
        raise HTTPException(status_code=404, detail="Not found")
    callback()
    return {"stopping": True}