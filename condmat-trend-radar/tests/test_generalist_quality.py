from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.db.database import init_db
from backend.db.reclassify_generalist_quality import run as run_generalist_reclassifier
from backend.db.strict_condmat import (
    GENERALIST_TEXT_POLICY_VERSION,
    apply_strict_condmat_policy_migrations,
    refresh_generalist_journal_flags,
    refresh_strict_condmat_flags,
)
from backend.migrations.unified_library import apply_unified_schema


MICROROBOT_DOI = "10.1038/s41467-026-76462-y"


class FetchManyOnlyCursor:
    def __init__(self, cursor: sqlite3.Cursor, sql: str, paper_fetch_sizes: list[int]) -> None:
        self._cursor = cursor
        self._sql = sql.lower()
        self._paper_fetch_sizes = paper_fetch_sizes

    def fetchmany(self, size: int | None = None):
        requested = int(size or self._cursor.arraysize)
        if "from papers" in self._sql or "from paper_terms" in self._sql:
            self._paper_fetch_sizes.append(requested)
        return self._cursor.fetchmany(requested)

    def fetchall(self):
        raise AssertionError("strict reclassification must never call fetchall")

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class FetchManyOnlyConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.paper_fetch_sizes: list[int] = []

    def execute(self, sql, *args, **kwargs):
        return FetchManyOnlyCursor(
            self._connection.execute(sql, *args, **kwargs),
            str(sql),
            self.paper_fetch_sizes,
        )

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def connection_for(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    connection.commit()
    return connection


def insert_paper(
    connection: sqlite3.Connection,
    *,
    paper_id: str,
    title: str,
    abstract: str,
    journal: str,
    doi: str = "",
    eligible: int = 1,
    reason: str = "legacy-topic-score",
) -> None:
    connection.execute(
        """
        INSERT INTO papers(
            id, doi, title, abstract, journal, source, source_scope, data_mode,
            condmat_confidence, condmat_view_eligible, condmat_view_reason
        ) VALUES (?, ?, ?, ?, ?, 'openalex', 'published', 'real', 'high', ?, ?)
        """,
        (paper_id, doi, title, abstract, journal, eligible, reason),
    )


class GeneralistHistoricalQualityTests(unittest.TestCase):
    def seed_policy_fixture(self, connection: sqlite3.Connection) -> None:
        insert_paper(
            connection,
            paper_id="robot",
            doi=MICROROBOT_DOI,
            title="Controlled flight of high-thrust ultralight ion-propelled microrobot",
            abstract="A flying robot uses propulsion, sensing, and transport control.",
            journal="Nature Communications",
        )
        # A historical OpenAlex topic must not substitute for literal text.
        connection.executemany(
            """
            INSERT INTO paper_terms(
                paper_id, term, term_type, normalized_term, confidence,
                display_eligible, display_reason, source
            ) VALUES ('robot', ?, ?, ?, 0.95, 1, 'legacy-openalex-topic', 'openalex')
            """,
            [
                ("Condensed Matter Physics", "concept", "condensed matter physics"),
                ("transport", "method", "transport"),
            ],
        )
        insert_paper(
            connection,
            paper_id="nb3cl8",
            title="Correlated magnetism in the layered material Nb3Cl8",
            abstract="Quantum magnetism and a spin liquid regime emerge in Nb3Cl8.",
            journal="Nature",
        )
        insert_paper(
            connection,
            paper_id="prb-control",
            title="Transport in a correlated lattice",
            abstract="Electronic transport is measured in a solid.",
            journal="Physical Review B",
            reason="outside-generalist-selection",
        )
        connection.commit()

    def test_targeted_reclassification_downgrades_exact_doi_without_topic_repromotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = connection_for(database)
            self.seed_policy_fixture(connection)

            preview = refresh_generalist_journal_flags(connection, dry_run=True, batch_size=1)
            before = connection.execute(
                "SELECT condmat_view_eligible FROM papers WHERE id='robot'"
            ).fetchone()[0]
            applied = refresh_generalist_journal_flags(connection, batch_size=1)
            rows = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT id,doi,condmat_view_eligible,condmat_view_reason FROM papers"
                )
            }
            connection.close()

        self.assertEqual(before, 1)
        self.assertEqual(preview["processed"], 2)
        self.assertEqual(preview["downgraded"], 1)
        self.assertEqual(applied["processed"], 2)
        self.assertEqual(rows["robot"]["doi"], MICROROBOT_DOI)
        self.assertEqual(rows["robot"]["condmat_view_eligible"], 0)
        self.assertEqual(
            rows["robot"]["condmat_view_reason"],
            "generalist_requires_condmat_text",
        )
        self.assertEqual(rows["nb3cl8"]["condmat_view_eligible"], 1)
        self.assertIn("generalist_text", rows["nb3cl8"]["condmat_view_reason"])
        self.assertEqual(rows["prb-control"]["condmat_view_eligible"], 1)
        self.assertEqual(
            rows["prb-control"]["condmat_view_reason"],
            "outside-generalist-selection",
        )

    def test_unchanged_second_pass_performs_no_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            self.seed_policy_fixture(connection)

            first = refresh_generalist_journal_flags(connection, batch_size=1)
            connection.commit()
            changes_before_second = connection.total_changes
            second = refresh_generalist_journal_flags(connection, batch_size=1)
            changes_after_second = connection.total_changes
            connection.close()

        # Robot eligibility changes; Nb3Cl8 only receives the canonical reason.
        self.assertEqual(first["changed"], 1)
        self.assertEqual(first["downgraded"], 1)
        self.assertEqual(first["promoted"], 0)
        self.assertEqual(first["updates_needed"], 2)
        self.assertEqual(first["rows_updated"], 2)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(second["downgraded"], 0)
        self.assertEqual(second["promoted"], 0)
        self.assertEqual(second["updates_needed"], 0)
        self.assertEqual(second["rows_updated"], 0)
        self.assertEqual(changes_after_second, changes_before_second)
    def test_one_time_startup_policy_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            self.seed_policy_fixture(connection)
            first = apply_strict_condmat_policy_migrations(connection, batch_size=1)
            second = apply_strict_condmat_policy_migrations(connection, batch_size=1)
            marker = connection.execute(
                "SELECT version FROM corpus_quality_policy_migrations"
            ).fetchone()[0]
            robot = connection.execute(
                "SELECT condmat_view_eligible FROM papers WHERE doi=?",
                (MICROROBOT_DOI,),
            ).fetchone()[0]
            connection.close()

        self.assertFalse(first["already_applied"])
        self.assertEqual(first["downgraded"], 1)
        self.assertTrue(second["already_applied"])
        self.assertEqual(second["processed"], 0)
        self.assertEqual(marker, GENERALIST_TEXT_POLICY_VERSION)
        self.assertEqual(robot, 0)

    def test_full_refresh_uses_fetchmany_bounded_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connection_for(Path(directory) / "radar.sqlite")
            for index in range(73):
                insert_paper(
                    connection,
                    paper_id=f"generalist-{index:03d}",
                    title=f"Ultralight robot flight study {index}",
                    abstract="Propulsion, control, and sensing are studied.",
                    journal="Nature Communications",
                )
            connection.commit()

            guarded = FetchManyOnlyConnection(connection)
            result = refresh_strict_condmat_flags(guarded, batch_size=17)
            excluded = connection.execute(
                "SELECT COUNT(*) FROM papers WHERE condmat_view_eligible=0"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(result["processed"], 73)
        self.assertEqual(excluded, 73)
        self.assertTrue(guarded.paper_fetch_sizes)
        self.assertLessEqual(max(guarded.paper_fetch_sizes), 17)

    def test_command_defaults_to_read_only_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = connection_for(database)
            self.seed_policy_fixture(connection)
            connection.close()

            preview = run_generalist_reclassifier(
                database=database,
                dry_run=True,
                batch_size=1,
            )
            connection = sqlite3.connect(database)
            robot = connection.execute(
                "SELECT condmat_view_eligible FROM papers WHERE doi=?",
                (MICROROBOT_DOI,),
            ).fetchone()[0]
            connection.close()

        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["downgraded"], 1)
        self.assertEqual(robot, 1)


if __name__ == "__main__":
    unittest.main()
