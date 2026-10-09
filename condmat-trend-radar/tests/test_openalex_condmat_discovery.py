from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.ingest.openalex_client import OpenAlexClient


class OpenAlexCondensedMatterDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OpenAlexClient(
            api_key="test-key",
            polite_delay=0,
            warn_if_missing_key=False,
        )

    def test_page_query_uses_condmat_filter_cursor_and_one_hundred_cap(self) -> None:
        response = {"results": [], "meta": {"next_cursor": "cursor-after"}}
        with patch.object(self.client, "_get_json", return_value=response) as get_json:
            actual = self.client.fetch_condensed_matter_page(
                "2026-07-01",
                "2026-07-31",
                per_page=500,
                cursor="cursor-before",
            )

        self.assertIs(actual, response)
        endpoint, params = get_json.call_args.args
        self.assertEqual(endpoint, "/works")
        self.assertEqual(
            params["filter"],
            "topics.subfield.id:3104,"
            "from_publication_date:2026-07-01,"
            "to_publication_date:2026-07-31,"
            "type:article|preprint|review",
        )
        self.assertEqual(params["cursor"], "cursor-before")
        self.assertEqual(params["per-page"], "100")
        self.assertEqual(params["sort"], "publication_date:asc")
        selected = set(params["select"].split(","))
        self.assertTrue(
            {
                "topics",
                "primary_topic",
                "keywords",
                "locations",
                "concepts",
                "abstract_inverted_index",
            }.issubset(selected)
        )

    def test_page_query_normalizes_empty_cursor_and_lower_page_bound(self) -> None:
        with patch.object(self.client, "_get_json", return_value={}) as get_json:
            self.client.fetch_condensed_matter_page(
                "2026-08-01",
                "2026-08-02",
                per_page=0,
                cursor="",
            )

        params = get_json.call_args.args[1]
        self.assertEqual(params["cursor"], "*")
        self.assertEqual(params["per-page"], "1")

    def test_normalize_preserves_openalex_fields_and_merges_topic_names(self) -> None:
        topics = [
            {
                "id": "https://openalex.org/T100",
                "display_name": "Condensed Matter Physics",
                "subfield": {"id": "https://openalex.org/subfields/3104"},
                "score": 0.99,
            },
            {
                "id": "https://openalex.org/T200",
                "display_name": "Superconductivity",
                "score": 0.91,
            },
        ]
        primary_topic = {
            "id": "https://openalex.org/T200",
            "display_name": "Superconductivity",
            "score": 0.91,
        }
        keywords = [
            {
                "id": "https://openalex.org/keywords/quantum-materials",
                "display_name": "Quantum materials",
                "score": 0.8,
            }
        ]
        locations = [
            {
                "landing_page_url": "https://example.test/work",
                "pdf_url": "https://example.test/work.pdf",
                "source": {
                    "id": "https://openalex.org/S-repository",
                    "display_name": "Example Repository",
                    "type": "repository",
                },
            }
        ]
        work = {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1000/example",
            "title": "A &amp; B",
            "publication_date": "2026-07-10",
            "primary_location": locations[0],
            "locations": locations,
            "open_access": {"is_oa": True, "oa_status": "green"},
            "authorships": [{"author": {"display_name": "Ada Example"}}],
            "concepts": [
                {"display_name": "Physics"},
                {"display_name": "condensed matter physics"},
            ],
            "topics": topics,
            "primary_topic": primary_topic,
            "keywords": keywords,
            "type": "preprint",
            "type_crossref": "posted-content",
        }

        normalized = self.client._normalize_work(work, "Fallback Journal")

        self.assertEqual(
            normalized["concepts"],
            ["Physics", "condensed matter physics", "Superconductivity"],
        )
        self.assertIs(normalized["topics"], topics)
        self.assertIs(normalized["primary_topic"], primary_topic)
        self.assertIs(normalized["keywords"], keywords)
        self.assertIs(normalized["locations"], locations)
        self.assertIs(normalized["raw_json"], work)
        self.assertEqual(normalized["title"], "A & B")
        self.assertEqual(normalized["source_scope"], "preprint")
        self.assertEqual(normalized["pdf_url"], "https://example.test/work.pdf")
        self.assertEqual(normalized["primary_source_id"], "https://openalex.org/S-repository")
        self.assertEqual(normalized["primary_source_name"], "Example Repository")
        self.assertEqual(normalized["primary_source_type"], "repository")
        self.assertEqual(normalized["openalex_type_crossref"], "posted-content")


if __name__ == "__main__":
    unittest.main()
