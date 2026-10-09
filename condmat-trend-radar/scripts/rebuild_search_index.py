from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import db_path
from backend.library.local_search import rebuild_search_index
from backend.migrations.unified_library import apply_unified_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the local SQLite FTS5 library index")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    add_execution_arguments(parser, supports_apply=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.apply:
        connection = sqlite3.connect(f"file:{args.radar_db.resolve().as_posix()}?mode=ro", uri=True)
        try:
            versions = connection.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0]
            emit_result({"status": "PASS", "dry_run": True, "writes_performed": False, "versions_to_index": versions}, args)
        finally:
            connection.close()
        return
    connection = sqlite3.connect(args.radar_db, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        apply_unified_schema(connection)
        result = rebuild_search_index(connection)
        connection.commit()
        emit_result({"status": "PASS", "dry_run": False, **result}, args)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
