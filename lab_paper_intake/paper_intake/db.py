from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .config import DB_PATH
from .dedup import merge_papers
from .models import (
    ChineseSummary,
    Paper,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
)


SCHEMA_VERSION = 3
SQLITE_TIMEOUT_SECONDS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a write connection with safe concurrency and transaction semantics."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.OperationalError:
        # Another process can briefly hold the mode-change lock. The configured
        # busy timeout still protects the current connection.
        pass
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create or migrate the Intake database to the latest schema."""
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        migrations = (
            (1, "base_schema", _migration_base_schema),
            (2, "canonical_identity_and_provenance", _migration_identity_schema),
            (3, "download_foreign_key", _migration_download_foreign_key),
        )
        for version, name, migration in migrations:
            if current >= version:
                continue
            migration(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, utc_now()),
            )
            conn.execute(f"PRAGMA user_version = {version}")
            current = version


def _migration_base_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
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
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(conn, "papers", "local_pdf_path", "TEXT")
    _ensure_column(conn, "papers", "created_at", "TEXT")
    _ensure_column(conn, "papers", "last_seen_at", "TEXT")
    now = utc_now()
    conn.execute(
        "UPDATE papers SET created_at = COALESCE(NULLIF(created_at, ''), updated_at, ?) "
        "WHERE created_at IS NULL OR created_at = ''",
        (now,),
    )
    conn.execute(
        "UPDATE papers SET last_seen_at = COALESCE(NULLIF(last_seen_at, ''), updated_at, ?) "
        "WHERE last_seen_at IS NULL OR last_seen_at = ''",
        (now,),
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi)",
        "CREATE INDEX IF NOT EXISTS idx_papers_arxiv_id ON papers(arxiv_id)",
        "CREATE INDEX IF NOT EXISTS idx_papers_normalized_title ON papers(normalized_title)",
        "CREATE INDEX IF NOT EXISTS idx_papers_pdf_status ON papers(pdf_status)",
        "CREATE INDEX IF NOT EXISTS idx_papers_selected ON papers(selected)",
        "CREATE INDEX IF NOT EXISTS idx_papers_updated_at ON papers(updated_at)",
    ):
        conn.execute(statement)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key TEXT PRIMARY KEY,
            model_name TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloaded_pdfs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id TEXT NOT NULL,
            pdf_url TEXT NOT NULL,
            local_path TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _migration_identity_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_identities (
            namespace TEXT NOT NULL,
            identity TEXT NOT NULL,
            paper_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(namespace, identity),
            FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_identities_paper_id "
        "ON paper_identities(paper_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_aliases (
            alias_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_aliases_paper_id ON paper_aliases(paper_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id TEXT NOT NULL,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(paper_id, payload_sha256),
            FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_observations_paper_id "
        "ON paper_observations(paper_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_identity_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL,
            identity TEXT NOT NULL,
            existing_paper_id TEXT,
            incoming_paper_id TEXT,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS database_anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            record_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _reconcile_legacy_papers(conn)


def _migration_download_foreign_key(conn: sqlite3.Connection) -> None:
    foreign_keys = conn.execute("PRAGMA foreign_key_list(downloaded_pdfs)").fetchall()
    has_paper_fk = any(
        row[2] == "papers" and row[3] == "paper_id" and row[4] == "id"
        for row in foreign_keys
    )
    if not has_paper_fk:
        orphan_rows = conn.execute(
            """
            SELECT d.* FROM downloaded_pdfs d
            LEFT JOIN papers p ON p.id = d.paper_id
            WHERE p.id IS NULL
            """
        ).fetchall()
        for row in orphan_rows:
            _record_anomaly(conn, "orphan_download", str(row["id"]), dict(row))
        conn.execute("ALTER TABLE downloaded_pdfs RENAME TO downloaded_pdfs_legacy")
        conn.execute(
            """
            CREATE TABLE downloaded_pdfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT NOT NULL,
                pdf_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO downloaded_pdfs(id, paper_id, pdf_url, local_path, status, created_at)
            SELECT d.id, d.paper_id, d.pdf_url, d.local_path, d.status, d.created_at
            FROM downloaded_pdfs_legacy d
            JOIN papers p ON p.id = d.paper_id
            """
        )
        conn.execute("DROP TABLE downloaded_pdfs_legacy")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloaded_pdfs_paper_id "
        "ON downloaded_pdfs(paper_id)"
    )
    _backfill_downloaded_pdf_status(conn)


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _reconcile_legacy_papers(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM papers ORDER BY updated_at, id").fetchall()
    if not rows:
        return
    by_id = {str(row["id"]): row for row in rows}
    parent = {paper_id: paper_id for paper_id in by_id}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: str, second: str) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[max(left, right)] = min(left, right)

    doi_groups: dict[str, list[str]] = {}
    arxiv_groups: dict[str, list[str]] = {}
    title_groups: dict[str, list[str]] = {}
    for row in rows:
        paper_id = str(row["id"])
        doi = normalize_doi(row["doi"])
        arxiv_id = normalize_arxiv_id(row["arxiv_id"])
        title = normalize_title(row["title"])
        if doi:
            doi_groups.setdefault(doi, []).append(paper_id)
        if arxiv_id:
            arxiv_groups.setdefault(arxiv_id, []).append(paper_id)
        if title:
            title_groups.setdefault(title, []).append(paper_id)
    for groups in (doi_groups, arxiv_groups):
        for values in groups.values():
            for paper_id in values[1:]:
                union(values[0], paper_id)

    safe_titles: set[str] = set()
    for title, values in title_groups.items():
        dois = {normalize_doi(by_id[item]["doi"]) for item in values}
        arxiv_ids = {normalize_arxiv_id(by_id[item]["arxiv_id"]) for item in values}
        dois.discard(None)
        arxiv_ids.discard(None)
        if len(dois) <= 1 and len(arxiv_ids) <= 1:
            safe_titles.add(title)
            for paper_id in values[1:]:
                union(values[0], paper_id)
        elif len(values) > 1:
            _record_identity_conflict(
                conn,
                "title",
                title,
                values[0],
                values[-1],
                "legacy_title_has_conflicting_external_ids",
                {"paper_ids": values, "dois": sorted(dois), "arxiv_ids": sorted(arxiv_ids)},
            )

    components: dict[str, list[sqlite3.Row]] = {}
    for paper_id, row in by_id.items():
        components.setdefault(find(paper_id), []).append(row)
    for component in components.values():
        _merge_legacy_component(conn, component)

    conn.execute("DELETE FROM paper_identities")
    for row in conn.execute("SELECT * FROM papers ORDER BY id").fetchall():
        _refresh_identities(conn, paper_from_row(row), safe_titles=safe_titles)


def _merge_legacy_component(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> None:
    ranked = sorted(rows, key=_row_quality, reverse=True)
    canonical_row = ranked[0]
    canonical_id = str(canonical_row["id"])
    merged = paper_from_row(canonical_row)
    created_at = min(str(row["created_at"] or row["updated_at"]) for row in rows)
    last_seen_at = max(str(row["last_seen_at"] or row["updated_at"]) for row in rows)
    for row in rows:
        _record_observation(conn, canonical_id, row["source"] or "legacy", dict(row))
        if row["id"] != canonical_id:
            merged = _merge_paper_records(merged, paper_from_row(row), canonical_id)
    _write_paper_row(conn, merged, created_at=created_at, last_seen_at=last_seen_at)
    for row in rows:
        alias_id = str(row["id"])
        if alias_id == canonical_id:
            continue
        _move_related_records(conn, alias_id, canonical_id)
        _store_alias(conn, alias_id, canonical_id, "migration_identity_merge")
        conn.execute("DELETE FROM papers WHERE id = ?", (alias_id,))


def _row_quality(row: Mapping[str, Any]) -> tuple[int, int, int, int, str, str]:
    values = [
        row["title"], row["authors_json"], row["year"], row["journal"], row["doi"],
        row["arxiv_id"], row["abstract"], row["url"], row["pdf_url"],
        row["local_pdf_path"], row["citation_count"], row["relevance_score"],
    ]
    return (
        1 if row["pdf_status"] == "downloaded" and row["local_pdf_path"] else 0,
        int(bool(row["doi"])) + int(bool(row["arxiv_id"])),
        sum(1 for value in values if value),
        len(row["abstract"] or ""),
        str(row["updated_at"] or ""),
        str(row["id"]),
    )


def _move_related_records(
    conn: sqlite3.Connection,
    alias_id: str,
    canonical_id: str,
) -> None:
    conn.execute(
        "UPDATE downloaded_pdfs SET paper_id = ? WHERE paper_id = ?",
        (canonical_id, alias_id),
    )
    conn.execute("DELETE FROM paper_identities WHERE paper_id = ?", (alias_id,))
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_observations
        (paper_id, source, observed_at, payload_sha256, payload_json)
        SELECT ?, source, observed_at, payload_sha256, payload_json
        FROM paper_observations WHERE paper_id = ?
        """,
        (canonical_id, alias_id),
    )
    conn.execute("DELETE FROM paper_observations WHERE paper_id = ?", (alias_id,))
    conn.execute(
        "UPDATE paper_aliases SET paper_id = ? WHERE paper_id = ?",
        (canonical_id, alias_id),
    )


def _record_observation(
    conn: sqlite3.Connection,
    paper_id: str,
    source: str,
    payload: Mapping[str, Any],
) -> None:
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_observations
        (paper_id, source, observed_at, payload_sha256, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (paper_id, source or "unknown", utc_now(), payload_digest, payload_json),
    )


def _record_identity_conflict(
    conn: sqlite3.Connection,
    namespace: str,
    identity: str,
    existing_paper_id: str | None,
    incoming_paper_id: str | None,
    reason: str,
    payload: Mapping[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO paper_identity_conflicts
        (namespace, identity, existing_paper_id, incoming_paper_id, reason, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            namespace,
            identity,
            existing_paper_id,
            incoming_paper_id,
            reason,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            utc_now(),
        ),
    )


def _record_anomaly(
    conn: sqlite3.Connection,
    category: str,
    record_key: str,
    payload: Mapping[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO database_anomalies(category, record_key, payload_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            category,
            record_key,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            utc_now(),
        ),
    )


def _paper_identity_keys(paper: Paper) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    doi = normalize_doi(paper.doi)
    arxiv_id = normalize_arxiv_id(paper.arxiv_id)
    title = paper.normalized_title or normalize_title(paper.title)
    if doi:
        keys.append(("doi", doi))
    if arxiv_id:
        keys.append(("arxiv", arxiv_id))
    if title:
        keys.append(("title", title))
    return keys


def _refresh_identities(
    conn: sqlite3.Connection,
    paper: Paper,
    safe_titles: set[str] | None = None,
) -> None:
    conn.execute("DELETE FROM paper_identities WHERE paper_id = ?", (paper.id,))
    for namespace, identity in _paper_identity_keys(paper):
        if namespace == "title" and safe_titles is not None and identity not in safe_titles:
            continue
        existing = conn.execute(
            "SELECT paper_id FROM paper_identities WHERE namespace = ? AND identity = ?",
            (namespace, identity),
        ).fetchone()
        if existing and existing["paper_id"] != paper.id:
            _record_identity_conflict(
                conn,
                namespace,
                identity,
                existing["paper_id"],
                paper.id,
                "identity_already_assigned",
                paper.model_dump(mode="json"),
            )
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_identities(namespace, identity, paper_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (namespace, identity, paper.id, utc_now()),
        )


def _store_alias(
    conn: sqlite3.Connection,
    alias_id: str,
    paper_id: str,
    reason: str,
) -> None:
    if alias_id == paper_id:
        return
    conn.execute(
        """
        INSERT INTO paper_aliases(alias_id, paper_id, reason, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(alias_id) DO UPDATE SET
            paper_id = excluded.paper_id,
            reason = excluded.reason
        """,
        (alias_id, paper_id, reason, utc_now()),
    )


def _resolve_alias(conn: sqlite3.Connection, paper_id: str) -> str | None:
    if conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
        return paper_id
    row = conn.execute(
        "SELECT paper_id FROM paper_aliases WHERE alias_id = ?", (paper_id,)
    ).fetchone()
    return str(row["paper_id"]) if row else None


def _candidate_paper_ids(conn: sqlite3.Connection, paper: Paper) -> list[str]:
    candidates: list[str] = []
    resolved_id = _resolve_alias(conn, paper.id)
    if resolved_id:
        candidates.append(resolved_id)
    for namespace, identity in _paper_identity_keys(paper):
        row = conn.execute(
            "SELECT paper_id FROM paper_identities WHERE namespace = ? AND identity = ?",
            (namespace, identity),
        ).fetchone()
        if not row:
            continue
        candidate_id = str(row["paper_id"])
        if namespace == "title":
            candidate_row = conn.execute(
                "SELECT * FROM papers WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate_row and _external_identity_conflict(
                paper_from_row(candidate_row), paper
            ):
                _record_identity_conflict(
                    conn,
                    namespace,
                    identity,
                    candidate_id,
                    paper.id,
                    "title_match_has_conflicting_external_ids",
                    paper.model_dump(mode="json"),
                )
                continue
        if candidate_id not in candidates:
            candidates.append(candidate_id)
    return candidates


def _external_identity_conflict(first: Paper, second: Paper) -> bool:
    first_doi, second_doi = normalize_doi(first.doi), normalize_doi(second.doi)
    first_arxiv = normalize_arxiv_id(first.arxiv_id)
    second_arxiv = normalize_arxiv_id(second.arxiv_id)
    return bool(
        (first_doi and second_doi and first_doi != second_doi)
        or (first_arxiv and second_arxiv and first_arxiv != second_arxiv)
    )


def _merge_paper_records(primary: Paper, secondary: Paper, canonical_id: str) -> Paper:
    merged = merge_papers(primary, secondary)
    data = merged.model_dump(mode="python")
    data["id"] = canonical_id
    if not merged.chinese_summary.has_content():
        alternate = (
            secondary.chinese_summary
            if secondary.chinese_summary.has_content()
            else primary.chinese_summary
        )
        data["chinese_summary"] = alternate
    scores = [value for value in (primary.relevance_score, secondary.relevance_score) if value is not None]
    if scores:
        data["relevance_score"] = max(scores)
    data["selected"] = primary.selected or secondary.selected
    return Paper(**data)


def _merge_existing_candidates(
    conn: sqlite3.Connection,
    candidate_ids: list[str],
) -> str:
    rows = [
        conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        for paper_id in candidate_ids
    ]
    rows = [row for row in rows if row is not None]
    canonical_row = max(rows, key=_row_quality)
    canonical_id = str(canonical_row["id"])
    merged = paper_from_row(canonical_row)
    created_at = str(canonical_row["created_at"])
    for row in rows:
        if row["id"] == canonical_id:
            continue
        incoming = paper_from_row(row)
        if _external_identity_conflict(merged, incoming):
            _record_identity_conflict(
                conn,
                "bridge",
                incoming.id,
                canonical_id,
                incoming.id,
                "incoming_observation_links_conflicting_records",
                incoming.model_dump(mode="json"),
            )
            continue
        merged = _merge_paper_records(merged, incoming, canonical_id)
        _move_related_records(conn, incoming.id, canonical_id)
        _store_alias(conn, incoming.id, canonical_id, "runtime_identity_bridge")
        conn.execute("DELETE FROM papers WHERE id = ?", (incoming.id,))
        created_at = min(created_at, str(row["created_at"]))
    _write_paper_row(conn, merged, created_at=created_at, last_seen_at=utc_now())
    _refresh_identities(conn, merged)
    return canonical_id


def _write_paper_row(
    conn: sqlite3.Connection,
    paper: Paper,
    *,
    created_at: str | None = None,
    last_seen_at: str | None = None,
) -> None:
    now = utc_now()
    created = created_at or now
    last_seen = last_seen_at or now
    conn.execute(
        """
        INSERT INTO papers (
            id, title, normalized_title, authors_json, year, journal, doi,
            arxiv_id, abstract, url, pdf_url, local_pdf_path, pdf_status,
            source, citation_count, relevance_score, relevance_reason,
            tags_json, chinese_summary_json, selected, raw_json,
            created_at, last_seen_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            normalized_title = excluded.normalized_title,
            authors_json = excluded.authors_json,
            year = excluded.year,
            journal = excluded.journal,
            doi = excluded.doi,
            arxiv_id = excluded.arxiv_id,
            abstract = excluded.abstract,
            url = excluded.url,
            pdf_url = excluded.pdf_url,
            local_pdf_path = excluded.local_pdf_path,
            pdf_status = excluded.pdf_status,
            source = excluded.source,
            citation_count = excluded.citation_count,
            relevance_score = excluded.relevance_score,
            relevance_reason = excluded.relevance_reason,
            tags_json = excluded.tags_json,
            chinese_summary_json = excluded.chinese_summary_json,
            selected = excluded.selected,
            raw_json = excluded.raw_json,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (
            paper.id,
            paper.title,
            paper.normalized_title or normalize_title(paper.title),
            json.dumps(paper.authors, ensure_ascii=False),
            paper.year,
            paper.journal,
            normalize_doi(paper.doi),
            normalize_arxiv_id(paper.arxiv_id),
            paper.abstract,
            paper.url,
            paper.pdf_url,
            paper.local_pdf_path,
            paper.pdf_status,
            paper.source,
            paper.citation_count,
            paper.relevance_score,
            paper.relevance_reason,
            json.dumps(paper.tags, ensure_ascii=False),
            paper.chinese_summary.model_dump_json(),
            int(paper.selected),
            json.dumps(paper.raw, ensure_ascii=False),
            created,
            last_seen,
            now,
        ),
    )


def _backfill_downloaded_pdf_status(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE papers
        SET pdf_status = 'downloaded',
            local_pdf_path = (
                SELECT d.local_path FROM downloaded_pdfs d
                WHERE d.paper_id = papers.id AND d.status = 'downloaded'
                ORDER BY d.id DESC LIMIT 1
            ),
            updated_at = ?
        WHERE EXISTS (
            SELECT 1 FROM downloaded_pdfs d
            WHERE d.paper_id = papers.id AND d.status = 'downloaded'
        ) AND (
            pdf_status <> 'downloaded' OR local_pdf_path IS NULL OR local_pdf_path = ''
        )
        """,
        (utc_now(),),
    )


def prompt_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_cache_key(kind: str, model_name: str, payload: dict[str, Any]) -> str:
    return f"{kind}:{model_name}:{prompt_hash(payload)}"


def get_llm_cache(cache_key: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT response_json FROM llm_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    return json.loads(row["response_json"]) if row else None


def set_llm_cache(
    cache_key: str,
    model_name: str,
    payload_hash: str,
    response_json: dict[str, Any],
    db_path: Path = DB_PATH,
) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_cache
            (cache_key, model_name, prompt_hash, response_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                model_name = excluded.model_name,
                prompt_hash = excluded.prompt_hash,
                response_json = excluded.response_json,
                created_at = excluded.created_at
            """,
            (
                cache_key,
                model_name,
                payload_hash,
                json.dumps(response_json, ensure_ascii=False),
                utc_now(),
            ),
        )


def upsert_papers(papers: list[Paper], db_path: Path = DB_PATH) -> None:
    """Merge observations into stable canonical papers across search runs."""
    init_db(db_path)
    with connect(db_path) as conn:
        for incoming in papers:
            candidates = _candidate_paper_ids(conn, incoming)
            if len(candidates) > 1:
                canonical_id = _merge_existing_candidates(conn, candidates)
            elif candidates:
                canonical_id = candidates[0]
            else:
                canonical_id = incoming.id

            row = conn.execute(
                "SELECT * FROM papers WHERE id = ?", (canonical_id,)
            ).fetchone()
            if row:
                stored = paper_from_row(row)
                merged = _merge_paper_records(stored, incoming, canonical_id)
                created_at = str(row["created_at"])
                _write_paper_row(
                    conn, merged, created_at=created_at, last_seen_at=utc_now()
                )
                if incoming.id != canonical_id:
                    _store_alias(conn, incoming.id, canonical_id, "observation_identity_merge")
            else:
                merged = incoming.model_copy(update={"id": canonical_id})
                _write_paper_row(conn, merged)
            _record_observation(
                conn,
                canonical_id,
                incoming.source or "runtime",
                incoming.model_dump(mode="json"),
            )
            _refresh_identities(conn, merged)


def record_search_run(
    prompt: str,
    plan_json: dict[str, Any],
    result_count: int,
    db_path: Path = DB_PATH,
) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO search_runs (prompt, plan_json, result_count, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (prompt, json.dumps(plan_json, ensure_ascii=False), result_count, utc_now()),
        )


def record_pdf_download(
    paper_id: str,
    pdf_url: str,
    local_path: Path | str,
    status: str,
    db_path: Path = DB_PATH,
) -> None:
    init_db(db_path)
    local_path_text = str(local_path)
    with connect(db_path) as conn:
        canonical_id = _resolve_alias(conn, paper_id)
        if not canonical_id:
            raise ValueError(f"Unknown paper id: {paper_id}")
        conn.execute(
            """
            INSERT INTO downloaded_pdfs
            (paper_id, pdf_url, local_path, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (canonical_id, pdf_url, local_path_text, status, utc_now()),
        )
        if status == "downloaded":
            conn.execute(
                """
                UPDATE papers
                SET pdf_status = 'downloaded', local_pdf_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (local_path_text, utc_now(), canonical_id),
            )


def sync_paper_pdf_status(papers: list[Paper], db_path: Path = DB_PATH) -> list[Paper]:
    init_db(db_path)
    if not papers:
        return []
    with connect(db_path) as conn:
        synced: list[Paper] = []
        for paper in papers:
            canonical_id = _resolve_alias(conn, paper.id)
            if not canonical_id:
                synced.append(paper)
                continue
            row = conn.execute(
                """
                SELECT local_path FROM downloaded_pdfs
                WHERE paper_id = ? AND status = 'downloaded'
                ORDER BY id DESC LIMIT 1
                """,
                (canonical_id,),
            ).fetchone()
            if row:
                synced.append(
                    paper.model_copy(
                        update={
                            "id": canonical_id,
                            "pdf_status": "downloaded",
                            "local_pdf_path": row["local_path"],
                        }
                    )
                )
            else:
                synced.append(paper.model_copy(update={"id": canonical_id}))
    return synced


def paper_from_row(row: Mapping[str, Any]) -> Paper:
    keys = row.keys()
    return Paper(
        id=row["id"],
        title=row["title"],
        normalized_title=row["normalized_title"],
        authors=json.loads(row["authors_json"]),
        year=row["year"],
        journal=row["journal"],
        doi=row["doi"],
        arxiv_id=row["arxiv_id"],
        abstract=row["abstract"],
        url=row["url"],
        pdf_url=row["pdf_url"],
        local_pdf_path=row["local_pdf_path"] if "local_pdf_path" in keys else None,
        pdf_status=row["pdf_status"],
        source=row["source"] or "",
        citation_count=row["citation_count"],
        relevance_score=row["relevance_score"],
        relevance_reason=row["relevance_reason"],
        tags=json.loads(row["tags_json"]),
        chinese_summary=ChineseSummary.model_validate_json(row["chinese_summary_json"]),
        selected=bool(row["selected"]),
        raw=json.loads(row["raw_json"]),
    )


def count_papers(db_path: Path = DB_PATH) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
    return int(row[0]) if row else 0


def fetch_papers(db_path: Path = DB_PATH) -> list[Paper]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM papers
            ORDER BY relevance_score IS NULL, relevance_score DESC, year DESC
            """
        ).fetchall()
    return [paper_from_row(row) for row in rows]
