from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import REPO_ROOT, data_dir, db_path
from backend.library.pdf_importer import import_existing_pdfs
from backend.migrations.unified_library import apply_unified_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hash-deduplicate and import legacy PDFs")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    parser.add_argument("--intake-db", type=Path, default=REPO_ROOT / "lab_paper_intake" / "data" / "papers.db")
    parser.add_argument("--root", action="append", type=Path, default=[])
    parser.add_argument("--destination", type=Path, default=data_dir() / "library" / "pdf")
    parser.add_argument("--no-text", action="store_true")
    add_execution_arguments(parser, supports_apply=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    roots = args.root or [
        REPO_ROOT / "lab_paper_intake" / "data" / "pdfs",
        REPO_ROOT / "lab_paper_intake" / "data" / "exports",
        REPO_ROOT / "lab_paper_intake" / "exports",
    ]
    if args.apply:
        connection = sqlite3.connect(args.radar_db, timeout=60)
    else:
        connection = sqlite3.connect(f"file:{args.radar_db.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        if args.apply:
            apply_unified_schema(connection)
        result = import_existing_pdfs(
            connection,
            roots,
            args.destination,
            intake_db=args.intake_db,
            dry_run=not args.apply,
            extract_text=not args.no_text,
        )
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
