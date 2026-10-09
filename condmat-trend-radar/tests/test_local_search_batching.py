from __future__ import annotations

import sqlite3
import unittest

from backend.db.database import init_db
from backend.library.local_search import (
    index_missing_search_entries,
    refresh_search_entries,
)
from backend.migrations.unified_library import apply_unified_schema


def connection_for_test() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    return connection


def seed_versions(connection: sqlite3.Connection, count: int) -> list[str]:
    paper_rows = [
        (f"paper-{index}", f"Paper title {index}") for index in range(count)
    ]
    version_rows = [
        (
            f"version-{index}",
            f"paper-{index}",
            f"Paper title {index}",
            f"Abstract {index}",
            f"source-{index}",
        )
        for index in range(count)
    ]
    connection.executemany(
        "INSERT INTO papers(id, title, data_mode) VALUES (?, ?, 'real')",
        paper_rows,
    )
    connection.executemany(
        """
        INSERT INTO paper_versions(
            id, canonical_paper_id, version_type, title, abstract, source,
            source_record_id, raw_json, first_seen_at, last_seen_at
        ) VALUES (?, ?, 'metadata', ?, ?, 'fixture', ?, '{}', 'now', 'now')
        """,
        version_rows,
    )
    return [row[0] for row in version_rows]


class BatchedSearchRefreshTests(unittest.TestCase):
    def test_empty_refresh_is_a_true_noop(self) -> None:
        connection = connection_for_test()
        traced: list[str] = []
        connection.set_trace_callback(traced.append)
        result = refresh_search_entries(connection, [])
        connection.set_trace_callback(None)

        self.assertEqual(result, {"refreshed": 0})
        self.assertEqual(traced, [])
        connection.close()

    def test_thousands_of_ids_use_one_fts_delete_and_refresh_content(self) -> None:
        connection = connection_for_test()
        version_ids = seed_versions(connection, 3_500)
        connection.executemany(
            """
            INSERT INTO library_fts(
                canonical_paper_id, paper_version_id, title, abstract, body
            ) VALUES (?, ?, 'Stale title', 'Stale abstract', 'Stale body')
            """,
            ((f"paper-{index}", version_id) for index, version_id in enumerate(version_ids)),
        )
        connection.execute(
            "UPDATE paper_versions SET title='Fresh title', abstract='Fresh abstract' "
            "WHERE id=?",
            (version_ids[0],),
        )
        connection.execute(
            """
            INSERT INTO paper_files(
                id, canonical_paper_id, paper_version_id, absolute_path, sha256,
                file_size, mime_type, extraction_status, validation_status,
                created_at, updated_at
            ) VALUES ('file-0', 'paper-0', ?, 'fixture.pdf', 'sha-0', 42,
                      'application/pdf', 'completed', 'valid', 'now', 'now')
            """,
            (version_ids[0],),
        )
        connection.execute(
            """
            INSERT INTO paper_file_text(
                paper_file_id, extractor, text_content, text_sha256, extracted_at
            ) VALUES ('file-0', 'fixture', 'Fresh extracted body', 'text-sha-0', 'now')
            """
        )

        traced: list[str] = []
        connection.set_trace_callback(traced.append)
        result = refresh_search_entries(
            connection,
            [*version_ids, version_ids[0], "", None],  # type: ignore[list-item]
        )
        connection.set_trace_callback(None)

        fts_deletes = [
            sql
            for sql in traced
            if sql.lstrip().lower().startswith("delete from library_fts")
        ]
        fts_inserts = [
            sql
            for sql in traced
            if sql.lstrip().lower().startswith("insert into library_fts")
        ]
        self.assertEqual(result, {"refreshed": len(version_ids)})
        self.assertEqual(len(fts_deletes), 1)
        self.assertEqual(len(fts_inserts), 1)
        self.assertNotIn("paper_version_id=?", fts_deletes[0].lower())
        delete_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            DELETE FROM library_fts
            WHERE paper_version_id IN (
                SELECT id FROM temp._refresh_search_entry_ids
            )
            """
        ).fetchall()
        delete_details = " | ".join(str(row[3]) for row in delete_plan)
        self.assertEqual(delete_details.count("SCAN library_fts"), 1)
        self.assertIn("FOR IN-OPERATOR", delete_details)

        counts = connection.execute(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT paper_version_id) AS versions
            FROM library_fts
            """
        ).fetchone()
        self.assertEqual((counts["rows"], counts["versions"]), (3_500, 3_500))
        refreshed = connection.execute(
            """
            SELECT title, abstract, body
            FROM library_fts
            WHERE paper_version_id=?
            """,
            (version_ids[0],),
        ).fetchone()
        self.assertEqual(
            (refreshed["title"], refreshed["abstract"], refreshed["body"]),
            ("Fresh title", "Fresh abstract", "Fresh extracted body"),
        )
        connection.close()

    def test_missing_index_snapshots_fts_once_and_does_not_duplicate(self) -> None:
        connection = connection_for_test()
        version_ids = seed_versions(connection, 2_000)
        connection.executemany(
            """
            INSERT INTO library_fts(
                canonical_paper_id, paper_version_id, title, abstract, body
            ) VALUES (?, ?, ?, '', '')
            """,
            (
                (f"paper-{index}", version_id, f"Paper title {index}")
                for index, version_id in enumerate(version_ids[:1_500])
            ),
        )

        traced: list[str] = []
        connection.set_trace_callback(traced.append)
        first = index_missing_search_entries(connection)
        second = index_missing_search_entries(connection)
        connection.set_trace_callback(None)

        trace = "\n".join(traced).lower()
        self.assertEqual(first, {"before": 1_500, "after": 2_000, "indexed": 500})
        self.assertEqual(second, {"before": 2_000, "after": 2_000, "indexed": 0})
        self.assertNotIn("where not exists", trace)
        self.assertIn("left join temp._indexed_search_entry_ids", trace)
        counts = connection.execute(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT paper_version_id) AS versions
            FROM library_fts
            """
        ).fetchone()
        self.assertEqual((counts["rows"], counts["versions"]), (2_000, 2_000))
        connection.close()


if __name__ == "__main__":
    unittest.main()
