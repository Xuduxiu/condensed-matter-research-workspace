from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from backend.db.database import upsert_paper
from backend.ingest.arxiv_client import ArxivClient
from backend.ingest.crossref_client import CrossrefClient
from backend.ingest.openalex_client import OpenAlexClient
from backend.library.deduplication import normalize_doi, parse_arxiv_id
from backend.library.repository import LibraryRepository


CROSSCHECK_SOURCES = ("openalex", "crossref", "arxiv")
IGNORED_VERSION_SOURCES = {"", "radar_legacy", "legacy_intake", "manual", "imported", "repository"}


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS source_coverage_audits (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        window_from TEXT,
        window_to TEXT,
        canonical_count INTEGER NOT NULL,
        cross_source_count INTEGER NOT NULL,
        single_source_count INTEGER NOT NULL,
        stable_identity_count INTEGER NOT NULL,
        queued_count INTEGER NOT NULL DEFAULT 0,
        report_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_coverage_created ON source_coverage_audits(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS source_backfill_queue (
        id TEXT PRIMARY KEY,
        canonical_paper_id TEXT NOT NULL,
        target_source TEXT NOT NULL,
        lookup_type TEXT NOT NULL,
        lookup_value TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_attempt_at TEXT,
        completed_at TEXT,
        UNIQUE(canonical_paper_id, target_source, lookup_type, lookup_value),
        FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_backfill_status ON source_backfill_queue(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_source_backfill_paper ON source_backfill_queue(canonical_paper_id, target_source)",
    """
    CREATE TABLE IF NOT EXISTS source_coverage_state (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_source_coverage_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)


def seed_canonical_source_versions(
    connection: sqlite3.Connection,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Recover historical provider provenance that older Radar seeds flattened."""
    ensure_source_coverage_schema(connection)
    state = connection.execute(
        "SELECT value_json FROM source_coverage_state WHERE key='canonical_source_seed_v1'"
    ).fetchone()
    if state and not force:
        try:
            payload = json.loads(state["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return {"already_seeded": True, **payload}
    before = int(
        connection.execute(
            "SELECT COUNT(*) FROM paper_versions WHERE lower(source) IN ('openalex','crossref','arxiv')"
        ).fetchone()[0]
    )
    now = utc_now()
    connection.execute(
        """
        INSERT OR IGNORE INTO paper_versions (
            id, canonical_paper_id, version_type, title, abstract, doi,
            arxiv_id, arxiv_version, journal, publication_date, submitted_date,
            updated_date, source, source_record_id, journal_reference,
            url, pdf_url, raw_json, first_seen_at, last_seen_at
        )
        SELECT
            'source-version:' || lower(source) || ':' || id,
            id,
            CASE
                WHEN lower(source)='arxiv' OR (COALESCE(arxiv_id,'')<>'' AND COALESCE(doi,'')='') THEN 'preprint'
                WHEN COALESCE(doi,'')<>'' THEN 'published'
                ELSE 'metadata'
            END,
            title, COALESCE(abstract,''), NULLIF(lower(doi),''),
            NULLIF(arxiv_id,''), NULL, COALESCE(journal,''),
            COALESCE(publication_date,''),
            CASE WHEN lower(source)='arxiv' THEN COALESCE(publication_date,'') ELSE '' END,
            COALESCE(updated_at,''), lower(source),
            CASE
                WHEN lower(source)='openalex' AND COALESCE(openalex_id,'')<>'' THEN lower(openalex_id)
                WHEN lower(source)='arxiv' AND COALESCE(arxiv_id,'')<>'' THEN lower(arxiv_id)
                WHEN COALESCE(doi,'')<>'' THEN lower(doi)
                ELSE id
            END,
            '', COALESCE(url,''), COALESCE(pdf_url,''), COALESCE(raw_json,'{}'),
            COALESCE(created_at, ?), COALESCE(updated_at, created_at, ?)
        FROM papers
        WHERE data_mode='real' AND lower(source) IN ('openalex','crossref','arxiv')
        """,
        (now, now),
    )
    after = int(
        connection.execute(
            "SELECT COUNT(*) FROM paper_versions WHERE lower(source) IN ('openalex','crossref','arxiv')"
        ).fetchone()[0]
    )
    result = {"already_seeded": False, "before": before, "after": after, "inserted": after - before}
    connection.execute(
        """
        INSERT INTO source_coverage_state(key, value_json, updated_at)
        VALUES ('canonical_source_seed_v1', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
        """,
        (json.dumps(result, ensure_ascii=False, sort_keys=True), now),
    )
    return result

def normalize_source(value: Any) -> str:
    source = str(value or "").strip().lower()
    if "openalex" in source:
        return "openalex"
    if "crossref" in source:
        return "crossref"
    if source == "arxiv" or source.startswith("arxiv_"):
        return "arxiv"
    return source


def _paper_where(scope: str, window_from: str | None, window_to: str | None) -> tuple[str, list[Any]]:
    clauses = ["p.data_mode='real'"]
    params: list[Any] = []
    if scope in {"eligible", "strict", "core"}:
        clauses.append("COALESCE(p.condmat_view_eligible,0)=1")
    if window_from:
        clauses.append("COALESCE(p.publication_date,'') >= ?")
        params.append(window_from)
    if window_to:
        clauses.append("COALESCE(p.publication_date,'') <= ?")
        params.append(window_to)
    return " AND ".join(clauses), params


def _source_sets(
    connection: sqlite3.Connection,
    papers: Iterable[sqlite3.Row],
    where: str,
    params: list[Any],
) -> dict[str, set[str]]:
    paper_rows = list(papers)
    output: dict[str, set[str]] = {str(row["id"]): set() for row in paper_rows}
    has_versions = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_versions'"
    ).fetchone()
    if has_versions:
        rows = connection.execute(
            f"""
            SELECT v.canonical_paper_id, v.source
            FROM paper_versions v
            JOIN papers p ON p.id=v.canonical_paper_id
            WHERE {where}
            GROUP BY v.canonical_paper_id, lower(v.source)
            """,
            params,
        ).fetchall()
        for row in rows:
            source = normalize_source(row["source"])
            if source not in IGNORED_VERSION_SOURCES:
                output.setdefault(str(row["canonical_paper_id"]), set()).add(source)
    for row in paper_rows:
        paper_id = str(row["id"])
        if not output.get(paper_id):
            source = normalize_source(row["source"])
            if source:
                output.setdefault(paper_id, set()).add(source)
    return output


def _queue_gap(
    connection: sqlite3.Connection,
    *,
    canonical_paper_id: str,
    target_source: str,
    lookup_type: str,
    lookup_value: str,
    reason: str,
) -> bool:
    now = utc_now()
    queue_id = "source-backfill:" + str(
        uuid.uuid5(
            uuid.UUID("25dc788f-2d99-41ad-99c5-da135951ba85"),
            f"{canonical_paper_id}:{target_source}:{lookup_type}:{lookup_value.lower()}",
        )
    )
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO source_backfill_queue
        (id, canonical_paper_id, target_source, lookup_type, lookup_value,
         reason, status, attempts, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            queue_id,
            canonical_paper_id,
            target_source,
            lookup_type,
            lookup_value,
            reason,
            now,
            now,
        ),
    )
    return bool(cursor.rowcount)


def run_source_coverage_audit(
    connection: sqlite3.Connection,
    *,
    scope: str = "eligible",
    window_from: str | None = None,
    window_to: str | None = None,
    queue_missing: bool = False,
    persist: bool = True,
    seed_historical: bool = True,
) -> dict[str, Any]:
    """Measure source overlap and optionally queue identifier-safe backfills.

    The result is a lower-bound audit.  No public metadata provider represents
    the entire scholarly web, so the report never labels a corpus as complete.
    """
    if persist or queue_missing or seed_historical:
        ensure_source_coverage_schema(connection)
    historical_source_seed = (
        seed_canonical_source_versions(connection)
        if seed_historical
        else {"skipped": True, "reason": "read_only_audit"}
    )
    where, params = _paper_where(scope, window_from, window_to)
    papers = connection.execute(
        f"""
        SELECT p.id, p.doi, p.arxiv_id, p.openalex_id, p.title,
               p.publication_date, p.source
        FROM papers p
        WHERE {where}
        """,
        params,
    ).fetchall()
    sources_by_paper = _source_sets(connection, papers, where, params)
    source_papers: dict[str, set[str]] = {}
    cross_source = single_source = stable_identity = weak_identity = 0
    gaps = {"crossref": 0, "openalex": 0, "arxiv": 0}
    queued = 0
    for paper in papers:
        paper_id = str(paper["id"])
        sources = sources_by_paper.get(paper_id, set())
        for source in sources:
            source_papers.setdefault(source, set()).add(paper_id)
        known = {source for source in sources if source in CROSSCHECK_SOURCES}
        if len(known) >= 2:
            cross_source += 1
        elif len(known) == 1:
            single_source += 1
        doi = normalize_doi(paper["doi"])
        arxiv_id, _ = parse_arxiv_id(paper["arxiv_id"])
        openalex_id = str(paper["openalex_id"] or "").strip()
        if doi or arxiv_id or openalex_id:
            stable_identity += 1
        else:
            weak_identity += 1
        targets: list[tuple[str, str, str, str]] = []
        if doi and "crossref" not in known:
            targets.append(("crossref", "doi", doi, "DOI record has no Crossref observation"))
        if doi and "openalex" not in known:
            targets.append(("openalex", "doi", doi, "DOI record has no OpenAlex observation"))
        if arxiv_id and "arxiv" not in known:
            targets.append(("arxiv", "arxiv", arxiv_id, "arXiv identity has no arXiv observation"))
        for target_source, lookup_type, lookup_value, reason in targets:
            gaps[target_source] += 1
            if queue_missing and _queue_gap(
                connection,
                canonical_paper_id=paper_id,
                target_source=target_source,
                lookup_type=lookup_type,
                lookup_value=lookup_value,
                reason=reason,
            ):
                queued += 1

    canonical_count = len(papers)
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(CROSSCHECK_SOURCES):
        for right in CROSSCHECK_SOURCES[index + 1 :]:
            left_ids = source_papers.get(left, set())
            right_ids = source_papers.get(right, set())
            intersection = len(left_ids & right_ids)
            union = len(left_ids | right_ids)
            pairs.append(
                {
                    "left": left,
                    "right": right,
                    "intersection": intersection,
                    "union": union,
                    "jaccard": round(intersection / union, 6) if union else 0.0,
                }
            )
    queue_table_exists = bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_backfill_queue'"
        ).fetchone()
    )
    queue_status = {
        str(row["status"]): int(row["n"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM source_backfill_queue GROUP BY status"
        ).fetchall()
    } if queue_table_exists else {}
    report: dict[str, Any] = {
        "scope": scope,
        "window_from": window_from,
        "window_to": window_to,
        "canonical_count": canonical_count,
        "cross_source_count": cross_source,
        "single_source_count": single_source,
        "cross_source_rate": round(cross_source / canonical_count, 6) if canonical_count else 0.0,
        "stable_identity_count": stable_identity,
        "stable_identity_rate": round(stable_identity / canonical_count, 6) if canonical_count else 0.0,
        "weak_identity_count": weak_identity,
        "source_counts": {key: len(value) for key, value in sorted(source_papers.items())},
        "source_pair_overlap": pairs,
        "missing_source_gaps": gaps,
        "newly_queued": queued,
        "queue_status": queue_status,
        "historical_source_seed": historical_source_seed,
        "interpretation": {
            "cross_source_rate": "share of canonical papers independently observed by at least two tracked providers",
            "stable_identity_rate": "share carrying DOI, base arXiv ID, or OpenAlex ID",
            "completeness_claim": "lower_bound_only",
        },
        "limitations": [
            "OpenAlex, Crossref and arXiv each have scope and indexing delays; their union cannot prove all-web completeness.",
            "A missing source observation can mean provider scope exclusion, not necessarily a missing paper.",
            "Title-only identities are not auto-merged unless title, first author and year satisfy the strict matcher.",
        ],
    }
    if persist:
        audit_id = str(uuid.uuid4())
        created_at = utc_now()
        connection.execute(
            """
            INSERT INTO source_coverage_audits
            (id, created_at, scope, window_from, window_to, canonical_count,
             cross_source_count, single_source_count, stable_identity_count,
             queued_count, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                created_at,
                scope,
                window_from,
                window_to,
                canonical_count,
                cross_source,
                single_source,
                stable_identity,
                queued,
                json.dumps(report, ensure_ascii=False, sort_keys=True),
            ),
        )
        report["audit_id"] = audit_id
        report["created_at"] = created_at
    return report


def _source_record_id(source: str, record: dict[str, Any]) -> str:
    if source == "openalex" and record.get("openalex_id"):
        return str(record["openalex_id"]).strip().lower()
    if source == "arxiv" and record.get("arxiv_id"):
        arxiv_id, parsed_version = parse_arxiv_id(record.get("arxiv_id"))
        version = record.get("arxiv_version") or parsed_version
        return f"{arxiv_id}v{version}" if arxiv_id and version else str(arxiv_id or record["arxiv_id"]).lower()
    doi = normalize_doi(record.get("doi"))
    if doi:
        return doi
    return str(record.get("id") or "").strip().lower()


def _validate_lookup(item: sqlite3.Row, record: dict[str, Any]) -> None:
    lookup_type = str(item["lookup_type"])
    expected = str(item["lookup_value"] or "").strip().lower()
    if lookup_type == "doi":
        actual = normalize_doi(record.get("doi")) or ""
        if actual != expected:
            raise ValueError(f"source returned mismatched DOI: {actual or 'missing'}")
    elif lookup_type == "arxiv":
        actual, _ = parse_arxiv_id(record.get("arxiv_id"))
        if str(actual or "").lower() != expected:
            raise ValueError(f"source returned mismatched arXiv ID: {actual or 'missing'}")


def run_source_backfill(
    connection: sqlite3.Connection,
    *,
    limit: int = 25,
    timeout: int = 15,
    progress_callback: Callable[[int, int, int, int, int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Resolve queued exact-source lookups with item-level durable checkpoints.

    The existing transaction behavior is intentionally preserved: the
    ``running`` marker is committed before each remote request and every final
    item outcome is committed before progress is published.  The optional
    callback is best-effort and cannot turn a successful lookup into a failed
    queue item.
    """
    ensure_source_coverage_schema(connection)
    limit = max(0, min(int(limit), 500))
    if limit == 0:
        progress_callback_errors: list[str] = []
        if progress_callback is not None:
            try:
                progress_callback(0, 0, 0, 0, 0, 0, "")
            except Exception as exc:
                progress_callback_errors.append(
                    f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"
                )
        return {
            "requested": 0,
            "total": 0,
            "processed": 0,
            "completed": 0,
            "not_found": 0,
            "failed": 0,
            "identity_conflicts": 0,
            "paper_version_ids": [],
            "canonical_paper_ids": [],
            "progress_callback_errors": progress_callback_errors,
        }
    items = connection.execute(
        """
        SELECT q.*, p.condmat_view_eligible
        FROM source_backfill_queue q
        JOIN papers p ON p.id=q.canonical_paper_id
        WHERE q.status IN ('queued','retryable_failed')
        ORDER BY q.attempts ASC, q.created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    clients: dict[str, Any] = {}
    completed = not_found = failed = identity_conflicts = 0
    processed = 0
    total = len(items)
    version_ids: list[str] = []
    changed_canonical_ids: list[str] = []
    results: list[dict[str, Any]] = []
    progress_callback_errors: list[str] = []
    repository = LibraryRepository(connection)

    def publish_progress(current_source: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                processed,
                total,
                completed,
                not_found,
                failed,
                identity_conflicts,
                current_source,
            )
        except Exception as exc:
            if len(progress_callback_errors) < 10:
                progress_callback_errors.append(
                    f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"
                )

    publish_progress("")
    for item in items:
        now = utc_now()
        connection.execute(
            "UPDATE source_backfill_queue SET status='running', attempts=attempts+1, last_attempt_at=?, updated_at=? WHERE id=?",
            (now, now, item["id"]),
        )
        connection.commit()
        source = str(item["target_source"])
        publish_progress(source)
        try:
            if source == "crossref":
                client = clients.setdefault(source, CrossrefClient(timeout=timeout, polite_delay=0.05))
                record = client.get_work(str(item["lookup_value"]))
            elif source == "openalex":
                client = clients.setdefault(source, OpenAlexClient(timeout=timeout, polite_delay=0.05))
                record = client.get_work_by_doi(str(item["lookup_value"]))
            elif source == "arxiv":
                client = clients.setdefault(source, ArxivClient(timeout=timeout, polite_delay=0.05))
                record = client.fetch_by_id(str(item["lookup_value"]))
            else:
                raise ValueError(f"unsupported backfill source: {source}")
            if not record:
                not_found += 1
                connection.execute(
                    "UPDATE source_backfill_queue SET status='not_found', last_error='provider returned no exact record', updated_at=? WHERE id=?",
                    (utc_now(), item["id"]),
                )
                results.append({"id": item["id"], "source": source, "status": "not_found"})
                connection.commit()
                processed += 1
                publish_progress(source)
                continue
            _validate_lookup(item, record)
            record["condmat_view_eligible"] = bool(item["condmat_view_eligible"])
            result = repository.upsert_version(
                record,
                source=source,
                source_record_id=_source_record_id(source, record),
                default_condmat_eligible=bool(item["condmat_view_eligible"]),
                create_corpus_review=False,
            )
            if result.canonical_paper_id != str(item["canonical_paper_id"]):
                identity_conflicts += 1
                connection.execute(
                    "UPDATE source_backfill_queue SET status='identity_conflict', last_error=?, updated_at=? WHERE id=?",
                    (f"resolved to {result.canonical_paper_id}", utc_now(), item["id"]),
                )
                results.append({"id": item["id"], "source": source, "status": "identity_conflict"})
                connection.commit()
                processed += 1
                publish_progress(source)
                continue
            record["id"] = result.canonical_paper_id
            upsert_paper(connection, record)
            completed += 1
            version_ids.append(result.paper_version_id)
            if result.action in {"attached_version", "created_canonical"}:
                changed_canonical_ids.append(result.canonical_paper_id)
            finished = utc_now()
            connection.execute(
                "UPDATE source_backfill_queue SET status='completed', last_error=NULL, completed_at=?, updated_at=? WHERE id=?",
                (finished, finished, item["id"]),
            )
            results.append({"id": item["id"], "source": source, "status": "completed", "paper_version_id": result.paper_version_id, "canonical_paper_id": result.canonical_paper_id, "metadata_changed": result.action in {"attached_version", "created_canonical"}})
            connection.commit()
            processed += 1
            publish_progress(source)
        except Exception as exc:
            failed += 1
            message = f"{type(exc).__name__}: {' '.join(str(exc).split())[:400]}"
            connection.execute(
                "UPDATE source_backfill_queue SET status='retryable_failed', last_error=?, updated_at=? WHERE id=?",
                (message, utc_now(), item["id"]),
            )
            connection.commit()
            results.append({"id": item["id"], "source": source, "status": "retryable_failed", "error": message})
            processed += 1
            publish_progress(source)
    return {
        "requested": total,
        "total": total,
        "processed": processed,
        "completed": completed,
        "not_found": not_found,
        "failed": failed,
        "identity_conflicts": identity_conflicts,
        "paper_version_ids": list(dict.fromkeys(version_ids)),
        "canonical_paper_ids": list(dict.fromkeys(changed_canonical_ids)),
        "results": results,
        "progress_callback_errors": progress_callback_errors,
    }


def latest_source_coverage_audit(connection: sqlite3.Connection) -> dict[str, Any] | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_coverage_audits'"
    ).fetchone()
    if not exists:
        return None
    row = connection.execute(
        "SELECT * FROM source_coverage_audits ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["report"] = json.loads(item.pop("report_json") or "{}")
    except json.JSONDecodeError:
        item["report"] = {}
    return item