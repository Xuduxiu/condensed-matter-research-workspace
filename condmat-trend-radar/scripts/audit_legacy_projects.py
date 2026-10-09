from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import REPO_ROOT, db_path
from backend.library.pdf_importer import discover_pdf_paths, hash_pdf
from backend.migrations.legacy_projects import audit_legacy_projects


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit of Radar and Lab Paper Intake")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    parser.add_argument("--intake-db", type=Path, default=REPO_ROOT / "lab_paper_intake" / "data" / "papers.db")
    parser.add_argument("--pdf-root", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, help="Deprecated alias for --log-file")
    add_execution_arguments(parser, supports_apply=False)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = audit_legacy_projects(args.radar_db, args.intake_db)
    roots = args.pdf_root or [
        REPO_ROOT / "lab_paper_intake" / "data" / "pdfs",
        REPO_ROOT / "lab_paper_intake" / "data" / "exports",
        REPO_ROOT / "lab_paper_intake" / "exports",
    ]
    hashes: dict[str, list[str]] = {}
    invalid: list[str] = []
    for path in discover_pdf_paths(roots):
        digest, _, valid = hash_pdf(path)
        hashes.setdefault(digest, []).append(str(path))
        if not valid:
            invalid.append(str(path))
    result["pdfs"] = {
        "roots": [str(root) for root in roots],
        "files": sum(len(paths) for paths in hashes.values()),
        "unique_hashes": len(hashes),
        "duplicate_copies": sum(len(paths) for paths in hashes.values()) - len(hashes),
        "duplicate_groups": sum(1 for paths in hashes.values() if len(paths) > 1),
        "invalid_headers": invalid,
    }
    if args.output and not args.log_file:
        args.log_file = args.output
    emit_result(result, args)


if __name__ == "__main__":
    main()
