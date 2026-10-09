from __future__ import annotations

import sqlite3
import unittest

from backend.analytics.stats import concept_series, rebuild_preset_term_month_stats, scoped_months, top_terms
from backend.db.database import init_db
from backend.llm.evidence_builder import concept_evidence
from backend.db.strict_condmat import summarize_term_timeseries


class SparseTermMonthStatsTests(unittest.TestCase):
    def connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for paper_id, month in (("p1", "2020-01"), ("p2", "2020-03"), ("p3", "2020-03")):
            conn.execute(
                "INSERT INTO papers (id, title, month, journal, source, data_mode, condmat_view_eligible) VALUES (?, ?, ?, 'arXiv', 'arxiv', 'real', 1)",
                (paper_id, paper_id, month),
            )
        for paper_id, term in (("p1", "Alpha"), ("p2", "Alpha"), ("p3", "Beta")):
            conn.execute(
                "INSERT INTO paper_terms (paper_id, term, term_type, normalized_term, display_eligible) VALUES (?, ?, 'concept', ?, 1)",
                (paper_id, term, term),
            )
        return conn

    def test_sparse_rows_reconstruct_dense_series_and_average(self) -> None:
        conn = self.connection()
        try:
            inserted = rebuild_preset_term_month_stats(conn, "all_real")
            self.assertEqual(inserted, 3)
            rows = conn.execute(
                "SELECT term, month, raw_freq FROM term_month_stats WHERE data_mode='real' ORDER BY term, month"
            ).fetchall()
            self.assertEqual(
                [(row["term"], row["month"], row["raw_freq"]) for row in rows],
                [("Alpha", "2020-01", 1), ("Alpha", "2020-03", 1), ("Beta", "2020-03", 1)],
            )
            self.assertEqual(scoped_months(conn, "all", None, None, "real"), ["2020-01", "2020-02", "2020-03"])
            series = concept_series(conn, "Alpha", "all", "2020-01", "2020-03", data_mode="real")
            self.assertEqual([row["raw_freq"] for row in series], [1, 0, 1])
            ranked = top_terms(conn, "raw_freq", 5, "all", "2020-01", "2020-03", "real")
            self.assertEqual(ranked[0]["term"], "Alpha")
            self.assertAlmostEqual(ranked[0]["normalized_share"], 0.5)
            evidence = concept_evidence(conn, "Alpha", "real")
            self.assertEqual([row["raw_freq"] for row in evidence["series"]], [1, 0, 1])
            summary = summarize_term_timeseries(conn, "Alpha", "real", "all")
            self.assertEqual(summary["months"], 3)
            self.assertEqual(summary["nonzero_months"], 2)
        finally:
            conn.close()

    def test_rebuild_rolls_back_deleted_rows_on_insert_failure(self) -> None:
        conn = self.connection()
        try:
            conn.execute(
                "INSERT INTO term_month_stats (term, month, corpus_scope, data_mode, raw_freq, weighted_freq, momentum, journal_breakdown) VALUES ('old', '2019-01', 'all', 'real', 1, 1, 1, '{}')"
            )
            conn.execute(
                "CREATE TRIGGER reject_alpha BEFORE INSERT ON term_month_stats WHEN NEW.term='Alpha' BEGIN SELECT RAISE(ABORT, 'forced'); END"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                rebuild_preset_term_month_stats(conn, "all_real")
            row = conn.execute("SELECT term FROM term_month_stats WHERE data_mode='real'").fetchone()
            self.assertEqual(row["term"], "old")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
