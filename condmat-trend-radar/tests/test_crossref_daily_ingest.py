from __future__ import annotations

import argparse
import sqlite3
import ssl
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.db.database import init_db
from backend.ingest.crossref_client import CrossrefClient, CrossrefPage
from backend.ingest.journal_registry import find_journal
from backend.ingest_real import run
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import DISCOVERY_CURSOR_SOURCE, _record_cursor_attempt


class CrossrefCursorPaginationTests(unittest.TestCase):
    def test_cursor_pages_are_consumed_until_a_complete_short_page(self) -> None:
        journal = find_journal("Physical Review B")
        self.assertIsNotNone(journal)
        client = CrossrefClient(timeout=1, polite_delay=0)
        first = ([{"doi": "10.1/a"}, {"doi": "10.1/b"}], "cursor-2", [], 2)
        second = ([{"doi": "10.1/c"}], "cursor-3", [], 1)
        with patch.object(client, "_fetch_journal_page_raw", side_effect=[first, second]) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=2,
                    max_pages=0,
                    sleep_seconds=0,
                )
            )
        self.assertEqual([page.fetched for page in pages], [2, 1])
        self.assertEqual([page.cursor_used for page in pages], ["*", "cursor-2"])
        self.assertEqual(pages[-1].stop_reason, "short_page")
        self.assertEqual(fetch.call_count, 2)

    def test_page_budget_is_an_explicit_incomplete_stop(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(timeout=1, polite_delay=0)
        response = ([{"doi": "10.1/a"}, {"doi": "10.1/b"}], "cursor-2", [], 2)
        with patch.object(client, "_fetch_journal_page_raw", return_value=response):
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=2,
                    max_pages=1,
                    sleep_seconds=0,
                )
            )
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].stop_reason, "max_pages_per_chunk")


    def test_transient_failure_retries_same_cursor_then_succeeds(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=2,
            retry_backoff_base=0,
        )
        transient = urllib.error.URLError(ssl.SSLError("TLS handshake timed out"))
        recovered = ([{"doi": "10.1/recovered"}], "cursor-2", [], 1)
        hooks: list[tuple[str, int]] = []
        with patch.object(client, "_fetch_journal_page_raw", side_effect=[transient, recovered]) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=2,
                    max_pages=0,
                    sleep_seconds=0,
                    page_hook=lambda phase, page: hooks.append((phase, page)),
                )
            )

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([call.kwargs["cursor"] for call in fetch.call_args_list], ["*", "*"])
        self.assertEqual(hooks, [("before", 1), ("after", 1), ("before", 1), ("after", 1)])
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].attempts, 2)
        self.assertEqual(pages[0].errors, [])
        self.assertEqual(pages[0].stop_reason, "short_page")

    def test_retry_after_controls_short_bounded_backoff(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=1,
            retry_backoff_base=0.1,
            retry_backoff_cap=5,
        )
        transient = (
            [],
            "",
            [
                {
                    "status": 429,
                    "transient": True,
                    "retry_after_seconds": 2,
                }
            ],
            0,
        )
        recovered = ([{"doi": "10.1/recovered"}], "", [], 1)
        with (
            patch.object(client, "_fetch_journal_page_raw", side_effect=[transient, recovered]),
            patch("backend.ingest.crossref_client.time.sleep") as sleep,
        ):
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=2,
                    max_pages=0,
                    sleep_seconds=0,
                )
            )

        sleep.assert_called_once_with(2.0)
        self.assertEqual(pages[0].attempts, 2)
        self.assertFalse(pages[0].errors)

    def test_transient_retry_exhaustion_yields_one_error_page_at_original_cursor(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=2,
            retry_backoff_base=0,
        )
        with patch.object(
            client,
            "_fetch_journal_page_raw",
            side_effect=TimeoutError("read timed out"),
        ) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=2,
                    cursor="cursor-before-failure",
                    max_pages=0,
                    sleep_seconds=0,
                )
            )

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            [call.kwargs["cursor"] for call in fetch.call_args_list],
            ["cursor-before-failure"] * 3,
        )
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].cursor_used, "cursor-before-failure")
        self.assertEqual(pages[0].next_cursor, "")
        self.assertEqual(pages[0].stop_reason, "error")
        self.assertEqual(pages[0].errors[0]["attempts"], 3)
        self.assertTrue(pages[0].errors[0]["retries_exhausted"])

    def test_cursor_500_npe_falls_back_to_smaller_rows_after_retries(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=1,
            retry_backoff_base=0,
        )
        cursor_npe = (
            [],
            "",
            [
                {
                    "status": 500,
                    "response": "java.lang.NullPointerException",
                    "transient": True,
                }
            ],
            0,
        )
        recovered = (
            [{"doi": f"10.1/recovered-{index}"} for index in range(250)],
            "cursor-3",
            [],
            250,
        )
        completed = ([{"doi": "10.1/final-short-page"}], "cursor-4", [], 1)
        with patch.object(
            client,
            "_fetch_journal_page_raw",
            side_effect=[cursor_npe, cursor_npe, recovered, completed],
        ) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=1000,
                    cursor="cursor-2",
                    max_pages=0,
                    sleep_seconds=0,
                )
            )

        self.assertEqual(
            [call.kwargs["cursor"] for call in fetch.call_args_list],
            ["cursor-2", "cursor-2", "cursor-2", "cursor-3"],
        )
        self.assertEqual(
            [call.kwargs["rows"] for call in fetch.call_args_list],
            [1000, 1000, 250, 250],
        )
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].attempts, 3)
        self.assertEqual(pages[0].rows_used, 250)
        self.assertEqual(pages[0].row_fallback_from, 1000)
        self.assertEqual(pages[0].errors, [])
        self.assertEqual(pages[0].stop_reason, "")
        self.assertEqual(pages[1].cursor_used, "cursor-3")
        self.assertEqual(pages[1].rows_used, 250)
        self.assertIsNone(pages[1].row_fallback_from)
        self.assertEqual(pages[1].stop_reason, "short_page")
        self.assertEqual(
            len({paper["doi"] for page in pages for paper in page.papers}),
            251,
        )

    def test_persistent_cursor_npe_switches_to_bounded_offset_and_completes(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=0,
            retry_backoff_base=0,
        )
        cursor_npe = (
            [],
            "",
            [
                {
                    "status": 500,
                    "response": "java.lang.NullPointerException",
                    "transient": True,
                }
            ],
            0,
        )
        replayed = (
            [{"doi": f"10.1/offset-{index}"} for index in range(250)],
            "offset:250",
            [],
            250,
        )
        completed = ([{"doi": "10.1/offset-final"}], "offset:251", [], 1)
        with patch.object(
            client,
            "_fetch_journal_page_raw",
            side_effect=[cursor_npe, cursor_npe, replayed, completed],
        ) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=1000,
                    cursor="cursor-2",
                    max_pages=0,
                    sleep_seconds=0,
                )
            )

        self.assertEqual(
            [call.kwargs["cursor"] for call in fetch.call_args_list],
            ["cursor-2", "cursor-2", "offset:0", "offset:250"],
        )
        self.assertEqual(
            [call.kwargs["offset"] for call in fetch.call_args_list],
            [None, None, 0, 250],
        )
        self.assertEqual(
            [call.kwargs["rows"] for call in fetch.call_args_list],
            [1000, 250, 250, 250],
        )
        self.assertEqual([page.pagination_mode for page in pages], ["offset", "offset"])
        self.assertEqual([page.cursor_used for page in pages], ["offset:0", "offset:250"])
        self.assertEqual(pages[-1].stop_reason, "short_page")
        self.assertEqual(
            len({paper["doi"] for page in pages for paper in page.papers}),
            251,
        )

    def test_cursor_npe_fallback_exhaustion_remains_partial_at_same_cursor(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=1,
            retry_backoff_base=0,
        )
        cursor_npe = (
            [],
            "",
            [
                {
                    "status": 500,
                    "response": "java.lang.NullPointerException",
                    "transient": True,
                }
            ],
            0,
        )
        with patch.object(client, "_fetch_journal_page_raw", return_value=cursor_npe) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=1000,
                    cursor="cursor-2",
                    max_pages=0,
                    sleep_seconds=0,
                )
            )

        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(
            [call.kwargs["cursor"] for call in fetch.call_args_list],
            ["cursor-2", "cursor-2", "cursor-2", "cursor-2", "offset:0", "offset:0"],
        )
        self.assertEqual(
            [call.kwargs["rows"] for call in fetch.call_args_list],
            [1000, 1000, 250, 250, 250, 250],
        )
        self.assertEqual(
            [call.kwargs["offset"] for call in fetch.call_args_list],
            [None, None, None, None, 0, 0],
        )
        self.assertEqual(pages[0].cursor_used, "offset:0")
        self.assertEqual(pages[0].pagination_mode, "offset")
        self.assertEqual(pages[0].stop_reason, "error")
        self.assertEqual(pages[0].errors[0]["row_fallback_from"], 1000)
        self.assertEqual(pages[0].errors[0]["rows_used"], 250)
        self.assertTrue(pages[0].errors[0]["retries_exhausted"])

    def test_cancel_hook_after_failed_attempt_prevents_retry(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=2,
            retry_backoff_base=0,
        )
        transient = (
            [],
            "",
            [{"type": "URLError", "message": "temporary TLS error", "transient": True}],
            0,
        )
        hooks: list[tuple[str, int]] = []

        def cancel_after_attempt(phase: str, page: int) -> None:
            hooks.append((phase, page))
            if phase == "after":
                raise RuntimeError("cancel requested")

        with patch.object(client, "_fetch_journal_page_raw", return_value=transient) as fetch:
            with self.assertRaisesRegex(RuntimeError, "cancel requested"):
                list(
                    client.iterate_crossref_works(
                        journal,
                        "2026-08-01",
                        "2026-08-09",
                        rows=2,
                        max_pages=0,
                        sleep_seconds=0,
                        page_hook=cancel_after_attempt,
                    )
                )

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(hooks, [("before", 1), ("after", 1)])

    def test_retry_does_not_repeat_already_yielded_page(self) -> None:
        journal = find_journal("Physical Review B")
        client = CrossrefClient(
            timeout=1,
            polite_delay=0,
            max_retries=2,
            retry_backoff_base=0,
        )
        first = ([{"doi": "10.1/a"}, {"doi": "10.1/b"}], "cursor-2", [], 2)
        transient = (
            [],
            "",
            [{"type": "SSLError", "message": "TLS read timed out", "transient": True}],
            0,
        )
        second = ([{"doi": "10.1/c"}], "cursor-3", [], 1)
        with patch.object(
            client,
            "_fetch_journal_page_raw",
            side_effect=[first, transient, second],
        ) as fetch:
            pages = list(
                client.iterate_crossref_works(
                    journal,
                    "2026-08-01",
                    "2026-08-09",
                    rows=2,
                    max_pages=0,
                    sleep_seconds=0,
                )
            )

        self.assertEqual(
            [call.kwargs["cursor"] for call in fetch.call_args_list],
            ["*", "cursor-2", "cursor-2"],
        )
        self.assertEqual([page.cursor_used for page in pages], ["*", "cursor-2"])
        self.assertEqual(
            [paper["doi"] for page in pages for paper in page.papers],
            ["10.1/a", "10.1/b", "10.1/c"],
        )
        self.assertEqual(pages[1].attempts, 2)


class DailyCrossrefDiscoveryTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            baseline_from="2026-08-01",
            baseline_to="2026-08-09",
            scope="core",
            journals="Physical Review B",
            limit_per_journal=0,
            resume=True,
            force_refresh=False,
            dry_run=False,
            mailto=None,
            include_arxiv=False,
            include_openalex_field=False,
            include_crossref=True,
            crossref_rows=1000,
            max_pages=0,
            sleep_seconds=0,
            timeout=1,
            incremental=True,
        )

    def test_same_doi_keeps_two_source_versions_and_replay_is_idempotent(self) -> None:
        openalex_paper = {
            "id": "https://openalex.org/W123",
            "openalex_id": "https://openalex.org/W123",
            "doi": "10.5555/shared",
            "title": "Superconductivity in a layered quantum material",
            "abstract": "A condensed matter superconductivity study.",
            "publication_date": "2026-08-02",
            "journal": "Physical Review B",
            "source": "openalex",
            "raw_json": {"id": "https://openalex.org/W123"},
        }
        crossref_paper = {
            "id": "doi:10.5555/shared",
            "doi": "10.5555/shared",
            "title": "Superconductivity in a layered quantum material",
            "abstract": "A condensed matter superconductivity study.",
            "publication_date": "2026-08-02",
            "journal": "Physical Review B",
            "source": "crossref",
            "raw_json": {"DOI": "10.5555/shared"},
        }

        class FakeOpenAlex:
            def __init__(self, **_kwargs):
                pass

            def find_source_id(self, _journal):
                return "S123"

            def fetch_journal_page(self, *_args, **_kwargs):
                return {"results": [dict(openalex_paper)], "meta": {"next_cursor": ""}}

            def _normalize_work(self, raw, _journal):
                return dict(raw)

        class FakeCrossref:
            def __init__(self, **_kwargs):
                pass

            def iterate_crossref_works(self, _journal, _date_from, _date_to, **kwargs):
                yield CrossrefPage(
                    page=1,
                    cursor_used=str(kwargs.get("cursor") or "*"),
                    next_cursor="",
                    fetched=1,
                    papers=[dict(crossref_paper)],
                    errors=[],
                    duplicate_rate_vs_previous_page=0.0,
                    stop_reason="short_page",
                )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"

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

            patches = (
                patch("backend.ingest_real.connect", test_connect),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", FakeOpenAlex),
                patch("backend.ingest_real.CrossrefClient", FakeCrossref),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                first = run(self._args())
                replay = run(self._args())

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            versions = connection.execute(
                "SELECT canonical_paper_id, source, source_record_id FROM paper_versions "
                "WHERE source IN ('openalex','crossref') ORDER BY source"
            ).fetchall()
            observations = connection.execute(
                "SELECT COUNT(*) FROM paper_version_observations"
            ).fetchone()[0]
            connection.close()

            self.assertEqual(first["status"], "ok")
            self.assertEqual(replay["status"], "ok")
            self.assertEqual(len(versions), 2)
            self.assertEqual({row["source"] for row in versions}, {"openalex", "crossref"})
            self.assertEqual(len({row["canonical_paper_id"] for row in versions}), 1)
            self.assertEqual(replay["changed_count"], 0)
            self.assertEqual(observations, 2)

    def test_crossref_page_cap_makes_the_ingest_partial(self) -> None:
        paper = {
            "id": "doi:10.5555/capped",
            "doi": "10.5555/capped",
            "title": "Superconductivity under a Crossref page cap",
            "abstract": "A condensed matter superconductivity study.",
            "publication_date": "2026-08-02",
            "journal": "Physical Review B",
            "source": "crossref",
            "raw_json": {"DOI": "10.5555/capped"},
        }

        class EmptyOpenAlex:
            def __init__(self, **_kwargs):
                pass

            def find_source_id(self, _journal):
                return "S123"

            def fetch_journal_page(self, *_args, **_kwargs):
                return {"results": [], "meta": {"next_cursor": ""}}

        class CappedCrossref:
            def __init__(self, **_kwargs):
                pass

            def iterate_crossref_works(self, _journal, _date_from, _date_to, **kwargs):
                yield CrossrefPage(
                    page=1,
                    cursor_used=str(kwargs.get("cursor") or "*"),
                    next_cursor="cursor-2",
                    fetched=1,
                    papers=[dict(paper)],
                    errors=[],
                    duplicate_rate_vs_previous_page=0.0,
                    stop_reason="max_pages_per_chunk",
                )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"

            @contextmanager
            def test_connect(_path=None):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                finally:
                    connection.close()

            args = self._args()
            args.max_pages = 1
            patches = (
                patch("backend.ingest_real.connect", test_connect),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", EmptyOpenAlex),
                patch("backend.ingest_real.CrossrefClient", CappedCrossref),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                result = run(args)

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["source_completeness"]["watermark_safe_to_advance"])
        self.assertEqual(result["source_counts"]["crossref"]["journals_partial"], 1)
        self.assertTrue(
            any(item.get("stop_reason") == "max_pages_per_chunk" for item in result["errors"])
        )
    def test_exhausted_crossref_page_is_partial_without_advancing_provider_checkpoint(self) -> None:
        class EmptyOpenAlex:
            def __init__(self, **_kwargs):
                pass

            def find_source_id(self, _journal):
                return "S123"

            def fetch_journal_page(self, *_args, **_kwargs):
                return {"results": [], "meta": {"next_cursor": ""}}

        class ExhaustedCrossref:
            def __init__(self, **_kwargs):
                pass

            def iterate_crossref_works(self, _journal, _date_from, _date_to, **kwargs):
                yield CrossrefPage(
                    page=1,
                    cursor_used=str(kwargs.get("cursor") or "*"),
                    next_cursor="",
                    fetched=0,
                    papers=[],
                    errors=[
                        {
                            "type": "TimeoutError",
                            "message": "read timed out",
                            "attempts": 3,
                            "retries_exhausted": True,
                        }
                    ],
                    duplicate_rate_vs_previous_page=0.0,
                    stop_reason="error",
                    attempts=3,
                )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"

            @contextmanager
            def test_connect(_path=None):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
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
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", EmptyOpenAlex),
                patch("backend.ingest_real.CrossrefClient", ExhaustedCrossref),
                patch("backend.ingest_real.update_checkpoint") as checkpoint,
            ):
                result = run(self._args())

        crossref_checkpoint_calls = [
            call for call in checkpoint.call_args_list if call.kwargs.get("source") == "crossref"
        ]
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["source_completeness"]["watermark_safe_to_advance"])
        self.assertEqual(result["source_counts"]["crossref"]["journals_partial"], 1)
        self.assertEqual(crossref_checkpoint_calls, [])

    def test_partial_crossref_attempt_does_not_advance_discovery_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            init_db(connection)
            apply_unified_schema(connection)
            _record_cursor_attempt(
                connection,
                DISCOVERY_CURSOR_SOURCE,
                status="ok",
                cursor="2026-08-08",
            )
            _record_cursor_attempt(
                connection,
                DISCOVERY_CURSOR_SOURCE,
                status="partial",
                cursor="2026-08-09",
                error="crossref page cap",
            )
            row = connection.execute(
                "SELECT * FROM source_cursors WHERE source_name=?",
                (DISCOVERY_CURSOR_SOURCE,),
            ).fetchone()
            connection.close()
            self.assertEqual(row["last_successful_cursor"], "2026-08-08")
            self.assertEqual(row["status"], "partial")


if __name__ == "__main__":
    unittest.main()
