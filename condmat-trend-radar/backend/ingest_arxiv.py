from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats
from backend.db.database import connect, init_db, upsert_paper
from backend.ingest.arxiv_client import ArxivClient
from backend.nlp.condmat_filter import is_condensed_matter_record


def run(args: argparse.Namespace) -> dict[str, Any]:
    categories = [item.strip() for item in args.categories.split(",") if item.strip()]
    client = ArxivClient(timeout=args.timeout)
    papers, failures = client.fetch(args.from_date, args.to_date, max_results=args.max_results, categories=categories)
    kept = inserted = updated = linked = 0
    with connect() as conn:
        init_db(conn)
        for paper in papers:
            keep, reasons = is_condensed_matter_record(paper)
            if not keep:
                continue
            paper["filter_reasons"] = reasons
            if paper.get("doi"):
                existing = conn.execute("SELECT id FROM papers WHERE doi = ? AND source != 'arxiv'", (str(paper["doi"]).lower(),)).fetchone()
                if existing:
                    paper["linked_published_paper_id"] = existing["id"]
                    linked += 1
            status = upsert_paper(conn, paper)
            kept += 1
            inserted += 1 if status == "inserted" else 0
            updated += 1 if status == "updated" else 0
        if kept:
            rebuild_paper_terms(conn)
            rebuild_term_month_stats(conn)
            rebuild_lifecycle(conn, args.from_date, args.to_date, args.from_date, args.to_date)
            cooccurrence_network(conn, min_count=2)
    return {"fetched_count": len(papers), "kept_count": kept, "inserted": inserted, "updated": updated, "linked_published_paper_count": linked, "failed_count": failures}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest real arXiv cond-mat metadata.")
    parser.add_argument("--from", dest="from_date", default="2024-01-01")
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat())
    parser.add_argument("--categories", default="cond-mat.str-el,cond-mat.mtrl-sci,cond-mat.mes-hall,cond-mat.supr-con")
    parser.add_argument("--max-results", type=int, default=500)
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
