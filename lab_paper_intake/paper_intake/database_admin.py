from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH
from .db import SCHEMA_VERSION, init_db


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def database_status(db_path: Path = DB_PATH) -> dict[str, Any]:
    """Return a JSON-safe, non-mutating database health report."""
    path = Path(db_path)
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "expected_schema_version": SCHEMA_VERSION,
    }
    if not path.exists():
        return result
    result["size_bytes"] = path.stat().st_size
    result["wal_size_bytes"] = Path(f"{path}-wal").stat().st_size if Path(f"{path}-wal").exists() else 0
    result["shm_exists"] = Path(f"{path}-shm").exists()
    with closing(_read_only_connection(path)) as conn:
        tables = _table_names(conn)
        quick_check_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        foreign_key_rows = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
        result.update(
            {
                "healthy": quick_check_rows == ["ok"] and not foreign_key_rows,
                "quick_check": quick_check_rows,
                "foreign_key_errors": foreign_key_rows,
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
                "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
                "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
                "freelist_count": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
                "tables": sorted(tables),
            }
        )
        count_tables = (
            "papers",
            "downloaded_pdfs",
            "search_runs",
            "llm_cache",
            "paper_identities",
            "paper_aliases",
            "paper_observations",
            "paper_identity_conflicts",
            "database_anomalies",
            "schema_migrations",
        )
        result["counts"] = {
            table: _scalar(conn, f'SELECT COUNT(*) FROM "{table}"')
            for table in count_tables
            if table in tables
        }
        if "papers" in tables:
            result["duplicate_groups"] = {
                "doi": _scalar(
                    conn,
                    "SELECT COUNT(*) FROM (SELECT doi FROM papers "
                    "WHERE doi IS NOT NULL AND doi <> '' GROUP BY lower(doi) HAVING COUNT(*) > 1)",
                ),
                "arxiv": _scalar(
                    conn,
                    "SELECT COUNT(*) FROM (SELECT arxiv_id FROM papers "
                    "WHERE arxiv_id IS NOT NULL AND arxiv_id <> '' "
                    "GROUP BY lower(arxiv_id) HAVING COUNT(*) > 1)",
                ),
                "normalized_title": _scalar(
                    conn,
                    "SELECT COUNT(*) FROM (SELECT normalized_title FROM papers "
                    "WHERE normalized_title <> '' GROUP BY normalized_title HAVING COUNT(*) > 1)",
                ),
            }
        if "papers" in tables and "downloaded_pdfs" in tables:
            result["orphan_downloads"] = _scalar(
                conn,
                "SELECT COUNT(*) FROM downloaded_pdfs d "
                "LEFT JOIN papers p ON p.id = d.paper_id WHERE p.id IS NULL",
            )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Create a consistent SQLite backup and an adjacent checksum manifest."""
    source_path = Path(db_path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    destination_dir = Path(output_dir) if output_dir else source_path.parent / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{source_path.stem}_{_utc_stamp()}"
    destination = destination_dir / f"{stem}.sqlite"
    counter = 1
    while destination.exists():
        destination = destination_dir / f"{stem}_{counter}.sqlite"
        counter += 1

    source = sqlite3.connect(source_path, timeout=30)
    target = sqlite3.connect(destination)
    try:
        source.execute("PRAGMA busy_timeout = 30000")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()

    status = database_status(destination)
    if not status.get("healthy"):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Backup verification failed: {status}")
    manifest = {
        "source": str(source_path.resolve()),
        "backup": str(destination.resolve()),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": _sha256(destination),
        "size_bytes": destination.stat().st_size,
        "user_version": status.get("user_version"),
        "quick_check": status.get("quick_check"),
        "foreign_key_error_count": len(status.get("foreign_key_errors", [])),
    }
    manifest_path = destination.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**manifest, "manifest": str(manifest_path.resolve())}


def migrate_database(
    db_path: Path = DB_PATH,
    backup_dir: Path | None = None,
    *,
    create_backup: bool = True,
) -> dict[str, Any]:
    """Back up and migrate a database, returning before/after evidence."""
    path = Path(db_path)
    before = database_status(path)
    backup = None
    if path.exists() and create_backup:
        backup = backup_database(path, backup_dir)
    init_db(path)
    after = database_status(path)
    if not after.get("healthy") or after.get("user_version") != SCHEMA_VERSION:
        raise RuntimeError(f"Migration verification failed: {after}")
    return {"before": before, "backup": backup, "after": after}


def restore_database(
    backup_path: Path,
    db_path: Path = DB_PATH,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Restore through a verified temporary copy and retain a safety backup."""
    if not confirm:
        raise ValueError("Restore requires confirm=True")
    source_path = Path(backup_path)
    target_path = Path(db_path)
    source_status = database_status(source_path)
    if not source_status.get("healthy"):
        raise ValueError(f"Backup is not healthy: {source_status}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        probe = sqlite3.connect(target_path, timeout=3, isolation_level=None)
        try:
            probe.execute("PRAGMA busy_timeout = 3000")
            checkpoint = probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise RuntimeError("Refusing restore: the target database is busy")
            probe.execute("BEGIN EXCLUSIVE")
            probe.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "Refusing restore: the target database is open or busy"
            ) from exc
        finally:
            probe.close()
    safety_backup = backup_database(target_path) if target_path.exists() else None
    temporary_path = target_path.with_name(f".{target_path.name}.restore-{_utc_stamp()}.tmp")
    temporary_path.unlink(missing_ok=True)
    source = sqlite3.connect(source_path, timeout=30)
    temporary = sqlite3.connect(temporary_path)
    try:
        source.backup(temporary)
        temporary.commit()
    finally:
        temporary.close()
        source.close()
    temporary_status = database_status(temporary_path)
    if not temporary_status.get("healthy"):
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"Restored copy verification failed: {temporary_status}")
    for sidecar in (Path(f"{temporary_path}-wal"), Path(f"{temporary_path}-shm")):
        try:
            sidecar.unlink(missing_ok=True)
        except PermissionError as exc:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(
                "Refusing restore: temporary SQLite sidecar is still active"
            ) from exc
    for sidecar in (Path(f"{target_path}-wal"), Path(f"{target_path}-shm")):
        try:
            sidecar.unlink(missing_ok=True)
        except PermissionError as exc:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(
                "Refusing restore: SQLite sidecar is still held by another process"
            ) from exc
    temporary_path.replace(target_path)
    init_db(target_path)
    restored_status = database_status(target_path)
    if not restored_status.get("healthy"):
        raise RuntimeError(f"Restored database verification failed: {restored_status}")
    return {
        "source": str(source_path.resolve()),
        "target": str(target_path.resolve()),
        "safety_backup": safety_backup,
        "status": restored_status,
    }
