from __future__ import annotations

import json
import sqlite3
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from backend.db.database import init_db
from backend.downloader.audit import candidate_attempts_for_task, redact_url
from backend.downloader.queue import claim_next_task, enqueue_download, process_download_task, requeue_failed_downloads
from backend.downloader.resolver import (
    Resolution,
    is_safe_institutional_url,
    resolve_legal_oa_candidates,
)
from backend.downloader.retry import classify_failure, failure_class_for
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


class StreamResponse:
    def __init__(
        self,
        data: bytes,
        url: str,
        *,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.data = data
        self.url = url
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }

    def __enter__(self) -> "StreamResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self):
        midpoint = max(1, len(self.data) // 2)
        yield self.data[:midpoint]
        yield self.data[midpoint:]


class InstitutionalResolverTests(unittest.TestCase):
    def test_institutional_candidates_are_disabled_by_default(self) -> None:
        record = {
            "raw_crossref_json": json.dumps(
                {
                    "link": [
                        {
                            "URL": "https://publisher.example/subscribed.pdf",
                            "content-type": "application/pdf",
                        }
                    ]
                }
            )
        }

        candidates = resolve_legal_oa_candidates(record)

        self.assertEqual(candidates, [])

    def test_unsafe_institutional_urls_are_rejected(self) -> None:
        for url in (
            "http://publisher.example/paper.pdf",
            "https://localhost/paper.pdf",
            "https://127.0.0.1/paper.pdf",
            "https://10.1.2.3/paper.pdf",
            "https://alice:secret@publisher.example/paper.pdf",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_safe_institutional_url(url))

        self.assertTrue(
            is_safe_institutional_url("https://publisher.example/paper.pdf")
        )

    def test_non_cc_crossref_pdf_is_institutional_and_follows_oa_candidates(
        self,
    ) -> None:
        record = {
            "is_open_access": True,
            "pdf_url": "https://repository.example/open.pdf",
            "source": "repository",
            "raw_crossref_json": json.dumps(
                {
                    # Deliberately no Creative Commons licence: campus-IP
                    # access must not be relabelled as Open Access.
                    "link": [
                        {
                            "URL": "https://publisher.example/subscribed.pdf",
                            "content-type": "application/pdf",
                        }
                    ]
                }
            ),
        }

        candidates = resolve_legal_oa_candidates(record, allow_institutional_ip=True)

        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual(candidates[0].url, "https://repository.example/open.pdf")
        self.assertTrue(candidates[0].legal_open_access)
        self.assertEqual(candidates[0].access_basis, "open_access")

        institutional = next(
            item
            for item in candidates
            if item.url == "https://publisher.example/subscribed.pdf"
        )
        self.assertEqual(institutional.source, "crossref_institutional_ip")
        self.assertEqual(institutional.access_basis, "institutional_ip")
        self.assertFalse(institutional.legal_open_access)
        self.assertGreater(candidates.index(institutional), 0)


class InstitutionalLoginPageTests(unittest.TestCase):
    def test_subscription_redirect_to_login_html_requires_manual_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connection_for(root / "radar.sqlite")
            try:
                paper = LibraryRepository(connection).upsert_version(
                    {"title": "Campus subscription fixture", "doi": "10.1000/campus"},
                    source="crossref",
                    source_record_id="campus-login",
                )
                enqueue_download(
                    connection,
                    paper.canonical_paper_id,
                    paper.paper_version_id,
                )
                connection.commit()
                task = claim_next_task(connection)
                self.assertIsNotNone(task)

                candidate = Resolution(
                    "https://publisher.example/subscribed.pdf",
                    "crossref_institutional_ip",
                    False,
                    "publisher PDF available through institutional IP access",
                    access_basis="institutional_ip",
                )
                login_url = (
                    "https://idp.university.example/login"
                    "?ticket=do-not-store&SAMLResponse=also-secret"
                )
                login_html = (
                    b"<!doctype html><html><body><form action='/sso' method='post'>"
                    b"<label>Campus sign in</label><input type='password' name='password'>"
                    b"</form></body></html>" + b" " * 512
                )
                response = StreamResponse(login_html, login_url)

                with patch(
                    "backend.downloader.queue.resolve_legal_oa_candidates",
                    return_value=[candidate],
                ), patch("httpx.stream", return_value=response):
                    result = process_download_task(
                        connection,
                        task or {},
                        root / "pdf",
                    )

                self.assertEqual(result["status"], "manual_review")
                self.assertEqual(result["failure_class"], "institutional_auth_required")
                attempts = candidate_attempts_for_task(connection, str(task["id"]))
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["status"], "rejected")
                self.assertEqual(
                    attempts[0]["failure_class"],
                    "institutional_auth_required",
                )
                self.assertNotIn("do-not-store", attempts[0]["final_url"])
                self.assertNotIn("also-secret", attempts[0]["final_url"])
                partial_dir = root / "pdf" / ".partial"
                self.assertEqual(list(partial_dir.glob("*.part")), [])
            finally:
                connection.close()


class DownloadPrivacyTests(unittest.TestCase):
    def test_redact_url_removes_userinfo_fragment_and_sso_secrets(self) -> None:
        raw = (
            "https://alice:password@example.org/article.pdf"
            "?code=alpha&ticket=beta&SAMLResponse=gamma&RelayState=delta"
            "&session=epsilon&download=1#access_token=zeta"
        )

        redacted = redact_url(raw)
        parsed = urlsplit(redacted)
        query = parse_qs(parsed.query, keep_blank_values=True)

        self.assertIsNone(parsed.username)
        self.assertIsNone(parsed.password)
        self.assertEqual(parsed.netloc, "example.org")
        self.assertEqual(parsed.fragment, "")
        for key in ("code", "ticket", "SAMLResponse", "RelayState", "session"):
            self.assertEqual(query[key], ["***REDACTED***"])
        self.assertEqual(query["download"], ["1"])
        for secret in (
            "alice",
            "password",
            "alpha",
            "beta",
            "gamma",
            "delta",
            "epsilon",
            "zeta",
        ):
            self.assertNotIn(secret, redacted)


class FilteredCampusRetryTests(unittest.TestCase):
    def test_only_network_failures_are_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            try:
                repository = LibraryRepository(connection)
                network_paper = repository.upsert_version(
                    {"title": "Network failure"},
                    source="manual",
                    source_record_id="network",
                )
                no_access_paper = repository.upsert_version(
                    {"title": "No access"},
                    source="manual",
                    source_record_id="no-access",
                )
                first = enqueue_download(
                    connection,
                    network_paper.canonical_paper_id,
                    network_paper.paper_version_id,
                )
                second = enqueue_download(
                    connection,
                    no_access_paper.canonical_paper_id,
                    no_access_paper.paper_version_id,
                )
                connection.execute(
                    "UPDATE download_tasks SET status='manual_review', "
                    "last_error='[connection_error] TLS ended' WHERE id=?",
                    (first["id"],),
                )
                connection.execute(
                    "UPDATE download_tasks SET status='manual_review', "
                    "last_error='[no_legal_oa_location] none' WHERE id=?",
                    (second["id"],),
                )
                connection.commit()

                result = requeue_failed_downloads(
                    connection,
                    failure_classes=[
                        "connection_error",
                        "tls_error",
                        "http_server_error",
                    ],
                    limit=25,
                )

                self.assertEqual(result["requeued"], 1)
                statuses = dict(
                    connection.execute("SELECT id, status FROM download_tasks")
                )
                self.assertEqual(statuses[first["id"]], "pending")
                self.assertEqual(statuses[second["id"]], "manual_review")
            finally:
                connection.close()

class TlsRetryTests(unittest.TestCase):
    def test_tls_failures_are_classified_as_transient(self) -> None:
        failures: tuple[BaseException, ...] = (
            ssl.SSLError(
                "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
            ),
            httpx.ConnectError("TLS handshake terminated unexpectedly"),
        )

        for failure in failures:
            with self.subTest(exception=type(failure).__name__):
                failure_class = failure_class_for(exception=failure)
                self.assertEqual(failure_class, "tls_error")
                decision = classify_failure(
                    1,
                    failure_class=failure_class,
                    max_attempts=5,
                )
                self.assertTrue(decision.retryable)
                self.assertEqual(decision.status, "retryable_failed")
                self.assertGreater(decision.delay_seconds or 0, 0)


if __name__ == "__main__":
    unittest.main()
