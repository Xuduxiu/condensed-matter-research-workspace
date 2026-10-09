from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db import reclassify_openalex_quality as reclassify_command
from backend.db.database import init_db
from backend.db.strict_condmat import refresh_strict_condmat_flags
from backend.ingest.openalex_quality import reclassify_openalex_repository_quality
from backend.ingest_real import _store_paper
from backend.migrations.unified_library import apply_unified_schema


def zenodo_record(*, doi: str = "10.5281/zenodo.1234567") -> dict:
    return {
        "id": "https://openalex.org/W-historical-zenodo",
        "openalex_id": "https://openalex.org/W-historical-zenodo",
        "doi": doi,
        "title": "Superconductivity in a speculative unified model",
        "abstract": "A superconductivity and quantum materials proposal.",
        "publication_date": "2026-08-08",
        "journal": "Zenodo",
        "source": "openalex",
        "source_scope": "preprint",
        "data_mode": "real",
        "condmat_view_eligible": True,
        "condmat_view_reason": "legacy-openalex-topic-score",
        "raw_json": {
            "id": "https://openalex.org/W-historical-zenodo",
            "doi": f"https://doi.org/{doi}",
            "type": "preprint",
            "type_crossref": "posted-content",
            "primary_location": {
                "landing_page_url": "https://zenodo.org/records/1234567",
                "source": {
                    "display_name": "Zenodo",
                    "type": "repository",
                },
            },
        },
    }


def crossref_record(doi: str) -> dict:
    return {
        "id": "crossref:formal-version",
        "doi": doi,
        "title": "Superconductivity in a speculative unified model",
        "abstract": "A superconductivity and quantum materials study.",
        "publication_date": "2026-08-08",
        "journal": "Physical Review B",
        "source": "crossref",
        "source_scope": "published",
        "data_mode": "real",
        "condmat_view_eligible": True,
        "condmat_view_reason": "crossref-formal-version",
        "raw_json": {
            "DOI": doi,
            "type": "journal-article",
            "container-title": ["Physical Review B"],
        },
    }


class FetchManyOnlyCursor:
    def __init__(self, cursor: sqlite3.Cursor, fetch_sizes: list[int]) -> None:
        self._cursor = cursor
        self._fetch_sizes = fetch_sizes

    def fetchmany(self, size: int | None = None):
        requested = int(size or self._cursor.arraysize)
        self._fetch_sizes.append(requested)
        return self._cursor.fetchmany(requested)

    def fetchall(self):
        raise AssertionError("reclassifier must never call fetchall")

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class FetchManyOnlyConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.fetch_sizes: list[int] = []

    def execute(self, *args, **kwargs):
        return FetchManyOnlyCursor(
            self._connection.execute(*args, **kwargs), self.fetch_sizes
        )

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

class OpenAlexRepositoryHistoricalQualityTests(unittest.TestCase):
    def connection(self, database: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        init_db(connection)
        apply_unified_schema(connection)
        return connection

    def test_historical_repository_only_canonical_is_downgraded_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _store_paper(connection, zenodo_record(), dry_run=False)
            connection.commit()

            before_versions = connection.execute(
                "SELECT COUNT(*) FROM paper_versions"
            ).fetchone()[0]
            first = reclassify_openalex_repository_quality(connection)
            row = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason FROM papers"
            ).fetchone()
            after_versions = connection.execute(
                "SELECT COUNT(*) FROM paper_versions"
            ).fetchone()[0]
            second = reclassify_openalex_repository_quality(connection)
            connection.close()

        self.assertEqual(first["downgraded"], 1)
        self.assertEqual(row["condmat_view_eligible"], 0)
        self.assertIn("openalex-source-excluded:repository:zenodo", row["condmat_view_reason"])
        self.assertEqual(before_versions, after_versions)
        self.assertEqual(after_versions, 1)
        self.assertEqual(second["downgraded"], 0)
        self.assertEqual(second["already_excluded"], 1)

    def test_formal_crossref_version_prevents_downgrade(self) -> None:
        doi = "10.1103/physrevb.formal"
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _store_paper(connection, zenodo_record(doi=doi), dry_run=False)
            _store_paper(connection, crossref_record(doi), dry_run=False)
            connection.commit()

            result = reclassify_openalex_repository_quality(connection)
            eligible = connection.execute(
                "SELECT condmat_view_eligible FROM papers"
            ).fetchone()[0]
            sources = {
                row[0]
                for row in connection.execute(
                    "SELECT source FROM paper_versions"
                ).fetchall()
            }
            connection.close()

        self.assertEqual(result["downgraded"], 0)
        self.assertEqual(result["preserved_with_scholarly_version"], 1)
        self.assertEqual(eligible, 1)
        self.assertEqual(sources, {"openalex", "crossref"})

    def test_crossref_posted_content_is_not_treated_as_formal_version(self) -> None:
        doi = "10.1234/repository-record"
        posted_content = crossref_record(doi)
        posted_content["journal"] = "Zenodo"
        posted_content["raw_json"]["type"] = "posted-content"
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _store_paper(connection, zenodo_record(doi=doi), dry_run=False)
            _store_paper(connection, posted_content, dry_run=False)
            connection.commit()

            result = reclassify_openalex_repository_quality(connection)
            paper = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason FROM papers"
            ).fetchone()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM paper_versions"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["downgraded"], 1)
        self.assertEqual(paper["condmat_view_eligible"], 0)
        self.assertIn("openalex-source-excluded:repository:zenodo", paper["condmat_view_reason"])
        self.assertEqual(version_count, 2)
    def test_strict_refresh_cannot_repromote_repository_only_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _store_paper(connection, zenodo_record(), dry_run=False)
            connection.commit()

            result = refresh_strict_condmat_flags(connection)
            paper = connection.execute(
                "SELECT condmat_view_eligible, condmat_view_reason FROM papers"
            ).fetchone()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM paper_versions"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["openalex_source_quality"]["downgraded"], 1)
        self.assertEqual(paper["condmat_view_eligible"], 0)
        self.assertIn("openalex-source-excluded:repository:zenodo", paper["condmat_view_reason"])
        self.assertEqual(version_count, 1)

    def test_large_history_is_fetchmany_bounded_and_never_fetches_all(self) -> None:
        repository_raw = (
            '{"type_crossref":"posted-content","primary_location":'
            '{"landing_page_url":"https://zenodo.org/records/1","source":'
            '{"display_name":"Zenodo","type":"repository"}}}'
        )
        formal_raw = '{"type":"journal-article"}'
        journal_raw = (
            '{"primary_location":{"source":'
            '{"display_name":"Physical Review B","type":"journal"}}}'
        )
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            paper_rows = []
            version_rows = []
            for index in range(700):
                paper_id = f"repo-{index:04d}"
                doi = f"10.5281/zenodo.{index + 1}"
                paper_rows.append(
                    (paper_id, f"Repository paper {index}", "real", "openalex", 1, "legacy")
                )
                version_rows.append(
                    (
                        f"openalex-{index}", paper_id, "preprint",
                        f"Repository paper {index}", doi, None, "Zenodo",
                        "openalex", f"W-repo-{index}",
                        "https://zenodo.org/records/1", None, repository_raw,
                        "2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00",
                    )
                )
                if index % 2 == 0:
                    version_rows.append(
                        (
                            f"crossref-{index}", paper_id, "published",
                            f"Repository paper {index}", doi, None,
                            "Physical Review B", "crossref", f"doi-{index}",
                            f"https://doi.org/{doi}", None, formal_raw,
                            "2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00",
                        )
                    )
            for index in range(200):
                paper_id = f"journal-{index:04d}"
                paper_rows.append(
                    (paper_id, f"Journal paper {index}", "real", "openalex", 1, "strict")
                )
                version_rows.append(
                    (
                        f"journal-version-{index}", paper_id, "published",
                        f"Journal paper {index}", f"10.1103/test.{index}", None,
                        "Physical Review B", "openalex", f"W-journal-{index}",
                        "https://journals.aps.org/prb/", None, journal_raw,
                        "2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00",
                    )
                )
            connection.executemany(
                "INSERT INTO papers(id,title,data_mode,source,condmat_view_eligible,condmat_view_reason) "
                "VALUES (?,?,?,?,?,?)",
                paper_rows,
            )
            connection.executemany(
                """
                INSERT INTO paper_versions(
                    id,canonical_paper_id,version_type,title,doi,arxiv_id,journal,
                    source,source_record_id,url,pdf_url,raw_json,first_seen_at,last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                version_rows,
            )
            connection.commit()
            before_versions = connection.execute(
                "SELECT COUNT(*) FROM paper_versions"
            ).fetchone()[0]

            guarded = FetchManyOnlyConnection(connection)
            result = reclassify_openalex_repository_quality(guarded, batch_size=17)
            after_versions = connection.execute(
                "SELECT COUNT(*) FROM paper_versions"
            ).fetchone()[0]
            eligible = connection.execute(
                "SELECT COUNT(*) FROM papers WHERE condmat_view_eligible=1"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["processed_canonicals"], 900)
        self.assertEqual(result["repository_candidates"], 700)
        self.assertEqual(result["downgraded"], 350)
        self.assertEqual(result["preserved_with_scholarly_version"], 350)
        self.assertEqual(result["retained_source_versions"], 1050)
        self.assertEqual(before_versions, 1250)
        self.assertEqual(after_versions, before_versions)
        self.assertEqual(eligible, 550)
        self.assertGreater(len(guarded.fetch_sizes), 100)
        self.assertEqual(max(guarded.fetch_sizes), 17)

    def test_reclassifier_does_not_commit_callers_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _action, canonical_id = _store_paper(
                connection, zenodo_record(), dry_run=False
            )
            connection.commit()
            connection.execute("BEGIN")

            result = reclassify_openalex_repository_quality(
                connection, canonical_ids=[canonical_id], batch_size=1
            )
            changed_inside_transaction = connection.execute(
                "SELECT condmat_view_eligible FROM papers WHERE id=?", (canonical_id,)
            ).fetchone()[0]
            transaction_still_open = connection.in_transaction
            connection.rollback()
            restored = connection.execute(
                "SELECT condmat_view_eligible FROM papers WHERE id=?", (canonical_id,)
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["downgraded"], 1)
        self.assertEqual(changed_inside_transaction, 0)
        self.assertTrue(transaction_still_open)
        self.assertEqual(restored, 1)

    def test_subset_and_dry_run_are_respected(self) -> None:
        second = zenodo_record(doi="10.5281/zenodo.7654321")
        second["id"] = "https://openalex.org/W-historical-zenodo-2"
        second["openalex_id"] = second["id"]
        second["title"] = "A second repository-only superconductivity model"
        second["raw_json"]["id"] = second["id"]
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _first_action, first_id = _store_paper(
                connection, zenodo_record(), dry_run=False
            )
            _second_action, second_id = _store_paper(
                connection, second, dry_run=False
            )
            connection.commit()

            preview = reclassify_openalex_repository_quality(
                connection, canonical_ids=[first_id], dry_run=True, batch_size=1
            )
            after_preview = {
                row["id"]: row["condmat_view_eligible"]
                for row in connection.execute(
                    "SELECT id,condmat_view_eligible FROM papers"
                )
            }
            applied = reclassify_openalex_repository_quality(
                connection, canonical_ids=[first_id], batch_size=1
            )
            after_apply = {
                row["id"]: row["condmat_view_eligible"]
                for row in connection.execute(
                    "SELECT id,condmat_view_eligible FROM papers"
                )
            }
            connection.close()

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(preview["processed_canonicals"], 1)
        self.assertEqual(preview["downgraded"], 1)
        self.assertEqual(after_preview[first_id], 1)
        self.assertEqual(after_preview[second_id], 1)
        self.assertEqual(applied["downgraded"], 1)
        self.assertEqual(after_apply[first_id], 0)
        self.assertEqual(after_apply[second_id], 1)

    def test_empty_subset_never_expands_to_full_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = self.connection(Path(directory) / "radar.sqlite")
            _store_paper(connection, zenodo_record(), dry_run=False)
            connection.commit()

            result = reclassify_openalex_repository_quality(
                connection, canonical_ids=[]
            )
            eligible = connection.execute(
                "SELECT condmat_view_eligible FROM papers"
            ).fetchone()[0]
            connection.close()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "empty_canonical_subset")
        self.assertEqual(result["selection_mode"], "subset")
        self.assertEqual(result["subset_size"], 0)
        self.assertEqual(result["processed_canonicals"], 0)
        self.assertEqual(eligible, 1)

    def test_cli_default_dry_run_uses_only_read_only_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = self.connection(database)
            _store_paper(connection, zenodo_record(), dry_run=False)
            connection.commit()
            connection.close()

            statements: list[str] = []
            connect_calls: list[tuple[tuple, dict]] = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connect_calls.append((args, kwargs))
                traced = real_connect(*args, **kwargs)
                traced.set_trace_callback(statements.append)
                return traced

            with (
                patch.object(
                    reclassify_command.sqlite3,
                    "connect",
                    side_effect=traced_connect,
                ),
                patch.object(reclassify_command, "init_db") as init_schema,
                patch.object(reclassify_command, "apply_unified_schema") as migrate_schema,
            ):
                result = reclassify_command.run(dry_run=True, database=database)

            verification = real_connect(database)
            eligible = verification.execute(
                "SELECT condmat_view_eligible FROM papers"
            ).fetchone()[0]
            verification.close()

        init_schema.assert_not_called()
        migrate_schema.assert_not_called()
        self.assertEqual(len(connect_calls), 1)
        self.assertIn("mode=ro", str(connect_calls[0][0][0]))
        self.assertTrue(connect_calls[0][1].get("uri"))
        write_prefixes = (
            "BEGIN", "COMMIT", "ROLLBACK", "CREATE", "ALTER", "DROP",
            "INSERT", "UPDATE", "DELETE", "REPLACE", "VACUUM", "REINDEX",
            "ANALYZE", "ATTACH", "DETACH",
        )
        attempted_writes = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(write_prefixes)
        ]
        self.assertEqual(attempted_writes, [])
        self.assertEqual(result["downgraded"], 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual(eligible, 1)

if __name__ == "__main__":
    unittest.main()
