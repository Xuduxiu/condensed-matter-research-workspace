from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.config import (
    PROJECT_ROOT,
    cache_dir,
    data_dir,
    db_path,
    ensure_data_layout,
    export_dir,
    logs_dir,
    processed_dir,
    raw_dir,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_SCHEMA_INIT_LOCK = threading.Lock()
_INITIALIZED_SCHEMA_VERSIONS: dict[str, int] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect(path: Path | None = None) -> Iterable[sqlite3.Connection]:
    ensure_data_layout()
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    database_row = conn.execute("PRAGMA database_list").fetchone()
    database_name = str(database_row[2]) if database_row and database_row[2] else f"memory:{id(conn)}"
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    if _INITIALIZED_SCHEMA_VERSIONS.get(database_name) == schema_version:
        return

    # All routes used to run idempotent migration writes concurrently. They
    # are safe but can take SQLite's database-wide write lock, making a fresh
    # dashboard load intermittently fail while the live scanner is active.
    # Serialize and cache the bootstrap work per database instead.
    with _SCHEMA_INIT_LOCK:
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        if _INITIALIZED_SCHEMA_VERSIONS.get(database_name) == schema_version:
            return
        migrate_term_month_stats(conn)
        migrate_monthly_corpus_stats(conn)
        migrate_legacy_papers(conn)
        migrate_legacy_paper_terms(conn)
        migrate_legacy_lifecycle(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        migrate_db(conn)
        conn.commit()
        _INITIALIZED_SCHEMA_VERSIONS[database_name] = int(conn.execute("PRAGMA schema_version").fetchone()[0])

def migrate_legacy_papers(conn: sqlite3.Connection) -> None:
    if not table_columns(conn, "papers"):
        return
    add_missing_columns(conn, "papers", paper_columns())
    conn.execute("UPDATE papers SET data_mode = CASE WHEN source = 'mock' THEN 'mock' ELSE COALESCE(data_mode, 'real') END WHERE data_mode IS NULL OR data_mode = ''")
def migrate_db(conn: sqlite3.Connection) -> None:
    migrate_monthly_corpus_stats(conn)
    migrate_term_month_stats(conn)
    add_missing_columns(conn, "papers", paper_columns())
    add_missing_columns(
        conn,
        "paper_terms",
        {
            "display_eligible": "INTEGER NOT NULL DEFAULT 1",
            "display_reason": "TEXT DEFAULT 'legacy'",
            "source": "TEXT DEFAULT 'extractor'",
        },
    )
    add_missing_columns(conn, "concept_lifecycle", lifecycle_columns())
    add_missing_columns(conn, "update_runs", update_run_columns())
    add_missing_columns(conn, "ingest_runs", ingest_run_columns())
    add_missing_columns(conn, "ingest_checkpoints", ingest_checkpoint_columns())
    conn.execute("UPDATE papers SET data_mode = CASE WHEN source = 'mock' THEN 'mock' ELSE COALESCE(data_mode, 'real') END WHERE data_mode IS NULL OR data_mode = ''")


def migrate_legacy_paper_terms(conn: sqlite3.Connection) -> None:
    if not table_columns(conn, "paper_terms"):
        return
    add_missing_columns(
        conn,
        "paper_terms",
        {
            "display_eligible": "INTEGER NOT NULL DEFAULT 1",
            "display_reason": "TEXT DEFAULT 'legacy'",
            "source": "TEXT DEFAULT 'extractor'",
        },
    )


def migrate_legacy_lifecycle(conn: sqlite3.Connection) -> None:
    if not table_columns(conn, "concept_lifecycle"):
        return
    add_missing_columns(conn, "concept_lifecycle", lifecycle_columns())


def paper_columns() -> dict[str, str]:
    return {
        "data_mode": "TEXT NOT NULL DEFAULT 'real'",
        "pdf_url": "TEXT",
        "is_open_access": "INTEGER NOT NULL DEFAULT 0",
        "oa_status": "TEXT",
        "openalex_id": "TEXT",
        "arxiv_id": "TEXT",
        "linked_published_paper_id": "TEXT",
        "raw_crossref_json": "TEXT",
        "condmat_view_eligible": "INTEGER NOT NULL DEFAULT 0",
        "condmat_view_reason": "TEXT DEFAULT ''",
    }


def update_run_columns() -> dict[str, str]:
    return {
        "baseline_from": "TEXT",
        "baseline_to": "TEXT",
        "trend_from": "TEXT",
        "trend_to": "TEXT",
        "trend_months": "INTEGER",
        "scope": "TEXT",
    }


def lifecycle_columns() -> dict[str, str]:
    return {
        "historical_first_seen": "TEXT",
        "trend_first_seen": "TEXT",
        "baseline_total_count": "INTEGER DEFAULT 0",
        "historical_count_before_trend": "INTEGER DEFAULT 0",
        "trend_total_count": "INTEGER DEFAULT 0",
        "novelty_score": "REAL DEFAULT 0",
        "concept_class": "TEXT DEFAULT 'physics_concept'",
        "is_historical": "INTEGER DEFAULT 0",
        "is_platform_term": "INTEGER DEFAULT 0",
    }


def term_month_columns() -> dict[str, str]:
    return {
        "data_mode": "TEXT NOT NULL DEFAULT 'mixed'",
        "normalized_share": "REAL NOT NULL DEFAULT 0",
        "weighted_normalized_share": "REAL NOT NULL DEFAULT 0",
        "display_eligible": "INTEGER NOT NULL DEFAULT 1",
        "display_reason": "TEXT DEFAULT 'legacy'",
    }


def monthly_corpus_columns() -> dict[str, str]:
    return {"data_mode": "TEXT NOT NULL DEFAULT 'mixed'"}


def ingest_run_columns() -> dict[str, str]:
    return {
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "source": "TEXT",
        "scope": "TEXT",
        "baseline_from": "TEXT",
        "baseline_to": "TEXT",
        "status": "TEXT",
        "fetched_count": "INTEGER DEFAULT 0",
        "kept_count": "INTEGER DEFAULT 0",
        "deduped_count": "INTEGER DEFAULT 0",
        "failed_count": "INTEGER DEFAULT 0",
        "error_summary": "TEXT",
        "config_json": "TEXT",
    }


def ingest_checkpoint_columns() -> dict[str, str]:
    return {
        "cursor": "TEXT",
        "page": "INTEGER DEFAULT 0",
        "updated_at": "TEXT",
    }


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    if not table_columns(conn, table):
        return
    existing = table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_monthly_corpus_stats(conn: sqlite3.Connection) -> None:
    existing = table_columns(conn, "monthly_corpus_stats")
    if existing and "data_mode" not in existing:
        conn.execute("DROP TABLE IF EXISTS monthly_corpus_stats")
        existing = set()
    if not existing:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_corpus_stats (
              month TEXT NOT NULL,
              corpus_scope TEXT NOT NULL,
              data_mode TEXT NOT NULL DEFAULT 'mixed',
              total_papers INTEGER NOT NULL DEFAULT 0,
              total_weighted_papers REAL NOT NULL DEFAULT 0,
              PRIMARY KEY (month, corpus_scope, data_mode)
            )
            """
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_monthly_corpus_scope ON monthly_corpus_stats(corpus_scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_monthly_corpus_mode ON monthly_corpus_stats(data_mode)")


def migrate_term_month_stats(conn: sqlite3.Connection) -> None:
    existing = table_columns(conn, "term_month_stats")
    if existing and ("corpus_scope" not in existing or "data_mode" not in existing):
        conn.execute("DROP TABLE IF EXISTS term_month_stats")
        existing = set()
    if not existing:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS term_month_stats (
              term TEXT NOT NULL,
              month TEXT NOT NULL,
              corpus_scope TEXT NOT NULL DEFAULT 'all',
              data_mode TEXT NOT NULL DEFAULT 'mixed',
              raw_freq INTEGER NOT NULL,
              weighted_freq REAL NOT NULL,
              normalized_share REAL NOT NULL DEFAULT 0,
              weighted_normalized_share REAL NOT NULL DEFAULT 0,
              momentum REAL NOT NULL,
              journal_breakdown TEXT NOT NULL,
              citation_signal REAL NOT NULL DEFAULT 0,
              display_eligible INTEGER NOT NULL DEFAULT 1,
              display_reason TEXT DEFAULT 'legacy',
              PRIMARY KEY (term, month, corpus_scope, data_mode)
            )
            """
        )
    else:
        add_missing_columns(conn, "term_month_stats", term_month_columns())
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_month_stats_month ON term_month_stats(month)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_month_stats_scope ON term_month_stats(corpus_scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_month_stats_display ON term_month_stats(display_eligible)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_term_month_stats_mode ON term_month_stats(data_mode)")


def _paper_payload(paper: dict[str, Any]) -> dict[str, Any]:
    date = paper.get("publication_date") or paper.get("submitted_date") or ""
    month = date[:7] if len(date) >= 7 else None
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
    doi = normalize_doi(paper.get("doi"))
    source = clean_text(paper.get("source") or "unknown")
    data_mode_value = paper.get("data_mode") or ("mock" if source == "mock" else "real")
    openalex_id = paper.get("openalex_id") or (paper.get("id") if str(paper.get("id") or "").startswith("https://openalex.org/") else "")
    arxiv_id = paper.get("arxiv_id") or ""
    paper_id = (
        paper.get("id")
        or (f"doi:{doi}" if doi else None)
        or (f"arxiv:{arxiv_id}" if arxiv_id else None)
        or stable_title_id(paper.get("title", "untitled"))
    )
    return {
        "id": paper_id,
        "doi": doi,
        "title": clean_text(paper.get("title") or "Untitled"),
        "abstract": clean_text(paper.get("abstract") or ""),
        "journal": clean_text(paper.get("journal") or paper.get("source_name") or "Unknown"),
        "publication_date": date,
        "year": year,
        "month": month,
        "source": source,
        "data_mode": clean_text(data_mode_value),
        "url": paper.get("url") or paper.get("doi_url") or "",
        "pdf_url": paper.get("pdf_url") or "",
        "is_open_access": 1 if paper.get("is_open_access") else 0,
        "oa_status": paper.get("oa_status") or "",
        "openalex_id": openalex_id,
        "arxiv_id": arxiv_id,
        "linked_published_paper_id": paper.get("linked_published_paper_id") or "",
        "cited_by_count": int(paper.get("cited_by_count") or 0),
        # The ingest filter is the source of truth for the strict Radar view.
        # Persist it so newly scanned papers are not silently hidden from the UI.
        "condmat_view_eligible": 1 if paper.get("condmat_view_eligible") else 0,
        "condmat_view_reason": clean_text(paper.get("condmat_view_reason") or ",".join(paper.get("filter_reasons") or [])),
        "raw_json": json.dumps(paper.get("raw_json", paper), ensure_ascii=False),
        "raw_crossref_json": json.dumps(paper.get("raw_crossref_json"), ensure_ascii=False) if paper.get("raw_crossref_json") else None,
    }


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    doi = str(value).strip().lower()
    doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    doi = doi.removeprefix("doi:")
    return doi or None


def stable_title_id(title: str) -> str:
    import hashlib

    normalized = "".join(ch.lower() for ch in title if ch.isalnum())
    return "title:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def normalized_title_key(title: str) -> str:
    return "".join(ch.lower() for ch in title if ch.isalnum())


def upsert_paper(conn: sqlite3.Connection, paper: dict[str, Any]) -> str:
    payload = _paper_payload(paper)
    now = utc_now()
    existing = None
    if payload["doi"]:
        existing = conn.execute(
            "SELECT id FROM papers "
            "WHERE doi = ? AND doi IS NOT NULL AND doi != ''",
            (payload["doi"],),
        ).fetchone()
    if not existing and payload["openalex_id"]:
        existing = conn.execute("SELECT id FROM papers WHERE openalex_id = ?", (payload["openalex_id"],)).fetchone()
    if not existing and payload["arxiv_id"]:
        existing = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", (payload["arxiv_id"],)).fetchone()
    if not existing:
        existing = conn.execute("SELECT id FROM papers WHERE id = ?", (payload["id"],)).fetchone()

    if existing:
        payload["id"] = existing["id"]
        conn.execute(
            """
            UPDATE papers SET
              doi = COALESCE(?, doi),
              title = ?,
              abstract = COALESCE(NULLIF(?, ''), abstract),
              journal = ?,
              publication_date = ?,
              year = ?,
              month = ?,
              source = ?,
              data_mode = ?,
              url = COALESCE(NULLIF(?, ''), url),
              pdf_url = COALESCE(NULLIF(?, ''), pdf_url),
              is_open_access = MAX(COALESCE(is_open_access, 0), ?),
              oa_status = COALESCE(NULLIF(?, ''), oa_status),
              openalex_id = COALESCE(NULLIF(?, ''), openalex_id),
              arxiv_id = COALESCE(NULLIF(?, ''), arxiv_id),
              linked_published_paper_id = COALESCE(NULLIF(?, ''), linked_published_paper_id),
              cited_by_count = MAX(COALESCE(cited_by_count, 0), ?),
              condmat_view_eligible = MAX(COALESCE(condmat_view_eligible, 0), ?),
              condmat_view_reason = COALESCE(NULLIF(?, ''), condmat_view_reason),
              raw_json = ?,
              raw_crossref_json = COALESCE(?, raw_crossref_json),
              updated_at = ?
            WHERE id = ?
            """,
            (
                payload["doi"],
                payload["title"],
                payload["abstract"],
                payload["journal"],
                payload["publication_date"],
                payload["year"],
                payload["month"],
                payload["source"],
                payload["data_mode"],
                payload["url"],
                payload["pdf_url"],
                payload["is_open_access"],
                payload["oa_status"],
                payload["openalex_id"],
                payload["arxiv_id"],
                payload["linked_published_paper_id"],
                payload["cited_by_count"],
                payload["condmat_view_eligible"],
                payload["condmat_view_reason"],
                payload["raw_json"],
                payload["raw_crossref_json"],
                now,
                payload["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO papers (
          id, doi, title, abstract, journal, publication_date, year, month, source, data_mode, url,
          pdf_url, is_open_access, oa_status, openalex_id, arxiv_id, linked_published_paper_id,
          cited_by_count, condmat_view_eligible, condmat_view_reason, raw_json, raw_crossref_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["id"],
            payload["doi"],
            payload["title"],
            payload["abstract"],
            payload["journal"],
            payload["publication_date"],
            payload["year"],
            payload["month"],
            payload["source"],
            payload["data_mode"],
            payload["url"],
            payload["pdf_url"],
            payload["is_open_access"],
            payload["oa_status"],
            payload["openalex_id"],
            payload["arxiv_id"],
            payload["linked_published_paper_id"],
            payload["cited_by_count"],
            payload["condmat_view_eligible"],
            payload["condmat_view_reason"],
            payload["raw_json"],
            payload["raw_crossref_json"],
            now,
            now,
        ),
    )
    return "inserted"


def replace_terms(conn: sqlite3.Connection, paper_id: str, terms: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM paper_terms WHERE paper_id = ?", (paper_id,))
    conn.executemany(
        """
        INSERT OR REPLACE INTO paper_terms
          (paper_id, term, term_type, normalized_term, confidence, display_eligible, display_reason, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                paper_id,
                term["term"],
                term["term_type"],
                term["normalized_term"],
                float(term.get("confidence", 1.0)),
                int(term.get("display_eligible", 1)),
                str(term.get("display_reason", "legacy")),
                str(term.get("source", "extractor")),
            )
            for term in terms
        ],
    )


def clear_data_mode(conn: sqlite3.Connection, data_mode: str) -> None:
    ids = [row["id"] for row in conn.execute("SELECT id FROM papers WHERE data_mode = ?", (data_mode,)).fetchall()]
    if not ids:
        return
    conn.executemany("DELETE FROM paper_terms WHERE paper_id = ?", [(paper_id,) for paper_id in ids])
    conn.execute("DELETE FROM papers WHERE data_mode = ?", (data_mode,))
    conn.execute("DELETE FROM term_month_stats")
    conn.execute("DELETE FROM monthly_corpus_stats")
    conn.execute("DELETE FROM concept_lifecycle")


def query_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def export_query_to_csv(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(sql, params).fetchall()
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = None
        for row in rows:
            data = dict(row)
            if writer is None:
                writer = csv.DictWriter(handle, fieldnames=list(data.keys()))
                writer.writeheader()
            writer.writerow(data)
        if writer is None:
            handle.write("")
    return output_path



# --- Real-data quickstart compatibility overrides (added after initial prototype). ---
def paper_columns() -> dict[str, str]:
    return {
        "source_scope": "TEXT",
        "data_mode": "TEXT NOT NULL DEFAULT 'real'",
        "condmat_confidence": "TEXT DEFAULT 'medium'",
        "pdf_url": "TEXT",
        "is_open_access": "INTEGER NOT NULL DEFAULT 0",
        "oa_status": "TEXT",
        "openalex_id": "TEXT",
        "arxiv_id": "TEXT",
        "linked_published_paper_id": "TEXT",
        "raw_crossref_json": "TEXT",
        "condmat_view_eligible": "INTEGER NOT NULL DEFAULT 0",
        "condmat_view_reason": "TEXT DEFAULT ''",
    }


def _paper_payload(paper: dict[str, Any]) -> dict[str, Any]:
    date = paper.get("publication_date") or paper.get("submitted_date") or ""
    month = date[:7] if len(date) >= 7 else None
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
    doi = normalize_doi(paper.get("doi"))
    source = clean_text(paper.get("source") or "unknown")
    data_mode_value = paper.get("data_mode") or ("mock" if source == "mock" else "real")
    openalex_id = paper.get("openalex_id") or (paper.get("id") if str(paper.get("id") or "").startswith("https://openalex.org/") else "")
    arxiv_id = paper.get("arxiv_id") or ""
    paper_id = (
        paper.get("id")
        or (f"doi:{doi}" if doi else None)
        or (f"arxiv:{arxiv_id}" if arxiv_id else None)
        or stable_title_id(paper.get("title", "untitled"))
    )
    return {
        "id": paper_id,
        "doi": doi,
        "title": clean_text(paper.get("title") or "Untitled"),
        "abstract": clean_text(paper.get("abstract") or ""),
        "journal": clean_text(paper.get("journal") or paper.get("source_name") or "Unknown"),
        "publication_date": date,
        "year": year,
        "month": month,
        "source": source,
        "source_scope": clean_text(paper.get("source_scope") or ("preprint" if source == "arxiv" else "published")),
        "data_mode": clean_text(data_mode_value),
        "condmat_confidence": clean_text(paper.get("condmat_confidence") or "medium"),
        "url": paper.get("url") or paper.get("doi_url") or "",
        "pdf_url": paper.get("pdf_url") or "",
        "is_open_access": 1 if paper.get("is_open_access") else 0,
        "oa_status": paper.get("oa_status") or "",
        "openalex_id": openalex_id,
        "arxiv_id": arxiv_id,
        "linked_published_paper_id": paper.get("linked_published_paper_id") or "",
        "cited_by_count": int(paper.get("cited_by_count") or 0),
        # The ingest filter is the source of truth for the strict Radar view.
        # Persist it so newly scanned papers are not silently hidden from the UI.
        "condmat_view_eligible": 1 if paper.get("condmat_view_eligible") else 0,
        "condmat_view_reason": clean_text(paper.get("condmat_view_reason") or ",".join(paper.get("filter_reasons") or [])),
        "raw_json": json.dumps(paper.get("raw_json", paper), ensure_ascii=False),
        "raw_crossref_json": json.dumps(paper.get("raw_crossref_json"), ensure_ascii=False) if paper.get("raw_crossref_json") else None,
    }


def upsert_paper(conn: sqlite3.Connection, paper: dict[str, Any]) -> str:
    payload = _paper_payload(paper)
    now = utc_now()
    existing = None
    if payload["doi"]:
        existing = conn.execute(
            "SELECT id FROM papers "
            "WHERE doi = ? AND doi IS NOT NULL AND doi != ''",
            (payload["doi"],),
        ).fetchone()
    if not existing and payload["openalex_id"]:
        existing = conn.execute("SELECT id FROM papers WHERE openalex_id = ?", (payload["openalex_id"],)).fetchone()
    if not existing and payload["arxiv_id"]:
        existing = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", (payload["arxiv_id"],)).fetchone()
    if not existing:
        existing = conn.execute("SELECT id FROM papers WHERE id = ?", (payload["id"],)).fetchone()

    if existing:
        payload["id"] = existing["id"]
        conn.execute(
            """
            UPDATE papers SET
              doi = COALESCE(?, doi),
              title = ?,
              abstract = COALESCE(NULLIF(?, ''), abstract),
              journal = ?,
              publication_date = ?,
              year = ?,
              month = ?,
              source = ?,
              source_scope = COALESCE(NULLIF(?, ''), source_scope),
              data_mode = ?,
              condmat_confidence = COALESCE(NULLIF(?, ''), condmat_confidence),
              url = COALESCE(NULLIF(?, ''), url),
              pdf_url = COALESCE(NULLIF(?, ''), pdf_url),
              is_open_access = MAX(COALESCE(is_open_access, 0), ?),
              oa_status = COALESCE(NULLIF(?, ''), oa_status),
              openalex_id = COALESCE(NULLIF(?, ''), openalex_id),
              arxiv_id = COALESCE(NULLIF(?, ''), arxiv_id),
              linked_published_paper_id = COALESCE(NULLIF(?, ''), linked_published_paper_id),
              cited_by_count = MAX(COALESCE(cited_by_count, 0), ?),
              condmat_view_eligible = MAX(COALESCE(condmat_view_eligible, 0), ?),
              condmat_view_reason = COALESCE(NULLIF(?, ''), condmat_view_reason),
              raw_json = ?,
              raw_crossref_json = COALESCE(?, raw_crossref_json),
              updated_at = ?
            WHERE id = ?
            """,
            (
                payload["doi"], payload["title"], payload["abstract"], payload["journal"], payload["publication_date"],
                payload["year"], payload["month"], payload["source"], payload["source_scope"], payload["data_mode"],
                payload["condmat_confidence"], payload["url"], payload["pdf_url"], payload["is_open_access"],
                payload["oa_status"], payload["openalex_id"], payload["arxiv_id"], payload["linked_published_paper_id"],
                payload["cited_by_count"], payload["raw_json"], payload["raw_crossref_json"], now, payload["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO papers (
          id, doi, title, abstract, journal, publication_date, year, month, source, source_scope, data_mode,
          condmat_confidence, url, pdf_url, is_open_access, oa_status, openalex_id, arxiv_id,
          linked_published_paper_id, cited_by_count, raw_json, raw_crossref_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["id"], payload["doi"], payload["title"], payload["abstract"], payload["journal"], payload["publication_date"],
            payload["year"], payload["month"], payload["source"], payload["source_scope"], payload["data_mode"],
            payload["condmat_confidence"], payload["url"], payload["pdf_url"], payload["is_open_access"], payload["oa_status"],
            payload["openalex_id"], payload["arxiv_id"], payload["linked_published_paper_id"], payload["cited_by_count"],
            payload["raw_json"], payload["raw_crossref_json"], now, now,
        ),
    )
    return "inserted"
# --- OpenAlex enrichment/download metadata overrides. ---
def paper_columns() -> dict[str, str]:
    return {
        "source_scope": "TEXT",
        "data_mode": "TEXT NOT NULL DEFAULT 'real'",
        "condmat_confidence": "TEXT DEFAULT 'medium'",
        "url": "TEXT",
        "pdf_url": "TEXT",
        "oa_url": "TEXT",
        "is_open_access": "INTEGER NOT NULL DEFAULT 0",
        "oa_status": "TEXT",
        "openalex_id": "TEXT",
        "arxiv_id": "TEXT",
        "linked_published_paper_id": "TEXT",
        "raw_crossref_json": "TEXT",
        "raw_openalex_json": "TEXT",
        "authorships_json": "TEXT",
        "institutions_json": "TEXT",
        "openalex_concepts_json": "TEXT",
        "openalex_topics_json": "TEXT",
        "openalex_keywords_json": "TEXT",
        "referenced_works_json": "TEXT",
        "related_works_json": "TEXT",
        "openalex_enriched_at": "TEXT",
        "download_status": "TEXT DEFAULT 'not_requested'",
        "download_priority": "INTEGER DEFAULT 0",
        "local_pdf_path": "TEXT",
        "zotero_export_status": "TEXT DEFAULT 'not_requested'",
        "last_download_attempt_at": "TEXT",
        "last_download_error": "TEXT",
        "condmat_view_eligible": "INTEGER NOT NULL DEFAULT 0",
        "condmat_view_reason": "TEXT DEFAULT ''",
    }


def _json_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return json.dumps(value, ensure_ascii=False)


def _paper_payload(paper: dict[str, Any]) -> dict[str, Any]:
    date = paper.get("publication_date") or paper.get("submitted_date") or ""
    month = date[:7] if len(date) >= 7 else None
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
    doi = normalize_doi(paper.get("doi"))
    source = clean_text(paper.get("source") or "unknown")
    data_mode_value = paper.get("data_mode") or ("mock" if source == "mock" else "real")
    openalex_id = paper.get("openalex_id") or (paper.get("id") if str(paper.get("id") or "").startswith("https://openalex.org/") else "")
    arxiv_id = paper.get("arxiv_id") or ""
    raw_json_value = paper.get("raw_json", paper)
    raw_openalex_value = paper.get("raw_openalex_json") or (raw_json_value if source == "openalex" or openalex_id else None)
    paper_id = (
        paper.get("id")
        or (f"doi:{doi}" if doi else None)
        or (f"arxiv:{arxiv_id}" if arxiv_id else None)
        or stable_title_id(paper.get("title", "untitled"))
    )
    open_access = paper.get("open_access") if isinstance(paper.get("open_access"), dict) else {}
    return {
        "id": paper_id,
        "doi": doi,
        "title": clean_text(paper.get("title") or "Untitled"),
        "abstract": clean_text(paper.get("abstract") or ""),
        "journal": clean_text(paper.get("journal") or paper.get("source_name") or "Unknown"),
        "publication_date": date,
        "year": year,
        "month": month,
        "source": source,
        "source_scope": clean_text(paper.get("source_scope") or ("preprint" if source == "arxiv" else "published")),
        "data_mode": clean_text(data_mode_value),
        "condmat_confidence": clean_text(paper.get("condmat_confidence") or "medium"),
        "url": paper.get("url") or paper.get("doi_url") or "",
        "pdf_url": paper.get("pdf_url") or "",
        "oa_url": paper.get("oa_url") or open_access.get("oa_url") or "",
        "is_open_access": 1 if paper.get("is_open_access") else 0,
        "oa_status": paper.get("oa_status") or open_access.get("oa_status") or "",
        "openalex_id": openalex_id,
        "arxiv_id": arxiv_id,
        "linked_published_paper_id": paper.get("linked_published_paper_id") or "",
        "cited_by_count": int(paper.get("cited_by_count") or 0),
        # The ingest filter is the source of truth for the strict Radar view.
        # Persist it so newly scanned papers are not silently hidden from the UI.
        "condmat_view_eligible": 1 if paper.get("condmat_view_eligible") else 0,
        "condmat_view_reason": clean_text(paper.get("condmat_view_reason") or ",".join(paper.get("filter_reasons") or [])),
        "raw_json": json.dumps(raw_json_value, ensure_ascii=False),
        "raw_crossref_json": _json_or_none(paper.get("raw_crossref_json")),
        "raw_openalex_json": _json_or_none(raw_openalex_value),
        "authorships_json": _json_or_none(paper.get("authorships") or paper.get("authorships_json")),
        "institutions_json": _json_or_none(paper.get("institutions") or paper.get("institutions_json")),
        "openalex_concepts_json": _json_or_none(paper.get("concepts") or paper.get("openalex_concepts") or paper.get("openalex_concepts_json")),
        "openalex_topics_json": _json_or_none(paper.get("topics") or paper.get("openalex_topics") or paper.get("openalex_topics_json")),
        "openalex_keywords_json": _json_or_none(paper.get("keywords") or paper.get("openalex_keywords") or paper.get("openalex_keywords_json")),
        "referenced_works_json": _json_or_none(paper.get("referenced_works") or paper.get("referenced_works_json")),
        "related_works_json": _json_or_none(paper.get("related_works") or paper.get("related_works_json")),
        "openalex_enriched_at": paper.get("openalex_enriched_at") or (utc_now() if raw_openalex_value else None),
    }


def upsert_paper(conn: sqlite3.Connection, paper: dict[str, Any]) -> str:
    payload = _paper_payload(paper)
    now = utc_now()
    existing = None
    if payload["doi"]:
        existing = conn.execute(
            "SELECT id FROM papers "
            "WHERE doi = ? AND doi IS NOT NULL AND doi != ''",
            (payload["doi"],),
        ).fetchone()
    if not existing and payload["openalex_id"]:
        existing = conn.execute("SELECT id FROM papers WHERE openalex_id = ?", (payload["openalex_id"],)).fetchone()
    if not existing and payload["arxiv_id"]:
        existing = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", (payload["arxiv_id"],)).fetchone()
    if not existing:
        existing = conn.execute("SELECT id FROM papers WHERE id = ?", (payload["id"],)).fetchone()

    if existing:
        payload["id"] = existing["id"]
        conn.execute(
            """
            UPDATE papers SET
              doi = COALESCE(?, doi),
              title = ?,
              abstract = COALESCE(NULLIF(?, ''), abstract),
              journal = ?,
              publication_date = ?,
              year = ?,
              month = ?,
              source = ?,
              source_scope = COALESCE(NULLIF(?, ''), source_scope),
              data_mode = ?,
              condmat_confidence = COALESCE(NULLIF(?, ''), condmat_confidence),
              url = COALESCE(NULLIF(?, ''), url),
              pdf_url = COALESCE(NULLIF(?, ''), pdf_url),
              oa_url = COALESCE(NULLIF(?, ''), oa_url),
              is_open_access = MAX(COALESCE(is_open_access, 0), ?),
              oa_status = COALESCE(NULLIF(?, ''), oa_status),
              openalex_id = COALESCE(NULLIF(?, ''), openalex_id),
              arxiv_id = COALESCE(NULLIF(?, ''), arxiv_id),
              linked_published_paper_id = COALESCE(NULLIF(?, ''), linked_published_paper_id),
              cited_by_count = MAX(COALESCE(cited_by_count, 0), ?),
              condmat_view_eligible = MAX(COALESCE(condmat_view_eligible, 0), ?),
              condmat_view_reason = COALESCE(NULLIF(?, ''), condmat_view_reason),
              raw_json = ?,
              raw_crossref_json = COALESCE(?, raw_crossref_json),
              raw_openalex_json = COALESCE(?, raw_openalex_json),
              authorships_json = COALESCE(?, authorships_json),
              institutions_json = COALESCE(?, institutions_json),
              openalex_concepts_json = COALESCE(?, openalex_concepts_json),
              openalex_topics_json = COALESCE(?, openalex_topics_json),
              openalex_keywords_json = COALESCE(?, openalex_keywords_json),
              referenced_works_json = COALESCE(?, referenced_works_json),
              related_works_json = COALESCE(?, related_works_json),
              openalex_enriched_at = COALESCE(?, openalex_enriched_at),
              updated_at = ?
            WHERE id = ?
            """,
            (
                payload["doi"], payload["title"], payload["abstract"], payload["journal"], payload["publication_date"],
                payload["year"], payload["month"], payload["source"], payload["source_scope"], payload["data_mode"],
                payload["condmat_confidence"], payload["url"], payload["pdf_url"], payload["oa_url"], payload["is_open_access"],
                payload["oa_status"], payload["openalex_id"], payload["arxiv_id"], payload["linked_published_paper_id"],
                payload["cited_by_count"], payload["condmat_view_eligible"], payload["condmat_view_reason"], payload["raw_json"], payload["raw_crossref_json"], payload["raw_openalex_json"],
                payload["authorships_json"], payload["institutions_json"], payload["openalex_concepts_json"],
                payload["openalex_topics_json"], payload["openalex_keywords_json"], payload["referenced_works_json"],
                payload["related_works_json"], payload["openalex_enriched_at"], now, payload["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO papers (
          id, doi, title, abstract, journal, publication_date, year, month, source, source_scope, data_mode,
          condmat_confidence, url, pdf_url, oa_url, is_open_access, oa_status, openalex_id, arxiv_id,
          linked_published_paper_id, cited_by_count, condmat_view_eligible, condmat_view_reason, raw_json, raw_crossref_json, raw_openalex_json,
          authorships_json, institutions_json, openalex_concepts_json, openalex_topics_json,
          openalex_keywords_json, referenced_works_json, related_works_json, openalex_enriched_at,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["id"], payload["doi"], payload["title"], payload["abstract"], payload["journal"], payload["publication_date"],
            payload["year"], payload["month"], payload["source"], payload["source_scope"], payload["data_mode"],
            payload["condmat_confidence"], payload["url"], payload["pdf_url"], payload["oa_url"], payload["is_open_access"],
            payload["oa_status"], payload["openalex_id"], payload["arxiv_id"], payload["linked_published_paper_id"],
            payload["cited_by_count"], payload["condmat_view_eligible"], payload["condmat_view_reason"], payload["raw_json"], payload["raw_crossref_json"], payload["raw_openalex_json"],
            payload["authorships_json"], payload["institutions_json"], payload["openalex_concepts_json"],
            payload["openalex_topics_json"], payload["openalex_keywords_json"], payload["referenced_works_json"],
            payload["related_works_json"], payload["openalex_enriched_at"], now, now,
        ),
    )
    return "inserted"
