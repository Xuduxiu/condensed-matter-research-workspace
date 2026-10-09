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
from backend.ingest_real import _journal_progress_percent, _publish_source_progress, run
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import DailyOptions, run_daily_update


class SourceProgressSnapshotTests(unittest.TestCase):
    def test_snapshot_merges_existing_report_and_cli_without_run_id_is_noop(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE daily_runs (id TEXT PRIMARY KEY, report_json TEXT)")
        connection.execute(
            "INSERT INTO daily_runs(id, report_json) VALUES (?, ?)",
            (
                "daily-1",
                json.dumps(
                    {
                        "stage": "fetching_sources",
                        "percent": 10,
                        "scan_mode": "live",
                        "date_from": "2026-08-08",
                    }
                ),
            ),
        )

        changed = _publish_source_progress(
            connection,
            argparse.Namespace(progress_run_id="daily-1", dry_run=False),
            source="crossref",
            source_label="Crossref 期刊：Physical Review B",
            page=3,
            fetched=321,
            inserted=17,
            updated=5,
            deduped=12,
            eligible=29,
            review_candidates=3,
            percent=34,
        )
        report = json.loads(
            connection.execute(
                "SELECT report_json FROM daily_runs WHERE id='daily-1'"
            ).fetchone()[0]
        )

        self.assertTrue(changed)
        self.assertEqual(report["stage"], "fetching_sources")
        self.assertEqual(report["percent"], 34)
        self.assertEqual(report["source"], "crossref")
        self.assertEqual(report["source_label"], "Crossref 期刊：Physical Review B")
        self.assertEqual(report["page"], 3)
        self.assertEqual(report["fetched"], 321)
        self.assertEqual(report["eligible"], 29)
        self.assertEqual(report["review_candidates"], 3)
        self.assertEqual(report["inserted"], 17)
        self.assertEqual(report["updated"], 5)
        self.assertEqual(report["deduped"], 12)
        self.assertEqual(report["counts"], {
            "fetched": 321,
            "eligible": 29,
            "review_candidates": 3,
            "inserted": 17,
            "updated": 5,
            "deduped": 12,
        })
        self.assertEqual(report["kept"], 17)
        self.assertEqual(report["kept_semantics"], "legacy_alias_of_inserted")
        self.assertIn("legacy alias of inserted", report["count_semantics"]["kept"])
        self.assertEqual(report["scan_mode"], "live")
        self.assertEqual(report["date_from"], "2026-08-08")
        self.assertTrue(report["updated_at"])

        before = connection.execute(
            "SELECT report_json FROM daily_runs WHERE id='daily-1'"
        ).fetchone()[0]
        self.assertFalse(
            _publish_source_progress(
                connection,
                argparse.Namespace(dry_run=False),
                source="arxiv",
                source_label="arXiv cond-mat 全分类",
                page=1,
                fetched=999,
                kept=999,
                percent=40,
            )
        )
        after = connection.execute(
            "SELECT report_json FROM daily_runs WHERE id='daily-1'"
        ).fetchone()[0]
        self.assertEqual(after, before)
        connection.close()

    def test_unknown_page_totals_still_produce_monotonic_bounded_progress(self) -> None:
        values = [
            _journal_progress_percent(
                base=29,
                span=9,
                journal_index=journal_index,
                journal_count=3,
                page=page,
            )
            for journal_index, page in [(0, 0), (0, 1), (0, 5), (1, 0), (1, 3), (2, 0), (2, 5)]
        ]
        self.assertEqual(values, sorted(values))
        self.assertGreaterEqual(min(values), 29)
        self.assertLessEqual(max(values), 38)


class ArxivPageProgressIntegrationTests(unittest.TestCase):
    def test_completed_remote_pages_publish_real_page_and_cumulative_fetch_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            bootstrap = sqlite3.connect(database)
            bootstrap.row_factory = sqlite3.Row
            try:
                init_db(bootstrap)
                apply_unified_schema(bootstrap)
                bootstrap.execute(
                    """
                    INSERT INTO daily_runs(id, status, dry_run, started_at, trigger_type, report_json)
                    VALUES ('daily-arxiv', 'running', 0, '2026-08-09T00:00:00Z', 'test', '{}')
                    """
                )
                bootstrap.commit()
            finally:
                bootstrap.close()

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

            snapshots: list[dict[str, object]] = []

            class FakeOpenAlex:
                def __init__(self, **_kwargs):
                    pass

            class FakeArxiv:
                def __init__(self, **_kwargs):
                    self.last_error = None
                    self.last_fetched_count = 0

                def fetch(self, *_args, page_hook=None, **_kwargs):
                    for page, cumulative in ((0, 50), (1, 57)):
                        page_hook("before", page)
                        self.last_fetched_count = cumulative
                        page_hook("after", page)
                        observer = sqlite3.connect(database)
                        try:
                            raw = observer.execute(
                                "SELECT report_json FROM daily_runs WHERE id='daily-arxiv'"
                            ).fetchone()[0]
                            snapshots.append(json.loads(raw))
                        finally:
                            observer.close()
                    return [], 0

            args = argparse.Namespace(
                baseline_from="2026-08-08",
                baseline_to="2026-08-09",
                scope="arxiv_live",
                journals="",
                limit_per_journal=50,
                resume=True,
                force_refresh=False,
                dry_run=False,
                mailto=None,
                include_arxiv=True,
                include_openalex_field=False,
                include_crossref=False,
                max_pages=0,
                sleep_seconds=0,
                timeout=1,
                incremental=True,
                progress_run_id="daily-arxiv",
            )
            with (
                patch("backend.ingest_real.connect", test_connect),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", FakeOpenAlex),
                patch("backend.ingest_real.ArxivClient", FakeArxiv),
            ):
                result = run(args)

            observer = sqlite3.connect(database)
            try:
                final_report = json.loads(
                    observer.execute(
                        "SELECT report_json FROM daily_runs WHERE id='daily-arxiv'"
                    ).fetchone()[0]
                )
            finally:
                observer.close()

        self.assertEqual([(item["page"], item["fetched"]) for item in snapshots], [(1, 50), (2, 57)])
        self.assertTrue(all(item["source"] == "arxiv" and item["percent"] == 39 for item in snapshots))
        self.assertEqual(result["fetched_count"], 57)
        self.assertEqual(final_report["page"], 2)
        self.assertEqual(final_report["fetched"], 57)
        self.assertEqual(final_report["eligible"], 0)
        self.assertEqual(final_report["review_candidates"], 0)
        self.assertEqual(final_report["inserted"], 0)
        self.assertEqual(final_report["updated"], 0)
        self.assertEqual(final_report["deduped"], 0)
        self.assertEqual(final_report["kept"], 0)
        self.assertEqual(final_report["kept_semantics"], "legacy_alias_of_inserted")
        self.assertEqual(final_report["percent"], 40)

class FullPostprocessProgressIntegrationTests(unittest.TestCase):
    @staticmethod
    def _args() -> argparse.Namespace:
        return argparse.Namespace(
            baseline_from="2026-01-01",
            baseline_to="2026-08-09",
            scope="arxiv_live",
            journals="",
            limit_per_journal=0,
            resume=True,
            force_refresh=False,
            dry_run=False,
            mailto=None,
            include_arxiv=False,
            include_openalex_field=False,
            include_crossref=False,
            crossref_rows=1000,
            max_pages=0,
            sleep_seconds=0,
            timeout=1,
            incremental=False,
            progress_run_id="daily-full",
        )

    @staticmethod
    def _bootstrap(database: Path) -> None:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            init_db(connection)
            apply_unified_schema(connection)
            connection.execute(
                """
                INSERT INTO daily_runs(id, status, dry_run, started_at, trigger_type, report_json)
                VALUES (?, 'running', 0, '2026-08-09T00:00:00Z', 'test', ?)
                """,
                (
                    "daily-full",
                    json.dumps(
                        {
                            "stage": "fetching_sources",
                            "percent": 40,
                            "scan_mode": "full",
                            "date_from": "2026-01-01",
                            "date_to": "2026-08-09",
                            "counts": {"fetched": 7},
                        }
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    @contextmanager
    def _connect(database: Path):
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

    @staticmethod
    def _report(database: Path) -> dict[str, object]:
        connection = sqlite3.connect(database, timeout=2)
        try:
            raw = connection.execute(
                "SELECT report_json FROM daily_runs WHERE id='daily-full'"
            ).fetchone()[0]
            return json.loads(raw)
        finally:
            connection.close()

    def test_full_rebuild_publishes_all_stage_starts_to_a_second_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            self._bootstrap(database)
            snapshots: list[tuple[str, str, tuple[str, ...]]] = []

            def stage_result(stage: str, value):
                def run_stage(*_args, **_kwargs):
                    report = self._report(database)
                    postprocess = report["postprocess"]
                    snapshots.append(
                        (
                            str(report["stage"]),
                            str(postprocess["current_status"]),
                            tuple(postprocess["completed_stages"]),
                        )
                    )
                    self.assertEqual(report["stage"], stage)
                    self.assertEqual(postprocess["current_status"], "running")
                    return value

                return run_stage

            class FakeOpenAlex:
                def __init__(self, **_kwargs):
                    pass

            @contextmanager
            def test_connect(_path=None):
                with self._connect(database) as connection:
                    yield connection

            with (
                patch("backend.ingest_real.connect", test_connect),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", FakeOpenAlex),
                patch(
                    "backend.ingest_real.reclassify_openalex_repository_quality",
                    side_effect=stage_result(
                        "quality_reclassification",
                        {"processed_canonicals": 0, "repository_candidates": 0, "downgraded": 0},
                    ),
                ),
                patch("backend.ingest_real.rebuild_paper_terms", side_effect=stage_result("entity_extraction", 7)),
                patch("backend.ingest_real.rebuild_term_month_stats", side_effect=stage_result("term_statistics", 11)),
                patch("backend.ingest_real.rebuild_lifecycle", side_effect=stage_result("lifecycle_analysis", 3)),
                patch(
                    "backend.ingest_real.cooccurrence_network",
                    side_effect=stage_result("cooccurrence_network", {"nodes": [{"id": "x"}], "links": []}),
                ),
            ):
                result = run(self._args())

            final_report = self._report(database)

        expected = [
            "quality_reclassification",
            "entity_extraction",
            "term_statistics",
            "lifecycle_analysis",
            "cooccurrence_network",
        ]
        self.assertEqual([item[0] for item in snapshots], expected)
        self.assertTrue(all(item[1] == "running" for item in snapshots))
        self.assertEqual(snapshots[-1][2], tuple(expected[:-1]))
        self.assertEqual(final_report["stage"], "cooccurrence_network")
        self.assertEqual(final_report["postprocess"]["current_status"], "completed")
        self.assertEqual(final_report["postprocess"]["completed_stages"], expected)
        self.assertEqual(final_report["counts"], {"fetched": 7})
        self.assertEqual(final_report["scan_mode"], "full")
        self.assertEqual(result["analytics_mode"], "full_rebuild")
        self.assertEqual(result["postprocess"]["entity_extraction"]["processed_papers"], 7)

    def test_failed_destructive_stage_rolls_back_without_false_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            self._bootstrap(database)
            setup = sqlite3.connect(database)
            try:
                setup.execute("CREATE TABLE stage_sentinel(value TEXT NOT NULL)")
                setup.execute("INSERT INTO stage_sentinel(value) VALUES ('preserved')")
                setup.commit()
            finally:
                setup.close()

            class FakeOpenAlex:
                def __init__(self, **_kwargs):
                    pass

            def fail_term_stats(connection: sqlite3.Connection) -> int:
                visible = self._report(database)
                self.assertEqual(visible["stage"], "term_statistics")
                self.assertEqual(visible["postprocess"]["current_status"], "running")
                connection.execute("DELETE FROM stage_sentinel")
                raise RuntimeError("term statistics failed")

            @contextmanager
            def test_connect(_path=None):
                with self._connect(database) as connection:
                    yield connection

            with (
                patch("backend.ingest_real.connect", test_connect),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", FakeOpenAlex),
                patch(
                    "backend.ingest_real.reclassify_openalex_repository_quality",
                    return_value={"processed_canonicals": 0, "repository_candidates": 0, "downgraded": 0},
                ),
                patch("backend.ingest_real.rebuild_paper_terms", return_value=0),
                patch("backend.ingest_real.rebuild_term_month_stats", side_effect=fail_term_stats),
            ):
                with self.assertRaisesRegex(RuntimeError, "term statistics failed"):
                    run(self._args())

            observer = sqlite3.connect(database)
            try:
                sentinel = observer.execute("SELECT value FROM stage_sentinel").fetchone()[0]
            finally:
                observer.close()
            report = self._report(database)

        self.assertEqual(sentinel, "preserved")
        self.assertEqual(report["stage"], "term_statistics")
        self.assertEqual(report["postprocess"]["current_status"], "running")
        self.assertEqual(
            report["postprocess"]["completed_stages"],
            ["quality_reclassification", "entity_extraction"],
        )
        self.assertNotIn("completed_at", report["postprocess"]["stages"]["term_statistics"])
class DailyProgressPropagationTests(unittest.TestCase):
    def test_daily_scheduler_passes_own_run_id_to_real_ingest(self) -> None:
        captured: dict[str, object] = {}

        def fake_ingest(args: argparse.Namespace) -> dict[str, object]:
            captured["progress_run_id"] = getattr(args, "progress_run_id", None)
            return {
                "status": "ok",
                "fetched_count": 0,
                "kept_count": 0,
                "inserted_count": 0,
                "updated_count": 0,
                "changed_count": 0,
                "deduped_count": 0,
                "failed_count": 0,
                "affected_paper_ids": [],
                "source_counts": {},
                "errors": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap = sqlite3.connect(database)
            bootstrap.row_factory = sqlite3.Row
            try:
                init_db(bootstrap)
            finally:
                bootstrap.close()
            with (
                patch("backend.scheduler.daily_update.run_real_ingest", side_effect=fake_ingest),
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
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
                    trigger_type="test_progress",
                )

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(captured["progress_run_id"])
        self.assertEqual(captured["progress_run_id"], result["run_id"])

    def test_full_local_pipeline_stages_are_visible_and_persist_completed_results(self) -> None:
        from backend.scheduler import daily_update as daily_module

        snapshots: list[tuple[str, str]] = []
        actual_seed = daily_module.seed_radar_versions
        actual_link = daily_module.link_preprints_and_publications
        actual_search = daily_module.rebuild_search_index
        actual_topics = daily_module.seed_topics_and_materials
        actual_materials = daily_module.sync_materials_from_papers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap = sqlite3.connect(database)
            bootstrap.row_factory = sqlite3.Row
            try:
                init_db(bootstrap)
            finally:
                bootstrap.close()

            def observe(expected_stage: str) -> None:
                observer = sqlite3.connect(database)
                try:
                    raw = observer.execute(
                        "SELECT report_json FROM daily_runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()[0]
                finally:
                    observer.close()
                report = json.loads(raw)
                snapshots.append((str(report["stage"]), str(report["stage_status"])))
                self.assertEqual(report["stage"], expected_stage)
                self.assertEqual(report["stage_status"], "running")

            def seed_wrapper(connection: sqlite3.Connection, **kwargs):
                observe("seeding_library_versions")
                return actual_seed(connection, **kwargs)

            def link_wrapper(connection: sqlite3.Connection, **kwargs):
                observe("linking_preprint_publications")
                return actual_link(connection, **kwargs)

            def search_wrapper(connection: sqlite3.Connection):
                observe("rebuilding_search_index")
                return actual_search(connection)

            def topics_wrapper(connection: sqlite3.Connection, **kwargs):
                observe("syncing_topics_materials")
                return actual_topics(connection, **kwargs)

            def materials_wrapper(connection: sqlite3.Connection, **kwargs):
                return actual_materials(connection, **kwargs)

            with (
                patch("backend.scheduler.daily_update.run_source_coverage_audit", return_value={"status": "ok"}),
                patch("backend.scheduler.daily_update.seed_radar_versions", side_effect=seed_wrapper),
                patch("backend.scheduler.daily_update.link_preprints_and_publications", side_effect=link_wrapper),
                patch("backend.scheduler.daily_update.rebuild_search_index", side_effect=search_wrapper),
                patch("backend.scheduler.daily_update.seed_topics_and_materials", side_effect=topics_wrapper),
                patch("backend.scheduler.daily_update.sync_materials_from_papers", side_effect=materials_wrapper),
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
            ):
                result = run_daily_update(
                    DailyOptions(
                        dry_run=False,
                        skip_network=True,
                        download_limit=0,
                        scan_mode="full",
                        abstract_backfill_limit=0,
                    ),
                    database=database,
                    pdf_root=root / "pdf",
                    trigger_type="test_full_local_pipeline_progress",
                )

        expected = [
            "seeding_library_versions",
            "linking_preprint_publications",
            "rebuilding_search_index",
            "syncing_topics_materials",
        ]
        self.assertEqual([item[0] for item in snapshots], expected)
        self.assertTrue(all(item[1] == "running" for item in snapshots))
        pipeline = result["report"]["pipeline_stages"]
        self.assertEqual([pipeline[stage]["status"] for stage in expected], ["completed"] * 4)
        self.assertEqual(
            pipeline["rebuilding_search_index"]["result"]["transaction_mode"],
            "atomic_full_rebuild",
        )
        self.assertEqual(
            pipeline["syncing_topics_materials"]["result"]["transaction_mode"],
            "atomic_stage",
        )
    def test_failed_full_search_rebuild_rolls_back_and_records_failed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap = sqlite3.connect(database)
            bootstrap.row_factory = sqlite3.Row
            try:
                init_db(bootstrap)
                apply_unified_schema(bootstrap)
                bootstrap.execute(
                    """
                    INSERT INTO library_fts(canonical_paper_id, paper_version_id, title, abstract, body)
                    VALUES ('sentinel-paper', 'sentinel-version', 'PreservedSearchToken', '', '')
                    """
                )
                bootstrap.commit()
            finally:
                bootstrap.close()

            def fail_search(connection: sqlite3.Connection):
                observer = sqlite3.connect(database)
                try:
                    raw = observer.execute(
                        "SELECT report_json FROM daily_runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()[0]
                finally:
                    observer.close()
                visible = json.loads(raw)
                self.assertEqual(visible["stage"], "rebuilding_search_index")
                self.assertEqual(visible["stage_status"], "running")
                connection.execute("DELETE FROM library_fts")
                raise RuntimeError("search rebuild failed")

            with (
                patch("backend.scheduler.daily_update.run_source_coverage_audit", return_value={"status": "ok"}),
                patch("backend.scheduler.daily_update.rebuild_search_index", side_effect=fail_search),
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
            ):
                with self.assertRaisesRegex(RuntimeError, "search rebuild failed"):
                    run_daily_update(
                        DailyOptions(
                            dry_run=False,
                            skip_network=True,
                            download_limit=0,
                            scan_mode="full",
                            abstract_backfill_limit=0,
                        ),
                        database=database,
                        pdf_root=root / "pdf",
                        trigger_type="test_failed_search_progress",
                    )

            observer = sqlite3.connect(database)
            try:
                sentinel = observer.execute(
                    "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'PreservedSearchToken'"
                ).fetchone()[0]
                status, raw = observer.execute(
                    "SELECT status, report_json FROM daily_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            finally:
                observer.close()
            report = json.loads(raw)

        self.assertEqual(sentinel, 1)
        self.assertEqual(status, "failed")
        self.assertEqual(report["failed_stage"], "rebuilding_search_index")
        search_stage = report["pipeline_stages"]["rebuilding_search_index"]
        self.assertEqual(search_stage["status"], "running")
        self.assertNotIn("completed_at", search_stage)
        self.assertEqual(
            report["pipeline_stages"]["linking_preprint_publications"]["status"],
            "completed",
        )
    def test_source_backfill_progress_is_committed_for_pollers(self) -> None:
        snapshots: list[dict[str, object]] = []
        captured: dict[str, object] = {}

        def fake_ingest(_args: argparse.Namespace) -> dict[str, object]:
            return {
                "status": "ok",
                "fetched_count": 0,
                "kept_count": 0,
                "inserted_count": 0,
                "updated_count": 0,
                "changed_count": 0,
                "deduped_count": 0,
                "failed_count": 0,
                "affected_paper_ids": [],
                "metadata_changed_paper_ids": [],
                "source_counts": {},
                "errors": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap = sqlite3.connect(database)
            bootstrap.row_factory = sqlite3.Row
            try:
                init_db(bootstrap)
            finally:
                bootstrap.close()

            def fake_backfill(
                _connection: sqlite3.Connection,
                *,
                limit: int,
                timeout: int,
                progress_callback,
            ) -> dict[str, object]:
                captured["limit"] = limit
                captured["timeout"] = timeout
                updates = [
                    (0, 2, 0, 0, 0, 0, ""),
                    (0, 2, 0, 0, 0, 0, "openalex"),
                    (1, 2, 1, 0, 0, 0, "openalex"),
                    (1, 2, 1, 0, 0, 0, "crossref"),
                    (2, 2, 1, 1, 0, 0, "crossref"),
                ]
                for update in updates:
                    progress_callback(*update)
                    observer = sqlite3.connect(database)
                    try:
                        raw = observer.execute(
                            "SELECT report_json FROM daily_runs ORDER BY started_at DESC LIMIT 1"
                        ).fetchone()[0]
                        snapshots.append(json.loads(raw))
                    finally:
                        observer.close()
                return {
                    "requested": 2,
                    "total": 2,
                    "processed": 2,
                    "completed": 1,
                    "not_found": 1,
                    "failed": 0,
                    "identity_conflicts": 0,
                    "paper_version_ids": [],
                    "canonical_paper_ids": [],
                    "results": [],
                    "progress_callback_errors": [],
                }

            with (
                patch("backend.scheduler.daily_update.run_real_ingest", side_effect=fake_ingest),
                patch("backend.scheduler.daily_update.run_source_coverage_audit", return_value={"status": "ok"}),
                patch("backend.scheduler.daily_update.run_source_backfill", side_effect=fake_backfill),
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
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
                    trigger_type="test_backfill_progress",
                )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(captured, {"limit": 12, "timeout": 8})
        self.assertEqual([item["stage"] for item in snapshots], ["backfilling_missing_sources"] * 5)
        self.assertEqual([item["percent"] for item in snapshots], [52, 52, 54, 54, 57])
        self.assertEqual(
            [item["source_backfill"]["processed"] for item in snapshots],
            [0, 0, 1, 1, 2],
        )
        self.assertEqual(
            [item["source_backfill"]["queue_remaining"] for item in snapshots],
            [2, 2, 1, 1, 0],
        )
        self.assertEqual(
            [item["source_backfill"]["current_source"] for item in snapshots],
            ["", "openalex", "openalex", "crossref", "crossref"],
        )
        self.assertTrue(all(item["source_coverage"] == {"status": "ok"} for item in snapshots))
        self.assertEqual(result["report"]["source_backfill"]["processed"], 2)

    def test_abstract_progress_is_committed_for_pollers(self) -> None:
        snapshots: list[dict[str, object]] = []

        captured: dict[str, object] = {}

        def fake_ingest(_args: argparse.Namespace) -> dict[str, object]:
            return {
                "status": "ok",
                "fetched_count": 0,
                "kept_count": 0,
                "inserted_count": 0,
                "updated_count": 0,
                "changed_count": 0,
                "deduped_count": 0,
                "failed_count": 0,
                "affected_paper_ids": [],
                "metadata_changed_paper_ids": [],
                "source_counts": {},
                "errors": [],
            }

        empty_source_backfill = {
            "requested": 0,
            "total": 0,
            "processed": 0,
            "completed": 0,
            "not_found": 0,
            "failed": 0,
            "identity_conflicts": 0,
            "paper_version_ids": [],
            "canonical_paper_ids": [],
            "results": [],
            "progress_callback_errors": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap = sqlite3.connect(database)
            bootstrap.row_factory = sqlite3.Row
            try:
                init_db(bootstrap)
            finally:
                bootstrap.close()

            def read_report() -> dict[str, object]:
                observer = sqlite3.connect(database)
                try:
                    raw = observer.execute(
                        "SELECT report_json FROM daily_runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()[0]
                    return json.loads(raw)
                finally:
                    observer.close()

            def fake_abstracts(
                _connection: sqlite3.Connection,
                *,
                limit: int,
                timeout: int,
                progress_callback,
            ) -> dict[str, object]:
                captured["limit"] = limit
                captured["timeout"] = timeout
                updates = [
                    (0, 2, 0, 0, 0, "", ""),
                    (0, 2, 0, 0, 0, "paper-a", "resolver"),
                    (1, 2, 1, 0, 0, "paper-a", "openalex"),
                    (1, 2, 1, 0, 0, "paper-b", "resolver"),
                    (2, 2, 1, 1, 0, "paper-b", "all_sources"),
                ]
                for update in updates:
                    progress_callback(*update)
                    snapshots.append(read_report())
                return {
                    "requested": 2,
                    "total": 2,
                    "processed": 2,
                    "enriched": 1,
                    "not_found": 1,
                    "failed": 0,
                    "cooldown_skipped": 0,
                    "results": [],
                    "progress_callback_errors": [],
                }


            with (
                patch("backend.scheduler.daily_update.run_real_ingest", side_effect=fake_ingest),
                patch("backend.scheduler.daily_update.run_source_coverage_audit", return_value={"status": "ok"}),
                patch("backend.scheduler.daily_update.run_source_backfill", return_value=empty_source_backfill),
                patch("backend.scheduler.daily_update.backfill_missing_abstracts", side_effect=fake_abstracts),

                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
            ):
                result = run_daily_update(
                    DailyOptions(
                        dry_run=False,
                        skip_network=False,
                        include_arxiv=True,
                        include_crossref=True,
                        download_limit=0,
                        scan_mode="live",
                        abstract_backfill_limit=2,
                    ),
                    database=database,
                    pdf_root=root / "pdf",
                    trigger_type="test_abstract_progress",
                )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(captured, {"limit": 2, "timeout": 4})
        self.assertEqual([item["stage"] for item in snapshots], ["backfilling_abstracts"] * 5)
        self.assertEqual([item["percent"] for item in snapshots], [70, 70, 74, 74, 78])
        self.assertEqual(
            [item["abstracts"]["processed"] for item in snapshots],
            [0, 0, 1, 1, 2],
        )
        self.assertEqual(
            [item["abstracts"]["queue_remaining"] for item in snapshots],
            [2, 2, 1, 1, 0],
        )
        self.assertEqual(
            [item["abstracts"]["current_source"] for item in snapshots],
            ["", "resolver", "openalex", "resolver", "all_sources"],
        )

        self.assertEqual(result["report"]["abstracts"]["processed"], 2)


if __name__ == "__main__":
    unittest.main()