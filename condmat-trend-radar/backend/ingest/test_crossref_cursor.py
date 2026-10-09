from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from backend.ingest.crossref_client import CrossrefClient
from backend.ingest.journal_registry import find_journal


def run_check(args: argparse.Namespace) -> dict[str, Any]:
    journal = find_journal(args.journal)
    if not journal:
        raise SystemExit(f"unknown journal: {args.journal}")
    client = CrossrefClient(timeout=args.timeout, polite_delay=args.sleep_seconds)
    pages: list[dict[str, Any]] = []
    for page in client.iterate_crossref_works(
        journal,
        args.from_date,
        args.to_date,
        rows=args.rows,
        cursor="*",
        max_pages=args.pages,
        sleep_seconds=args.sleep_seconds,
    ):
        pages.append(
            {
                "page": page.page,
                "cursor_used_prefix": page.cursor_used_prefix,
                "next_cursor_prefix": page.next_cursor_prefix,
                "fetched": page.fetched,
                "paper_count": len(page.papers),
                "duplicate_rate_vs_previous_page": page.duplicate_rate_vs_previous_page,
                "stop_reason": page.stop_reason,
                "errors": page.errors[:3],
            }
        )
        if page.stop_reason and page.page < args.pages:
            break
    same_cursor_count = sum(1 for item in pages if item["next_cursor_prefix"] and item["next_cursor_prefix"] == item["cursor_used_prefix"])
    high_duplicate_pages = sum(1 for item in pages[1:] if float(item["duplicate_rate_vs_previous_page"] or 0) > 0.95)
    errors = [item for item in pages if item.get("errors")]
    cursor_left_star = len(pages) < 2 or pages[1]["cursor_used_prefix"] != "*"
    ok = len(pages) >= min(args.pages, 1) and cursor_left_star and high_duplicate_pages < 2 and not errors
    return {
        "ok": ok,
        "journal": journal.canonical_name,
        "date_from": args.from_date,
        "date_to": args.to_date,
        "requested_pages": args.pages,
        "observed_pages": len(pages),
        "same_cursor_count": same_cursor_count,
        "cursor_left_star": cursor_left_star,
        "high_duplicate_pages": high_duplicate_pages,
        "pages": pages,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Crossref cursor pagination advances across pages.")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    result = run_check(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()