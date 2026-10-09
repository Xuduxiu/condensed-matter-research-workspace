from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.db.database import init_db
from backend.ingest.arxiv_client import ArxivClient
from backend.ingest.crossref_client import CrossrefClient
from backend.ingest_real import _cancel_requested, _existing_paper, _store_paper, run
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import DailyOptions, run_daily_update


FIELD_PAPER = {
    "id": "https://openalex.org/W-cancelled-page",
    "openalex_id": "https://openalex.org/W-cancelled-page",
    "doi": "10.5555/cancelled-page",
    "title": "Quantum transport in a correlated layered material",
    "abstract": "A condensed matter superconductivity and transport study.",
    "publication_date": "2026-08-09",
    "journal": "Test Journal",
    "source": "openalex",
    "source_scope": "published",
    "concepts": ["Condensed Matter Physics"],
    "raw_json": {"id": "https://openalex.org/W-cancelled-page"},
}


def connect_to(database: Path):
    @contextmanager
    def test_connect(_path=None):
        connection = sqlite3.connect(database, timeout=2)
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

    return test_connect


def bootstrap(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        init_db(connection)
        apply_unified_schema(connection)
        connection.commit()
    finally:
        connection.close()


class CancellationFlagTests(unittest.TestCase):
    def test_flag_is_scoped_to_daily_run_and_cli_without_run_id_is_unchanged(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE daily_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        connection.execute("INSERT INTO daily_runs VALUES ('run-1', 'cancel_requested')")

        self.assertTrue(
            _cancel_requested(
                connection,
                argparse.Namespace(progress_run_id="run-1", dry_run=False),
            )
        )
        self.assertFalse(_cancel_requested(connection, argparse.Namespace(dry_run=False)))
        self.assertFalse(
            _cancel_requested(
                connection,
                argparse.Namespace(progress_run_id="run-1", dry_run=True),
            )
        )
        connection.close()

    def test_remote_clients_expose_page_boundaries_without_changing_default_calls(self) -> None:
        arxiv_events: list[tuple[str, int]] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b"<feed />"

        arxiv = ArxivClient(timeout=1, polite_delay=0)
        with (
            patch("backend.ingest.arxiv_client.urllib.request.urlopen", return_value=Response()),
            patch("backend.ingest.arxiv_client.parse_arxiv_feed", return_value=[]),
        ):
            papers, failures = arxiv.fetch(
                "2026-08-08",
                "2026-08-09",
                page_hook=lambda phase, page: arxiv_events.append((phase, page)),
            )
        self.assertEqual((papers, failures), ([], 0))
        self.assertEqual(arxiv_events, [("before", 0), ("after", 0)])

        crossref_events: list[tuple[str, int]] = []
        crossref = CrossrefClient(timeout=1, polite_delay=0)
        with patch.object(crossref, "_fetch_journal_page_raw", return_value=([], "", [], 0)):
            pages = list(
                crossref.iterate_crossref_works(
                    object(),
                    "2026-08-08",
                    "2026-08-09",
                    page_hook=lambda phase, page: crossref_events.append((phase, page)),
                )
            )
        self.assertEqual(len(pages), 1)
        self.assertEqual(crossref_events, [("before", 1), ("after", 1)])


class CooperativeIngestCancellationTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            baseline_from="2026-08-08",
            baseline_to="2026-08-09",
            scope="core",
            journals="Physical Review B",
            limit_per_journal=0,
            resume=True,
            force_refresh=False,
            dry_run=False,
            mailto=None,
            include_arxiv=True,
            include_openalex_field=True,
            include_crossref=True,
            crossref_rows=1000,
            max_pages=0,
            sleep_seconds=0,
            timeout=1,
            incremental=True,
            progress_run_id="daily-cancel",
        )

    def test_cancel_after_remote_page_stops_other_sources_and_does_not_checkpoint_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            bootstrap(database)
            connection = sqlite3.connect(database)
            connection.execute(
                """
                INSERT INTO daily_runs(id, status, dry_run, started_at, trigger_type, report_json)
                VALUES ('daily-cancel', 'running', 0, '2026-08-09T00:00:00Z', 'test', '{}')
                """
            )
            connection.commit()
            connection.close()

            class CancellingOpenAlex:
                fetch_calls = 0

                def __init__(self, **_kwargs):
                    pass

                def fetch_condensed_matter_page(self, *_args, **_kwargs):
                    type(self).fetch_calls += 1
                    external = sqlite3.connect(database, timeout=2)
                    try:
                        external.execute(
                            "UPDATE daily_runs SET status='cancel_requested' WHERE id='daily-cancel'"
                        )
                        external.commit()
                    finally:
                        external.close()
                    return {"results": [dict(FIELD_PAPER)], "meta": {"next_cursor": "cursor-2"}}

                def _normalize_work(self, raw, _journal):
                    return dict(raw)

                def find_source_id(self, _journal):
                    raise AssertionError("journal source must not start after cancellation")

            with (
                patch("backend.ingest_real.connect", connect_to(database)),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", CancellingOpenAlex),
                patch("backend.ingest_real.CrossrefClient", side_effect=AssertionError("Crossref must be skipped")),
                patch("backend.ingest_real.ArxivClient", side_effect=AssertionError("arXiv must be skipped")),
            ):
                result = run(self._args())

            connection = sqlite3.connect(database)
            ingest_status = connection.execute(
                "SELECT status FROM ingest_runs WHERE id=?", (result["run_id"],)
            ).fetchone()[0]
            paper_count = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) FROM ingest_checkpoints WHERE source='openalex_field'"
            ).fetchone()[0]
            daily_status = connection.execute(
                "SELECT status FROM daily_runs WHERE id='daily-cancel'"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(CancellingOpenAlex.fetch_calls, 1)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["partial"])
        self.assertIn("openalex_field:after_fetch", result["cancellation"]["boundary"])
        self.assertFalse(result["source_completeness"]["watermark_safe_to_advance"])
        self.assertEqual(ingest_status, "cancelled")
        self.assertEqual(daily_status, "cancel_requested")
        self.assertEqual(paper_count, 0)
        self.assertEqual(checkpoint_count, 0)


    def test_cancelled_arxiv_page_does_not_advance_published_page_or_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            bootstrap(database)
            connection = sqlite3.connect(database)
            connection.execute(
                """
                INSERT INTO daily_runs(id, status, dry_run, started_at, trigger_type, report_json)
                VALUES ('daily-cancel', 'running', 0, '2026-08-09T00:00:00Z', 'test', '{}')
                """
            )
            connection.commit()
            connection.close()

            class FakeOpenAlex:
                def __init__(self, **_kwargs):
                    pass

            class CancellingArxiv:
                def __init__(self, **_kwargs):
                    self.last_error = None
                    self.last_fetched_count = 0

                def fetch(self, *_args, page_hook=None, **_kwargs):
                    page_hook("before", 0)
                    self.last_fetched_count = 50
                    page_hook("after", 0)
                    page_hook("before", 1)
                    external = sqlite3.connect(database, timeout=2)
                    try:
                        external.execute(
                            "UPDATE daily_runs SET status='cancel_requested' WHERE id='daily-cancel'"
                        )
                        external.commit()
                    finally:
                        external.close()
                    self.last_fetched_count = 57
                    page_hook("after", 1)
                    raise AssertionError("cancelled arXiv page must terminate fetch")

            args = self._args()
            args.scope = "arxiv_live"
            args.journals = ""
            args.include_openalex_field = False
            args.include_crossref = False

            with (
                patch("backend.ingest_real.connect", connect_to(database)),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", FakeOpenAlex),
                patch("backend.ingest_real.ArxivClient", CancellingArxiv),
            ):
                result = run(args)

            connection = sqlite3.connect(database)
            report = json.loads(
                connection.execute(
                    "SELECT report_json FROM daily_runs WHERE id='daily-cancel'"
                ).fetchone()[0]
            )
            connection.close()

        self.assertEqual(result["status"], "cancelled")
        self.assertIn("arxiv:after_remote_page:2", result["cancellation"]["boundary"])
        self.assertFalse(result["source_completeness"]["watermark_safe_to_advance"])
        self.assertEqual(report["source"], "arxiv")
        self.assertEqual(report["page"], 1)
        self.assertEqual(report["fetched"], 50)
        self.assertEqual(report["percent"], 39)

    def test_cancel_at_short_commit_boundary_keeps_idempotent_writes_without_advancing_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            bootstrap(database)
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                INSERT INTO daily_runs(id, status, dry_run, started_at, trigger_type, report_json)
                VALUES ('daily-cancel', 'running', 0, '2026-08-09T00:00:00Z', 'test', '{}');
                CREATE TRIGGER request_cancel_after_twenty_five_versions
                AFTER INSERT ON paper_versions
                WHEN (SELECT COUNT(*) FROM paper_versions) = 25
                BEGIN
                    UPDATE daily_runs SET status='cancel_requested' WHERE id='daily-cancel';
                END;
                """
            )
            connection.commit()
            connection.close()

            records = []
            for index in range(30):
                paper = dict(FIELD_PAPER)
                paper["id"] = f"https://openalex.org/W-batch-{index}"
                paper["openalex_id"] = paper["id"]
                paper["doi"] = f"10.5555/cancel-batch-{index}"
                paper["title"] = f"Quantum transport batch record {index}"
                paper["raw_json"] = {"id": paper["id"]}
                records.append(paper)

            class BatchOpenAlex:
                def __init__(self, **_kwargs):
                    pass

                def fetch_condensed_matter_page(self, *_args, **_kwargs):
                    return {"results": [dict(item) for item in records], "meta": {"next_cursor": "cursor-2"}}

                def _normalize_work(self, raw, _journal):
                    return dict(raw)

                def find_source_id(self, _journal):
                    raise AssertionError("later sources must not start after cancellation")

            with (
                patch("backend.ingest_real.connect", connect_to(database)),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", BatchOpenAlex),
                patch("backend.ingest_real.CrossrefClient", side_effect=AssertionError("Crossref must be skipped")),
                patch("backend.ingest_real.ArxivClient", side_effect=AssertionError("arXiv must be skipped")),
            ):
                result = run(self._args())

            connection = sqlite3.connect(database)
            paper_count = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) FROM ingest_checkpoints WHERE source='openalex_field'"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["status"], "cancelled")
        self.assertIn("openalex_field:batch", result["cancellation"]["boundary"])
        self.assertEqual(paper_count, 25)
        self.assertEqual(checkpoint_count, 0)


class DailyCancellationFinalizationTests(unittest.TestCase):
    def test_daily_update_persists_cancelled_and_skips_post_ingest_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap(database)

            def fake_ingest(args: argparse.Namespace):
                external = sqlite3.connect(database, timeout=2)
                try:
                    external.execute(
                        "UPDATE daily_runs SET status='cancel_requested' WHERE id=?",
                        (args.progress_run_id,),
                    )
                    external.commit()
                finally:
                    external.close()
                return {
                    "status": "cancelled",
                    "cancelled": True,
                    "partial": True,
                    "cancellation": {"boundary": "openalex_field:after_fetch:1"},
                    "affected_paper_ids": [],
                    "errors": [],
                }

            with (
                patch("backend.scheduler.daily_update.run_real_ingest", side_effect=fake_ingest),
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
                patch(
                    "backend.scheduler.daily_update.run_source_coverage_audit",
                    side_effect=AssertionError("post-ingest stages must be skipped"),
                ),
            ):
                result = run_daily_update(
                    DailyOptions(
                        dry_run=False,
                        skip_network=False,
                        include_arxiv=True,
                        include_crossref=True,
                        download_limit=0,
                        scan_mode="live",
                        abstract_backfill_limit=0,
                    ),
                    database=database,
                    pdf_root=root / "pdf",
                    trigger_type="test_cancel",
                )

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT status, finished_at, report_json FROM daily_runs WHERE id=?",
                (result["run_id"],),
            ).fetchone()
            connection.close()
            report = json.loads(row[2])

        self.assertEqual(result["status"], "CANCELLED")
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["partial"])
        self.assertEqual(row[0], "cancelled")
        self.assertTrue(row[1])
        self.assertEqual(report["stage"], "cancelled")
        self.assertTrue(report["cancelled"])


class UnchangedSourceFastPathTests(unittest.TestCase):
    def _paper(self, *, eligible: bool = True) -> dict[str, object]:
        return {
            "id": "https://openalex.org/W-fast-path",
            "openalex_id": "https://openalex.org/W-fast-path",
            "doi": "10.5555/fast-path",
            "title": "Fast path for identical source observations",
            "abstract": "A condensed matter transport study.",
            "publication_date": "2026-08-09",
            "journal": "Test Journal",
            "source": "openalex",
            "source_scope": "published",
            "url": "https://doi.org/10.5555/fast-path",
            "raw_json": {"id": "https://openalex.org/W-fast-path", "revision": 1},
            "authors": ["A. Researcher"],
            "condmat_view_eligible": eligible,
            "condmat_view_reason": "test",
        }

    def test_identical_version_returns_before_repository_write(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        init_db(connection)
        apply_unified_schema(connection)
        first_action, canonical_id = _store_paper(connection, self._paper(), dry_run=False)
        observations_before = connection.execute(
            "SELECT COUNT(*) FROM paper_version_observations"
        ).fetchone()[0]

        with patch(
            "backend.ingest_real.LibraryRepository.upsert_version",
            side_effect=AssertionError("identical version must not call repository upsert"),
        ):
            second_action, second_id = _store_paper(connection, self._paper(), dry_run=False)
        observations_after = connection.execute(
            "SELECT COUNT(*) FROM paper_version_observations"
        ).fetchone()[0]
        connection.close()

        self.assertEqual(first_action, "inserted")
        self.assertEqual(second_action, "unchanged")
        self.assertEqual(second_id, canonical_id)
        self.assertEqual(observations_after, observations_before)

    def test_eligibility_upgrade_still_uses_repository_path(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        init_db(connection)
        apply_unified_schema(connection)
        _store_paper(connection, self._paper(eligible=False), dry_run=False)

        original = __import__("backend.ingest_real", fromlist=["LibraryRepository"]).LibraryRepository.upsert_version
        with patch(
            "backend.ingest_real.LibraryRepository.upsert_version",
            autospec=True,
            wraps=original,
        ) as upsert:
            action, _ = _store_paper(connection, self._paper(eligible=True), dry_run=False)
        eligible = connection.execute(
            "SELECT condmat_view_eligible FROM papers WHERE doi='10.5555/fast-path'"
        ).fetchone()[0]
        connection.close()

        self.assertEqual(action, "updated")
        self.assertEqual(upsert.call_count, 1)
        self.assertEqual(eligible, 1)

    def test_existing_doi_lookup_includes_partial_index_predicate(self) -> None:
        class EmptyCursor:
            def fetchone(self):
                return None

        class RecordingConnection:
            def __init__(self):
                self.statements: list[str] = []

            def execute(self, statement, _parameters=()):
                self.statements.append(" ".join(statement.split()))
                return EmptyCursor()

        connection = RecordingConnection()
        _existing_paper(connection, {"doi": "10.5555/indexed"})
        self.assertEqual(len(connection.statements), 1)
        self.assertIn("doi IS NOT NULL AND doi<>''", connection.statements[0])


if __name__ == "__main__":
    unittest.main()