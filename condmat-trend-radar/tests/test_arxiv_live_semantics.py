from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.ingest.arxiv_client import ARXIV_CATEGORIES, ArxivClient
from backend.ingest_real import run


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/2501.01234v2</id>
    <updated>2026-07-17T08:00:00Z</updated>
    <published>2025-01-05T08:00:00Z</published>
    <title>Graphene quantum transport</title>
    <summary>A condensed matter result.</summary>
    <author><name>A. Researcher</name></author>
    <arxiv:primary_category term="cond-mat.mes-hall" />
    <category term="cond-mat.mes-hall" />
  </entry>
</feed>
"""


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return FEED


class ArxivLiveSemanticsTests(unittest.TestCase):
    def test_updated_version_is_included_and_id_is_normalized(self) -> None:
        client = ArxivClient(timeout=1, polite_delay=0)
        with patch("backend.ingest.arxiv_client.urllib.request.urlopen", return_value=FakeResponse()):
            papers, failures = client.fetch("2026-07-16", "2026-07-17", max_results=10)
        self.assertEqual(failures, 0)
        self.assertEqual(client.last_fetched_count, 1)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["arxiv_id"], "2501.01234")
        self.assertEqual(papers[0]["arxiv_version"], 2)
        self.assertEqual(papers[0]["submitted_date"], "2025-01-05")
        self.assertEqual(papers[0]["updated_date"], "2026-07-17")
        self.assertEqual(papers[0]["primary_category"], "cond-mat.mes-hall")
        self.assertEqual(papers[0]["raw_json"]["primary_category"], "cond-mat.mes-hall")
        self.assertTrue(papers[0]["pdf_url"].endswith("2501.01234v2"))

    def test_all_condensed_matter_categories_are_scanned(self) -> None:
        self.assertEqual(len(ARXIV_CATEGORIES), 9)
        self.assertTrue({"cond-mat.soft", "cond-mat.dis-nn", "cond-mat.other"}.issubset(ARXIV_CATEGORIES))

    def test_network_failure_preserves_safe_error_summary(self) -> None:
        client = ArxivClient(timeout=1, polite_delay=0)
        with patch("backend.ingest.arxiv_client.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            papers, failures = client.fetch("2026-07-16", "2026-07-17", max_results=10)
        self.assertEqual(papers, [])
        self.assertEqual(failures, 1)
        self.assertEqual(client.last_error["error"], "arxiv_fetch_failed")
        self.assertEqual(client.last_error["type"], "TimeoutError")
        self.assertIn("timed out", client.last_error["message"])

    def test_updated_date_pagination_does_not_stop_at_first_page(self) -> None:
        client = ArxivClient(timeout=1, polite_delay=0)
        first_page = [
            {"id": f"arxiv:2608.{index:05d}", "arxiv_id": f"2608.{index:05d}", "updated_date": "2026-08-09", "submitted_date": "2026-08-09"}
            for index in range(50)
        ]
        older_page = [{"id": "arxiv:old", "arxiv_id": "2501.00001", "updated_date": "2026-08-01", "submitted_date": "2025-01-01"}]
        with patch("backend.ingest.arxiv_client.urllib.request.urlopen", side_effect=[FakeResponse(), FakeResponse()]) as request, patch(
            "backend.ingest.arxiv_client.parse_arxiv_feed", side_effect=[first_page, older_page]
        ):
            papers, failures = client.fetch("2026-08-08", "2026-08-09", max_results=50)
        self.assertEqual(failures, 0)
        self.assertEqual(len(papers), 50)
        self.assertEqual(client.last_fetched_count, 51)
        self.assertEqual(request.call_count, 2)
        self.assertIn("start=50", request.call_args_list[1].args[0].full_url)
        self.assertIn("sortBy=lastUpdatedDate", request.call_args_list[0].args[0].full_url)

    def test_after_page_hook_observes_updated_fetched_count(self) -> None:
        client = ArxivClient(timeout=1, polite_delay=0)
        first_page = [
            {"id": f"arxiv:2608.{index:05d}", "arxiv_id": f"2608.{index:05d}", "updated_date": "2026-08-09", "submitted_date": "2026-08-09"}
            for index in range(50)
        ]
        last_page = [
            {"id": "arxiv:2608.99999", "arxiv_id": "2608.99999", "updated_date": "2026-08-09", "submitted_date": "2026-08-09"}
        ]
        observed: list[tuple[int, int]] = []

        def page_hook(phase: str, page: int) -> None:
            if phase == "after":
                observed.append((page + 1, client.last_fetched_count))

        with patch("backend.ingest.arxiv_client.urllib.request.urlopen", side_effect=[FakeResponse(), FakeResponse()]), patch(
            "backend.ingest.arxiv_client.parse_arxiv_feed", side_effect=[first_page, last_page]
        ):
            papers, failures = client.fetch(
                "2026-08-08",
                "2026-08-09",
                max_results=50,
                page_hook=page_hook,
            )

        self.assertEqual(failures, 0)
        self.assertEqual(len(papers), 51)
        self.assertEqual(observed, [(1, 50), (2, 51)])
    def test_page_cap_is_reported_as_partial_instead_of_silent_success(self) -> None:
        client = ArxivClient(timeout=1, polite_delay=0)
        full_page = [
            {"id": f"arxiv:2608.{index:05d}", "arxiv_id": f"2608.{index:05d}", "updated_date": "2026-08-09", "submitted_date": "2026-08-09"}
            for index in range(50)
        ]
        with patch("backend.ingest.arxiv_client.urllib.request.urlopen", return_value=FakeResponse()), patch(
            "backend.ingest.arxiv_client.parse_arxiv_feed", return_value=full_page
        ):
            papers, failures = client.fetch("2026-08-08", "2026-08-09", max_results=50, max_pages=1)
        self.assertEqual(len(papers), 50)
        self.assertEqual(failures, 1)
        self.assertEqual(client.last_error["error"], "arxiv_page_limit_reached")
    def test_partial_later_page_preserves_records_and_reports_failure(self) -> None:
        client = ArxivClient(timeout=1, polite_delay=0)
        first_page = [
            {"id": f"arxiv:2608.{index:05d}", "arxiv_id": f"2608.{index:05d}", "updated_date": "2026-08-09", "submitted_date": "2026-08-09"}
            for index in range(50)
        ]
        with patch("backend.ingest.arxiv_client.urllib.request.urlopen", side_effect=[FakeResponse(), TimeoutError("page two timed out")]), patch(
            "backend.ingest.arxiv_client.parse_arxiv_feed", return_value=first_page
        ):
            papers, failures = client.fetch("2026-08-08", "2026-08-09", max_results=50)
        self.assertEqual(len(papers), 50)
        self.assertEqual(failures, 1)
        self.assertEqual(client.last_error["page"], 1)
        self.assertEqual(client.last_error["records_preserved"], 50)
    def test_ingest_exposes_distinct_counts_and_keeps_legacy_inserted_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            current = [{
                "id": "arxiv:2607.00001",
                "arxiv_id": "2607.00001",
                "arxiv_version": 1,
                "title": "Graphene live scan",
                "abstract": "A condensed matter transport study.",
                "publication_date": "2026-07-17",
                "submitted_date": "2026-07-17",
                "updated_date": "2026-07-17",
                "journal": "arXiv",
                "source": "arxiv",
                "source_scope": "preprint",
                "pdf_url": "https://arxiv.org/pdf/2607.00001v1",
                "url": "https://arxiv.org/abs/2607.00001v1",
                "is_open_access": True,
                "oa_status": "arxiv",
                "categories": ["cond-mat.mes-hall"],
                "raw_json": {"arxiv_id": "2607.00001", "arxiv_version": 1, "updated": "2026-07-17T00:00:00Z"},
            }]

            @contextmanager
            def test_connect():
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

            class FakeOpenAlex:
                def __init__(self, **_kwargs):
                    pass

            class FakeArxiv:
                def __init__(self, **_kwargs):
                    self.last_error = None

                def fetch(self, *_args, **_kwargs):
                    return [dict(current[0])], 0

            args = argparse.Namespace(
                baseline_from="2026-07-16",
                baseline_to="2026-07-17",
                scope="arxiv_live",
                journals="",
                limit_per_journal=50,
                resume=True,
                force_refresh=False,
                dry_run=False,
                mailto=None,
                include_arxiv=True,
                include_openalex_field=False,
                max_pages=1,
                sleep_seconds=0,
                timeout=1,
                incremental=True,
            )
            patches = (
                patch("backend.ingest_real.connect", test_connect),
                patch("backend.ingest_real.ensure_data_layout", return_value={}),
                patch("backend.ingest_real.logs_dir", return_value=Path(directory)),
                patch("backend.ingest_real.write_ingest_log"),
                patch("backend.ingest_real.OpenAlexClient", FakeOpenAlex),
                patch("backend.ingest_real.ArxivClient", FakeArxiv),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                first = run(args)
                repeated = run(args)
                current[0]["arxiv_version"] = 2
                current[0]["updated_date"] = "2026-07-18"
                current[0]["pdf_url"] = "https://arxiv.org/pdf/2607.00001v2"
                current[0]["url"] = "https://arxiv.org/abs/2607.00001v2"
                current[0]["raw_json"] = {"arxiv_id": "2607.00001", "arxiv_version": 2, "updated": "2026-07-18T00:00:00Z"}
                updated = run(args)

            self.assertEqual(first["fetched_count"], 1)
            self.assertEqual(first["eligible_count"], 1)
            self.assertEqual(first["review_candidates_count"], 0)
            self.assertEqual(first["inserted_count"], 1)
            self.assertEqual(first["counts"], {
                "fetched": 1,
                "inserted": 1,
                "updated": 0,
                "deduped": 0,
                "eligible": 1,
                "review_candidates": 0,
            })
            self.assertEqual(first["kept_count"], 1)
            self.assertEqual(first["kept_semantics"], "legacy_alias_of_inserted")
            self.assertEqual(first["legacy_aliases"]["kept_count"], "inserted_count")
            self.assertIn("not an eligibility", first["count_semantics"]["kept_count"])

            self.assertEqual(repeated["fetched_count"], 1)
            self.assertEqual(repeated["eligible_count"], 1)
            self.assertEqual(repeated["kept_count"], 0)
            self.assertEqual(repeated["inserted_count"], 0)
            self.assertEqual(repeated["deduped_count"], 1)

            self.assertEqual(updated["eligible_count"], 1)
            self.assertEqual(updated["kept_count"], 0)
            self.assertEqual(updated["inserted_count"], 0)
            self.assertEqual(updated["updated_count"], 1)
            self.assertEqual(updated["source_counts"]["arxiv"]["updated"], 1)


if __name__ == "__main__":
    unittest.main()