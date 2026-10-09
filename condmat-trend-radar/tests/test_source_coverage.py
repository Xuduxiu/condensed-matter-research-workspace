from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db.database import init_db, upsert_paper
from backend.ingest.source_coverage import (
    run_source_backfill,
    run_source_coverage_audit,
)
from backend.library.repository import LibraryRepository
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import _record_cursor_attempt


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    return connection


class CanonicalIdentityFallbackTests(unittest.TestCase):
    def test_exact_title_author_year_merges_without_external_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            first = repository.upsert_version(
                {
                    "title": "Emergent topology in layered quantum-matter",
                    "authors": ["Alice Smith", "Bo Li"],
                    "publication_date": "2025-10-01",
                },
                source="openalex",
                source_record_id="W-title-only",
            )
            second = repository.upsert_version(
                {
                    "title": "Emergent topology in layered quantum matter",
                    "authors": ["Alice Smith", "Bo Li"],
                    "publication_date": "2025-01-02",
                    "doi": "10.1234/example",
                },
                source="crossref",
                source_record_id="10.1234/example",
            )
            self.assertEqual(first.canonical_paper_id, second.canonical_paper_id)
            self.assertEqual(second.match_rule, "title_author_year")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0], 2)
            connection.close()

    def test_adjacent_year_or_different_first_author_never_auto_merges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            base = repository.upsert_version(
                {
                    "title": "Exact normalized identity needs all three fields",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                },
                source="openalex",
                source_record_id="base",
            )
            adjacent_year = repository.upsert_version(
                {
                    "title": "Exact normalized identity needs all three fields",
                    "authors": ["Alice Smith"],
                    "year": 2026,
                },
                source="crossref",
                source_record_id="adjacent-year",
            )
            other_author = repository.upsert_version(
                {
                    "title": "Exact normalized identity needs all three fields",
                    "authors": ["Bob Jones"],
                    "year": 2025,
                },
                source="manual",
                source_record_id="other-author",
            )
            self.assertNotEqual(base.canonical_paper_id, adjacent_year.canonical_paper_id)
            self.assertNotEqual(base.canonical_paper_id, other_author.canonical_paper_id)
            self.assertTrue(adjacent_year.manual_review_created)
            self.assertTrue(other_author.manual_review_created)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 3)
            connection.close()
    def test_conflicting_dois_with_same_title_are_not_auto_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            first = repository.upsert_version(
                {
                    "title": "Identical title for a collision test",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                    "doi": "10.1000/left",
                },
                source="crossref",
                source_record_id="left",
            )
            second = repository.upsert_version(
                {
                    "title": "Identical title for a collision test",
                    "authors": ["Alice Smith"],
                    "year": 2025,
                    "doi": "10.1000/right",
                },
                source="openalex",
                source_record_id="right",
            )
            self.assertNotEqual(first.canonical_paper_id, second.canonical_paper_id)
            self.assertTrue(second.manual_review_created)
            review = connection.execute(
                "SELECT review_type, status FROM manual_review_items WHERE candidate_paper_id=?",
                (second.canonical_paper_id,),
            ).fetchone()
            self.assertEqual(dict(review), {"review_type": "identity_match", "status": "pending"})
            connection.close()


class CursorCompletenessTests(unittest.TestCase):
    def test_partial_multi_source_run_does_not_advance_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            _record_cursor_attempt(connection, "openalex_arxiv", status="ok", cursor="2026-01-01")
            _record_cursor_attempt(connection, "openalex_arxiv", status="partial", cursor="2026-01-02", error="arxiv failed")
            row = connection.execute(
                "SELECT * FROM source_cursors WHERE source_name='openalex_arxiv'"
            ).fetchone()
            self.assertEqual(row["last_successful_cursor"], "2026-01-01")
            self.assertEqual(row["status"], "partial")
            connection.close()

class SourceCoverageTests(unittest.TestCase):
    def test_audit_recovers_historical_source_and_queues_exact_id_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            upsert_paper(
                connection,
                {
                    "id": "doi:10.5555/history",
                    "title": "Historical Crossref paper",
                    "doi": "10.5555/history",
                    "publication_date": "2025-01-01",
                    "source": "Crossref",
                    "data_mode": "real",
                    "condmat_view_eligible": True,
                },
            )
            report = run_source_coverage_audit(connection, scope="all", queue_missing=True)
            self.assertEqual(report["canonical_count"], 1)
            self.assertEqual(report["source_counts"]["crossref"], 1)
            self.assertEqual(report["missing_source_gaps"]["openalex"], 1)
            self.assertEqual(report["newly_queued"], 1)
            self.assertEqual(report["interpretation"]["completeness_claim"], "lower_bound_only")
            source_version = connection.execute(
                "SELECT source FROM paper_versions WHERE source='crossref'"
            ).fetchone()
            self.assertIsNotNone(source_version)
            connection.close()

    def test_read_only_audit_does_not_seed_versions_or_audit_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            upsert_paper(
                connection,
                {
                    "id": "doi:10.5555/read-only",
                    "title": "Read only coverage audit",
                    "doi": "10.5555/read-only",
                    "publication_date": "2026-01-01",
                    "source": "Crossref",
                    "data_mode": "real",
                    "condmat_view_eligible": True,
                },
            )
            before_versions = connection.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0]
            report = run_source_coverage_audit(
                connection,
                scope="eligible",
                queue_missing=False,
                persist=False,
                seed_historical=False,
            )
            after_versions = connection.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0]
            coverage_tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'source_coverage_%'"
            ).fetchone()[0]
            self.assertEqual(before_versions, after_versions)
            self.assertEqual(coverage_tables, 0)
            self.assertEqual(report["source_counts"], {"crossref": 1})
            self.assertTrue(report["historical_source_seed"]["skipped"])
            connection.close()
    def test_backfill_adds_second_source_and_closes_gap(self) -> None:
        class FakeOpenAlex:
            def __init__(self, **_kwargs):
                pass

            def get_work_by_doi(self, doi: str):
                return {
                    "id": "https://openalex.org/W123",
                    "openalex_id": "https://openalex.org/W123",
                    "doi": doi,
                    "title": "Cross checked paper",
                    "authors": ["A. Researcher"],
                    "publication_date": "2026-01-01",
                    "journal": "Physical Review B",
                    "source": "openalex",
                    "data_mode": "real",
                    "raw_json": {"id": "https://openalex.org/W123"},
                }

        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            LibraryRepository(connection).upsert_version(
                {
                    "title": "Cross checked paper",
                    "authors": ["A. Researcher"],
                    "doi": "10.5555/crosscheck",
                    "publication_date": "2026-01-01",
                },
                source="crossref",
                source_record_id="10.5555/crosscheck",
                default_condmat_eligible=True,
            )
            connection.execute("UPDATE papers SET condmat_view_eligible=1")
            before = run_source_coverage_audit(connection, scope="eligible", queue_missing=True)
            self.assertEqual(before["cross_source_count"], 0)
            connection.commit()
            progress: list[tuple[int, int, int, int, int, int, str]] = []
            visible_statuses: list[str] = []

            def on_progress(
                processed: int,
                total: int,
                completed: int,
                not_found: int,
                failed: int,
                identity_conflicts: int,
                current_source: str,
            ) -> None:
                progress.append((processed, total, completed, not_found, failed, identity_conflicts, current_source))
                observer = sqlite3.connect(Path(directory) / "radar.sqlite")
                try:
                    row = observer.execute(
                        "SELECT status FROM source_backfill_queue ORDER BY created_at LIMIT 1"
                    ).fetchone()
                    visible_statuses.append(str(row[0]) if row else "missing")
                finally:
                    observer.close()

            with patch("backend.ingest.source_coverage.OpenAlexClient", FakeOpenAlex):
                result = run_source_backfill(
                    connection,
                    limit=10,
                    timeout=3,
                    progress_callback=on_progress,
                )
            after = run_source_coverage_audit(connection, scope="eligible", queue_missing=False)
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["requested"], 1)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["progress_callback_errors"], [])
            self.assertEqual(
                progress,
                [
                    (0, 1, 0, 0, 0, 0, ""),
                    (0, 1, 0, 0, 0, 0, "openalex"),
                    (1, 1, 1, 0, 0, 0, "openalex"),
                ],
            )
            self.assertEqual(visible_statuses, ["queued", "running", "completed"])
            self.assertEqual(len(result["canonical_paper_ids"]), 1)
            self.assertTrue(result["results"][0]["metadata_changed"])
            self.assertEqual(after["cross_source_count"], 1)
            self.assertEqual(after["missing_source_gaps"]["openalex"], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(DISTINCT source) FROM paper_versions WHERE source IN ('crossref','openalex')").fetchone()[0],
                2,
            )
            connection.close()

    def test_backfill_progress_counters_cover_not_found_and_failure(self) -> None:
        class MixedOpenAlex:
            def __init__(self, **_kwargs):
                pass

            def get_work_by_doi(self, doi: str):
                if doi.endswith("missing"):
                    return None
                if doi.endswith("failure"):
                    raise TimeoutError("lookup timed out")
                return {
                    "id": "https://openalex.org/W-mixed-success",
                    "openalex_id": "https://openalex.org/W-mixed-success",
                    "doi": doi,
                    "title": "Mixed source progress success",
                    "authors": ["A. Researcher"],
                    "publication_date": "2026-01-01",
                    "journal": "Physical Review B",
                    "source": "openalex",
                    "data_mode": "real",
                    "raw_json": {"id": "https://openalex.org/W-mixed-success"},
                }

        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            for suffix, title in (
                ("success", "Mixed source progress success"),
                ("missing", "Mixed source progress missing"),
                ("failure", "Mixed source progress failure"),
            ):
                doi = f"10.5555/{suffix}"
                repository.upsert_version(
                    {
                        "title": title,
                        "authors": ["A. Researcher"],
                        "doi": doi,
                        "publication_date": "2026-01-01",
                    },
                    source="crossref",
                    source_record_id=doi,
                    default_condmat_eligible=True,
                )
            connection.execute("UPDATE papers SET condmat_view_eligible=1")
            audit = run_source_coverage_audit(connection, scope="eligible", queue_missing=True)
            connection.commit()
            progress: list[tuple[int, int, int, int, int, int, str]] = []

            with patch("backend.ingest.source_coverage.OpenAlexClient", MixedOpenAlex):
                result = run_source_backfill(
                    connection,
                    limit=10,
                    timeout=3,
                    progress_callback=lambda *values: progress.append(values),
                )
            statuses = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM source_backfill_queue GROUP BY status"
                ).fetchall()
            }
            connection.close()

        self.assertEqual(audit["newly_queued"], 3)
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["not_found"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["identity_conflicts"], 0)
        self.assertEqual(progress[-1][:6], (3, 3, 1, 1, 1, 0))
        self.assertEqual(statuses, {"completed": 1, "not_found": 1, "retryable_failed": 1})


if __name__ == "__main__":
    unittest.main()