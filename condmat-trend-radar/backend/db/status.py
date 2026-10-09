from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from backend.config import db_path as configured_db_path


REQUIRED_TABLES = {
    "papers",
    "paper_terms",
    "term_month_stats",
    "monthly_corpus_stats",
    "ingest_runs",
    "ingest_checkpoints",
}


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def database_status(
    path: Path | None = None,
    *,
    full_check: bool = False,
) -> dict[str, Any]:
    """Inspect the Radar database without creating, migrating, or checkpointing it."""
    target = Path(path) if path else configured_db_path()
    result: dict[str, Any] = {
        "path": str(target.resolve()),
        "exists": target.exists(),
        "check_mode": "full" if full_check else "light",
    }
    if not target.exists():
        result["healthy"] = False
        result["error"] = "database_not_found"
        return result

    result["size_bytes"] = target.stat().st_size
    result["wal_size_bytes"] = (
        Path(f"{target}-wal").stat().st_size if Path(f"{target}-wal").exists() else 0
    )
    try:
        with closing(_read_only_connection(target)) as conn:
            tables = _table_names(conn)
            missing_tables = sorted(REQUIRED_TABLES - tables)
            result.update(
                {
                    "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
                    "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
                    "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
                    "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
                    "freelist_count": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
                    "missing_required_tables": missing_tables,
                }
            )
            counts: dict[str, int] = {}
            if "papers" in tables:
                counts["papers"] = _scalar(conn, "SELECT COUNT(*) FROM papers")
                paper_columns = _table_columns(conn, "papers")
                if "data_mode" in paper_columns:
                    counts["real_papers"] = _scalar(
                        conn, "SELECT COUNT(*) FROM papers WHERE data_mode = 'real'"
                    )
                    counts["mock_papers"] = _scalar(
                        conn, "SELECT COUNT(*) FROM papers WHERE data_mode = 'mock'"
                    )
            for table in ("ingest_runs", "ingest_checkpoints"):
                if table in tables:
                    counts[table] = _scalar(conn, f'SELECT COUNT(*) FROM "{table}"')
            result["counts"] = counts
            result["quick_check"] = None
            result["foreign_key_error_count"] = None
            if full_check:
                quick_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
                foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
                result["quick_check"] = quick_rows
                result["foreign_key_error_count"] = len(foreign_key_errors)
                result["healthy"] = (
                    not missing_tables
                    and quick_rows == ["ok"]
                    and not foreign_key_errors
                )
            else:
                result["healthy"] = not missing_tables
    except sqlite3.Error as exc:
        result["healthy"] = False
        result["error"] = str(exc)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only CondMat Radar database status")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    status = database_status(args.db, full_check=args.full)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status.get("healthy") else 1


if __name__ == "__main__":
    raise SystemExit(main())