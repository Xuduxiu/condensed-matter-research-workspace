from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats
from backend.config import ensure_data_layout, logs_dir
from backend.db.database import connect, init_db, normalized_title_key, utc_now
from backend.ingest_real_quickstart import RunLogger, ingest_crossref


def run(args: argparse.Namespace) -> dict[str, Any]:
    ensure_data_layout()
    log_path = logs_dir() / f"crossref_quickstart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = RunLogger(log_path)
    with connect() as conn:
        init_db(conn)
        seen_doi = {row["doi"] for row in conn.execute("SELECT doi FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchall()}
        seen_title = {normalized_title_key(row["title"] or "") for row in conn.execute("SELECT title FROM papers WHERE title IS NOT NULL").fetchall()}
        result = ingest_crossref(conn, args, args.target, seen_doi, seen_title, logger)
        if result["kept_count"]:
            rebuild_paper_terms(conn)
            rebuild_term_month_stats(conn)
            rebuild_lifecycle(conn, args.from_date, args.to_date, args.from_date, args.to_date)
            cooccurrence_network(conn, min_count=2)
    payload = {"status": "finished" if result["kept_count"] else "failed", "log_path": str(log_path), **{k: (dict(v) if hasattr(v, "items") else v) for k, v in result.items() if k != "errors"}, "errors": result.get("errors", [])[:20]}
    logger.write("finished", payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crossref-only quickstart import for Core journal published metadata.")
    parser.add_argument("--target", type=int, default=1500)
    parser.add_argument("--from", dest="from_date", default="2023-01-01")
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat())
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--strict-condmat", action=argparse.BooleanOptionalAction, default=False)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
