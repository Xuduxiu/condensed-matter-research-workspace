from __future__ import annotations

import copy
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Hashable


# Bump this when payload semantics change without a database migration.
ANALYTICS_ALGORITHM_VERSION = "v2.5-2026-08-10"
_MAX_ENTRIES = 16
_CACHE: OrderedDict[tuple[Hashable, ...], dict[str, Any]] = OrderedDict()
_IN_FLIGHT: dict[tuple[Hashable, ...], threading.Event] = {}
_LOCK = threading.Lock()


def _file_revision(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_size), int(stat.st_mtime_ns))


def analytics_database_revision(connection: sqlite3.Connection) -> tuple[Hashable, ...]:
    """Return a cheap revision token covering SQLite database and WAL writes."""
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database_name = str(database_row[2] or "") if database_row else ""
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    if not database_name or database_name == ":memory:":
        return (
            ANALYTICS_ALGORITHM_VERSION,
            database_name or f"memory:{id(connection)}",
            schema_version,
            user_version,
            int(connection.total_changes),
        )

    database_path = Path(database_name).resolve()
    wal_path = Path(f"{database_path}-wal")
    return (
        ANALYTICS_ALGORITHM_VERSION,
        str(database_path),
        schema_version,
        user_version,
        _file_revision(database_path),
        _file_revision(wal_path),
    )


def cached_analytics_payload(
    *,
    days: int,
    revision: tuple[Hashable, ...],
    builder: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Cache one analytics result per time window and exact DB revision."""
    key = ("analytics", int(days), *revision)
    while True:
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                return copy.deepcopy(cached)
            event = _IN_FLIGHT.get(key)
            if event is None:
                event = threading.Event()
                _IN_FLIGHT[key] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        event.wait(timeout=90.0)

    try:
        payload = builder()
        with _LOCK:
            prefix = ("analytics", int(days))
            obsolete = [item_key for item_key in _CACHE if item_key[:2] == prefix and item_key != key]
            for item_key in obsolete:
                _CACHE.pop(item_key, None)
            _CACHE[key] = copy.deepcopy(payload)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        return payload
    finally:
        with _LOCK:
            finished = _IN_FLIGHT.pop(key, None)
            if finished is not None:
                finished.set()


def clear_analytics_cache() -> None:
    with _LOCK:
        _CACHE.clear()
