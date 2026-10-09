from __future__ import annotations

import re
import sqlite3
from typing import Any


def fts_query(value: str) -> str:
    tokens = re.findall(r"[\w.-]+", value, flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def rebuild_search_index(connection: sqlite3.Connection) -> dict[str, int]:
    """Atomically replace the complete FTS index in the caller's transaction.

    Do not commit between DELETE and INSERT: readers must see either the prior
    complete index or the new complete index, never a partially rebuilt corpus.
    """
    before = int(connection.execute("SELECT COUNT(*) FROM library_fts").fetchone()[0])
    connection.execute("DELETE FROM library_fts")
    connection.execute(
        """
        INSERT INTO library_fts(canonical_paper_id, paper_version_id, title, abstract, body)
        SELECT v.canonical_paper_id, v.id, v.title, COALESCE(v.abstract, ''),
               COALESCE((
                 SELECT group_concat(t.text_content, '\n')
                 FROM paper_files f
                 JOIN paper_file_text t ON t.paper_file_id=f.id
                 WHERE f.paper_version_id=v.id OR
                       (f.paper_version_id IS NULL AND f.canonical_paper_id=v.canonical_paper_id)
               ), '')
        FROM paper_versions v
        """
    )
    after = int(connection.execute("SELECT COUNT(*) FROM library_fts").fetchone()[0])
    return {"before": before, "after": after, "indexed": after}


def refresh_search_entries(connection: sqlite3.Connection, version_ids: list[str]) -> dict[str, int]:
    ids = list(dict.fromkeys(str(item) for item in version_ids if item))
    if not ids:
        return {"refreshed": 0}

    # FTS5 UNINDEXED columns are stored metadata, not B-tree indexes. Deleting
    # one version at a time therefore scans the whole virtual table once per
    # version. Materialize requested IDs in a connection-local B-tree so FTS is
    # scanned only once and paper_versions is joined by its primary key.
    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _refresh_search_entry_ids (
            id TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    connection.execute("DELETE FROM temp._refresh_search_entry_ids")
    connection.executemany(
        "INSERT OR IGNORE INTO temp._refresh_search_entry_ids(id) VALUES (?)",
        ((version_id,) for version_id in ids),
    )
    connection.execute(
        """
        DELETE FROM library_fts
        WHERE paper_version_id IN (
            SELECT id FROM temp._refresh_search_entry_ids
        )
        """
    )
    connection.execute(
        """
        INSERT INTO library_fts(canonical_paper_id, paper_version_id, title, abstract, body)
        SELECT v.canonical_paper_id, v.id, v.title, COALESCE(v.abstract, ''),
               COALESCE((
                 SELECT group_concat(t.text_content, '\n')
                 FROM paper_files f
                 JOIN paper_file_text t ON t.paper_file_id=f.id
                 WHERE f.paper_version_id=v.id OR
                       (f.paper_version_id IS NULL AND f.canonical_paper_id=v.canonical_paper_id)
               ), '')
        FROM temp._refresh_search_entry_ids requested
        JOIN paper_versions v ON v.id=requested.id
        """
    )
    connection.execute("DELETE FROM temp._refresh_search_entry_ids")
    return {"refreshed": len(ids)}


def index_missing_search_entries(connection: sqlite3.Connection) -> dict[str, int]:
    before = int(connection.execute("SELECT COUNT(*) FROM library_fts").fetchone()[0])
    # Avoid a correlated NOT EXISTS against the UNINDEXED FTS metadata column:
    # snapshot existing IDs with one FTS scan, then use a B-tree join.
    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _indexed_search_entry_ids (
            id TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    connection.execute("DELETE FROM temp._indexed_search_entry_ids")
    connection.execute(
        """
        INSERT OR IGNORE INTO temp._indexed_search_entry_ids(id)
        SELECT paper_version_id
        FROM library_fts
        WHERE paper_version_id IS NOT NULL AND paper_version_id <> ''
        """
    )
    connection.execute(
        """
        INSERT INTO library_fts(canonical_paper_id, paper_version_id, title, abstract, body)
        SELECT v.canonical_paper_id, v.id, v.title, COALESCE(v.abstract, ''),
               COALESCE((
                 SELECT group_concat(t.text_content, '\n')
                 FROM paper_files f
                 JOIN paper_file_text t ON t.paper_file_id=f.id
                 WHERE f.paper_version_id=v.id OR
                       (f.paper_version_id IS NULL AND f.canonical_paper_id=v.canonical_paper_id)
               ), '')
        FROM paper_versions v
        LEFT JOIN temp._indexed_search_entry_ids existing ON existing.id=v.id
        WHERE existing.id IS NULL
        """
    )
    connection.execute("DELETE FROM temp._indexed_search_entry_ids")
    after = int(connection.execute("SELECT COUNT(*) FROM library_fts").fetchone()[0])
    return {"before": before, "after": after, "indexed": max(0, after - before)}

def search_library(
    connection: sqlite3.Connection,
    query: str = "",
    *,
    author: str = "",
    topic: str = "",
    material: str = "",
    journal: str = "",
    canonical_paper_id: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    download_status: str = "",
    open_access_only: bool = False,
    condmat_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    joins: list[str] = []
    clauses: list[str] = []
    params: list[Any] = []
    rank = "0.0"
    if condmat_only:
        clauses.extend(["p.data_mode='real'", "COALESCE(p.condmat_view_eligible, 0)=1"])
    if query.strip():
        expression = fts_query(query)
        if not expression:
            return {"items": [], "count": 0, "query": query}
        joins.append("JOIN library_fts ON library_fts.paper_version_id=v.id")
        clauses.append("library_fts MATCH ?")
        params.append(expression)
        rank = "bm25(library_fts)"
    if author.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_authors pa JOIN authors a ON a.id=pa.author_id "
            "WHERE pa.paper_version_id=v.id AND a.normalized_name LIKE ?)"
        )
        params.append(f"%{author.strip().lower()}%")
    if topic.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id "
            "WHERE pt.canonical_paper_id=v.canonical_paper_id AND lower(t.canonical_name) LIKE ?)"
        )
        params.append(f"%{topic.strip().lower()}%")
    if material.strip():
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_materials pm JOIN materials m ON m.id=pm.material_id "
            "WHERE pm.canonical_paper_id=v.canonical_paper_id AND lower(m.canonical_name) LIKE ?)"
        )
        params.append(f"%{material.strip().lower()}%")
    if journal.strip():
        clauses.append("lower(COALESCE(NULLIF(v.journal, ''), p.journal, '')) LIKE ?")
        params.append(f"%{journal.strip().lower()}%")
    if canonical_paper_id.strip():
        clauses.append("v.canonical_paper_id=?")
        params.append(canonical_paper_id.strip())
    if year_from is not None:
        clauses.append("p.year >= ?")
        params.append(year_from)
    if year_to is not None:
        clauses.append("p.year <= ?")
        params.append(year_to)
    if download_status == "downloaded":
        clauses.append("EXISTS (SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id)")
    elif download_status == "missing":
        clauses.append("NOT EXISTS (SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id)")
    if open_access_only:
        clauses.append("p.is_open_access=1")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        SELECT v.id AS paper_version_id, v.canonical_paper_id, v.version_type,
               v.title, v.abstract, v.doi, v.arxiv_id, v.arxiv_version,
               v.journal, v.publication_date, v.source, v.url, v.pdf_url,
               p.year, p.is_open_access, p.cited_by_count, {rank} AS rank,
               EXISTS(SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id) AS downloaded
        FROM paper_versions v
        JOIN papers p ON p.id=v.canonical_paper_id
        {' '.join(joins)}
        {where}
        ORDER BY rank ASC, COALESCE(v.publication_date, '') DESC
        LIMIT ? OFFSET ?
    """
    rows = [dict(row) for row in connection.execute(sql, (*params, max(1, min(limit, 500)), max(0, offset)))]
    return {"items": rows, "count": len(rows), "query": query, "offset": offset}
