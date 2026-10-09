from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from backend.config import data_dir, db_path, export_dir
from backend.db.database import connect
from backend.ingest.source_coverage import (
    latest_source_coverage_audit,
    run_source_backfill,
    run_source_coverage_audit,
)
from backend.downloader.citation_export import export_citation_package
from backend.downloader.queue import enqueue_download
from backend.library.local_search import search_library
from backend.library.repository import stable_id, utc_now
from backend.migrations.unified_library import apply_unified_schema, unified_schema_status
from backend.scheduler.daily_update import DailyOptions, active_daily_run, create_daily_run, daily_run_status, evaluate_monitors, run_daily_update
from backend.library.workbench import ensure_workbench_schema
from backend.scheduler.live_scanner import live_scan_status, update_live_scan_settings


router = APIRouter(prefix="/api", tags=["unified-library"])


class DownloadRequest(BaseModel):
    canonical_paper_id: str
    paper_version_id: str | None = None
    priority: int = 0


class MonitorRequest(BaseModel):
    name: str
    query_text: str
    filters: dict[str, Any] = Field(default_factory=dict)
    auto_download_oa: bool = False
    enabled: bool = True


class ExportRequest(BaseModel):
    paper_version_ids: list[str] = Field(default_factory=list)


class MonitorSettingsRequest(BaseModel):
    enabled: bool | None = None
    auto_download_oa: bool | None = None


class ReviewResolution(BaseModel):
    status: str = "resolved"
    resolution: dict[str, Any] = Field(default_factory=dict)


class DailyRequest(BaseModel):
    apply: bool = False
    skip_network: bool = False
    download_limit: int = 10
    scan_mode: str = Field(default="live", pattern="^(live|full)$")
    full_lookback_days: int = Field(default=180, ge=30, le=3650)
    abstract_backfill_limit: int = Field(default=10, ge=0, le=25)


class LiveScanSettingsRequest(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=300, le=86400)
    abstract_backfill_limit: int | None = Field(default=None, ge=0, le=25)


class SourceBackfillRequest(BaseModel):
    apply: bool = False
    scope: str = Field(default="eligible", pattern="^(eligible|all)$")
    window_from: str | None = None
    window_to: str | None = None
    limit: int = Field(default=25, ge=0, le=500)
    timeout: int = Field(default=15, ge=3, le=60)


def _has_unified_schema(connection: sqlite3.Connection) -> bool:
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE name='unified_schema_migrations'").fetchone())


def _require_schema(connection: sqlite3.Connection) -> None:
    if not _has_unified_schema(connection):
        raise HTTPException(status_code=503, detail="Unified library schema is not installed; run migration dry-run and apply")


@router.get("/library/status")
def library_status() -> dict[str, Any]:
    uri = f"file:{db_path().resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        schema = unified_schema_status(connection)
        if not schema["installed"]:
            return {"installed": False, "database": str(db_path()), "schema": schema}
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("paper_versions", "paper_files", "download_tasks", "monitor_queries", "manual_review_items", "library_fts")
        }
        queue = {
            row["status"]: int(row["n"])
            for row in connection.execute("SELECT status, COUNT(*) AS n FROM download_tasks GROUP BY status")
        }
        return {"installed": True, "database": str(db_path()), "schema": schema, "counts": counts, "download_queue": queue}
    finally:
        connection.close()


@router.get("/library/coverage")
def api_source_coverage(
    refresh: bool = False,
    scope: str = Query("eligible", pattern="^(eligible|all)$"),
    window_from: str | None = None,
    window_to: str | None = None,
) -> dict[str, Any]:
    with connect() as connection:
        _require_schema(connection)
        if not refresh and not window_from and not window_to and scope == "eligible":
            latest = latest_source_coverage_audit(connection)
            if latest:
                return {"status": "cached", "audit": latest}
        report = run_source_coverage_audit(
            connection,
            scope=scope,
            window_from=window_from,
            window_to=window_to,
            queue_missing=False,
            persist=False,
            seed_historical=False,
        )
        return {"status": "computed", "writes_performed": False, "report": report}


@router.post("/library/coverage/backfill")
def api_source_coverage_backfill(payload: SourceBackfillRequest) -> dict[str, Any]:
    with connect() as connection:
        if payload.apply:
            apply_unified_schema(connection)
        else:
            _require_schema(connection)
        before = run_source_coverage_audit(
            connection,
            scope=payload.scope,
            window_from=payload.window_from,
            window_to=payload.window_to,
            queue_missing=payload.apply,
            persist=payload.apply,
            seed_historical=payload.apply,
        )
        if not payload.apply:
            return {
                "status": "dry_run",
                "writes_performed": False,
                "coverage": before,
                "would_process_at_most": payload.limit,
            }
        connection.commit()
        backfill = run_source_backfill(connection, limit=payload.limit, timeout=payload.timeout)
        after = run_source_coverage_audit(
            connection,
            scope=payload.scope,
            window_from=payload.window_from,
            window_to=payload.window_to,
            queue_missing=False,
        )
        return {"status": "completed", "writes_performed": True, "before": before, "backfill": backfill, "after": after}

@router.get("/library/search")
def api_library_search(
    q: str = "",
    author: str = "",
    topic: str = "",
    material: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    download_status: str = "",
    open_access_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with connect() as connection:
        _require_schema(connection)
        return search_library(
            connection,
            q,
            author=author,
            topic=topic,
            material=material,
            year_from=year_from,
            year_to=year_to,
            download_status=download_status,
            open_access_only=open_access_only,
            limit=limit,
            offset=offset,
        )


@router.post("/library/downloads")
def api_enqueue_download(payload: DownloadRequest) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        paper = connection.execute("SELECT 1 FROM papers WHERE id=?", (payload.canonical_paper_id,)).fetchone()
        if not paper:
            raise HTTPException(status_code=404, detail="paper not found")
        return enqueue_download(connection, payload.canonical_paper_id, payload.paper_version_id, priority=payload.priority)


@router.get("/library/downloads")
def api_download_queue(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    with connect() as connection:
        _require_schema(connection)
        rows = [dict(row) for row in connection.execute("SELECT * FROM download_tasks ORDER BY created_at DESC LIMIT ?", (limit,))]
        return {"items": rows, "count": len(rows)}


@router.post("/library/monitors")
def api_create_monitor(payload: MonitorRequest) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        now = utc_now()
        monitor_id = stable_id("monitor", f"{payload.name}:{payload.query_text}:{json.dumps(payload.filters, sort_keys=True)}")
        connection.execute(
            """
            INSERT INTO monitor_queries
            (id, name, query_text, filters_json, auto_download_oa, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, query_text=excluded.query_text,
              filters_json=excluded.filters_json,
              auto_download_oa=excluded.auto_download_oa,
              enabled=excluded.enabled, updated_at=excluded.updated_at
            """,
            (monitor_id, payload.name, payload.query_text, json.dumps(payload.filters, ensure_ascii=False), int(payload.auto_download_oa), int(payload.enabled), now, now),
        )
        return {"id": monitor_id, "status": "saved"}


@router.get("/library/monitors")
def api_monitors() -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        rows = [dict(row) for row in connection.execute(
            """
            SELECT m.*, COALESCE(h.total_hits, 0) AS total_hits,
                   COALESCE(h.today_new_hits, 0) AS today_new_hits
            FROM monitor_queries m
            LEFT JOIN (
              SELECT monitor_id, COUNT(*) AS total_hits,
                     SUM(CASE WHEN substr(first_matched_at, 1, 10)=date('now') THEN 1 ELSE 0 END) AS today_new_hits
              FROM (
                SELECT h.monitor_id, h.canonical_paper_id,
                       MIN(h.first_matched_at) AS first_matched_at
                FROM monitor_hits h
                JOIN papers p ON p.id=h.canonical_paper_id
                WHERE p.data_mode='real'
                  AND COALESCE(p.condmat_view_eligible, 0)=1
                GROUP BY h.monitor_id, h.canonical_paper_id
              ) canonical_hits
              GROUP BY monitor_id
            ) h ON h.monitor_id=m.id
            ORDER BY m.enabled DESC, m.updated_at DESC
            """
        )]
        total_hits = sum(int(row["total_hits"] or 0) for row in rows)
        today_new_hits = sum(int(row["today_new_hits"] or 0) for row in rows)
        return {"items": rows, "count": len(rows), "total_hits": total_hits, "today_new_hits": today_new_hits}


@router.get("/library/monitors/hits")
def api_monitor_hits(
    monitor_id: str = "",
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        clauses = [
            "p.data_mode='real'",
            "COALESCE(p.condmat_view_eligible, 0)=1",
        ]
        params: list[Any] = []
        if monitor_id:
            clauses.append("h.monitor_id=?")
            params.append(monitor_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = [dict(row) for row in connection.execute(
            f"""
            WITH ranked_hits AS (
              SELECT h.*, m.name AS monitor_name, v.title, v.version_type,
                     v.publication_date, v.source, p.journal, p.is_open_access,
                     ROW_NUMBER() OVER (
                       PARTITION BY h.monitor_id, h.canonical_paper_id
                       ORDER BY
                         CASE WHEN v.version_type IN ('publication','published') THEN 0 ELSE 1 END,
                         h.last_matched_at DESC, h.id
                     ) AS canonical_rank,
                     MIN(h.first_matched_at) OVER (
                       PARTITION BY h.monitor_id, h.canonical_paper_id
                     ) AS canonical_first_matched_at,
                     MAX(h.last_matched_at) OVER (
                       PARTITION BY h.monitor_id, h.canonical_paper_id
                     ) AS canonical_last_matched_at,
                     SUM(h.match_count) OVER (
                       PARTITION BY h.monitor_id, h.canonical_paper_id
                     ) AS canonical_match_count
              FROM monitor_hits h
              JOIN monitor_queries m ON m.id=h.monitor_id
              JOIN papers p ON p.id=h.canonical_paper_id
              LEFT JOIN paper_versions v ON v.id=h.paper_version_id
              {where}
            )
            SELECT id, monitor_id, canonical_paper_id, paper_version_id,
                   canonical_first_matched_at AS first_matched_at,
                   canonical_last_matched_at AS last_matched_at,
                   first_run_id, last_run_id,
                   canonical_match_count AS match_count, payload_json,
                   monitor_name, title, version_type, publication_date, source,
                   journal, is_open_access
            FROM ranked_hits
            WHERE canonical_rank=1
            ORDER BY first_matched_at DESC, last_matched_at DESC
            LIMIT ?
            """,
            (*params, limit),
        )]
        return {"items": rows, "count": len(rows), "monitor_id": monitor_id or None}


@router.post("/library/monitors/{monitor_id}/settings")
def api_update_monitor(monitor_id: str, payload: MonitorSettingsRequest) -> dict[str, Any]:
    if payload.enabled is None and payload.auto_download_oa is None:
        raise HTTPException(status_code=422, detail="provide at least one monitor setting")
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        current = connection.execute("SELECT * FROM monitor_queries WHERE id=?", (monitor_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="monitor not found")
        enabled = int(current["enabled"]) if payload.enabled is None else int(payload.enabled)
        auto_download = int(current["auto_download_oa"]) if payload.auto_download_oa is None else int(payload.auto_download_oa)
        connection.execute(
            "UPDATE monitor_queries SET enabled=?, auto_download_oa=?, updated_at=? WHERE id=?",
            (enabled, auto_download, utc_now(), monitor_id),
        )
        return dict(connection.execute("SELECT * FROM monitor_queries WHERE id=?", (monitor_id,)).fetchone())


@router.delete("/library/monitors/{monitor_id}")
def api_delete_monitor(monitor_id: str) -> dict[str, Any]:
    """Delete one saved rule and its derived hit history.

    Download tasks already created by this rule are intentionally retained: they
    are user-visible library work, not part of the monitor definition itself.
    """
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        monitor = connection.execute("SELECT id, name FROM monitor_queries WHERE id=?", (monitor_id,)).fetchone()
        if not monitor:
            raise HTTPException(status_code=404, detail="monitor not found")
        deleted_hits = int(connection.execute("DELETE FROM monitor_hits WHERE monitor_id=?", (monitor_id,)).rowcount)
        connection.execute("DELETE FROM monitor_queries WHERE id=?", (monitor_id,))
        return {"id": monitor_id, "name": str(monitor["name"]), "deleted_hits": deleted_hits, "status": "deleted"}

@router.post("/library/monitors/{monitor_id}/run")
def api_run_monitor(monitor_id: str) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        monitor = connection.execute("SELECT * FROM monitor_queries WHERE id=?", (monitor_id,)).fetchone()
        if not monitor:
            raise HTTPException(status_code=404, detail="monitor not found")
        if not monitor["enabled"]:
            raise HTTPException(status_code=409, detail="monitor is disabled; enable it before running")
        result = evaluate_monitors(connection, monitor_ids=[monitor_id])
        item = dict(connection.execute("SELECT * FROM monitor_queries WHERE id=?", (monitor_id,)).fetchone())
        return {"monitor": item, "result": result}

@router.get("/library/reviews")
def api_reviews(status: str = "pending", limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    with connect() as connection:
        _require_schema(connection)
        rows = [dict(row) for row in connection.execute("SELECT * FROM manual_review_items WHERE status=? ORDER BY created_at LIMIT ?", (status, limit))]
        return {"items": rows, "count": len(rows)}


@router.post("/library/reviews/{review_id}")
def api_resolve_review(review_id: str, payload: ReviewResolution) -> dict[str, Any]:
    if payload.status not in {"resolved", "rejected", "pending"}:
        raise HTTPException(status_code=422, detail="unsupported review status")
    with connect() as connection:
        _require_schema(connection)
        cursor = connection.execute(
            "UPDATE manual_review_items SET status=?, resolved_at=?, resolution_json=? WHERE id=?",
            (payload.status, utc_now() if payload.status != "pending" else None, json.dumps(payload.resolution, ensure_ascii=False), review_id),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="review not found")
        return {"id": review_id, "status": payload.status}


@router.post("/library/export/zotero")
def api_export_zotero(payload: ExportRequest) -> dict[str, Any]:
    if not payload.paper_version_ids:
        raise HTTPException(status_code=422, detail="select at least one paper version")
    with connect() as connection:
        _require_schema(connection)
        result = export_citation_package(connection, payload.paper_version_ids, export_dir() / "zotero")
        package_id = str(result["package_id"])
        result["ris_download_url"] = f"/api/library/exports/zotero/{package_id}/{result['ris_filename']}"
        result["zip_download_url"] = f"/api/library/exports/zotero/{package_id}/{result['zip_filename']}"
        return result


@router.get("/daily/status")
def api_daily_status(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    with connect() as connection:
        _require_schema(connection)
        runs = [dict(row) for row in connection.execute("SELECT * FROM daily_runs ORDER BY started_at DESC LIMIT ?", (limit,))]
        for run in runs:
            try:
                run["report"] = json.loads(run.get("report_json") or "{}")
            except json.JSONDecodeError:
                run["report"] = {}
        cursors = [dict(row) for row in connection.execute("SELECT * FROM source_cursors ORDER BY source_name")]
        return {"runs": runs, "source_cursors": cursors}


@router.get("/daily/runs/{run_id}")
def api_daily_run_status(run_id: str) -> dict[str, Any]:
    result = daily_run_status(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="scan run not found")
    return result


@router.get("/live-scan/status")
def api_live_scan_status() -> dict[str, Any]:
    return live_scan_status()


@router.post("/live-scan/settings")
def api_live_scan_settings(payload: LiveScanSettingsRequest) -> dict[str, Any]:
    return update_live_scan_settings(
        enabled=payload.enabled,
        interval_seconds=payload.interval_seconds,
        abstract_backfill_limit=payload.abstract_backfill_limit,
    )


@router.post("/daily/stop/{run_id}")
def api_daily_stop(run_id: str) -> dict[str, Any]:
    with connect() as connection:
        _require_schema(connection)
        cursor = connection.execute(
            "UPDATE daily_runs SET status='cancel_requested' WHERE id=? AND status='running'",
            (run_id,),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=409, detail="run is not active")
        return {"id": run_id, "status": "cancel_requested"}


@router.post("/daily/run")
def api_daily_run(payload: DailyRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    options = DailyOptions(
        dry_run=not payload.apply,
        skip_network=payload.skip_network,
        download_limit=payload.download_limit,
        scan_mode=payload.scan_mode,
        historical_days=payload.full_lookback_days,
        abstract_backfill_limit=payload.abstract_backfill_limit,
        force_stale_lock=bool(payload.apply),
    )
    if not payload.apply:
        return run_daily_update(options)
    active = active_daily_run()
    if active:
        return {"status": "already_running", "background": True, "run_id": active["id"], "run": active}
    run_id = create_daily_run(options, trigger_type="manual_live" if payload.scan_mode == "live" else "manual_full")
    background_tasks.add_task(
        run_daily_update,
        options,
        run_id=run_id,
        trigger_type="manual_live" if payload.scan_mode == "live" else "manual_full",
    )
    return {
        "status": "accepted",
        "background": True,
        "run_id": run_id,
        "scan_mode": payload.scan_mode,
        "full_lookback_days": payload.full_lookback_days if payload.scan_mode == "full" else None,
    }
