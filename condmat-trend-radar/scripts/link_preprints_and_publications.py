from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import db_path
from backend.library.version_linker import link_preprints_and_publications
from backend.migrations.unified_library import apply_unified_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Link arXiv preprints with published versions")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    add_execution_arguments(parser, supports_apply=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    connection = sqlite3.connect(args.radar_db, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        if args.apply:
            apply_unified_schema(connection)
        result = link_preprints_and_publications(connection, dry_run=not args.apply)
        if args.apply:
            connection.commit()
        emit_result(result, args)
    except BaseException:
        if args.apply:
            connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
