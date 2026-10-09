from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.db.database import connect
from backend.library.workbench import get_live_scan_settings, set_live_scan_settings
from backend.scheduler.daily_update import DailyOptions, active_daily_run, create_daily_run, run_daily_update


_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def live_scan_status() -> dict[str, Any]:
    with connect() as connection:
        settings = get_live_scan_settings(connection)
        active = active_daily_run()
        row = connection.execute(
            """
            SELECT started_at, finished_at, status FROM daily_runs
            WHERE status IN ('completed','partial','failed','cancelled')
            ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1
            """
        ).fetchone()
        last_at = _parse_time(str((row["finished_at"] or row["started_at"]) if row else ""))
        now = datetime.now(timezone.utc)
        next_at = (last_at + timedelta(seconds=settings["interval_seconds"])) if last_at else now
        due = bool(settings["enabled"] and not active and next_at <= now)
        return {
            **settings,
            "active_run": active,
            "last_scan_at": last_at.isoformat(timespec="seconds") if last_at else None,
            "next_scan_at": next_at.isoformat(timespec="seconds"),
            "due": due,
            "scheduler_running": bool(_thread and _thread.is_alive()),
            "live_sources": ["OpenAlex configured journals", "Crossref configured journals", "arXiv cond-mat"],
            "full_refresh_sources": ["OpenAlex configured journals", "Crossref configured journals", "arXiv cond-mat"],
        }


def update_live_scan_settings(**values: Any) -> dict[str, Any]:
    with connect() as connection:
        settings = set_live_scan_settings(connection, **values)
    return {**live_scan_status(), **settings}


def _loop() -> None:
    while not _stop_event.is_set():
        try:
            status = live_scan_status()
            if status["enabled"] and status["due"] and not status["active_run"]:
                options = DailyOptions(
                    dry_run=False,
                    scan_mode="live",
                    download_limit=3,
                    abstract_backfill_limit=int(status["abstract_backfill_limit"]),
                    force_stale_lock=True,
                )
                run_id = create_daily_run(options, trigger_type="automatic_live")
                run_daily_update(options, run_id=run_id, trigger_type="automatic_live")
        except Exception:
            # Failure details are persisted by run_daily_update. The scheduler stays alive.
            pass
        _stop_event.wait(20)


def start_live_scanner() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="condmat-live-scanner", daemon=True)
    _thread.start()


def stop_live_scanner() -> None:
    global _thread
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)
    _thread = None
