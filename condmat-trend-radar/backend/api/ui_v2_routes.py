from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.api.analytics_cache import (
    analytics_database_revision,
    cached_analytics_payload,
    clear_analytics_cache,
)
from backend.api.analytics_v25 import build_analytics_payload
from backend.config import db_path
from backend.db.database import connect
from backend.library.local_search import fts_query
from backend.library.workbench import ensure_workbench_schema, get_user_state, list_collections


router = APIRouter(prefix="/api/ui-v2", tags=["radar-web-v2"])

_SYSTEM_HEALTH_CACHE_SECONDS = 86_400
_SYSTEM_HEALTH_ASYNC_THRESHOLD_BYTES = 1_000_000_000
_SYSTEM_HEALTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SYSTEM_HEALTH_IN_PROGRESS: set[str] = set()
_SYSTEM_HEALTH_LOCK = threading.Lock()

# These read models aggregate the production corpus and are intentionally
# cached briefly. A 30-second TTL keeps live-scan changes comfortably inside
# the 60-second visibility contract while avoiding repeated whole-corpus work
# when several dashboard widgets mount together.
_UI_RESPONSE_CACHE_SECONDS = 30.0
_UI_RESPONSE_CACHE_MAX_ENTRIES = 128
_UI_RESPONSE_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_UI_RESPONSE_IN_FLIGHT: dict[tuple[Any, ...], threading.Event] = {}
_UI_RESPONSE_CACHE_LOCK = threading.Lock()


def _ui_cache_now() -> float:
    return time.monotonic()


def _cached_ui_payload(key: tuple[Any, ...], builder) -> dict[str, Any]:
    """Return a short-lived deep-copied payload with per-key single flight."""
    while True:
        now = _ui_cache_now()
        with _UI_RESPONSE_CACHE_LOCK:
            cached = _UI_RESPONSE_CACHE.get(key)
            if cached and now - cached[0] < _UI_RESPONSE_CACHE_SECONDS:
                return copy.deepcopy(cached[1])
            event = _UI_RESPONSE_IN_FLIGHT.get(key)
            if event is None:
                event = threading.Event()
                _UI_RESPONSE_IN_FLIGHT[key] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        # The owner always signals in finally. The timeout is defensive; after
        # it expires we re-check instead of returning an unbounded stale value.
        event.wait(timeout=60.0)

    try:
        payload = builder()
        with _UI_RESPONSE_CACHE_LOCK:
            stored_at = _ui_cache_now()
            expired = [
                item_key
                for item_key, (item_time, _) in _UI_RESPONSE_CACHE.items()
                if stored_at - item_time >= _UI_RESPONSE_CACHE_SECONDS
            ]
            for item_key in expired:
                _UI_RESPONSE_CACHE.pop(item_key, None)
            if len(_UI_RESPONSE_CACHE) >= _UI_RESPONSE_CACHE_MAX_ENTRIES and key not in _UI_RESPONSE_CACHE:
                oldest_key = min(_UI_RESPONSE_CACHE, key=lambda item_key: _UI_RESPONSE_CACHE[item_key][0])
                _UI_RESPONSE_CACHE.pop(oldest_key, None)
            _UI_RESPONSE_CACHE[key] = (stored_at, copy.deepcopy(payload))
        return payload
    finally:
        with _UI_RESPONSE_CACHE_LOCK:
            finished = _UI_RESPONSE_IN_FLIGHT.pop(key, None)
            if finished is not None:
                finished.set()


def _clear_ui_response_cache() -> None:
    """Clear endpoint caches; primarily useful for deterministic tests."""
    with _UI_RESPONSE_CACHE_LOCK:
        _UI_RESPONSE_CACHE.clear()
    clear_analytics_cache()


def _collect_database_health(connection: sqlite3.Connection) -> dict[str, Any]:
    quick_row = connection.execute("PRAGMA quick_check(1)").fetchone()
    quick_check = str(quick_row[0]) if quick_row else "unknown"
    # Existence is sufficient for the health card; do not materialize a
    # potentially huge violation result set on the production database.
    foreign_key_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    return {
        "quick_check": quick_check,
        "foreign_key_violations": 1 if foreign_key_violation else 0,
        "health_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "health_cache_seconds": _SYSTEM_HEALTH_CACHE_SECONDS,
        "health_cached": False,
        "health_check_in_progress": False,
    }


def _collect_database_health_in_background(cache_key: str) -> None:
    try:
        background_connection = sqlite3.connect(cache_key, timeout=30)
        try:
            result = _collect_database_health(background_connection)
        finally:
            background_connection.close()
    except Exception:
        result = {
            "quick_check": "check_failed",
            "foreign_key_violations": None,
            "health_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "health_cache_seconds": _SYSTEM_HEALTH_CACHE_SECONDS,
            "health_cached": False,
            "health_check_in_progress": False,
        }
    with _SYSTEM_HEALTH_LOCK:
        _SYSTEM_HEALTH_CACHE[cache_key] = (time.monotonic(), result)
        _SYSTEM_HEALTH_IN_PROGRESS.discard(cache_key)


def _database_health(connection: sqlite3.Connection, cache_key: str) -> dict[str, Any]:
    now_monotonic = time.monotonic()
    with _SYSTEM_HEALTH_LOCK:
        cached = _SYSTEM_HEALTH_CACHE.get(cache_key)
        if cached and now_monotonic - cached[0] < _SYSTEM_HEALTH_CACHE_SECONDS:
            return {**cached[1], "health_cached": True}

    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    if page_count * page_size >= _SYSTEM_HEALTH_ASYNC_THRESHOLD_BYTES:
        with _SYSTEM_HEALTH_LOCK:
            should_start = cache_key not in _SYSTEM_HEALTH_IN_PROGRESS
            if should_start:
                _SYSTEM_HEALTH_IN_PROGRESS.add(cache_key)
        if should_start:
            threading.Thread(
                target=_collect_database_health_in_background,
                args=(cache_key,),
                name="condmat-db-health",
                daemon=True,
            ).start()
        return {
            "quick_check": "checking",
            "foreign_key_violations": None,
            "health_checked_at": None,
            "health_cache_seconds": _SYSTEM_HEALTH_CACHE_SECONDS,
            "health_cached": False,
            "health_check_in_progress": True,
        }

    result = _collect_database_health(connection)
    with _SYSTEM_HEALTH_LOCK:
        _SYSTEM_HEALTH_CACHE[cache_key] = (now_monotonic, result)
    return result

def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _csv_rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(sql, params):
        grouped[str(row[0])].append(str(row[1]))
    return grouped


def _decorate_papers(connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    version_ids = [str(row["paper_version_id"]) for row in rows]
    paper_ids = [str(row["canonical_paper_id"]) for row in rows]
    version_marks = ",".join("?" for _ in version_ids)
    paper_marks = ",".join("?" for _ in paper_ids)
    authors = _csv_rows(
        connection,
        f"""
        SELECT pa.paper_version_id, COALESCE(pa.raw_name, a.display_name)
        FROM paper_authors pa JOIN authors a ON a.id=pa.author_id
        WHERE pa.paper_version_id IN ({version_marks})
        ORDER BY pa.paper_version_id, pa.author_position
        """,
        tuple(version_ids),
    )
    materials = _csv_rows(
        connection,
        f"""
        SELECT pm.canonical_paper_id, m.canonical_name
        FROM paper_materials pm JOIN materials m ON m.id=pm.material_id
        WHERE pm.canonical_paper_id IN ({paper_marks})
        ORDER BY pm.canonical_paper_id, pm.confidence DESC, m.canonical_name
        """,
        tuple(paper_ids),
    )
    topics = _csv_rows(
        connection,
        f"""
        SELECT pt.canonical_paper_id, t.canonical_name
        FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id
        WHERE pt.canonical_paper_id IN ({paper_marks})
        ORDER BY pt.canonical_paper_id, pt.confidence DESC, t.canonical_name
        """,
        tuple(paper_ids),
    )
    for row in rows:
        row["authors"] = authors.get(str(row["paper_version_id"]), [])
        row["materials"] = materials.get(str(row["canonical_paper_id"]), [])
        row["topics"] = topics.get(str(row["canonical_paper_id"]), [])
        row["downloaded"] = bool(row.get("downloaded"))
        row["is_open_access"] = bool(row.get("is_open_access"))
        row["favorite"] = bool(row.get("favorite"))
    return rows


def _paper_search_sql(
    *,
    q: str,
    search_scope: str,
    author: str,
    topic: str,
    material: str,
    journal: str,
    version_type: str,
    year_from: int | None,
    year_to: int | None,
    pdf_status: str,
    open_access_only: bool,
    review_only: bool,
    condmat_only: bool,
    favorite_only: bool,
    reading_status: str,
    collection_id: str,
) -> tuple[str, list[str], list[Any], str, str]:
    joins: list[str] = []
    # This endpoint backs Discover, not the review console. Keep the legacy
    # condmat_only parameter for client compatibility, but never allow it to
    # widen the main Radar corpus. Ineligible records remain available through
    # /api/library/reviews and direct evidence/detail endpoints.
    clauses: list[str] = [
        "p.data_mode='real'",
        "COALESCE(p.condmat_view_eligible, 0)=1",
    ]
    params: list[Any] = []
    rank = "0.0"
    if q.strip():
        expression = fts_query(q)
        if not expression:
            clauses.append("0=1")
        else:
            joins.append("JOIN library_fts ON library_fts.paper_version_id=v.id")
            if search_scope == "fulltext":
                expression = " AND ".join(f"body:{token}" for token in expression.split(" AND "))
            elif search_scope == "metadata":
                expression = " AND ".join(f"{{title abstract}}:{token}" for token in expression.split(" AND "))
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
        clauses.append("lower(COALESCE(v.journal, '')) LIKE ?")
        params.append(f"%{journal.strip().lower()}%")
    if version_type == "preprint":
        clauses.append("v.version_type='preprint'")
    elif version_type == "publication":
        clauses.append("v.version_type IN ('publication','published')")
    if year_from is not None:
        clauses.append("p.year>=?")
        params.append(year_from)
    if year_to is not None:
        clauses.append("p.year<=?")
        params.append(year_to)
    if pdf_status == "downloaded":
        clauses.append("EXISTS (SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id)")
    elif pdf_status == "missing":
        clauses.append("NOT EXISTS (SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id)")
    elif pdf_status in {"pending", "retryable_failed", "permanent_failed", "downloading"}:
        clauses.append("EXISTS (SELECT 1 FROM download_tasks d WHERE d.canonical_paper_id=v.canonical_paper_id AND d.status=?)")
        params.append(pdf_status)
    elif pdf_status == "failed":
        clauses.append("EXISTS (SELECT 1 FROM download_tasks d WHERE d.canonical_paper_id=v.canonical_paper_id AND d.status IN ('retryable_failed','permanent_failed'))")
    if open_access_only:
        clauses.append("p.is_open_access=1")
    if review_only:
        clauses.append("EXISTS (SELECT 1 FROM manual_review_items r WHERE r.candidate_paper_id=v.canonical_paper_id AND r.status='pending')")
    if favorite_only:
        clauses.append("EXISTS (SELECT 1 FROM paper_user_state s WHERE s.canonical_paper_id=v.canonical_paper_id AND s.favorite=1)")
    if reading_status:
        clauses.append("EXISTS (SELECT 1 FROM paper_user_state s WHERE s.canonical_paper_id=v.canonical_paper_id AND s.reading_status=?)")
        params.append(reading_status)
    if collection_id:
        clauses.append("EXISTS (SELECT 1 FROM collection_papers cp WHERE cp.canonical_paper_id=v.canonical_paper_id AND cp.collection_id=?)")
        params.append(collection_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return " ".join(joins), clauses, params, rank, where


@router.get("/papers")
def papers(
    q: str = "",
    search_scope: str = Query("all", pattern="^(all|metadata|fulltext)$"),
    author: str = "",
    topic: str = "",
    material: str = "",
    journal: str = "",
    version_type: str = Query("", pattern="^(|preprint|publication)$"),
    year_from: int | None = None,
    year_to: int | None = None,
    pdf_status: str = "",
    open_access_only: bool = False,
    review_only: bool = False,
    condmat_only: bool = True,
    favorite_only: bool = False,
    reading_status: str = Query("", pattern="^(|unread|later|reading|read|archived)$"),
    collection_id: str = "",
    sort: str = Query("recent", pattern="^(recent|relevance|citations)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        joins, _clauses, params, rank, where = _paper_search_sql(
            q=q,
            search_scope=search_scope,
            author=author,
            topic=topic,
            material=material,
            journal=journal,
            version_type=version_type,
            year_from=year_from,
            year_to=year_to,
            pdf_status=pdf_status,
            open_access_only=open_access_only,
            review_only=review_only,
            condmat_only=condmat_only,
            favorite_only=favorite_only,
            reading_status=reading_status,
            collection_id=collection_id,
        )
        total = int(connection.execute(
            f"SELECT COUNT(DISTINCT v.canonical_paper_id) FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id {joins} {where}",
            tuple(params),
        ).fetchone()[0])
        grouped_rank = "MIN(library_fts.rank)" if "JOIN library_fts" in joins else "0.0"
        order_by = {
            "recent": "matched.latest_date DESC, v.id",
            "relevance": "matched.rank ASC, matched.latest_date DESC, v.id",
            "citations": "matched.citations DESC, matched.latest_date DESC, v.id",
        }[sort]
        rows = [dict(row) for row in connection.execute(
            f"""
            WITH matched AS (
              SELECT v.canonical_paper_id,
                     COALESCE(
                       MIN(CASE WHEN v.source<>'radar_legacy' AND NULLIF(TRIM(v.abstract),'') IS NOT NULL THEN v.id END),
                       MIN(CASE WHEN v.source<>'radar_legacy' THEN v.id END),
                       MIN(CASE WHEN NULLIF(TRIM(v.abstract),'') IS NOT NULL THEN v.id END),
                       MIN(v.id)
                     ) AS paper_version_id,
                     {grouped_rank} AS rank,
                     MAX(COALESCE(v.publication_date, v.submitted_date, '')) AS latest_date,
                     MAX(COALESCE(p.cited_by_count, 0)) AS citations
              FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id
              {joins} {where}
              GROUP BY v.canonical_paper_id
            )
            SELECT v.id AS paper_version_id, v.canonical_paper_id, v.version_type,
                   v.title, v.abstract, v.doi, v.arxiv_id, v.arxiv_version,
                   v.journal, v.publication_date, v.submitted_date, v.source, v.url, v.pdf_url,
                   p.year, p.is_open_access, p.cited_by_count, matched.rank AS rank,
                   EXISTS(SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id) AS downloaded,
                   (SELECT d.status FROM download_tasks d WHERE d.canonical_paper_id=v.canonical_paper_id
                    ORDER BY d.updated_at DESC LIMIT 1) AS download_status,
                   (SELECT f.extraction_status FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id
                    ORDER BY f.updated_at DESC LIMIT 1) AS extraction_status,
                   EXISTS(SELECT 1 FROM paper_user_state s WHERE s.canonical_paper_id=v.canonical_paper_id AND s.favorite=1) AS favorite,
                   COALESCE((SELECT s.reading_status FROM paper_user_state s WHERE s.canonical_paper_id=v.canonical_paper_id), 'unread') AS reading_status
            FROM matched
            JOIN paper_versions v ON v.id=matched.paper_version_id
            JOIN papers p ON p.id=matched.canonical_paper_id
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )]
        return {"items": _decorate_papers(connection, rows), "total": total, "count": len(rows),
                "limit": limit, "offset": offset, "query": q, "search_scope": search_scope}


@router.get("/papers/{canonical_paper_id:path}")
def paper_detail(canonical_paper_id: str) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        paper = connection.execute("SELECT * FROM papers WHERE id=?", (canonical_paper_id,)).fetchone()
        if not paper:
            raise HTTPException(status_code=404, detail="paper not found")
        versions = [dict(row) for row in connection.execute(
            "SELECT * FROM paper_versions WHERE canonical_paper_id=? ORDER BY COALESCE(publication_date, submitted_date, '') DESC",
            (canonical_paper_id,),
        )]
        version_ids = [row["id"] for row in versions]
        authors: list[dict[str, Any]] = []
        if version_ids:
            marks = ",".join("?" for _ in version_ids)
            authors = [dict(row) for row in connection.execute(
                f"""SELECT pa.paper_version_id, pa.author_position, COALESCE(pa.raw_name, a.display_name) AS display_name,
                           a.orcid, a.affiliations_json FROM paper_authors pa JOIN authors a ON a.id=pa.author_id
                    WHERE pa.paper_version_id IN ({marks}) ORDER BY pa.paper_version_id, pa.author_position""",
                tuple(version_ids),
            )]
            for author in authors:
                author["affiliations"] = _json(author.pop("affiliations_json", "[]"), [])
        materials = [dict(row) for row in connection.execute(
            """SELECT m.*, pm.confidence, pm.context_json FROM paper_materials pm
               JOIN materials m ON m.id=pm.material_id WHERE pm.canonical_paper_id=?
               ORDER BY pm.confidence DESC, m.canonical_name""", (canonical_paper_id,),
        )]
        for material in materials:
            material["context"] = _json(material.pop("context_json", "{}"), {})
        topics = [dict(row) for row in connection.execute(
            """SELECT t.*, pt.confidence FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id
               WHERE pt.canonical_paper_id=? ORDER BY pt.confidence DESC, t.canonical_name""", (canonical_paper_id,),
        )]
        files = [dict(row) for row in connection.execute(
            "SELECT * FROM paper_files WHERE canonical_paper_id=? ORDER BY created_at DESC", (canonical_paper_id,)
        )]
        tasks = [dict(row) for row in connection.execute(
            "SELECT * FROM download_tasks WHERE canonical_paper_id=? ORDER BY created_at DESC", (canonical_paper_id,)
        )]
        links = [dict(row) for row in connection.execute(
            """SELECT l.*, sv.version_type AS source_type, tv.version_type AS target_type
               FROM paper_version_links l JOIN paper_versions sv ON sv.id=l.source_version_id
               JOIN paper_versions tv ON tv.id=l.target_version_id
               WHERE sv.canonical_paper_id=? OR tv.canonical_paper_id=?""",
            (canonical_paper_id, canonical_paper_id),
        )]
        ai_row = connection.execute(
            """SELECT provider, model, result_json, updated_at FROM analysis_results
               WHERE canonical_paper_id=? AND analysis_type='deepseek_paper_brief' AND status='completed'
               ORDER BY updated_at DESC LIMIT 1""",
            (canonical_paper_id,),
        ).fetchone()
        ai_analysis = None
        if ai_row:
            ai_analysis = {
                "provider": ai_row["provider"] or "deepseek",
                "model": ai_row["model"] or "",
                "updated_at": ai_row["updated_at"],
                "analysis": _json(ai_row["result_json"], {}),
            }
        return {"paper": dict(paper), "versions": versions, "authors": authors, "materials": materials,
                "topics": topics, "files": files, "download_tasks": tasks, "version_links": links,
                "ai_analysis": ai_analysis, "user_state": get_user_state(connection, canonical_paper_id),
                "available_collections": list_collections(connection)}


def _material_rows(
    connection: sqlite3.Connection,
    *,
    min_evidence: int = 2,
    row_limit: int | None = None,
    offset: int = 0,
    q: str = "",
    view: str = "hot",
) -> tuple[str, list[dict[str, Any]], int, int]:
    anchor = str(connection.execute(
        """SELECT MIN(
                 COALESCE(MAX(substr(last_successful_cursor,1,10)), date('now')),
                 date('now')
               )
           FROM source_cursors WHERE status='ok'"""
    ).fetchone()[0])
    normalized_query = q.strip().lower()
    matched_materials_sql = "SELECT id FROM materials"
    query_params: tuple[Any, ...] = ()
    if normalized_query:
        # Scan aliases once. The previous correlated EXISTS scanned the complete
        # alias table for every material (millions of comparisons in production).
        matched_materials_sql = """
          SELECT id FROM materials
          WHERE lower(canonical_name) LIKE ?
             OR lower(COALESCE(material_family, '')) LIKE ?
             OR lower(COALESCE(phase, '')) LIKE ?
             OR lower(COALESCE(thickness, '')) LIKE ?
             OR lower(COALESCE(composition, '')) LIKE ?
          UNION
          SELECT material_id AS id FROM material_aliases WHERE lower(alias) LIKE ?
        """
        pattern = f"%{normalized_query}%"
        query_params = (pattern,) * 6
    order_by = {
        "hot": "count_30d DESC, count_7d DESC, total_papers DESC, canonical_name",
        "preprint": "preprint_change DESC, preprint_30d DESC, canonical_name",
        "publication": "publication_change DESC, publication_30d DESC, canonical_name",
        "burst": "trend_score DESC, count_7d DESC, canonical_name",
        "persistent": "count_12m DESC, count_30d DESC, canonical_name",
    }.get(view, "count_30d DESC, count_7d DESC, total_papers DESC, canonical_name")
    rows = [dict(row) for row in connection.execute(
        f"""
        WITH matched_materials AS MATERIALIZED (
          {matched_materials_sql}
        ), facts AS MATERIALIZED (
          SELECT pm.material_id, p.id AS canonical_paper_id, p.data_mode, pm.source AS evidence_source,
                 CASE
                   WHEN p.source_scope='preprint' THEN 'preprint'
                   WHEN p.source_scope='published' THEN 'publication'
                   WHEN p.source='arxiv' AND COALESCE(p.doi, '')='' THEN 'preprint'
                   ELSE 'publication'
                 END AS version_type,
                 substr(COALESCE(p.publication_date, p.month || '-01', ''),1,10) AS paper_date,
                 CASE WHEN p.data_mode='real'
                            AND COALESCE(p.publication_date, p.month || '-01', '')<>''
                            AND substr(COALESCE(p.publication_date, p.month || '-01', ''),1,10)<=?
                      THEN 1 ELSE 0 END AS is_dated_real
          FROM matched_materials matched
          JOIN paper_materials pm ON pm.material_id=matched.id
          JOIN papers p ON p.id=pm.canonical_paper_id
          WHERE p.data_mode='real'
            AND COALESCE(p.condmat_view_eligible, 0)=1
        ), agg AS (
          SELECT material_id,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND paper_date>=date(?, '-6 days') THEN canonical_paper_id END) AS count_7d,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND paper_date>=date(?, '-29 days') THEN canonical_paper_id END) AS count_30d,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND paper_date>=date(?, '-12 months') THEN canonical_paper_id END) AS count_12m,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND version_type='preprint' AND paper_date>=date(?, '-29 days') THEN canonical_paper_id END) AS preprint_30d,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND version_type='preprint' AND paper_date>=date(?, '-59 days') AND paper_date<date(?, '-29 days') THEN canonical_paper_id END) AS preprint_prev_30d,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND version_type IN ('publication','published') AND paper_date>=date(?, '-29 days') THEN canonical_paper_id END) AS publication_30d,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 AND version_type IN ('publication','published') AND paper_date>=date(?, '-59 days') AND paper_date<date(?, '-29 days') THEN canonical_paper_id END) AS publication_prev_30d,
                 COUNT(DISTINCT CASE WHEN is_dated_real=1 THEN canonical_paper_id END) AS total_papers,
                 MAX(CASE WHEN is_dated_real=1 THEN paper_date END) AS latest_paper_date,
                 COUNT(*) AS evidence_mentions,
                 replace(group_concat(DISTINCT evidence_source), ',', ', ') AS extraction_sources,
                 MAX(CASE WHEN data_mode='real'
                               AND evidence_source IN ('paper_text_formula','paper_text_registry')
                          THEN 1 ELSE 0 END) AS is_dynamic
          FROM facts GROUP BY material_id
        )        SELECT m.id, m.canonical_name, m.material_family, m.phase, m.thickness, m.composition,
               COALESCE(a.count_7d,0) AS count_7d, COALESCE(a.count_30d,0) AS count_30d,
               COALESCE(a.count_12m,0) AS count_12m, ROUND(COALESCE(a.count_12m,0)/12.0,1) AS monthly_baseline,
               COALESCE(a.preprint_30d,0) AS preprint_30d, COALESCE(a.preprint_prev_30d,0) AS preprint_prev_30d,
               COALESCE(a.publication_30d,0) AS publication_30d, COALESCE(a.publication_prev_30d,0) AS publication_prev_30d,
               COALESCE(a.total_papers,0) AS total_papers, a.latest_paper_date, NULL AS new_team_count,
               COALESCE(a.evidence_mentions,0) AS evidence_mentions,
               COALESCE(a.extraction_sources, '') AS extraction_sources,
               COALESCE(a.preprint_30d,0) - COALESCE(a.preprint_prev_30d,0) AS preprint_change,
               COALESCE(a.publication_30d,0) - COALESCE(a.publication_prev_30d,0) AS publication_change,
               ROUND(
                 (COALESCE(a.count_30d,0) - ROUND(COALESCE(a.count_12m,0)/12.0,1))
                 / MAX(ROUND(COALESCE(a.count_12m,0)/12.0,1), 1.0),
                 3
               ) AS trend_score,
               COUNT(*) OVER() AS _total_qualified,
               SUM(COALESCE(a.is_dynamic,0)) OVER() AS _dynamic_entities
        FROM matched_materials matched
        JOIN materials m ON m.id=matched.id
        LEFT JOIN agg a ON a.material_id=m.id
        WHERE COALESCE(a.total_papers,0)>=?
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (
            *query_params,
            anchor, anchor, anchor, anchor, anchor, anchor, anchor, anchor, anchor, anchor,
            max(1, int(min_evidence)),
            max(1, int(row_limit)) if row_limit else 1_000_000,
            max(0, int(offset)),
        ),
    )]
    total_qualified = int(rows[0]["_total_qualified"]) if rows else 0
    dynamic_entities = int(rows[0]["_dynamic_entities"] or 0) if rows else 0
    for row in rows:
        row.pop("_total_qualified", None)
        row.pop("_dynamic_entities", None)
        row["preprint_change"] = int(row["preprint_change"] or 0)
        row["publication_change"] = int(row["publication_change"] or 0)
        row["trend_score"] = float(row["trend_score"] or 0)
    return anchor, rows, total_qualified, dynamic_entities


def _dashboard_material_rows(connection: sqlite3.Connection, *, as_of_date: str, row_limit: int = 8) -> tuple[str, list[dict[str, Any]]]:
    """Fast, paper-derived material signals for the dashboard only.

    The full Materials view retains its all-history, version-level aggregation.
    The dashboard is intentionally limited to a one-year live window, using the
    paper-first index so its first paint is not delayed by the whole corpus.
    """
    anchor = str(connection.execute(
        """SELECT MIN(COALESCE(MAX(substr(publication_date,1,10)), date(?)), date(?))
           FROM papers WHERE data_mode='real' AND condmat_view_eligible=1
             AND publication_date IS NOT NULL""",
        (as_of_date, as_of_date),
    ).fetchone()[0])
    rows = [dict(row) for row in connection.execute(
        """
        WITH recent_papers AS (
          SELECT id, source_scope, substr(COALESCE(publication_date, month || '-01', ''),1,10) AS paper_date
          FROM papers
          WHERE data_mode='real' AND condmat_view_eligible=1
            AND COALESCE(publication_date, month || '-01', '')<>''
            AND substr(COALESCE(publication_date, month || '-01', ''),1,10)<=?
            AND substr(COALESCE(publication_date, month || '-01', ''),1,10)>=date(?, '-12 months')
        ), aggregate AS (
          SELECT pm.material_id,
                 COUNT(DISTINCT CASE WHEN rp.paper_date>=date(?, '-6 days') THEN rp.id END) AS count_7d,
                 COUNT(DISTINCT CASE WHEN rp.paper_date>=date(?, '-29 days') THEN rp.id END) AS count_30d,
                 COUNT(DISTINCT rp.id) AS count_12m,
                 COUNT(DISTINCT CASE WHEN rp.source_scope='preprint' AND rp.paper_date>=date(?, '-29 days') THEN rp.id END) AS preprint_30d,
                 COUNT(DISTINCT CASE WHEN rp.source_scope='preprint' AND rp.paper_date>=date(?, '-59 days') AND rp.paper_date<date(?, '-29 days') THEN rp.id END) AS preprint_prev_30d,
                 COUNT(DISTINCT CASE WHEN rp.source_scope<>'preprint' AND rp.paper_date>=date(?, '-29 days') THEN rp.id END) AS publication_30d,
                 COUNT(DISTINCT CASE WHEN rp.source_scope<>'preprint' AND rp.paper_date>=date(?, '-59 days') AND rp.paper_date<date(?, '-29 days') THEN rp.id END) AS publication_prev_30d,
                 MAX(rp.paper_date) AS latest_paper_date,
                 COUNT(*) AS evidence_mentions,
                 group_concat(DISTINCT pm.source) AS extraction_sources
          FROM recent_papers rp JOIN paper_materials pm ON pm.canonical_paper_id=rp.id
          GROUP BY pm.material_id
        )
        SELECT m.id, m.canonical_name, m.material_family, m.phase, m.thickness, m.composition,
               a.count_7d, a.count_30d, a.count_12m, ROUND(a.count_12m/12.0, 1) AS monthly_baseline,
               a.preprint_30d, a.preprint_prev_30d, a.publication_30d, a.publication_prev_30d,
               a.count_12m AS total_papers, a.latest_paper_date, NULL AS new_team_count,
               a.evidence_mentions, a.extraction_sources
        FROM aggregate a JOIN materials m ON m.id=a.material_id
        WHERE a.count_12m>=?
        ORDER BY a.count_30d DESC, a.count_7d DESC, a.count_12m DESC, m.canonical_name
        LIMIT ?
        """,
        (anchor,) * 10 + (1, max(1, row_limit)),
    )]
    for row in rows:
        row["preprint_change"] = int(row["preprint_30d"] or 0) - int(row["preprint_prev_30d"] or 0)
        row["publication_change"] = int(row["publication_30d"] or 0) - int(row["publication_prev_30d"] or 0)
        baseline = float(row["monthly_baseline"] or 0)
        row["trend_score"] = round((int(row["count_30d"] or 0) - baseline) / max(baseline, 1.0), 3)
    return anchor, rows

def _build_materials_payload(
    *,
    view: str,
    min_evidence: int,
    limit: int,
    offset: int,
    q: str,
) -> dict[str, Any]:
    with connect() as connection:
        anchor, rows, total_qualified, dynamic_entities = _material_rows(
            connection,
            min_evidence=min_evidence,
            row_limit=limit,
            offset=offset,
            q=q,
            view=view,
        )
        if not rows and offset > 0:
            _, _, total_qualified, dynamic_entities = _material_rows(
                connection,
                min_evidence=min_evidence,
                row_limit=1,
                offset=0,
                q=q,
                view=view,
            )
        extraction = {
            str(row["source"]): int(row["n"])
            for row in connection.execute(
                """SELECT pm.source, COUNT(*) AS n
                   FROM paper_materials pm
                   JOIN papers p ON p.id=pm.canonical_paper_id
                   WHERE p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1
                   GROUP BY pm.source ORDER BY n DESC"""
            )
        }
        return {
            "items": rows,
            "count": total_qualified,
            "returned_count": len(rows),
            "min_evidence": min_evidence,
            "offset": offset,
            "limit": limit,
            "query": q,
            "view": view,
            "data_anchor": anchor,
            "generation_method": "paper_title_abstract_extraction",
            "dynamic_entities": dynamic_entities,
            "extraction_sources": extraction,
        }


@router.get("/materials")
def materials(
    view: str = Query("hot", pattern="^(hot|preprint|publication|burst|persistent)$"),
    min_evidence: int = Query(2, ge=1, le=50),
    limit: int = Query(300, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    q: str = "",
) -> dict[str, Any]:
    cache_key = (
        "materials",
        str(db_path().resolve()),
        view,
        int(min_evidence),
        int(limit),
        int(offset),
        q.strip(),
    )
    return _cached_ui_payload(
        cache_key,
        lambda: _build_materials_payload(
            view=view,
            min_evidence=min_evidence,
            limit=limit,
            offset=offset,
            q=q,
        ),
    )


@router.get("/materials/{material_id}")
def material_detail(material_id: str) -> dict[str, Any]:
    with connect() as connection:
        material = connection.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not material:
            raise HTTPException(status_code=404, detail="material not found")
        aliases = [row[0] for row in connection.execute(
            "SELECT alias FROM material_aliases WHERE material_id=? ORDER BY alias", (material_id,)
        )]
        series = [dict(row) for row in connection.execute(
            """SELECT substr(COALESCE(v.publication_date, v.submitted_date),1,7) AS month,
                      COUNT(DISTINCT CASE WHEN v.version_type='preprint' THEN v.canonical_paper_id END) AS preprints,
                      COUNT(DISTINCT CASE WHEN v.version_type IN ('publication','published') THEN v.canonical_paper_id END) AS publications,
                      COUNT(DISTINCT v.canonical_paper_id) AS total
               FROM paper_materials pm JOIN paper_versions v ON v.canonical_paper_id=pm.canonical_paper_id
               JOIN papers p ON p.id=v.canonical_paper_id
               WHERE pm.material_id=? AND COALESCE(v.publication_date, v.submitted_date) IS NOT NULL AND p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1 AND date(COALESCE(v.publication_date, v.submitted_date))<=date('now')
               GROUP BY month ORDER BY month DESC LIMIT 24""", (material_id,),
        )]
        series.reverse()
        papers_rows = [dict(row) for row in connection.execute(
            """WITH eligible_canonicals AS (
                 SELECT pm.canonical_paper_id
                 FROM paper_materials pm
                 JOIN papers p ON p.id=pm.canonical_paper_id
                 WHERE pm.material_id=? AND p.data_mode='real'
                   AND COALESCE(p.condmat_view_eligible,0)=1
                 GROUP BY pm.canonical_paper_id
                 ORDER BY COALESCE(p.publication_date, p.month || '-01', '') DESC, p.id
                 LIMIT 12
               )
               SELECT v.id AS paper_version_id, v.canonical_paper_id, v.version_type, v.title, v.abstract,
                      v.doi, v.arxiv_id, v.journal, v.publication_date, v.submitted_date, v.source,
                      p.year, p.is_open_access, p.cited_by_count,
                      EXISTS(SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id) AS downloaded,
                      (SELECT d.status FROM download_tasks d WHERE d.canonical_paper_id=v.canonical_paper_id ORDER BY d.updated_at DESC LIMIT 1) AS download_status,
                      NULL AS extraction_status
               FROM eligible_canonicals ec
               JOIN papers p ON p.id=ec.canonical_paper_id
               JOIN paper_versions v ON v.id=(
                 SELECT v2.id FROM paper_versions v2
                 WHERE v2.canonical_paper_id=ec.canonical_paper_id
                 ORDER BY CASE WHEN v2.source='radar_legacy' THEN 1 ELSE 0 END,
                          CASE WHEN NULLIF(TRIM(v2.abstract),'') IS NULL THEN 1 ELSE 0 END,
                          CASE WHEN v2.version_type IN ('publication','published') THEN 0 ELSE 1 END,
                          COALESCE(v2.updated_date,v2.last_seen_at,v2.first_seen_at,'') DESC,
                          v2.id
                 LIMIT 1
               )
               ORDER BY COALESCE(p.publication_date, p.month || '-01', '') DESC, p.id""",
            (material_id,),
        )]
        topics = [dict(row) for row in connection.execute(
            """SELECT t.canonical_name, COUNT(DISTINCT pt.canonical_paper_id) AS paper_count
               FROM paper_materials pm JOIN paper_topics pt ON pt.canonical_paper_id=pm.canonical_paper_id
               JOIN topics t ON t.id=pt.topic_id JOIN papers p ON p.id=pm.canonical_paper_id WHERE pm.material_id=? AND p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1
               GROUP BY t.id ORDER BY paper_count DESC, t.canonical_name LIMIT 16""", (material_id,),
        )]
        evidence = [dict(row) for row in connection.execute(
            """
            SELECT pm.source, pm.confidence, pm.context_json,
                   p.id AS canonical_paper_id, p.title
            FROM paper_materials pm JOIN papers p ON p.id=pm.canonical_paper_id
            WHERE pm.material_id=? AND p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1
            ORDER BY pm.confidence DESC, p.publication_date DESC LIMIT 24
            """,
            (material_id,),
        )]
        for item in evidence:
            item["context"] = _json(item.pop("context_json", "{}"), {})
        counts = dict(connection.execute(
            """SELECT COUNT(DISTINCT v.canonical_paper_id) AS total,
                      COUNT(DISTINCT CASE WHEN v.version_type='preprint' THEN v.canonical_paper_id END) AS preprints,
                      COUNT(DISTINCT CASE WHEN v.version_type IN ('publication','published') THEN v.canonical_paper_id END) AS publications
               FROM paper_materials pm JOIN paper_versions v ON v.canonical_paper_id=pm.canonical_paper_id
               JOIN papers p ON p.id=v.canonical_paper_id WHERE pm.material_id=? AND p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1""", (material_id,),
        ).fetchone())
        return {"material": dict(material), "aliases": aliases, "series": series, "counts": counts,
                "papers": _decorate_papers(connection, papers_rows), "topics": topics, "evidence": evidence, "teams": None}


def _prepare_analytics_window(
    connection: sqlite3.Connection,
    range_from: str,
    range_to: str,
) -> None:
    """Materialize the small date window once instead of rescanning the full corpus."""
    connection.execute("DROP TABLE IF EXISTS temp.analytics_scoped_versions")
    connection.execute(
        """
        CREATE TEMP TABLE analytics_scoped_versions AS
        SELECT
          v.id AS paper_version_id,
          v.canonical_paper_id,
          v.version_type,
          date(COALESCE(
            NULLIF(v.publication_date, ''),
            NULLIF(v.submitted_date, ''),
            NULLIF(p.publication_date, '')
          )) AS paper_date,
          COALESCE(NULLIF(v.source, ''), NULLIF(p.source, ''), 'unknown') AS source_name,
          COALESCE(NULLIF(v.journal, ''), NULLIF(p.journal, ''), 'Unknown') AS journal_name,
          CASE
            WHEN trim(COALESCE(v.abstract, '')) <> ''
              OR trim(COALESCE(p.abstract, '')) <> '' THEN 1
            ELSE 0
          END AS has_abstract,
          CASE WHEN p.is_open_access=1 THEN 1 ELSE 0 END AS is_open_access
        FROM paper_versions v
        JOIN papers p ON p.id=v.canonical_paper_id
        WHERE p.data_mode='real'
          AND p.condmat_view_eligible=1
          AND date(COALESCE(
            NULLIF(v.publication_date, ''),
            NULLIF(v.submitted_date, ''),
            NULLIF(p.publication_date, '')
          )) BETWEEN date(?) AND date(?)
        """,
        (range_from, range_to),
    )
    connection.execute(
        "CREATE INDEX temp.idx_analytics_scoped_paper "
        "ON analytics_scoped_versions(canonical_paper_id)"
    )
    connection.execute(
        "CREATE INDEX temp.idx_analytics_scoped_date "
        "ON analytics_scoped_versions(paper_date)"
    )


_ANALYTICS_WINDOW_CTE = """
WITH scoped_versions AS (
  SELECT * FROM temp.analytics_scoped_versions
),
window_papers AS (
  SELECT
    canonical_paper_id,
    MAX(CASE WHEN version_type='preprint' THEN 1 ELSE 0 END) AS has_preprint,
    MAX(CASE WHEN version_type IN ('publication','published') THEN 1 ELSE 0 END) AS has_publication,
    MAX(has_abstract) AS has_abstract,
    MAX(is_open_access) AS is_open_access,
    MAX(paper_date) AS latest_paper_date
  FROM scoped_versions
  GROUP BY canonical_paper_id
)
"""

def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator * 100.0 / denominator, 1)


@router.get("/analytics")
def analytics(days: int = Query(30, ge=7, le=365)) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        revision = analytics_database_revision(connection)
        return cached_analytics_payload(
            days=days,
            revision=revision,
            builder=lambda: build_analytics_payload(connection, days),
        )
def _build_dashboard_payload(today: str) -> dict[str, Any]:
    tomorrow = (datetime.strptime(today, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    with connect() as connection:
        ensure_workbench_schema(connection)
        anchor, material_rows = _dashboard_material_rows(connection, as_of_date=today, row_limit=8)
        counts = dict(connection.execute(
            """SELECT COUNT(DISTINCT v.canonical_paper_id) AS new_papers,
                      COUNT(DISTINCT CASE WHEN v.version_type='preprint' THEN v.canonical_paper_id END) AS new_preprints,
                      COUNT(DISTINCT CASE WHEN v.version_type IN ('publication','published') THEN v.canonical_paper_id END) AS new_publications
               FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id
               WHERE v.first_seen_at>=? AND v.first_seen_at<?
                 AND p.data_mode='real' AND p.condmat_view_eligible=1""",
            (today, tomorrow),
        ).fetchone())
        recent_counts = dict(connection.execute(
            """
            SELECT
              COUNT(CASE WHEN paper_date>=date(?, '-6 days') THEN 1 END) AS papers_7d,
              COUNT(CASE WHEN paper_date=? THEN 1 END) AS latest_day_papers,
              COUNT(CASE WHEN paper_date=? AND paper_kind='preprint' THEN 1 END) AS latest_day_preprints,
              COUNT(CASE WHEN paper_date=? AND paper_kind='publication' THEN 1 END) AS latest_day_publications
            FROM (
              SELECT
                substr(COALESCE(publication_date, month || '-01', ''),1,10) AS paper_date,
                CASE
                  WHEN COALESCE(source_scope, '')='preprint'
                    OR (source='arxiv' AND COALESCE(doi, '')='') THEN 'preprint'
                  ELSE 'publication'
                END AS paper_kind
              FROM papers
              WHERE data_mode='real' AND condmat_view_eligible=1
                AND substr(COALESCE(publication_date, month || '-01', ''),1,10)
                    BETWEEN date(?, '-6 days') AND ?
            )
            """,
            (anchor, anchor, anchor, anchor, anchor, anchor),
        ).fetchone())
        counts["latest_data_date"] = anchor
        counts["papers_7d"] = int(recent_counts["papers_7d"] or 0)
        counts["latest_day_papers"] = int(recent_counts["latest_day_papers"] or 0)
        counts["latest_day_preprints"] = int(recent_counts["latest_day_preprints"] or 0)
        counts["latest_day_publications"] = int(recent_counts["latest_day_publications"] or 0)
        counts["preprint_to_publication"] = int(connection.execute(
            "SELECT COUNT(*) FROM paper_version_links WHERE link_type='preprint_to_publication' AND substr(created_at,1,10)=?", (today,)
        ).fetchone()[0])
        queue = {str(row["status"]): int(row["n"]) for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM download_tasks GROUP BY status"
        )}
        counts["downloads_completed"] = int(connection.execute(
            "SELECT COUNT(*) FROM download_tasks WHERE status='completed' AND substr(completed_at,1,10)=?", (today,)
        ).fetchone()[0])
        counts["download_pending"] = queue.get("pending", 0) + queue.get("resolving", 0) + queue.get("downloading", 0)
        counts["download_failed"] = queue.get("retryable_failed", 0) + queue.get("permanent_failed", 0)
        counts["manual_reviews"] = int(connection.execute(
            "SELECT COUNT(*) FROM manual_review_items WHERE status='pending'"
        ).fetchone()[0])
        counts["monitor_new_hits"] = int(connection.execute(
            """SELECT COUNT(*) FROM (
                   SELECT mh.monitor_id, mh.canonical_paper_id
                   FROM monitor_hits mh JOIN papers p ON p.id=mh.canonical_paper_id
                   WHERE substr(mh.first_matched_at,1,10)=?
                     AND p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1
                   GROUP BY mh.monitor_id, mh.canonical_paper_id
               )""",
            (today,),
        ).fetchone()[0])
        spotlight = [dict(row) for row in connection.execute(
            """WITH recent_canonicals AS (
                 SELECT p.id
                 FROM papers p
                 WHERE p.data_mode='real' AND p.condmat_view_eligible=1
                   AND date(COALESCE(p.publication_date, p.month || '-01'))<=date(?)
                 ORDER BY COALESCE(p.publication_date, p.month || '-01', '') DESC,
                          COALESCE(p.cited_by_count,0) DESC, p.id
                 LIMIT 32
               )
               SELECT v.id AS paper_version_id, v.canonical_paper_id, v.version_type, v.title, v.abstract,
                       v.doi, v.arxiv_id, v.journal, v.publication_date, v.submitted_date, v.source, v.url, v.pdf_url,
                       p.year, p.is_open_access, p.cited_by_count,
                       EXISTS(SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id) AS downloaded,
                       (SELECT d.status FROM download_tasks d WHERE d.canonical_paper_id=v.canonical_paper_id ORDER BY d.updated_at DESC LIMIT 1) AS download_status,
                       (SELECT f.extraction_status FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id ORDER BY f.updated_at DESC LIMIT 1) AS extraction_status
               FROM recent_canonicals r
               JOIN papers p ON p.id=r.id
               JOIN paper_versions v ON v.id=(
                 SELECT v2.id FROM paper_versions v2
                 WHERE v2.canonical_paper_id=p.id
                 ORDER BY CASE WHEN v2.source='radar_legacy' THEN 1 ELSE 0 END,
                          CASE WHEN NULLIF(TRIM(v2.abstract),'') IS NULL THEN 1 ELSE 0 END,
                          CASE WHEN v2.version_type IN ('publication','published') THEN 0 ELSE 1 END,
                          COALESCE(v2.updated_date,v2.last_seen_at,v2.first_seen_at,'') DESC,
                          v2.id
                 LIMIT 1
               )
               ORDER BY COALESCE(p.publication_date, p.month || '-01', '') DESC,
                        COALESCE(p.cited_by_count,0) DESC, p.id
               LIMIT 8""",
            (today,),
        )]
        runs = [dict(row) for row in connection.execute("SELECT * FROM daily_runs ORDER BY started_at DESC LIMIT 1")]
        for run in runs:
            run["report"] = _json(run.pop("report_json", "{}"), {})
        latest_report = runs[0].get("report") if runs else {}
        seeded_versions = latest_report.get("seeded_versions", {}) if isinstance(latest_report, dict) else {}
        ingest = latest_report.get("ingest", {}) if isinstance(latest_report, dict) else {}
        try:
            counts["latest_scan_versions"] = int(seeded_versions.get("inserted", 0))
            counts["latest_scan_kept"] = int(ingest.get("kept_count", 0))
        except (TypeError, ValueError, AttributeError):
            counts["latest_scan_versions"] = 0
            counts["latest_scan_kept"] = 0
        last_updated = connection.execute("SELECT MAX(updated_at) FROM papers WHERE data_mode='real' AND COALESCE(condmat_view_eligible,0)=1").fetchone()[0]
        metrics_range_from = (datetime.strptime(anchor, "%Y-%m-%d").date() - timedelta(days=6)).isoformat()
        return {"today": today, "last_updated_at": last_updated, "latest_data_date": anchor,
                "metrics_range_from": metrics_range_from, "metrics": counts,
                "spotlight": _decorate_papers(connection, spotlight), "hot_materials": material_rows[:8],
                "material_data_anchor": anchor, "latest_run": runs[0] if runs else None, "download_queue": queue}


@router.get("/dashboard")
def dashboard() -> dict[str, Any]:
    today = datetime.now().astimezone().date().isoformat()
    cache_key = ("dashboard", str(db_path().resolve()), today)
    return _cached_ui_payload(cache_key, lambda: _build_dashboard_payload(today))


@router.get("/system")
def system_status() -> dict[str, Any]:
    database = db_path()
    with connect() as connection:
        ensure_workbench_schema(connection)
        counts = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                  for table in ("papers", "paper_versions", "paper_files", "paper_file_text", "library_fts", "manual_review_items", "paper_user_state", "paper_collections", "collection_papers", "user_action_log")}
        counts["real_papers"] = int(connection.execute("SELECT COUNT(*) FROM papers WHERE data_mode='real'").fetchone()[0])
        counts["mock_papers"] = int(connection.execute("SELECT COUNT(*) FROM papers WHERE data_mode='mock'").fetchone()[0])
        counts["real_versions"] = int(connection.execute("SELECT COUNT(*) FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id WHERE p.data_mode='real'").fetchone()[0])
        file_stats = dict(connection.execute(
            """SELECT COALESCE(SUM(file_size),0) AS total_size,
                      COUNT(CASE WHEN validation_status NOT IN ('valid','ok','valid_pdf_header') THEN 1 END) AS invalid_files,
                      COUNT(CASE WHEN extraction_status='completed' THEN 1 END) AS parsed_files,
                      COUNT(CASE WHEN extraction_status='pending' THEN 1 END) AS pending_parse FROM paper_files"""
        ).fetchone())
        queue = {str(row["status"]): int(row["n"]) for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM download_tasks GROUP BY status"
        )}
        migrations = [dict(row) for row in connection.execute("SELECT * FROM unified_schema_migrations ORDER BY version")]
        runs = [dict(row) for row in connection.execute("SELECT * FROM daily_runs ORDER BY started_at DESC LIMIT 8")]
        cursors = [dict(row) for row in connection.execute("SELECT * FROM source_cursors ORDER BY source_name")]
        last_fts_source = connection.execute("SELECT MAX(extracted_at) FROM paper_file_text").fetchone()[0]
        health = _database_health(connection, str(database.resolve()))
        quick_check = str(health["quick_check"])
        foreign_key_value = health["foreign_key_violations"]
        foreign_key_violations = int(foreign_key_value) if foreign_key_value is not None else None
        duplicate_pdfs = int(connection.execute(
            """SELECT COALESCE(SUM(duplicate_count),0) FROM (
                   SELECT COUNT(*) - 1 AS duplicate_count
                   FROM paper_files GROUP BY sha256 HAVING COUNT(*)>1
               )"""
        ).fetchone()[0])
        unmatched_pdfs = int(connection.execute(
            "SELECT COUNT(*) FROM paper_files WHERE canonical_paper_id IS NULL"
        ).fetchone()[0])
        return {
            "database": {"path": str(database), "size_bytes": database.stat().st_size if database.exists() else 0,
                         "quick_check": quick_check,
                         "foreign_key_violations": foreign_key_violations,
                         "health_checked_at": health["health_checked_at"],
                         "health_cache_seconds": health["health_cache_seconds"],
                         "health_cached": health["health_cached"],
                         "health_check_in_progress": health["health_check_in_progress"],
                         "counts": counts},
            "pdf_library": {**file_stats, "unique_pdfs": counts["paper_files"],
                            "duplicates": duplicate_pdfs, "unmatched": unmatched_pdfs},
            "search": {"fts_entries": counts["library_fts"], "last_content_extracted_at": last_fts_source,
                       "pdf_fulltext_entries": counts["paper_file_text"]},
            "download_queue": queue,
            "daily": {"runs": runs, "source_cursors": cursors, "registered": None, "schedule": None},
            "migration": {"installed": bool(migrations), "migrations": migrations, "verification": {
                "quick_check": quick_check,
                "foreign_key_violations": foreign_key_violations,
                "health_checked_at": health["health_checked_at"],
                "health_cache_seconds": health["health_cache_seconds"],
                "health_cached": health["health_cached"],
                "health_check_in_progress": health["health_check_in_progress"],
                "source": "live",
            }},
        }
