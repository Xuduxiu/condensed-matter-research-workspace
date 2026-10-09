from __future__ import annotations

import sqlite3
from typing import Any

from backend.analytics.stats import paper_mode_predicate
from backend.nlp.normalize import month_range, normalize_term


def concept_evidence(conn: sqlite3.Connection, concept: str, data_mode: str = "strict_core_published", limit: int = 12) -> dict[str, Any]:
    term = normalize_term(concept)
    mode_sql, mode_params = paper_mode_predicate(data_mode, "p")
    rows = conn.execute(
        f"""
        SELECT p.id, p.doi, p.title, p.journal, p.publication_date, p.url, p.openalex_id,
               p.cited_by_count, p.abstract, p.raw_openalex_json
        FROM papers p JOIN paper_terms pt ON pt.paper_id = p.id
        WHERE pt.normalized_term = ? AND {mode_sql}
        GROUP BY p.id
        ORDER BY p.cited_by_count DESC, p.publication_date DESC
        LIMIT ?
        """,
        (term, *mode_params, limit),
    ).fetchall()
    key_papers = []
    for row in rows:
        abstract = row["abstract"] or ""
        key_papers.append(
            {
                "id": row["id"],
                "doi": row["doi"],
                "title": row["title"],
                "journal": row["journal"],
                "publication_date": row["publication_date"],
                "url": row["url"],
                "openalex_id": row["openalex_id"],
                "cited_by_count": row["cited_by_count"],
                "abstract_snippet": abstract[:400],
            }
        )
    observed = [dict(row) for row in conn.execute(
        """
        SELECT month, raw_freq, weighted_freq, momentum
        FROM term_month_stats
        WHERE term = ? AND data_mode = ? AND corpus_scope = 'all'
        ORDER BY month
        """,
        (term, data_mode),
    ).fetchall()]
    series = observed
    if observed:
        bounds = conn.execute(
            "SELECT MIN(month) AS min_month, MAX(month) AS max_month FROM monthly_corpus_stats WHERE data_mode = ?",
            (data_mode,),
        ).fetchone()
        if bounds and bounds["min_month"] and bounds["max_month"]:
            by_month = {item["month"]: item for item in observed}
            series = [
                by_month.get(month, {"month": month, "raw_freq": 0, "weighted_freq": 0.0, "momentum": 0.0})
                for month in month_range(bounds["min_month"], bounds["max_month"])
            ]
    methods = associated(conn, term, "method", data_mode)
    materials = associated(conn, term, "material", data_mode)
    gaps = []
    if not key_papers:
        gaps.append("no_local_key_papers")
    if not series:
        gaps.append("no_cached_timeseries")
    return {"concept": term, "data_mode": data_mode, "key_papers": key_papers, "series": series, "associated_methods": methods, "associated_materials": materials, "evidence_missing": gaps}


def associated(conn: sqlite3.Connection, term: str, term_type: str, data_mode: str) -> list[dict[str, Any]]:
    mode_sql, mode_params = paper_mode_predicate(data_mode, "p")
    return [dict(row) for row in conn.execute(
        f"""
        SELECT other.normalized_term AS term, COUNT(DISTINCT p.id) AS count
        FROM paper_terms root
        JOIN papers p ON p.id = root.paper_id
        JOIN paper_terms other ON other.paper_id = p.id
        WHERE root.normalized_term = ? AND other.term_type = ? AND other.normalized_term != ? AND {mode_sql}
        GROUP BY other.normalized_term ORDER BY count DESC, term ASC LIMIT 20
        """,
        (term, term_type, term, *mode_params),
    ).fetchall()]