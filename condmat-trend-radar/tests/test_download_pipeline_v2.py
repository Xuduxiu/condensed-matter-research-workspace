from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.db.database import init_db
from backend.downloader.audit import candidate_attempts_for_task, download_audit_statistics
from backend.downloader.citation_export import export_citation_package
from backend.downloader.queue import (
    _record_for_task,
    _retry_after,
    claim_next_task,
    enqueue_download,
    process_download_task,
)
from backend.downloader.resolver import Resolution, resolve_legal_oa_candidates
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


def valid_pdf_bytes() -> bytes:
    import fitz

    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), "CondMat Radar real PDF fixture")
        return document.tobytes()
    finally:
        document.close()


class JsonResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class StreamResponse:
    def __init__(
        self,
        data: bytes,
        url: str,
        *,
        status_code: int = 200,
        content_type: str = "application/pdf",
    ) -> None:
        self.data = data
        self.url = url
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        midpoint = max(1, len(self.data) // 2)
        yield self.data[:midpoint]
        yield self.data[midpoint:]


class ResolverCoverageTests(unittest.TestCase):
    def test_resolver_combines_explicit_legal_oa_locations(self) -> None:
        record = {
            "doi": "10.1000/legal-oa",
            "arxiv_id": "2601.00001v2",
            "is_open_access": True,
            "pdf_url": "https://publisher.example/open.pdf",
            "raw_json": json.dumps(
                {
                    "best_oa_location": {"pdf_url": "https://repo.example/best.pdf"},
                    "locations": [
                        {"pdf_url": "https://repo.example/green.pdf", "is_oa": True},
                        {"pdf_url": "https://publisher.example/paywalled.pdf", "is_oa": False},
                    ],
                    "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                    "link": [
                        {"URL": "https://crossref.example/cc.pdf", "content-type": "application/pdf"}
                    ],
                }
            ),
        }
        unpaywall = {
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": "https://unpaywall.example/best.pdf"},
            "oa_locations": [
                {"url_for_pdf": "https://unpaywall.example/alternate.pdf"},
                {"url": "https://unpaywall.example/landing"},
            ],
        }
        with patch("httpx.get", return_value=JsonResponse(unpaywall)):
            candidates = resolve_legal_oa_candidates(
                record,
                unpaywall_email="researcher@example.org",
                timeout=1,
            )
        urls = [str(candidate.url) for candidate in candidates]
        self.assertEqual(urls[0], "https://arxiv.org/pdf/2601.00001v2.pdf")
        self.assertIn("https://export.arxiv.org/pdf/2601.00001.pdf", urls)
        self.assertIn("https://publisher.example/open.pdf", urls)
        self.assertIn("https://repo.example/best.pdf", urls)
        self.assertIn("https://repo.example/green.pdf", urls)
        self.assertIn("https://crossref.example/cc.pdf", urls)
        self.assertIn("https://unpaywall.example/best.pdf", urls)
        self.assertIn("https://unpaywall.example/alternate.pdf", urls)
        self.assertNotIn("https://publisher.example/paywalled.pdf", urls)
        self.assertNotIn("https://unpaywall.example/landing", urls)

    def test_prefixed_arxiv_id_and_related_oa_versions_are_resolved(self) -> None:
        record = {
            "arxiv_id": "arXiv:2601.00001v2",
            "alternate_versions": [
                {
                    "source": "openalex",
                    "raw_json": {
                        "best_oa_location": {
                            "pdf_url": "https://repository.example/related.pdf"
                        }
                    },
                }
            ],
        }

        candidates = resolve_legal_oa_candidates(record)
        urls = {str(candidate.url) for candidate in candidates}

        self.assertIn("https://arxiv.org/pdf/2601.00001v2.pdf", urls)
        self.assertIn("https://repository.example/related.pdf", urls)

    def test_lone_stale_metadata_url_triggers_live_openalex_refresh(self) -> None:
        record = {
            "doi": "10.1000/stale-location",
            "is_open_access": True,
            "pdf_url": "https://old.example/stale.pdf",
            "source": "crossref",
        }
        with patch(
            "backend.downloader.resolver._live_openalex_urls",
            return_value=["https://current.example/open.pdf"],
        ) as openalex, patch(
            "backend.downloader.resolver._live_crossref_urls",
            return_value=[],
        ) as crossref:
            candidates = resolve_legal_oa_candidates(record, timeout=1)

        self.assertEqual(
            [str(candidate.url) for candidate in candidates],
            ["https://old.example/stale.pdf", "https://current.example/open.pdf"],
        )
        openalex.assert_called_once()
        crossref.assert_not_called()

    def test_selected_version_inherits_canonical_doi_and_raw_oa_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            repository = LibraryRepository(connection)
            canonical = repository.upsert_version(
                {
                    "title": "Canonical metadata fallback",
                    "doi": "10.1000/canonical-doi",
                    "arxiv_id": "2601.00002",
                    "raw_json": {
                        "best_oa_location": {
                            "pdf_url": "https://repository.example/canonical.pdf"
                        }
                    },
                },
                source="openalex",
                source_record_id="openalex-canonical",
            )
            selected = repository.upsert_version(
                {
                    "title": "Canonical metadata fallback",
                    "arxiv_id": "2601.00002",
                    "raw_json": {},
                },
                source="arxiv",
                source_record_id="arxiv-selected",
            )
            task = {
                "canonical_paper_id": canonical.canonical_paper_id,
                "paper_version_id": selected.paper_version_id,
            }
            record = _record_for_task(connection, task)
            connection.close()

        self.assertEqual(record["doi"], "10.1000/canonical-doi")
        self.assertTrue(record["paper_raw_json"])
        related_urls = {
            str(candidate.url)
            for candidate in resolve_legal_oa_candidates(record)
        }
        self.assertIn("https://repository.example/canonical.pdf", related_urls)

    def test_retry_after_accepts_standard_http_date(self) -> None:
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        delay = _retry_after(type("Response", (), {"headers": {"Retry-After": format_datetime(future)}})())
        self.assertIsNotNone(delay)
        self.assertGreater(delay or 0, 50)
        self.assertLessEqual(delay or 0, 60)

class DownloadAuditAndZoteroTests(unittest.TestCase):
    def test_invalid_candidate_falls_back_and_success_is_packaged_for_zotero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connection_for(root / "radar.sqlite")
            try:
                paper = LibraryRepository(connection).upsert_version(
                    {"title": "Fallback PDF", "doi": "10.1000/fallback", "authors": ["A. Author"]},
                    source="crossref",
                    source_record_id="fallback",
                )
                enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
                connection.commit()
                task = claim_next_task(connection)
                self.assertIsNotNone(task)
                candidates = [
                    Resolution("https://first.example/item", "publisher_oa", True, "explicit OA metadata"),
                    Resolution(
                        "https://second.example/paper.pdf?token=do-not-store&download=1",
                        "repository_oa",
                        True,
                        "repository copy",
                    ),
                ]
                html = b"<html><body>not a PDF</body></html>" + b"x" * 512
                responses = [
                    StreamResponse(html, candidates[0].url or "", content_type="text/html"),
                    StreamResponse(valid_pdf_bytes(), candidates[1].url or ""),
                ]
                with patch(
                    "backend.downloader.queue.resolve_legal_oa_candidates",
                    return_value=candidates,
                ), patch("httpx.stream", side_effect=responses):
                    result = process_download_task(connection, task or {}, root / "pdf")

                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["source"], "repository_oa")
                self.assertEqual(result["candidates_tried"], 2)
                self.assertEqual(result["validation"]["page_count"], 1)
                attempts = candidate_attempts_for_task(connection, str(task["id"]))
                self.assertEqual([item["status"] for item in attempts], ["rejected", "completed"])
                self.assertEqual(attempts[0]["failure_class"], "not_pdf_content")
                self.assertNotIn("do-not-store", attempts[1]["source_url"])
                self.assertEqual(
                    connection.execute("SELECT validation_status FROM paper_files").fetchone()[0],
                    "valid",
                )

                package = export_citation_package(
                    connection,
                    [paper.paper_version_id],
                    root / "exports",
                )
                self.assertEqual(package["attachment_count"], 1)
                ris = Path(package["ris_path"]).read_text(encoding="utf-8")
                self.assertIn("L1  - pdf/001_", ris)
                with zipfile.ZipFile(package["zip_path"]) as archive:
                    names = archive.namelist()
                    self.assertIn("selected_papers.ris", names)
                    self.assertEqual(len([name for name in names if name.startswith("pdf/")]), 1)
                    self.assertIsNone(archive.testzip())

                stats = download_audit_statistics(connection)
                repository_stats = next(
                    item for item in stats["candidate_sources"] if item["source"] == "repository_oa"
                )
                self.assertEqual(repository_stats["attempts"], 1)
                self.assertEqual(repository_stats["successes"], 1)
            finally:
                connection.close()

    def test_all_invalid_pdf_candidates_are_retryable_and_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connection_for(root / "radar.sqlite")
            try:
                paper = LibraryRepository(connection).upsert_version(
                    {"title": "Invalid candidates"},
                    source="manual",
                    source_record_id="invalid",
                )
                enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
                connection.commit()
                task = claim_next_task(connection)
                candidates = [Resolution("https://example.org/not-pdf", "metadata", True, "OA flag")]
                response = StreamResponse(
                    b"<html>blocked</html>" + b"x" * 512,
                    candidates[0].url or "",
                    content_type="text/html",
                )
                with patch(
                    "backend.downloader.queue.resolve_legal_oa_candidates",
                    return_value=candidates,
                ), patch("httpx.stream", return_value=response):
                    result = process_download_task(connection, task or {}, root / "pdf")
                self.assertEqual(result["status"], "retryable_failed")
                self.assertEqual(result["failure_class"], "not_pdf_content")
                self.assertEqual(result["candidate_attempts"][0]["failure_class"], "not_pdf_content")
            finally:
                connection.close()

    def test_no_legal_location_routes_to_manual_review_without_paywall_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connection_for(root / "radar.sqlite")
            try:
                paper = LibraryRepository(connection).upsert_version(
                    {"title": "Paywalled only", "doi": "10.1000/paywall"},
                    source="crossref",
                    source_record_id="paywall",
                )
                enqueue_download(connection, paper.canonical_paper_id, paper.paper_version_id)
                connection.commit()
                task = claim_next_task(connection)
                with patch(
                    "backend.downloader.queue.resolve_legal_oa_candidates",
                    return_value=[],
                ), patch(
                    "backend.downloader.queue.resolve_legal_oa_url",
                    return_value=Resolution(None, "none", False, "none"),
                ), patch("httpx.stream") as stream:
                    result = process_download_task(connection, task or {}, root / "pdf")
                self.assertEqual(result["status"], "manual_review")
                self.assertEqual(result["failure_class"], "no_legal_oa_location")
                stream.assert_not_called()
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
