from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.api.analytics_cache import (
    analytics_database_revision,
    cached_analytics_payload,
    clear_analytics_cache,
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, value TEXT)")
    connection.commit()
    return connection


class AnalyticsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_analytics_cache()

    def tearDown(self) -> None:
        clear_analytics_cache()

    def test_repeated_revision_uses_cached_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = _connect(Path(directory) / "analytics.sqlite")
            calls = 0

            def builder() -> dict[str, int]:
                nonlocal calls
                calls += 1
                return {"calls": calls}

            revision = analytics_database_revision(connection)
            first = cached_analytics_payload(days=90, revision=revision, builder=builder)
            second = cached_analytics_payload(days=90, revision=revision, builder=builder)
            connection.close()

        self.assertEqual(first, {"calls": 1})
        self.assertEqual(second, {"calls": 1})
        self.assertEqual(calls, 1)

    def test_committed_wal_write_invalidates_cache_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "analytics.sqlite"
            reader = _connect(database)
            first_revision = analytics_database_revision(reader)
            first = cached_analytics_payload(
                days=90,
                revision=first_revision,
                builder=lambda: {"count": reader.execute("SELECT COUNT(*) FROM records").fetchone()[0]},
            )

            writer = sqlite3.connect(database)
            writer.execute("INSERT INTO records(value) VALUES ('new')")
            writer.commit()
            writer.close()

            second_revision = analytics_database_revision(reader)
            second = cached_analytics_payload(
                days=90,
                revision=second_revision,
                builder=lambda: {"count": reader.execute("SELECT COUNT(*) FROM records").fetchone()[0]},
            )
            reader.close()

        self.assertNotEqual(first_revision, second_revision)
        self.assertEqual(first, {"count": 0})
        self.assertEqual(second, {"count": 1})

    def test_single_flight_builds_once_for_concurrent_callers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = _connect(Path(directory) / "analytics.sqlite")
            revision = analytics_database_revision(connection)
            connection.close()
        started = threading.Event()
        release = threading.Event()
        call_count = 0
        call_lock = threading.Lock()

        def builder() -> dict[str, bool]:
            nonlocal call_count
            with call_lock:
                call_count += 1
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return {"ready": True}

        with ThreadPoolExecutor(max_workers=4) as executor:
            first = executor.submit(cached_analytics_payload, days=90, revision=revision, builder=builder)
            self.assertTrue(started.wait(timeout=5))
            others = [
                executor.submit(cached_analytics_payload, days=90, revision=revision, builder=builder)
                for _ in range(3)
            ]
            release.set()
            results = [first.result(timeout=5), *(item.result(timeout=5) for item in others)]

        self.assertEqual(call_count, 1)
        self.assertEqual(results, [{"ready": True}] * 4)


if __name__ == "__main__":
    unittest.main()
