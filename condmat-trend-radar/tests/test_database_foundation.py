from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.db.database import connect
from backend.db.status import database_status


class DatabaseFoundationTests(unittest.TestCase):
    def test_radar_status_is_read_only_and_can_run_full_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "radar.sqlite"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE papers(id TEXT PRIMARY KEY, data_mode TEXT NOT NULL);
                    CREATE TABLE paper_terms(paper_id TEXT);
                    CREATE TABLE term_month_stats(term TEXT);
                    CREATE TABLE monthly_corpus_stats(month TEXT);
                    CREATE TABLE ingest_runs(id INTEGER PRIMARY KEY);
                    CREATE TABLE ingest_checkpoints(id INTEGER PRIMARY KEY);
                    PRAGMA user_version = 7;
                    """
                )
                conn.executemany(
                    "INSERT INTO papers(id, data_mode) VALUES (?, ?)",
                    [("real-1", "real"), ("mock-1", "mock")],
                )
                conn.commit()
            modified_before = db_path.stat().st_mtime_ns

            status = database_status(db_path, full_check=True)

            self.assertTrue(status["healthy"])
            self.assertEqual(status["check_mode"], "full")
            self.assertEqual(status["user_version"], 7)
            self.assertEqual(status["quick_check"], ["ok"])
            self.assertEqual(status["foreign_key_error_count"], 0)
            self.assertEqual(status["counts"]["papers"], 2)
            self.assertEqual(status["counts"]["real_papers"], 1)
            self.assertEqual(status["counts"]["mock_papers"], 1)
            self.assertEqual(db_path.stat().st_mtime_ns, modified_before)

    def test_radar_connection_rolls_back_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "rollback.sqlite"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE records(value TEXT)")
                conn.commit()

            with self.assertRaisesRegex(RuntimeError, "stop"):
                with connect(db_path) as conn:
                    conn.execute("INSERT INTO records(value) VALUES ('must rollback')")
                    raise RuntimeError("stop")

            with closing(sqlite3.connect(db_path)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()