from __future__ import annotations

import json
from typing import Any

from backend.db.database import connect, db_path, init_db
from backend.nlp.normalize import normalize_term


WATCH_TERMS = ["hBN", "graphene", "ARPES", "STM", "DFT"]
TIMESERIES_TERMS = ["ZrTe5", "HfTe5", "moiré", "FCI", "altermagnetism"]


def rows(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return row[0]


def coverage(total: int, count: int) -> dict[str, Any]:
    return {"count": count, "ratio": round(count / total, 4) if total else 0.0}


def term_status(conn, term: str) -> dict[str, Any]:
    normalized = normalize_term(term)
    stats = rows(
        conn,
        """
        SELECT pt.term_type, pt.display_eligible, pt.display_reason, COUNT(DISTINCT pt.paper_id) AS papers
        FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
        WHERE p.data_mode='real' AND pt.normalized_term=?
        GROUP BY pt.term_type, pt.display_eligible, pt.display_reason
        ORDER BY papers DESC
        """,
        (normalized,),
    )
    return {"term": term, "normalized_term": normalized, "total_papers": sum(int(item["papers"] or 0) for item in stats), "breakdown": stats}


def nonzero_months(conn, term: str) -> dict[str, Any]:
    normalized = normalize_term(term)
    months = rows(
        conn,
        """
        SELECT month, SUM(raw_freq) AS raw_freq, SUM(weighted_freq) AS weighted_freq
        FROM term_month_stats
        WHERE data_mode='real' AND corpus_scope='core' AND term=? AND raw_freq > 0
        GROUP BY month
        ORDER BY month
        """,
        (normalized,),
    )
    return {"term": term, "normalized_term": normalized, "nonzero_month_count": len(months), "months": months}


def top_terms(conn, term_type: str, limit: int = 20) -> list[dict[str, Any]]:
    return rows(
        conn,
        """
        SELECT pt.normalized_term AS term, COUNT(DISTINCT pt.paper_id) AS count
        FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
        WHERE p.data_mode='real' AND pt.term_type=? AND pt.display_eligible=1
        GROUP BY pt.normalized_term
        ORDER BY count DESC, term ASC LIMIT ?
        """,
        (term_type, limit),
    )


def inspect_real_data() -> dict[str, Any]:
    with connect() as conn:
        init_db(conn)
        total = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real'") or 0)
        doi_count = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(doi, '') != ''") or 0)
        abstract_count = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(abstract, '') != ''") or 0)
        report = {
            "db_path": str(db_path()),
            "total_real_papers": total,
            "by_journal": rows(conn, "SELECT journal, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY journal ORDER BY count DESC"),
            "by_year": rows(conn, "SELECT year, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY year ORDER BY year"),
            "by_source": rows(conn, "SELECT source, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY source ORDER BY count DESC"),
            "empty_title_count": int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(title, '') = ''") or 0),
            "empty_date_count": int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(publication_date, '') = ''") or 0),
            "empty_journal_count": int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(journal, '') = ''") or 0),
            "doi_coverage": coverage(total, doi_count),
            "abstract_coverage": coverage(total, abstract_count),
            "top_concepts": top_terms(conn, "concept"),
            "top_materials": top_terms(conn, "material"),
            "top_methods": top_terms(conn, "method"),
            "filtered_generic_term_examples": rows(
                conn,
                """
                SELECT pt.normalized_term AS term, pt.term_type, pt.display_reason, COUNT(DISTINCT pt.paper_id) AS count
                FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
                WHERE p.data_mode='real' AND pt.display_eligible=0
                GROUP BY pt.normalized_term, pt.term_type, pt.display_reason
                ORDER BY count DESC, term ASC LIMIT 20
                """,
            ),
            "watch_term_status": [term_status(conn, term) for term in WATCH_TERMS],
            "nonzero_time_series": [nonzero_months(conn, term) for term in TIMESERIES_TERMS],
        }
        return report


def main() -> None:
    print(json.dumps(inspect_real_data(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()