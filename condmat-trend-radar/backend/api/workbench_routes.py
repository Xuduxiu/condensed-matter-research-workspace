from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.config import db_path, export_dir, institutional_access_mode, unpaywall_email
from backend.db.database import connect
from backend.downloader.audit import (
    candidate_attempts_for_task,
    download_audit_statistics,
    ensure_download_audit_schema,
)
from backend.downloader.citation_export import export_citation_package
from backend.downloader.queue import download_paper_now, enqueue_download, requeue_failed_downloads, run_download_queue
from backend.library.material_discovery import sync_materials_from_papers
from backend.llm.paper_analysis import (
    DeepSeekNotConfigured,
    DeepSeekRequestError,
    analyze_paper,
    deepseek_status,
    generate_radar_brief,
    get_latest_radar_brief,
)
from backend.library.metadata_enrichment import backfill_missing_abstracts, enrich_paper_metadata
from backend.library.remote_search import (
    import_payload,
    record_is_condmat,
    source_record_id,
    unified_remote_search,
)
from backend.library.repository import LibraryRepository, utc_now
from backend.library.workbench import (
    READING_STATUSES,
    add_papers_to_collection,
    delete_collection,
    ensure_workbench_schema,
    get_user_state,
    list_collections,
    record_action,
    remove_paper_from_collection,
    save_collection,
    set_user_state,
)
from backend.migrations.unified_library import apply_unified_schema


router = APIRouter(prefix="/api/library", tags=["radar-workbench"])

_ABSTRACT_RUNS: dict[str, dict[str, Any]] = {}
_ABSTRACT_RUNS_LOCK = threading.Lock()


class RemoteImportRequest(BaseModel):
    record: dict[str, Any]
    enqueue_download: bool = False
    favorite: bool = False
    reading_status: str | None = None


class UserStateRequest(BaseModel):
    favorite: bool | None = None
    reading_status: str | None = None
    note: str | None = Field(default=None, max_length=4000)


class CollectionRequest(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#56b6c2", max_length=20)


class CollectionPapersRequest(BaseModel):
    canonical_paper_ids: list[str] = Field(default_factory=list, max_length=500)


class QueueRunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)


class BulkRetryFailedRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=100)
    failure_classes: list[str] = Field(default_factory=list, max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=20)


class ImmediateDownloadRequest(BaseModel):
    paper_version_id: str | None = None
    zotero: bool = True


class BatchDownloadRequest(BaseModel):
    paper_version_ids: list[str] = Field(default_factory=list, min_length=1, max_length=100)


class PaperAnalysisRequest(BaseModel):
    tier: str = Field(default="fast", pattern="^(fast|pro)$")
    force: bool = False


class RadarBriefRequest(BaseModel):
    days: int = Field(default=7, ge=7, le=90)
    tier: str = Field(default="fast", pattern="^(fast|pro)$")
    force: bool = False
    limit: int = Field(default=20, ge=5, le=50)


class EnrichmentRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)


class MaterialSyncRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=200000)


def _require_paper(connection: Any, canonical_paper_id: str) -> None:
    if not connection.execute("SELECT 1 FROM papers WHERE id=?", (canonical_paper_id,)).fetchone():
        raise HTTPException(status_code=404, detail="paper not found")


def _index_version(connection: Any, paper_version_id: str) -> None:
    connection.execute("DELETE FROM library_fts WHERE paper_version_id=?", (paper_version_id,))
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
        FROM paper_versions v WHERE v.id=?
        """,
        (paper_version_id,),
    )


def _process_queue(limit: int) -> None:
    with connect() as connection:
        apply_unified_schema(connection)
        pdf_root = db_path().parent / "library" / "pdf"
        run_download_queue(connection, pdf_root, limit=limit, unpaywall_email=unpaywall_email())


def _run_abstract_enrichment(run_id: str, limit: int) -> None:
    """Run source-only abstract lookup with bounded item and batch latency."""

    def update(**values: Any) -> None:
        with _ABSTRACT_RUNS_LOCK:
            if run_id in _ABSTRACT_RUNS:
                _ABSTRACT_RUNS[run_id].update(values, updated_at=utc_now())

    update(status="running", started_at=utc_now())
    try:
        with connect() as connection:
            apply_unified_schema(connection)
            ensure_workbench_schema(connection)

            def publish_progress(
                processed: int,
                total: int,
                enriched: int,
                not_found: int,
                failed: int,
                current_paper_id: str,
                current_source: str,
            ) -> None:
                update(
                    total=total,
                    processed=processed,
                    enriched=enriched,
                    not_found=not_found,
                    failed=failed,
                    current_paper_id=current_paper_id,
                    current_source=current_source,
                )

            result = backfill_missing_abstracts(
                connection,
                limit=limit,
                timeout=5,
                item_timeout_seconds=20,
                batch_timeout_seconds=180,
                progress_callback=publish_progress,
            )
            record_action(
                connection,
                "abstract_backfill_run",
                "library",
                "abstracts",
                {
                    "run_id": run_id,
                    "requested": result["requested"],
                    "processed": result["processed"],
                    "enriched": result["enriched"],
                    "deferred": result["deferred"],
                },
            )
        update(
            status="partial" if result["budget_exhausted"] else "completed",
            total=result["total"],
            processed=result["processed"],
            enriched=result["enriched"],
            not_found=result["not_found"],
            failed=result["failed"],
            deferred=result["deferred"],
            current_paper_id="",
            current_source="",
            result=result,
            finished_at=utc_now(),
        )
    except Exception as exc:
        update(
            status="failed",
            error=f"{type(exc).__name__}: {str(exc)[:500]}",
            finished_at=utc_now(),
        )


def _zotero_urls(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    package = dict(result)
    package_id = str(package["package_id"])
    package["ris_download_url"] = f"/api/library/exports/zotero/{package_id}/{package['ris_filename']}"
    package["zip_download_url"] = f"/api/library/exports/zotero/{package_id}/{package['zip_filename']}"
    return package


def _run_bulk_download(run_id: str) -> None:
    """Run a user-requested batch immediately, then make one Zotero package.

    This intentionally bypasses the generic queue: the user pressed a concrete
    batch action and receives progress plus one linked export instead of an
    opaque pending task list.
    """
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        row = connection.execute("SELECT * FROM bulk_download_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return
        try:
            version_ids = list(dict.fromkeys(str(item) for item in json.loads(row["paper_version_ids_json"] or "[]") if item))
        except (TypeError, json.JSONDecodeError):
            version_ids = []
        connection.execute(
            "UPDATE bulk_download_runs SET status='running', started_at=?, error_message=NULL WHERE id=?",
            (utc_now(), run_id),
        )
        connection.commit()

    results: list[dict[str, Any]] = []
    completed = failed = 0
    for index, version_id in enumerate(version_ids, start=1):
        try:
            with connect() as connection:
                apply_unified_schema(connection)
                ensure_workbench_schema(connection)
                version = connection.execute(
                    "SELECT id, canonical_paper_id FROM paper_versions WHERE id=?", (version_id,)
                ).fetchone()
                if not version:
                    outcome: dict[str, Any] = {"paper_version_id": version_id, "status": "failed", "error": "paper version not found"}
                else:
                    download = download_paper_now(
                        connection,
                        str(version["canonical_paper_id"]),
                        str(version["id"]),
                        db_path().parent / "library" / "pdf",
                        unpaywall_email=unpaywall_email(),
                    )
                    status = str(download.get("status") or "failed")
                    outcome = {
                        "paper_version_id": version_id,
                        "canonical_paper_id": version["canonical_paper_id"],
                        "status": status,
                        "source": download.get("source"),
                        "failure_class": download.get("failure_class"),
                        "candidates_tried": download.get("candidates_tried"),
                        "error": download.get("error"),
                    }
                if outcome["status"] in {"completed", "already_available"}:
                    completed += 1
                else:
                    failed += 1
                results.append(outcome)
                connection.execute(
                    """
                    UPDATE bulk_download_runs
                    SET processed_count=?, completed_count=?, failed_count=?, result_json=?
                    WHERE id=?
                    """,
                    (index, completed, failed, json.dumps({"items": results}, ensure_ascii=False), run_id),
                )
        except Exception as exc:  # keep the rest of the selected papers running
            failed += 1
            results.append({"paper_version_id": version_id, "status": "failed", "error": str(exc)[:500]})
            with connect() as connection:
                ensure_workbench_schema(connection)
                connection.execute(
                    """
                    UPDATE bulk_download_runs
                    SET processed_count=?, completed_count=?, failed_count=?, result_json=?
                    WHERE id=?
                    """,
                    (index, completed, failed, json.dumps({"items": results}, ensure_ascii=False), run_id),
                )

    try:
        with connect() as connection:
            apply_unified_schema(connection)
            ensure_workbench_schema(connection)
            zotero = _zotero_urls(export_citation_package(connection, version_ids, export_dir() / "zotero")) if version_ids else None
            status = "completed" if failed == 0 else "partial" if completed else "failed"
            connection.execute(
                """
                UPDATE bulk_download_runs
                SET status=?, processed_count=?, completed_count=?, failed_count=?,
                    result_json=?, zotero_package_id=?, finished_at=?, error_message=?
                WHERE id=?
                """,
                (
                    status, len(version_ids), completed, failed,
                    json.dumps({"items": results, "zotero": zotero}, ensure_ascii=False),
                    zotero.get("package_id") if zotero else None, utc_now(),
                    None if zotero else "Zotero export was not created", run_id,
                ),
            )
    except Exception as exc:
        with connect() as connection:
            ensure_workbench_schema(connection)
            connection.execute(
                "UPDATE bulk_download_runs SET status='failed', finished_at=?, error_message=? WHERE id=?",
                (utc_now(), str(exc)[:500], run_id),
            )


@router.get("/remote-search")
def remote_search(
    q: str = Query(min_length=2, max_length=300),
    source: str = Query("all", pattern="^(all|openalex|crossref|arxiv)$"),
    limit: int = Query(30, ge=1, le=100),
) -> dict[str, Any]:
    requested = None if source == "all" else [source]
    with connect() as connection:
        apply_unified_schema(connection)
        return unified_remote_search(connection, q, sources=requested, limit=limit)


@router.post("/remote/import")
def import_remote(payload: RemoteImportRequest) -> dict[str, Any]:
    record = import_payload(payload.record)
    source = str(record.get("source") or "")
    if source not in {"openalex", "crossref", "arxiv"}:
        raise HTTPException(status_code=422, detail="unsupported remote source")
    if len(str(record.get("title") or "").strip()) < 3:
        raise HTTPException(status_code=422, detail="remote record title is required")
    if payload.reading_status is not None and payload.reading_status not in READING_STATUSES:
        raise HTTPException(status_code=422, detail="unsupported reading status")
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        repository = LibraryRepository(connection)
        source_id = source_record_id(record)
        result = repository.upsert_version(
            record,
            source=source,
            source_record_id=source_id,
            default_condmat_eligible=record_is_condmat(record),
            create_corpus_review=True,
        )
        _index_version(connection, result.paper_version_id)
        state = None
        if payload.favorite or payload.reading_status:
            state = set_user_state(
                connection,
                result.canonical_paper_id,
                favorite=payload.favorite,
                reading_status=payload.reading_status,
            )
        task = None
        if payload.enqueue_download:
            task = enqueue_download(
                connection,
                result.canonical_paper_id,
                result.paper_version_id,
                priority=20,
            )
        record_action(
            connection,
            "remote_paper_imported",
            "paper",
            result.canonical_paper_id,
            {"source": source, "source_record_id": source_id, "enqueue_download": payload.enqueue_download},
        )
        return {
            "status": "saved",
            "paper": result.as_dict(),
            "user_state": state,
            "download_task": task,
        }


@router.get("/workbench/status")
def workbench_status() -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        counts = {
            "favorites": int(connection.execute("SELECT COUNT(*) FROM paper_user_state WHERE favorite=1").fetchone()[0]),
            "reading_later": int(connection.execute("SELECT COUNT(*) FROM paper_user_state WHERE reading_status='later'").fetchone()[0]),
            "collections": int(connection.execute("SELECT COUNT(*) FROM paper_collections").fetchone()[0]),
            "collection_papers": int(connection.execute("SELECT COUNT(*) FROM collection_papers").fetchone()[0]),
            "actions": int(connection.execute("SELECT COUNT(*) FROM user_action_log").fetchone()[0]),
        }
        migration = connection.execute(
            "SELECT * FROM workbench_schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return {
            "installed": True,
            "migration": dict(migration),
            "counts": counts,
            "institutional_access": {"mode": institutional_access_mode(), "browser_cookies": False},
        }


@router.get("/user-state/{canonical_paper_id:path}")
def api_user_state(canonical_paper_id: str) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        _require_paper(connection, canonical_paper_id)
        return get_user_state(connection, canonical_paper_id)


@router.post("/user-state/{canonical_paper_id:path}")
def api_set_user_state(canonical_paper_id: str, payload: UserStateRequest) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        _require_paper(connection, canonical_paper_id)
        try:
            return set_user_state(
                connection,
                canonical_paper_id,
                favorite=payload.favorite,
                reading_status=payload.reading_status,
                note=payload.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/collections")
def api_collections() -> dict[str, Any]:
    with connect() as connection:
        return {"items": list_collections(connection)}


@router.post("/collections")
def api_save_collection(payload: CollectionRequest) -> dict[str, Any]:
    with connect() as connection:
        try:
            item = save_collection(
                connection,
                name=payload.name,
                description=payload.description,
                color=payload.color,
                collection_id=payload.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise HTTPException(status_code=409, detail="collection name already exists") from exc
            raise
        return item


@router.post("/collections/{collection_id}/delete")
def api_delete_collection(collection_id: str) -> dict[str, Any]:
    with connect() as connection:
        try:
            return delete_collection(connection, collection_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

@router.post("/collections/{collection_id}/papers")
def api_add_collection_papers(collection_id: str, payload: CollectionPapersRequest) -> dict[str, Any]:
    if not payload.canonical_paper_ids:
        raise HTTPException(status_code=422, detail="select at least one paper")
    with connect() as connection:
        try:
            return add_papers_to_collection(connection, collection_id, payload.canonical_paper_ids)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/collections/{collection_id}/remove")
def api_remove_collection_papers(collection_id: str, payload: CollectionPapersRequest) -> dict[str, Any]:
    with connect() as connection:
        removed = 0
        for paper_id in payload.canonical_paper_ids[:500]:
            removed += remove_paper_from_collection(connection, collection_id, paper_id)["removed"]
        return {"collection_id": collection_id, "removed": removed}


@router.get("/downloads/statistics")
def download_statistics() -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_download_audit_schema(connection)
        return download_audit_statistics(connection)


@router.get("/downloads/{task_id}/audit")
def download_task_audit(task_id: str) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_download_audit_schema(connection)
        task = connection.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="download task not found")
        attempts = candidate_attempts_for_task(connection, task_id)
        return {"task": dict(task), "candidate_attempts": attempts, "count": len(attempts)}


@router.post("/downloads/retry-failed")
def bulk_retry_failed_downloads(payload: BulkRetryFailedRequest) -> dict[str, Any]:
    """User-triggered bounded retry with optional failure/source filters."""
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        result = requeue_failed_downloads(
            connection,
            limit=payload.limit,
            failure_classes=payload.failure_classes,
            sources=payload.sources,
        )
        for task_id in result["task_ids"]:
            record_action(connection, "download_bulk_retried", "download_task", task_id)
        return {
            "status": "accepted",
            "requested_limit": payload.limit,
            **result,
        }


@router.post("/downloads/{task_id}/retry")
def retry_download(task_id: str) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        row = connection.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="download task not found")
        if row["status"] not in {"retryable_failed", "permanent_failed", "manual_review"}:
            raise HTTPException(status_code=409, detail="download task is not retryable")
        result = requeue_failed_downloads(connection, task_ids=[task_id], limit=1)
        if result["requeued"] != 1:
            raise HTTPException(status_code=409, detail="download task changed before retry")
        record_action(connection, "download_retried", "download_task", task_id)
        return dict(connection.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone())


@router.post("/downloads/run")
def run_downloads(payload: QueueRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    background_tasks.add_task(_process_queue, payload.limit)
    return {"status": "accepted", "limit": payload.limit, "background": True}


@router.get("/files/{file_id}/content")
def open_pdf(file_id: str) -> FileResponse:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT f.*, COALESCE(p.title, 'paper') AS paper_title
            FROM paper_files f
            LEFT JOIN papers p ON p.id=f.canonical_paper_id
            WHERE f.id=?
            """,
            (file_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="paper file not found")
        path = Path(str(row["absolute_path"])).expanduser().resolve()
        if path.suffix.casefold() != ".pdf" or not path.is_file():
            raise HTTPException(status_code=410, detail="local PDF is missing or invalid")
        if path.stat().st_size < 5:
            raise HTTPException(status_code=410, detail="local PDF is empty")
        title = re.sub(r"[^\w\-. ]+", "_", str(row["paper_title"]))[:120].strip() or "paper"
        filename = f"{title}.pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/ai/status")
def ai_status() -> dict[str, Any]:
    return deepseek_status()


@router.get("/ai/radar-brief")
def cached_radar_brief(days: int = Query(7, ge=7, le=90)) -> dict[str, Any]:
    """Read the newest cached brief without making any model request."""
    with connect() as connection:
        apply_unified_schema(connection)
        return get_latest_radar_brief(connection, days=days)


@router.post("/ai/radar-brief")
def create_radar_brief(payload: RadarBriefRequest) -> dict[str, Any]:
    """Generate a brief only after an explicit user request; never scheduled."""
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        try:
            result = generate_radar_brief(
                connection,
                days=payload.days,
                tier=payload.tier,
                force=payload.force,
                limit=payload.limit,
            )
        except DeepSeekNotConfigured as exc:
            raise HTTPException(
                status_code=409,
                detail="DeepSeek API 尚未配置。请在本机 CondMat Radar 配置文件中设置 DEEPSEEK_API_KEY 后重启程序。",
            ) from exc
        except DeepSeekRequestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record_action(
            connection,
            "deepseek_radar_brief_requested",
            "library",
            "radar-brief",
            {
                "days": payload.days,
                "tier": payload.tier,
                "limit": payload.limit,
                "force": payload.force,
                "available": result.get("available", False),
                "cached": result.get("cached", False),
                "model": result.get("model"),
            },
        )
        return result


@router.post("/ai/papers/{canonical_paper_id:path}/analyze")
def analyze_paper_with_deepseek(canonical_paper_id: str, payload: PaperAnalysisRequest) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        _require_paper(connection, canonical_paper_id)
        try:
            result = analyze_paper(connection, canonical_paper_id, tier=payload.tier, force=payload.force)
        except DeepSeekNotConfigured as exc:
            raise HTTPException(status_code=409, detail="DeepSeek API 尚未配置。请在本机 CondMat Radar 配置文件中设置 DEEPSEEK_API_KEY 后重启程序。") from exc
        except DeepSeekRequestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        record_action(connection, "deepseek_paper_analyzed", "paper", canonical_paper_id, {"tier": payload.tier, "cached": result["cached"], "model": result["model"]})
        return result

@router.post("/download-now/batch")
def download_now_batch(payload: BatchDownloadRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    version_ids = list(dict.fromkeys(str(item).strip() for item in payload.paper_version_ids if str(item).strip()))
    if not version_ids:
        raise HTTPException(status_code=422, detail="select at least one paper version")
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        run_id = f"bulk_download:{uuid.uuid4()}"
        now = utc_now()
        connection.execute(
            """
            INSERT INTO bulk_download_runs
            (id, status, total_count, paper_version_ids_json, created_at)
            VALUES (?, 'queued', ?, ?, ?)
            """,
            (run_id, len(version_ids), json.dumps(version_ids, ensure_ascii=False), now),
        )
        record_action(connection, "bulk_download_requested", "download_batch", run_id, {"paper_version_count": len(version_ids)})
    background_tasks.add_task(_run_bulk_download, run_id)
    return {"status": "accepted", "run_id": run_id, "total_count": len(version_ids), "background": True}


@router.get("/download-batches/{run_id}")
def download_batch_status(run_id: str) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        row = connection.execute("SELECT * FROM bulk_download_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="download batch not found")
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json") or "{}")
        except json.JSONDecodeError:
            result["result"] = {}
        return result

@router.post("/download-now/{canonical_paper_id:path}")
def download_now(canonical_paper_id: str, payload: ImmediateDownloadRequest) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        _require_paper(connection, canonical_paper_id)
        version_id = payload.paper_version_id
        if not version_id:
            row = connection.execute(
                "SELECT id FROM paper_versions WHERE canonical_paper_id=? ORDER BY COALESCE(publication_date, submitted_date, '') DESC LIMIT 1",
                (canonical_paper_id,),
            ).fetchone()
            version_id = str(row["id"]) if row else None
        if not version_id:
            raise HTTPException(status_code=422, detail="paper has no downloadable version")
        download = download_paper_now(
            connection,
            canonical_paper_id,
            version_id,
            db_path().parent / "library" / "pdf",
            unpaywall_email=unpaywall_email(),
        )
        zotero = export_citation_package(connection, [version_id], export_dir() / "zotero") if payload.zotero else None
        if zotero:
            package_id = str(zotero["package_id"])
            zotero["ris_download_url"] = f"/api/library/exports/zotero/{package_id}/{zotero['ris_filename']}"
            zotero["zip_download_url"] = f"/api/library/exports/zotero/{package_id}/{zotero['zip_filename']}"
        record_action(
            connection,
            "paper_download_now",
            "paper",
            canonical_paper_id,
            {"download_status": download.get("status"), "zotero_package": zotero.get("package_id") if zotero else None},
        )
        return {
            "status": "completed" if download.get("status") in {"completed", "already_available"} else "failed",
            "download": download,
            "zotero": zotero,
        }


@router.post("/enrich/{canonical_paper_id:path}")
def enrich_paper(canonical_paper_id: str) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        _require_paper(connection, canonical_paper_id)
        try:
            result = enrich_paper_metadata(connection, canonical_paper_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        record_action(connection, "paper_metadata_enriched", "paper", canonical_paper_id, result)
        return result


@router.post("/enrichment/abstracts/run", status_code=202)
def enrich_missing_abstracts(
    payload: EnrichmentRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    with _ABSTRACT_RUNS_LOCK:
        active = next(
            (
                item for item in reversed(tuple(_ABSTRACT_RUNS.values()))
                if item["status"] in {"queued", "running"}
            ),
            None,
        )
        if active:
            return {**active, "reused": True}
        run_id = f"abstract_enrichment:{uuid.uuid4()}"
        run = {
            "status": "queued",
            "run_id": run_id,
            "limit": payload.limit,
            "background": True,
            "reused": False,
            "total": 0,
            "processed": 0,
            "enriched": 0,
            "not_found": 0,
            "failed": 0,
            "deferred": 0,
            "current_paper_id": "",
            "current_source": "",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        _ABSTRACT_RUNS[run_id] = run
        while len(_ABSTRACT_RUNS) > 100:
            _ABSTRACT_RUNS.pop(next(iter(_ABSTRACT_RUNS)))
    background_tasks.add_task(_run_abstract_enrichment, run_id, payload.limit)
    return dict(run)


@router.get("/enrichment/abstracts/runs/{run_id}")
def abstract_enrichment_status(run_id: str) -> dict[str, Any]:
    with _ABSTRACT_RUNS_LOCK:
        run = _ABSTRACT_RUNS.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="abstract enrichment run not found")
        return dict(run)


@router.post("/materials/sync")
def sync_materials(payload: MaterialSyncRequest) -> dict[str, Any]:
    with connect() as connection:
        apply_unified_schema(connection)
        ensure_workbench_schema(connection)
        result = sync_materials_from_papers(connection, limit=payload.limit)
        record_action(connection, "paper_materials_synced", "library", "materials", result)
        return result


@router.get("/exports/zotero/{package_id}/{filename}")
def download_zotero_export(package_id: str, filename: str) -> FileResponse:
    if not re.fullmatch(r"zotero_import_[A-Za-z0-9]+", package_id):
        raise HTTPException(status_code=404, detail="export package not found")
    allowed = {"selected_papers.ris", "selected_papers.bib", "selected_papers.csv", "selected_summary.md", "manifest.json", "zotero_package.zip"}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="export file not found")
    root = (export_dir() / "zotero").resolve()
    path = (root / package_id / filename).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="export file not found")
    media_type = "application/zip" if path.suffix == ".zip" else "application/x-research-info-systems" if path.suffix == ".ris" else "application/octet-stream"
    return FileResponse(path, filename=filename, media_type=media_type, content_disposition_type="attachment")

@router.get("/actions")
def recent_actions(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    with connect() as connection:
        ensure_workbench_schema(connection)
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM user_action_log ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        ]
        return {"items": rows, "count": len(rows)}
