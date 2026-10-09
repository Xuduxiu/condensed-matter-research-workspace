from __future__ import annotations

import argparse
import json

from backend.config import db_path
from backend.db.database import connect, init_db
from backend.ingest.source_coverage import run_source_backfill, run_source_coverage_audit
from backend.migrations.unified_library import apply_unified_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit OpenAlex/Crossref/arXiv overlap and repair exact-ID gaps.")
    parser.add_argument("--scope", choices=("eligible", "all"), default="eligible")
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--to-date", default=None)
    parser.add_argument("--apply", action="store_true", help="Persist provenance seed/audit and allow queue writes. Default is read-only.")
    parser.add_argument("--queue-missing", action="store_true")
    parser.add_argument("--backfill-limit", type=int, default=0, help="Network lookups to perform after queueing; 0 is audit-only.")
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.apply and (args.queue_missing or args.backfill_limit > 0):
        raise SystemExit("--queue-missing/--backfill-limit require explicit --apply")
    with connect() as connection:
        if args.apply:
            init_db(connection)
            apply_unified_schema(connection)
        elif not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_versions'"
        ).fetchone():
            raise SystemExit("unified library schema is not installed")
        before = run_source_coverage_audit(
            connection,
            scope=args.scope,
            window_from=args.from_date,
            window_to=args.to_date,
            queue_missing=args.apply and (args.queue_missing or args.backfill_limit > 0),
            persist=args.apply,
            seed_historical=args.apply,
        )
        if args.apply:
            connection.commit()
        backfill = (
            run_source_backfill(connection, limit=args.backfill_limit, timeout=args.timeout)
            if args.apply and args.backfill_limit
            else None
        )
        after = run_source_coverage_audit(
            connection,
            scope=args.scope,
            window_from=args.from_date,
            window_to=args.to_date,
            queue_missing=False,
        ) if backfill else None
    print(json.dumps({
        "database": str(db_path()),
        "writes_performed": bool(args.apply),
        "before": before,
        "backfill": backfill,
        "after": after,
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()