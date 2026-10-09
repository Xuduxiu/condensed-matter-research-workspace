from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.analytics.stats import rebuild_paper_terms
from backend.db.database import init_db, upsert_paper
from backend.library.repository import seed_topics_and_materials
from backend.migrations.unified_library import apply_unified_schema


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    return connection


class IncrementalEntityExtractionTests(unittest.TestCase):
    def test_single_id_is_isolated_and_empty_list_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            upsert_paper(
                connection,
                {
                    "id": "paper:one",
                    "title": "Graphene superconductivity studied with Raman spectroscopy",
                    "abstract": "We measure superconductivity in monolayer graphene.",
                    "publication_date": "2026-08-01",
                    "source": "arxiv",
                    "condmat_view_eligible": True,
                },
            )
            upsert_paper(
                connection,
                {
                    "id": "paper:two",
                    "title": "NbSe2 topology measured by scanning tunneling microscopy",
                    "abstract": "A deliberately unprocessed control paper.",
                    "publication_date": "2026-08-02",
                    "source": "crossref",
                    "condmat_view_eligible": True,
                },
            )
            connection.execute(
                """
                INSERT INTO paper_terms
                  (paper_id, term, term_type, normalized_term, confidence,
                   display_eligible, display_reason, source)
                VALUES ('paper:two', 'control sentinel', 'concept',
                        'control sentinel', 1.0, 1, 'test', 'test')
                """
            )

            processed = rebuild_paper_terms(connection, paper_ids=["paper:one"])

            self.assertEqual(processed, 1)
            first_terms = {
                row["normalized_term"]
                for row in connection.execute(
                    "SELECT normalized_term FROM paper_terms WHERE paper_id='paper:one'"
                )
            }
            self.assertIn("graphene", first_terms)
            self.assertIn("Raman spectroscopy", first_terms)
            second_terms = connection.execute(
                "SELECT normalized_term FROM paper_terms WHERE paper_id='paper:two'"
            ).fetchall()
            self.assertEqual([row["normalized_term"] for row in second_terms], ["control sentinel"])

            links = seed_topics_and_materials(connection, paper_ids=["paper:one"])

            self.assertGreater(links["paper_material_links_seen"], 0)
            self.assertGreater(links["paper_topic_links_seen"], 0)
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_materials WHERE canonical_paper_id='paper:one'"
                ).fetchone()[0],
                0,
            )
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_topics WHERE canonical_paper_id='paper:one'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_materials WHERE canonical_paper_id='paper:two'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_topics WHERE canonical_paper_id='paper:two'"
                ).fetchone()[0],
                0,
            )

            counts_before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("paper_terms", "materials", "topics", "paper_materials", "paper_topics")
            }
            with patch("backend.analytics.stats.extract_terms") as extract_terms:
                self.assertEqual(rebuild_paper_terms(connection, paper_ids=[]), 0)
                extract_terms.assert_not_called()
            empty_links = seed_topics_and_materials(connection, paper_ids=[])
            self.assertEqual(empty_links["paper_material_links_seen"], 0)
            self.assertEqual(empty_links["paper_topic_links_seen"], 0)
            counts_after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in counts_before
            }
            self.assertEqual(counts_after, counts_before)
            connection.close()


class BoundedEntityExtractionTests(unittest.TestCase):
    def test_large_rebuild_uses_bounded_fetchmany_and_opt_in_batch_checkpoints(self) -> None:
        rows = [
            {
                "id": f"paper:{index:04d}",
                "title": f"Paper {index}",
                "abstract": "A condensed matter abstract.",
                "condmat_confidence": "high",
            }
            for index in range(1003)
        ]
        events: list[tuple[str, int, int | None]] = []

        class CountCursor:
            def fetchone(self):
                return (len(rows),)

        class StreamingCursor:
            def __init__(self) -> None:
                self.offset = 0
                self.requests: list[int] = []

            def fetchmany(self, size: int):
                self.requests.append(size)
                batch = rows[self.offset:self.offset + size]
                self.offset += len(batch)
                return batch

            def fetchall(self):
                raise AssertionError("full-corpus fetchall must not be used")

        class FakeConnection:
            def __init__(self) -> None:
                self.cursor = StreamingCursor()
                self.commits = 0

            def execute(self, sql: str, _params=None):
                if sql.startswith("SELECT COUNT(*)"):
                    return CountCursor()
                if sql.startswith("SELECT id, title"):
                    return self.cursor
                raise AssertionError(sql)

            def commit(self) -> None:
                self.commits += 1
                events.append(("commit", self.commits, None))

        connection = FakeConnection()

        def on_progress(processed: int, total: int) -> None:
            events.append(("progress", processed, total))

        with (
            patch("backend.analytics.stats.extract_terms", return_value=[]),
            patch("backend.analytics.stats.replace_terms") as replace_terms,
        ):
            processed = rebuild_paper_terms(
                connection,
                batch_size=128,
                progress_callback=on_progress,
                commit_batches=True,
            )

        self.assertEqual(processed, 1003)
        self.assertEqual(replace_terms.call_count, 1003)
        self.assertEqual(connection.cursor.requests, [128] * 9)
        self.assertEqual(connection.commits, 8)
        progress_events = [item for item in events if item[0] == "progress"]
        self.assertEqual(progress_events[-1], ("progress", 1003, 1003))
        self.assertEqual(
            [item[0] for item in events],
            [value for _ in range(8) for value in ("progress", "commit")],
        )

    def test_default_streaming_rebuild_keeps_transaction_owned_by_caller(self) -> None:
        rows = [{"id": "paper:one", "title": "Title", "abstract": "", "condmat_confidence": "high"}]

        class Cursor:
            offset = 0

            def fetchmany(self, _size: int):
                if self.offset:
                    return []
                self.offset = 1
                return rows

        class CountCursor:
            def fetchone(self):
                return (1,)

        class FakeConnection:
            def __init__(self) -> None:
                self.commits = 0

            def execute(self, sql: str, _params=None):
                return CountCursor() if sql.startswith("SELECT COUNT(*)") else Cursor()

            def commit(self) -> None:
                self.commits += 1

        connection = FakeConnection()
        with (
            patch("backend.analytics.stats.extract_terms", return_value=[]),
            patch("backend.analytics.stats.replace_terms"),
        ):
            self.assertEqual(rebuild_paper_terms(connection, batch_size=1), 1)
        self.assertEqual(connection.commits, 0)

    def test_opt_in_batch_commits_are_supported_by_a_live_sqlite_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = connection_for(database)
            for index in range(5):
                upsert_paper(
                    connection,
                    {
                        "id": f"paper:{index}",
                        "title": f"Graphene transport paper {index}",
                        "abstract": "Raman spectroscopy of a condensed matter system.",
                        "publication_date": "2026-08-01",
                        "source": "arxiv",
                        "condmat_view_eligible": True,
                    },
                )
            connection.commit()
            callbacks: list[tuple[int, int]] = []

            processed = rebuild_paper_terms(
                connection,
                batch_size=2,
                progress_callback=lambda current, total: callbacks.append((current, total)),
                commit_batches=True,
            )
            connection.close()

            observer = sqlite3.connect(database)
            try:
                paper_count = observer.execute(
                    "SELECT COUNT(DISTINCT paper_id) FROM paper_terms"
                ).fetchone()[0]
            finally:
                observer.close()

        self.assertEqual(processed, 5)
        self.assertEqual(callbacks, [(2, 5), (4, 5), (5, 5)])
        self.assertEqual(paper_count, 5)

if __name__ == "__main__":
    unittest.main()
