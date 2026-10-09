from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.db.database import init_db
from backend.migrations.legacy_projects import migrate_legacy_databases
from backend.migrations.verification import verify_migration


INTAKE_SCHEMA = """
CREATE TABLE papers (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, normalized_title TEXT NOT NULL,
 authors_json TEXT NOT NULL, year INTEGER, journal TEXT, doi TEXT, arxiv_id TEXT,
 abstract TEXT, url TEXT, pdf_url TEXT, local_pdf_path TEXT, pdf_status TEXT NOT NULL,
 source TEXT, citation_count INTEGER, relevance_score REAL, relevance_reason TEXT,
 tags_json TEXT NOT NULL, chinese_summary_json TEXT NOT NULL, selected INTEGER NOT NULL,
 raw_json TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE paper_identities(namespace TEXT, identity TEXT, paper_id TEXT, created_at TEXT);
CREATE TABLE paper_aliases(alias_id TEXT, paper_id TEXT, reason TEXT, created_at TEXT);
CREATE TABLE paper_observations(id INTEGER PRIMARY KEY, paper_id TEXT, source TEXT, observed_at TEXT, payload_sha256 TEXT, payload_json TEXT);
CREATE TABLE downloaded_pdfs(id INTEGER PRIMARY KEY, paper_id TEXT, pdf_url TEXT, local_path TEXT, status TEXT, created_at TEXT);
CREATE TABLE search_runs(id INTEGER PRIMARY KEY, prompt TEXT, plan_json TEXT, result_count INTEGER, created_at TEXT);
CREATE TABLE llm_cache(cache_key TEXT PRIMARY KEY, model_name TEXT, prompt_hash TEXT, response_json TEXT, created_at TEXT);
CREATE TABLE paper_identity_conflicts(id INTEGER PRIMARY KEY, namespace TEXT, identity TEXT, existing_paper_id TEXT, incoming_paper_id TEXT, reason TEXT, payload_json TEXT, created_at TEXT);
CREATE TABLE database_anomalies(id INTEGER PRIMARY KEY, category TEXT, record_key TEXT, payload_json TEXT, created_at TEXT);
"""


class LegacyMigrationTests(unittest.TestCase):
    def test_formal_migration_is_idempotent_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            radar_path = root / "radar.sqlite"
            intake_path = root / "intake.sqlite"
            radar = sqlite3.connect(radar_path)
            radar.row_factory = sqlite3.Row
            init_db(radar)
            radar.execute(
                "INSERT INTO papers(id, title, doi, data_mode, condmat_view_eligible) VALUES ('radar-match', 'Existing', '10.1000/match', 'real', 1)"
            )
            radar.commit()
            radar.close()
            intake = sqlite3.connect(intake_path)
            intake.executescript(INTAKE_SCHEMA)
            intake.execute(
                """
                INSERT INTO papers VALUES
                ('old-1', 'Existing', 'existing', '["Jane Smith"]', 2025, 'PRL',
                 '10.1000/match', NULL, 'Abstract', 'https://example.org', NULL,
                 NULL, 'metadata_only', 'crossref', 3, 0.9, 'relevant', '["tag"]',
                 '{"problem":"test"}', 1, '{}', '2025-01-01', '2025-01-02', '2025-01-02')
                """
            )
            intake.execute("INSERT INTO paper_identities VALUES ('doi', '10.1000/match', 'old-1', '2025-01-01')")
            intake.execute("INSERT INTO paper_aliases VALUES ('alias-1', 'old-1', 'deduplicated', '2025-01-01')")
            intake.execute("INSERT INTO paper_observations VALUES (1, 'old-1', 'crossref', '2025-01-01', ?, '{}')", ("a" * 64,))
            intake.execute("INSERT INTO search_runs VALUES (1, 'graphene', '{}', 1, '2025-01-01')")
            intake.execute("INSERT INTO llm_cache VALUES ('cache', 'model', ?, '{}', '2025-01-01')", ("b" * 64,))
            intake.commit()
            intake.close()

            dry = migrate_legacy_databases(radar_path, intake_path, dry_run=True)
            first = migrate_legacy_databases(radar_path, intake_path, dry_run=False)
            second = migrate_legacy_databases(radar_path, intake_path, dry_run=False)
            verified = verify_migration(radar_path, intake_path)

            self.assertTrue(dry["dry_run"])
            self.assertEqual(first["intake_papers"]["attached"], 1)
            self.assertEqual(second["intake_papers"]["updated"], 1)
            self.assertTrue(verified["passed"])
            connection = sqlite3.connect(radar_path)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_version_observations WHERE source_project='legacy_intake'").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM monitor_queries").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0], 2)
            connection.close()


if __name__ == "__main__":
    unittest.main()
