from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.api.library_routes import api_monitor_hits, api_monitors
from backend.db.database import init_db, upsert_paper
from backend.library.local_search import rebuild_search_index
from backend.library.repository import LibraryRepository, stable_id, utc_now
from backend.library.workbench import ensure_workbench_schema
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import evaluate_monitors


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    ensure_workbench_schema(connection)
    connection.commit()
    return connection


class RealTimeVisibilityAndMonitorTests(unittest.TestCase):
    def test_ingest_visibility_flag_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            upsert_paper(connection, {
                "id": "arxiv:2607.00001", "arxiv_id": "2607.00001", "source": "arxiv",
                "title": "Graphene transport in a moire lattice", "submitted_date": "2026-07-16",
                "condmat_view_eligible": True, "condmat_view_reason": "arxiv_category:cond-mat",
            })
            row = connection.execute("SELECT condmat_view_eligible, condmat_view_reason FROM papers WHERE arxiv_id=?", ("2607.00001",)).fetchone()
            self.assertEqual(row["condmat_view_eligible"], 1)
            self.assertEqual(row["condmat_view_reason"], "arxiv_category:cond-mat")
            connection.close()

    def test_monitor_keeps_first_hit_and_does_not_requeue_on_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            paper = LibraryRepository(connection).upsert_version(
                {"title": "Graphene quantum Hall transport", "abstract": "Condensed matter transport study.", "publication_date": "2026-07-16", "is_open_access": True},
                source="manual", source_record_id="monitor-test", default_condmat_eligible=True,
            )
            rebuild_search_index(connection)
            monitor_id = stable_id("monitor", "graphene-test")
            now = utc_now()
            connection.execute("""INSERT INTO monitor_queries
                (id, name, query_text, filters_json, auto_download_oa, enabled, created_at, updated_at)
                VALUES (?, 'Graphene', 'Graphene', '{}', 1, 1, ?, ?)""", (monitor_id, now, now))
            first = evaluate_monitors(connection, monitor_ids=[monitor_id], run_id="run-1")
            second = evaluate_monitors(connection, monitor_ids=[monitor_id], run_id="run-2")
            hit = connection.execute("SELECT * FROM monitor_hits WHERE monitor_id=?", (monitor_id,)).fetchone()
            self.assertEqual(first["new_hits"], 1)
            self.assertEqual(first["download_tasks_enqueued"], 1)
            self.assertEqual(second["new_hits"], 0)
            self.assertEqual(second["download_tasks_enqueued"], 0)
            self.assertEqual(hit["match_count"], 2)
            self.assertEqual(hit["canonical_paper_id"], paper.canonical_paper_id)
            connection.close()


    def test_automatic_monitor_scope_cannot_be_crowded_by_old_top_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            old = repository.upsert_version(
                {
                    "title": "Graphene old corpus result",
                    "abstract": "Graphene transport.",
                    "publication_date": "2026-07-17",
                    "is_open_access": False,
                },
                source="manual",
                source_record_id="monitor-old",
                default_condmat_eligible=True,
            )
            current = repository.upsert_version(
                {
                    "title": "Graphene newly discovered result",
                    "abstract": "Graphene transport.",
                    "publication_date": "2026-07-16",
                    "is_open_access": False,
                },
                source="manual",
                source_record_id="monitor-current",
                default_condmat_eligible=True,
            )
            rebuild_search_index(connection)
            monitor_id = stable_id("monitor", "graphene-current-scan")
            now = utc_now()
            connection.execute(
                """INSERT INTO monitor_queries
                (id, name, query_text, filters_json, auto_download_oa, enabled, created_at, updated_at)
                VALUES (?, 'Graphene current', 'Graphene', '{"limit": 1}', 0, 1, ?, ?)""",
                (monitor_id, now, now),
            )

            automatic = evaluate_monitors(
                connection,
                monitor_ids=[monitor_id],
                run_id="automatic-1",
                candidate_version_ids=[current.paper_version_id],
            )
            repeated = evaluate_monitors(
                connection,
                monitor_ids=[monitor_id],
                run_id="automatic-2",
                candidate_version_ids=[],
            )
            automatic_hits = connection.execute(
                "SELECT paper_version_id, match_count FROM monitor_hits WHERE monitor_id=?",
                (monitor_id,),
            ).fetchall()

            self.assertEqual(automatic["matched"], 1)
            self.assertEqual(automatic["new_hits"], 1)
            self.assertEqual(automatic_hits[0]["paper_version_id"], current.paper_version_id)
            self.assertEqual(automatic_hits[0]["match_count"], 1)
            self.assertEqual(repeated["matched"], 0)

            manual = evaluate_monitors(connection, monitor_ids=[monitor_id], run_id="manual")
            all_hits = connection.execute(
                "SELECT paper_version_id FROM monitor_hits WHERE monitor_id=? ORDER BY paper_version_id",
                (monitor_id,),
            ).fetchall()
            self.assertEqual(manual["matched"], 1)
            self.assertEqual(manual["new_hits"], 1)
            self.assertEqual({row["paper_version_id"] for row in all_hits}, {old.paper_version_id, current.paper_version_id})
            connection.close()

    def test_structured_journal_monitor_uses_filter_and_excludes_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            eligible = repository.upsert_version(
                {
                    "title": "Correlated electron phase diagram",
                    "abstract": "A condensed matter study.",
                    "journal": "Nature Physics",
                    "publication_date": "2026-07-18",
                },
                source="openalex",
                source_record_id="eligible-journal-monitor",
                default_condmat_eligible=True,
            )
            ineligible = repository.upsert_version(
                {
                    "title": "A biological imaging result",
                    "abstract": "Cell imaging and clinical analysis.",
                    "journal": "Nature Physics",
                    "publication_date": "2026-07-18",
                },
                source="openalex",
                source_record_id="ineligible-journal-monitor",
                default_condmat_eligible=False,
            )
            rebuild_search_index(connection)
            monitor_id = stable_id("monitor", "nature-physics-structured")
            now = utc_now()
            connection.execute(
                """INSERT INTO monitor_queries
                (id, name, query_text, filters_json, auto_download_oa, enabled, created_at, updated_at)
                VALUES (?, 'Nature Physics', 'Nature Physics',
                        '{"monitor_type":"journal","journal":"Nature Physics"}',
                        0, 1, ?, ?)""",
                (monitor_id, now, now),
            )

            result = evaluate_monitors(connection, monitor_ids=[monitor_id])
            hits = connection.execute(
                "SELECT canonical_paper_id FROM monitor_hits WHERE monitor_id=?",
                (monitor_id,),
            ).fetchall()
            connection.close()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["new_hits"], 1)
        self.assertEqual([row["canonical_paper_id"] for row in hits], [eligible.canonical_paper_id])
        self.assertNotEqual(eligible.canonical_paper_id, ineligible.canonical_paper_id)

    def test_monitor_deduplicates_versions_by_canonical_and_prefers_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            preprint = repository.upsert_version(
                {
                    "title": "Graphene correlated transport",
                    "abstract": "Graphene quantum transport.",
                    "doi": "10.5555/monitor-canonical",
                    "arxiv_id": "2607.12345v1",
                    "version_type": "preprint",
                    "journal": "arXiv",
                    "publication_date": "2026-07-15",
                },
                source="arxiv",
                source_record_id="2607.12345v1",
                default_condmat_eligible=True,
            )
            publication = repository.upsert_version(
                {
                    "title": "Graphene correlated transport",
                    "abstract": "Graphene quantum transport.",
                    "doi": "10.5555/monitor-canonical",
                    "version_type": "published",
                    "journal": "Physical Review B",
                    "publication_date": "2026-07-18",
                },
                source="crossref",
                source_record_id="10.5555/monitor-canonical",
                default_condmat_eligible=True,
            )
            rebuild_search_index(connection)
            monitor_id = stable_id("monitor", "canonical-dedup")
            now = utc_now()
            connection.execute(
                """INSERT INTO monitor_queries
                (id, name, query_text, filters_json, auto_download_oa, enabled, created_at, updated_at)
                VALUES (?, 'Graphene', 'Graphene', '{}', 0, 1, ?, ?)""",
                (monitor_id, now, now),
            )

            result = evaluate_monitors(
                connection,
                monitor_ids=[monitor_id],
                candidate_version_ids=[
                    preprint.paper_version_id,
                    publication.paper_version_id,
                ],
            )
            hits = connection.execute(
                "SELECT canonical_paper_id,paper_version_id FROM monitor_hits WHERE monitor_id=?",
                (monitor_id,),
            ).fetchall()
            connection.close()

        self.assertEqual(preprint.canonical_paper_id, publication.canonical_paper_id)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["new_hits"], 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["canonical_paper_id"], publication.canonical_paper_id)
        self.assertEqual(hits[0]["paper_version_id"], publication.paper_version_id)
    def test_monitor_api_hides_ineligible_and_rolls_up_historical_version_hits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = connection_for(database)
            repository = LibraryRepository(connection)
            preprint = repository.upsert_version(
                {
                    "title": "Nb3Cl8 spin liquid",
                    "abstract": "Quantum magnetism.",
                    "doi": "10.5555/nb3cl8-monitor",
                    "version_type": "preprint",
                    "publication_date": "2026-07-15",
                },
                source="arxiv",
                source_record_id="nb3cl8-preprint",
                default_condmat_eligible=True,
            )
            publication = repository.upsert_version(
                {
                    "title": "Nb3Cl8 spin liquid",
                    "abstract": "Quantum magnetism.",
                    "doi": "10.5555/nb3cl8-monitor",
                    "version_type": "published",
                    "journal": "Nature Physics",
                    "publication_date": "2026-07-18",
                },
                source="crossref",
                source_record_id="nb3cl8-publication",
                default_condmat_eligible=True,
            )
            robot = repository.upsert_version(
                {
                    "title": "Ultralight ion-propelled microrobot",
                    "abstract": "Controlled flight.",
                    "doi": "10.1038/s41467-026-76462-y",
                    "version_type": "published",
                    "journal": "Nature Communications",
                    "publication_date": "2026-07-18",
                },
                source="openalex",
                source_record_id="microrobot-monitor-hit",
                default_condmat_eligible=False,
            )
            monitor_id = stable_id("monitor", "api-canonical-rollup")
            now = utc_now()
            connection.execute(
                """INSERT INTO monitor_queries
                (id,name,query_text,filters_json,auto_download_oa,enabled,created_at,updated_at)
                VALUES (?, 'Nb3Cl8', 'Nb3Cl8', '{}', 0, 1, ?, ?)""",
                (monitor_id, now, now),
            )
            connection.executemany(
                """INSERT INTO monitor_hits(
                id,monitor_id,canonical_paper_id,paper_version_id,
                first_matched_at,last_matched_at,match_count,payload_json
                ) VALUES (?,?,?,?,?,?,?, '{}')""",
                [
                    ("hit-preprint", monitor_id, preprint.canonical_paper_id, preprint.paper_version_id, now, now, 1),
                    ("hit-publication", monitor_id, publication.canonical_paper_id, publication.paper_version_id, now, now, 2),
                    ("hit-robot", monitor_id, robot.canonical_paper_id, robot.paper_version_id, now, now, 7),
                ],
            )
            connection.commit()
            connection.close()

            @contextmanager
            def test_connect(_path=None):
                current = sqlite3.connect(database)
                current.row_factory = sqlite3.Row
                current.execute("PRAGMA foreign_keys=ON")
                try:
                    yield current
                finally:
                    current.close()

            with patch("backend.api.library_routes.connect", test_connect):
                summary = api_monitors()
                hits = api_monitor_hits(limit=100)

        self.assertEqual(summary["total_hits"], 1)
        self.assertEqual(summary["items"][0]["total_hits"], 1)
        self.assertEqual(hits["count"], 1)
        self.assertEqual(hits["items"][0]["canonical_paper_id"], publication.canonical_paper_id)
        self.assertEqual(hits["items"][0]["paper_version_id"], publication.paper_version_id)
        self.assertEqual(hits["items"][0]["match_count"], 3)

if __name__ == "__main__":
    unittest.main()