from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from backend.config import db_path
from backend.db.database import connect, init_db
from backend.db.strict_condmat import refresh_generalist_journal_flags
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
    batch_size: int = 500,
) -> dict[str, Any]:
    target = database or db_path()
    if dry_run:
        with closing(_read_only_connection(target)) as connection:
            return refresh_generalist_journal_flags(
                connection,
                batch_size=batch_size,
                dry_run=True,
            )

    with connect(target) as connection:
        init_db(connection)
        apply_unified_schema(connection)
        return refresh_generalist_journal_flags(
            connection,
            batch_size=batch_size,
            dry_run=False,
            commit_batches=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reclassify only Nature/Science generalist-journal records using "
            "literal title/abstract condensed-matter evidence."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist eligibility changes. Default is a read-only dry run.",
    )
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run(
        dry_run=not args.apply,
        database=args.database,
        batch_size=args.batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
