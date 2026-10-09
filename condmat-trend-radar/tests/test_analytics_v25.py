from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.analytics_v25 import _momentum_metrics
from backend.api.main import create_app
from tests.test_ui_v2_analytics import insert_paper, prepare_database, test_connect


class AnalyticsV25Tests(unittest.TestCase):
    def test_normalized_hotspots_provenance_and_canonical_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "analytics-v25.sqlite"
            prepare_database(database)
            today = datetime.now(timezone.utc).date()
            anchor = today - timedelta(days=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")

            for index in range(6):
                paper_day = anchor - timedelta(days=index)
                insert_paper(
                    connection,
                    paper_id=f"paper:current:{index}",
                    version_id=f"version:current:{index}",
                    paper_date=paper_day.isoformat(),
                    version_type="preprint" if index == 0 else "publication",
                    source="crossref",
                    abstract="complete abstract",
                    journal="Journal A",
                    open_access=index % 2 == 0,
                )
            for index in range(6):
                paper_day = anchor - timedelta(days=7 + index)
                insert_paper(
                    connection,
                    paper_id=f"paper:baseline:{index}",
                    version_id=f"version:baseline:{index}",
                    paper_date=paper_day.isoformat(),
                    version_type="publication",
                    source="crossref",
                    abstract="complete abstract",
                    journal="Journal A",
                )

            connection.execute(
                "UPDATE paper_versions SET source='radar_legacy' WHERE id='version:current:0'"
            )
            connection.execute(
                """
                INSERT INTO paper_versions (
                  id, canonical_paper_id, version_type, title, abstract, journal,
                  publication_date, submitted_date, source, source_record_id,
                  raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'published', ?, ?, ?, ?, ?, 'openalex', ?, '{}', ?, ?)
                """,
                (
                    "version:current:0:published",
                    "paper:current:0",
                    "Linked publication",
                    "complete abstract",
                    "Journal A",
                    anchor.isoformat(),
                    anchor.isoformat(),
                    "openalex:linked",
                    anchor.isoformat(),
                    anchor.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE paper_versions SET raw_json=? WHERE id='version:current:0:published'",
                ('{"primary_topic":{"id":"https://openalex.org/T10037","display_name":"Physics of Superconductivity and Magnetism","subfield":{"id":"https://openalex.org/subfields/3104"}}}',),
            )
            connection.execute(
                """
                INSERT INTO paper_version_observations(
                  paper_version_id, source_project, source_record_id,
                  observed_at, payload_sha256, payload_json
                ) VALUES (
                  'version:current:0:published', 'openalex_field',
                  'openalex:linked:observation', ?, 'sha-openalex-alias', '{}'
                )
                """,
                (anchor.isoformat(),),
            )
            connection.execute(
                "INSERT INTO topics(id, canonical_name, topic_type, created_at) VALUES ('topic:hot', 'non-Hermitian', 'physics_concept', ?)",
                (anchor.isoformat(),),
            )
            connection.execute(
                "INSERT INTO topics(id, canonical_name, topic_type, created_at) VALUES ('topic:background', 'quantum spin liquid', 'physics_concept', ?)",
                (anchor.isoformat(),),
            )
            for paper_id in [
                "paper:current:4", "paper:current:5",
                "paper:baseline:1", "paper:baseline:2", "paper:baseline:3",
                "paper:baseline:4", "paper:baseline:5",
            ]:
                connection.execute(
                    "INSERT INTO paper_topics(canonical_paper_id, topic_id, confidence, source, created_at) VALUES (?, 'topic:background', 1, 'test', ?)",
                    (paper_id, anchor.isoformat()),
                )
            for index in range(4):
                connection.execute(
                    "INSERT INTO paper_topics(canonical_paper_id, topic_id, confidence, source, created_at) VALUES (?, 'topic:hot', 1, 'test', ?)",
                    (f"paper:current:{index}", anchor.isoformat()),
                )
            connection.execute(
                "INSERT INTO paper_topics(canonical_paper_id, topic_id, confidence, source, created_at) VALUES ('paper:baseline:0', 'topic:hot', 1, 'test', ?)",
                (anchor.isoformat(),),
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/analytics", params={"days": 7})

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["summary"]["window_papers"], 6)
            self.assertEqual(sum(item["total"] for item in payload["paper_timeline"]), 6)
            self.assertNotIn("radar_legacy", {item["name"] for item in payload["source_distribution"]})
            openalex_rows = [item for item in payload["source_distribution"] if item["name"] == "openalex"]
            self.assertEqual(openalex_rows, [{"name": "openalex", "count": 1}])
            self.assertIn("crossref", payload["source_overlap"]["sources"])
            self.assertIn("openalex", payload["source_overlap"]["sources"])
            crossref = payload["source_overlap"]["sources"].index("crossref")
            openalex = payload["source_overlap"]["sources"].index("openalex")
            self.assertEqual(payload["source_overlap"]["matrix"][crossref][openalex], 1)
            hot = next(item for item in payload["topic_trends"] if item["name"] == "non-Hermitian")
            self.assertEqual(hot["current_count"], 4)
            self.assertEqual(hot["baseline_count"], 1)
            self.assertGreater(hot["share_change_pp"], 0)
            self.assertGreater(hot["momentum"], 0)
            linked = next(item for item in payload["publication_pathways"] if item["name"] == "linked_preprint_publication")
            self.assertEqual(linked["count"], 1)
            self.assertEqual(payload["trend_methodology"]["unit"], "canonical_research_output")
            superconductivity = next(item for item in payload["field_distribution"] if item["name"] == "superconductivity")
            self.assertGreaterEqual(superconductivity["direct_count"], 1)


    def test_anchor_uses_canonical_first_scholarly_date_not_later_version_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "canonical-anchor.sqlite"
            prepare_database(database)
            today = datetime.now(timezone.utc).date()
            research_day = today - timedelta(days=10)
            later_publication = today - timedelta(days=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            insert_paper(
                connection,
                paper_id="paper:old-preprint",
                version_id="version:old-preprint",
                paper_date=research_day.isoformat(),
                version_type="preprint",
                source="arxiv",
                abstract="A complete condensed matter abstract.",
                journal="arXiv",
            )
            connection.execute(
                """
                INSERT INTO paper_versions (
                  id, canonical_paper_id, version_type, title, abstract, journal,
                  publication_date, submitted_date, source, source_record_id,
                  raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'published', ?, ?, ?, ?, ?, 'crossref', ?, '{}', ?, ?)
                """,
                (
                    "version:later-publication",
                    "paper:old-preprint",
                    "Later journal version",
                    "A complete condensed matter abstract.",
                    "Physical Review B",
                    later_publication.isoformat(),
                    later_publication.isoformat(),
                    "crossref:later-publication",
                    later_publication.isoformat(),
                    later_publication.isoformat(),
                ),
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/analytics", params={"days": 7})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data_anchor"], research_day.isoformat())
        self.assertEqual(payload["latest_observed_date"], research_day.isoformat())
        self.assertFalse(payload["data_quality"]["partial_today_excluded"])
        self.assertEqual(payload["summary"]["window_papers"], 1)
        self.assertEqual(payload["summary"]["publications"], 0)
        current_days = (
            datetime.fromisoformat(payload["range_to"]).date()
            - datetime.fromisoformat(payload["range_from"]).date()
        ).days + 1
        baseline_days = (
            datetime.fromisoformat(payload["baseline_to"]).date()
            - datetime.fromisoformat(payload["baseline_from"]).date()
        ).days + 1
        self.assertEqual((current_days, baseline_days), (7, 7))

    def test_topic_coverage_excludes_generic_topics_rejected_from_charts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "topic-coverage.sqlite"
            prepare_database(database)
            anchor = datetime.now(timezone.utc).date() - timedelta(days=1)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            paper_ids = []
            for index in range(5):
                paper_id = f"paper:topic-current:{index}"
                paper_ids.append(paper_id)
                insert_paper(
                    connection,
                    paper_id=paper_id,
                    version_id=f"version:topic-current:{index}",
                    paper_date=(anchor - timedelta(days=index)).isoformat(),
                    version_type="publication",
                    source="crossref",
                    abstract="Complete abstract.",
                    journal="Journal A",
                )
            for index in range(5):
                paper_id = f"paper:topic-baseline:{index}"
                paper_ids.append(paper_id)
                insert_paper(
                    connection,
                    paper_id=paper_id,
                    version_id=f"version:topic-baseline:{index}",
                    paper_date=(anchor - timedelta(days=7 + index)).isoformat(),
                    version_type="publication",
                    source="crossref",
                    abstract="Complete abstract.",
                    journal="Journal A",
                )
            connection.execute(
                "INSERT INTO topics(id, canonical_name, topic_type, created_at) "
                "VALUES ('topic:generic', 'condensed matter physics', 'physics_concept', ?)",
                (anchor.isoformat(),),
            )
            connection.executemany(
                "INSERT INTO paper_topics(canonical_paper_id, topic_id, confidence, source, created_at) "
                "VALUES (?, 'topic:generic', 1, 'paper_text_dictionary', ?)",
                ((paper_id, anchor.isoformat()) for paper_id in paper_ids),
            )
            connection.execute(
                "INSERT INTO materials(id,canonical_name,material_family,created_at,updated_at) "
                "VALUES ('material:preset-only','Presetium','registry-only',?,?)",
                (anchor.isoformat(), anchor.isoformat()),
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/analytics", params={"days": 7})

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        quality = payload["data_quality"]
        self.assertEqual(quality["current_topic_extraction_coverage_pct"], 0.0)
        self.assertEqual(quality["baseline_topic_extraction_coverage_pct"], 0.0)
        self.assertFalse(quality["topic_coverage_comparable"])
        self.assertFalse(quality["topic_trend_reliable"])
        self.assertFalse(quality["material_trend_reliable"])
        self.assertFalse(quality["method_trend_reliable"])
        self.assertEqual(payload["topic_trends"], [])
        self.assertNotIn("Presetium", {item["name"] for item in payload["top_materials"]})

    def test_multidisciplinary_non_condmat_is_preserved_but_excluded_and_canonical_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "strict-analytics-scope.sqlite"
            prepare_database(database)
            today = datetime.now(timezone.utc).date()
            anchor = today - timedelta(days=1)
            stamp = anchor.isoformat()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")

            # One eligible canonical output has two scholarly versions and two
            # pieces of entity evidence. Every paper-level chart must count the
            # canonical output once, not once per version/evidence row.
            insert_paper(
                connection,
                paper_id="paper:eligible-condmat",
                version_id="version:eligible-preprint",
                paper_date=stamp,
                version_type="preprint",
                source="arxiv",
                abstract="A quantum spin-liquid experiment on kagome layers.",
                journal="Physical Review B",
                eligible=True,
            )
            connection.execute(
                """
                INSERT INTO paper_versions (
                  id, canonical_paper_id, version_type, title, abstract, journal,
                  publication_date, submitted_date, source, source_record_id,
                  raw_json, first_seen_at, last_seen_at
                ) VALUES (
                  'version:eligible-published', 'paper:eligible-condmat', 'published',
                  'Published quantum spin liquid', 'Complete condensed-matter abstract.',
                  'Physical Review B', ?, ?, 'crossref', 'crossref:eligible', '{}', ?, ?
                )
                """,
                (stamp, stamp, stamp, stamp),
            )

            # This multidisciplinary-journal record is deliberately retained
            # for review, but is not eligible for the condensed-matter view.
            insert_paper(
                connection,
                paper_id="paper:nature-biomed-review",
                version_id="version:nature-biomed",
                paper_date=today.isoformat(),
                version_type="publication",
                source="crossref",
                abstract="A clinical cancer immunotherapy cohort study.",
                journal="Nature",
                eligible=False,
            )
            connection.execute(
                """
                UPDATE papers
                SET title='Clinical cancer immunotherapy in a multicentre cohort',
                    condmat_view_reason='pending_manual_review:multidisciplinary_journal'
                WHERE id='paper:nature-biomed-review'
                """
            )
            connection.execute(
                """
                INSERT INTO paper_versions (
                  id, canonical_paper_id, version_type, title, abstract, journal,
                  publication_date, submitted_date, source, source_record_id,
                  raw_json, first_seen_at, last_seen_at
                ) VALUES (
                  'version:nature-biomed-openalex', 'paper:nature-biomed-review', 'published',
                  'Clinical cancer immunotherapy in a multicentre cohort',
                  'A clinical cancer immunotherapy cohort study.', 'Nature', ?, ?,
                  'openalex', 'openalex:biomed', '{}', ?, ?
                )
                """,
                (today.isoformat(), today.isoformat(), stamp, stamp),
            )

            connection.executemany(
                "INSERT INTO topics(id, canonical_name, topic_type, created_at) VALUES (?, ?, 'physics_concept', ?)",
                (
                    ("topic:spin-liquid", "quantum spin liquid", stamp),
                    ("topic:biomed-leak", "cancer immunotherapy", stamp),
                ),
            )
            connection.executemany(
                """
                INSERT INTO paper_topics(
                  canonical_paper_id, topic_id, confidence, source, created_at
                ) VALUES (?, ?, 1, ?, ?)
                """,
                (
                    ("paper:eligible-condmat", "topic:spin-liquid", "abstract", stamp),
                    ("paper:eligible-condmat", "topic:spin-liquid", "openalex", stamp),
                    ("paper:nature-biomed-review", "topic:biomed-leak", "abstract", stamp),
                ),
            )
            connection.executemany(
                """
                INSERT INTO materials(
                  id, canonical_name, material_family, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ("material:kagome", "Kagome lattice", "quantum material", stamp, stamp),
                    ("material:doxorubicin", "Doxorubicin", "biomedical", stamp, stamp),
                ),
            )
            connection.executemany(
                """
                INSERT INTO paper_materials(
                  canonical_paper_id, material_id, confidence, source, context_json, created_at
                ) VALUES (?, ?, 1, ?, '{}', ?)
                """,
                (
                    ("paper:eligible-condmat", "material:kagome", "abstract", stamp),
                    ("paper:eligible-condmat", "material:kagome", "openalex", stamp),
                    ("paper:nature-biomed-review", "material:doxorubicin", "abstract", stamp),
                ),
            )
            connection.executemany(
                """
                INSERT INTO download_tasks(
                  id, canonical_paper_id, paper_version_id, status, source,
                  created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
                """,
                (
                    (
                        "task:eligible", "paper:eligible-condmat", "version:eligible-published",
                        "unpaywall", stamp, stamp, stamp,
                    ),
                    (
                        "task:excluded", "paper:nature-biomed-review", "version:nature-biomed",
                        "publisher:nature", stamp, stamp, stamp,
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO monitor_hits(
                  id, monitor_id, canonical_paper_id, paper_version_id,
                  first_matched_at, last_matched_at, match_count
                ) VALUES (
                  'hit:excluded', 'monitor:biomed', 'paper:nature-biomed-review',
                  'version:nature-biomed', ?, ?, 1
                )
                """,
                (stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO manual_review_items(
                  id, review_type, status, source_project, source_record_id,
                  candidate_paper_id, reason, confidence, payload_json, created_at
                ) VALUES (
                  'review:excluded', 'identity_conflict', 'pending', 'crossref',
                  'crossref:biomed', 'paper:nature-biomed-review',
                  'multidisciplinary journal review', 0.2, '{}', ?
                )
                """,
                (stamp,),
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.api.ui_v2_routes.connect", test_connect(database)),
                patch("backend.api.ui_v2_routes.db_path", return_value=database),
                TestClient(create_app()) as client,
            ):
                response = client.get("/api/ui-v2/analytics", params={"days": 7})

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["data_anchor"], stamp)
            self.assertEqual(payload["latest_observed_date"], stamp)
            self.assertEqual(payload["summary"]["window_papers"], 1)
            self.assertEqual(payload["summary"]["preprints"], 1)
            self.assertEqual(payload["summary"]["publications"], 1)
            self.assertEqual(sum(item["total"] for item in payload["paper_timeline"]), 1)
            self.assertEqual(sum(item["total"] for item in payload["version_event_timeline"]), 1)
            self.assertEqual(payload["journal_distribution"], [{"name": "Physical Review B", "count": 1}])
            self.assertNotIn("Nature", {item["name"] for item in payload["journal_distribution"]})
            self.assertNotIn("cancer immunotherapy", {item["name"] for item in payload["topic_trends"]})
            self.assertNotIn("Doxorubicin", {item["name"] for item in payload["top_materials"]})
            kagome = next(item for item in payload["top_materials"] if item["name"] == "Kagome lattice")
            self.assertEqual(kagome["paper_count"], 1)
            self.assertEqual(kagome["evidence_mentions"], 2)
            self.assertEqual(sum(item["hits"] for item in payload["monitor_timeline"]), 0)
            self.assertEqual(payload["data_quality"]["pending_identity_conflicts"], 0)
            self.assertEqual(
                payload["download_source_performance"],
                [{"name": "unpaywall", "attempted": 1, "completed": 1, "success_rate_pct": 100.0}],
            )

            # Analytics is a view gate only: the review candidate and its
            # evidence/task records remain available for later adjudication.
            connection = sqlite3.connect(database)
            preserved = connection.execute(
                "SELECT condmat_view_eligible FROM papers WHERE id='paper:nature-biomed-review'"
            ).fetchone()
            excluded_tasks = connection.execute(
                "SELECT COUNT(*) FROM download_tasks WHERE canonical_paper_id='paper:nature-biomed-review'"
            ).fetchone()[0]
            excluded_reviews = connection.execute(
                "SELECT COUNT(*) FROM manual_review_items WHERE candidate_paper_id='paper:nature-biomed-review'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(preserved, (0,))
            self.assertEqual(excluded_tasks, 1)
            self.assertEqual(excluded_reviews, 1)
    def test_single_paper_growth_is_shrunk_and_marked_insufficient(self) -> None:
        signal = _momentum_metrics(
            current_count=1,
            baseline_count=0,
            current_total=100,
            baseline_total=100,
            extraction_coverage=1.0,
            min_count=2,
        )
        self.assertEqual(signal["growth_ratio"], 3.0)
        self.assertEqual(signal["status"], "insufficient_evidence")
        self.assertLess(signal["reliability"], 0.25)
        self.assertLess(abs(signal["momentum"]), 15.0)

if __name__ == "__main__":
    unittest.main()