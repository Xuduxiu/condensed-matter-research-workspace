from __future__ import annotations

import sqlite3
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.workbench_routes import (
    EnrichmentRequest,
    _ABSTRACT_RUNS,
    _ABSTRACT_RUNS_LOCK,
    abstract_enrichment_status,
    enrich_missing_abstracts,
)
from backend.db.database import init_db
from backend.library.metadata_enrichment import (
    _fetch_semantic_scholar,
    backfill_missing_abstracts,
)
from backend.library.repository import LibraryRepository
from backend.migrations.unified_library import apply_unified_schema


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    connection.commit()
    return connection


def add_paper(connection: sqlite3.Connection, index: int) -> None:
    LibraryRepository(connection).upsert_version(
        {
            "title": f"Missing abstract budget {index}",
            "doi": f"10.1000/budget-{index}",
            "publication_date": f"2026-04-{index + 1:02d}",
        },
        source="test",
        source_record_id=f"budget-{index}",
        default_condmat_eligible=True,
    )
    connection.commit()


class FakeBackgroundTasks:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def add_task(self, function: object, *args: object, **_kwargs: object) -> None:
        self.calls.append((function, args))


class AbstractEnrichmentLatencyTests(unittest.TestCase):
    def tearDown(self) -> None:
        with _ABSTRACT_RUNS_LOCK:
            _ABSTRACT_RUNS.clear()

    def test_semantic_scholar_deadline_prevents_retry_sleep(self) -> None:
        deadline = time.monotonic() + 0.1
        with patch(
            "backend.library.metadata_enrichment.urllib.request.urlopen",
            side_effect=ssl.SSLError("transient"),
        ) as urlopen, patch("backend.library.metadata_enrichment.time.sleep") as sleep:
            with self.assertRaises(TimeoutError):
                _fetch_semantic_scholar(
                    "10.1000/deadline",
                    "",
                    5,
                    deadline=deadline,
                )
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_batch_budget_defers_unstarted_papers_without_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            for index in range(3):
                add_paper(connection, index)

            def unavailable(
                _connection: sqlite3.Connection,
                paper_id: str,
                **_kwargs: object,
            ) -> dict[str, object]:
                return {
                    "canonical_paper_id": paper_id,
                    "abstract_available": False,
                    "updated": False,
                    "errors": [],
                }

            with patch(
                "backend.library.metadata_enrichment.enrich_paper_metadata",
                side_effect=unavailable,
            ), patch(
                "backend.library.metadata_enrichment.time.monotonic",
                side_effect=[0.0, 0.0, 0.0, 2.0],
            ):
                result = backfill_missing_abstracts(
                    connection,
                    limit=3,
                    timeout=5,
                    item_timeout_seconds=20,
                    batch_timeout_seconds=1,
                )

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["deferred"], 2)
            self.assertTrue(result["budget_exhausted"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM metadata_enrichment_backoff WHERE state_type='paper'"
                ).fetchone()[0],
                1,
            )
            connection.close()

    def test_direct_api_accepts_without_executing_network_work(self) -> None:
        tasks = FakeBackgroundTasks()
        response = enrich_missing_abstracts(EnrichmentRequest(limit=25), tasks)  # type: ignore[arg-type]
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["limit"], 25)
        self.assertTrue(response["background"])
        self.assertEqual(len(tasks.calls), 1)
        self.assertEqual(tasks.calls[0][1], (response["run_id"], 25))
        status = abstract_enrichment_status(response["run_id"])
        self.assertEqual(status["processed"], 0)


if __name__ == "__main__":
    unittest.main()
