from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.db.database import init_db
from backend.downloader.audit import (
    candidate_attempts_for_task,
    finish_candidate_attempt,
    start_candidate_attempt,
)
from backend.downloader.queue import (
    claim_next_task,
    enqueue_download,
    process_download_task,
    requeue_downloads_after_metadata_change,
)
from backend.downloader.resolver import Resolution
from backend.library.repository import LibraryRepository
from backend.library.workbench import ensure_workbench_schema
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import DailyOptions, run_daily_update


FAILED_STATUSES = ("retryable_failed", "permanent_failed", "manual_review")


def valid_pdf_bytes() -> bytes:
    import fitz

    document = fitz.open()
    try:
        document.new_page().insert_text((72, 72), "Metadata retry audit fixture")
        return document.tobytes()
    finally:
        document.close()


class StreamResponse:
    status_code = 200

    def __init__(self, data: bytes, url: str) -> None:
        self.data = data
        self.url = url
        self.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(data)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        yield self.data


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    ensure_workbench_schema(connection)
    connection.commit()
    return connection


def route_connect(path: Path):
    @contextmanager
    def _connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    return _connect


def add_failed_task(
    connection: sqlite3.Connection,
    index: int,
    *,
    status: str = "permanent_failed",
) -> tuple[str, str]:
    paper = LibraryRepository(connection).upsert_version(
        {
            "title": f"Download retry fixture {index}",
            "doi": f"10.9999/retry-{index}",
            "publication_date": "2026-08-01",
        },
        source="crossref",
        source_record_id=f"10.9999/retry-{index}",
        default_condmat_eligible=True,
    )
    task = enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
    connection.execute(
        """
        UPDATE download_tasks
        SET status=?, attempt_count=5, next_attempt_at='2099-01-01T00:00:00Z',
            last_error='old failure', resolved_url='https://old.example/paper.pdf',
            source='old_source', completed_at='2026-08-01T00:00:00Z'
        WHERE id=?
        """,
        (status, task["id"]),
    )
    return paper.canonical_paper_id, str(task["id"])


class MetadataChangeRequeueTests(unittest.TestCase):
    def test_only_changed_canonicals_are_reset_and_pending_tasks_do_not_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = connection_for(database)
            try:
                changed: list[str] = []
                task_ids: list[str] = []
                for index, status in enumerate(FAILED_STATUSES):
                    paper_id, task_id = add_failed_task(connection, index, status=status)
                    changed.append(paper_id)
                    task_ids.append(task_id)
                untouched_paper, untouched_task = add_failed_task(connection, 99)
                connection.commit()

                result = requeue_downloads_after_metadata_change(
                    connection,
                    [*changed, changed[0]],
                )
                connection.commit()
                self.assertEqual(result["requeued"], 3)
                self.assertEqual(result["previous_statuses"], {
                    "retryable_failed": 1,
                    "permanent_failed": 1,
                    "manual_review": 1,
                })
                rows = connection.execute(
                    "SELECT * FROM download_tasks WHERE id IN (?, ?, ?) ORDER BY id",
                    task_ids,
                ).fetchall()
                self.assertEqual({row["status"] for row in rows}, {"pending"})
                for row in rows:
                    self.assertEqual(row["attempt_count"], 0)
                    self.assertIsNone(row["next_attempt_at"])
                    self.assertIsNone(row["last_error"])
                    self.assertIsNone(row["resolved_url"])
                    self.assertIsNone(row["source"])
                    self.assertIsNone(row["completed_at"])

                # A second call cannot requeue the already pending tasks. More
                # importantly, an empty changed-ID set leaves old failures alone.
                self.assertEqual(
                    requeue_downloads_after_metadata_change(connection, changed)["requeued"],
                    0,
                )
                self.assertEqual(
                    requeue_downloads_after_metadata_change(connection, [])["requeued"],
                    0,
                )
                untouched = connection.execute(
                    "SELECT status, attempt_count FROM download_tasks WHERE id=?",
                    (untouched_task,),
                ).fetchone()
                self.assertEqual(untouched["status"], "permanent_failed")
                self.assertEqual(untouched["attempt_count"], 5)
                self.assertNotIn(untouched_paper, result["canonical_paper_ids"])
            finally:
                connection.close()


    def test_policy_reset_preserves_prior_candidate_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connection_for(root / "radar.sqlite")
            try:
                paper_id, task_id = add_failed_task(connection, 150)
                start_candidate_attempt(
                    connection,
                    task_id=task_id,
                    attempt_number=1,
                    candidate_index=1,
                    source="old_oa_source",
                    reason="old metadata",
                    source_url="https://old.example/paper.pdf",
                )
                finish_candidate_attempt(
                    connection,
                    task_id=task_id,
                    attempt_number=1,
                    candidate_index=1,
                    status="failed",
                    failure_class="not_found",
                )
                connection.commit()

                reset = requeue_downloads_after_metadata_change(connection, [paper_id])
                self.assertEqual(reset["requeued"], 1)
                connection.commit()
                task = claim_next_task(connection)
                self.assertIsNotNone(task)
                candidate = Resolution(
                    "https://new.example/paper.pdf",
                    "new_oa_source",
                    True,
                    "new cross-source OA metadata",
                )
                with (
                    patch(
                        "backend.downloader.queue.resolve_legal_oa_candidates",
                        return_value=[candidate],
                    ),
                    patch(
                        "httpx.stream",
                        return_value=StreamResponse(valid_pdf_bytes(), str(candidate.url)),
                    ),
                ):
                    result = process_download_task(connection, task or {}, root / "pdf")

                self.assertEqual(result["status"], "completed")
                attempts = candidate_attempts_for_task(connection, task_id)
                self.assertEqual(
                    [(item["attempt_number"], item["candidate_source"], item["status"]) for item in attempts],
                    [
                        (1, "old_oa_source", "failed"),
                        (2, "new_oa_source", "completed"),
                    ],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT attempt_count FROM download_tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()


class DailyMetadataRequeueTests(unittest.TestCase):
    def test_daily_report_combines_ingest_and_new_backfill_canonicals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            connection = connection_for(database)
            ingest_paper, ingest_task = add_failed_task(connection, 201)
            backfill_paper, backfill_task = add_failed_task(connection, 202, status="manual_review")
            connection.commit()
            connection.close()

            changed_ids = [ingest_paper]
            backfill_ids = [backfill_paper]

            def fake_ingest(_args: argparse.Namespace) -> dict[str, object]:
                return {
                    "status": "ok",
                    "fetched_count": len(changed_ids),
                    "kept_count": len(changed_ids),
                    "inserted_count": 0,
                    "updated_count": len(changed_ids),
                    "changed_count": len(changed_ids),
                    "deduped_count": 0,
                    "failed_count": 0,
                    "affected_paper_ids": list(changed_ids),
                    "metadata_changed_paper_ids": list(changed_ids),
                    "source_counts": {},
                    "errors": [],
                }

            def fake_backfill(*_args, **_kwargs) -> dict[str, object]:
                return {
                    "requested": len(backfill_ids),
                    "completed": len(backfill_ids),
                    "not_found": 0,
                    "failed": 0,
                    "identity_conflicts": 0,
                    "paper_version_ids": [],
                    "canonical_paper_ids": list(backfill_ids),
                    "results": [],
                }

            options = DailyOptions(
                dry_run=False,
                skip_network=False,
                download_limit=0,
                scan_mode="live",
                abstract_backfill_limit=0,
            )
            with (
                patch("backend.scheduler.daily_update.run_real_ingest", side_effect=fake_ingest),
                patch(
                    "backend.scheduler.daily_update.run_source_coverage_audit",
                    return_value={"status": "ok"},
                ),
                patch("backend.scheduler.daily_update.run_source_backfill", side_effect=fake_backfill),
                patch("backend.scheduler.daily_update.unpaywall_email", return_value="oa-test@example.org"),
                patch(
                    "backend.scheduler.daily_update.run_download_queue",
                    return_value={"processed": 0, "results": []},
                ) as queue_runner,
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
            ):
                first = run_daily_update(
                    options,
                    database=database,
                    pdf_root=root / "pdf",
                    trigger_type="test_download_requeue",
                )
                self.assertEqual(first["report"]["download_requeue"]["requeued"], 2)
                self.assertEqual(
                    set(first["report"]["download_requeue"]["canonical_paper_ids"]),
                    {ingest_paper, backfill_paper},
                )
                queue_runner.assert_called()
                self.assertEqual(
                    queue_runner.call_args.kwargs["unpaywall_email"],
                    "oa-test@example.org",
                )

                connection = connection_for(database)
                connection.execute(
                    """
                    UPDATE download_tasks
                    SET status='permanent_failed', attempt_count=4,
                        next_attempt_at='2099-01-01T00:00:00Z'
                    WHERE id IN (?, ?)
                    """,
                    (ingest_task, backfill_task),
                )
                connection.commit()
                connection.close()
                changed_ids.clear()
                backfill_ids.clear()

                second = run_daily_update(
                    options,
                    database=database,
                    pdf_root=root / "pdf",
                    trigger_type="test_download_requeue_no_change",
                )

            self.assertEqual(second["report"]["download_requeue"]["requeued"], 0)
            connection = connection_for(database)
            rows = connection.execute(
                "SELECT status, attempt_count FROM download_tasks WHERE id IN (?, ?)",
                (ingest_task, backfill_task),
            ).fetchall()
            connection.close()
            self.assertEqual({row["status"] for row in rows}, {"permanent_failed"})
            self.assertEqual({row["attempt_count"] for row in rows}, {4})


class BulkRetryApiTests(unittest.TestCase):
    def test_endpoint_defaults_to_25_and_rejects_limits_above_100(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = connection_for(database)
            for index in range(30):
                add_failed_task(connection, 1000 + index, status=FAILED_STATUSES[index % 3])
            connection.commit()
            connection.close()

            with (
                patch("backend.api.workbench_routes.connect", route_connect(database)),
                TestClient(create_app()) as client,
            ):
                invalid = client.post("/api/library/downloads/retry-failed", json={"limit": 101})
                response = client.post("/api/library/downloads/retry-failed", json={})

            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["requested_limit"], 25)
            self.assertEqual(payload["requeued"], 25)
            self.assertEqual(len(payload["task_ids"]), 25)

            connection = connection_for(database)
            status_counts = dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM download_tasks GROUP BY status"
                ).fetchall()
            )
            action_count = connection.execute(
                "SELECT COUNT(*) FROM user_action_log WHERE action='download_bulk_retried'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(status_counts["pending"], 25)
            self.assertEqual(sum(status_counts.get(status, 0) for status in FAILED_STATUSES), 5)
            self.assertEqual(action_count, 25)


if __name__ == "__main__":
    unittest.main()