from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from backend.config import db_path
from backend.db.database import connect, init_db
from backend.ingest.openalex_quality import reclassify_openalex_repository_quality
from backend.migrations.unified_library import apply_unified_schema


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA query_only = ON")
    return connection


def run(
    *,
    dry_run: bool = True,
    database: Path | None = None,
) -> dict[str, Any]:
    target = database or db_path()
    if dry_run:
        # The default command is genuinely read-only: no bootstrap migration,
        # schema DDL, commit, or eligibility update is attempted.
        with closing(_read_only_connection(target)) as connection:
            return reclassify_openalex_repository_quality(
                connection,
                dry_run=True,
            )

    with connect(target) as connection:
        init_db(connection)
        apply_unified_schema(connection)
        return reclassify_openalex_repository_quality(
            connection,
            dry_run=False,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently exclude repository-only OpenAlex records from the "
            "hotspot corpus without deleting papers or source versions."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist eligibility downgrades. The default is a read-only dry run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(run(dry_run=not args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
