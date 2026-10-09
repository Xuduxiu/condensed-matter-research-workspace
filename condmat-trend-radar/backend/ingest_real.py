from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats
from backend.config import ensure_data_layout, logs_dir
from backend.db.database import clear_data_mode, connect, init_db, normalized_title_key, upsert_paper, utc_now
from backend.ingest.arxiv_client import ArxivClient
from backend.ingest.crossref_client import CrossrefClient
from backend.ingest.journal_registry import find_journal
from backend.ingest.openalex_client import OpenAlexClient, OpenAlexHTTPError
from backend.ingest.openalex_quality import (
    openalex_source_quality,
    reclassify_openalex_repository_quality,
    source_quality_reason,
)
from backend.library.deduplication import normalize_doi, parse_arxiv_id
from backend.library.repository import LibraryRepository
from backend.migrations.unified_library import apply_unified_schema
from backend.nlp.condmat_filter import is_condensed_matter_record
from backend.nlp.dictionaries import CONTEXT_JOURNALS, CORE_JOURNALS, GENERALIST_JOURNALS


CROSSREF_COMPLETE_STOP_REASONS = {"items_empty", "short_page", "no_next_cursor"}
INGEST_WRITE_BATCH_SIZE = 25


class IngestCancellationRequested(RuntimeError):
    """Internal control flow for a user-requested, transaction-safe stop."""

    def __init__(self, boundary: str) -> None:
        super().__init__(f"ingest cancellation requested at {boundary}")
        self.boundary = boundary


def _cancel_requested(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    commit_pending: bool = False,
) -> bool:
    """Return whether the owning daily run requested cancellation.

    Standalone CLI invocations do not carry ``progress_run_id`` and keep their
    previous behavior.  At a remote-page boundary we commit idempotent writes
    before reading the flag, both releasing SQLite's writer lock and refreshing
    the connection's view of the row changed by the stop API.
    """
    progress_run_id = str(getattr(args, "progress_run_id", "") or "").strip()
    if not progress_run_id or bool(getattr(args, "dry_run", False)):
        return False
    if commit_pending:
        conn.commit()
    row = conn.execute(
        "SELECT status FROM daily_runs WHERE id=?",
        (progress_run_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        status = row["status"]
    except (IndexError, KeyError, TypeError):
        status = row[0]
    return str(status or "") == "cancel_requested"


def _raise_if_cancel_requested(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    boundary: str,
    *,
    commit_pending: bool = False,
) -> None:
    if _cancel_requested(conn, args, commit_pending=commit_pending):
        raise IngestCancellationRequested(boundary)


def _publish_source_progress(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    source: str,
    source_label: str,
    page: int,
    fetched: int,
    percent: int,
    inserted: int | None = None,
    updated: int = 0,
    deduped: int = 0,
    eligible: int = 0,
    review_candidates: int = 0,
    kept: int | None = None,
) -> bool:
    """Publish a short, transaction-local snapshot for the owning daily run.

    The CLI does not provide ``progress_run_id`` and therefore remains a no-op.
    Callers invoke this immediately before an existing page checkpoint commit,
    so progress reporting never owns a long-lived SQLite writer transaction.
    """
    progress_run_id = str(getattr(args, "progress_run_id", "") or "").strip()
    if not progress_run_id or bool(getattr(args, "dry_run", False)):
        return False
    row = conn.execute(
        "SELECT report_json FROM daily_runs WHERE id=?",
        (progress_run_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        report = json.loads(row["report_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        report = {}
    if not isinstance(report, dict):
        report = {}
    # ``kept`` used to be the only stored-result counter. In this ingest path
    # it has always meant newly inserted rows, not records passing the
    # condensed-matter gate. Keep it as an explicit legacy alias while
    # publishing the real counters separately.
    inserted_count = max(0, int(kept or 0) if inserted is None else int(inserted))
    counts = {
        "fetched": max(0, int(fetched)),
        "eligible": max(0, int(eligible)),
        "review_candidates": max(0, int(review_candidates)),
        "inserted": inserted_count,
        "updated": max(0, int(updated)),
        "deduped": max(0, int(deduped)),
    }
    report.update(
        {
            "stage": "fetching_sources",
            "percent": max(10, min(40, int(percent))),
            "source": source,
            "source_label": source_label,
            "page": max(0, int(page)),
            **counts,
            "counts": counts,
            "kept": inserted_count,
            "kept_semantics": "legacy_alias_of_inserted",
            "count_semantics": {
                "fetched": "remote source records received before local filtering",
                "eligible": "source records passing the current inclusion gate; not cross-source unique papers",
                "review_candidates": "source records retained for review but not eligible for the condensed-matter view",
                "inserted": "new canonical papers inserted in this run",
                "updated": "existing canonical papers with meaningful metadata or version changes",
                "deduped": "source records already seen or stored without meaningful changes",
                "kept": "legacy alias of inserted; not an eligibility or strict-sample count",
            },
            "updated_at": utc_now(),
        }
    )
    conn.execute(
        "UPDATE daily_runs SET report_json=? WHERE id=?",
        (json.dumps(report, ensure_ascii=False, default=str), progress_run_id),
    )
    return True


POSTPROCESS_STAGE_LABELS = {
    "quality_reclassification": "本地后处理：来源质量重分类",
    "entity_extraction": "本地后处理：论文实体抽取",
    "term_statistics": "本地后处理：术语统计重建",
    "lifecycle_analysis": "本地后处理：研究生命周期分析",
    "cooccurrence_network": "本地后处理：概念共现网络",
}


def _publish_postprocess_progress(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    stage: str,
    percent: int,
    status: str,
    result: dict[str, Any] | None = None,
) -> bool:
    """Publish one bounded post-processing stage without committing it.

    The caller commits only immediately before a stage starts or after that
    stage has completed successfully. Destructive rebuilds therefore remain
    atomic: no progress checkpoint is written between their DELETE and INSERT
    operations.
    """
    progress_run_id = str(getattr(args, "progress_run_id", "") or "").strip()
    if not progress_run_id or bool(getattr(args, "dry_run", False)):
        return False
    row = conn.execute(
        "SELECT report_json FROM daily_runs WHERE id=?",
        (progress_run_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        report = json.loads(row["report_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        report = {}
    if not isinstance(report, dict):
        report = {}

    now = utc_now()
    existing = report.get("postprocess")
    postprocess = dict(existing) if isinstance(existing, dict) else {}
    existing_stages = postprocess.get("stages")
    stages = dict(existing_stages) if isinstance(existing_stages, dict) else {}
    existing_stage = stages.get(stage)
    stage_state = dict(existing_stage) if isinstance(existing_stage, dict) else {}
    if status == "running":
        stage_state.setdefault("started_at", now)
        stage_state.pop("completed_at", None)
    elif status in {"completed", "skipped"}:
        stage_state.setdefault("started_at", now)
        stage_state["completed_at"] = now
    stage_state["status"] = status
    if result is not None:
        stage_state["result"] = result
    stages[stage] = stage_state

    completed = [
        str(item)
        for item in postprocess.get("completed_stages", [])
        if item
    ]
    if status in {"completed", "skipped"} and stage not in completed:
        completed.append(stage)
    postprocess.update(
        {
            "current_stage": stage,
            "current_status": status,
            "completed_stages": completed,
            "stages": stages,
            "updated_at": now,
        }
    )
    report.update(
        {
            "stage": stage,
            "percent": max(0, min(100, int(percent))),
            "source": "local_postprocess",
            "source_label": POSTPROCESS_STAGE_LABELS.get(stage, stage),
            "page": 0,
            "postprocess": postprocess,
            "updated_at": now,
        }
    )
    conn.execute(
        "UPDATE daily_runs SET report_json=? WHERE id=?",
        (json.dumps(report, ensure_ascii=False, default=str), progress_run_id),
    )
    return True


def _checkpoint_postprocess_progress(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    *,
    stage: str,
    percent: int,
    status: str,
    result: dict[str, Any] | None = None,
) -> bool:
    """Commit a visible checkpoint only at a safe stage boundary."""
    published = _publish_postprocess_progress(
        conn,
        args,
        stage=stage,
        percent=percent,
        status=status,
        result=result,
    )
    if published:
        conn.commit()
    return published


def _progress_count_snapshot(
    *,
    fetched: int,
    inserted: int,
    updated: int,
    deduped: int,
    source_counts: dict[str, dict[str, int]],
) -> dict[str, int]:
    """Build the cumulative counters shared by live and final reports."""
    return {
        "fetched": max(0, int(fetched)),
        "inserted": max(0, int(inserted)),
        "updated": max(0, int(updated)),
        "deduped": max(0, int(deduped)),
        "eligible": sum(max(0, int(item.get("eligible") or 0)) for item in source_counts.values()),
        "review_candidates": sum(
            max(0, int(item.get("review_candidates") or 0)) for item in source_counts.values()
        ),
    }


def _journal_progress_percent(
    *,
    base: int,
    span: int,
    journal_index: int,
    journal_count: int,
    page: int,
) -> int:
    """Return best-effort progress when a provider does not expose page totals."""
    total = max(1, int(journal_count))
    within_journal = min(max(int(page), 0), 5) / 6
    progress = (max(0, int(journal_index)) + within_journal) / total
    return max(base, min(base + span, base + int(span * progress)))


def _commit_ingest_batch(
    conn: sqlite3.Connection,
    records_since_commit: int,
    *,
    dry_run: bool,
    batch_size: int = INGEST_WRITE_BATCH_SIZE,
) -> int:
    """Release SQLite's writer lock during large provider pages.

    Provider cursors are checkpointed only after their page is complete. It is
    safe to commit canonical/source-version upserts earlier because replay is
    idempotent. This prevents a 1,000-record Crossref page from blocking UI
    settings and download actions for tens of seconds on a large database.
    """
    if dry_run:
        return records_since_commit
    pending = records_since_commit + 1
    if pending >= max(1, int(batch_size)):
        conn.commit()
        return 0
    return pending


def journals_for_scope(scope: str) -> list[str]:
    if scope == "core":
        return list(CORE_JOURNALS)
    return list(CORE_JOURNALS + CONTEXT_JOURNALS)


def parse_journals(value: str, scope: str) -> list[str]:
    if value:
        return [item.strip() for item in value.split(",") if item.strip()]
    if scope == "arxiv_live":
        return []
    return journals_for_scope(scope)


def _safe_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _existing_paper(conn, paper: dict[str, Any]):
    doi = normalize_doi(paper.get("doi"))
    if doi:
        row = conn.execute("SELECT * FROM papers WHERE doi=? AND doi IS NOT NULL AND doi<>''", (doi,)).fetchone()
        if row:
            return row
    openalex_id = str(paper.get("openalex_id") or "").strip()
    if openalex_id:
        row = conn.execute("SELECT * FROM papers WHERE openalex_id=?", (openalex_id,)).fetchone()
        if row:
            return row
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    if arxiv_id:
        row = conn.execute(
            "SELECT * FROM papers WHERE arxiv_id=? OR arxiv_id GLOB ? ORDER BY updated_at DESC LIMIT 1",
            (arxiv_id, f"{arxiv_id}v[0-9]*"),
        ).fetchone()
        if row:
            return row
    paper_id = str(paper.get("id") or "").strip()
    if paper_id:
        return conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    return None


def _paper_changed(existing, paper: dict[str, Any]) -> bool:
    if existing is None:
        return True
    incoming = {
        "doi": normalize_doi(paper.get("doi")) or "",
        "title": " ".join(str(paper.get("title") or "").split()),
        "abstract": " ".join(str(paper.get("abstract") or "").split()),
        "journal": " ".join(str(paper.get("journal") or "").split()),
        "publication_date": str(paper.get("publication_date") or paper.get("submitted_date") or ""),
        "source": str(paper.get("source") or ""),
        "url": str(paper.get("url") or paper.get("doi_url") or ""),
        "pdf_url": str(paper.get("pdf_url") or ""),
        "oa_status": str(paper.get("oa_status") or ""),
        "openalex_id": str(paper.get("openalex_id") or ""),
        "arxiv_id": str(paper.get("arxiv_id") or ""),
    }
    for field, value in incoming.items():
        if value and " ".join(str(existing[field] or "").split()) != value:
            return True
    if paper.get("is_open_access") and not bool(existing["is_open_access"]):
        return True
    raw = paper.get("raw_json", paper)
    return _safe_json(raw) != _safe_json(existing["raw_json"] or {})


def _source_version_changed(existing, paper: dict[str, Any]) -> bool:
    if existing is None:
        return True
    incoming = {
        "doi": normalize_doi(paper.get("doi")) or "",
        "title": " ".join(str(paper.get("title") or "").split()),
        "abstract": " ".join(str(paper.get("abstract") or "").split()),
        "journal": " ".join(str(paper.get("journal") or "").split()),
        "publication_date": str(paper.get("publication_date") or ""),
        "submitted_date": str(paper.get("submitted_date") or ""),
        "updated_date": str(paper.get("updated_date") or ""),
        "source": str(paper.get("source") or "").strip().lower(),
        "url": str(paper.get("url") or paper.get("doi_url") or ""),
        "pdf_url": str(paper.get("pdf_url") or ""),
        "arxiv_id": parse_arxiv_id(paper.get("arxiv_id"))[0] or "",
    }
    for field, value in incoming.items():
        if value and " ".join(str(existing[field] or "").split()) != value:
            return True
    raw = paper.get("raw_json", paper)
    return _safe_json(raw) != _safe_json(existing["raw_json"] or {})

def _source_record_id(paper: dict[str, Any]) -> str:
    source = str(paper.get("source") or "unknown").strip().lower()
    if source == "openalex" and paper.get("openalex_id"):
        return str(paper["openalex_id"]).strip().lower()
    if source == "arxiv" and paper.get("arxiv_id"):
        arxiv_id, parsed_version = parse_arxiv_id(paper.get("arxiv_id"))
        version = paper.get("arxiv_version") or parsed_version
        return f"{arxiv_id}v{version}" if arxiv_id and version else str(arxiv_id or paper["arxiv_id"]).lower()
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return doi
    if paper.get("id"):
        return str(paper["id"]).strip().lower()
    title_key = normalized_title_key(str(paper.get("title") or ""))
    published = str(paper.get("publication_date") or paper.get("submitted_date") or "")[:10]
    return f"title:{title_key}:{published}"


def _store_paper(conn, paper: dict[str, Any], *, dry_run: bool) -> tuple[str, str]:
    existing = _existing_paper(conn, paper)
    if dry_run:
        return ("updated" if existing is not None else "inserted"), str(existing["id"] if existing is not None else paper.get("id") or "")

    source = str(paper.get("source") or "unknown").strip().lower()
    source_record_id = _source_record_id(paper)
    existing_version = conn.execute(
        "SELECT * FROM paper_versions WHERE source=? AND source_record_id=?",
        (source, source_record_id),
    ).fetchone()
    version_changed = _source_version_changed(existing_version, paper)
    eligibility_upgrade = bool(paper.get("condmat_view_eligible")) and existing is not None and not bool(existing["condmat_view_eligible"])
    if existing_version is not None and not version_changed and not eligibility_upgrade:
        # Avoid rewriting the identical source version and its observation row.
        # The exact source identity already points at the canonical paper.
        return "unchanged", str(existing_version["canonical_paper_id"])

    repository = LibraryRepository(conn)
    version_result = repository.upsert_version(
        paper,
        source=source,
        source_record_id=source_record_id,
        default_condmat_eligible=bool(paper.get("condmat_view_eligible")),
        create_corpus_review=False,
    )
    paper["id"] = version_result.canonical_paper_id
    upsert_paper(conn, paper)
    action = "inserted" if version_result.action == "created_canonical" else "updated"
    return action, version_result.canonical_paper_id


def _openalex_subfield_id(topic: Any) -> str:
    if not isinstance(topic, dict):
        return ""
    subfield = topic.get("subfield") or {}
    value = subfield.get("id") if isinstance(subfield, dict) else subfield
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _classify_openalex_field_candidate(paper: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    """Balance broad OpenAlex recall with an auditable precision gate.

    Every API candidate is retained as a source version. Only strong text or
    topic evidence enters the strict condensed-matter analytics view.
    """
    local_record = dict(paper)
    # Topic names are provider predictions, not independent textual evidence.
    # Excluding them here prevents a weak secondary topic from auto-accepting
    # the same record that the broad topic query returned.
    local_record["concepts"] = []
    local_record["subjects"] = []
    text_keep, text_reasons = is_condensed_matter_record(local_record)

    raw = paper.get("raw_json") if isinstance(paper.get("raw_json"), dict) else {}
    topics = raw.get("topics") if isinstance(raw.get("topics"), list) else paper.get("topics") or []
    primary = raw.get("primary_topic") if isinstance(raw.get("primary_topic"), dict) else paper.get("primary_topic") or {}
    primary_id = str(primary.get("id") or "") if isinstance(primary, dict) else ""
    primary_score = float(primary.get("score") or 0) if isinstance(primary, dict) else 0.0
    best_condmat_score = 0.0
    for topic in topics:
        if not isinstance(topic, dict) or _openalex_subfield_id(topic) != "3104":
            continue
        score = float(topic.get("score") or 0)
        best_condmat_score = max(best_condmat_score, score)
        if primary_id and str(topic.get("id") or "") == primary_id:
            primary_score = max(primary_score, score)
    primary_is_condmat = _openalex_subfield_id(primary) == "3104"
    topic_keep = (primary_is_condmat and primary_score >= 0.35) or best_condmat_score >= 0.45
    reasons = list(text_reasons)
    if topic_keep:
        reasons.append(
            f"openalex-condmat-topic:{'primary' if primary_is_condmat else 'secondary'}:{max(primary_score, best_condmat_score):.3f}"
        )
    journal_key = " ".join(str(paper.get("journal") or "").split()).casefold()
    topic_only_generalist = bool(
        topic_keep
        and not text_keep
        and journal_key in {journal.casefold() for journal in GENERALIST_JOURNALS}
    )
    if topic_only_generalist:
        reasons.append("openalex-generalist-topic-requires-text")
    scientific_keep = bool(text_keep or (topic_keep and not topic_only_generalist))
    source_evidence = openalex_source_quality(paper)
    source_keep = bool(source_evidence["eligible_for_hotspot_source_gate"])
    if not scientific_keep:
        reasons.append("openalex-field-candidate-unverified")
    reasons.append(source_quality_reason(source_evidence))
    evidence = {
        "text_filter_passed": bool(text_keep),
        "primary_topic_id": primary_id,
        "primary_subfield_id": _openalex_subfield_id(primary),
        "primary_topic_score": round(primary_score, 4),
        "best_condmat_topic_score": round(best_condmat_score, 4),
        "scientific_gate_passed": scientific_keep,
        "topic_only_generalist": topic_only_generalist,
        "source_quality": source_evidence,
        "strict_eligible": bool(scientific_keep and source_keep),
        "policy": "scientific_text_gate_for_generalist_and_scholarly_source_gate_v2",
    }
    return bool(scientific_keep and source_keep), reasons, evidence
def run(args: argparse.Namespace) -> dict[str, Any]:
    ensure_data_layout()
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    journals = parse_journals(args.journals or "", args.scope)
    client = OpenAlexClient(email=args.mailto, timeout=args.timeout, polite_delay=args.sleep_seconds)
    fetched = deduped = failed = 0
    inserted = updated = 0
    affected_paper_ids: list[str] = []
    metadata_changed_paper_ids: list[str] = []
    source_counts: dict[str, dict[str, int]] = {
        "openalex": {"fetched": 0, "in_window": 0, "eligible": 0, "inserted": 0, "updated": 0, "deduped": 0},
        "openalex_field": {"fetched": 0, "in_window": 0, "eligible": 0, "review_candidates": 0, "inserted": 0, "updated": 0, "deduped": 0, "pages": 0, "complete": 0},
        "crossref": {"fetched": 0, "in_window": 0, "eligible": 0, "inserted": 0, "updated": 0, "deduped": 0, "pages": 0, "journals_completed": 0, "journals_partial": 0},
        "arxiv": {"fetched": 0, "in_window": 0, "eligible": 0, "inserted": 0, "updated": 0, "deduped": 0},
    }
    errors: list[dict[str, Any]] = []
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    cancellation: dict[str, Any] | None = None

    with connect() as conn:
        records_since_commit = 0
        init_db(conn)
        apply_unified_schema(conn)
        if args.force_refresh and not args.dry_run:
            clear_data_mode(conn, "real")
        start_ingest_run(conn, run_id, started_at, args, journals)
        # Do not keep SQLite's write transaction open while a remote source is
        # being fetched.  The dashboard and search routes must stay responsive
        # during a slow arXiv/OpenAlex request.
        if not args.dry_run:
            conn.commit()
        if _cancel_requested(conn, args):
            cancellation = {"requested": True, "boundary": "startup", "requested_at": utc_now()}
        if cancellation is None and bool(getattr(args, "include_openalex_field", True)):
            field_checkpoint_name = "__condmat_subfield_3104__"
            checkpoint = (
                get_checkpoint(
                    conn,
                    args.scope,
                    field_checkpoint_name,
                    args.baseline_from,
                    args.baseline_to,
                    source="openalex_field",
                )
                if args.resume
                else None
            )
            cursor = str(checkpoint.get("cursor") or "*") if checkpoint else "*"
            page_offset = int(checkpoint.get("page") or 0) if checkpoint else 0
            max_pages = int(getattr(args, "max_pages", 0) or 0)
            field_pages = 0
            field_complete = False
            if not args.dry_run:
                _publish_source_progress(
                    conn,
                    args,
                    source="openalex_field",
                    source_label="OpenAlex 全领域（凝聚态子领域）",
                    page=page_offset,
                    **_progress_count_snapshot(
                        fetched=fetched,
                        inserted=inserted,
                        updated=updated,
                        deduped=deduped,
                        source_counts=source_counts,
                    ),
                    percent=10,
                )
                conn.commit()
            try:
                while True:
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"openalex_field:before_page:{page_offset + field_pages + 1}",
                        commit_pending=True,
                    )
                    if max_pages > 0 and field_pages >= max_pages:
                        failed += 1
                        errors.append({
                            "source": "openalex_field",
                            "error": "openalex_condmat_window_incomplete",
                            "stop_reason": "max_pages",
                            "page": page_offset + field_pages,
                        })
                        break
                    previous_cursor = cursor or "*"
                    payload = client.fetch_condensed_matter_page(
                        args.baseline_from,
                        args.baseline_to,
                        per_page=100,
                        cursor=previous_cursor,
                    )
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"openalex_field:after_fetch:{page_offset + field_pages + 1}",
                        commit_pending=True,
                    )
                    results = payload.get("results", []) or []
                    source_counts["openalex_field"]["pages"] += 1
                    field_pages += 1
                    fetched += len(results)
                    source_counts["openalex_field"]["fetched"] += len(results)
                    source_counts["openalex_field"]["in_window"] += len(results)
                    for raw in results:
                        paper = client._normalize_work(raw, "")
                        paper["data_mode"] = "real"
                        keep, reasons, field_evidence = _classify_openalex_field_candidate(paper)
                        raw_payload = dict(paper.get("raw_json") or {})
                        raw_payload["_radar_condmat_evidence"] = field_evidence
                        paper["raw_json"] = raw_payload
                        if keep:
                            source_counts["openalex_field"]["eligible"] += 1
                        else:
                            source_counts["openalex_field"]["review_candidates"] += 1
                        paper["filter_reasons"] = reasons
                        paper["condmat_view_eligible"] = bool(keep)
                        paper["condmat_view_reason"] = ",".join(reasons) if reasons else "openalex-field-candidate-unverified"
                        doi = normalize_doi(paper.get("doi")) or ""
                        title_key = normalized_title_key(paper.get("title") or "")
                        if doi and doi in seen_doi:
                            deduped += 1
                            source_counts["openalex_field"]["deduped"] += 1
                            continue
                        if not doi and title_key and title_key in seen_title:
                            deduped += 1
                            source_counts["openalex_field"]["deduped"] += 1
                            continue
                        if doi:
                            seen_doi.add(doi)
                        if title_key:
                            seen_title.add(title_key)
                        action, paper_id = _store_paper(conn, paper, dry_run=args.dry_run)
                        records_since_commit = _commit_ingest_batch(
                            conn,
                            records_since_commit,
                            dry_run=args.dry_run,
                        )
                        if records_since_commit == 0:
                            _raise_if_cancel_requested(
                                conn,
                                args,
                                f"openalex_field:batch:{source_counts['openalex_field']['fetched']}",
                            )
                        if action == "unchanged":
                            deduped += 1
                            source_counts["openalex_field"]["deduped"] += 1
                        else:
                            source_counts["openalex_field"][action] += 1
                            inserted += int(action == "inserted")
                            updated += int(action == "updated")
                            if paper_id:
                                metadata_changed_paper_ids.append(paper_id)
                            if paper_id:
                                affected_paper_ids.append(paper_id)
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"openalex_field:after_page:{page_offset + field_pages}",
                        commit_pending=True,
                    )
                    next_cursor = str(payload.get("meta", {}).get("next_cursor") or "")
                    if not args.dry_run:
                        update_checkpoint(
                            conn,
                            args.scope,
                            field_checkpoint_name,
                            args.baseline_from,
                            args.baseline_to,
                            next_cursor or previous_cursor,
                            page_offset + field_pages,
                            source="openalex_field",
                        )
                        _publish_source_progress(
                            conn,
                            args,
                            source="openalex_field",
                            source_label="OpenAlex 全领域（凝聚态子领域）",
                            page=page_offset + field_pages,
                            **_progress_count_snapshot(
                                fetched=fetched,
                                inserted=inserted,
                                updated=updated,
                                deduped=deduped,
                                source_counts=source_counts,
                            ),
                            percent=min(18, 10 + min(field_pages, 8)),
                        )
                        conn.commit()
                    if not results or not next_cursor:
                        field_complete = True
                        source_counts["openalex_field"]["complete"] = 1
                        break
                    if next_cursor == previous_cursor:
                        failed += 1
                        errors.append({
                            "source": "openalex_field",
                            "error": "openalex_condmat_window_incomplete",
                            "stop_reason": "next_cursor_not_advanced",
                            "page": page_offset + field_pages,
                        })
                        break
                    cursor = next_cursor
                    if args.sleep_seconds:
                        time.sleep(args.sleep_seconds)
            except IngestCancellationRequested as exc:
                cancellation = {"requested": True, "boundary": exc.boundary, "requested_at": utc_now()}
            except OpenAlexHTTPError as exc:
                failed += 1
                errors.append({"source": "openalex_field", **exc.to_dict()})
            except Exception as exc:
                failed += 1
                errors.append({
                    "source": "openalex_field",
                    "type": type(exc).__name__,
                    "message": " ".join(str(exc).split())[:300],
                })
            if cancellation is None and not field_complete and not any(item.get("source") == "openalex_field" for item in errors):
                failed += 1
                errors.append({
                    "source": "openalex_field",
                    "error": "openalex_condmat_window_incomplete",
                    "stop_reason": "unknown",
                })
        for journal_index, journal in enumerate([] if cancellation else journals):
            if not args.dry_run:
                _publish_source_progress(
                    conn,
                    args,
                    source="openalex",
                    source_label=f"OpenAlex 期刊：{journal}",
                    page=0,
                    **_progress_count_snapshot(
                        fetched=fetched,
                        inserted=inserted,
                        updated=updated,
                        deduped=deduped,
                        source_counts=source_counts,
                    ),
                    percent=_journal_progress_percent(
                        base=19,
                        span=9,
                        journal_index=journal_index,
                        journal_count=len(journals),
                        page=0,
                    ),
                )
                conn.commit()
            try:
                _raise_if_cancel_requested(
                    conn,
                    args,
                    f"openalex:{journal}:before_source_lookup",
                    commit_pending=True,
                )
                source_id = client.find_source_id(journal)
                _raise_if_cancel_requested(
                    conn,
                    args,
                    f"openalex:{journal}:after_source_lookup",
                    commit_pending=True,
                )
                if not source_id:
                    failed += 1
                    errors.append({"source": "openalex", "journal": journal, "error": "source_not_found"})
                    continue
                checkpoint = get_checkpoint(conn, args.scope, journal, args.baseline_from, args.baseline_to) if args.resume else None
                cursor = checkpoint.get("cursor") if checkpoint else "*"
                page = int(checkpoint.get("page") or 0) if checkpoint else 0
                max_pages = int(getattr(args, "max_pages", 0) or 0)
                pages_this_attempt = 0
                journal_fetched = 0
                while True:
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"openalex:{journal}:before_page:{page + 1}",
                        commit_pending=True,
                    )
                    if max_pages > 0 and pages_this_attempt >= max_pages:
                        failed += 1
                        errors.append(
                            {
                                "source": "openalex",
                                "journal": journal,
                                "error": "openalex_window_incomplete",
                                "stop_reason": "max_pages",
                                "page": page,
                            }
                        )
                        break
                    previous_cursor = cursor or "*"
                    payload = client.fetch_journal_page(
                        journal,
                        source_id,
                        from_date=args.baseline_from,
                        to_date=args.baseline_to,
                        per_page=200,
                        cursor=cursor or "*",
                    )
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"openalex:{journal}:after_fetch:{page + 1}",
                        commit_pending=True,
                    )
                    results = payload.get("results", [])
                    if not results:
                        if not args.dry_run:
                            update_checkpoint(conn, args.scope, journal, args.baseline_from, args.baseline_to, "", page + 1)
                            _publish_source_progress(
                                conn,
                                args,
                                source="openalex",
                                source_label=f"OpenAlex 期刊：{journal}",
                                page=page + 1,
                                **_progress_count_snapshot(
                                    fetched=fetched,
                                    inserted=inserted,
                                    updated=updated,
                                    deduped=deduped,
                                    source_counts=source_counts,
                                ),
                                percent=_journal_progress_percent(
                                    base=19,
                                    span=9,
                                    journal_index=journal_index,
                                    journal_count=len(journals),
                                    page=pages_this_attempt + 1,
                                ),
                            )
                            conn.commit()
                        break
                    for raw in results:
                        fetched += 1
                        source_counts["openalex"]["fetched"] += 1
                        source_counts["openalex"]["in_window"] += 1
                        journal_fetched += 1
                        paper = client._normalize_work(raw, journal)
                        paper["data_mode"] = "real"
                        keep, reasons = is_condensed_matter_record(paper)
                        if not keep:
                            continue
                        source_counts["openalex"]["eligible"] += 1
                        paper["filter_reasons"] = reasons
                        paper["condmat_view_eligible"] = True
                        paper["condmat_view_reason"] = ",".join(reasons)
                        doi = (paper.get("doi") or "").lower()
                        title_key = normalized_title_key(paper.get("title") or "")
                        if doi and doi in seen_doi:
                            deduped += 1
                            source_counts["openalex"]["deduped"] += 1
                            continue
                        if not doi and title_key and title_key in seen_title:
                            deduped += 1
                            source_counts["openalex"]["deduped"] += 1
                            continue
                        if doi:
                            seen_doi.add(doi)
                        if title_key:
                            seen_title.add(title_key)
                        action, paper_id = _store_paper(conn, paper, dry_run=args.dry_run)
                        records_since_commit = _commit_ingest_batch(
                            conn,
                            records_since_commit,
                            dry_run=args.dry_run,
                        )
                        if records_since_commit == 0:
                            _raise_if_cancel_requested(
                                conn,
                                args,
                                f"openalex:{journal}:batch:{journal_fetched}",
                            )
                        if action == "unchanged":
                            deduped += 1
                            source_counts["openalex"]["deduped"] += 1
                        else:
                            source_counts["openalex"][action] += 1
                            inserted += int(action == "inserted")
                            updated += int(action == "updated")
                            if paper_id:
                                metadata_changed_paper_ids.append(paper_id)
                                affected_paper_ids.append(paper_id)
                        if args.limit_per_journal and journal_fetched >= args.limit_per_journal:
                            break
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"openalex:{journal}:after_page:{page + 1}",
                        commit_pending=True,
                    )
                    cursor = payload.get("meta", {}).get("next_cursor") or ""
                    page += 1
                    pages_this_attempt += 1
                    if not args.dry_run:
                        update_checkpoint(conn, args.scope, journal, args.baseline_from, args.baseline_to, cursor, page)
                    if not args.dry_run:
                        _publish_source_progress(
                            conn,
                            args,
                            source="openalex",
                            source_label=f"OpenAlex 期刊：{journal}",
                            page=page,
                            **_progress_count_snapshot(
                                fetched=fetched,
                                inserted=inserted,
                                updated=updated,
                                deduped=deduped,
                                source_counts=source_counts,
                            ),
                            percent=_journal_progress_percent(
                                base=19,
                                span=9,
                                journal_index=journal_index,
                                journal_count=len(journals),
                                page=pages_this_attempt,
                            ),
                        )
                        # Release the writer before requesting the next page.
                        conn.commit()
                    if cursor and cursor == previous_cursor:
                        failed += 1
                        errors.append(
                            {
                                "source": "openalex",
                                "journal": journal,
                                "error": "openalex_window_incomplete",
                                "stop_reason": "next_cursor_not_advanced",
                                "page": page,
                            }
                        )
                        break
                    if args.limit_per_journal and journal_fetched >= args.limit_per_journal:
                        failed += 1
                        errors.append(
                            {
                                "source": "openalex",
                                "journal": journal,
                                "error": "openalex_window_incomplete",
                                "stop_reason": "limit_per_journal",
                                "page": page,
                            }
                        )
                        break
                    if not cursor:
                        break
            except IngestCancellationRequested as exc:
                cancellation = {"requested": True, "boundary": exc.boundary, "requested_at": utc_now()}
            except OpenAlexHTTPError as exc:
                failed += 1
                errors.append({"journal": journal, **exc.to_dict()})
            except Exception as exc:
                failed += 1
                errors.append({"journal": journal, "type": type(exc).__name__, "message": str(exc)})
            if cancellation is not None:
                break

        if cancellation is None and bool(getattr(args, "include_crossref", True)):
            crossref_client = CrossrefClient(
                timeout=int(getattr(args, "crossref_timeout", None) or args.timeout),
                polite_delay=args.sleep_seconds,
                mailto=args.mailto,
            )
            for journal_index, journal_name in enumerate(journals):
                if not args.dry_run:
                    _publish_source_progress(
                        conn,
                        args,
                        source="crossref",
                        source_label=f"Crossref 期刊：{journal_name}",
                        page=0,
                        **_progress_count_snapshot(
                            fetched=fetched,
                            inserted=inserted,
                            updated=updated,
                            deduped=deduped,
                            source_counts=source_counts,
                        ),
                        percent=_journal_progress_percent(
                            base=29,
                            span=9,
                            journal_index=journal_index,
                            journal_count=len(journals),
                            page=0,
                        ),
                    )
                    conn.commit()
                journal = find_journal(journal_name)
                if journal is None:
                    failed += 1
                    source_counts["crossref"]["journals_partial"] += 1
                    errors.append(
                        {
                            "source": "crossref",
                            "journal": journal_name,
                            "error": "journal_not_in_registry",
                        }
                    )
                    continue
                checkpoint = (
                    get_checkpoint(
                        conn,
                        args.scope,
                        journal_name,
                        args.baseline_from,
                        args.baseline_to,
                        source="crossref",
                    )
                    if args.resume
                    else None
                )
                cursor = str(checkpoint.get("cursor") or "*") if checkpoint else "*"
                page_offset = int(checkpoint.get("page") or 0) if checkpoint else 0
                page_limit = int(getattr(args, "max_pages", 0) or 0)
                rows = int(getattr(args, "crossref_rows", 1000) or 1000)
                seen_crossref_records: set[str] = set()
                journal_complete = False
                saw_page = False
                try:
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"crossref:{journal_name}:before_page:{page_offset + 1}",
                        commit_pending=True,
                    )
                    for page_data in crossref_client.iterate_crossref_works(
                        journal,
                        args.baseline_from,
                        args.baseline_to,
                        rows=rows,
                        cursor=cursor,
                        max_pages=page_limit,
                        sleep_seconds=args.sleep_seconds,
                        page_hook=lambda phase, source_page: _raise_if_cancel_requested(
                            conn,
                            args,
                            f"crossref:{journal_name}:{phase}_remote_page:{page_offset + source_page}",
                            commit_pending=True,
                        ),
                    ):
                        _raise_if_cancel_requested(
                            conn,
                            args,
                            f"crossref:{journal_name}:after_fetch:{page_offset + page_data.page}",
                            commit_pending=True,
                        )
                        saw_page = True
                        fetched += page_data.fetched
                        source_counts["crossref"]["fetched"] += page_data.fetched
                        source_counts["crossref"]["in_window"] += len(page_data.papers)
                        source_counts["crossref"]["pages"] += 1
                        for paper in page_data.papers:
                            paper["data_mode"] = "real"
                            keep, reasons = is_condensed_matter_record(paper)
                            if not keep:
                                continue
                            source_counts["crossref"]["eligible"] += 1
                            paper["filter_reasons"] = reasons
                            paper["condmat_view_eligible"] = True
                            paper["condmat_view_reason"] = ",".join(reasons)
                            source_record_id = _source_record_id(paper)
                            if source_record_id in seen_crossref_records:
                                deduped += 1
                                source_counts["crossref"]["deduped"] += 1
                                continue
                            seen_crossref_records.add(source_record_id)
                            # Do not compare this set with OpenAlex DOI values.
                            # A shared DOI joins the canonical paper while each
                            # provider keeps its own independently auditable version.
                            action, paper_id = _store_paper(conn, paper, dry_run=args.dry_run)
                            records_since_commit = _commit_ingest_batch(
                                conn,
                                records_since_commit,
                                dry_run=args.dry_run,
                            )
                            if records_since_commit == 0:
                                _raise_if_cancel_requested(
                                    conn,
                                    args,
                                    f"crossref:{journal_name}:batch:{source_counts['crossref']['fetched']}",
                                )
                            if action == "unchanged":
                                deduped += 1
                                source_counts["crossref"]["deduped"] += 1
                            else:
                                source_counts["crossref"][action] += 1
                                inserted += int(action == "inserted")
                                updated += int(action == "updated")
                                if paper_id:
                                    metadata_changed_paper_ids.append(paper_id)
                                    affected_paper_ids.append(paper_id)

                        _raise_if_cancel_requested(
                            conn,
                            args,
                            f"crossref:{journal_name}:after_page:{page_offset + page_data.page}",
                            commit_pending=True,
                        )
                        stop_reason = str(page_data.stop_reason or "")
                        checkpoint_cursor = page_data.next_cursor or page_data.cursor_used
                        if not args.dry_run:
                            # Only a fully received page may move the provider
                            # checkpoint. Earlier pages are already durable; a
                            # failed retry resumes from their saved next cursor.
                            if not page_data.errors and stop_reason != "next_cursor_not_advanced":
                                update_checkpoint(
                                    conn,
                                    args.scope,
                                    journal_name,
                                    args.baseline_from,
                                    args.baseline_to,
                                    checkpoint_cursor,
                                    page_offset + page_data.page,
                                    source="crossref",
                                )
                            _publish_source_progress(
                                conn,
                                args,
                                source="crossref",
                                source_label=f"Crossref 期刊：{journal_name}",
                                page=page_offset + page_data.page,
                                **_progress_count_snapshot(
                                    fetched=fetched,
                                    inserted=inserted,
                                    updated=updated,
                                    deduped=deduped,
                                    source_counts=source_counts,
                                ),
                                percent=_journal_progress_percent(
                                    base=29,
                                    span=9,
                                    journal_index=journal_index,
                                    journal_count=len(journals),
                                    page=page_data.page,
                                ),
                            )
                            conn.commit()
                        if page_data.errors:
                            failed += len(page_data.errors)
                            errors.extend(
                                {"source": "crossref", **item}
                                for item in page_data.errors
                            )
                        if stop_reason in CROSSREF_COMPLETE_STOP_REASONS:
                            journal_complete = True
                        elif stop_reason:
                            failed += 1
                            errors.append(
                                {
                                    "source": "crossref",
                                    "journal": journal_name,
                                    "error": "crossref_window_incomplete",
                                    "stop_reason": stop_reason,
                                    "page": page_offset + page_data.page,
                                }
                            )
                        if stop_reason:
                            break
                    if not saw_page:
                        failed += 1
                        errors.append(
                            {
                                "source": "crossref",
                                "journal": journal_name,
                                "error": "crossref_no_page_result",
                            }
                        )
                except IngestCancellationRequested as exc:
                    cancellation = {"requested": True, "boundary": exc.boundary, "requested_at": utc_now()}
                except Exception as exc:
                    failed += 1
                    errors.append(
                        {
                            "source": "crossref",
                            "journal": journal_name,
                            "type": type(exc).__name__,
                            "message": " ".join(str(exc).split())[:300],
                        }
                    )
                if journal_complete:
                    source_counts["crossref"]["journals_completed"] += 1
                else:
                    source_counts["crossref"]["journals_partial"] += 1
                if cancellation is not None:
                    break
        if cancellation is None and args.include_arxiv:
            if not args.dry_run:
                _publish_source_progress(
                    conn,
                    args,
                    source="arxiv",
                    source_label="arXiv cond-mat 全分类",
                    page=0,
                    **_progress_count_snapshot(
                        fetched=fetched,
                        inserted=inserted,
                        updated=updated,
                        deduped=deduped,
                        source_counts=source_counts,
                    ),
                    percent=39,
                )
                conn.commit()
            arxiv_progress = {"page": 0}
            try:
                _raise_if_cancel_requested(conn, args, "arxiv:before_fetch", commit_pending=True)
                arxiv_base_fetched = fetched
                arxiv_client = ArxivClient(timeout=args.timeout, polite_delay=0 if args.scope == "arxiv_live" else 3.0)

                def handle_arxiv_page(phase: str, page: int) -> None:
                    # A completed remote page becomes visible only after the
                    # cooperative-cancellation check.  Therefore a page that
                    # observes ``cancel_requested`` never advances UI progress.
                    _raise_if_cancel_requested(
                        conn,
                        args,
                        f"arxiv:{phase}_remote_page:{page + 1}",
                        commit_pending=True,
                    )
                    if phase != "after":
                        return
                    arxiv_progress["page"] = page + 1
                    if _publish_source_progress(
                        conn,
                        args,
                        source="arxiv",
                        source_label="arXiv cond-mat 全分类",
                        page=page + 1,
                        **_progress_count_snapshot(
                            fetched=arxiv_base_fetched + int(arxiv_client.last_fetched_count),
                            inserted=inserted,
                            updated=updated,
                            deduped=deduped,
                            source_counts=source_counts,
                        ),
                        percent=39,
                    ):
                        conn.commit()

                arxiv_papers, arxiv_failures = arxiv_client.fetch(
                    args.baseline_from,
                    args.baseline_to,
                    max_results=args.limit_per_journal or 500,
                    page_hook=handle_arxiv_page,
                )
                _raise_if_cancel_requested(conn, args, "arxiv:after_fetch", commit_pending=True)
                arxiv_fetched = int(getattr(arxiv_client, "last_fetched_count", len(arxiv_papers)))
                fetched += arxiv_fetched
                source_counts["arxiv"]["fetched"] += arxiv_fetched
                source_counts["arxiv"]["in_window"] += len(arxiv_papers)
                failed += arxiv_failures
                if arxiv_failures:
                    errors.append({"source": "arxiv", **(arxiv_client.last_error or {"error": "arxiv_fetch_failed"})})
                seen_arxiv: set[str] = set()
                for paper in arxiv_papers:
                    paper["data_mode"] = "real"
                    keep, reasons = is_condensed_matter_record(paper)
                    if not keep:
                        continue
                    source_counts["arxiv"]["eligible"] += 1
                    arxiv_id = str(paper.get("arxiv_id") or "")
                    if arxiv_id and arxiv_id in seen_arxiv:
                        deduped += 1
                        source_counts["arxiv"]["deduped"] += 1
                        continue
                    if arxiv_id:
                        seen_arxiv.add(arxiv_id)
                    paper["filter_reasons"] = reasons
                    paper["condmat_view_eligible"] = True
                    paper["condmat_view_reason"] = ",".join(reasons)
                    action, paper_id = _store_paper(conn, paper, dry_run=args.dry_run)
                    records_since_commit = _commit_ingest_batch(
                        conn,
                        records_since_commit,
                        dry_run=args.dry_run,
                    )
                    if records_since_commit == 0:
                        _raise_if_cancel_requested(
                            conn,
                            args,
                            f"arxiv:batch:{source_counts['arxiv']['fetched']}",
                        )
                    if action == "unchanged":
                        deduped += 1
                        source_counts["arxiv"]["deduped"] += 1
                    else:
                        source_counts["arxiv"][action] += 1
                        inserted += int(action == "inserted")
                        updated += int(action == "updated")
                        if paper_id:
                            metadata_changed_paper_ids.append(paper_id)
                            affected_paper_ids.append(paper_id)
                _raise_if_cancel_requested(conn, args, "arxiv:after_page_processing", commit_pending=True)
                if not args.dry_run:
                    _publish_source_progress(
                        conn,
                        args,
                        source="arxiv",
                        source_label="arXiv cond-mat 全分类",
                        page=int(arxiv_progress["page"]),
                        **_progress_count_snapshot(
                            fetched=fetched,
                            inserted=inserted,
                            updated=updated,
                            deduped=deduped,
                            source_counts=source_counts,
                        ),
                        percent=40,
                    )
                    conn.commit()
            except IngestCancellationRequested as exc:
                cancellation = {"requested": True, "boundary": exc.boundary, "requested_at": utc_now()}
            except Exception as exc:
                failed += 1
                message = " ".join(str(exc).split())[:300]
                errors.append({"source": "arxiv", "type": type(exc).__name__, "message": message})

        changed_canonical_ids = list(dict.fromkeys(affected_paper_ids))
        metadata_changed_canonical_ids = list(dict.fromkeys(metadata_changed_paper_ids))
        incremental_mode = bool(getattr(args, "incremental", False))
        quality_mode = "incremental_subset" if incremental_mode else "full_corpus"
        quality_subset_size = len(changed_canonical_ids) if incremental_mode else None
        source_quality_reclassification = {
            "processed_canonicals": 0,
            "repository_candidates": 0,
            "downgraded": 0,
            "skipped": True,
            "mode": quality_mode,
            "subset_size": quality_subset_size,
        }
        if args.dry_run:
            source_quality_reclassification["reason"] = "ingest_dry_run"
        elif cancellation is not None:
            source_quality_reclassification["reason"] = "ingest_cancelled"
        elif incremental_mode and not changed_canonical_ids:
            source_quality_reclassification["reason"] = "no_changed_canonicals"
        else:
            _checkpoint_postprocess_progress(
                conn,
                args,
                stage="quality_reclassification",
                percent=40,
                status="running",
            )
            source_quality_reclassification = reclassify_openalex_repository_quality(
                conn,
                **(
                    {"canonical_ids": changed_canonical_ids}
                    if incremental_mode
                    else {}
                ),
            )
            source_quality_reclassification["mode"] = quality_mode
            source_quality_reclassification["subset_size"] = quality_subset_size
            _checkpoint_postprocess_progress(
                conn,
                args,
                stage="quality_reclassification",
                percent=41,
                status="completed",
                result=dict(source_quality_reclassification),
            )

        postprocess_results: dict[str, Any] = {
            "quality_reclassification": dict(source_quality_reclassification),
        }
        analytics_mode = "none"
        if not args.dry_run and cancellation is None:
            if incremental_mode:
                # Live scans update the unified paper/version/material/search path below.
                # The expensive whole-corpus analytics rebuild remains available to full runs.
                extracted = stats_rows = lifecycle_rows = 0
                analytics_mode = "deferred_full_rebuild"
            else:
                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="entity_extraction",
                    percent=41,
                    status="running",
                )
                # ``rebuild_paper_terms`` streams with fetchmany. Keep its
                # caller-owned whole-stage transaction here: per-batch commits
                # are opt-in for workflows that explicitly accept them.
                extracted = rebuild_paper_terms(conn, batch_size=250)
                entity_result = {"processed_papers": extracted, "batch_size": 250}
                postprocess_results["entity_extraction"] = entity_result
                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="entity_extraction",
                    percent=42,
                    status="completed",
                    result=entity_result,
                )

                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="term_statistics",
                    percent=42,
                    status="running",
                )
                stats_rows = rebuild_term_month_stats(conn)
                term_stats_result = {"rows_rebuilt": stats_rows}
                postprocess_results["term_statistics"] = term_stats_result
                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="term_statistics",
                    percent=43,
                    status="completed",
                    result=term_stats_result,
                )

                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="lifecycle_analysis",
                    percent=43,
                    status="running",
                )
                lifecycle_rows = rebuild_lifecycle(
                    conn,
                    args.baseline_from,
                    args.baseline_to,
                    args.baseline_from,
                    args.baseline_to,
                )
                lifecycle_result = {"rows_rebuilt": lifecycle_rows}
                postprocess_results["lifecycle_analysis"] = lifecycle_result
                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="lifecycle_analysis",
                    percent=44,
                    status="completed",
                    result=lifecycle_result,
                )

                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="cooccurrence_network",
                    percent=44,
                    status="running",
                )
                cooccurrence = cooccurrence_network(conn, min_count=2)
                cooccurrence_result = {
                    "nodes": len(cooccurrence.get("nodes") or []),
                    "links": len(cooccurrence.get("links") or []),
                }
                postprocess_results["cooccurrence_network"] = cooccurrence_result
                _checkpoint_postprocess_progress(
                    conn,
                    args,
                    stage="cooccurrence_network",
                    percent=44,
                    status="completed",
                    result=cooccurrence_result,
                )
                analytics_mode = "full_rebuild"
            write_update_run(
                conn,
                started_at,
                args,
                inserted,
                updated,
                deduped,
                failed,
                f"real ingest; analytics={analytics_mode}; extracted={extracted}; "
                f"stats_rows={stats_rows}; lifecycle_rows={lifecycle_rows}",
            )
        changed = inserted + updated
        run_status = "cancelled" if cancellation is not None else ("ok" if not errors else "partial")
        finish_ingest_run(conn, run_id, run_status, fetched, inserted, deduped, failed, errors)

    final_counts = _progress_count_snapshot(
        fetched=fetched,
        inserted=inserted,
        updated=updated,
        deduped=deduped,
        source_counts=source_counts,
    )
    result = {
        "run_id": run_id,
        "status": run_status,
        **final_counts,
        "counts": final_counts,
        "kept": inserted,
        "kept_semantics": "legacy_alias_of_inserted",
        "fetched_count": fetched,
        "kept_count": inserted,
        "inserted_count": inserted,
        "updated_count": updated,
        "changed_count": inserted + updated,
        "deduped_count": deduped,
        "eligible_count": final_counts["eligible"],
        "review_candidates_count": final_counts["review_candidates"],
        "failed_count": failed,
        "affected_paper_ids": changed_canonical_ids,
        "metadata_changed_paper_ids": metadata_changed_canonical_ids,
        "source_counts": source_counts,
        "source_quality_reclassification": source_quality_reclassification,
        "postprocess": postprocess_results,
        "legacy_aliases": {
            "kept": "inserted",
            "kept_count": "inserted_count",
        },
        "count_semantics": {
            "fetched": "remote source records received before local filtering",
            "eligible": "source records passing the current inclusion gate; not cross-source unique papers",
            "review_candidates": "source records retained for review but not eligible for the condensed-matter view",
            "inserted": "new canonical papers inserted in this run",
            "updated": "existing canonical papers with meaningful metadata or version changes",
            "deduped": "source records already seen or stored without meaningful changes",
            "kept": "legacy alias of inserted; not an eligibility or strict-sample count",
            "fetched_count": "compatibility alias of fetched",
            "eligible_count": "compatibility alias of eligible",
            "review_candidates_count": "compatibility alias of review_candidates",
            "inserted_count": "compatibility alias of inserted",
            "updated_count": "compatibility alias of updated",
            "deduped_count": "compatibility alias of deduped",
            "kept_count": "legacy compatibility alias of inserted_count; not an eligibility or strict-sample count",
        },
        "dry_run": args.dry_run,
        "cancelled": cancellation is not None,
        "partial": cancellation is not None or bool(errors),
        "cancellation": cancellation,
        "errors": errors[:100],
        "source_completeness": {
            "complete": cancellation is None and not bool(errors),
            "openalex_field_complete": bool(source_counts["openalex_field"]["complete"]) if bool(getattr(args, "include_openalex_field", True)) else None,
            "openalex_field_pages": source_counts["openalex_field"]["pages"],
            "crossref_journals_completed": source_counts["crossref"]["journals_completed"],
            "crossref_journals_partial": source_counts["crossref"]["journals_partial"],
            "cancelled": cancellation is not None,
            "watermark_safe_to_advance": cancellation is None and not bool(errors),
        },
        "analytics_mode": analytics_mode,
        "logs_dir": str(logs_dir()),
    }
    write_ingest_log(run_id, result)
    return result


def start_ingest_run(conn, run_id: str, started_at: str, args: argparse.Namespace, journals: list[str]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_runs
          (id, started_at, source, scope, baseline_from, baseline_to, status, config_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, started_at, "openalex_crossref_arxiv", args.scope, args.baseline_from, args.baseline_to, "running", json.dumps({**vars(args), "journals": journals}, ensure_ascii=False)),
    )


def finish_ingest_run(conn, run_id: str, status: str, fetched: int, kept: int, deduped: int, failed: int, errors: list[dict[str, Any]]) -> None:
    conn.execute(
        """
        UPDATE ingest_runs SET finished_at=?, status=?, fetched_count=?, kept_count=?, deduped_count=?, failed_count=?, error_summary=?
        WHERE id=?
        """,
        (utc_now(), status, fetched, kept, deduped, failed, json.dumps(errors[:20], ensure_ascii=False), run_id),
    )


def get_checkpoint(
    conn,
    scope: str,
    journal: str,
    date_from: str,
    date_to: str,
    *,
    source: str = "openalex",
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM ingest_checkpoints WHERE source=? AND scope=? AND journal=? AND date_from=? AND date_to=?",
        (source, scope, journal, date_from, date_to),
    ).fetchone()
    return dict(row) if row else None


def update_checkpoint(
    conn,
    scope: str,
    journal: str,
    date_from: str,
    date_to: str,
    cursor: str,
    page: int,
    *,
    source: str = "openalex",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_checkpoints (source, scope, journal, date_from, date_to, cursor, page, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, scope, journal, date_from, date_to, cursor, page, utc_now()),
    )

def write_update_run(conn, started_at: str, args: argparse.Namespace, inserted: int, updated: int, duplicates: int, failed: int, message: str) -> None:
    conn.execute(
        """
        INSERT INTO update_runs
          (started_at, finished_at, from_date, to_date, baseline_from, baseline_to, trend_from, trend_to, trend_months, scope,
           requested_live, inserted_papers, updated_papers, duplicate_papers, failed_requests, used_mock_data, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, ?)
        """,
        (started_at, utc_now(), args.baseline_from, args.baseline_to, args.baseline_from, args.baseline_to, args.baseline_from, args.baseline_to, 24, args.scope, inserted, updated, duplicates, failed, message),
    )


def write_ingest_log(run_id: str, result: dict[str, Any]) -> None:
    directory = logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (directory / f"ingest_real_{stamp}_{run_id[:8]}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest real condensed matter metadata into the local radar database.")
    parser.add_argument("--baseline-from", default="2015-01-01")
    parser.add_argument("--baseline-to", default=date.today().isoformat())
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--journals", default="")
    parser.add_argument("--limit-per-journal", type=int, default=0)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mailto", default=None)
    parser.add_argument("--include-arxiv", action="store_true")
    parser.add_argument("--include-openalex-field", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-crossref", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--crossref-rows", type=int, default=1000)
    parser.add_argument(
        "--crossref-timeout",
        type=int,
        default=None,
        help="Crossref per-attempt timeout; defaults to --timeout.",
    )
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.12)
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
