from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import REPO_ROOT, data_dir, db_path
from backend.migrations.legacy_projects import migrate_legacy_databases, sqlite_backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Idempotent migration into the unified Radar database")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    parser.add_argument("--intake-db", type=Path, default=REPO_ROOT / "lab_paper_intake" / "data" / "papers.db")
    parser.add_argument("--skip-backup", action="store_true", help="Explicitly skip the pre-migration SQLite backup")
    parser.add_argument("--backup-dir", type=Path, default=data_dir() / "backups" / "integration")
    add_execution_arguments(parser, supports_apply=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backup = None
    if args.apply and not args.skip_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = sqlite_backup(args.radar_db, args.backup_dir / f"condmat_radar_before_unified_{stamp}.sqlite")
        if backup["quick_check"] != ["ok"]:
            raise RuntimeError("pre-migration backup failed SQLite validation")
    result = migrate_legacy_databases(args.radar_db, args.intake_db, dry_run=not args.apply)
    result["backup"] = backup
    emit_result(result, args)


if __name__ == "__main__":
    main()
