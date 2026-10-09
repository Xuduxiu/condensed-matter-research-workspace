from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


UNIFIED_SCHEMA_VERSION = 1
MIGRATION_NAME = "unified_library_v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS unified_schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_versions (
        id TEXT PRIMARY KEY,
        canonical_paper_id TEXT NOT NULL,
        version_type TEXT NOT NULL,
        title TEXT NOT NULL,
        abstract TEXT,
        doi TEXT,
        arxiv_id TEXT,
        arxiv_version INTEGER,
        journal TEXT,
        publication_date TEXT,
        submitted_date TEXT,
        updated_date TEXT,
        source TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        journal_reference TEXT,
        url TEXT,
        pdf_url TEXT,
        raw_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE(source, source_record_id),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_versions_canonical ON paper_versions(canonical_paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_versions_doi ON paper_versions(doi)",
    "CREATE INDEX IF NOT EXISTS idx_paper_versions_arxiv ON paper_versions(arxiv_id, arxiv_version)",
    "CREATE INDEX IF NOT EXISTS idx_paper_versions_title ON paper_versions(title)",
    """
    CREATE TABLE IF NOT EXISTS paper_external_ids (
        namespace TEXT NOT NULL,
        external_id TEXT NOT NULL,
        canonical_paper_id TEXT NOT NULL,
        paper_version_id TEXT,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(namespace, external_id),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE,
        FOREIGN KEY(paper_version_id) REFERENCES paper_versions(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_external_ids_paper ON paper_external_ids(canonical_paper_id)",
    """
    CREATE TABLE IF NOT EXISTS authors (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        orcid TEXT,
        affiliations_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_orcid ON authors(orcid) WHERE orcid IS NOT NULL AND orcid <> ''",
    "CREATE INDEX IF NOT EXISTS idx_authors_normalized_name ON authors(normalized_name)",
    """
    CREATE TABLE IF NOT EXISTS paper_authors (
        paper_version_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        author_position INTEGER NOT NULL,
        is_corresponding INTEGER NOT NULL DEFAULT 0,
        raw_name TEXT,
        PRIMARY KEY(paper_version_id, author_position),
        FOREIGN KEY(paper_version_id) REFERENCES paper_versions(id) ON DELETE CASCADE,
        FOREIGN KEY(author_id) REFERENCES authors(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_authors_author ON paper_authors(author_id)",
    """
    CREATE TABLE IF NOT EXISTS topics (
        id TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL UNIQUE,
        topic_type TEXT NOT NULL DEFAULT 'concept',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_topics (
        canonical_paper_id TEXT NOT NULL,
        topic_id TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(canonical_paper_id, topic_id, source),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE,
        FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS materials (
        id TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL UNIQUE,
        material_family TEXT NOT NULL,
        phase TEXT,
        thickness TEXT,
        composition TEXT,
        is_platform_material INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS material_aliases (
        alias TEXT PRIMARY KEY,
        normalized_alias TEXT NOT NULL,
        material_id TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(material_id) REFERENCES materials(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_material_aliases_normalized ON material_aliases(normalized_alias)",
    """
    CREATE TABLE IF NOT EXISTS paper_materials (
        canonical_paper_id TEXT NOT NULL,
        material_id TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL,
        context_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY(canonical_paper_id, material_id, source),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE,
        FOREIGN KEY(material_id) REFERENCES materials(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_version_links (
        source_version_id TEXT NOT NULL,
        target_version_id TEXT NOT NULL,
        link_type TEXT NOT NULL,
        match_rule TEXT NOT NULL,
        confidence REAL NOT NULL,
        reversible INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_version_id, target_version_id, link_type),
        FOREIGN KEY(source_version_id) REFERENCES paper_versions(id) ON DELETE CASCADE,
        FOREIGN KEY(target_version_id) REFERENCES paper_versions(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_files (
        id TEXT PRIMARY KEY,
        canonical_paper_id TEXT,
        paper_version_id TEXT,
        absolute_path TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,
        file_size INTEGER NOT NULL,
        mime_type TEXT NOT NULL,
        source_url TEXT,
        download_source TEXT,
        downloaded_at TEXT,
        page_count INTEGER,
        extraction_status TEXT NOT NULL DEFAULT 'pending',
        validation_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE SET NULL,
        FOREIGN KEY(paper_version_id) REFERENCES paper_versions(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_files_paper ON paper_files(canonical_paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_files_version ON paper_files(paper_version_id)",
    """
    CREATE TABLE IF NOT EXISTS paper_file_text (
        paper_file_id TEXT PRIMARY KEY,
        extractor TEXT NOT NULL,
        extractor_version TEXT,
        text_content TEXT NOT NULL,
        text_sha256 TEXT NOT NULL,
        page_count INTEGER,
        extracted_at TEXT NOT NULL,
        error_message TEXT,
        FOREIGN KEY(paper_file_id) REFERENCES paper_files(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_file_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_file_id TEXT NOT NULL,
        legacy_path TEXT NOT NULL,
        source_project TEXT NOT NULL,
        source_record_id TEXT,
        source_url TEXT,
        observed_at TEXT NOT NULL,
        UNIQUE(paper_file_id, legacy_path),
        FOREIGN KEY(paper_file_id) REFERENCES paper_files(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS download_tasks (
        id TEXT PRIMARY KEY,
        canonical_paper_id TEXT,
        paper_version_id TEXT,
        status TEXT NOT NULL CHECK(status IN (
            'pending','resolving','downloading','completed',
            'retryable_failed','permanent_failed','manual_review'
        )),
        priority INTEGER NOT NULL DEFAULT 0,
        requested_url TEXT,
        resolved_url TEXT,
        source TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE SET NULL,
        FOREIGN KEY(paper_version_id) REFERENCES paper_versions(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_download_tasks_status_next ON download_tasks(status, next_attempt_at, priority)",
    """
    CREATE TABLE IF NOT EXISTS download_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        source_url TEXT,
        http_status INTEGER,
        error_message TEXT,
        retry_after_seconds REAL,
        FOREIGN KEY(task_id) REFERENCES download_tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_queries (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        query_text TEXT NOT NULL,
        filters_json TEXT NOT NULL DEFAULT '{}',
        auto_download_oa INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_matched_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_cursors (
        source_name TEXT PRIMARY KEY,
        last_successful_cursor TEXT,
        last_successful_at TEXT,
        last_attempt_at TEXT,
        status TEXT NOT NULL DEFAULT 'never_run',
        error_message TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_runs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        trigger_type TEXT NOT NULL,
        report_json TEXT NOT NULL DEFAULT '{}',
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_run_steps (
        run_id TEXT NOT NULL,
        step_name TEXT NOT NULL,
        source_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        cursor_before TEXT,
        cursor_after TEXT,
        result_json TEXT NOT NULL DEFAULT '{}',
        error_message TEXT,
        PRIMARY KEY(run_id, step_name, source_name),
        FOREIGN KEY(run_id) REFERENCES daily_runs(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analysis_results (
        id TEXT PRIMARY KEY,
        canonical_paper_id TEXT,
        analysis_type TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        input_hash TEXT NOT NULL,
        result_json TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(canonical_paper_id, analysis_type, input_hash),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_version_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_version_id TEXT NOT NULL,
        source_project TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE(source_project, source_record_id, payload_sha256),
        FOREIGN KEY(paper_version_id) REFERENCES paper_versions(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_version_observations_version ON paper_version_observations(paper_version_id)",
    """
    CREATE TABLE IF NOT EXISTS legacy_id_aliases (
        source_project TEXT NOT NULL,
        legacy_id TEXT NOT NULL,
        canonical_paper_id TEXT NOT NULL,
        paper_version_id TEXT,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_project, legacy_id),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE,
        FOREIGN KEY(paper_version_id) REFERENCES paper_versions(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_name TEXT NOT NULL,
        source_project TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        target_table TEXT,
        target_record_id TEXT,
        action TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        migrated_at TEXT NOT NULL,
        UNIQUE(migration_name, source_fingerprint, source_record_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_migration_records_target ON migration_records(target_table, target_record_id)",
    """
    CREATE TABLE IF NOT EXISTS paper_merge_events (
        id TEXT PRIMARY KEY,
        match_rule TEXT NOT NULL,
        confidence REAL NOT NULL,
        source_record_id TEXT NOT NULL,
        source_paper_id TEXT,
        target_paper_id TEXT NOT NULL,
        reversible INTEGER NOT NULL DEFAULT 1,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(target_paper_id) REFERENCES papers(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manual_review_items (
        id TEXT PRIMARY KEY,
        review_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        source_project TEXT NOT NULL,
        source_record_id TEXT,
        candidate_paper_id TEXT,
        reason TEXT NOT NULL,
        confidence REAL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolution_json TEXT,
        FOREIGN KEY(candidate_paper_id) REFERENCES papers(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_manual_review_status ON manual_review_items(status, review_type)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
        canonical_paper_id UNINDEXED,
        paper_version_id UNINDEXED,
        title,
        abstract,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
)


@dataclass(frozen=True)
class SchemaResult:
    version: int
    migration_name: str
    checksum: str
    already_applied: bool
    dry_run: bool
    statement_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "migration_name": self.migration_name,
            "checksum": self.checksum,
            "already_applied": self.already_applied,
            "dry_run": self.dry_run,
            "statement_count": self.statement_count,
        }


def schema_checksum() -> str:
    encoded = "\n;\n".join(statement.strip() for statement in SCHEMA_STATEMENTS)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def apply_unified_schema(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> SchemaResult:
    checksum = schema_checksum()
    migration_table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_schema_migrations'"
    ).fetchone()
    existing = None
    if migration_table_exists:
        existing = conn.execute(
            "SELECT checksum FROM unified_schema_migrations WHERE version = ?",
            (UNIFIED_SCHEMA_VERSION,),
        ).fetchone()
    if existing:
        existing_checksum = str(existing[0])
        if existing_checksum != checksum:
            raise RuntimeError(
                "Unified schema migration checksum changed after it was applied"
            )
        return SchemaResult(
            UNIFIED_SCHEMA_VERSION,
            MIGRATION_NAME,
            checksum,
            True,
            dry_run,
            len(SCHEMA_STATEMENTS),
        )
    if dry_run:
        return SchemaResult(
            UNIFIED_SCHEMA_VERSION,
            MIGRATION_NAME,
            checksum,
            False,
            True,
            len(SCHEMA_STATEMENTS),
        )
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        """
        INSERT INTO unified_schema_migrations(version, name, checksum, applied_at)
        VALUES (?, ?, ?, ?)
        """,
        (UNIFIED_SCHEMA_VERSION, MIGRATION_NAME, checksum, utc_now()),
    )
    return SchemaResult(
        UNIFIED_SCHEMA_VERSION,
        MIGRATION_NAME,
        checksum,
        False,
        False,
        len(SCHEMA_STATEMENTS),
    )


def unified_schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_schema_migrations'"
    ).fetchone()
    if not table:
        return {"installed": False, "version": 0}
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT version, name, checksum, applied_at FROM unified_schema_migrations "
            "ORDER BY version"
        )
    ]
    return {
        "installed": bool(rows),
        "version": max((int(row["version"]) for row in rows), default=0),
        "migrations": rows,
    }
