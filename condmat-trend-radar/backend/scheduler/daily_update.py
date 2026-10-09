from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from backend.analytics.stats import rebuild_paper_terms
from backend.config import db_path, locks_dir, unpaywall_email
from backend.db.database import connect
from backend.downloader.queue import enqueue_download, requeue_downloads_after_metadata_change, run_download_queue
from backend.ingest.lock import IngestLock
from backend.ingest.source_coverage import run_source_backfill, run_source_coverage_audit
from backend.ingest_real import run as run_real_ingest
from backend.library.local_search import fts_query, rebuild_search_index, refresh_search_entries, search_library
from backend.library.material_discovery import sync_materials_from_papers
from backend.library.metadata_enrichment import backfill_missing_abstracts
from backend.library.repository import seed_radar_versions, seed_topics_and_materials, stable_id, utc_now
from backend.library.version_linker import link_preprints_and_publications
from backend.library.workbench import ensure_workbench_schema
from backend.migrations.unified_library import apply_unified_schema


DISCOVERY_CURSOR_SOURCE = "openalex_crossref_arxiv"
LEGACY_DISCOVERY_CURSOR_SOURCE = "openalex_arxiv"


@dataclass(frozen=True)
class DailyOptions:
    dry_run: bool = True
    skip_network: bool = False
    scope: str = "core"
    include_arxiv: bool = True
    include_crossref: bool = True
    download_limit: int = 10
    force_stale_lock: bool = False
    scan_mode: str = "live"
    historical_days: int = 180
    abstract_backfill_limit: int = 10


def _source_cursor(connection: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM source_cursors WHERE source_name=?", (source,)).fetchone()
    return dict(row) if row else None

def _discovery_cursor(connection: sqlite3.Connection) -> dict[str, Any] | None:
    current = _source_cursor(connection, DISCOVERY_CURSOR_SOURCE)
    if current and current.get("last_successful_cursor"):
        return current
    legacy = _source_cursor(connection, LEGACY_DISCOVERY_CURSOR_SOURCE)
    return legacy or current


def _date_window(
    cursor: dict[str, Any] | None,
    *,
    today_value: date | None = None,
) -> tuple[str, str]:
    """Incremental window anchored only to the last safe watermark."""
    today = today_value or date.today()
    if cursor and cursor.get("last_successful_cursor"):
        try:
            start = date.fromisoformat(str(cursor["last_successful_cursor"]))
        except ValueError:
            start = today - timedelta(days=2)
    else:
        start = today - timedelta(days=2)
    return start.isoformat(), today.isoformat()


def _scan_window(
    options: DailyOptions,
    cursor: dict[str, Any] | None,
    *,
    today_value: date | None = None,
) -> tuple[str, str, str, int | None]:
    today = today_value or date.today()
    if options.scan_mode == "full":
        historical_days = max(30, min(int(options.historical_days), 3650))
        start = today - timedelta(days=historical_days - 1)
        return (
            start.isoformat(),
            today.isoformat(),
            "fixed_historical_lookback",
            historical_days,
        )
    date_from, date_to = _date_window(cursor, today_value=today)
    return date_from, date_to, "safe_watermark_incremental", None


def _progress(
    connection: sqlite3.Connection,
    run_id: str,
    stage: str,
    percent: int,
    *,
    commit: bool = True,
    **payload: Any,
) -> None:
    preserved: dict[str, Any] = {}
    row = connection.execute("SELECT report_json FROM daily_runs WHERE id=?", (run_id,)).fetchone()
    if row:
        try:
            previous = json.loads(row["report_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            previous = {}
        if isinstance(previous, dict):
            for key in (
                "scan_mode",
                "date_from",
                "date_to",
                "window_policy",
                "historical_days",
                "pipeline_stages",
            ):
                if key in previous:
                    preserved[key] = previous[key]
    report = {
        **preserved,
        "stage": stage,
        "percent": max(0, min(100, int(percent))),
        "updated_at": utc_now(),
        **payload,
    }
    connection.execute(
        "UPDATE daily_runs SET report_json=? WHERE id=?",
        (json.dumps(report, ensure_ascii=False, default=str), run_id),
    )
    if commit:
        connection.commit()


def _pipeline_stage_progress(
    connection: sqlite3.Connection,
    run_id: str,
    stage: str,
    percent: int,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    commit: bool = True,
    **payload: Any,
) -> None:
    """Publish durable begin/completed state for a bounded local stage."""
    row = connection.execute("SELECT report_json FROM daily_runs WHERE id=?", (run_id,)).fetchone()
    try:
        previous = json.loads(row["report_json"] or "{}") if row else {}
    except (json.JSONDecodeError, TypeError):
        previous = {}
    existing_pipeline = previous.get("pipeline_stages") if isinstance(previous, dict) else None
    pipeline = dict(existing_pipeline) if isinstance(existing_pipeline, dict) else {}
    existing_state = pipeline.get(stage)
    stage_state = dict(existing_state) if isinstance(existing_state, dict) else {}
    now = utc_now()
    if status == "running":
        stage_state.setdefault("started_at", now)
        stage_state.pop("completed_at", None)
    elif status in {"completed", "skipped", "deferred"}:
        stage_state.setdefault("started_at", now)
        stage_state["completed_at"] = now
    stage_state["status"] = status
    if result is not None:
        stage_state["result"] = result
    pipeline[stage] = stage_state
    _progress(
        connection,
        run_id,
        stage,
        percent,
        commit=commit,
        stage_status=status,
        pipeline_stages=pipeline,
        **payload,
    )

def _finalize_cancelled_run(
    connection: sqlite3.Connection,
    run_id: str,
    ingest_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Atomically preserve a cancellation instead of overwriting it later."""
    row = connection.execute(
        "SELECT status, report_json FROM daily_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    ingest_cancelled = bool(
        ingest_result
        and (
            ingest_result.get("cancelled")
            or str(ingest_result.get("status") or "").lower() == "cancelled"
        )
    )
    if row is None or (str(row["status"] or "") not in {"cancel_requested", "cancelled"} and not ingest_cancelled):
        return None
    try:
        report = json.loads(row["report_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        report = {}
    if not isinstance(report, dict):
        report = {}
    finished_at = utc_now()
    report.update(
        {
            "stage": "cancelled",
            "cancelled": True,
            "partial": True,
            "updated_at": finished_at,
            "finished_at": finished_at,
        }
    )
    if ingest_result is not None:
        report["ingest"] = ingest_result
    connection.execute(
        "UPDATE daily_runs SET status='cancelled', finished_at=?, error_message=NULL, report_json=? WHERE id=?",
        (finished_at, json.dumps(report, ensure_ascii=False, default=str), run_id),
    )
    connection.commit()
    return {
        "status": "CANCELLED",
        "run_id": run_id,
        "cancelled": True,
        "partial": True,
        "finished_at": finished_at,
        "ingest": ingest_result or {},
        "report": report,
    }


def create_daily_run(options: DailyOptions, *, database: Path | None = None, trigger_type: str = "manual_live") -> str:
    database = database or db_path()
    run_id = str(uuid.uuid4())
    with connect(database) as connection:
        apply_unified_schema(connection)
        cursor = _discovery_cursor(connection)
        date_from, date_to, window_policy, historical_days = _scan_window(options, cursor)
        queued_report = {
            "stage": "queued",
            "percent": 0,
            "scan_mode": options.scan_mode,
            "date_from": date_from,
            "date_to": date_to,
            "window_policy": window_policy,
            "historical_days": historical_days,
        }
        connection.execute(
            """
            INSERT INTO daily_runs(id, status, dry_run, started_at, trigger_type, report_json)
            VALUES (?, 'queued', ?, ?, ?, ?)
            """,
            (
                run_id,
                int(options.dry_run),
                utc_now(),
                trigger_type,
                json.dumps(queued_report, ensure_ascii=False),
            ),
        )
    return run_id


def active_daily_run(*, database: Path | None = None) -> dict[str, Any] | None:
    with connect(database or db_path()) as connection:
        apply_unified_schema(connection)
        row = connection.execute(
            "SELECT * FROM daily_runs WHERE status IN ('queued','running','cancel_requested') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["report"] = json.loads(item.get("report_json") or "{}")
        except json.JSONDecodeError:
            item["report"] = {}
        return item


def daily_run_status(run_id: str, *, database: Path | None = None) -> dict[str, Any] | None:
    with connect(database or db_path()) as connection:
        apply_unified_schema(connection)
        row = connection.execute("SELECT * FROM daily_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["report"] = json.loads(item.get("report_json") or "{}")
        except json.JSONDecodeError:
            item["report"] = {}
        return item


def _record_cursor_attempt(
    connection: sqlite3.Connection,
    source: str,
    *,
    status: str,
    cursor: str | None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    # A partial multi-source run may have missed an entire provider or journal.
    # Do not advance the watermark: the next scan must replay the overlap and
    # rely on canonical deduplication rather than make that gap permanent.
    successful = status in {"ok", "completed"}
    connection.execute(
        """
        INSERT INTO source_cursors
        (source_name, last_successful_cursor, last_successful_at, last_attempt_at,
         status, error_message, metadata_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_name) DO UPDATE SET
          last_successful_cursor=CASE WHEN ? THEN excluded.last_successful_cursor ELSE source_cursors.last_successful_cursor END,
          last_successful_at=CASE WHEN ? THEN excluded.last_successful_at ELSE source_cursors.last_successful_at END,
          last_attempt_at=excluded.last_attempt_at,
          status=excluded.status,
          error_message=excluded.error_message,
          metadata_json=excluded.metadata_json,
          updated_at=excluded.updated_at
        """,
        (source, cursor if successful else None, now if successful else None, now, status, error, json.dumps(metadata or {}, ensure_ascii=False), now, 1 if successful else 0, 1 if successful else 0),
    )


def _effective_monitor_query(query: str, filters: dict[str, Any]) -> str:
    """Avoid requiring structured author/journal text to appear in the title."""
    monitor_type = str(filters.get("monitor_type") or "").strip().lower()
    if monitor_type in {"author", "journal", "material", "topic"}:
        if str(filters.get(monitor_type) or "").strip():
            return ""
    if str(filters.get("canonical_paper_id") or "").strip():
        return ""
    return str(query or "")


def _canonical_monitor_items(
    items: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Return one preferred source version for each logical paper."""
    ordered_ids: list[str] = []
    preferred: dict[str, dict[str, Any]] = {}

    def version_score(item: dict[str, Any]) -> tuple[int, int, str, str]:
        version_type = str(item.get("version_type") or "").lower()
        return (
            1 if version_type in {"published", "publication"} else 0,
            1 if item.get("doi") else 0,
            str(item.get("publication_date") or ""),
            str(item.get("paper_version_id") or ""),
        )

    for item in items:
        canonical_id = str(item.get("canonical_paper_id") or "")
        if not canonical_id:
            continue
        current = preferred.get(canonical_id)
        if current is None:
            ordered_ids.append(canonical_id)
            preferred[canonical_id] = item
        elif version_score(item) > version_score(current):
            preferred[canonical_id] = item
    return [preferred[item_id] for item_id in ordered_ids[: max(1, limit)]]


def _search_monitor_candidates(
    connection: sqlite3.Connection,
    query: str,
    filters: dict[str, Any],
    candidate_version_ids: list[str],
) -> list[dict[str, Any]]:
    """Apply a monitor to the current scan set before its result limit.

    Filtering after a global top-N search lets older, newer-dated records crowd
    the just-discovered versions out of an automatic scan. This query mirrors
    library search but constrains versions first, so the monitor limit applies
    only to the current scan.
    """
    ids = list(dict.fromkeys(str(item) for item in candidate_version_ids if item))
    if not ids:
        return []
    joins: list[str] = []
    clauses = [
        "v.id IN (" + ",".join("?" for _ in ids) + ")",
        "p.data_mode='real'",
        "COALESCE(p.condmat_view_eligible, 0)=1",
    ]
    params: list[Any] = list(ids)
    rank = "0.0"
    if query.strip():
        expression = fts_query(query)
        if not expression:
            return []
        joins.append("JOIN library_fts ON library_fts.paper_version_id=v.id")
        clauses.append("library_fts MATCH ?")
        params.append(expression)
        rank = "bm25(library_fts)"
    author = str(filters.get("author") or "").strip()
    if author:
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_authors pa JOIN authors a ON a.id=pa.author_id "
            "WHERE pa.paper_version_id=v.id AND a.normalized_name LIKE ?)"
        )
        params.append(f"%{author.lower()}%")
    topic = str(filters.get("topic") or "").strip()
    if topic:
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id "
            "WHERE pt.canonical_paper_id=v.canonical_paper_id AND lower(t.canonical_name) LIKE ?)"
        )
        params.append(f"%{topic.lower()}%")
    material = str(filters.get("material") or "").strip()
    if material:
        clauses.append(
            "EXISTS (SELECT 1 FROM paper_materials pm JOIN materials m ON m.id=pm.material_id "
            "WHERE pm.canonical_paper_id=v.canonical_paper_id AND lower(m.canonical_name) LIKE ?)"
        )
        params.append(f"%{material.lower()}%")
    journal = str(filters.get("journal") or "").strip()
    if journal:
        clauses.append("lower(COALESCE(NULLIF(v.journal, ''), p.journal, '')) LIKE ?")
        params.append(f"%{journal.lower()}%")
    if filters.get("year_from") is not None:
        clauses.append("p.year >= ?")
        params.append(filters["year_from"])
    if filters.get("year_to") is not None:
        clauses.append("p.year <= ?")
        params.append(filters["year_to"])
    if bool(filters.get("open_access_only")):
        clauses.append("p.is_open_access=1")
    canonical_filter = str(filters.get("canonical_paper_id") or "").strip()
    if canonical_filter:
        clauses.append("v.canonical_paper_id=?")
        params.append(canonical_filter)
    limit = min(max(int(filters.get("limit") or 100), 1), 500)
    fetch_limit = min(500, max(limit, limit * 5))
    rows = connection.execute(
        f"""
        SELECT v.id AS paper_version_id, v.canonical_paper_id, v.version_type,
               v.title, v.abstract, v.doi, v.arxiv_id, v.arxiv_version,
               v.journal, v.publication_date, v.source, v.url, v.pdf_url,
               p.year, p.is_open_access, p.cited_by_count, {rank} AS rank,
               EXISTS(SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=v.canonical_paper_id) AS downloaded
        FROM paper_versions v
        JOIN papers p ON p.id=v.canonical_paper_id
        {' '.join(joins)}
        WHERE {' AND '.join(clauses)}
        ORDER BY rank ASC, COALESCE(v.updated_date, v.publication_date, '') DESC,
                 COALESCE(v.last_seen_at, '') DESC
        LIMIT ?
        """,
        (*params, fetch_limit),
    ).fetchall()
    return _canonical_monitor_items([dict(row) for row in rows], limit)


def evaluate_monitors(
    connection: sqlite3.Connection,
    *,
    monitor_ids: list[str] | None = None,
    run_id: str | None = None,
    candidate_version_ids: list[str] | None = None,
) -> dict[str, int]:
    """Evaluate live monitor rules and persist their individual matching papers.

    A rule's `last_matched_at` is useful for a quick overview, but is not a
    history.  `monitor_hits` preserves the first match and subsequent sightings
    so the UI can distinguish new literature from a rule merely being rerun.
    """
    ensure_workbench_schema(connection)
    params: list[Any] = []
    sql = "SELECT * FROM monitor_queries WHERE enabled=1"
    if monitor_ids:
        ids = list(dict.fromkeys(str(item) for item in monitor_ids if item))
        if not ids:
            return {"monitors": 0, "matched": 0, "new_hits": 0, "download_tasks_enqueued": 0}
        sql += " AND id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    monitors = connection.execute(sql + " ORDER BY updated_at DESC", params).fetchall()
    matched = new_hits = enqueued = 0
    for monitor in monitors:
        try:
            filters = json.loads(monitor["filters_json"] or "{}")
        except json.JSONDecodeError:
            filters = {}
        monitor_query = _effective_monitor_query(
            str(monitor["query_text"] or ""), filters
        )
        monitor_limit = min(max(int(filters.get("limit") or 100), 1), 500)
        if candidate_version_ids is None:
            fetch_limit = min(500, max(monitor_limit, monitor_limit * 5))
            result = search_library(
                connection,
                monitor_query,
                author=str(filters.get("author") or ""),
                topic=str(filters.get("topic") or ""),
                material=str(filters.get("material") or ""),
                journal=str(filters.get("journal") or ""),
                canonical_paper_id=str(filters.get("canonical_paper_id") or ""),
                year_from=filters.get("year_from"),
                year_to=filters.get("year_to"),
                open_access_only=bool(filters.get("open_access_only")),
                condmat_only=True,
                limit=fetch_limit,
            )
            items = _canonical_monitor_items(
                list(result["items"]), monitor_limit
            )
        else:
            items = _search_monitor_candidates(
                connection,
                monitor_query,
                filters,
                candidate_version_ids,
            )
        now = utc_now()
        item_count = 0
        for item in items:
            version_id = str(item["paper_version_id"])
            canonical_id = str(item["canonical_paper_id"])
            hit_id = stable_id("monitor_hit", f"{monitor['id']}:{canonical_id}")
            existed = connection.execute(
                "SELECT id FROM monitor_hits "
                "WHERE monitor_id=? AND canonical_paper_id=? "
                "ORDER BY first_matched_at, id LIMIT 1",
                (monitor["id"], canonical_id),
            ).fetchone()
            payload = {
                "title": item.get("title") or "",
                "version_type": item.get("version_type") or "",
                "source": item.get("source") or "",
                "publication_date": item.get("publication_date") or "",
            }
            if existed:
                connection.execute(
                    """
                    UPDATE monitor_hits
                    SET last_matched_at=?, last_run_id=?, match_count=match_count+1,
                        payload_json=?
                    WHERE id=?
                    """,
                    (now, run_id, json.dumps(payload, ensure_ascii=False), existed["id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO monitor_hits
                    (id, monitor_id, canonical_paper_id, paper_version_id,
                     first_matched_at, last_matched_at, first_run_id, last_run_id,
                     match_count, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (hit_id, monitor["id"], canonical_id, version_id, now, now, run_id, run_id, json.dumps(payload, ensure_ascii=False)),
                )
                new_hits += 1
                if monitor["auto_download_oa"] and item.get("is_open_access"):
                    enqueue_download(connection, canonical_id, version_id)
                    enqueued += 1
            item_count += 1
        matched += item_count
        connection.execute(
            """
            UPDATE monitor_queries
            SET last_checked_at=?, last_matched_at=CASE WHEN ? > 0 THEN ? ELSE last_matched_at END,
                updated_at=?
            WHERE id=?
            """,
            (now, item_count, now, now, monitor["id"]),
        )
    return {"monitors": len(monitors), "matched": matched, "new_hits": new_hits, "download_tasks_enqueued": enqueued}


def run_daily_update(
    options: DailyOptions,
    *,
    database: Path | None = None,
    pdf_root: Path | None = None,
    run_id: str | None = None,
    trigger_type: str = "manual_live",
) -> dict[str, Any]:
    database = database or db_path()
    if options.dry_run:
        run_id = run_id or str(uuid.uuid4())
        connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            has_schema = connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_cursors'").fetchone()
            cursor = _discovery_cursor(connection) if has_schema else None
            date_from, date_to, window_policy, historical_days = _scan_window(options, cursor)
            return {
                "status": "PASS",
                "dry_run": True,
                "writes_performed": False,
                "run_id": run_id,
                "database": str(database),
                "date_from": date_from,
                "date_to": date_to,
                "scan_mode": options.scan_mode,
                "window_policy": window_policy,
                "historical_days": historical_days,
                "steps": ["ingest_sources", "audit_source_coverage", "backfill_missing_sources", "seed_versions", "incremental_search", "extract_materials", "backfill_abstracts", "evaluate_monitors", "download_oa"],
                "network_skipped": options.skip_network,
            }
        finally:
            connection.close()
    run_id = run_id or create_daily_run(options, database=database, trigger_type=trigger_type)
    lock = IngestLock(locks_dir() / "daily_update.lock")
    started = utc_now()
    try:
        lock.acquire(force=options.force_stale_lock, command=["run_daily_update", options.scan_mode])
        with connect(database) as connection:
            apply_unified_schema(connection)
            connection.execute(
                "UPDATE daily_runs SET status='running', started_at=?, trigger_type=? WHERE id=?",
                (started, trigger_type, run_id),
            )
            cursor = _discovery_cursor(connection)
            date_from, date_to, window_policy, historical_days = _scan_window(options, cursor)
            _progress(
                connection,
                run_id,
                "fetching_sources",
                10,
                scan_mode=options.scan_mode,
                date_from=date_from,
                date_to=date_to,
                window_policy=window_policy,
                historical_days=historical_days,
            )
        ingest_result: dict[str, Any]
        if options.skip_network:
            ingest_result = {"status": "skipped", "reason": "--skip-network", "fetched_count": 0, "kept_count": 0}
        else:
            live = options.scan_mode == "live"
            args = argparse.Namespace(
                baseline_from=date_from,
                baseline_to=date_to,
                # A live scan still covers the configured journal scope; using
                # the old arxiv_live pseudo-scope silently disabled OpenAlex.
                scope=options.scope,
                journals="",
                limit_per_journal=0,
                resume=True,
                force_refresh=False,
                dry_run=False,
                mailto=None,
                include_arxiv=options.include_arxiv,
                include_openalex_field=True,
                include_crossref=options.include_crossref,
                crossref_rows=1000,
                # Crossref occasionally needs longer for TLS/read completion
                # than the other live providers. Ingest commits before every
                # remote attempt, so this wait does not retain a SQLite writer.
                crossref_timeout=25 if live else 30,
                # Never sample the first page only: advance the watermark only
                # after every page in the date window has been consumed.
                max_pages=0,
                sleep_seconds=0.05 if live else 0.12,
                timeout=12 if live else 30,
                incremental=live,
                progress_run_id=run_id,
            )
            ingest_result = run_real_ingest(args)
        with connect(database) as connection:
            apply_unified_schema(connection)
            cancelled_result = _finalize_cancelled_run(connection, run_id, ingest_result)
            if cancelled_result is not None:
                return cancelled_result
            if not options.skip_network:
                status = str(ingest_result.get("status") or "failed")
                _record_cursor_attempt(
                    connection,
                    DISCOVERY_CURSOR_SOURCE,
                    status=status,
                    cursor=date_to,
                    error=json.dumps(ingest_result.get("errors") or [], ensure_ascii=False) if status in {"failed", "partial"} else None,
                    metadata=ingest_result,
                )
            _progress(connection, run_id, "auditing_source_coverage", 45, ingest=ingest_result)
            live_mode = options.scan_mode == "live"
            coverage_before = run_source_coverage_audit(
                connection,
                scope="eligible",
                window_from=date_from,
                window_to=date_to,
                queue_missing=not options.skip_network,
            )
            connection.commit()
            _progress(connection, run_id, "backfilling_missing_sources", 52, source_coverage=coverage_before)

            def publish_source_backfill_progress(
                processed: int,
                total: int,
                completed: int,
                not_found: int,
                failed: int,
                identity_conflicts: int,
                current_source: str,
            ) -> None:
                progress = {
                    "requested": total,
                    "total": total,
                    "processed": processed,
                    "completed": completed,
                    "not_found": not_found,
                    "failed": failed,
                    "identity_conflicts": identity_conflicts,
                    "current_source": current_source,
                    "queue_remaining": max(0, total - processed),
                }
                percent = 52 if total <= 0 else min(57, 52 + int((processed / total) * 5))
                _progress(
                    connection,
                    run_id,
                    "backfilling_missing_sources",
                    percent,
                    source_coverage=coverage_before,
                    source_backfill=progress,
                )

            source_backfill = (
                run_source_backfill(
                    connection,
                    limit=12 if live_mode else 100,
                    timeout=8 if live_mode else 20,
                    progress_callback=publish_source_backfill_progress,
                )
                if not options.skip_network
                else {
                    "requested": 0,
                    "total": 0,
                    "processed": 0,
                    "completed": 0,
                    "not_found": 0,
                    "failed": 0,
                    "identity_conflicts": 0,
                    "paper_version_ids": [],
                    "canonical_paper_ids": [],
                    "results": [],
                    "progress_callback_errors": [],
                    "skipped": True,
                }
            )
            connection.commit()
            cancelled_result = _finalize_cancelled_run(connection, run_id, ingest_result)
            if cancelled_result is not None:
                return cancelled_result
            coverage_after = run_source_coverage_audit(
                connection,
                scope="eligible",
                window_from=date_from,
                window_to=date_to,
                queue_missing=False,
            )
            _pipeline_stage_progress(
                connection,
                run_id,
                "seeding_library_versions",
                58,
                status="running",
                source_backfill=source_backfill,
                source_coverage=coverage_after,
            )
            seeded = seed_radar_versions(connection, updated_since=started if live_mode else None)
            seeded_progress = {
                key: value for key, value in seeded.items() if key != "version_ids"
            }
            seed_stage_result = {
                **seeded_progress,
                "transaction_mode": "atomic_bulk_sql",
                "progress_granularity": "stage_boundary_only",
            }
            _pipeline_stage_progress(
                connection,
                run_id,
                "seeding_library_versions",
                59,
                status="completed",
                result=seed_stage_result,
                seeded_versions=seeded_progress,
                source_backfill=source_backfill,
                source_coverage=coverage_after,
            )
            seeded_version_ids = list(seeded.pop("version_ids", []))
            seeded_version_ids.extend(source_backfill.get("paper_version_ids") or [])
            affected_paper_ids = ingest_result.get("affected_paper_ids")
            ingest_metadata_changed = ingest_result.get("metadata_changed_paper_ids")
            if not isinstance(ingest_metadata_changed, list):
                # Compatibility with older/custom ingest implementations.
                ingest_metadata_changed = affected_paper_ids if isinstance(affected_paper_ids, list) else []
            metadata_changed_ids = list(
                dict.fromkeys(
                    str(item)
                    for item in [
                        *ingest_metadata_changed,
                        *(source_backfill.get("canonical_paper_ids") or []),
                    ]
                    if item
                )
            )
            download_requeue = requeue_downloads_after_metadata_change(
                connection,
                metadata_changed_ids,
            )
            if live_mode and isinstance(affected_paper_ids, list) and affected_paper_ids:
                paper_ids = list(dict.fromkeys(str(item) for item in affected_paper_ids if item))[:500]
                placeholders = ",".join("?" for _ in paper_ids)
                source_versions = connection.execute(
                    f"SELECT id FROM paper_versions WHERE canonical_paper_id IN ({placeholders}) AND source <> 'radar_legacy'",
                    paper_ids,
                ).fetchall()
                seeded_version_ids.extend(str(row["id"]) for row in source_versions)
            seeded_version_ids = list(dict.fromkeys(seeded_version_ids))
            monitor_candidate_ids = seeded_version_ids
            if live_mode and isinstance(affected_paper_ids, list):
                affected = {str(item) for item in affected_paper_ids if item}
                monitor_candidate_ids = [
                    version_id for version_id in seeded_version_ids
                    if connection.execute("SELECT canonical_paper_id FROM paper_versions WHERE id=?", (version_id,)).fetchone()[0] in affected
                ] if affected else []
            if live_mode:
                linked = {
                    "status": "PASS",
                    "dry_run": False,
                    "deferred_to_full_refresh": True,
                    "links_created": 0,
                    "reviews_created": 0,
                }
                _progress(
                    connection,
                    run_id,
                    "indexing_new_papers",
                    60,
                    ingest=ingest_result,
                    seeded_versions=seeded_progress,
                    download_requeue=download_requeue,
                )
                refreshed = refresh_search_entries(connection, seeded_version_ids)
                indexed = {"indexed": int(refreshed["refreshed"]), **refreshed, "incremental": True}
            else:
                _pipeline_stage_progress(
                    connection,
                    run_id,
                    "linking_preprint_publications",
                    59,
                    status="running",
                    seeded_versions=seeded_progress,
                    download_requeue=download_requeue,
                )

                def publish_version_link_progress(
                    processed: int,
                    total: int,
                    links_seen: int,
                    reviews_seen: int,
                ) -> None:
                    progress = {
                        "processed": processed,
                        "total": total,
                        "links_seen": links_seen,
                        "reviews_seen": reviews_seen,
                        "batch_size": 100,
                    }
                    percent = 59 if total <= 0 else min(60, 59 + int(processed / total))
                    _pipeline_stage_progress(
                        connection,
                        run_id,
                        "linking_preprint_publications",
                        percent,
                        status="running",
                        result=progress,
                        commit=False,
                        seeded_versions=seeded_progress,
                        version_links=progress,
                        download_requeue=download_requeue,
                    )

                linked = link_preprints_and_publications(
                    connection,
                    dry_run=False,
                    batch_size=100,
                    progress_callback=publish_version_link_progress,
                    commit_batches=True,
                )
                _pipeline_stage_progress(
                    connection,
                    run_id,
                    "linking_preprint_publications",
                    60,
                    status="completed",
                    result=linked,
                    seeded_versions=seeded_progress,
                    version_links=linked,
                    download_requeue=download_requeue,
                )

                _pipeline_stage_progress(
                    connection,
                    run_id,
                    "rebuilding_search_index",
                    60,
                    status="running",
                    seeded_versions=seeded_progress,
                    version_links=linked,
                )
                # FTS replacement is intentionally one transaction. Committing
                # between DELETE and INSERT would expose an incomplete index.
                indexed = rebuild_search_index(connection)
                _pipeline_stage_progress(
                    connection,
                    run_id,
                    "rebuilding_search_index",
                    69,
                    status="completed",
                    result={
                        **indexed,
                        "transaction_mode": "atomic_full_rebuild",
                        "progress_granularity": "stage_boundary_only",
                        "reason": "fts_delete_insert_must_not_be_partially_visible",
                    },
                    seeded_versions=seeded_progress,
                    version_links=linked,
                    search_index=indexed,
                )
            _progress(connection, run_id, "backfilling_abstracts", 70, ingest=ingest_result, search_index=indexed)
            # Abstract backfill performs outbound requests. Commit all prior scan
            # writes first so dashboard/search reads are never blocked by TLS or
            # source timeouts. Entity extraction runs afterwards so a recovered
            # abstract becomes searchable and analyzable in this same run.
            connection.commit()

            def publish_abstract_progress(
                processed: int,
                total: int,
                enriched: int,
                not_found: int,
                failed: int,
                current_paper_id: str,
                current_source: str,
            ) -> None:
                progress = {
                    "requested": total,
                    "total": total,
                    "processed": processed,
                    "enriched": enriched,
                    "not_found": not_found,
                    "failed": failed,
                    "current_paper_id": current_paper_id,
                    "current_source": current_source,
                    "queue_remaining": max(0, total - processed),
                }
                percent = 70 if total <= 0 else min(78, 70 + int((processed / total) * 8))
                _progress(
                    connection,
                    run_id,
                    "backfilling_abstracts",
                    percent,
                    ingest=ingest_result,
                    search_index=indexed,
                    abstracts=progress,
                )

            abstracts = (
                backfill_missing_abstracts(
                    connection,
                    limit=options.abstract_backfill_limit,
                    timeout=4 if live_mode else 12,
                    progress_callback=publish_abstract_progress,
                )
                if options.abstract_backfill_limit
                else {
                    "requested": 0,
                    "total": 0,
                    "processed": 0,
                    "enriched": 0,
                    "not_found": 0,
                    "failed": 0,
                    "results": [],
                    "progress_callback_errors": [],
                }
            )
            connection.commit()
            cancelled_result = _finalize_cancelled_run(connection, run_id, ingest_result)
            if cancelled_result is not None:
                return cancelled_result

            affected_ids = [str(item) for item in (affected_paper_ids or []) if item]
            enriched_ids = [
                str(item.get("canonical_paper_id"))
                for item in abstracts.get("results") or []
                if isinstance(item, dict) and item.get("abstract_available") and item.get("canonical_paper_id")
            ]
            extraction_ids = list(dict.fromkeys([*affected_ids, *enriched_ids]))
            _pipeline_stage_progress(
                connection,
                run_id,
                "syncing_topics_materials",
                80,
                status="running",
                abstracts={"requested": abstracts["requested"], "enriched": abstracts["enriched"]},
                search_index=indexed,
            )
            if live_mode:
                extracted_terms = rebuild_paper_terms(connection, paper_ids=extraction_ids)
                topic_links = seed_topics_and_materials(connection, paper_ids=extraction_ids)
                materials = (
                    sync_materials_from_papers(connection, paper_ids=extraction_ids)
                    if extraction_ids
                    else {"papers_scanned": 0, "materials_discovered": 0, "links_created": 0, "links_updated": 0, "links_replaced": 0, "evidence_count": 0}
                )
                term_extraction = {"mode": "incremental", "papers_scanned": extracted_terms, "paper_ids": len(extraction_ids)}
            else:
                # ingest_real rebuilt the full corpus before abstract backfill.
                # Re-extract only papers enriched above so those new abstracts
                # are analyzable in this same run.
                post_backfill_terms = rebuild_paper_terms(connection, paper_ids=enriched_ids)
                topic_links = seed_topics_and_materials(connection)
                materials = sync_materials_from_papers(connection)
                term_extraction = {
                    "mode": "full_ingest_rebuild",
                    "papers_scanned": int(ingest_result.get("changed_count") or 0),
                    "post_backfill_papers": post_backfill_terms,
                }
            sync_result = {
                "mode": "incremental" if live_mode else "full",
                "term_extraction": term_extraction,
                "topic_links": topic_links,
                "materials": materials,
                "transaction_mode": "atomic_stage",
                "progress_granularity": "stage_boundary_only",
                "reason": "topic_and_material_derivatives_publish_together",
            }
            _pipeline_stage_progress(
                connection,
                run_id,
                "syncing_topics_materials",
                84,
                status="completed",
                result=sync_result,
                materials=materials,
                term_extraction=term_extraction,
                topic_links=topic_links,
                abstracts={"requested": abstracts["requested"], "enriched": abstracts["enriched"]},
            )
            _progress(
                connection,
                run_id,
                "evaluating_monitors",
                85,
                materials=materials,
                term_extraction=term_extraction,
                topic_links=topic_links,
                abstracts={"requested": abstracts["requested"], "enriched": abstracts["enriched"]},
            )
            monitors = evaluate_monitors(connection, run_id=run_id, candidate_version_ids=monitor_candidate_ids if live_mode else None)
            connection.commit()
            _progress(connection, run_id, "processing_downloads", 92, monitors=monitors)
            # Download resolution can also wait on remote OA services.
            connection.commit()
            downloads = run_download_queue(
                connection,
                pdf_root or (database.parent / "library" / "pdf"),
                limit=options.download_limit,
                unpaywall_email=unpaywall_email(),
            )
            connection.commit()
            cancelled_result = _finalize_cancelled_run(connection, run_id, ingest_result)
            if cancelled_result is not None:
                return cancelled_result
            progress_row = connection.execute(
                "SELECT report_json FROM daily_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            try:
                progress_report = json.loads(progress_row["report_json"] or "{}") if progress_row else {}
            except (json.JSONDecodeError, TypeError):
                progress_report = {}
            pipeline_stages = (
                progress_report.get("pipeline_stages", {})
                if isinstance(progress_report, dict)
                else {}
            )
            report = {
                "stage": "completed",
                "percent": 100,
                "scan_mode": options.scan_mode,
                "date_from": date_from,
                "date_to": date_to,
                "window_policy": window_policy,
                "historical_days": historical_days,
                "pipeline_stages": pipeline_stages,
                "ingest": ingest_result,
                "source_coverage_before": coverage_before,
                "source_backfill": source_backfill,
                "source_coverage_after": coverage_after,
                "download_requeue": download_requeue,
                "seeded_versions": seeded,
                "version_links": linked,
                "search_index": indexed,
                "term_extraction": term_extraction,
                "topic_links": topic_links,
                "materials": materials,
                "abstracts": abstracts,
                "monitors": monitors,
                "downloads": downloads,
            }
            final_status = (
                "partial"
                if ingest_result.get("status") in {"partial", "failed"}
                or int(source_backfill.get("failed") or 0) > 0
                or int(source_backfill.get("identity_conflicts") or 0) > 0
                else "completed"
            )
            completed_at = utc_now()
            cursor = connection.execute(
                """
                UPDATE daily_runs SET status=?, finished_at=?, report_json=?
                WHERE id=? AND status NOT IN ('cancel_requested', 'cancelled')
                """,
                (final_status, completed_at, json.dumps(report, ensure_ascii=False, default=str), run_id),
            )
            if not cursor.rowcount:
                cancelled_result = _finalize_cancelled_run(connection, run_id, ingest_result)
                if cancelled_result is not None:
                    return cancelled_result
        return {"status": "PASS" if final_status == "completed" else "PARTIAL", "dry_run": False, "run_id": run_id, "started_at": started, "finished_at": utc_now(), "report": report}
    except BaseException as exc:
        try:
            with connect(database) as connection:
                apply_unified_schema(connection)
                cancelled_result = _finalize_cancelled_run(
                    connection,
                    run_id,
                    locals().get("ingest_result"),
                )
                if cancelled_result is None:
                    progress_row = connection.execute(
                        "SELECT report_json FROM daily_runs WHERE id=?",
                        (run_id,),
                    ).fetchone()
                    try:
                        previous_progress = json.loads(progress_row["report_json"] or "{}") if progress_row else {}
                    except (json.JSONDecodeError, TypeError):
                        previous_progress = {}
                    failure = {
                        "stage": "failed",
                        "failed_stage": previous_progress.get("stage") if isinstance(previous_progress, dict) else None,
                        "percent": 100,
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                        "pipeline_stages": previous_progress.get("pipeline_stages", {}) if isinstance(previous_progress, dict) else {},
                    }
                    connection.execute(
                        """
                        UPDATE daily_runs SET status='failed', finished_at=?, error_message=?, report_json=?
                        WHERE id=? AND status NOT IN ('cancel_requested', 'cancelled')
                        """,
                        (utc_now(), failure["error"], json.dumps(failure, ensure_ascii=False), run_id),
                    )
        except Exception:
            pass
        raise
    finally:
        lock.release()
