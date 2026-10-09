from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.db.database import init_db
from backend.library.repository import (
    LibraryRepository,
    _case_exact_candidates,
    _fts_title_phrase,
    _in_clause,
    _openalex_exact_candidates,
)
from backend.migrations.unified_library import apply_unified_schema


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    return connection


class ExactIdentityLookupPerformanceTests(unittest.TestCase):
    def test_external_id_mapping_short_circuits_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            paper = repository.upsert_version(
                {"title": "External ID priority", "arxiv_id": "cond-mat/0123456"},
                source="arxiv",
                source_record_id="external-priority",
            )
            traced: list[str] = []
            connection.set_trace_callback(traced.append)
            actual = repository.find_exact_canonical(
                {"arxiv_id": "COND-MAT/0123456"}
            )
            connection.set_trace_callback(None)

            self.assertEqual(actual, (paper.canonical_paper_id, "arxiv_external_id", 0.99))
            lookup_sql = [sql.lower() for sql in traced if sql.lstrip().lower().startswith("select")]
            self.assertTrue(lookup_sql)
            self.assertIn("from paper_external_ids", lookup_sql[0])
            self.assertFalse(any("from paper_versions" in sql for sql in lookup_sql))
            connection.close()

    def test_case_compatible_fallback_uses_only_exact_column_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            arxiv = repository.upsert_version(
                {"title": "arXiv fallback", "arxiv_id": "COND-MAT/0654321"},
                source="legacy",
                source_record_id="arxiv-fallback",
            )
            openalex = repository.upsert_version(
                {
                    "title": "OpenAlex fallback",
                    "openalex_id": "https://openalex.org/W987654321",
                },
                source="legacy",
                source_record_id="openalex-fallback",
            )
            connection.execute(
                "DELETE FROM paper_external_ids WHERE canonical_paper_id IN (?, ?)",
                (arxiv.canonical_paper_id, openalex.canonical_paper_id),
            )

            traced: list[str] = []
            connection.set_trace_callback(traced.append)
            arxiv_match = repository.find_exact_canonical(
                {"arxiv_id": "cond-mat/0654321"}
            )
            openalex_match = repository.find_exact_canonical(
                {"openalex_id": "https://openalex.org/w987654321"}
            )
            connection.set_trace_callback(None)

            self.assertEqual(arxiv_match[0], arxiv.canonical_paper_id)
            self.assertEqual(arxiv_match[1], "arxiv_exact")
            self.assertEqual(openalex_match[0], openalex.canonical_paper_id)
            self.assertEqual(openalex_match[1], "openalex_exact")
            lookup_sql = "\n".join(traced).lower()
            self.assertNotIn("lower(arxiv_id)", lookup_sql)
            self.assertNotIn("lower(openalex_id)", lookup_sql)
            connection.close()

    def test_fallback_query_plans_search_existing_id_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            arxiv_candidates = _case_exact_candidates("cond-mat/0123456")
            openalex_candidates = _openalex_exact_candidates(
                "https://openalex.org/w123456789"
            )
            cases = (
                (
                    "papers",
                    "idx_papers_doi",
                    "SELECT id FROM papers WHERE doi=? "
                    "AND doi IS NOT NULL AND doi <> '' LIMIT 1",
                    ("10.1234/indexed",),
                ),
                (
                    "paper_versions",
                    "idx_paper_versions_arxiv",
                    "SELECT canonical_paper_id FROM paper_versions "
                    f"WHERE arxiv_id IN ({_in_clause(arxiv_candidates)}) LIMIT 1",
                    arxiv_candidates,
                ),
                (
                    "papers",
                    "idx_papers_arxiv",
                    "SELECT id FROM papers "
                    f"WHERE arxiv_id IN ({_in_clause(arxiv_candidates)}) LIMIT 1",
                    arxiv_candidates,
                ),
                (
                    "papers",
                    "idx_papers_openalex",
                    "SELECT id FROM papers "
                    f"WHERE openalex_id IN ({_in_clause(openalex_candidates)}) LIMIT 1",
                    openalex_candidates,
                ),
            )
            for table, index, sql, params in cases:
                with self.subTest(table=table, index=index):
                    plan = connection.execute(
                        f"EXPLAIN QUERY PLAN {sql}", params
                    ).fetchall()
                    details = " | ".join(str(row[3]) for row in plan)
                    self.assertIn(f"SEARCH {table} USING INDEX {index}", details)
                    self.assertNotIn(f"SCAN {table}", details)
                    self.assertNotIn("lower(", sql.lower())
            connection.close()


    def test_title_candidate_plans_use_btree_and_fts_not_corpus_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            exact_plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT v.id
                FROM paper_versions v INDEXED BY idx_paper_versions_title
                JOIN papers p ON p.id=v.canonical_paper_id
                WHERE v.title=?
                """,
                ("Indexed exact title",),
            ).fetchall()
            exact_details = " | ".join(str(row[3]) for row in exact_plan)
            self.assertIn(
                "SEARCH v USING INDEX idx_paper_versions_title (title=?)",
                exact_details,
            )
            self.assertNotIn("SCAN v", exact_details)

            fts_plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT v.id
                FROM library_fts
                JOIN paper_versions v ON v.id=library_fts.paper_version_id
                WHERE library_fts MATCH ?
                """,
                (_fts_title_phrase("indexed exact title"),),
            ).fetchall()
            fts_details = " | ".join(str(row[3]) for row in fts_plan)
            self.assertIn("SCAN library_fts VIRTUAL TABLE INDEX", fts_details)
            self.assertIn("SEARCH v USING", fts_details)
            self.assertNotIn("SCAN v", fts_details)
            connection.close()

    def test_title_fallback_uses_small_candidates_and_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            paper = repository.upsert_version(
                {
                    "title": "Index-friendly identity in quantum-matter",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                },
                source="openalex",
                source_record_id="title-index-base",
            )
            traced: list[str] = []
            connection.set_trace_callback(traced.append)
            actual = repository.find_canonical(
                {
                    "title": "Index friendly identity in quantum matter",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                }
            )
            connection.set_trace_callback(None)

            self.assertEqual(actual[0], paper.canonical_paper_id)
            self.assertEqual(actual[1], "title_author_year")
            lookup_sql = "\n".join(
                sql.lower()
                for sql in traced
                if sql.lstrip().lower().startswith("select")
            )
            self.assertIn("indexed by idx_paper_versions_title", lookup_sql)
            self.assertIn("library_fts match", lookup_sql)
            self.assertNotIn(" between ", lookup_sql)
            self.assertNotIn("group by", lookup_sql)
            self.assertNotIn("lower(trim", lookup_sql)
            connection.close()

    def test_title_lookup_vm_work_stays_bounded_with_large_same_year_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            target = repository.upsert_version(
                {
                    "title": "Bounded identity lookup for quantum-matter",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                },
                source="openalex",
                source_record_id="bounded-target",
            )
            distractor_count = 10_000
            papers = [
                (f"distractor-paper-{index}", f"Unrelated study number {index}", 2025)
                for index in range(distractor_count)
            ]
            versions = [
                (
                    f"distractor-version-{index}",
                    f"distractor-paper-{index}",
                    f"Unrelated study number {index}",
                    f"distractor-source-{index}",
                )
                for index in range(distractor_count)
            ]
            connection.executemany(
                "INSERT INTO papers(id, title, year, data_mode) VALUES (?, ?, ?, 'real')",
                papers,
            )
            connection.executemany(
                """
                INSERT INTO paper_versions(
                    id, canonical_paper_id, version_type, title, source,
                    source_record_id, raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'metadata', ?, 'fixture', ?, '{}', 'now', 'now')
                """,
                versions,
            )
            connection.executemany(
                """
                INSERT INTO library_fts(
                    canonical_paper_id, paper_version_id, title, abstract, body
                ) VALUES (?, ?, ?, '', '')
                """,
                ((paper_id, version_id, title) for version_id, paper_id, title, _ in versions),
            )

            progress_calls = [0]

            def count_vm_work() -> int:
                progress_calls[0] += 1
                return 0

            connection.set_progress_handler(count_vm_work, 100)
            actual = repository.find_canonical(
                {
                    "title": "Bounded identity lookup for quantum matter",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                }
            )
            connection.set_progress_handler(None, 0)
            connection.close()

            self.assertEqual(actual[0], target.canonical_paper_id)
            # The old year-slice fallback visited all 10,000 same-year rows.
            # Indexed B-tree/FTS candidate generation remains comfortably
            # below 50,000 VM instructions on this fixed regression corpus.
            self.assertLess(progress_calls[0], 500)

if __name__ == "__main__":
    unittest.main()
