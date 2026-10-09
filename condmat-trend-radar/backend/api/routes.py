from __future__ import annotations

import argparse
import csv
import json
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import lifecycle_table
from backend.analytics.stats import (
    compare_data,
    compare_papers_query,
    concept_search,
    concept_series,
    data_status,
    download_tasks,
    heatmap,
    overview,
    paper_mode_predicate,
    papers_query,
)
from backend.db.database import connect, export_dir, export_query_to_csv, init_db
from backend.db.strict_condmat import DEFAULT_CORPUS_PRESET, normalize_corpus_preset, stats_mode_for_preset
from backend.ingest.task_store import create_task, existing_real_count, finished_existing_task, read_task, run_task
from backend.ingest_status import collect_ingest_status
from backend.integration.intake_status import summarize_intake_queue
from backend.integration.paper_downloader_bridge import export_to_downloader
from backend.integration.task_schema import TASK_FIELDS
from backend.nlp.normalize import normalize_term
from backend.update_all import run_update


router = APIRouter(prefix="/api")



class RealQuickstartRequest(BaseModel):
    target: int = 3000
    from_date: str = Field("2023-01-01", alias="from")
    to_date: str = Field("2026-07-10", alias="to")
    scope: str = "core"
    prefer: str = "openalex"
    fallback: str = "crossref_arxiv"
    strict_condmat: bool = False
    timeout: int = 20

    model_config = {"populate_by_name": True}
class ExportToDownloaderRequest(BaseModel):
    concepts: list[str] = []
    mode: str = "or"
    from_month: str = Field("2015-01", alias="from")
    to_month: str = Field("2026-07", alias="to")
    scope: str = "core"
    limit: int = 100
    min_momentum: float = 0.0
    journal: str = ""
    include_arxiv: bool = False
    output_dir: str | None = None
    dry_run: bool = False

    model_config = {"populate_by_name": True}


def canonical_concept(value: str) -> str:
    candidates = [value]
    try:
        candidates.append(value.encode("latin1").decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    for candidate in candidates:
        normalized = normalize_term(candidate)
        if normalized:
            return normalized
    return value


def parse_concepts(value: str) -> list[str]:
    return [canonical_concept(item.strip()) for item in value.split(",") if item.strip()]


def stats_mode_from_request(corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = "") -> str:
    explicit = (data_mode or "").strip().lower()
    if explicit and explicit != "auto":
        return explicit
    return stats_mode_for_preset(normalize_corpus_preset(corpus_preset))


@router.get("/data/status")
def api_data_status(scope: str = "core"):
    with connect() as conn:
        init_db(conn)
        return data_status(conn, scope=scope)


@router.get("/overview")
def api_overview(scope: str = "core", corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = "", from_month: str = Query("", alias="from"), to_month: str = Query("", alias="to")):
    with connect() as conn:
        init_db(conn)
        return overview(conn, scope=scope, trend_from=from_month or None, trend_to=to_month or None, data_mode=stats_mode_from_request(corpus_preset, data_mode))


@router.get("/heatmap")
def api_heatmap(
    metric: str = "momentum",
    journal: str = "all",
    scope: str = "core",
    concept_class: str = "all",
    exclude_methods: bool = False,
    exclude_platform_materials: bool = False,
    min_count: int = 2,
    corpus_preset: str = DEFAULT_CORPUS_PRESET,
    data_mode: str = "",
    from_month: str = Query("", alias="from"),
    to_month: str = Query("", alias="to"),
):
    with connect() as conn:
        init_db(conn)
        return heatmap(
            conn,
            metric=metric,
            journal=journal,
            scope=scope,
            from_month=from_month or None,
            to_month=to_month or None,
            concept_class=concept_class,
            exclude_methods=exclude_methods,
            exclude_platform_materials=exclude_platform_materials,
            min_count=min_count,
            data_mode=stats_mode_from_request(corpus_preset, data_mode),
        )

@router.get("/concepts/search")
def api_concepts_search(q: str = "", limit: int = 20):
    with connect() as conn:
        init_db(conn)
        return {"items": concept_search(conn, q, limit=limit)}


@router.get("/compare")
def api_compare(
    concepts: str,
    metric: str = "momentum",
    scope: str = "core",
    smoothing: str = "raw",
    corpus_preset: str = DEFAULT_CORPUS_PRESET,
    data_mode: str = "",
    from_month: str = Query("2015-01", alias="from"),
    to_month: str = Query("2026-07", alias="to"),
):
    with connect() as conn:
        init_db(conn)
        return compare_data(conn, parse_concepts(concepts), metric=metric, from_month=from_month, to_month=to_month, smoothing=smoothing, scope=scope, data_mode=stats_mode_from_request(corpus_preset, data_mode))

@router.get("/compare/papers")
def api_compare_papers(
    concepts: str,
    mode: str = "or",
    scope: str = "core",
    corpus_preset: str = DEFAULT_CORPUS_PRESET,
    data_mode: str = "",
    from_month: str = Query("2015-01", alias="from"),
    to_month: str = Query("2026-07", alias="to"),
):
    with connect() as conn:
        init_db(conn)
        return {"items": compare_papers_query(conn, parse_concepts(concepts), mode=mode, from_month=from_month, to_month=to_month, scope=scope, data_mode=stats_mode_from_request(corpus_preset, data_mode))}

@router.get("/concept/{concept}")
def api_concept(concept: str, smoothing: str = "raw", scope: str = "all", corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = "", from_month: str = Query("", alias="from"), to_month: str = Query("", alias="to")):
    concept_key = canonical_concept(concept)
    with connect() as conn:
        init_db(conn)
        lifecycle = conn.execute("SELECT * FROM concept_lifecycle WHERE concept = ?", (concept_key,)).fetchone()
        mode = stats_mode_from_request(corpus_preset, data_mode)
        series = concept_series(conn, concept_key, scope=scope, from_month=from_month or None, to_month=to_month or None, smoothing=smoothing, data_mode=mode)
        associated_materials = associated_terms(conn, concept_key, "material", mode)
        associated_methods = associated_terms(conn, concept_key, "method", mode)
        co_terms = cooccurring_terms(conn, concept_key, mode)
        mode_sql, mode_params = paper_mode_predicate(mode, "p")
        journals = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT p.journal, COUNT(DISTINCT p.id) AS count
                FROM papers p JOIN paper_terms pt ON pt.paper_id = p.id
                WHERE pt.normalized_term = ? AND {mode_sql}
                GROUP BY p.journal ORDER BY count DESC
                """,
                (concept_key, *mode_params),
            )
        ]
        return {
            "concept": concept_key,
            "series": series,
            "lifecycle": dict(lifecycle) if lifecycle else None,
            "associated_materials": associated_materials,
            "associated_methods": associated_methods,
            "cooccurring_concepts": co_terms,
            "journal_distribution": journals,
        }

@router.get("/concept/{concept}/papers")
def api_concept_papers(concept: str, corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = ""):
    with connect() as conn:
        init_db(conn)
        return {"items": papers_query(conn, concept=canonical_concept(concept), limit=100, scope="all", data_mode=stats_mode_from_request(corpus_preset, data_mode))}


@router.get("/lifecycle")
def api_lifecycle(corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = ""):
    with connect() as conn:
        init_db(conn)
        return lifecycle_table(conn)


@router.get("/cooccurrence")
def api_cooccurrence(min_count: int = 2, corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = ""):
    with connect() as conn:
        init_db(conn)
        return cooccurrence_network(conn, min_count=min_count, data_mode=stats_mode_from_request(corpus_preset, data_mode))


@router.get("/papers")
def api_papers(query: str = "", journal: str = "", concept: str = "", scope: str = "core", corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = "", from_date: str = Query("", alias="from"), to_date: str = Query("", alias="to"), limit: int = Query(50, ge=1, le=500)):
    concept_key = canonical_concept(concept) if concept else ""
    with connect() as conn:
        init_db(conn)
        return {"items": papers_query(conn, query=query, journal=journal, concept=concept_key, scope=scope, from_date=from_date, to_date=to_date, limit=limit, data_mode=stats_mode_from_request(corpus_preset, data_mode))}



@router.post("/ingest/real_quickstart")
def api_ingest_real_quickstart(payload: RealQuickstartRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    config = payload.model_dump(by_alias=True)
    real_count = existing_real_count()
    if real_count >= payload.target:
        return finished_existing_task(task_id, config, real_count)
    task = create_task(task_id, config)
    background_tasks.add_task(run_task, task_id, config)
    return task


@router.get("/ingest/status")
def api_ingest_status():
    return collect_ingest_status()

@router.get("/ingest/tasks/{task_id}")
def api_ingest_task(task_id: str):
    task = read_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task
@router.post("/integration/export_to_downloader")
def api_export_to_downloader(payload: ExportToDownloaderRequest):
    concepts = [canonical_concept(item) for item in payload.concepts if item.strip()]
    return export_to_downloader(
        concepts=concepts,
        mode=payload.mode,
        from_month=payload.from_month,
        to_month=payload.to_month,
        scope=payload.scope,
        min_momentum=payload.min_momentum,
        journal=payload.journal,
        limit=payload.limit,
        include_arxiv=payload.include_arxiv,
        output_dir=payload.output_dir,
        dry_run=payload.dry_run,
    )


@router.post("/update")
def api_update():
    args = argparse.Namespace(
        from_date=None,
        to_date=None,
        baseline_from="2015-01-01",
        baseline_to="2026-07-10",
        trend_from=None,
        trend_to="2026-07-10",
        trend_months=24,
        scope="core",
        max_results_per_source=120,
        force_refresh=False,
        skip_network=False,
        mock=False,
        mock_if_empty=True,
        include_arxiv=False,
        manual=[],
        per_page=None,
        max_pages=1,
        timeout=12,
    )
    return run_update(args)


@router.get("/export/terms.csv")
def export_terms_csv():
    path = export_dir() / "terms.csv"
    with connect() as conn:
        init_db(conn)
        export_query_to_csv(
            conn,
            """
            SELECT term, month, corpus_scope, data_mode, raw_freq, weighted_freq, normalized_share,
                   weighted_normalized_share, momentum, display_eligible, display_reason, journal_breakdown
            FROM term_month_stats ORDER BY term, month, corpus_scope, data_mode
            """,
            (),
            path,
        )
    return FileResponse(path, media_type="text/csv", filename="terms.csv")


@router.get("/export/papers.csv")
def export_papers_csv():
    path = export_dir() / "papers.csv"
    with connect() as conn:
        init_db(conn)
        export_query_to_csv(
            conn,
            """
            SELECT id, doi, title, journal, publication_date, month, source, data_mode, url, pdf_url,
                   is_open_access, oa_status, openalex_id, arxiv_id, cited_by_count
            FROM papers ORDER BY publication_date DESC
            """,
            (),
            path,
        )
    return FileResponse(path, media_type="text/csv", filename="papers.csv")


@router.get("/export/download_tasks.csv")
def export_download_tasks_csv(concept: str = "", scope: str = "all", corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = "", from_month: str = Query("2015-01", alias="from"), to_month: str = Query("2026-07", alias="to")):
    path = export_dir() / "download_tasks.csv"
    with connect() as conn:
        init_db(conn)
        rows = download_tasks(conn, concept=canonical_concept(concept) if concept else "", from_month=from_month, to_month=to_month, scope=scope, data_mode=stats_mode_from_request(corpus_preset, data_mode))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else TASK_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return FileResponse(path, media_type="text/csv", filename="download_tasks.csv")


@router.get("/export/download_tasks.json")
def export_download_tasks_json(concept: str = "", scope: str = "all", corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = "", from_month: str = Query("2015-01", alias="from"), to_month: str = Query("2026-07", alias="to")):
    path = export_dir() / "download_tasks.json"
    with connect() as conn:
        init_db(conn)
        rows = download_tasks(conn, concept=canonical_concept(concept) if concept else "", from_month=from_month, to_month=to_month, scope=scope, data_mode=stats_mode_from_request(corpus_preset, data_mode))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return FileResponse(path, media_type="application/json", filename="download_tasks.json")


def associated_terms(conn, concept: str, term_type: str, data_mode: str = "strict_condmat"):
    mode_sql, mode_params = paper_mode_predicate(data_mode, "p")
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT other.normalized_term AS term, COUNT(DISTINCT other.paper_id) AS count
            FROM paper_terms concept_term
            JOIN papers p ON p.id = concept_term.paper_id
            JOIN paper_terms other ON other.paper_id = concept_term.paper_id
            WHERE concept_term.normalized_term = ? AND other.term_type = ? AND other.normalized_term != ? AND {mode_sql}
            GROUP BY other.normalized_term ORDER BY count DESC, term ASC LIMIT 20
            """,
            (concept, term_type, concept, *mode_params),
        )
    ]


def cooccurring_terms(conn, concept: str, data_mode: str = "strict_condmat"):
    mode_sql, mode_params = paper_mode_predicate(data_mode, "p")
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT other.normalized_term AS term, COUNT(DISTINCT other.paper_id) AS count
            FROM paper_terms concept_term
            JOIN papers p ON p.id = concept_term.paper_id
            JOIN paper_terms other ON other.paper_id = concept_term.paper_id
            WHERE concept_term.normalized_term = ? AND other.term_type = 'concept' AND other.normalized_term != ? AND other.display_eligible = 1 AND {mode_sql}
            GROUP BY other.normalized_term ORDER BY count DESC, term ASC LIMIT 25
            """,
            (concept, concept, *mode_params),
        )
    ]
class DownloadCenterExportRequest(BaseModel):
    concept: str = ""
    corpus_preset: str = DEFAULT_CORPUS_PRESET
    scope: str = "all"
    from_month: str = Field("2015-01", alias="from")
    to_month: str = Field("2026-07", alias="to")
    limit: int = 250

    model_config = {"populate_by_name": True}


class LLMDryRunRequest(BaseModel):
    concept: str
    corpus_preset: str = DEFAULT_CORPUS_PRESET
    tier: str = "fast"


@router.get("/config/status")
def api_config_status():
    from backend.config_check import build_status

    return build_status()


@router.get("/published-preprint/top")
def api_published_preprint_top(limit: int = 20):
    from backend.analytics.published_preprint import top_published_preprint

    with connect() as conn:
        init_db(conn)
        return top_published_preprint(conn, limit=limit)


@router.get("/published-preprint/{concept}")
def api_published_preprint_concept(concept: str, from_month: str = Query("", alias="from"), to_month: str = Query("", alias="to")):
    from backend.analytics.published_preprint import published_preprint_series

    with connect() as conn:
        init_db(conn)
        return published_preprint_series(conn, canonical_concept(concept), from_month or None, to_month or None)


@router.get("/explainer/concept/{concept}")
def api_concept_explainer(concept: str, corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = ""):
    from backend.llm.evidence_builder import concept_evidence

    mode = stats_mode_from_request(corpus_preset, data_mode)
    with connect() as conn:
        init_db(conn)
        evidence = concept_evidence(conn, canonical_concept(concept), mode)
    evidence["llm_used"] = False
    evidence["claim_policy"] = "local_metadata_only"
    return evidence


@router.get("/download-center/status")
def api_download_center_status():
    return summarize_intake_queue()


@router.post("/download-center/export")
def api_download_center_export(payload: DownloadCenterExportRequest):
    from datetime import datetime, timezone
    from backend.config import paper_downloader_inbox

    mode = stats_mode_from_request(payload.corpus_preset, "")
    concept_key = canonical_concept(payload.concept) if payload.concept else ""
    with connect() as conn:
        init_db(conn)
        rows = download_tasks(
            conn,
            concept=concept_key,
            from_month=payload.from_month,
            to_month=payload.to_month,
            scope=payload.scope,
            limit=payload.limit,
            data_mode=mode,
        )
    directory = paper_downloader_inbox()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = directory / f"download_tasks_{stamp}.csv"
    json_path = directory / f"download_tasks_{stamp}.json"
    manifest_path = directory / f"download_tasks_{stamp}.manifest.json"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else TASK_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "created_at": stamp,
        "task_count": len(rows),
        "corpus_preset": payload.corpus_preset,
        "data_mode": mode,
        "concept": concept_key,
        "pdf_download_started": False,
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "task_count": len(rows), "csv_path": str(csv_path), "json_path": str(json_path), "manifest_path": str(manifest_path), "pdf_download_started": False}


@router.get("/cooccurrence/ego")
def api_cooccurrence_ego(concept: str, top_n_nodes: int = 20, min_edge_weight: int = 1, corpus_preset: str = DEFAULT_CORPUS_PRESET, data_mode: str = ""):
    from backend.analytics.cooccurrence import ego_cooccurrence_network

    with connect() as conn:
        init_db(conn)
        return ego_cooccurrence_network(conn, canonical_concept(concept), top_n_nodes=top_n_nodes, min_edge_weight=min_edge_weight, data_mode=stats_mode_from_request(corpus_preset, data_mode))


@router.post("/review/build")
def api_review_build(payload: LLMDryRunRequest):
    from backend.llm.evidence_builder import concept_evidence
    from backend.llm.provider import DeepSeekProvider

    mode = stats_mode_from_request(payload.corpus_preset, "")
    with connect() as conn:
        init_db(conn)
        evidence = concept_evidence(conn, canonical_concept(payload.concept), mode)
    return DeepSeekProvider.from_config(payload.tier).build_dry_run_response("mini_review", evidence)


@router.post("/ideas")
def api_ideas(payload: LLMDryRunRequest):
    from backend.llm.evidence_builder import concept_evidence
    from backend.llm.provider import DeepSeekProvider

    mode = stats_mode_from_request(payload.corpus_preset, "")
    with connect() as conn:
        init_db(conn)
        evidence = concept_evidence(conn, canonical_concept(payload.concept), mode)
    return DeepSeekProvider.from_config(payload.tier).build_dry_run_response("ideas", evidence)


@router.post("/skeleton")
def api_skeleton(payload: LLMDryRunRequest):
    from backend.llm.evidence_builder import concept_evidence
    from backend.llm.provider import DeepSeekProvider

    mode = stats_mode_from_request(payload.corpus_preset, "")
    with connect() as conn:
        init_db(conn)
        evidence = concept_evidence(conn, canonical_concept(payload.concept), mode)
    return DeepSeekProvider.from_config(payload.tier).build_dry_run_response("paper_skeleton", evidence)
