from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.db.strict_condmat import classify_strict_condmat
from backend.ingest_real import _commit_ingest_batch, run


FIELD_PAPER = {
    "id": "https://openalex.org/W-field-only",
    "openalex_id": "https://openalex.org/W-field-only",
    "doi": "10.5555/field-only",
    "title": "Quantum transport in a correlated layered material",
    "abstract": "A condensed matter superconductivity and electronic transport study.",
    "publication_date": "2026-08-08",
    "journal": "Journal Outside The Curated Eighteen",
    "source": "openalex",
    "source_scope": "published",
    "concepts": ["Condensed Matter Physics", "Superconductivity"],
    "raw_json": {"id": "https://openalex.org/W-field-only"},
}


class FakeFieldOpenAlex:
    next_cursor = ""

    def __init__(self, **_kwargs):
        pass

    def fetch_condensed_matter_page(self, *_args, **_kwargs):
        return {"results": [dict(FIELD_PAPER)], "meta": {"next_cursor": self.next_cursor}}

    def _normalize_work(self, raw, _journal):
        return dict(raw)


class OpenAlexFieldIngestTests(unittest.TestCase):
    def _args(self, *, max_pages: int = 0, incremental: bool = True) -> argparse.Namespace:
        return argparse.Namespace(
            baseline_from="2026-08-08",
            baseline_to="2026-08-09",
            scope="arxiv_live",
            journals="",
            limit_per_journal=0,
            resume=True,
            force_refresh=False,
            dry_run=False,
            mailto=None,
            include_arxiv=False,
            include_openalex_field=True,
            include_crossref=False,
            crossref_rows=1000,
            max_pages=max_pages,
            sleep_seconds=0,
            timeout=1,
            incremental=incremental,
        )

    def _run(
        self,
        database: Path,
        client_type: type[FakeFieldOpenAlex],
        *,
        max_pages: int = 0,
        incremental: bool = True,
    ):
        @contextmanager
        def test_connect(_path=None):
            connection = sqlite3.connect(database)
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

        with (
            patch("backend.ingest_real.connect", test_connect),
            patch("backend.ingest_real.ensure_data_layout", return_value={}),
            patch("backend.ingest_real.logs_dir", return_value=database.parent),
            patch("backend.ingest_real.write_ingest_log"),
            patch("backend.ingest_real.OpenAlexClient", client_type),
        ):
            return run(self._args(max_pages=max_pages, incremental=incremental))

    def test_field_stream_discovers_paper_outside_curated_journal_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, FakeFieldOpenAlex)
            connection = sqlite3.connect(database)
            paper = connection.execute(
                "SELECT journal, condmat_view_eligible FROM papers WHERE doi='10.5555/field-only'"
            ).fetchone()
            versions = connection.execute(
                "SELECT source FROM paper_versions WHERE source='openalex'"
            ).fetchall()
            connection.close()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_counts"]["openalex_field"]["fetched"], 1)
        self.assertEqual(result["source_counts"]["openalex_field"]["complete"], 1)
        self.assertEqual(paper, ("Journal Outside The Curated Eighteen", 1))
        self.assertEqual(versions, [("openalex",)])

    def test_weak_topic_candidate_is_retained_but_excluded_from_hotspot_corpus(self) -> None:
        weak = {
            "id": "https://openalex.org/W-weak-topic",
            "openalex_id": "https://openalex.org/W-weak-topic",
            "title": "A four dimensional code on a lattice",
            "abstract": "An algebraic construction for coding theory.",
            "publication_date": "2026-08-08",
            "journal": "Zenodo",
            "source": "openalex",
            "raw_json": {
                "id": "https://openalex.org/W-weak-topic",
                "primary_topic": {
                    "id": "https://openalex.org/T10037",
                    "display_name": "Physics of Superconductivity and Magnetism",
                    "score": 0.2265,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                },
                "topics": [{
                    "id": "https://openalex.org/T10037",
                    "display_name": "Physics of Superconductivity and Magnetism",
                    "score": 0.2265,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                }],
            },
        }

        class WeakFieldOpenAlex(FakeFieldOpenAlex):
            def fetch_condensed_matter_page(self, *_args, **_kwargs):
                return {"results": [dict(weak)], "meta": {"next_cursor": ""}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, WeakFieldOpenAlex)
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason, raw_json FROM papers"
            ).fetchone()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM paper_versions WHERE source='openalex'"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_counts"]["openalex_field"]["review_candidates"], 1)
        self.assertEqual(result["review_candidates_count"], 1)
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["counts"]["review_candidates"], 1)
        self.assertEqual(row[0], 0)
        self.assertIn("unverified", row[1])
        self.assertIn('"strict_eligible": false', row[2])
        self.assertEqual(version_count, 1)
        self.assertEqual(result["affected_paper_ids"], result["metadata_changed_paper_ids"])
        self.assertEqual(len(result["affected_paper_ids"]), 1)
        self.assertEqual(len(result["metadata_changed_paper_ids"]), 1)

    def test_high_topic_zenodo_record_is_retained_but_source_gated(self) -> None:
        zenodo = {
            "id": "https://openalex.org/W-zenodo-high-topic",
            "openalex_id": "https://openalex.org/W-zenodo-high-topic",
            "doi": "10.5281/zenodo.9999999",
            "title": "Type IV unification through superconductivity",
            "abstract": "A superconductivity and quantum materials proposal.",
            "publication_date": "2026-08-08",
            "journal": "Zenodo",
            "source": "openalex",
            "source_scope": "preprint",
            "raw_json": {
                "id": "https://openalex.org/W-zenodo-high-topic",
                "doi": "https://doi.org/10.5281/zenodo.9999999",
                "type": "preprint",
                "type_crossref": "posted-content",
                "primary_location": {
                    "landing_page_url": "https://zenodo.org/records/9999999",
                    "source": {
                        "id": "https://openalex.org/S-zenodo",
                        "display_name": "Zenodo",
                        "type": "repository",
                    },
                },
                "locations": [{
                    "landing_page_url": "https://zenodo.org/records/9999999",
                    "source": {
                        "id": "https://openalex.org/S-zenodo",
                        "display_name": "Zenodo",
                        "type": "repository",
                    },
                }],
                "primary_topic": {
                    "id": "https://openalex.org/T10037",
                    "display_name": "Physics of Superconductivity and Magnetism",
                    "score": 0.99,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                },
                "topics": [{
                    "id": "https://openalex.org/T10037",
                    "display_name": "Physics of Superconductivity and Magnetism",
                    "score": 0.99,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                }],
            },
        }

        class ZenodoOpenAlex(FakeFieldOpenAlex):
            def fetch_condensed_matter_page(self, *_args, **_kwargs):
                return {"results": [dict(zenodo)], "meta": {"next_cursor": ""}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, ZenodoOpenAlex)
            connection = sqlite3.connect(database)
            paper = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason, raw_json FROM papers"
            ).fetchone()
            versions = connection.execute(
                "SELECT source, raw_json FROM paper_versions WHERE source='openalex'"
            ).fetchall()
            connection.close()

        self.assertEqual(result["source_counts"]["openalex_field"]["review_candidates"], 1)
        self.assertEqual(result["review_candidates_count"], 1)
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["counts"]["review_candidates"], 1)
        self.assertEqual(paper[0], 0)
        self.assertIn("openalex-source-excluded:repository:zenodo", paper[1])
        self.assertIn('"primary_source_type": "repository"', paper[2])
        self.assertEqual(len(versions), 1)
        self.assertIn("W-zenodo-high-topic", versions[0][1])

    def test_arxiv_repository_record_passes_scientific_gate(self) -> None:
        arxiv = {
            "id": "https://openalex.org/W-arxiv",
            "openalex_id": "https://openalex.org/W-arxiv",
            "title": "Superconductivity in a layered quantum material",
            "abstract": "We study superconductivity and electronic transport.",
            "publication_date": "2026-08-08",
            "journal": "arXiv",
            "source": "openalex",
            "source_scope": "preprint",
            "raw_json": {
                "id": "https://openalex.org/W-arxiv",
                "type": "preprint",
                "primary_location": {
                    "landing_page_url": "https://arxiv.org/abs/2608.00001",
                    "source": {
                        "display_name": "arXiv",
                        "type": "repository",
                    },
                },
                "topics": [{
                    "id": "https://openalex.org/T-condmat",
                    "display_name": "Condensed Matter Physics",
                    "score": 0.9,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                }],
            },
        }

        class ArxivOpenAlex(FakeFieldOpenAlex):
            def fetch_condensed_matter_page(self, *_args, **_kwargs):
                return {"results": [dict(arxiv)], "meta": {"next_cursor": ""}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, ArxivOpenAlex)
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason FROM papers"
            ).fetchone()
            connection.close()

        self.assertEqual(result["source_counts"]["openalex_field"]["eligible"], 1)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["review_candidates_count"], 0)
        self.assertEqual(result["counts"]["eligible"], 1)
        self.assertEqual(row[0], 1)
        self.assertIn("openalex-source-accepted:repository:arxiv", row[1])

    def test_journal_record_passes_scientific_gate(self) -> None:
        journal = {
            "id": "https://openalex.org/W-journal",
            "openalex_id": "https://openalex.org/W-journal",
            "doi": "10.1103/physrevb.1.1",
            "title": "Superconductivity in a correlated layered material",
            "abstract": "A condensed matter superconductivity study.",
            "publication_date": "2026-08-08",
            "journal": "Physical Review B",
            "source": "openalex",
            "source_scope": "published",
            "raw_json": {
                "id": "https://openalex.org/W-journal",
                "doi": "https://doi.org/10.1103/physrevb.1.1",
                "type": "article",
                "type_crossref": "journal-article",
                "primary_location": {
                    "landing_page_url": "https://journals.aps.org/prb/abstract/example",
                    "source": {
                        "display_name": "Physical Review B",
                        "type": "journal",
                    },
                },
            },
        }

        class JournalOpenAlex(FakeFieldOpenAlex):
            def fetch_condensed_matter_page(self, *_args, **_kwargs):
                return {"results": [dict(journal)], "meta": {"next_cursor": ""}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, JournalOpenAlex)
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason FROM papers"
            ).fetchone()
            connection.close()

        self.assertEqual(result["source_counts"]["openalex_field"]["eligible"], 1)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["review_candidates_count"], 0)
        self.assertEqual(result["counts"]["eligible"], 1)
        self.assertEqual(row[0], 1)
        self.assertIn("openalex-source-accepted:journal:physical-review-b", row[1])
    def test_topic_only_nature_communications_robot_is_review_only(self) -> None:
        microrobot = {
            "id": "https://openalex.org/W-microrobot",
            "openalex_id": "https://openalex.org/W-microrobot",
            "doi": "10.1038/s41467-026-76462-y",
            "title": "Controlled flight of high-thrust ultralight ion-propelled microrobot with integrated sensing",
            "abstract": "An ultralight robot uses ion propulsion for controlled flight and onboard sensing.",
            "publication_date": "2026-08-08",
            "journal": "Nature Communications",
            "source": "openalex",
            "source_scope": "published",
            "raw_json": {
                "id": "https://openalex.org/W-microrobot",
                "doi": "https://doi.org/10.1038/s41467-026-76462-y",
                "type": "article",
                "type_crossref": "journal-article",
                "primary_location": {
                    "landing_page_url": "https://www.nature.com/articles/s41467-026-76462-y",
                    "source": {
                        "display_name": "Nature Communications",
                        "type": "journal",
                    },
                },
                "primary_topic": {
                    "id": "https://openalex.org/T-false-positive",
                    "display_name": "Condensed Matter Physics",
                    "score": 0.91,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                },
                "topics": [{
                    "id": "https://openalex.org/T-false-positive",
                    "display_name": "Condensed Matter Physics",
                    "score": 0.91,
                    "subfield": {"id": "https://openalex.org/subfields/3104"},
                }],
            },
        }

        class MicrorobotOpenAlex(FakeFieldOpenAlex):
            def fetch_condensed_matter_page(self, *_args, **_kwargs):
                return {"results": [dict(microrobot)], "meta": {"next_cursor": ""}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, MicrorobotOpenAlex)
            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT doi, condmat_view_eligible, condmat_view_reason, raw_json FROM papers"
            ).fetchone()
            versions = connection.execute(
                "SELECT COUNT(*) FROM paper_versions WHERE source='openalex'"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(row[0], "10.1038/s41467-026-76462-y")
        self.assertEqual(row[1], 0)
        self.assertIn("generalist-topic-requires-text", row[2])
        self.assertIn('"topic_only_generalist": true', row[3])
        self.assertEqual(versions, 1)
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["review_candidates_count"], 1)

    def test_generalist_generic_method_term_does_not_repromote_but_nb3cl8_remains(self) -> None:
        robot = {
            "data_mode": "real",
            "journal": "Nature Communications",
            "source": "openalex",
            "source_scope": "published",
            "condmat_confidence": "high",
            "title": "Controlled flight of an ion-propelled microrobot",
            "abstract": "We report propulsion, sensing, and transport control for a flying robot.",
        }
        flag, reason = classify_strict_condmat(
            robot,
            [{"normalized_term": "transport"}],
        )
        self.assertEqual(flag, 0)
        self.assertEqual(reason, "generalist_requires_condmat_text")

        nb3cl8 = {
            **robot,
            "title": "Correlated magnetism in the layered material Nb3Cl8",
            "abstract": "We reveal quantum magnetism and a spin liquid regime in Nb3Cl8.",
        }
        flag, reason = classify_strict_condmat(nb3cl8, [])
        self.assertEqual(flag, 1)
        self.assertIn("generalist_text", reason)

    def test_field_page_cap_is_partial_and_preserves_fetched_records(self) -> None:
        class CappedFieldOpenAlex(FakeFieldOpenAlex):
            next_cursor = "cursor-2"

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            result = self._run(database, CappedFieldOpenAlex, max_pages=1)
            connection = sqlite3.connect(database)
            stored = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            connection.close()

        self.assertEqual(stored, 1)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["source_completeness"]["watermark_safe_to_advance"])
        self.assertTrue(any(item.get("source") == "openalex_field" and item.get("stop_reason") == "max_pages" for item in result["errors"]))


    def test_incremental_quality_reclassification_uses_changed_subset(self) -> None:
        quality_result = {
            "processed_canonicals": 1,
            "repository_candidates": 0,
            "downgraded": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            with patch(
                "backend.ingest_real.reclassify_openalex_repository_quality",
                return_value=dict(quality_result),
            ) as reclassify:
                result = self._run(database, FakeFieldOpenAlex)

        reclassify.assert_called_once()
        subset = reclassify.call_args.kwargs.get("canonical_ids")
        self.assertEqual(subset, result["affected_paper_ids"])
        self.assertEqual(len(subset), 1)
        report = result["source_quality_reclassification"]
        self.assertEqual(report["mode"], "incremental_subset")
        self.assertEqual(report["subset_size"], 1)

    def test_incremental_empty_change_set_skips_quality_reclassification(self) -> None:
        class EmptyFieldOpenAlex(FakeFieldOpenAlex):
            def fetch_condensed_matter_page(self, *_args, **_kwargs):
                return {"results": [], "meta": {"next_cursor": ""}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            with patch(
                "backend.ingest_real.reclassify_openalex_repository_quality"
            ) as reclassify:
                result = self._run(database, EmptyFieldOpenAlex)

        reclassify.assert_not_called()
        report = result["source_quality_reclassification"]
        self.assertTrue(report["skipped"])
        self.assertEqual(report["reason"], "no_changed_canonicals")
        self.assertEqual(report["mode"], "incremental_subset")
        self.assertEqual(report["subset_size"], 0)

    def test_full_ingest_quality_reclassification_still_scans_full_corpus(self) -> None:
        quality_result = {
            "processed_canonicals": 1,
            "repository_candidates": 0,
            "downgraded": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            with (
                patch(
                    "backend.ingest_real.reclassify_openalex_repository_quality",
                    return_value=dict(quality_result),
                ) as reclassify,
                patch("backend.ingest_real.rebuild_paper_terms", return_value=0),
                patch("backend.ingest_real.rebuild_term_month_stats", return_value=0),
                patch("backend.ingest_real.rebuild_lifecycle", return_value=0),
                patch("backend.ingest_real.cooccurrence_network", return_value={}),
            ):
                result = self._run(
                    database,
                    FakeFieldOpenAlex,
                    incremental=False,
                )

        reclassify.assert_called_once()
        self.assertNotIn("canonical_ids", reclassify.call_args.kwargs)
        report = result["source_quality_reclassification"]
        self.assertEqual(report["mode"], "full_corpus")
        self.assertIsNone(report["subset_size"])

class IngestBatchCommitTests(unittest.TestCase):
    def test_large_provider_page_releases_writer_lock_in_bounded_batches(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.commits = 0

            def commit(self) -> None:
                self.commits += 1

        connection = FakeConnection()
        pending = 0
        for _ in range(24):
            pending = _commit_ingest_batch(connection, pending, dry_run=False, batch_size=25)
        self.assertEqual((pending, connection.commits), (24, 0))
        pending = _commit_ingest_batch(connection, pending, dry_run=False, batch_size=25)
        self.assertEqual((pending, connection.commits), (0, 1))
        self.assertEqual(
            _commit_ingest_batch(connection, 7, dry_run=True, batch_size=1),
            7,
        )
        self.assertEqual(connection.commits, 1)

if __name__ == "__main__":
    unittest.main()