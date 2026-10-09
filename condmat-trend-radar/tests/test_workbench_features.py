from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db.database import init_db
from backend.library.remote_search import record_is_condmat, unified_remote_search
from backend.library.repository import LibraryRepository
from backend.library.workbench import (
    add_papers_to_collection,
    ensure_workbench_schema,
    get_user_state,
    list_collections,
    remove_paper_from_collection,
    save_collection,
    set_user_state,
)
from backend.migrations.unified_library import apply_unified_schema


def workbench_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    ensure_workbench_schema(connection)
    connection.commit()
    return connection


class WorkbenchStateTests(unittest.TestCase):
    def test_favorites_reading_state_collections_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = workbench_connection(Path(directory) / "radar.sqlite")
            paper = LibraryRepository(connection).upsert_version(
                {"title": "Graphene transport", "doi": "10.1000/graphene"},
                source="crossref",
                source_record_id="graphene",
            )
            state = set_user_state(
                connection,
                paper.canonical_paper_id,
                favorite=True,
                reading_status="later",
                note="Read before group meeting.",
            )
            collection = save_collection(
                connection,
                name="Twistronics",
                description="Moiré and flat-band papers",
            )
            added = add_papers_to_collection(
                connection,
                collection["id"],
                [paper.canonical_paper_id, paper.canonical_paper_id],
            )
            connection.commit()

            self.assertTrue(state["favorite"])
            self.assertEqual(state["reading_status"], "later")
            self.assertEqual(added["requested"], 1)
            self.assertEqual(added["added"], 1)
            self.assertEqual(list_collections(connection)[0]["paper_count"], 1)
            self.assertEqual(get_user_state(connection, paper.canonical_paper_id)["collections"][0]["name"], "Twistronics")
            self.assertGreaterEqual(connection.execute("SELECT COUNT(*) FROM user_action_log").fetchone()[0], 3)

            removed = remove_paper_from_collection(connection, collection["id"], paper.canonical_paper_id)
            self.assertEqual(removed["removed"], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.close()


class RemoteSearchTests(unittest.TestCase):
    def test_remote_results_are_deduplicated_and_mark_existing_local_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = workbench_connection(Path(directory) / "radar.sqlite")
            LibraryRepository(connection).upsert_version(
                {"title": "Local graphene paper", "doi": "10.1000/same"},
                source="crossref",
                source_record_id="local",
            )
            connection.commit()

            openalex = lambda _query, _limit: [{
                "id": "https://openalex.org/W1",
                "openalex_id": "https://openalex.org/W1",
                "doi": "https://doi.org/10.1000/same",
                "title": "Remote graphene paper",
                "abstract": "OpenAlex abstract",
                "publication_date": "2026-01-01",
                "journal": "PRL",
                "source": "openalex",
                "authors": ["A. Researcher"],
                "concepts": ["Condensed matter physics"],
                "is_open_access": True,
                "pdf_url": "https://example.org/paper.pdf",
            }]
            crossref = lambda _query, _limit: [{
                "id": "doi:10.1000/same",
                "doi": "10.1000/same",
                "title": "Remote graphene paper",
                "publication_date": "2026-01-01",
                "journal": "PRL",
                "source": "crossref",
                "authors": ["A. Researcher"],
            }]
            with patch.dict(
                "backend.library.remote_search.FETCHERS",
                {"openalex": openalex, "crossref": crossref},
                clear=True,
            ):
                result = unified_remote_search(
                    connection,
                    "graphene",
                    sources=["openalex", "crossref"],
                    limit=10,
                )

            self.assertEqual(result["count"], 1)
            self.assertEqual(result["deduplicated"], 1)
            self.assertTrue(result["items"][0]["is_local"])
            self.assertEqual(set(result["items"][0]["sources"]), {"openalex", "crossref"})
            self.assertTrue(result["items"][0]["condmat_evidence"])
            connection.close()

    def test_condmat_evidence_requires_source_metadata(self) -> None:
        self.assertTrue(record_is_condmat({"categories": ["cond-mat.mes-hall"]}))
        self.assertTrue(record_is_condmat({"concepts": ["Condensed matter physics"]}))
        self.assertFalse(record_is_condmat({"concepts": ["Macroeconomics"]}))


if __name__ == "__main__":
    unittest.main()
