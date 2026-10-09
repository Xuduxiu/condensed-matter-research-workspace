from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from backend.db.database import SCHEMA_PATH, upsert_paper


DOI_LOOKUP_SQL = (
    "SELECT id FROM papers "
    "WHERE doi = ? AND doi IS NOT NULL AND doi != ''"
)


class DoiPartialIndexLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.connection.close()

    def test_doi_lookup_searches_partial_index_without_table_scan(self) -> None:
        plan = self.connection.execute(
            f"EXPLAIN QUERY PLAN {DOI_LOOKUP_SQL}",
            ("10.1000/indexed",),
        ).fetchall()
        details = " | ".join(str(row[3]) for row in plan)

        self.assertIn("SEARCH papers USING INDEX idx_papers_doi", details)
        self.assertNotIn("SCAN papers", details)

    def test_upsert_matches_normalized_doi_and_updates_existing_row(self) -> None:
        inserted = upsert_paper(
            self.connection,
            {
                "id": "first-record",
                "doi": "https://doi.org/10.1000/Indexed",
                "title": "Initial title",
                "publication_date": "2026-01-01",
                "source": "crossref",
            },
        )
        traced: list[str] = []
        self.connection.set_trace_callback(traced.append)
        updated = upsert_paper(
            self.connection,
            {
                "id": "duplicate-source-record",
                "doi": "doi:10.1000/indexed",
                "title": "Enriched title",
                "publication_date": "2026-01-02",
                "source": "openalex",
            },
        )
        self.connection.set_trace_callback(None)

        rows = self.connection.execute(
            "SELECT id, doi, title FROM papers"
        ).fetchall()
        self.assertEqual(inserted, "inserted")
        self.assertEqual(updated, "updated")
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0]), {
            "id": "first-record",
            "doi": "10.1000/indexed",
            "title": "Enriched title",
        })
        self.assertTrue(
            any(
                "WHERE doi = '10.1000/indexed' "
                "AND doi IS NOT NULL AND doi != ''" in statement
                for statement in traced
            )
        )

    def test_all_historical_upsert_definitions_include_partial_predicate(self) -> None:
        source = Path(__file__).parents[1].joinpath(
            "backend", "db", "database.py"
        ).read_text(encoding="utf-8")
        lookup_lines = [
            line.strip()
            for line in source.splitlines()
            if '"WHERE doi = ?' in line
        ]

        # database.py retains three compatibility-era upsert definitions. Keep
        # every one safe in case a future cleanup changes which definition wins.
        self.assertEqual(len(lookup_lines), 3)
        self.assertTrue(
            all(
                line == '"WHERE doi = ? AND doi IS NOT NULL AND doi != \'\'",'
                for line in lookup_lines
            )
        )


if __name__ == "__main__":
    unittest.main()