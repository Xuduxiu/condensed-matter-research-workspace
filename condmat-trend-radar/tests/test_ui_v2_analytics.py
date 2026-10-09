from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api import ui_v2_routes
from backend.db.database import init_db
from backend.library.workbench import ensure_workbench_schema
from backend.migrations.unified_library import apply_unified_schema


def prepare_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    ensure_workbench_schema(connection)
    connection.commit()
    connection.close()


def test_connect(path: Path):
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


def insert_paper(
    connection: sqlite3.Connection,
    *,
    paper_id: str,
    version_id: str,
    paper_date: str,
    version_type: str,
    source: str,
    abstract: str = "",
    journal: str = "",
    eligible: bool = True,
    open_access: bool = False,
) -> None:
    connection.execute(
        """
        INSERT INTO papers (
          id, title, abstract, journal, publication_date, source, data_mode,
          is_open_access, condmat_view_eligible, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'real', ?, ?, ?, ?)
        """,
        (
            paper_id,
            f"Title {paper_id}",
            abstract,
            journal,
            paper_date,
            source,
            1 if open_access else 0,
            1 if eligible else 0,
            paper_date,
            paper_date,
        ),
    )
    connection.execute(
        """
        INSERT INTO paper_versions (
          id, canonical_paper_id, version_type, title, abstract, journal,
          publication_date, submitted_date, source, source_record_id,
          raw_json, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
        """,
        (
            version_id,
            paper_id,
            version_type,
            f"Title {paper_id}",
            abstract,
            journal,
            paper_date,
            paper_date,
            source,
            version_id,
            paper_date,
            paper_date,
        ),
    )


class AnalyticsApiTests(unittest.TestCase):
    def test_empty_database_returns_zero_filled_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "empty.sqlite"
            prepare_database(database)
            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/analytics", params={"days": 7})

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["summary"]["window_papers"], 0)
            self.assertEqual(payload["summary"]["abstract_coverage_pct"], 0.0)
            self.assertEqual(payload["summary"]["success_rate_pct"], 0.0)
            self.assertEqual(len(payload["paper_timeline"]), 7)
            self.assertEqual(len(payload["monitor_timeline"]), 7)
            self.assertTrue(all(item["total"] == 0 for item in payload["paper_timeline"]))
            self.assertEqual(payload["range_to"], payload["data_anchor"])

    def test_analytics_uses_only_real_eligible_data_and_latest_non_future_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "analytics.sqlite"
            prepare_database(database)
            today = datetime.now(timezone.utc).date()
            anchor = today - timedelta(days=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:preprint",
                version_id="version:preprint",
                paper_date=anchor.isoformat(),
                version_type="preprint",
                source="arxiv",
                abstract="A complete abstract.",
                journal="arXiv",
                open_access=True,
            )
            insert_paper(
                connection,
                paper_id="paper:publication",
                version_id="version:publication",
                paper_date=(anchor - timedelta(days=1)).isoformat(),
                version_type="publication",
                source="crossref",
                journal="Physical Review Letters",
            )
            insert_paper(
                connection,
                paper_id="paper:excluded",
                version_id="version:excluded",
                paper_date=anchor.isoformat(),
                version_type="publication",
                source="crossref",
                abstract="Must not be counted.",
                eligible=False,
            )
            insert_paper(
                connection,
                paper_id="paper:future",
                version_id="version:future",
                paper_date=(today + timedelta(days=30)).isoformat(),
                version_type="publication",
                source="crossref",
                eligible=True,
            )
            connection.execute(
                """
                INSERT INTO download_tasks (
                  id, canonical_paper_id, paper_version_id, status, created_at, updated_at, completed_at
                ) VALUES
                  ('task:ok', 'paper:preprint', 'version:preprint', 'completed', ?, ?, ?),
                  ('task:failed', 'paper:publication', 'version:publication', 'retryable_failed', ?, ?, NULL)
                """,
                (
                    anchor.isoformat(),
                    anchor.isoformat(),
                    anchor.isoformat(),
                    anchor.isoformat(),
                    anchor.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_files (
                  id, canonical_paper_id, paper_version_id, absolute_path, sha256,
                  file_size, mime_type, extraction_status, validation_status, created_at, updated_at
                ) VALUES (
                  'file:preprint', 'paper:preprint', 'version:preprint', 'paper.pdf', 'sha-preprint',
                  100, 'application/pdf', 'completed', 'valid_pdf_header', ?, ?
                )
                """,
                (anchor.isoformat(), anchor.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO monitor_hits (
                  id, monitor_id, canonical_paper_id, paper_version_id,
                  first_matched_at, last_matched_at, match_count
                ) VALUES (
                  'hit:preprint', 'monitor:one', 'paper:preprint', 'version:preprint',
                  ?, ?, 1
                )
                """,
                (anchor.isoformat(), anchor.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO materials (
                  id, canonical_name, material_family, created_at, updated_at
                ) VALUES ('material:graphene', 'Graphene', '2D material', ?, ?)
                """,
                (anchor.isoformat(), anchor.isoformat()),
            )
            connection.executemany(
                """
                INSERT INTO paper_materials (
                  canonical_paper_id, material_id, confidence, source, context_json, created_at
                ) VALUES (?, 'material:graphene', 1.0, 'paper_text_formula', '{}', ?)
                """,
                [
                    ("paper:preprint", anchor.isoformat()),
                    ("paper:publication", anchor.isoformat()),
                    ("paper:excluded", anchor.isoformat()),
                ],
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/analytics", params={"days": 7})
                invalid_days = client.get("/api/ui-v2/analytics", params={"days": 6})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(invalid_days.status_code, 422)
            payload = response.json()
            summary = payload["summary"]
            self.assertEqual(payload["data_anchor"], anchor.isoformat())
            self.assertEqual(summary["window_papers"], 2)
            self.assertEqual(summary["preprints"], 1)
            self.assertEqual(summary["publications"], 1)
            self.assertEqual(summary["abstract_available"], 1)
            self.assertEqual(summary["abstract_missing"], 1)
            self.assertEqual(summary["abstract_coverage_pct"], 50.0)
            self.assertEqual(summary["oa_count"], 1)
            self.assertEqual(summary["pdf_count"], 1)
            self.assertEqual(summary["monitor_hits"], 1)
            self.assertEqual(summary["downloads_completed"], 1)
            self.assertEqual(summary["downloads_failed"], 1)
            self.assertEqual(summary["success_rate_pct"], 50.0)
            self.assertEqual(payload["paper_timeline"][-1]["preprints"], 1)
            self.assertEqual(payload["paper_timeline"][-2]["publications"], 1)
            self.assertEqual({item["name"] for item in payload["source_distribution"]}, {"arxiv", "crossref"})
            self.assertEqual(payload["top_materials"][0]["paper_count"], 2)
            self.assertEqual(payload["top_materials"][0]["preprints"], 1)
            self.assertEqual(payload["top_materials"][0]["publications"], 1)


class MaterialPaginationAndSystemTests(unittest.TestCase):
    def test_material_query_filters_before_count_dynamic_count_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "materials.sqlite"
            prepare_database(database)
            day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:one",
                version_id="version:one",
                paper_date=day,
                version_type="preprint",
                source="arxiv",
                eligible=True,
            )
            insert_paper(
                connection,
                paper_id="paper:two",
                version_id="version:two",
                paper_date=day,
                version_type="preprint",
                source="arxiv",
                eligible=True,
            )
            connection.executemany(
                """
                INSERT INTO materials (
                  id, canonical_name, material_family, created_at, updated_at
                ) VALUES (?, ?, '2D material', ?, ?)
                """,
                [
                    ("material:graphene", "Graphene", day, day),
                    ("material:graphite", "Graphite", day, day),
                    ("material:mos2", "Molybdenum disulfide", day, day),
                ],
            )
            connection.execute(
                """
                INSERT INTO material_aliases (
                  alias, normalized_alias, material_id, source, created_at
                ) VALUES ('MoS2', 'mos2', 'material:mos2', 'test', ?)
                """,
                (day,),
            )
            connection.executemany(
                """
                INSERT INTO paper_materials (
                  canonical_paper_id, material_id, confidence, source, context_json, created_at
                ) VALUES (?, ?, 1.0, ?, '{}', ?)
                """,
                [
                    ("paper:one", "material:graphene", "paper_text_formula", day),
                    ("paper:two", "material:graphite", "curated", day),
                    ("paper:one", "material:mos2", "paper_text_registry", day),
                ],
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                graph = client.get(
                    "/api/ui-v2/materials",
                    params={"q": "graph", "min_evidence": 1, "limit": 20, "offset": 1},
                ).json()
                alias = client.get(
                    "/api/ui-v2/materials",
                    params={"q": "mos2", "min_evidence": 1, "limit": 20},
                ).json()

            self.assertEqual(graph["count"], 2)
            self.assertEqual(graph["dynamic_entities"], 1)
            self.assertEqual(graph["returned_count"], 1)
            self.assertEqual(graph["offset"], 1)
            self.assertEqual(graph["items"][0]["canonical_name"], "Graphite")
            self.assertEqual(alias["count"], 1)
            self.assertEqual(alias["dynamic_entities"], 1)
            self.assertEqual(alias["items"][0]["canonical_name"], "Molybdenum disulfide")

    def test_system_status_uses_live_integrity_and_pdf_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "system.sqlite"
            prepare_database(database)
            day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:one",
                version_id="version:one",
                paper_date=day,
                version_type="publication",
                source="crossref",
                eligible=True,
            )
            connection.executemany(
                """
                INSERT INTO paper_files (
                  id, canonical_paper_id, paper_version_id, absolute_path, sha256,
                  file_size, mime_type, extraction_status, validation_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 100, 'application/pdf', ?, ?, ?, ?)
                """,
                [
                    (
                        "file:valid",
                        "paper:one",
                        "version:one",
                        "valid.pdf",
                        "sha-valid",
                        "completed",
                        "valid_pdf_header",
                        day,
                        day,
                    ),
                    (
                        "file:unmatched",
                        None,
                        None,
                        "unmatched.pdf",
                        "sha-unmatched",
                        "pending",
                        "invalid_header",
                        day,
                        day,
                    ),
                ],
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/system")
                cached_response = client.get("/api/ui-v2/system")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(cached_response.status_code, 200)
            payload = response.json()
            cached_payload = cached_response.json()
            self.assertEqual(payload["database"]["quick_check"], "ok")
            self.assertEqual(payload["database"]["foreign_key_violations"], 0)
            self.assertFalse(payload["database"]["health_cached"])
            self.assertEqual(payload["database"]["health_cache_seconds"], 86_400)
            self.assertTrue(payload["database"]["health_checked_at"])
            self.assertTrue(cached_payload["database"]["health_cached"])
            self.assertEqual(
                cached_payload["database"]["health_checked_at"],
                payload["database"]["health_checked_at"],
            )
            self.assertEqual(payload["pdf_library"]["invalid_files"], 1)
            self.assertEqual(payload["pdf_library"]["duplicates"], 0)
            self.assertEqual(payload["pdf_library"]["unmatched"], 1)
            self.assertEqual(payload["migration"]["verification"]["source"], "live")


class UiV2ResponseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        ui_v2_routes._clear_ui_response_cache()
        self.addCleanup(ui_v2_routes._clear_ui_response_cache)

    def test_dashboard_cache_refreshes_live_counts_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dashboard-cache.sqlite"
            prepare_database(database)
            today = datetime.now().astimezone().date().isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:cached-one",
                version_id="version:cached-one",
                paper_date=today,
                version_type="preprint",
                source="arxiv",
                eligible=True,
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                patch("backend.api.ui_v2_routes._ui_cache_now", return_value=100.0) as cache_clock,
                TestClient(create_app()) as client,
            ):
                first = client.get("/api/ui-v2/dashboard").json()

                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                insert_paper(
                    connection,
                    paper_id="paper:cached-two",
                    version_id="version:cached-two",
                    paper_date=today,
                    version_type="publication",
                    source="crossref",
                    eligible=True,
                )
                connection.commit()
                connection.close()

                cached = client.get("/api/ui-v2/dashboard").json()
                cache_clock.return_value = 131.0
                refreshed = client.get("/api/ui-v2/dashboard").json()

            self.assertEqual(first["metrics"]["new_papers"], 1)
            self.assertEqual(first["metrics"]["new_preprints"], 1)
            self.assertEqual(cached["metrics"]["new_papers"], 1)
            self.assertEqual(refreshed["metrics"]["new_papers"], 2)
            self.assertEqual(refreshed["metrics"]["new_publications"], 1)

    def test_dashboard_spotlight_returns_one_preferred_version_per_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dashboard-canonical.sqlite"
            prepare_database(database)
            today = datetime.now().astimezone().date().isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:canonical",
                version_id="radar-version:paper:canonical",
                paper_date=today,
                version_type="publication",
                source="radar_legacy",
                eligible=True,
            )
            connection.execute(
                """
                INSERT INTO paper_versions (
                  id, canonical_paper_id, version_type, title, abstract, journal,
                  publication_date, submitted_date, source, source_record_id,
                  raw_json, first_seen_at, last_seen_at
                ) VALUES ('version:openalex', 'paper:canonical', 'publication',
                          'Preferred source version', 'Complete abstract', 'Journal',
                          ?, ?, 'openalex', 'W-test', '{}', ?, ?)
                """,
                (today, today, today, today),
            )
            connection.executemany(
                """
                INSERT INTO library_fts(
                  canonical_paper_id, paper_version_id, title, abstract, body
                ) VALUES (?, ?, ?, ?, '')
                """,
                [
                    ("paper:canonical", "radar-version:paper:canonical", "Legacy duplicate", "",),
                    ("paper:canonical", "version:openalex", "Preferred source version", "Complete abstract",),
                ],
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                payload = client.get("/api/ui-v2/dashboard").json()
                papers_payload = client.get("/api/ui-v2/papers").json()
                search_payload = client.get(
                    "/api/ui-v2/papers",
                    params={"q": "Preferred", "sort": "relevance"},
                ).json()

            self.assertEqual(len(payload["spotlight"]), 1)
            self.assertEqual(payload["spotlight"][0]["canonical_paper_id"], "paper:canonical")
            self.assertEqual(payload["spotlight"][0]["paper_version_id"], "version:openalex")
            self.assertEqual(papers_payload["total"], 1)
            self.assertEqual(papers_payload["count"], 1)
            self.assertEqual(papers_payload["items"][0]["paper_version_id"], "version:openalex")
            self.assertEqual(search_payload["total"], 1)
            self.assertEqual(search_payload["count"], 1)
            self.assertEqual(search_payload["items"][0]["paper_version_id"], "version:openalex")
    def test_dashboard_uses_application_today_across_shanghai_utc_midnight(self) -> None:
        """Asia/Shanghai can be one calendar day ahead of SQLite UTC now."""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dashboard-timezone.sqlite"
            prepare_database(database)
            utc_today = datetime.now(timezone.utc).date()
            application_today = (utc_today + timedelta(days=1)).isoformat()
            future_date = (utc_today + timedelta(days=2)).isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:shanghai-today",
                version_id="version:shanghai-today",
                paper_date=application_today,
                version_type="publication",
                source="crossref",
                eligible=True,
            )
            insert_paper(
                connection,
                paper_id="paper:future",
                version_id="version:future",
                paper_date=future_date,
                version_type="publication",
                source="crossref",
                eligible=True,
            )
            connection.commit()
            connection.close()

            with patch("backend.api.ui_v2_routes.connect", test_connect(database)):
                payload = ui_v2_routes._build_dashboard_payload(application_today)

        self.assertEqual(payload["today"], application_today)
        self.assertEqual(payload["latest_data_date"], application_today)
        self.assertEqual(payload["material_data_anchor"], application_today)
        self.assertEqual(payload["metrics"]["latest_day_papers"], 1)
        self.assertEqual(
            [item["canonical_paper_id"] for item in payload["spotlight"]],
            ["paper:shanghai-today"],
        )
    def test_cache_single_flight_runs_builder_once_for_concurrent_callers(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def builder() -> dict[str, int]:
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return {"value": 7}

        with patch("backend.api.ui_v2_routes._ui_cache_now", return_value=100.0):
            with ThreadPoolExecutor(max_workers=4) as executor:
                first = executor.submit(ui_v2_routes._cached_ui_payload, ("single-flight",), builder)
                self.assertTrue(started.wait(timeout=5))
                others = [
                    executor.submit(ui_v2_routes._cached_ui_payload, ("single-flight",), builder)
                    for _ in range(3)
                ]
                release.set()
                results = [first.result(timeout=5), *(item.result(timeout=5) for item in others)]

        self.assertEqual(calls, 1)
        self.assertEqual(results, [{"value": 7}] * 4)


class MainRadarEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        ui_v2_routes._clear_ui_response_cache()
        self.addCleanup(ui_v2_routes._clear_ui_response_cache)

    def test_generalist_review_record_stays_out_of_main_views_and_materials_are_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "main-radar-eligibility.sqlite"
            prepare_database(database)
            today = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:nb3cl8",
                version_id="version:nb3cl8-preprint",
                paper_date=today,
                version_type="preprint",
                source="arxiv",
                journal="arXiv",
                eligible=True,
            )
            insert_paper(
                connection,
                paper_id="paper:microrobot",
                version_id="version:microrobot",
                paper_date=today,
                version_type="publication",
                source="openalex",
                journal="Nature Communications",
                eligible=False,
            )
            connection.execute(
                "UPDATE papers SET title=?, doi=?, condmat_view_reason=? WHERE id='paper:microrobot'",
                (
                    "Controlled flight of high-thrust ultralight ion-propelled microrobot with integrated sensing",
                    "10.1038/s41467-026-76462-y",
                    "openalex-generalist-topic-requires-text",
                ),
            )
            connection.execute(
                "UPDATE paper_versions SET title=?, doi=? WHERE id='version:microrobot'",
                (
                    "Controlled flight of high-thrust ultralight ion-propelled microrobot with integrated sensing",
                    "10.1038/s41467-026-76462-y",
                ),
            )
            connection.execute(
                "UPDATE papers SET title='Correlated magnetism in layered Nb3Cl8' WHERE id='paper:nb3cl8'"
            )
            connection.execute(
                "UPDATE paper_versions SET title='Correlated magnetism in layered Nb3Cl8' WHERE id='version:nb3cl8-preprint'"
            )
            connection.execute(
                """INSERT INTO paper_versions (
                     id, canonical_paper_id, version_type, title, abstract, journal,
                     publication_date, submitted_date, source, source_record_id,
                     raw_json, first_seen_at, last_seen_at
                   ) VALUES (
                     'version:nb3cl8-published', 'paper:nb3cl8', 'publication',
                     'Correlated magnetism in layered Nb3Cl8', 'Quantum magnetism and spin correlations.',
                     'Nature Communications', ?, ?, 'openalex', 'W-nb3cl8', '{}', ?, ?
                   )""",
                (today, today, today, today),
            )
            connection.execute(
                """INSERT INTO materials(id, canonical_name, material_family, created_at, updated_at)
                   VALUES ('material:nb3cl8', 'Nb3Cl8', 'layered material', ?, ?)""",
                (today, today),
            )
            connection.executemany(
                """INSERT INTO paper_materials(
                     canonical_paper_id, material_id, confidence, source, context_json, created_at
                   ) VALUES (?, 'material:nb3cl8', 1.0, 'paper_text_formula', '{}', ?)""",
                [
                    ("paper:nb3cl8", today),
                    ("paper:microrobot", today),
                ],
            )
            connection.executemany(
                """INSERT INTO library_fts(
                     canonical_paper_id, paper_version_id, title, abstract, body
                   ) VALUES (?, ?, ?, ?, '')""",
                [
                    ("paper:nb3cl8", "version:nb3cl8-preprint", "Correlated magnetism in layered Nb3Cl8", ""),
                    ("paper:nb3cl8", "version:nb3cl8-published", "Correlated magnetism in layered Nb3Cl8", "Quantum magnetism and spin correlations."),
                    ("paper:microrobot", "version:microrobot", "Controlled flight of high-thrust ultralight ion-propelled microrobot with integrated sensing", ""),
                ],
            )
            connection.execute(
                """INSERT INTO manual_review_items(
                     id, review_type, status, source_project, source_record_id,
                     candidate_paper_id, reason, payload_json, created_at
                   ) VALUES (
                     'review:microrobot', 'corpus_eligibility', 'pending', 'openalex',
                     'W-microrobot', 'paper:microrobot',
                     'generalist topic requires text evidence', '{}', ?
                   )""",
                (today,),
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                patch("backend.api.library_routes.connect", test_connect(database)),
                TestClient(create_app()) as client,
            ):
                dashboard = client.get("/api/ui-v2/dashboard").json()
                discover = client.get("/api/ui-v2/papers").json()
                bypass_attempt = client.get(
                    "/api/ui-v2/papers",
                    params={"q": "microrobot", "condmat_only": "false"},
                ).json()
                materials = client.get(
                    "/api/ui-v2/materials",
                    params={"min_evidence": 1, "limit": 20},
                ).json()
                detail = client.get("/api/ui-v2/materials/material:nb3cl8").json()
                retained = client.get("/api/ui-v2/papers/paper:microrobot")
                reviews = client.get("/api/library/reviews").json()

        self.assertEqual(
            [item["canonical_paper_id"] for item in dashboard["spotlight"]],
            ["paper:nb3cl8"],
        )
        self.assertEqual(discover["total"], 1)
        self.assertEqual(
            [item["canonical_paper_id"] for item in discover["items"]],
            ["paper:nb3cl8"],
        )
        self.assertEqual(bypass_attempt["total"], 0)
        self.assertTrue(materials["items"], materials)
        material = next(item for item in materials["items"] if item["id"] == "material:nb3cl8")
        self.assertEqual(material["total_papers"], 1)
        self.assertEqual(detail["counts"]["total"], 1)
        self.assertEqual(len(detail["papers"]), 1)
        self.assertEqual(detail["papers"][0]["canonical_paper_id"], "paper:nb3cl8")
        self.assertEqual(detail["papers"][0]["paper_version_id"], "version:nb3cl8-published")
        self.assertEqual({item["canonical_paper_id"] for item in detail["evidence"]}, {"paper:nb3cl8"})
        self.assertEqual(sum(int(item["total"]) for item in detail["series"]), 1)
        self.assertEqual(retained.status_code, 200)
        self.assertEqual(retained.json()["paper"]["doi"], "10.1038/s41467-026-76462-y")
        self.assertEqual(
            [item["candidate_paper_id"] for item in reviews["items"]],
            ["paper:microrobot"],
        )


if __name__ == "__main__":
    unittest.main()