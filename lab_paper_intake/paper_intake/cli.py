from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import DB_PATH, candidate_inbox_dirs, ensure_data_dirs
from .database_admin import (
    backup_database,
    database_status,
    migrate_database,
    restore_database,
)
from .task_importer import (
    find_latest_task_file,
    import_task_file,
    summarize_task_queue,
)


def _add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"SQLite database path (default: {DB_PATH})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lab Paper Intake command line tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser(
        "import-tasks",
        help="Import a Trend Radar CSV/JSON task file into the paper library.",
    )
    importer.add_argument(
        "path",
        nargs="?",
        help="Task file; defaults to the newest inbox file.",
    )
    importer.add_argument(
        "--inbox",
        action="append",
        default=[],
        help="Additional inbox to scan.",
    )
    importer.add_argument(
        "--unselected",
        action="store_true",
        help="Import papers without selecting them for the next export.",
    )

    status = subparsers.add_parser(
        "queue-status",
        help="Summarize pending and imported Trend Radar task batches.",
    )
    status.add_argument(
        "--inbox",
        action="append",
        default=[],
        help="Additional inbox to scan.",
    )

    db_status = subparsers.add_parser(
        "db-status",
        help="Inspect schema, integrity, duplication, and provenance counts without mutation.",
    )
    _add_db_argument(db_status)

    db_backup = subparsers.add_parser(
        "db-backup",
        help="Create and verify a consistent SQLite backup with SHA-256 manifest.",
    )
    _add_db_argument(db_backup)
    db_backup.add_argument("--output-dir", type=Path)

    db_migrate = subparsers.add_parser(
        "db-migrate",
        help="Back up and migrate the Intake database to the latest schema.",
    )
    _add_db_argument(db_migrate)
    db_migrate.add_argument("--backup-dir", type=Path)
    db_migrate.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the safety backup (intended only for disposable database copies).",
    )

    db_restore = subparsers.add_parser(
        "db-restore",
        help="Restore a verified backup after a safety backup and active-lock check.",
    )
    db_restore.add_argument("backup", type=Path)
    _add_db_argument(db_restore)
    db_restore.add_argument(
        "--confirm",
        action="store_true",
        help="Required acknowledgement that the target database will be replaced.",
    )
    return parser


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "db-status":
        report = database_status(args.db)
        _print_json(report)
        return 0 if report.get("exists") and report.get("healthy") else 1

    if args.command == "db-backup":
        _print_json(backup_database(args.db, args.output_dir))
        return 0

    if args.command == "db-migrate":
        _print_json(
            migrate_database(
                args.db,
                args.backup_dir,
                create_backup=not args.no_backup,
            )
        )
        return 0

    if args.command == "db-restore":
        if not args.confirm:
            _print_json({"ok": False, "error": "restore_requires_--confirm"})
            return 2
        _print_json(restore_database(args.backup, args.db, confirm=True))
        return 0

    search_dirs = [Path(path) for path in args.inbox] or candidate_inbox_dirs()
    if args.command == "queue-status":
        _print_json(summarize_task_queue(search_dirs).as_dict())
        return 0

    if args.command == "import-tasks":
        ensure_data_dirs()
        task_path = Path(args.path) if args.path else find_latest_task_file(search_dirs)
        if task_path is None:
            _print_json({"ok": False, "error": "no_task_file_found"})
            return 2
        result = import_task_file(task_path, selected=not args.unselected)
        _print_json(result.as_dict())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
