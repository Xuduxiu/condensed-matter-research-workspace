from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.db.database import init_db
from backend.downloader.citation_export import export_citation_package
from backend.downloader.queue import (
    claim_next_task,
    enqueue_download,
    process_download_task,
    recover_interrupted_tasks,
)
from backend.downloader.resolver import Resolution, resolve_legal_oa_candidates
from backend.downloader.retry import classify_failure, retry_delay
from backend.ingest.lock import IngestLock, IngestLockError
from backend.library.deduplication import normalize_doi, parse_arxiv_id
from backend.library.local_search import rebuild_search_index, search_library
from backend.library.pdf_importer import import_existing_pdfs
from backend.library.repository import LibraryRepository, seed_radar_versions
from backend.library.version_linker import link_preprints_and_publications
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import _record_cursor_attempt


def unified_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    connection.commit()
    return connection


def valid_pdf_bytes() -> bytes:
    import fitz

    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), "CondMat Radar PDF validation fixture")
        return document.tobytes()
    finally:
        document.close()


class UnifiedIdentityTests(unittest.TestCase):
    def test_doi_arxiv_normalization_and_idempotent_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = unified_connection(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            first = repository.upsert_version(
                {"title": "Paper", "doi": "https://doi.org/10.1000/ABC", "authors": ["A. Smith"]},
                source="legacy_intake",
                source_record_id="one",
            )
            repeat = repository.upsert_version(
                {"title": "Paper revised", "doi": "doi:10.1000/abc", "authors": ["A. Smith"]},
                source="legacy_intake",
                source_record_id="one",
            )
            second_source = repository.upsert_version(
                {"title": "Paper from another source", "doi": "10.1000/abc"},
                source="manual",
                source_record_id="two",
            )
            connection.commit()
            self.assertEqual(normalize_doi("https://doi.org/10.1000/ABC"), "10.1000/abc")
            self.assertEqual(parse_arxiv_id("https://arxiv.org/pdf/2401.00001v3.pdf"), ("2401.00001", 3))
            self.assertEqual(parse_arxiv_id("arXiv:2401.00001v4"), ("2401.00001", 4))
            self.assertEqual(
                parse_arxiv_id("https://export.arxiv.org/pdf/cond-mat/9901001v2.pdf?download=1"),
                ("cond-mat/9901001", 2),
            )
            self.assertEqual(first.paper_version_id, repeat.paper_version_id)
            self.assertEqual(first.canonical_paper_id, second_source.canonical_paper_id)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0], 2)
            connection.close()

    def test_explicit_journal_doi_links_preprint_to_published_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = unified_connection(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            published = repository.upsert_version(
                {"title": "Published work", "doi": "10.5555/work", "year": 2025},
                source="crossref",
                source_record_id="pub",
            )
            preprint = repository.upsert_version(
                {"title": "Published work", "arxiv_id": "2501.00001v2", "journal_reference": "Journal (2025), doi:10.5555/work"},
                source="arxiv",
                source_record_id="pre",
            )
            plan = link_preprints_and_publications(connection, dry_run=True)
            applied = link_preprints_and_publications(connection, dry_run=False)
            connection.commit()
            linked_canonical = connection.execute("SELECT canonical_paper_id FROM paper_versions WHERE id=?", (preprint.paper_version_id,)).fetchone()[0]
            self.assertEqual(plan["links_planned"], 1)
            self.assertEqual(applied["links_created"], 1)
            self.assertEqual(linked_canonical, published.canonical_paper_id)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_version_links").fetchone()[0], 1)
            connection.close()

    def test_version_link_batches_publish_before_each_safe_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = unified_connection(database)
            repository = LibraryRepository(connection)
            for index in range(5):
                doi = f"10.5555/batched-{index}"
                title = f"Batched publication {index}"
                repository.upsert_version(
                    {"title": title, "doi": doi, "year": 2025},
                    source="crossref",
                    source_record_id=f"pub-{index}",
                )
                repository.upsert_version(
                    {
                        "title": title,
                        "arxiv_id": f"2501.{index:05d}v1",
                        "journal_reference": f"Journal (2025), doi:{doi}",
                    },
                    source="arxiv",
                    source_record_id=f"pre-{index}",
                )
            connection.commit()
            callbacks: list[tuple[int, int, int, int]] = []
            visible_before_commit: list[int] = []

            def progress(processed: int, total: int, links_seen: int, reviews_seen: int) -> None:
                observer = sqlite3.connect(database)
                try:
                    visible_before_commit.append(
                        observer.execute(
                            "SELECT COUNT(*) FROM paper_version_links WHERE link_type='preprint_to_publication'"
                        ).fetchone()[0]
                    )
                finally:
                    observer.close()
                callbacks.append((processed, total, links_seen, reviews_seen))

            result = link_preprints_and_publications(
                connection,
                dry_run=False,
                batch_size=2,
                progress_callback=progress,
                commit_batches=True,
            )
            connection.close()

            observer = sqlite3.connect(database)
            try:
                linked = observer.execute(
                    "SELECT COUNT(*) FROM paper_version_links WHERE link_type='preprint_to_publication'"
                ).fetchone()[0]
            finally:
                observer.close()

        self.assertEqual(callbacks, [(2, 5, 2, 0), (4, 5, 4, 0), (5, 5, 5, 0)])
        self.assertEqual(visible_before_commit, [0, 2, 4])
        self.assertEqual(result["processed"], 5)
        self.assertEqual(result["links_created"], 5)
        self.assertEqual(linked, 5)
    def test_additive_schema_keeps_trend_tables_and_seed_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.sqlite"
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            init_db(connection)
            connection.execute("INSERT INTO papers(id, title, data_mode) VALUES ('legacy', 'Legacy trend paper', 'real')")
            connection.execute("INSERT INTO paper_terms(paper_id, term, term_type, normalized_term) VALUES ('legacy', 'graphene', 'material', 'graphene')")
            apply_unified_schema(connection)
            first = seed_radar_versions(connection)
            second = seed_radar_versions(connection)
            connection.commit()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_terms").fetchone()[0], 1)
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.close()


class UnifiedFileSearchAndExportTests(unittest.TestCase):
    def test_pdf_hash_dedup_and_source_path_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_a = root / "a.pdf"
            source_b = root / "nested" / "b.pdf"
            source_b.parent.mkdir()
            content = b"%PDF-1.4\n" + b"x" * 256
            source_a.write_bytes(content)
            source_b.write_bytes(content)
            connection = unified_connection(root / "radar.sqlite")
            result = import_existing_pdfs(connection, [source_a, source_b], root / "library", dry_run=False, extract_text=False)
            connection.commit()
            self.assertEqual(result["files_seen"], 2)
            self.assertEqual(result["unique_hashes"], 1)
            self.assertEqual(result["duplicate_copies"], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_file_sources").fetchone()[0], 2)
            canonical_path = Path(connection.execute("SELECT absolute_path FROM paper_files").fetchone()[0])
            self.assertTrue(canonical_path.is_file())
            connection.close()

    def test_full_search_rebuild_remains_caller_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = unified_connection(database)
            paper = LibraryRepository(connection).upsert_version(
                {"title": "AtomicOldToken", "publication_date": "2026-01-01"},
                source="manual",
                source_record_id="atomic-search",
                default_condmat_eligible=True,
            )
            rebuild_search_index(connection)
            connection.commit()

            connection.execute(
                "UPDATE paper_versions SET title='AtomicNewToken' WHERE id=?",
                (paper.paper_version_id,),
            )
            rebuild_search_index(connection)
            local_counts = (
                connection.execute(
                    "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'AtomicOldToken'"
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'AtomicNewToken'"
                ).fetchone()[0],
            )

            observer = sqlite3.connect(database)
            try:
                visible_counts = (
                    observer.execute(
                        "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'AtomicOldToken'"
                    ).fetchone()[0],
                    observer.execute(
                        "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'AtomicNewToken'"
                    ).fetchone()[0],
                )
            finally:
                observer.close()
            connection.rollback()
            connection.close()

            observer = sqlite3.connect(database)
            try:
                rolled_back_counts = (
                    observer.execute(
                        "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'AtomicOldToken'"
                    ).fetchone()[0],
                    observer.execute(
                        "SELECT COUNT(*) FROM library_fts WHERE library_fts MATCH 'AtomicNewToken'"
                    ).fetchone()[0],
                )
            finally:
                observer.close()

        self.assertEqual(local_counts, (0, 1))
        self.assertEqual(visible_counts, (1, 0))
        self.assertEqual(rolled_back_counts, (1, 0))
    def test_metadata_and_fulltext_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = unified_connection(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            paper = repository.upsert_version(
                {"title": "Topology in ZrTe5", "abstract": "transport evidence", "doi": "10.1000/zrte5", "year": 2024, "authors": ["Jane Smith"]},
                source="crossref",
                source_record_id="paper",
            )
            file_id = "file:test"
            connection.execute(
                "INSERT INTO paper_files(id, canonical_paper_id, paper_version_id, absolute_path, sha256, file_size, mime_type, extraction_status, validation_status, created_at, updated_at) VALUES (?, ?, ?, 'x.pdf', ?, 200, 'application/pdf', 'completed', 'valid_pdf_header', 'now', 'now')",
                (file_id, paper.canonical_paper_id, paper.paper_version_id, "0" * 64),
            )
            connection.execute(
                "INSERT INTO paper_file_text(paper_file_id, extractor, text_content, text_sha256, extracted_at) VALUES (?, 'test', 'hidden Weyl fermion evidence', ?, 'now')",
                (file_id, "1" * 64),
            )
            rebuild_search_index(connection)
            title_result = search_library(connection, "ZrTe5")
            text_result = search_library(connection, "Weyl fermion")
            author_result = search_library(connection, author="jane smith")
            self.assertEqual(title_result["count"], 1)
            self.assertEqual(text_result["count"], 1)
            self.assertEqual(author_result["count"], 1)
            connection.close()

    def test_zotero_ris_package_preserves_doi_and_local_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = unified_connection(root / "radar.sqlite")
            paper = LibraryRepository(connection).upsert_version(
                {"title": "RIS paper", "doi": "10.1000/ris", "year": 2024, "authors": ["Jane Smith"]},
                source="crossref",
                source_record_id="ris",
            )
            pdf_path = root / "paper.pdf"
            pdf_content = valid_pdf_bytes()
            pdf_path.write_bytes(pdf_content)
            connection.execute(
                "INSERT INTO paper_files(id, canonical_paper_id, paper_version_id, absolute_path, sha256, file_size, mime_type, extraction_status, validation_status, created_at, updated_at) VALUES ('file:ris', ?, ?, ?, ?, ?, 'application/pdf', 'pending', 'valid_pdf_header', 'now', 'now')",
                (paper.canonical_paper_id, paper.paper_version_id, str(pdf_path), "2" * 64, len(pdf_content)),
            )
            result = export_citation_package(connection, [paper.paper_version_id], root / "exports")
            ris = Path(result["ris_path"]).read_text(encoding="utf-8")
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertIn("DO  - 10.1000/ris", ris)
            self.assertIn("L1  - ", ris)
            self.assertEqual(manifest["zotero_import"]["mode"], "ris_with_packaged_relative_pdf")
            self.assertEqual(result["zotero_mode"], "ris_with_packaged_relative_pdf")
            self.assertTrue(Path(result["zip_path"]).is_file())
            self.assertEqual(result["attachment_count"], 1)
            self.assertIn("L1  - pdf/001_", ris)
            with zipfile.ZipFile(result["zip_path"]) as archive:
                self.assertTrue(any(name.startswith("pdf/001_") for name in archive.namelist()))
            connection.close()


class QueueCursorLockRetryTests(unittest.TestCase):
    def test_queue_claim_recovery_and_idempotent_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = unified_connection(Path(directory) / "radar.sqlite")
            paper = LibraryRepository(connection).upsert_version({"title": "Queue"}, source="manual", source_record_id="queue")
            first = enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
            second = enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
            connection.commit()
            self.assertEqual(first["id"], second["id"])
            claimed = claim_next_task(connection)
            self.assertEqual(claimed["status"], "resolving")
            recovered = recover_interrupted_tasks(connection)
            connection.commit()
            self.assertEqual(recovered, 1)
            self.assertEqual(connection.execute("SELECT status FROM download_tasks").fetchone()[0], "retryable_failed")
            connection.close()

    def test_legal_oa_download_validates_pdf_and_completes_task(self) -> None:
        class FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                yield valid_pdf_bytes()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = unified_connection(root / "radar.sqlite")
            paper = LibraryRepository(connection).upsert_version(
                {"title": "OA", "arxiv_id": "2501.00001"},
                source="arxiv",
                source_record_id="oa",
            )
            enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
            connection.commit()
            task = claim_next_task(connection)
            with patch(
                "backend.downloader.queue.resolve_legal_oa_url",
                return_value=Resolution("https://arxiv.org/pdf/2501.00001.pdf", "arxiv", True, "test"),
            ), patch("httpx.stream", return_value=FakeResponse()):
                result = process_download_task(connection, task, root / "pdf")
            self.assertEqual(result["status"], "completed")
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(connection.execute("SELECT status FROM download_tasks").fetchone()[0], "completed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0], 1)
            connection.close()

    def test_resolver_uses_crossref_cc_pdf_link(self) -> None:
        candidates = resolve_legal_oa_candidates({
            "doi": "10.1000/cc-paper",
            "is_open_access": True,
            "raw_json": json.dumps({
                "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                "link": [{"URL": "https://example.org/open-paper.pdf", "content-type": "application/pdf"}],
            }),
        })
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, "crossref_cc_license")
        self.assertEqual(candidates[0].url, "https://example.org/open-paper.pdf")
    def test_failure_cursor_does_not_advance_last_successful_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = unified_connection(Path(directory) / "radar.sqlite")
            _record_cursor_attempt(connection, "openalex", status="ok", cursor="2026-01-01")
            _record_cursor_attempt(connection, "openalex", status="failed", cursor="2026-02-01", error="network")
            row = connection.execute("SELECT * FROM source_cursors WHERE source_name='openalex'").fetchone()
            self.assertEqual(row["last_successful_cursor"], "2026-01-01")
            self.assertEqual(row["status"], "failed")
            connection.close()

    def test_single_instance_lock_and_retry_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "daily.lock"
            first = IngestLock(lock_path)
            second = IngestLock(lock_path)
            first.acquire()
            try:
                with self.assertRaises(IngestLockError):
                    second.acquire()
            finally:
                first.release()
            self.assertEqual(retry_delay(1), 5.0)
            self.assertEqual(retry_delay(3), 20.0)
            self.assertTrue(classify_failure(1, http_status=503).retryable)
            self.assertFalse(classify_failure(5, http_status=503).retryable)
            self.assertFalse(classify_failure(1, http_status=404).retryable)


if __name__ == "__main__":
    unittest.main()
