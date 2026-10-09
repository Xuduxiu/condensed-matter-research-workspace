from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats
from backend.db.database import connect, init_db, normalized_title_key, upsert_paper
from backend.ingest.arxiv_client import ARXIV_CATEGORIES, ArxivClient
from backend.ingest_real_quickstart import classify_condmat


def run(args: argparse.Namespace) -> dict[str, Any]:
    max_results = min(5000, max(args.target * 2, args.target + 300))
    papers, failures = ArxivClient(timeout=args.timeout).fetch(args.from_date, args.to_date, max_results=max_results, categories=ARXIV_CATEGORIES)
    fetched = len(papers)
    kept = inserted = updated = deduped = 0
    with connect() as conn:
        init_db(conn)
        seen_doi = {row["doi"] for row in conn.execute("SELECT doi FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchall()}
        seen_title = {normalized_title_key(row["title"] or "") for row in conn.execute("SELECT title FROM papers WHERE title IS NOT NULL").fetchall()}
        for paper in papers:
            keep, confidence, reasons = classify_condmat(paper, strict_condmat=args.strict_condmat)
            if not keep:
                continue
            doi = (paper.get("doi") or "").lower()
            title_key = normalized_title_key(paper.get("title") or "")
            if doi and doi in seen_doi:
                deduped += 1
                continue
            if not doi and title_key and title_key in seen_title:
                deduped += 1
                continue
            if doi:
                seen_doi.add(doi)
            if title_key:
                seen_title.add(title_key)
            paper["data_mode"] = "real"
            paper["source_scope"] = "preprint"
            paper["condmat_confidence"] = confidence
            paper["filter_reasons"] = reasons
            status = upsert_paper(conn, paper)
            kept += 1
            inserted += 1 if status == "inserted" else 0
            updated += 1 if status == "updated" else 0
            if kept >= args.target:
                break
        if kept:
            rebuild_paper_terms(conn)
            rebuild_term_month_stats(conn)
            rebuild_lifecycle(conn, args.from_date, args.to_date, args.from_date, args.to_date)
            cooccurrence_network(conn, min_count=2)
    return {"status": "finished" if kept else "failed", "fetched_count": fetched, "kept_count": kept, "deduped_count": deduped, "failed_count": failures, "inserted": inserted, "updated": updated, "categories": ARXIV_CATEGORIES}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="arXiv-only quickstart import for real cond-mat preprint metadata.")
    parser.add_argument("--target", type=int, default=1500)
    parser.add_argument("--from", dest="from_date", default="2023-01-01")
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat())
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--strict-condmat", action=argparse.BooleanOptionalAction, default=False)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
