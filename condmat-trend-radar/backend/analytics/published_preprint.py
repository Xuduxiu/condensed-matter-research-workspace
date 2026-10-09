from __future__ import annotations

import sqlite3
from typing import Any

from backend.db.database import utc_now
from backend.nlp.normalize import month_range, normalize_term

PUBLISHED_MODES = ("strict_core_plus_context_published", "strict_core_published")
PREPRINT_MODE = "arxiv_preprint"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS published_preprint_stats (
          term TEXT NOT NULL,
          month TEXT NOT NULL,
          published_heat_score REAL NOT NULL DEFAULT 0,
          preprint_rise_score REAL NOT NULL DEFAULT 0,
          validation_gap_score REAL NOT NULL DEFAULT 0,
          published_raw_freq INTEGER NOT NULL DEFAULT 0,
          preprint_raw_freq INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT,
          PRIMARY KEY (term, month)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pub_preprint_month ON published_preprint_stats(month)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pub_preprint_gap ON published_preprint_stats(validation_gap_score)")


def _published_mode(conn: sqlite3.Connection) -> str:
    for mode in PUBLISHED_MODES:
        row = conn.execute("SELECT COUNT(*) AS n FROM term_month_stats WHERE data_mode = ?", (mode,)).fetchone()
        if row and int(row["n"] or 0) > 0:
            return mode
    return "strict_core_published"


def _rows_for_mode(conn: sqlite3.Connection, mode: str) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT term, month, SUM(raw_freq) AS raw_freq, SUM(momentum) AS momentum,
               AVG(normalized_share) AS normalized_share
        FROM term_month_stats
        WHERE data_mode = ? AND corpus_scope = 'all' AND display_eligible = 1
        GROUP BY term, month
        """,
        (mode,),
    ).fetchall()
    return {(row["term"], row["month"]): dict(row) for row in rows}


def rebuild_published_preprint_stats(conn: sqlite3.Connection) -> int:
    ensure_table(conn)
    published_mode = _published_mode(conn)
    published = _rows_for_mode(conn, published_mode)
    preprint = _rows_for_mode(conn, PREPRINT_MODE)
    conn.execute("DELETE FROM published_preprint_stats")
    keys = sorted(set(published) | set(preprint))
    inserted = 0
    for term, month in keys:
        pub = published.get((term, month), {})
        pre = preprint.get((term, month), {})
        pub_momentum = float(pub.get("momentum") or 0.0)
        pre_momentum = float(pre.get("momentum") or 0.0)
        pub_share = float(pub.get("normalized_share") or 0.0)
        pre_share = float(pre.get("normalized_share") or 0.0)
        validation_gap = max(0.0, pre_share - pub_share) * (1.0 + pre_momentum)
        conn.execute(
            """
            INSERT OR REPLACE INTO published_preprint_stats
              (term, month, published_heat_score, preprint_rise_score, validation_gap_score,
               published_raw_freq, preprint_raw_freq, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                term,
                month,
                pub_momentum,
                pre_momentum,
                validation_gap,
                int(pub.get("raw_freq") or 0),
                int(pre.get("raw_freq") or 0),
                utc_now(),
            ),
        )
        inserted += 1
    return inserted


def top_published_preprint(conn: sqlite3.Connection, limit: int = 20) -> dict[str, Any]:
    ensure_table(conn)
    concept_filter = """
        FROM published_preprint_stats s
        JOIN concept_lifecycle l ON l.concept = s.term
        WHERE COALESCE(l.concept_class, '') = 'physics_concept'
          AND COALESCE(l.is_platform_term, 0) = 0
    """
    return {
        "published_heat": [dict(row) for row in conn.execute(f"SELECT s.term, SUM(s.published_heat_score) AS score, SUM(s.published_raw_freq) AS raw_freq {concept_filter} GROUP BY s.term HAVING raw_freq >= 2 ORDER BY score DESC LIMIT ?", (limit,)).fetchall()],
        "preprint_rising": [dict(row) for row in conn.execute(f"SELECT s.term, SUM(s.preprint_rise_score) AS score, SUM(s.preprint_raw_freq) AS raw_freq {concept_filter} GROUP BY s.term HAVING raw_freq >= 1 ORDER BY score DESC LIMIT ?", (limit,)).fetchall()],
        "validation_gap": [dict(row) for row in conn.execute(f"SELECT s.term, SUM(s.validation_gap_score) AS score, SUM(s.preprint_raw_freq) AS preprint_raw_freq, SUM(s.published_raw_freq) AS published_raw_freq {concept_filter} GROUP BY s.term HAVING preprint_raw_freq >= 1 ORDER BY score DESC LIMIT ?", (limit,)).fetchall()],
        "cooling_published": [dict(row) for row in conn.execute(f"SELECT s.term, SUM(s.published_heat_score) AS score, SUM(s.published_raw_freq) AS raw_freq {concept_filter} GROUP BY s.term HAVING raw_freq >= 2 ORDER BY score ASC, raw_freq DESC LIMIT ?", (limit,)).fetchall()],
    }


def published_preprint_series(conn: sqlite3.Connection, concept: str, from_month: str | None = None, to_month: str | None = None) -> dict[str, Any]:
    ensure_table(conn)
    term = normalize_term(concept)
    bounds = conn.execute("SELECT MIN(month) AS min_month, MAX(month) AS max_month FROM published_preprint_stats WHERE term = ?", (term,)).fetchone()
    start = (from_month or (bounds["min_month"] if bounds else None) or "2015-01")[:7]
    end = (to_month or (bounds["max_month"] if bounds else None) or start)[:7]
    rows = conn.execute(
        """
        SELECT month, published_heat_score, preprint_rise_score, validation_gap_score,
               published_raw_freq, preprint_raw_freq
        FROM published_preprint_stats
        WHERE term = ? AND month >= ? AND month <= ?
        ORDER BY month
        """,
        (term, start, end),
    ).fetchall()
    by_month = {row["month"]: dict(row) for row in rows}
    series = []
    for month in month_range(start, end):
        series.append(
            by_month.get(
                month,
                {
                    "month": month,
                    "published_heat_score": 0.0,
                    "preprint_rise_score": 0.0,
                    "validation_gap_score": 0.0,
                    "published_raw_freq": 0,
                    "preprint_raw_freq": 0,
                },
            )
        )
    return {"concept": term, "from": start, "to": end, "series": series}