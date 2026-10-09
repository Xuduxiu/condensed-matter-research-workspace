from __future__ import annotations

import argparse
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from backend.config import cache_dir, openalex_api_key
from backend.db.database import connect, init_db, normalize_doi, utc_now
from backend.ingest.openalex_client import MISSING_KEY_WARNING, OpenAlexClient, OpenAlexHTTPError
from backend.nlp.dictionaries import CONTEXT_JOURNALS, CORE_JOURNALS
from backend.nlp.normalize import normalize_term
from backend.nlp.term_filters import display_eligibility


def checkpoint_path() -> Path:
    path = cache_dir() / "openalex_enrich_existing_checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_checkpoint(resume: bool) -> dict[str, Any]:
    path = checkpoint_path()
    if not resume or not path.exists():
        return {"processed_ids": [], "updated": 0, "failed": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed_ids": [], "updated": 0, "failed": 0}


def save_checkpoint(data: dict[str, Any]) -> None:
    checkpoint_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def journal_filter(scope: str) -> tuple[str, list[Any]]:
    normalized = (scope or "core").lower()
    if normalized == "core":
        return f"journal IN ({','.join('?' for _ in CORE_JOURNALS)})", list(CORE_JOURNALS)
    if normalized == "context":
        return f"journal IN ({','.join('?' for _ in CONTEXT_JOURNALS)})", list(CONTEXT_JOURNALS)
    if normalized in {"core_context", "all"}:
        journals = list(dict.fromkeys([*CORE_JOURNALS, *CONTEXT_JOURNALS]))
        return f"journal IN ({','.join('?' for _ in journals)})", journals
    return "1=1", []


def candidate_rows(conn, scope: str, missing_only: bool, processed_ids: set[str], limit: int) -> list[dict[str, Any]]:
    where = ["data_mode = 'real'"]
    params: list[Any] = []
    journal_sql, journal_params = journal_filter(scope)
    where.append(journal_sql)
    params.extend(journal_params)
    if missing_only:
        where.append("(raw_openalex_json IS NULL OR raw_openalex_json = '' OR openalex_enriched_at IS NULL OR openalex_id IS NULL OR openalex_id = '')")
    if processed_ids:
        placeholders = ",".join("?" for _ in processed_ids)
        where.append(f"id NOT IN ({placeholders})")
        params.extend(sorted(processed_ids))
    rows = conn.execute(
        f"""
        SELECT id, doi, title, journal, publication_date, openalex_id
        FROM papers
        WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN doi IS NOT NULL AND doi != '' THEN 0 ELSE 1 END, publication_date DESC
        LIMIT ?
        """,
        (*params, max(limit, 1)),
    ).fetchall()
    return [dict(row) for row in rows]



def batch_lookup_works_by_doi(client: OpenAlexClient, rows: list[dict[str, Any]], chunk_size: int = 25) -> dict[str, dict[str, Any]]:
    doi_rows = [row for row in rows if normalize_doi(row.get("doi"))]
    output: dict[str, dict[str, Any]] = {}
    for start in range(0, len(doi_rows), chunk_size):
        chunk = doi_rows[start : start + chunk_size]
        doi_values = [normalize_doi(row.get("doi")) for row in chunk]
        doi_values = [doi for doi in doi_values if doi]
        if not doi_values:
            continue
        try:
            payload = client._get_json(
                "/works",
                {
                    "filter": "doi:" + "|".join(doi_values),
                    "per-page": str(min(200, max(1, len(doi_values)))),
                },
            )
        except OpenAlexHTTPError:
            continue
        for work in payload.get("results") or []:
            doi = normalize_doi(work.get("doi"))
            if doi:
                output[doi] = work
    return output

def lookup_work(client: OpenAlexClient, row: dict[str, Any]) -> dict[str, Any] | None:
    doi = normalize_doi(row.get("doi"))
    if doi:
        endpoint = "/works/" + urllib.parse.quote("https://doi.org/" + doi, safe=":/")
        try:
            return client._get_json(endpoint, {})
        except OpenAlexHTTPError as exc:
            if exc.status not in {404, 400}:
                raise
    title = (row.get("title") or "").strip()
    if not title:
        return None
    payload = client._get_json(
        "/works",
        {
            "search": title[:250],
            "per-page": "1",
            "sort": "relevance_score:desc",
        },
    )
    results = payload.get("results") or []
    return results[0] if results else None


def first_location(work: dict[str, Any]) -> dict[str, Any]:
    return work.get("primary_location") or (work.get("locations") or [{}])[0] or {}


def collect_institutions(authorships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for authorship in authorships:
        for institution in authorship.get("institutions") or []:
            key = institution.get("id") or institution.get("display_name")
            if not key or key in seen:
                continue
            seen.add(key)
            output.append(institution)
    return output


def update_enrichment(conn, paper_id: str, work: dict[str, Any]) -> None:
    location = first_location(work)
    open_access = work.get("open_access") or {}
    authorships = work.get("authorships") or []
    institutions = collect_institutions(authorships)
    pdf_url = location.get("pdf_url") or open_access.get("oa_url") or ""
    landing_url = location.get("landing_page_url") or work.get("id") or ""
    conn.execute(
        """
        UPDATE papers SET
          openalex_id = COALESCE(NULLIF(?, ''), openalex_id),
          url = COALESCE(NULLIF(?, ''), url),
          pdf_url = COALESCE(NULLIF(?, ''), pdf_url),
          oa_url = COALESCE(NULLIF(?, ''), oa_url),
          is_open_access = MAX(COALESCE(is_open_access, 0), ?),
          oa_status = COALESCE(NULLIF(?, ''), oa_status),
          cited_by_count = MAX(COALESCE(cited_by_count, 0), ?),
          raw_openalex_json = ?,
          authorships_json = ?,
          institutions_json = ?,
          openalex_concepts_json = ?,
          openalex_topics_json = ?,
          openalex_keywords_json = ?,
          referenced_works_json = ?,
          related_works_json = ?,
          openalex_enriched_at = ?,
          updated_at = ?
        WHERE id = ?
        """,
        (
            work.get("id") or "",
            landing_url,
            pdf_url,
            open_access.get("oa_url") or "",
            1 if open_access.get("is_oa") or pdf_url else 0,
            open_access.get("oa_status") or "",
            int(work.get("cited_by_count") or 0),
            json.dumps(work, ensure_ascii=False),
            json.dumps(authorships, ensure_ascii=False),
            json.dumps(institutions, ensure_ascii=False),
            json.dumps(work.get("concepts") or [], ensure_ascii=False),
            json.dumps(work.get("topics") or [], ensure_ascii=False),
            json.dumps(work.get("keywords") or [], ensure_ascii=False),
            json.dumps(work.get("referenced_works") or [], ensure_ascii=False),
            json.dumps(work.get("related_works") or [], ensure_ascii=False),
            utc_now(),
            utc_now(),
            paper_id,
        ),
    )
    upsert_openalex_terms(conn, paper_id, work)


def upsert_openalex_terms(conn, paper_id: str, work: dict[str, Any]) -> None:
    terms: list[tuple[Any, ...]] = []
    for item in work.get("concepts") or []:
        term = item.get("display_name") or ""
        score = float(item.get("score") or 0.0)
        normalized = normalize_term(term)
        if not normalized or score < 0.25:
            continue
        eligible, reason = display_eligibility(normalized, "concept", score)
        terms.append((paper_id, term, "concept", normalized, score, int(eligible), reason, "openalex"))
    for item in work.get("keywords") or []:
        term = item.get("display_name") or item.get("keyword") or ""
        score = float(item.get("score") or 0.5)
        normalized = normalize_term(term)
        if not normalized:
            continue
        eligible, reason = display_eligibility(normalized, "concept", score)
        terms.append((paper_id, term, "concept", normalized, score, int(eligible), reason, "openalex"))
    if terms:
        conn.executemany(
            """
            INSERT OR IGNORE INTO paper_terms
              (paper_id, term, term_type, normalized_term, confidence, display_eligible, display_reason, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            terms,
        )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not openalex_api_key():
        raise RuntimeError(MISSING_KEY_WARNING)
    checkpoint = load_checkpoint(args.resume)
    processed_ids = set(checkpoint.get("processed_ids") or [])
    client = OpenAlexClient(timeout=args.timeout, polite_delay=args.sleep_seconds, max_retries=3)
    updated = 0
    failed = 0
    scanned = 0
    errors: list[dict[str, Any]] = []
    max_works = max(0, int(args.max_works or 0))
    while max_works <= 0 or updated + failed < max_works:
        batch_limit = min(args.batch_size, max_works - updated - failed) if max_works > 0 else args.batch_size
        with connect() as conn:
            init_db(conn)
            rows = candidate_rows(conn, args.scope, args.missing_only, processed_ids, batch_limit)
        if not rows:
            break
        doi_map = batch_lookup_works_by_doi(client, rows)
        for row in rows:
            if max_works > 0 and updated + failed >= max_works:
                break
            scanned += 1
            paper_id = row["id"]
            doi = normalize_doi(row.get("doi"))
            try:
                work = doi_map.get(doi or "")
                if not work:
                    work = lookup_work(client, row)
                if not work:
                    failed += 1
                    errors.append({"paper_id": paper_id, "reason": "not_found", "title": row.get("title")})
                else:
                    with connect() as conn:
                        init_db(conn)
                        update_enrichment(conn, paper_id, work)
                    updated += 1
                processed_ids.add(paper_id)
            except Exception as exc:
                failed += 1
                errors.append({"paper_id": paper_id, "type": type(exc).__name__, "message": str(exc)[:300]})
                processed_ids.add(paper_id)
                time.sleep(min(5.0, max(0.1, args.sleep_seconds) * 3))
            checkpoint.update({"processed_ids": sorted(processed_ids), "updated": updated, "failed": failed, "last_paper_id": paper_id, "errors": errors[-20:]})
            save_checkpoint(checkpoint)
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)
    result = {"scanned": scanned, "updated": updated, "failed": failed, "checkpoint_path": str(checkpoint_path()), "errors": errors[-20:]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich existing papers with OpenAlex metadata without downloading PDFs.")
    parser.add_argument("--scope", choices=["core", "context", "core_context", "all"], default="core")
    parser.add_argument("--missing-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-works", type=int, default=1000)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.12)
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> dict[str, Any]:
    return run(parse_args())


if __name__ == "__main__":
    main()