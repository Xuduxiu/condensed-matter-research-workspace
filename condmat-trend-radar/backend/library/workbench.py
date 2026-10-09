from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from backend.library.repository import stable_id, utc_now


READING_STATUSES = {"unread", "later", "reading", "read", "archived"}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS workbench_schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_user_state (
        canonical_paper_id TEXT PRIMARY KEY,
        favorite INTEGER NOT NULL DEFAULT 0,
        reading_status TEXT NOT NULL DEFAULT 'unread'
            CHECK(reading_status IN ('unread','later','reading','read','archived')),
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_user_state_favorite ON paper_user_state(favorite, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_paper_user_state_reading ON paper_user_state(reading_status, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS paper_collections (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        color TEXT NOT NULL DEFAULT '#56b6c2',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_papers (
        collection_id TEXT NOT NULL,
        canonical_paper_id TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        added_at TEXT NOT NULL,
        PRIMARY KEY(collection_id, canonical_paper_id),
        FOREIGN KEY(collection_id) REFERENCES paper_collections(id) ON DELETE CASCADE,
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_collection_papers_paper ON collection_papers(canonical_paper_id, added_at)",
    """
    CREATE TABLE IF NOT EXISTS user_action_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_action_log_created ON user_action_log(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_papers_updated_at ON papers(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_papers_mode_updated ON papers(data_mode, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_paper_materials_material ON paper_materials(material_id, canonical_paper_id, source)",
    "CREATE INDEX IF NOT EXISTS idx_paper_materials_paper ON paper_materials(canonical_paper_id, material_id, source)",
    """
    CREATE TABLE IF NOT EXISTS workbench_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_hits (
        id TEXT PRIMARY KEY,
        monitor_id TEXT NOT NULL,
        canonical_paper_id TEXT NOT NULL,
        paper_version_id TEXT NOT NULL,
        first_matched_at TEXT NOT NULL,
        last_matched_at TEXT NOT NULL,
        first_run_id TEXT,
        last_run_id TEXT,
        match_count INTEGER NOT NULL DEFAULT 1,
        payload_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(monitor_id, paper_version_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitor_hits_monitor_latest ON monitor_hits(monitor_id, last_matched_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_monitor_hits_latest ON monitor_hits(last_matched_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS bulk_download_runs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        total_count INTEGER NOT NULL,
        processed_count INTEGER NOT NULL DEFAULT 0,
        completed_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        paper_version_ids_json TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '{}',
        zotero_package_id TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error_message TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bulk_download_runs_created ON bulk_download_runs(created_at DESC)",
)


LIVE_SCAN_SETTING_KEYS = (
    "live_scan_enabled",
    "live_scan_interval_seconds",
    "live_scan_abstract_backfill_limit",
)


def ensure_workbench_schema(connection: sqlite3.Connection) -> None:
    # This function is called from read-heavy API routes. Once v3 and its
    # defaults are present, avoid DDL/INSERT OR IGNORE statements: SQLite
    # treats them as writes and concurrent dashboard requests can otherwise
    # contend with a running scanner.
    try:
        migration = connection.execute(
            "SELECT 1 FROM workbench_schema_migrations WHERE version=5"
        ).fetchone()
        placeholders = ", ".join("?" for _ in LIVE_SCAN_SETTING_KEYS)
        setting_count = int(connection.execute(
            f"SELECT COUNT(*) FROM workbench_settings WHERE key IN ({placeholders})",
            LIVE_SCAN_SETTING_KEYS,
        ).fetchone()[0])
        monitor_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_queries'"
        ).fetchone()
        monitor_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(monitor_queries)")
        } if monitor_table else set()
        monitor_history_ready = not monitor_table or "last_checked_at" in monitor_columns
        if migration and setting_count == len(LIVE_SCAN_SETTING_KEYS) and monitor_history_ready:
            return
    except sqlite3.OperationalError:
        # Fresh databases do not have the workbench tables yet.
        pass

    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(
        """
        INSERT OR IGNORE INTO workbench_schema_migrations(version, name, applied_at)
        VALUES (2, 'radar_workbench_v2', ?)
        """,
        (utc_now(),),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO workbench_schema_migrations(version, name, applied_at)
        VALUES (3, 'radar_live_workflow_v3', ?)
        """,
        (utc_now(),),
    )
    now = utc_now()
    connection.executemany(
        "INSERT OR IGNORE INTO workbench_settings(key, value, updated_at) VALUES (?, ?, ?)",
        [
            (LIVE_SCAN_SETTING_KEYS[0], "1", now),
            (LIVE_SCAN_SETTING_KEYS[1], "900", now),
            (LIVE_SCAN_SETTING_KEYS[2], "10", now),
        ],
    )
    monitor_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_queries'"
    ).fetchone()
    if monitor_table:
        monitor_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(monitor_queries)")}
        if "last_checked_at" not in monitor_columns:
            connection.execute("ALTER TABLE monitor_queries ADD COLUMN last_checked_at TEXT")
    connection.execute(
        """
        INSERT OR IGNORE INTO workbench_schema_migrations(version, name, applied_at)
        VALUES (4, 'radar_monitor_history_v4', ?)
        """,
        (utc_now(),),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO workbench_schema_migrations(version, name, applied_at)
        VALUES (5, 'radar_dashboard_material_index_v5', ?)
        """,
        (utc_now(),),
    )


def record_action(
    connection: sqlite3.Connection,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO user_action_log(action, entity_type, entity_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (action, entity_type, entity_id, json.dumps(payload or {}, ensure_ascii=False), utc_now()),
    )


def get_user_state(connection: sqlite3.Connection, canonical_paper_id: str) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    row = connection.execute(
        "SELECT * FROM paper_user_state WHERE canonical_paper_id=?",
        (canonical_paper_id,),
    ).fetchone()
    state = dict(row) if row else {
        "canonical_paper_id": canonical_paper_id,
        "favorite": 0,
        "reading_status": "unread",
        "note": "",
        "created_at": None,
        "updated_at": None,
    }
    state["favorite"] = bool(state.get("favorite"))
    state["collections"] = [
        dict(item)
        for item in connection.execute(
            """
            SELECT c.id, c.name, c.description, c.color, cp.added_at, cp.note
            FROM collection_papers cp
            JOIN paper_collections c ON c.id=cp.collection_id
            WHERE cp.canonical_paper_id=?
            ORDER BY c.name COLLATE NOCASE
            """,
            (canonical_paper_id,),
        )
    ]
    return state


def set_user_state(
    connection: sqlite3.Connection,
    canonical_paper_id: str,
    *,
    favorite: bool | None = None,
    reading_status: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    if reading_status is not None and reading_status not in READING_STATUSES:
        raise ValueError("unsupported reading status")
    current = get_user_state(connection, canonical_paper_id)
    now = utc_now()
    next_favorite = bool(current["favorite"]) if favorite is None else bool(favorite)
    next_status = str(current["reading_status"]) if reading_status is None else reading_status
    next_note = str(current["note"] or "") if note is None else note.strip()[:4000]
    connection.execute(
        """
        INSERT INTO paper_user_state
        (canonical_paper_id, favorite, reading_status, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_paper_id) DO UPDATE SET
          favorite=excluded.favorite,
          reading_status=excluded.reading_status,
          note=excluded.note,
          updated_at=excluded.updated_at
        """,
        (canonical_paper_id, int(next_favorite), next_status, next_note, now, now),
    )
    record_action(
        connection,
        "paper_state_updated",
        "paper",
        canonical_paper_id,
        {"favorite": next_favorite, "reading_status": next_status},
    )
    if not next_favorite and next_status == "unread" and not next_note:
        connection.execute(
            "DELETE FROM paper_user_state WHERE canonical_paper_id=?",
            (canonical_paper_id,),
        )
    return get_user_state(connection, canonical_paper_id)


def list_collections(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_workbench_schema(connection)
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT c.*, COUNT(cp.canonical_paper_id) AS paper_count,
                   MAX(cp.added_at) AS last_paper_added_at
            FROM paper_collections c
            LEFT JOIN collection_papers cp ON cp.collection_id=c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC, c.name COLLATE NOCASE
            """
        )
    ]


def save_collection(
    connection: sqlite3.Connection,
    *,
    name: str,
    description: str = "",
    color: str = "#56b6c2",
    collection_id: str | None = None,
) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    clean_name = " ".join(name.split())[:120]
    if not clean_name:
        raise ValueError("collection name is required")
    now = utc_now()
    target_id = collection_id or stable_id("collection", clean_name.casefold())
    connection.execute(
        """
        INSERT INTO paper_collections(id, name, description, color, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name,
          description=excluded.description,
          color=excluded.color,
          updated_at=excluded.updated_at
        """,
        (target_id, clean_name, description.strip()[:1000], color[:20], now, now),
    )
    record_action(connection, "collection_saved", "collection", target_id, {"name": clean_name})
    row = connection.execute(
        "SELECT *, 0 AS paper_count FROM paper_collections WHERE id=?",
        (target_id,),
    ).fetchone()
    return dict(row)


def add_papers_to_collection(
    connection: sqlite3.Connection,
    collection_id: str,
    canonical_paper_ids: Iterable[str],
) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    if not connection.execute("SELECT 1 FROM paper_collections WHERE id=?", (collection_id,)).fetchone():
        raise LookupError("collection not found")
    ids = list(dict.fromkeys(str(value) for value in canonical_paper_ids if value))[:500]
    now = utc_now()
    added = 0
    for paper_id in ids:
        if not connection.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
            continue
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO collection_papers(collection_id, canonical_paper_id, added_at)
            VALUES (?, ?, ?)
            """,
            (collection_id, paper_id, now),
        )
        added += int(cursor.rowcount)
    connection.execute("UPDATE paper_collections SET updated_at=? WHERE id=?", (now, collection_id))
    record_action(
        connection,
        "papers_added_to_collection",
        "collection",
        collection_id,
        {"requested": len(ids), "added": added},
    )
    return {"collection_id": collection_id, "requested": len(ids), "added": added}


def remove_paper_from_collection(
    connection: sqlite3.Connection,
    collection_id: str,
    canonical_paper_id: str,
) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    cursor = connection.execute(
        "DELETE FROM collection_papers WHERE collection_id=? AND canonical_paper_id=?",
        (collection_id, canonical_paper_id),
    )
    record_action(
        connection,
        "paper_removed_from_collection",
        "collection",
        collection_id,
        {"canonical_paper_id": canonical_paper_id},
    )
    return {"collection_id": collection_id, "canonical_paper_id": canonical_paper_id, "removed": int(cursor.rowcount)}


def delete_collection(connection: sqlite3.Connection, collection_id: str) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    row = connection.execute(
        "SELECT name FROM paper_collections WHERE id=?",
        (collection_id,),
    ).fetchone()
    if not row:
        raise LookupError("collection not found")
    paper_count = int(connection.execute(
        "SELECT COUNT(*) FROM collection_papers WHERE collection_id=?",
        (collection_id,),
    ).fetchone()[0])
    connection.execute("DELETE FROM paper_collections WHERE id=?", (collection_id,))
    record_action(
        connection,
        "collection_deleted",
        "collection",
        collection_id,
        {"name": row["name"], "paper_count": paper_count},
    )
    return {"id": collection_id, "name": row["name"], "paper_count": paper_count, "deleted": True}

def get_live_scan_settings(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    values = {str(row["key"]): str(row["value"]) for row in connection.execute("SELECT key, value FROM workbench_settings")}
    return {
        "enabled": values.get("live_scan_enabled", "1") == "1",
        "interval_seconds": max(300, int(values.get("live_scan_interval_seconds", "900"))),
        "abstract_backfill_limit": max(0, min(25, int(values.get("live_scan_abstract_backfill_limit", "10")))),
    }


def set_live_scan_settings(
    connection: sqlite3.Connection,
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    abstract_backfill_limit: int | None = None,
) -> dict[str, Any]:
    ensure_workbench_schema(connection)
    updates: dict[str, str] = {}
    if enabled is not None:
        updates["live_scan_enabled"] = "1" if enabled else "0"
    if interval_seconds is not None:
        updates["live_scan_interval_seconds"] = str(max(300, min(86400, int(interval_seconds))))
    if abstract_backfill_limit is not None:
        updates["live_scan_abstract_backfill_limit"] = str(max(0, min(25, int(abstract_backfill_limit))))
    now = utc_now()
    for key, value in updates.items():
        connection.execute(
            """
            INSERT INTO workbench_settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now),
        )
    if updates:
        record_action(connection, "live_scan_settings_updated", "settings", "live_scan", updates)
    return get_live_scan_settings(connection)
