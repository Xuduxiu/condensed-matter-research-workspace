from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import REPO_ROOT, db_path
from backend.migrations.verification import verify_migration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only verification of the unified migration")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    parser.add_argument("--intake-db", type=Path, default=REPO_ROOT / "lab_paper_intake" / "data" / "papers.db")
    add_execution_arguments(parser, supports_apply=False)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = verify_migration(args.radar_db, args.intake_db)
    emit_result(result, args)
    if not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
