from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cli_support import add_execution_arguments, emit_result
from backend.config import db_path
from backend.scheduler.daily_update import DailyOptions, run_daily_update


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the unified Radar daily pipeline")
    parser.add_argument("--radar-db", type=Path, default=db_path())
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--no-arxiv", action="store_true")
    parser.add_argument("--download-limit", type=int, default=10)
    parser.add_argument("--force-stale-lock", action="store_true")
    add_execution_arguments(parser, supports_apply=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    options = DailyOptions(
        dry_run=not args.apply,
        skip_network=args.skip_network,
        scope=args.scope,
        include_arxiv=not args.no_arxiv,
        download_limit=args.download_limit,
        force_stale_lock=args.force_stale_lock,
    )
    result = run_daily_update(options, database=args.radar_db)
    emit_result(result, args)


if __name__ == "__main__":
    main()
