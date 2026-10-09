import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from paper_intake.database_admin import (
    backup_database,
    database_status,
    restore_database,
)
from paper_intake.db import SCHEMA_VERSION, connect, init_db, upsert_papers
from paper_intake.models import ChineseSummary, Paper


def test_cross_run_upsert_uses_one_canonical_paper(tmp_path):
    db_path = tmp_path / "papers.db"
    upsert_papers(
        [
            Paper(
                id="old-id",
                title="Stable Paper Identity",
                doi="10.1000/stable",
                pdf_status="oa_available",
            )
        ],
        db_path,
    )
    upsert_papers(
        [
            Paper(
                id="new-id",
                title="Stable Paper Identity",
                doi="https://doi.org/10.1000/stable",
                pdf_status="downloaded",
                local_pdf_path="pdfs/stable.pdf",
            )
        ],
        db_path,
    )

    with closing(sqlite3.connect(db_path)) as conn:
        paper = conn.execute(
            "SELECT id, pdf_status, local_pdf_path FROM papers"
        ).fetchone()
        alias = conn.execute(
            "SELECT paper_id FROM paper_aliases WHERE alias_id = 'new-id'"
        ).fetchone()
        observations = conn.execute(
            "SELECT COUNT(*) FROM paper_observations"
        ).fetchone()[0]
    assert paper == ("old-id", "downloaded", "pdfs/stable.pdf")
    assert alias == ("old-id",)
    assert observations == 2


def _create_legacy_database(db_path: Path) -> None:
    summary_json = ChineseSummary().model_dump_json()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE papers (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                year INTEGER,
                journal TEXT,
                doi TEXT,
                arxiv_id TEXT,
                abstract TEXT,
                url TEXT,
                pdf_url TEXT,
                local_pdf_path TEXT,
                pdf_status TEXT NOT NULL,
                source TEXT,
                citation_count INTEGER,
                relevance_score REAL,
                relevance_reason TEXT,
                tags_json TEXT NOT NULL,
                chinese_summary_json TEXT NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE llm_cache (
                cache_key TEXT PRIMARY KEY,
                model_name TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE downloaded_pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT NOT NULL,
                pdf_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        rows = [
            (
                "legacy-a",
                "Legacy Duplicate",
                "legacy duplicate",
                json.dumps(["A. Author"]),
                2024,
                "Journal",
                "10.1000/legacy",
                None,
                "Short abstract",
                "https://example.org/a",
                None,
                None,
                "metadata_only",
                "crossref",
                2,
                5.0,
                None,
                "[]",
                summary_json,
                0,
                json.dumps({"source_record": "a"}),
                "2026-01-01T00:00:00+00:00",
            ),
            (
                "legacy-b",
                "Legacy Duplicate",
                "legacy duplicate",
                json.dumps(["Alice Author"]),
                2024,
                "Journal",
                "https://doi.org/10.1000/legacy",
                None,
                "A much stronger and longer abstract",
                "https://example.org/b",
                "https://example.org/b.pdf",
                "pdfs/legacy.pdf",
                "downloaded",
                "publisher",
                7,
                8.0,
                "relevant",
                json.dumps(["legacy"]),
                summary_json,
                1,
                json.dumps({"source_record": "b"}),
                "2026-01-02T00:00:00+00:00",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO papers (
                id, title, normalized_title, authors_json, year, journal, doi,
                arxiv_id, abstract, url, pdf_url, local_pdf_path, pdf_status,
                source, citation_count, relevance_score, relevance_reason,
                tags_json, chinese_summary_json, selected, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute(
            """
            INSERT INTO downloaded_pdfs
            (paper_id, pdf_url, local_path, status, created_at)
            VALUES ('legacy-a', 'https://example.org/b.pdf', 'pdfs/legacy.pdf',
                    'downloaded', '2026-01-03T00:00:00+00:00')
            """
        )
        conn.commit()


def test_legacy_migration_preserves_observations_alias_and_download(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(db_path)
    init_db(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        canonical = conn.execute("SELECT * FROM papers").fetchone()
        aliases = conn.execute("SELECT * FROM paper_aliases").fetchall()
        observations = conn.execute("SELECT * FROM paper_observations").fetchall()
        download = conn.execute("SELECT paper_id FROM downloaded_pdfs").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert canonical["id"] == "legacy-b"
    assert canonical["pdf_status"] == "downloaded"
    assert canonical["selected"] == 1
    assert len(aliases) == 1
    assert aliases[0]["alias_id"] == "legacy-a"
    assert aliases[0]["paper_id"] == "legacy-b"
    assert len(observations) == 2
    assert download["paper_id"] == "legacy-b"
    assert version == SCHEMA_VERSION
    assert foreign_key_errors == []


def test_connect_rolls_back_on_exception(tmp_path):
    db_path = tmp_path / "rollback.db"
    init_db(db_path)
    with pytest.raises(RuntimeError, match="stop"):
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO search_runs(prompt, plan_json, result_count, created_at) "
                "VALUES ('must rollback', '{}', 0, 'now')"
            )
            raise RuntimeError("stop")
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0] == 0


def test_backup_and_restore_round_trip(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    upsert_papers([Paper(id="kept", title="Kept paper")], db_path)
    backup = backup_database(db_path, tmp_path / "manual_backups")
    assert len(backup["sha256"]) == 64
    assert Path(backup["manifest"]).exists()

    upsert_papers([Paper(id="later", title="Later paper")], db_path)
    restored = restore_database(Path(backup["backup"]), db_path, confirm=True)
    assert restored["status"]["healthy"] is True
    assert restored["status"]["counts"]["papers"] == 1
    assert database_status(db_path)["user_version"] == SCHEMA_VERSION
    assert list(tmp_path.glob(".*.restore-*.tmp*")) == []
