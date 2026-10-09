from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from backend.config import logs_dir
from backend.db.database import connect, init_db, utc_now
from backend.ingest.crossref_client import CrossrefClient


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = CrossrefClient(timeout=args.timeout)
    where = ["doi IS NOT NULL", "doi != ''"]
    if args.missing_only:
        where.append("raw_crossref_json IS NULL")
    with connect() as conn:
        init_db(conn)
        rows = conn.execute(
            f"SELECT id, doi FROM papers WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        updated = 0
        failed: list[dict[str, str]] = []
        for row in rows:
            doi = row["doi"]
            try:
                payload = client.get_work(doi)
                if not payload:
                    failed.append({"doi": doi, "error": "not_found"})
                    continue
                raw = json.dumps(payload.get("raw_json", payload), ensure_ascii=False)
                conn.execute(
                    """
                    UPDATE papers SET
                      abstract = CASE WHEN abstract IS NULL OR abstract = '' THEN ? ELSE abstract END,
                      journal = CASE WHEN journal IS NULL OR journal = '' OR journal = 'Unknown' THEN ? ELSE journal END,
                      publication_date = CASE WHEN publication_date IS NULL OR publication_date = '' THEN ? ELSE publication_date END,
                      url = COALESCE(NULLIF(url, ''), ?),
                      raw_crossref_json = ?,
                      updated_at = ?
                    WHERE id = ?
                    """,
                    (payload.get("abstract") or "", payload.get("journal") or "", payload.get("publication_date") or "", payload.get("url") or "", raw, utc_now(), row["id"]),
                )
                updated += 1
            except Exception as exc:
                failed.append({"doi": doi, "error": f"{type(exc).__name__}: {exc}"})
    result = {"updated_count": updated, "failed_count": len(failed), "failed": failed[:50]}
    if failed:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (directory / f"crossref_failed_{stamp}.json").write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich existing DOI papers with Crossref metadata.")
    parser.add_argument("--missing-only", action="store_true", default=False)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=12)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
