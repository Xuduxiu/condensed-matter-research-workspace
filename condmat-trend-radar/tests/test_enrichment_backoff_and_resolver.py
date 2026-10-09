from __future__ import annotations

import email.message
import json
import sqlite3
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.db.database import init_db
from backend.downloader.resolver import resolve_legal_oa_candidates
from backend.library.metadata_enrichment import (
    ProviderRateLimitError,
    _fetch_semantic_scholar,
    backfill_missing_abstracts,
    enrich_paper_metadata,
)
from backend.library.repository import LibraryRepository
from backend.migrations.unified_library import apply_unified_schema


def test_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    connection.commit()
    return connection


class JsonUrlResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "JsonUrlResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def add_missing_abstract_paper(
    connection: sqlite3.Connection,
    source_record_id: str,
    publication_date: str,
) -> str:
    result = LibraryRepository(connection).upsert_version(
        {
            "title": f"Missing abstract {source_record_id}",
            "doi": f"10.1000/{source_record_id}",
            "publication_date": publication_date,
        },
        source="test",
        source_record_id=source_record_id,
        default_condmat_eligible=True,
    )
    connection.commit()
    return result.canonical_paper_id


class MetadataEnrichmentBackoffTests(unittest.TestCase):
    def test_failed_candidates_enter_cooldown_and_next_run_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = test_connection(Path(directory) / "radar.sqlite")
            for index in range(4):
                add_missing_abstract_paper(
                    connection,
                    f"rotate-{index}",
                    f"2026-01-{index + 1:02d}",
                )

            def unavailable(_connection: sqlite3.Connection, paper_id: str, *, timeout: int) -> dict:
                return {
                    "canonical_paper_id": paper_id,
                    "abstract_available": False,
                    "updated": False,
                    "errors": ["semantic_scholar: rate limited"],
                }

            progress: list[tuple[int, int, int, int, int, str, str]] = []
            visible_backoff_counts: list[int] = []

            def on_progress(
                processed: int,
                total: int,
                enriched: int,
                not_found: int,
                failed: int,
                current_paper_id: str,
                current_source: str,
            ) -> None:
                progress.append((processed, total, enriched, not_found, failed, current_paper_id, current_source))
                observer = sqlite3.connect(Path(directory) / "radar.sqlite")
                try:
                    try:
                        count = observer.execute(
                            "SELECT COUNT(*) FROM metadata_enrichment_backoff WHERE state_type='paper'"
                        ).fetchone()[0]
                    except sqlite3.OperationalError:
                        count = 0
                    visible_backoff_counts.append(int(count))
                finally:
                    observer.close()

            with patch(
                "backend.library.metadata_enrichment.enrich_paper_metadata",
                side_effect=unavailable,
            ) as mocked:
                first = backfill_missing_abstracts(
                    connection,
                    limit=2,
                    timeout=1,
                    progress_callback=on_progress,
                )
                first_ids = {call.args[1] for call in mocked.call_args_list}
                mocked.reset_mock()
                second = backfill_missing_abstracts(connection, limit=2, timeout=1)
                second_ids = {call.args[1] for call in mocked.call_args_list}

            self.assertEqual(first["requested"], 2)
            self.assertEqual(first["total"], 2)
            self.assertEqual(first["processed"], 2)
            self.assertEqual(first["enriched"], 0)
            self.assertEqual(first["not_found"], 0)
            self.assertEqual(first["failed"], 2)
            self.assertEqual(first["progress_callback_errors"], [])
            self.assertEqual([item[:5] for item in progress], [
                (0, 2, 0, 0, 0),
                (0, 2, 0, 0, 0),
                (1, 2, 0, 0, 1),
                (1, 2, 0, 0, 1),
                (2, 2, 0, 0, 2),
            ])
            self.assertEqual(visible_backoff_counts, [0, 0, 1, 1, 2])
            self.assertEqual(progress[1][6], "resolver")
            self.assertEqual(progress[-1][6], "semantic_scholar")
            self.assertEqual(first["cooldown_skipped"], 0)
            self.assertEqual(second["requested"], 2)
            self.assertEqual(second["cooldown_skipped"], 2)
            self.assertTrue(first_ids.isdisjoint(second_ids))
            states = connection.execute(
                "SELECT state_key, attempt_count, next_attempt_at FROM metadata_enrichment_backoff WHERE state_type='paper'"
            ).fetchall()
            self.assertEqual(len(states), 4)
            self.assertTrue(all(int(row["attempt_count"]) == 1 for row in states))
            connection.close()

    def test_success_clears_existing_paper_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = test_connection(Path(directory) / "radar.sqlite")
            paper_id = add_missing_abstract_paper(connection, "success", "2026-02-01")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata_enrichment_backoff (
                    state_key TEXT PRIMARY KEY, state_type TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL, last_attempt_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL, status TEXT NOT NULL,
                    last_error TEXT, updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """INSERT INTO metadata_enrichment_backoff
                   VALUES (?, 'paper', 2, '2020-01-01', '2020-01-01', 'cooldown', 'old', '2020-01-01')""",
                (f"paper:{paper_id}",),
            )
            connection.commit()

            available = {
                "canonical_paper_id": paper_id,
                "abstract_available": True,
                "updated": True,
                "errors": [],
            }
            with patch(
                "backend.library.metadata_enrichment.enrich_paper_metadata",
                return_value=available,
            ):
                result = backfill_missing_abstracts(connection, limit=1, timeout=1)

            self.assertEqual(result["enriched"], 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM metadata_enrichment_backoff WHERE state_key=?",
                    (f"paper:{paper_id}",),
                ).fetchone()[0],
                0,
            )
            connection.close()

    def test_clean_no_result_counts_as_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = test_connection(Path(directory) / "radar.sqlite")
            paper_id = add_missing_abstract_paper(connection, "not-found", "2026-02-02")
            progress: list[tuple[int, int, int, int, int, str, str]] = []
            unavailable = {
                "canonical_paper_id": paper_id,
                "abstract_available": False,
                "updated": False,
                "errors": [],
            }
            with patch(
                "backend.library.metadata_enrichment.enrich_paper_metadata",
                return_value=unavailable,
            ):
                result = backfill_missing_abstracts(
                    connection,
                    limit=1,
                    timeout=1,
                    progress_callback=lambda *values: progress.append(values),
                )
            connection.close()

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["enriched"], 0)
        self.assertEqual(result["not_found"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(progress[-1][:5], (1, 1, 0, 1, 0))
        self.assertEqual(progress[-1][5:], (paper_id, "all_sources"))

    def test_semantic_scholar_retries_transient_network_errors_and_sets_user_agent(self) -> None:
        transient_errors = (
            ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
            urllib.error.URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")),
            TimeoutError("timed out"),
        )
        for transient_error in transient_errors:
            with self.subTest(error=type(transient_error).__name__):
                response = JsonUrlResponse(
                    {
                        "title": "Recovered metadata",
                        "abstract": "A provider response recovered after a transient connection failure.",
                        "url": "https://www.semanticscholar.org/paper/recovered",
                        "openAccessPdf": {"url": "https://example.org/recovered.pdf"},
                    }
                )
                with patch(
                    "backend.library.metadata_enrichment.urllib.request.urlopen",
                    side_effect=[transient_error, response],
                ) as urlopen, patch("backend.library.metadata_enrichment.time.sleep") as sleep:
                    result = _fetch_semantic_scholar("10.1000/recovered", "", 1)

                self.assertEqual(urlopen.call_count, 2)
                self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.5])
                self.assertEqual(result["abstract"], "A provider response recovered after a transient connection failure.")
                request = urlopen.call_args_list[-1].args[0]
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertIn("metadata enrichment", headers["user-agent"].lower())

    def test_semantic_scholar_network_retry_is_bounded(self) -> None:
        failure = ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")
        with patch(
            "backend.library.metadata_enrichment.urllib.request.urlopen",
            side_effect=failure,
        ) as urlopen, patch("backend.library.metadata_enrichment.time.sleep") as sleep:
            with self.assertRaises(ssl.SSLError):
                _fetch_semantic_scholar("10.1000/still-failing", "", 1)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.5, 1.0])

    def test_semantic_scholar_retries_transient_http_statuses(self) -> None:
        for status in (408, 425, 429, 500, 599):
            with self.subTest(status=status):
                headers = email.message.Message()
                headers["Retry-After"] = "0"
                transient = urllib.error.HTTPError(
                    "https://api.semanticscholar.org/test",
                    status,
                    "transient",
                    headers,
                    None,
                )
                response = JsonUrlResponse({"title": "Recovered", "abstract": "Recovered abstract."})
                with patch(
                    "backend.library.metadata_enrichment.urllib.request.urlopen",
                    side_effect=[transient, response],
                ) as urlopen, patch("backend.library.metadata_enrichment.time.sleep") as sleep:
                    result = _fetch_semantic_scholar("10.1000/http-retry", "", 1)

                self.assertEqual(urlopen.call_count, 2)
                self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.5])
                self.assertEqual(result["abstract"], "Recovered abstract.")

    def test_semantic_scholar_does_not_retry_permanent_4xx(self) -> None:
        permanent = urllib.error.HTTPError(
            "https://api.semanticscholar.org/test",
            404,
            "not found",
            email.message.Message(),
            None,
        )
        with patch(
            "backend.library.metadata_enrichment.urllib.request.urlopen",
            side_effect=permanent,
        ) as urlopen, patch("backend.library.metadata_enrichment.time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                _fetch_semantic_scholar("10.1000/not-found", "", 1)

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_semantic_scholar_429_is_single_attempt_and_provider_cooldown_persists(self) -> None:
        headers = email.message.Message()
        headers["Retry-After"] = "120"
        rate_limit = urllib.error.HTTPError(
            "https://api.semanticscholar.org/test",
            429,
            "rate limited",
            headers,
            None,
        )
        with patch("urllib.request.urlopen", side_effect=rate_limit) as urlopen:
            with self.assertRaises(ProviderRateLimitError):
                _fetch_semantic_scholar("10.1000/rate", "", 1)
        self.assertEqual(urlopen.call_count, 1)

        with tempfile.TemporaryDirectory() as directory:
            connection = test_connection(Path(directory) / "radar.sqlite")
            paper_id = add_missing_abstract_paper(connection, "provider-rate", "2026-03-01")
            provider_error = ProviderRateLimitError("semantic_scholar", 120)
            with patch(
                "backend.library.metadata_enrichment.CrossrefClient.get_work",
                return_value=None,
            ), patch(
                "backend.library.metadata_enrichment._fetch_openalex",
                return_value=None,
            ), patch(
                "backend.library.metadata_enrichment._fetch_semantic_scholar",
                side_effect=provider_error,
            ):
                first = enrich_paper_metadata(connection, paper_id, timeout=1)
            self.assertFalse(first["abstract_available"])
            provider_state = connection.execute(
                "SELECT * FROM metadata_enrichment_backoff WHERE state_key='provider:semantic_scholar'"
            ).fetchone()
            self.assertIsNotNone(provider_state)
            self.assertGreaterEqual(int(provider_state["attempt_count"]), 1)

            with patch(
                "backend.library.metadata_enrichment.CrossrefClient.get_work",
                return_value=None,
            ), patch(
                "backend.library.metadata_enrichment._fetch_openalex",
                return_value=None,
            ), patch(
                "backend.library.metadata_enrichment._fetch_semantic_scholar"
            ) as semantic:
                second = enrich_paper_metadata(connection, paper_id, timeout=1)
            self.assertFalse(second["abstract_available"])
            semantic.assert_not_called()
            self.assertTrue(any("provider cooldown" in item for item in second["errors"]))
            connection.close()


class ResolverFallbackTests(unittest.TestCase):
    def test_unpaywall_failure_keeps_candidates_and_raw_metadata_is_merged(self) -> None:
        record = {
            "doi": "10.1000/merge/test",
            "arxiv_id": "2601.00001",
            "is_open_access": True,
            "paper_raw_json": json.dumps(
                {
                    "best_oa_location": {"pdf_url": "https://repository.example/paper.pdf"},
                    "open_access": {"oa_url": "https://repository.example/fallback.pdf"},
                }
            ),
            "raw_json": json.dumps(
                {
                    "primary_location": {"landing_page_url": "https://publisher.example/item"},
                    "open_access": {"is_oa": True},
                }
            ),
        }
        with patch("httpx.get", side_effect=httpx.TimeoutException("unpaywall timeout")):
            candidates = resolve_legal_oa_candidates(
                record,
                unpaywall_email="researcher@example.org",
                timeout=1,
            )

        urls = {item.url for item in candidates}
        self.assertIn("https://arxiv.org/pdf/2601.00001.pdf", urls)
        self.assertIn("https://repository.example/paper.pdf", urls)
        self.assertIn("https://repository.example/fallback.pdf", urls)


if __name__ == "__main__":
    unittest.main()
