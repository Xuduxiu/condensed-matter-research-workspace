from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from backend.library.deduplication import normalize_doi, parse_arxiv_id
from backend.library.repository import (
    LibraryRepository,
    json_text,
    seed_radar_authors,
    seed_radar_versions,
    seed_topics_and_materials,
    stable_id,
    utc_now,
)
from backend.migrations.unified_library import apply_unified_schema


MIGRATION_NAME = "legacy_projects_to_unified_library_v1"


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def table_count(connection: sqlite3.Connection, table: str) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _identities(connection: sqlite3.Connection) -> tuple[set[str], set[str]]:
    dois: set[str] = set()
    arxiv_ids: set[str] = set()
    for row in connection.execute("SELECT doi, arxiv_id FROM papers"):
        doi = normalize_doi(row["doi"])
        arxiv_id, _ = parse_arxiv_id(row["arxiv_id"])
        if doi:
            dois.add(doi)
        if arxiv_id:
            arxiv_ids.add(arxiv_id.lower())
    return dois, arxiv_ids


def audit_legacy_projects(radar_db: Path, intake_db: Path) -> dict[str, Any]:
    radar_db = radar_db.resolve()
    intake_db = intake_db.resolve()
    if not radar_db.is_file():
        raise FileNotFoundError(f"Radar database not found: {radar_db}")
    if not intake_db.is_file():
        raise FileNotFoundError(f"Intake database not found: {intake_db}")
    with read_only_connection(radar_db) as radar, read_only_connection(intake_db) as intake:
        radar_dois, radar_arxiv = _identities(radar)
        exact_doi = exact_arxiv = unmatched = downloaded = 0
        intake_rows = table_count(intake, "papers")
        for row in intake.execute("SELECT doi, arxiv_id, pdf_status FROM papers"):
            doi = normalize_doi(row["doi"])
            arxiv_id, _ = parse_arxiv_id(row["arxiv_id"])
            if doi and doi in radar_dois:
                exact_doi += 1
            elif arxiv_id and arxiv_id.lower() in radar_arxiv:
                exact_arxiv += 1
            else:
                unmatched += 1
            if row["pdf_status"] == "downloaded":
                downloaded += 1
        radar_counts = {
            name: table_count(radar, name)
            for name in (
                "papers",
                "paper_terms",
                "term_month_stats",
                "monthly_corpus_stats",
                "concept_lifecycle",
                "published_preprint_stats",
                "ingest_runs",
                "ingest_chunks",
                "ingest_checkpoints",
                "update_runs",
            )
        }
        intake_counts = {
            name: table_count(intake, name)
            for name in (
                "papers",
                "paper_identities",
                "paper_aliases",
                "paper_observations",
                "downloaded_pdfs",
                "search_runs",
                "llm_cache",
                "paper_identity_conflicts",
                "database_anomalies",
            )
        }
        return {
            "status": "PASS",
            "read_only": True,
            "radar": {
                "path": str(radar_db),
                "size_bytes": radar_db.stat().st_size,
                "counts": radar_counts,
                "quick_check": [row[0] for row in radar.execute("PRAGMA quick_check")],
                "foreign_key_errors": len(radar.execute("PRAGMA foreign_key_check").fetchall()),
            },
            "intake": {
                "path": str(intake_db),
                "size_bytes": intake_db.stat().st_size,
                "counts": intake_counts,
                "quick_check": [row[0] for row in intake.execute("PRAGMA quick_check")],
                "foreign_key_errors": len(intake.execute("PRAGMA foreign_key_check").fetchall()),
            },
            "identity_plan": {
                "intake_papers": intake_rows,
                "exact_doi_matches": exact_doi,
                "exact_arxiv_matches": exact_arxiv,
                "new_library_only_records": unmatched,
                "manual_corpus_reviews": unmatched,
                "downloaded_canonical_papers": downloaded,
                "title_only_auto_merges": 0,
            },
        }


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _paper_record(row: sqlite3.Row) -> dict[str, Any]:
    raw = _parse_json(row["raw_json"], {})
    return {
        "title": row["title"],
        "authors": _parse_json(row["authors_json"], []),
        "year": row["year"],
        "journal": row["journal"],
        "doi": row["doi"],
        "arxiv_id": row["arxiv_id"],
        "abstract": row["abstract"],
        "url": row["url"],
        "pdf_url": row["pdf_url"],
        "citation_count": row["citation_count"],
        "source": row["source"] or "legacy_intake",
        "raw_json": raw,
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "updated_at": row["updated_at"],
        "tags": _parse_json(row["tags_json"], []),
        "chinese_summary": _parse_json(row["chinese_summary_json"], {}),
        "relevance_score": row["relevance_score"],
        "relevance_reason": row["relevance_reason"],
    }


def _record_ledger(
    connection: sqlite3.Connection,
    source_fingerprint: str,
    source_record_id: str,
    target_table: str,
    target_record_id: str | None,
    action: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO migration_records
        (migration_name, source_project, source_fingerprint, source_record_id,
         target_table, target_record_id, action, details_json, migrated_at)
        VALUES (?, 'lab_paper_intake', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            MIGRATION_NAME,
            source_fingerprint,
            source_record_id,
            target_table,
            target_record_id,
            action,
            json_text(details or {}, {}),
            utc_now(),
        ),
    )


def _migrate_intake_papers(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    fingerprint: str,
) -> dict[str, int]:
    repository = LibraryRepository(target)
    counts = {"processed": 0, "created": 0, "attached": 0, "updated": 0, "reviews": 0}
    for row in source.execute("SELECT * FROM papers ORDER BY id"):
        record = _paper_record(row)
        result = repository.upsert_version(
            record,
            source="legacy_intake",
            source_record_id=str(row["id"]),
            legacy_id=str(row["id"]),
            default_condmat_eligible=False,
            create_corpus_review=True,
        )
        counts["processed"] += 1
        if result.action == "created_canonical":
            counts["created"] += 1
        elif result.action == "attached_version":
            counts["attached"] += 1
        else:
            counts["updated"] += 1
        counts["reviews"] += int(result.manual_review_created)
        summary = record.get("chinese_summary") or {}
        if summary and any(str(value).strip() for value in summary.values()):
            input_hash = hashlib.sha256(str(row["id"]).encode("utf-8")).hexdigest()
            analysis_id = stable_id("analysis", f"legacy-summary:{row['id']}")
            target.execute(
                """
                INSERT OR IGNORE INTO analysis_results
                (id, canonical_paper_id, analysis_type, provider, model, input_hash,
                 result_json, status, created_at, updated_at)
                VALUES (?, ?, 'legacy_chinese_summary', 'lab_paper_intake', NULL,
                        ?, ?, 'preserved', ?, ?)
                """,
                (
                    analysis_id,
                    result.canonical_paper_id,
                    input_hash,
                    json_text(summary, {}),
                    row["created_at"] or utc_now(),
                    row["updated_at"] or utc_now(),
                ),
            )
        _record_ledger(
            target,
            fingerprint,
            f"papers:{row['id']}",
            "paper_versions",
            result.paper_version_id,
            result.action,
            result.as_dict(),
        )
    return counts


def _migrate_observations_and_aliases(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    fingerprint: str,
) -> dict[str, int]:
    observations = aliases = identities = 0
    for row in source.execute("SELECT * FROM paper_observations ORDER BY id"):
        link = target.execute(
            "SELECT canonical_paper_id, paper_version_id FROM legacy_id_aliases "
            "WHERE source_project='legacy_intake' AND legacy_id=?",
            (row["paper_id"],),
        ).fetchone()
        if not link:
            continue
        target.execute(
            """
            INSERT OR IGNORE INTO paper_version_observations
            (paper_version_id, source_project, source_record_id, observed_at,
             payload_sha256, payload_json)
            VALUES (?, 'legacy_intake', ?, ?, ?, ?)
            """,
            (
                link["paper_version_id"],
                f"observation:{row['id']}",
                row["observed_at"],
                row["payload_sha256"],
                row["payload_json"],
            ),
        )
        observations += 1
        _record_ledger(target, fingerprint, f"paper_observations:{row['id']}", "paper_version_observations", str(row["id"]), "preserved")
    for row in source.execute("SELECT * FROM paper_aliases ORDER BY alias_id"):
        link = target.execute(
            "SELECT canonical_paper_id, paper_version_id FROM legacy_id_aliases "
            "WHERE source_project='legacy_intake' AND legacy_id=?",
            (row["paper_id"],),
        ).fetchone()
        if not link:
            continue
        target.execute(
            """
            INSERT INTO legacy_id_aliases
            (source_project, legacy_id, canonical_paper_id, paper_version_id, reason, created_at)
            VALUES ('legacy_intake', ?, ?, ?, ?, ?)
            ON CONFLICT(source_project, legacy_id) DO UPDATE SET
              canonical_paper_id=excluded.canonical_paper_id,
              paper_version_id=excluded.paper_version_id,
              reason=excluded.reason
            """,
            (row["alias_id"], link["canonical_paper_id"], link["paper_version_id"], row["reason"], row["created_at"]),
        )
        aliases += 1
    for row in source.execute("SELECT * FROM paper_identities ORDER BY namespace, identity"):
        link = target.execute(
            "SELECT canonical_paper_id, paper_version_id FROM legacy_id_aliases "
            "WHERE source_project='legacy_intake' AND legacy_id=?",
            (row["paper_id"],),
        ).fetchone()
        if not link:
            continue
        namespace = str(row["namespace"]).lower()
        identity = str(row["identity"]).lower()
        target.execute(
            """
            INSERT OR IGNORE INTO paper_external_ids
            (namespace, external_id, canonical_paper_id, paper_version_id, source, created_at)
            VALUES (?, ?, ?, ?, 'legacy_intake_identity', ?)
            """,
            (namespace, identity, link["canonical_paper_id"], link["paper_version_id"], row["created_at"]),
        )
        identities += 1
    return {"observations_seen": observations, "aliases_seen": aliases, "identities_seen": identities}


def _migrate_history(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    fingerprint: str,
) -> dict[str, int]:
    searches = cache = reviews = 0
    for row in source.execute("SELECT * FROM search_runs ORDER BY id"):
        monitor_id = stable_id("monitor", f"legacy-search:{row['id']}")
        target.execute(
            """
            INSERT OR IGNORE INTO monitor_queries
            (id, name, query_text, filters_json, auto_download_oa, enabled,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                monitor_id,
                f"Legacy search {row['id']}",
                row["prompt"],
                row["plan_json"] or "{}",
                row["created_at"],
                row["created_at"],
            ),
        )
        searches += 1
        _record_ledger(target, fingerprint, f"search_runs:{row['id']}", "monitor_queries", monitor_id, "preserved_disabled")
    for row in source.execute("SELECT * FROM llm_cache ORDER BY cache_key"):
        analysis_id = stable_id("analysis", f"legacy-cache:{row['cache_key']}")
        target.execute(
            """
            INSERT OR IGNORE INTO analysis_results
            (id, canonical_paper_id, analysis_type, provider, model, input_hash,
             result_json, status, created_at, updated_at)
            VALUES (?, NULL, 'legacy_llm_cache', 'lab_paper_intake', ?, ?, ?,
                    'preserved', ?, ?)
            """,
            (
                analysis_id,
                row["model_name"],
                row["prompt_hash"],
                row["response_json"],
                row["created_at"],
                row["created_at"],
            ),
        )
        cache += 1
    for table, review_type in (("paper_identity_conflicts", "legacy_identity_conflict"), ("database_anomalies", "legacy_database_anomaly")):
        if not table_count(source, table):
            continue
        for row in source.execute(f"SELECT * FROM {table} ORDER BY id"):
            review_id = stable_id("review", f"{table}:{row['id']}")
            target.execute(
                """
                INSERT OR IGNORE INTO manual_review_items
                (id, review_type, status, source_project, source_record_id,
                 reason, payload_json, created_at)
                VALUES (?, ?, 'pending', 'legacy_intake', ?, ?, ?, ?)
                """,
                (
                    review_id,
                    review_type,
                    str(row["id"]),
                    str(row["reason"] if "reason" in row.keys() else row["category"]),
                    json_text(dict(row), {}),
                    row["created_at"],
                ),
            )
            reviews += 1
    return {"searches_preserved": searches, "llm_cache_preserved": cache, "legacy_reviews": reviews}


def migrate_legacy_databases(
    radar_db: Path,
    intake_db: Path,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    audit = audit_legacy_projects(radar_db, intake_db)
    if dry_run:
        return {
            "status": "PASS",
            "dry_run": True,
            "writes_performed": False,
            "audit": audit,
            "planned": {
                "apply_unified_schema": True,
                "seed_existing_radar_versions": audit["radar"]["counts"]["papers"],
                "migrate_intake_papers": audit["intake"]["counts"]["papers"],
                "preserve_observations": audit["intake"]["counts"]["paper_observations"],
                "preserve_aliases": audit["intake"]["counts"]["paper_aliases"],
                "manual_corpus_reviews": audit["identity_plan"]["manual_corpus_reviews"],
            },
        }
    fingerprint = file_sha256(intake_db)
    target = sqlite3.connect(radar_db, timeout=60)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys = ON")
    target.execute("PRAGMA busy_timeout = 60000")
    try:
        target.execute("BEGIN IMMEDIATE")
        schema = apply_unified_schema(target)
        seeded_versions = seed_radar_versions(target)
        seeded_authors = seed_radar_authors(target)
        seeded_terms = seed_topics_and_materials(target)
        with read_only_connection(intake_db) as source:
            papers = _migrate_intake_papers(target, source, fingerprint)
            identity = _migrate_observations_and_aliases(target, source, fingerprint)
            history = _migrate_history(target, source, fingerprint)
        foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"foreign key validation failed: {len(foreign_keys)} rows")
        target.commit()
        return {
            "status": "PASS",
            "dry_run": False,
            "writes_performed": True,
            "schema": schema.as_dict(),
            "radar_versions": seeded_versions,
            "radar_authors": seeded_authors,
            "radar_topics_materials": seeded_terms,
            "intake_papers": papers,
            "identity_history": identity,
            "other_history": history,
            "source_fingerprint": fingerprint,
        }
    except BaseException:
        target.rollback()
        raise
    finally:
        target.close()


def sqlite_backup(source_path: Path, backup_path: Path) -> dict[str, Any]:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_path, timeout=60)) as source, closing(
        sqlite3.connect(backup_path)
    ) as target:
        source.backup(target, pages=4096)
        target.commit()
        quick = [row[0] for row in target.execute("PRAGMA quick_check")]
    return {
        "path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "sha256": file_sha256(backup_path),
        "quick_check": quick,
    }
