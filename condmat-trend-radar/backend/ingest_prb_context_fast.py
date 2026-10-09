from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from typing import Any

from backend.config import ensure_data_layout
from backend.db.database import connect, init_db, upsert_paper, utc_now
from backend.ingest.openalex_client import OpenAlexClient
from backend.ingest_real_quickstart import classify_condmat


def run(args: argparse.Namespace) -> dict[str, Any]:
    ensure_data_layout()
    client = OpenAlexClient(timeout=args.timeout, polite_delay=args.sleep_seconds, max_retries=3)
    source_id = client.find_source_id(args.journal)
    if not source_id:
        raise RuntimeError(f"OpenAlex source not found for {args.journal}")
    cursor = args.cursor or "*"
    fetched = 0
    kept = 0
    inserted = 0
    updated = 0
    skipped = 0
    source_breakdown: Counter[str] = Counter()
    with connect() as conn:
        init_db(conn)
        while True:
            if args.max_papers and fetched >= args.max_papers:
                break
            payload = client.fetch_journal_page(args.journal, source_id, args.from_date, args.to_date, per_page=200, cursor=cursor)
            raw_items = payload.get("results") or []
            if not raw_items:
                break
            for raw in raw_items:
                if args.max_papers and fetched >= args.max_papers:
                    break
                fetched += 1
                paper = client._normalize_work(raw, args.journal)
                paper["source_scope"] = "published"
                keep, confidence, reasons = classify_condmat(paper, strict_condmat=False)
                if not keep:
                    skipped += 1
                    continue
                paper["data_mode"] = "real"
                paper["condmat_confidence"] = confidence
                paper["filter_reasons"] = reasons
                status = upsert_paper(conn, paper)
                if status == "inserted":
                    inserted += 1
                else:
                    updated += 1
                kept += 1
                source_breakdown[str(paper.get("source") or "openalex")] += 1
            conn.commit()
            cursor = payload.get("meta", {}).get("next_cursor") or ""
            if not cursor:
                break
            if args.sleep_seconds:
                time.sleep(args.sleep_seconds)
    result = {
        "started_at": utc_now(),
        "journal": args.journal,
        "from": args.from_date,
        "to": args.to_date,
        "source_id": source_id,
        "fetched": fetched,
        "kept": kept,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "source_breakdown": dict(source_breakdown),
        "next_cursor": bool(cursor),
        "pdf_download_started": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast metadata-only OpenAlex context ingest for a single journal.")
    parser.add_argument("--journal", default="Physical Review B")
    parser.add_argument("--from", dest="from_date", default="2015-01-01")
    parser.add_argument("--to", dest="to_date", default="2026-07-10")
    parser.add_argument("--max-papers", type=int, default=10000)
    parser.add_argument("--sleep-seconds", type=float, default=0.12)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--cursor", default="*")
    return parser.parse_args()


def main() -> dict[str, Any]:
    return run(parse_args())


if __name__ == "__main__":
    main()