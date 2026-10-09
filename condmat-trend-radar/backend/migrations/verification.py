from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from backend.migrations.legacy_projects import read_only_connection, table_count
from backend.migrations.unified_library import unified_schema_status


def verify_migration(radar_db: Path, intake_db: Path | None = None) -> dict[str, Any]:
    with read_only_connection(radar_db) as connection:
        required = (
            "paper_versions",
            "authors",
            "topics",
            "materials",
            "paper_files",
            "download_tasks",
            "monitor_queries",
            "source_cursors",
            "daily_runs",
            "analysis_results",
            "migration_records",
            "manual_review_items",
            "library_fts",
            "legacy_id_aliases",
        )
        missing = [name for name in required if not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()]
        quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        checks: dict[str, Any] = {
            "unified_schema": unified_schema_status(connection),
            "missing_required_objects": missing,
            "quick_check": quick,
            "foreign_key_errors": len(foreign_keys),
            "counts": {name: table_count(connection, name) for name in required if name not in missing},
        }
        if "paper_versions" not in missing:
            checks["orphan_versions"] = int(connection.execute(
                "SELECT COUNT(*) FROM paper_versions v LEFT JOIN papers p ON p.id=v.canonical_paper_id WHERE p.id IS NULL"
            ).fetchone()[0])
            checks["duplicate_source_versions"] = int(connection.execute(
                "SELECT COUNT(*) FROM (SELECT source, source_record_id FROM paper_versions GROUP BY source, source_record_id HAVING COUNT(*)>1)"
            ).fetchone()[0])
        if "paper_files" not in missing:
            checks["duplicate_file_hashes"] = int(connection.execute(
                "SELECT COUNT(*) FROM (SELECT sha256 FROM paper_files GROUP BY sha256 HAVING COUNT(*)>1)"
            ).fetchone()[0])
            checks["missing_canonical_pdf_paths"] = int(connection.execute(
                "SELECT COUNT(*) FROM paper_files WHERE absolute_path IS NULL OR absolute_path=''"
            ).fetchone()[0])
        if intake_db and intake_db.is_file() and "legacy_id_aliases" not in missing:
            with read_only_connection(intake_db) as intake:
                source_papers = table_count(intake, "papers")
                source_observations = table_count(intake, "paper_observations")
                source_aliases = table_count(intake, "paper_aliases")
            checks["legacy_parity"] = {
                "source_papers": source_papers,
                "migrated_paper_ids": int(connection.execute(
                    "SELECT COUNT(*) FROM legacy_id_aliases WHERE source_project='legacy_intake'"
                ).fetchone()[0]),
                "source_observations": source_observations,
                "migrated_observations": int(connection.execute(
                    "SELECT COUNT(*) FROM paper_version_observations WHERE source_project='legacy_intake'"
                ).fetchone()[0]),
                "source_aliases": source_aliases,
            }
        passed = quick == ["ok"] and not missing and not foreign_keys
        passed = passed and checks.get("orphan_versions", 0) == 0
        passed = passed and checks.get("duplicate_source_versions", 0) == 0
        passed = passed and checks.get("duplicate_file_hashes", 0) == 0
        return {"status": "PASS" if passed else "BLOCKED", "passed": passed, "checks": checks}
