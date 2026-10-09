from __future__ import annotations

import argparse
import json

from backend.integration.paper_downloader_bridge import export_to_downloader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export trend radar paper selections to the local paper downloader inbox.")
    parser.add_argument("--concept", default="")
    parser.add_argument("--concepts", default="")
    parser.add_argument("--mode", choices=["or", "and"], default="or")
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--from", dest="from_month", default="2015-01")
    parser.add_argument("--to", dest="to_month", default="2026-07")
    parser.add_argument("--min-momentum", type=float, default=0.0)
    parser.add_argument("--journal", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--include-arxiv", action="store_true")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    concepts = [args.concept] if args.concept else []
    if args.concepts:
        concepts.extend([item.strip() for item in args.concepts.split(",") if item.strip()])
    result = export_to_downloader(
        concepts=concepts,
        mode=args.mode,
        from_month=args.from_month,
        to_month=args.to_month,
        scope=args.scope,
        min_momentum=args.min_momentum,
        journal=args.journal,
        limit=args.limit,
        include_arxiv=args.include_arxiv,
        output_dir=args.output_dir or None,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
