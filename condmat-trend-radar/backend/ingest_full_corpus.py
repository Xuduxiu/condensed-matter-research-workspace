from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats
from backend.config import db_path, ensure_data_layout, export_dir, logs_dir, openalex_api_key
from backend.db.database import connect, init_db, normalized_title_key, upsert_paper, utc_now
from backend.ingest.crossref_client import CrossrefClient
from backend.ingest.journal_registry import JournalEntry, find_journal, journals_for_scope
from backend.ingest.lock import IngestLock, IngestLockError
from backend.ingest.openalex_client import MISSING_KEY_WARNING, OpenAlexClient, OpenAlexHTTPError
from backend.ingest_real_quickstart import classify_condmat, summarize_errors
from backend.nlp.normalize import normalize_term


WATCH_TERMS = ["hBN", "graphene", "ARPES", "STM", "DFT"]
TIMESERIES_TERMS = ["ZrTe5", "HfTe5", "moire", "FCI", "altermagnetism"]
COMPLETE_STOP_REASONS = {"items_empty", "short_page", "no_next_cursor", "next_cursor_not_advanced", "duplicate_rate_guard"}
GUARD_STOP_REASONS = {"max_pages_per_chunk", "chunk_timeout"}

@dataclass(frozen=True)
class Chunk:
    id: str
    scope: str
    journal: str
    date_from: str
    date_to: str


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict[str, Any]) -> None:
        record = {"ts": utc_now(), "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def month_end(year: int, month: int) -> date:
    return date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)


def period_ranges(from_date: str, to_date: str, chunk: str) -> list[tuple[str, str]]:
    start = parse_date(from_date)
    end = parse_date(to_date)
    if end < start:
        raise ValueError("--to must be on or after --from")
    out: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        if chunk == "yearly":
            period_end = date(cursor.year, 12, 31)
            next_cursor = date(cursor.year + 1, 1, 1)
        elif chunk == "quarterly":
            q_start = ((cursor.month - 1) // 3) * 3 + 1
            period_end = month_end(cursor.year, q_start + 2)
            next_cursor = period_end + timedelta(days=1)
        elif chunk == "monthly":
            period_end = month_end(cursor.year, cursor.month)
            next_cursor = period_end + timedelta(days=1)
        else:
            raise ValueError(f"unsupported chunk size: {chunk}")
        out.append((max(cursor, start).isoformat(), min(period_end, end).isoformat()))
        cursor = next_cursor
    return out


def make_chunk_id(scope: str, journal: str, date_from: str, date_to: str) -> str:
    digest = hashlib.sha1(f"{scope}|{journal}|{date_from}|{date_to}".encode("utf-8")).hexdigest()[:16]
    return f"chunk:{digest}"


def selected_journals(scope: str, journal_arg: str) -> list[JournalEntry]:
    if not journal_arg:
        return journals_for_scope(scope)
    output: list[JournalEntry] = []
    for name in [item.strip() for item in journal_arg.split(",") if item.strip()]:
        entry = find_journal(name)
        if not entry:
            raise ValueError(f"unknown journal: {name}")
        output.append(entry)
    return output


def build_chunks(args: argparse.Namespace) -> list[Chunk]:
    chunks: list[Chunk] = []
    for journal in selected_journals(args.scope, args.journal or ""):
        for date_from, date_to in period_ranges(args.from_date, args.to_date, args.chunk):
            chunks.append(Chunk(make_chunk_id(args.scope, journal.canonical_name, date_from, date_to), args.scope, journal.canonical_name, date_from, date_to))
    return chunks


def chunk_row(chunk: Chunk, run_id: str, status: str = "pending") -> dict[str, Any]:
    return {
        "id": chunk.id, "run_id": run_id, "source": "", "scope": chunk.scope, "journal": chunk.journal,
        "date_from": chunk.date_from, "date_to": chunk.date_to, "status": status,
        "fetched_count": 0, "kept_count": 0, "deduped_count": 0, "failed_count": 0,
        "started_at": "", "finished_at": "", "error_summary": "", "log_path": "", "checkpoint_json": "{}",
    }


def upsert_chunk(conn, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_chunks
          (id, run_id, source, scope, journal, date_from, date_to, status, fetched_count, kept_count,
           deduped_count, failed_count, started_at, finished_at, error_summary, log_path, checkpoint_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"], row.get("run_id", ""), row.get("source", ""), row["scope"], row["journal"], row["date_from"], row["date_to"], row.get("status", "pending"),
            int(row.get("fetched_count") or 0), int(row.get("kept_count") or 0), int(row.get("deduped_count") or 0), int(row.get("failed_count") or 0),
            row.get("started_at") or None, row.get("finished_at") or None, row.get("error_summary") or "", row.get("log_path") or "", row.get("checkpoint_json") or "{}",
        ),
    )


def read_chunk(conn, chunk_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM ingest_chunks WHERE id=?", (chunk_id,)).fetchone()
    return dict(row) if row else None


def write_chunk(chunk: Chunk, row: dict[str, Any], **updates: Any) -> dict[str, Any]:
    row = {**row, **updates}
    with connect() as conn:
        init_db(conn)
        upsert_chunk(conn, row)
    return row


def ensure_pending(run_id: str, chunks: list[Chunk], force: bool) -> None:
    with connect() as conn:
        init_db(conn)
        for chunk in chunks:
            existing = read_chunk(conn, chunk.id)
            if existing and not force:
                upsert_chunk(conn, {**existing, "run_id": run_id})
                continue
            upsert_chunk(conn, chunk_row(chunk, run_id))


def start_run(conn, run_id: str, args: argparse.Namespace, chunks: list[Chunk]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_runs
          (id, started_at, source, scope, baseline_from, baseline_to, status, config_json)
        VALUES (?, ?, 'full_corpus', ?, ?, ?, 'running', ?)
        """,
        (run_id, utc_now(), args.scope, args.from_date, args.to_date, json.dumps({**vars(args), "chunk_count": len(chunks)}, ensure_ascii=False)),
    )


def finish_run(conn, run_id: str, status: str, totals: dict[str, int], error_summary: str) -> None:
    conn.execute(
        """
        UPDATE ingest_runs
        SET finished_at=?, status=?, fetched_count=?, kept_count=?, deduped_count=?, failed_count=?, error_summary=?
        WHERE id=?
        """,
        (utc_now(), status, totals["fetched_count"], totals["kept_count"], totals["deduped_count"], totals["failed_count"], error_summary, run_id),
    )


def eligible_chunks(chunks: list[Chunk], args: argparse.Namespace) -> list[Chunk]:
    output: list[Chunk] = []
    with connect() as conn:
        init_db(conn)
        for chunk in chunks:
            row = read_chunk(conn, chunk.id)
            status = (row or {}).get("status") or "pending"
            if args.force or (args.resume and status != "finished") or (not args.resume and status == "pending"):
                output.append(chunk)
    return output[: args.max_chunks] if args.max_chunks and args.max_chunks > 0 else output

def log_path_for(run_id: str, chunk: Chunk) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in chunk.journal).strip("_")
    return logs_dir() / "full_ingest" / run_id / f"{safe}_{chunk.date_from}_{chunk.date_to}.log"


def load_seen_keys() -> tuple[set[str], set[str]]:
    with connect() as conn:
        init_db(conn)
        dois = {str(row["doi"]).lower() for row in conn.execute("SELECT doi FROM papers WHERE COALESCE(doi, '') != ''").fetchall()}
        titles = {normalized_title_key(row["title"] or "") for row in conn.execute("SELECT title FROM papers WHERE COALESCE(title, '') != ''").fetchall()}
    return dois, titles


def remaining_budget(args: argparse.Namespace, run_totals: dict[str, int]) -> int | None:
    if not args.max_papers or args.max_papers <= 0:
        return None
    return max(0, args.max_papers - run_totals["kept_count"])


def merge(total: dict[str, int], item: dict[str, Any]) -> None:
    for key in ("fetched_count", "kept_count", "deduped_count", "failed_count", "inserted", "updated"):
        total[key] += int(item.get(key) or 0)


def store_batch(papers: list[dict[str, Any]], args: argparse.Namespace, seen_doi: set[str], seen_title: set[str], remaining: int | None) -> dict[str, Any]:
    result: dict[str, Any] = {"kept_count": 0, "deduped_count": 0, "failed_count": 0, "inserted": 0, "updated": 0, "source_breakdown": Counter()}
    candidates: list[dict[str, Any]] = []
    for paper in papers:
        if remaining is not None and result["kept_count"] >= remaining:
            break
        keep, confidence, reasons = classify_condmat(paper, strict_condmat=args.strict_condmat)
        if not keep:
            continue
        doi = (paper.get("doi") or "").lower()
        title_key = normalized_title_key(paper.get("title") or "")
        if doi and doi in seen_doi:
            result["deduped_count"] += 1
            continue
        if not doi and title_key and title_key in seen_title:
            result["deduped_count"] += 1
            continue
        if doi:
            seen_doi.add(doi)
        if title_key:
            seen_title.add(title_key)
        paper["data_mode"] = "real"
        paper["condmat_confidence"] = confidence
        paper["filter_reasons"] = reasons
        paper.setdefault("source_scope", "published")
        candidates.append(paper)
        result["kept_count"] += 1
        result["source_breakdown"][str(paper.get("source") or "unknown")] += 1
    for start in range(0, len(candidates), args.batch_size):
        with connect() as conn:
            init_db(conn)
            for paper in candidates[start : start + args.batch_size]:
                try:
                    status = upsert_paper(conn, paper)
                    result["inserted" if status == "inserted" else "updated"] += 1
                except Exception:
                    result["failed_count"] += 1
    return result


def checkpoint(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("checkpoint_json") or "{}")
    except Exception:
        return {}


def checkpoint_progress(chunk: Chunk, source: str, result: dict[str, Any], checkpoint_data: dict[str, Any]) -> None:
    with connect() as conn:
        init_db(conn)
        row = read_chunk(conn, chunk.id) or chunk_row(chunk, "", "running")
        upsert_chunk(conn, {**row, "source": source, "status": "running", "fetched_count": result["fetched_count"], "kept_count": result["kept_count"], "deduped_count": result["deduped_count"], "failed_count": result["failed_count"], "checkpoint_json": json.dumps(checkpoint_data, ensure_ascii=False)})




def page_limit(args: argparse.Namespace, pages_already_done: int = 0) -> int:
    if not args.max_pages_per_chunk or args.max_pages_per_chunk <= 0:
        return 0
    return int(args.max_pages_per_chunk)


def chunk_deadline(args: argparse.Namespace) -> float | None:
    minutes = float(getattr(args, "chunk_timeout_minutes", 20) or 0)
    if minutes <= 0:
        return None
    return time.monotonic() + minutes * 60

def ingest_openalex(chunk: Chunk, args: argparse.Namespace, logger: JsonlLogger, source_cache: dict[str, str], seen_doi: set[str], seen_title: set[str], checkpoint_data: dict[str, Any], run_totals: dict[str, int]) -> dict[str, Any]:
    result: dict[str, Any] = {"fetched_count": 0, "kept_count": 0, "deduped_count": 0, "failed_count": 0, "inserted": 0, "updated": 0, "source_breakdown": Counter(), "errors": []}
    client = OpenAlexClient(timeout=args.timeout, polite_delay=args.sleep_seconds, max_retries=1)
    source_id = source_cache.get(chunk.journal) or client.find_source_id(chunk.journal) or ""
    source_cache[chunk.journal] = source_id
    if not source_id:
        result["failed_count"] += 1
        result["errors"].append({"source": "openalex", "journal": chunk.journal, "error": "source_not_found"})
        return result
    cursor = checkpoint_data.get("openalex_cursor") or "*"
    page = int(checkpoint_data.get("openalex_page") or 0)
    pages_this_attempt = 0
    max_pages_this_attempt = page_limit(args)
    while True:
        if max_pages_this_attempt and pages_this_attempt >= max_pages_this_attempt:
            checkpoint_data["openalex_page_limit_reached"] = True
            logger.write("openalex_page_limit", {"max_pages_this_attempt": max_pages_this_attempt})
            break
        remaining = None if not args.max_papers or args.max_papers <= 0 else max(0, args.max_papers - run_totals["kept_count"] - result["kept_count"])
        if remaining == 0:
            break
        try:
            payload = client.fetch_journal_page(chunk.journal, source_id, chunk.date_from, chunk.date_to, per_page=200, cursor=cursor)
        except OpenAlexHTTPError as exc:
            result["failed_count"] += 1
            result["errors"].append({"source": "openalex", "journal": chunk.journal, **exc.to_dict()})
            logger.write("openalex_error", {"page": page, **exc.to_dict()})
            break
        raw_items = payload.get("results", []) or []
        result["fetched_count"] += len(raw_items)
        stored = store_batch([client._normalize_work(raw, chunk.journal) for raw in raw_items], args, seen_doi, seen_title, remaining)
        merge(result, stored)
        result["source_breakdown"].update(stored.get("source_breakdown") or {})
        cursor = payload.get("meta", {}).get("next_cursor") or ""
        page += 1
        pages_this_attempt += 1
        checkpoint_data.update({"openalex_cursor": cursor, "openalex_page": page, "openalex_finished": not bool(cursor) or not bool(raw_items)})
        checkpoint_progress(chunk, "openalex", result, checkpoint_data)
        logger.write("openalex_page", {"page": page, "fetched": len(raw_items), "stored": {k: v for k, v in stored.items() if k != "source_breakdown"}, "next_cursor": bool(cursor)})
        if not cursor or not raw_items:
            break
        time.sleep(args.sleep_seconds)
    return result


def ingest_crossref(chunk: Chunk, journal: JournalEntry, args: argparse.Namespace, logger: JsonlLogger, seen_doi: set[str], seen_title: set[str], checkpoint_data: dict[str, Any], run_totals: dict[str, int]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "fetched_count": 0,
        "kept_count": 0,
        "deduped_count": 0,
        "failed_count": 0,
        "inserted": 0,
        "updated": 0,
        "source_breakdown": Counter(),
        "errors": [],
        "stop_reasons": [],
    }
    if remaining_budget(args, run_totals) == 0:
        return result
    client = CrossrefClient(timeout=args.timeout, polite_delay=args.sleep_seconds)
    cursor = checkpoint_data.get("crossref_cursor") or "*"
    page_offset = int(checkpoint_data.get("crossref_page") or 0)
    cursor_reset_attempted = False
    while True:
        errors_before = len(result["errors"])
        failed_before = result["failed_count"]
        fetched_before = result["fetched_count"]
        max_pages_remaining = page_limit(args, page_offset)
        if args.max_pages_per_chunk and args.max_pages_per_chunk > 0 and max_pages_remaining <= 0:
            checkpoint_data["crossref_page_limit_reached"] = True
            result["stop_reasons"].append("max_pages_per_chunk")
            logger.write("crossref_page_limit", {"max_pages_per_chunk": args.max_pages_per_chunk, "pages_already_done": page_offset})
            return result
        deadline = chunk_deadline(args)
        for page in client.iterate_crossref_works(
            journal,
            chunk.date_from,
            chunk.date_to,
            rows=args.rows_per_page,
            cursor=cursor,
            max_pages=max_pages_remaining,
            sleep_seconds=args.sleep_seconds,
            chunk_deadline=deadline,
        ):
            remaining = None if not args.max_papers or args.max_papers <= 0 else max(0, args.max_papers - run_totals["kept_count"] - result["kept_count"])
            result["fetched_count"] += page.fetched
            result["failed_count"] += len(page.errors)
            result["errors"].extend({"source": "crossref", **item} for item in page.errors)
            stored = store_batch(page.papers, args, seen_doi, seen_title, remaining)
            merge(result, stored)
            result["source_breakdown"].update(stored.get("source_breakdown") or {})
            page_no = page_offset + page.page
            if page.stop_reason:
                result["stop_reasons"].append(page.stop_reason)
            checkpoint_data.update(
                {
                    "crossref_cursor": page.next_cursor,
                    "crossref_page": page_no,
                    "crossref_finished": page.stop_reason in COMPLETE_STOP_REASONS,
                    "crossref_last_stop_reason": page.stop_reason,
                }
            )
            if page.stop_reason in GUARD_STOP_REASONS:
                checkpoint_data[f"crossref_{page.stop_reason}"] = True
            checkpoint_progress(chunk, "crossref", result, checkpoint_data)
            logger.write(
                "crossref_page",
                {
                    "page": page_no,
                    "cursor_used_prefix": page.cursor_used_prefix,
                    "next_cursor_prefix": page.next_cursor_prefix,
                    "fetched": page.fetched,
                    "inserted": int(stored.get("inserted") or 0),
                    "updated": int(stored.get("updated") or 0),
                    "deduped": int(stored.get("deduped_count") or 0),
                    "failed": int(stored.get("failed_count") or 0) + len(page.errors),
                    "kept": int(stored.get("kept_count") or 0),
                    "duplicate_rate_vs_previous_page": page.duplicate_rate_vs_previous_page,
                    "stop_reason": page.stop_reason,
                    "errors": page.errors[:3],
                },
            )
            cursor = page.next_cursor or cursor
            page_offset = page_no
            if page.errors or page.stop_reason or remaining_budget(args, {**run_totals, "kept_count": run_totals["kept_count"] + result["kept_count"]}) == 0:
                break
        new_errors = result["errors"][errors_before:]
        stale_cursor_error = (
            not cursor_reset_attempted
            and fetched_before == result["fetched_count"]
            and cursor != "*"
            and any(int(error.get("status") or 0) >= 500 for error in new_errors)
        )
        if stale_cursor_error:
            result["errors"] = result["errors"][:errors_before]
            result["failed_count"] = failed_before
            cursor = "*"
            page_offset = 0
            cursor_reset_attempted = True
            checkpoint_data.update({"crossref_cursor": "*", "crossref_page": 0, "crossref_stale_cursor_reset": True})
            logger.write("crossref_cursor_reset", {"reason": "server_error_from_checkpoint_cursor", "errors": new_errors[:3]})
            continue
        break
    return result

def process_chunk(chunk: Chunk, run_id: str, args: argparse.Namespace, source_cache: dict[str, str], run_totals: dict[str, int]) -> dict[str, Any]:
    journal = find_journal(chunk.journal)
    if not journal:
        raise ValueError(f"unknown journal: {chunk.journal}")
    log_path = log_path_for(run_id, chunk)
    logger = JsonlLogger(log_path)
    with connect() as conn:
        init_db(conn)
        existing = read_chunk(conn, chunk.id) or chunk_row(chunk, run_id)
    state = write_chunk(chunk, existing, run_id=run_id, status="running", started_at=utc_now(), finished_at="", error_summary="", log_path=str(log_path))
    logger.write("chunk_started", {"chunk": asdict(chunk), "config": vars(args)})
    checkpoint_data = checkpoint(state)
    seen_doi, seen_title = load_seen_keys()
    totals: dict[str, Any] = {"fetched_count": 0, "kept_count": 0, "deduped_count": 0, "failed_count": 0, "inserted": 0, "updated": 0}
    errors: list[dict[str, Any]] = []
    source_breakdown: Counter[str] = Counter()
    sources: list[str] = []
    if args.prefer == "openalex":
        if openalex_api_key():
            item = ingest_openalex(chunk, args, logger, source_cache, seen_doi, seen_title, checkpoint_data, run_totals)
            merge(totals, item)
            source_breakdown.update(item.get("source_breakdown") or {})
            errors.extend(item.get("errors") or [])
            if item.get("fetched_count") or item.get("kept_count"):
                sources.append("openalex")
        else:
            errors.append({"source": "openalex", "message": MISSING_KEY_WARNING})
            logger.write("openalex_skipped", {"message": MISSING_KEY_WARNING})
    if (args.prefer == "crossref" or (args.fallback == "crossref_arxiv" and (totals["kept_count"] == 0 or errors))) and remaining_budget(args, run_totals) != 0:
        item = ingest_crossref(chunk, journal, args, logger, seen_doi, seen_title, checkpoint_data, run_totals)
        merge(totals, item)
        source_breakdown.update(item.get("source_breakdown") or {})
        errors.extend(item.get("errors") or [])
        if item.get("fetched_count") or item.get("kept_count"):
            sources.append("crossref")
    status = "failed" if errors and totals["kept_count"] == 0 and totals["fetched_count"] == 0 else "finished"
    summary = summarize_errors(errors, [])
    write_chunk(chunk, state, source="+".join(dict.fromkeys(sources)) if sources else ("error" if errors else "none"), status=status, fetched_count=totals["fetched_count"], kept_count=totals["kept_count"], deduped_count=totals["deduped_count"], failed_count=totals["failed_count"], finished_at=utc_now(), error_summary=summary, checkpoint_json=json.dumps({**checkpoint_data, "source_breakdown": dict(source_breakdown)}, ensure_ascii=False))
    logger.write("chunk_finished", {"status": status, "totals": totals, "source_breakdown": dict(source_breakdown), "errors": errors[:10]})
    return {**totals, "status": status, "errors": errors, "source_breakdown": dict(source_breakdown)}


def recompute_stats(args: argparse.Namespace, run_id: str) -> dict[str, int]:
    started = utc_now()
    with connect() as conn:
        init_db(conn)
        extracted = rebuild_paper_terms(conn)
        stats_rows = rebuild_term_month_stats(conn)
        lifecycle_rows = rebuild_lifecycle(conn, args.from_date, args.to_date, args.from_date, args.to_date, data_mode="real")
        try:
            cooccurrence_network(conn, min_count=2, data_mode="real")
        except TypeError:
            cooccurrence_network(conn, min_count=2)
        conn.execute(
            """
            INSERT INTO update_runs
              (started_at, finished_at, from_date, to_date, baseline_from, baseline_to, trend_from, trend_to, trend_months, scope,
               requested_live, inserted_papers, updated_papers, duplicate_papers, failed_requests, used_mock_data, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 0, 0, 0, ?)
            """,
            (started, utc_now(), args.from_date, args.to_date, args.from_date, args.to_date, args.from_date, args.to_date, 24, args.scope, f"full_corpus recompute run_id={run_id}; extracted={extracted}; stats_rows={stats_rows}; lifecycle_rows={lifecycle_rows}"),
        )
    return {"extracted_terms_for_papers": extracted, "term_month_stats_rows": stats_rows, "lifecycle_rows": lifecycle_rows}


def qrows(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def cov(total: int, count: int) -> dict[str, Any]:
    return {"count": int(count), "ratio": round(count / total, 4) if total else 0.0}


def top_terms(conn, term_type: str, limit: int) -> list[dict[str, Any]]:
    return qrows(conn, """
        SELECT pt.normalized_term AS term, COUNT(DISTINCT pt.paper_id) AS count
        FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
        WHERE p.data_mode='real' AND pt.term_type=? AND pt.display_eligible=1
        GROUP BY pt.normalized_term ORDER BY count DESC, term ASC LIMIT ?
        """, (term_type, limit))


def top_physics(conn, limit: int) -> list[dict[str, Any]]:
    return qrows(conn, """
        SELECT pt.normalized_term AS term, COUNT(DISTINCT pt.paper_id) AS count
        FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
        LEFT JOIN concept_lifecycle l ON l.concept=pt.normalized_term
        WHERE p.data_mode='real' AND pt.term_type='concept' AND pt.display_eligible=1
          AND COALESCE(l.concept_class, 'physics_concept')='physics_concept'
        GROUP BY pt.normalized_term ORDER BY count DESC, term ASC LIMIT ?
        """, (limit,))



def chunk_status_counts(conn, run_id: str | None = None) -> dict[str, int]:
    params: tuple[Any, ...] = ()
    where = ""
    if run_id is not None:
        where = "WHERE run_id=?"
        params = (run_id,)
    counts = {"completed": 0, "failed": 0, "pending": 0, "running": 0, "skipped": 0, "total": 0}
    for row in qrows(conn, f"SELECT COALESCE(status, 'pending') AS status, COUNT(*) AS n FROM ingest_chunks {where} GROUP BY COALESCE(status, 'pending')", params):
        status = str(row["status"] or "pending")
        key = "completed" if status == "finished" else status
        if key in counts:
            counts[key] += int(row["n"] or 0)
        counts["total"] += int(row["n"] or 0)
    return counts


def filtered_generic_examples(conn, limit: int = 20) -> list[dict[str, Any]]:
    return qrows(conn, """
        SELECT pt.normalized_term AS term, pt.term_type, pt.display_reason, COUNT(DISTINCT pt.paper_id) AS count
        FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
        WHERE p.data_mode='real' AND pt.display_eligible=0
        GROUP BY pt.normalized_term, pt.term_type, pt.display_reason
        ORDER BY count DESC, term ASC LIMIT ?
        """, (limit,))


def term_status(conn, term: str) -> dict[str, Any]:
    normalized = normalize_term(term)
    stats = qrows(conn, """
        SELECT pt.term_type, pt.display_eligible, pt.display_reason, COUNT(DISTINCT pt.paper_id) AS papers
        FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id
        WHERE p.data_mode='real' AND pt.normalized_term=?
        GROUP BY pt.term_type, pt.display_eligible, pt.display_reason
        ORDER BY papers DESC
        """, (normalized,))
    return {"term": term, "normalized_term": normalized, "total_papers": sum(int(item["papers"] or 0) for item in stats), "breakdown": stats}


def nonzero_months(conn, term: str) -> dict[str, Any]:
    normalized = normalize_term(term)
    months = qrows(conn, """
        SELECT month, SUM(raw_freq) AS raw_freq, SUM(weighted_freq) AS weighted_freq
        FROM term_month_stats
        WHERE data_mode='real' AND corpus_scope='core' AND term=? AND raw_freq > 0
        GROUP BY month
        ORDER BY month
        """, (normalized,))
    return {"term": term, "normalized_term": normalized, "nonzero_month_count": len(months), "months": months[:36]}

def make_report(run_id: str, warnings: list[str]) -> dict[str, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = export_dir() / f"full_corpus_report_{stamp}"
    base.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        init_db(conn)
        total = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real'") or 0)
        mock_total = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='mock'") or 0)
        report: dict[str, Any] = {
            "run_id": run_id,
            "generated_at": utc_now(),
            "db_path": str(db_path()),
            "total_real_papers": total,
            "total_mock_papers": mock_total,
            "chunk_status_current_run": chunk_status_counts(conn, run_id),
            "chunk_status_all": chunk_status_counts(conn),
            "by_journal": qrows(conn, "SELECT journal, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY journal ORDER BY count DESC"),
            "by_year": qrows(conn, "SELECT year, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY year ORDER BY year"),
            "by_source": qrows(conn, "SELECT source, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY source ORDER BY count DESC"),
            "doi_coverage": cov(total, int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(doi, '') != ''") or 0)),
            "abstract_coverage": cov(total, int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(abstract, '') != ''") or 0)),
            "pdf_url_coverage": cov(total, int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real' AND COALESCE(pdf_url, '') != ''") or 0)),
            "condmat_confidence_breakdown": qrows(conn, "SELECT COALESCE(condmat_confidence, 'unknown') AS confidence, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY COALESCE(condmat_confidence, 'unknown') ORDER BY count DESC"),
            "top_50_concepts": top_terms(conn, "concept", 50),
            "top_50_physics_concepts": top_physics(conn, 50),
            "top_50_materials": top_terms(conn, "material", 50),
            "top_50_methods": top_terms(conn, "method", 50),
            "filtered_generic_terms_count": int(scalar(conn, "SELECT COUNT(*) FROM paper_terms WHERE display_eligible=0") or 0),
            "filtered_generic_term_examples": filtered_generic_examples(conn),
            "watch_term_status": [term_status(conn, term) for term in WATCH_TERMS],
            "nonzero_time_series": [nonzero_months(conn, term) for term in TIMESERIES_TERMS],
            "next_command": ".venv\\Scripts\\python.exe -m backend.ingest_full_corpus --from 2015-01-01 --to 2026-07-10 --scope core --chunk yearly --resume --prefer crossref --fallback none --max-pages-per-chunk 50 --sleep-seconds 1",
            "failed_chunks": qrows(conn, "SELECT id, run_id, journal, date_from, date_to, error_summary, log_path FROM ingest_chunks WHERE status='failed' ORDER BY journal, date_from"),
            "warnings": warnings,
        }
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    csv_path = base.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")
    write_csv(csv_path, report)
    return {"markdown": str(md_path), "json": str(json_path), "csv": str(csv_path)}


def render_md(report: dict[str, Any]) -> str:
    lines = ["# Full Corpus Report", "", f"- generated_at: {report['generated_at']}", f"- run_id: {report['run_id']}", f"- db_path: {report['db_path']}", f"- total_real_papers: {report['total_real_papers']}", f"- total_mock_papers: {report.get('total_mock_papers', 0)}", f"- chunk_status_current_run: {report.get('chunk_status_current_run', {})}", f"- chunk_status_all: {report.get('chunk_status_all', {})}"]
    for key in ("doi_coverage", "abstract_coverage", "pdf_url_coverage"):
        item = report[key]
        lines.append(f"- {key}: {item['count']} ({item['ratio']:.2%})")
    for section in ("by_source", "by_journal", "by_year", "condmat_confidence_breakdown", "top_50_concepts", "top_50_physics_concepts", "top_50_materials", "top_50_methods"):
        lines += ["", f"## {section}", ""]
        for item in report.get(section, [])[:50]:
            label = item.get("term") or item.get("journal") or item.get("year") or item.get("source") or item.get("confidence")
            lines.append(f"- {label}: {item.get('count')}")
    lines += ["", "## filtered_generic_term_examples", ""]
    lines += [f"- {x['term']} ({x['term_type']}): {x['count']} / {x.get('display_reason') or ''}" for x in report.get("filtered_generic_term_examples", [])] or ["- none"]
    lines += ["", "## watch_term_status", ""]
    lines += [f"- {x['term']}: {x['total_papers']}" for x in report.get("watch_term_status", [])] or ["- none"]
    lines += ["", "## nonzero_time_series", ""]
    lines += [f"- {x['term']}: {x['nonzero_month_count']} months" for x in report.get("nonzero_time_series", [])] or ["- none"]
    lines += ["", "## failed_chunks", ""]
    lines += [f"- {x['journal']} {x['date_from']}..{x['date_to']}: {x.get('error_summary') or ''}" for x in report["failed_chunks"]] or ["- none"]
    if report.get("next_command"):
        lines += ["", "## next_command", "", f"```powershell\n{report['next_command']}\n```"]
    if report.get("warnings"):
        lines += ["", "## warnings", ""] + [f"- {item}" for item in report["warnings"]]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, report: dict[str, Any]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "key", "value", "extra"])
        writer.writeheader()
        writer.writerow({"section": "summary", "key": "total_real_papers", "value": report["total_real_papers"], "extra": ""})
        writer.writerow({"section": "summary", "key": "total_mock_papers", "value": report.get("total_mock_papers", 0), "extra": ""})
        for key in ("doi_coverage", "abstract_coverage", "pdf_url_coverage"):
            writer.writerow({"section": "coverage", "key": key, "value": report[key]["count"], "extra": report[key]["ratio"]})
        for section in ("by_source", "by_journal", "by_year", "condmat_confidence_breakdown", "top_50_concepts", "top_50_physics_concepts", "top_50_materials", "top_50_methods"):
            for item in report.get(section, []):
                key = item.get("term") or item.get("journal") or item.get("year") or item.get("source") or item.get("confidence")
                writer.writerow({"section": section, "key": key, "value": item.get("count"), "extra": ""})
        for item in report.get("failed_chunks", []):
            writer.writerow({"section": "failed_chunks", "key": item.get("id"), "value": item.get("journal"), "extra": item.get("error_summary")})



def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    explicit_prefer = args.prefer is not None
    if args.prefer is None:
        if openalex_api_key():
            args.prefer = "openalex"
            args.fallback = args.fallback or "crossref_arxiv"
            args.auto_mode_message = ""
        else:
            args.prefer = "crossref"
            args.fallback = args.fallback or "none"
            args.auto_mode_message = "OpenAlex API key missing; using Crossref-only mode."
    else:
        args.fallback = args.fallback or ("crossref_arxiv" if args.prefer == "openalex" else "none")
        args.auto_mode_message = ""
    if args.fallback == "crossref":
        args.fallback = "crossref_arxiv"
    args.prefer_was_explicit = explicit_prefer
    if args.max_pages_per_chunk < 0:
        raise ValueError("--max-pages-per-chunk must be >= 0")
    if args.rows_per_page < 1:
        raise ValueError("--rows-per-page must be >= 1")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be >= 1")
    if args.chunk_timeout_minutes < 0:
        raise ValueError("--chunk-timeout-minutes must be >= 0")
    return args

def run(args: argparse.Namespace) -> dict[str, Any]:
    args = resolve_args(args)
    chunks = build_chunks(args)
    if args.dry_run:
        result = {"dry_run": True, "chunk_count": len(chunks), "chunks": [asdict(item) for item in chunks]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    ensure_data_layout()
    run_id = str(uuid.uuid4())
    warnings: list[str] = []
    if getattr(args, "auto_mode_message", ""):
        warnings.append(args.auto_mode_message)
        print(args.auto_mode_message, file=sys.stderr)
    if args.report_only:
        recompute = recompute_stats(args, run_id) if args.recompute else None
        reports = make_report(run_id, warnings)
        result = {"run_id": run_id, "status": "report_only", "recompute": recompute, "reports": reports}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    lock = IngestLock()
    try:
        lock.acquire(force=args.force, command=sys.argv)
    except IngestLockError as exc:
        raise SystemExit(str(exc)) from exc
    totals = {"fetched_count": 0, "kept_count": 0, "deduped_count": 0, "failed_count": 0, "inserted": 0, "updated": 0}
    errors: list[dict[str, Any]] = []
    try:
        with connect() as conn:
            init_db(conn)
            start_run(conn, run_id, args, chunks)
        ensure_pending(run_id, chunks, args.force)
        todo = eligible_chunks(chunks, args)
        source_cache: dict[str, str] = {}
        for chunk in todo:
            if remaining_budget(args, totals) == 0:
                break
            try:
                item = process_chunk(chunk, run_id, args, source_cache, totals)
                merge(totals, item)
                errors.extend(item.get("errors") or [])
                if item.get("status") == "failed":
                    errors.append({"chunk_id": chunk.id, "journal": chunk.journal, "date_from": chunk.date_from, "date_to": chunk.date_to, "status": "failed"})
            except Exception as exc:
                errors.append({"chunk_id": chunk.id, "journal": chunk.journal, "date_from": chunk.date_from, "date_to": chunk.date_to, "type": type(exc).__name__, "message": str(exc)})
                with connect() as conn:
                    init_db(conn)
                    row = read_chunk(conn, chunk.id) or chunk_row(chunk, run_id)
                    upsert_chunk(conn, {**row, "run_id": run_id, "status": "failed", "finished_at": utc_now(), "failed_count": int(row.get("failed_count") or 0) + 1, "error_summary": f"{type(exc).__name__}: {exc}"})
        recompute = recompute_stats(args, run_id)
        reports = make_report(run_id, warnings)
        status = "finished" if not errors else "partial" if totals["kept_count"] > 0 else "failed"
        with connect() as conn:
            init_db(conn)
            finish_run(conn, run_id, status, totals, summarize_errors(errors[:20], warnings))
        result = {"run_id": run_id, "status": status, **totals, "processed_chunks": len(todo), "planned_chunks": len(chunks), "errors": errors[:20], "recompute": recompute, "reports": reports}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        lock.release()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest the full Core condensed-matter metadata corpus by journal/time chunk.")
    parser.add_argument("--from", dest="from_date", default="2015-01-01")
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat())
    parser.add_argument("--scope", choices=["core", "context", "core_context", "all"], default="core")
    parser.add_argument("--chunk", choices=["yearly", "quarterly", "monthly"], default="yearly")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--prefer", choices=["openalex", "crossref"], default=None)
    parser.add_argument("--fallback", choices=["crossref", "crossref_arxiv", "none"], default=None)
    parser.add_argument("--max-papers", type=int, default=0)
    parser.add_argument("--max-chunks", type=int, default=0)
    parser.add_argument("--journal", default="")
    parser.add_argument("--sleep-seconds", type=float, default=0.12)
    parser.add_argument("--strict-condmat", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recompute", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--rows-per-page", type=int, default=100)
    parser.add_argument("--max-pages-per-chunk", type=int, default=50)
    parser.add_argument("--chunk-timeout-minutes", type=float, default=20)
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()

def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
