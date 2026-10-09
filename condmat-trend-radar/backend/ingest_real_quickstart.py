from __future__ import annotations

import argparse
import json
import math
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats
from backend.config import ensure_data_layout, logs_dir, openalex_api_key
from backend.db.database import connect, init_db, normalized_title_key, upsert_paper, utc_now
from backend.ingest.arxiv_client import ARXIV_CATEGORIES, ArxivClient
from backend.ingest.check_openalex_quota import check_quota
from backend.ingest.crossref_client import CrossrefClient
from backend.ingest.journal_registry import journals_for_scope
from backend.ingest.openalex_client import MISSING_KEY_WARNING, OpenAlexClient, OpenAlexHTTPError
from backend.nlp.condmat_filter import is_condensed_matter_record
from backend.nlp.dictionaries import CORE_JOURNALS
from backend.nlp.normalize import lookup_key


OPENALEX_CONCEPT_HINTS = {
    "condensed matter physics",
    "materials science",
    "quantum materials",
    "nanotechnology",
    "superconductivity",
    "magnetism",
    "semiconductor",
}
RELAXED_JOURNALS = {"Nature Materials", "Nature Physics", "npj Quantum Materials", "Physical Review X", "Physical Review Letters"}
RELAXED_TEXT_HINTS = {
    "condensed matter", "quantum material", "superconduct", "magnet", "topological", "semimetal", "insulator",
    "2d material", "two dimensional", "moire", "moiré", "graphene", "spin", "exciton", "phonon", "ferroelectric",
    "transport", "hall", "chern", "weyl", "dirac", "mott", "hubbard", "kagome", "van der waals",
}


class RunLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict[str, Any]) -> None:
        record = {"ts": utc_now(), "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(args: argparse.Namespace, task_id: str | None = None) -> dict[str, Any]:
    ensure_data_layout()
    started_at = utc_now()
    run_id = task_id or str(uuid.uuid4())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir() / f"real_quickstart_{stamp}.log"
    logger = RunLogger(log_path)
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []
    source_breakdown: Counter[str] = Counter()
    fetched_breakdown: Counter[str] = Counter()
    totals = {"fetched_count": 0, "kept_count": 0, "deduped_count": 0, "failed_count": 0, "inserted": 0, "updated": 0}

    logger.write("started", {"run_id": run_id, "config": vars(args)})
    with connect() as conn:
        init_db(conn)
        before_real = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
        start_ingest_run(conn, run_id, started_at, args)

        seen_doi = {row["doi"] for row in conn.execute("SELECT doi FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchall()}
        seen_title = {normalized_title_key(row["title"] or "") for row in conn.execute("SELECT title FROM papers WHERE title IS NOT NULL").fetchall()}

        used_openalex = False
        if args.prefer == "openalex":
            openalex_key = openalex_api_key()
            if not openalex_key:
                warnings.append(MISSING_KEY_WARNING)
                logger.write("openalex_skipped", {"reason": "missing_api_key", "message": MISSING_KEY_WARNING})
            else:
                quota = check_quota(timeout=args.timeout)
                logger.write("openalex_quota", quota)
                if not quota.get("ok"):
                    warnings.append(str(quota.get("message") or "OpenAlex quota check failed"))
                try:
                    result = ingest_openalex(conn, args, seen_doi, seen_title, logger)
                    merge_counts(totals, result)
                    source_breakdown.update(result["source_breakdown"])
                    fetched_breakdown.update(result["fetched_breakdown"])
                    errors.extend(result["errors"])
                    used_openalex = result["kept_count"] > 0
                except Exception as exc:
                    item = {"source": "openalex", "type": type(exc).__name__, "message": str(exc)}
                    errors.append(item)
                    totals["failed_count"] += 1
                    logger.write("openalex_failed", item)

        should_fallback = args.fallback == "crossref_arxiv" and totals["kept_count"] < args.target
        if should_fallback:
            remaining = max(0, args.target - totals["kept_count"])
            crossref_target = min(max(1, math.ceil(args.target / 2)), remaining)
            crossref_result = ingest_crossref(conn, args, crossref_target, seen_doi, seen_title, logger)
            merge_counts(totals, crossref_result)
            source_breakdown.update(crossref_result["source_breakdown"])
            fetched_breakdown.update(crossref_result["fetched_breakdown"])
            errors.extend(crossref_result["errors"])
            remaining = max(0, args.target - totals["kept_count"])
            if remaining > 0:
                arxiv_result = ingest_arxiv(conn, args, remaining, seen_doi, seen_title, logger)
                merge_counts(totals, arxiv_result)
                source_breakdown.update(arxiv_result["source_breakdown"])
                fetched_breakdown.update(arxiv_result["fetched_breakdown"])
                errors.extend(arxiv_result["errors"])

        extracted = rebuild_paper_terms(conn)
        stats_rows = rebuild_term_month_stats(conn)
        lifecycle_rows = rebuild_lifecycle(conn, args.from_date, args.to_date, args.from_date, args.to_date)
        cooccurrence_network(conn, min_count=2)
        after_real = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
        top = top_terms_snapshot(conn)
        status = "finished" if totals["kept_count"] > 0 else "failed"
        error_summary = summarize_errors(errors, warnings)
        finish_ingest_run(conn, run_id, status, totals, error_summary, args)
        conn.execute(
            """
            INSERT INTO update_runs
              (started_at, finished_at, from_date, to_date, baseline_from, baseline_to, trend_from, trend_to, trend_months, scope,
               requested_live, inserted_papers, updated_papers, duplicate_papers, failed_requests, used_mock_data, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, ?)
            """,
            (
                started_at, utc_now(), args.from_date, args.to_date, args.from_date, args.to_date, args.from_date, args.to_date, 24,
                args.scope, totals["inserted"], totals["updated"], totals["deduped_count"], totals["failed_count"],
                f"real quickstart; extracted={extracted}; stats_rows={stats_rows}; lifecycle_rows={lifecycle_rows}",
            ),
        )

    finished_at = utc_now()
    result = {
        "task_id": run_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "target": args.target,
        "fetched_count": totals["fetched_count"],
        "kept_count": totals["kept_count"],
        "deduped_count": totals["deduped_count"],
        "failed_count": totals["failed_count"],
        "source_used": "openalex" if used_openalex and not should_fallback else ("openalex+fallback" if used_openalex else "crossref_arxiv_fallback"),
        "source_breakdown": {"openalex": source_breakdown.get("openalex", 0), "crossref": source_breakdown.get("crossref", 0), "arxiv": source_breakdown.get("arxiv", 0)},
        "fetched_breakdown": dict(fetched_breakdown),
        "real_paper_count_before": before_real,
        "real_paper_count_after": after_real,
        "error_summary": summarize_errors(errors, warnings),
        "warnings": warnings,
        "errors": errors[:20],
        "log_path": str(log_path),
        "top_30_concepts": top["concepts"],
        "top_30_materials": top["materials"],
        "top_30_methods": top["methods"],
    }
    logger.write("finished", result)
    log_path.write_text(log_path.read_text(encoding="utf-8") + json.dumps({"summary": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def ingest_openalex(conn, args: argparse.Namespace, seen_doi: set[str], seen_title: set[str], logger: RunLogger) -> dict[str, Any]:
    client = OpenAlexClient(timeout=args.timeout, max_retries=1)
    result = empty_result()
    journals = list(CORE_JOURNALS)
    per_journal_goal = max(20, math.ceil(args.target / len(journals)) * 2)
    for journal in journals:
        if result["kept_count"] >= args.target:
            break
        try:
            source_id = client.find_source_id(journal)
            if not source_id:
                result["failed_count"] += 1
                result["errors"].append({"source": "openalex", "journal": journal, "error": "source_not_found"})
                continue
            cursor = "*"
            journal_kept = 0
            for page in range(50):
                if result["kept_count"] >= args.target or journal_kept >= per_journal_goal:
                    break
                payload = client.fetch_journal_page(journal, source_id, args.from_date, args.to_date, per_page=200, cursor=cursor)
                raw_results = payload.get("results", []) or []
                result["fetched_count"] += len(raw_results)
                result["fetched_breakdown"]["openalex"] += len(raw_results)
                for raw in raw_results:
                    paper = client._normalize_work(raw, journal)
                    stored = store_candidate(conn, paper, args.strict_condmat, seen_doi, seen_title)
                    add_store_result(result, stored, "openalex")
                    if stored.get("stored"):
                        journal_kept += 1
                    if result["kept_count"] >= args.target or journal_kept >= per_journal_goal:
                        break
                cursor = payload.get("meta", {}).get("next_cursor") or ""
                if not cursor or not raw_results:
                    break
            logger.write("openalex_journal", {"journal": journal, "kept": journal_kept, "total_kept": result["kept_count"]})
        except OpenAlexHTTPError as exc:
            result["failed_count"] += 1
            result["errors"].append({"source": "openalex", "journal": journal, **exc.to_dict()})
            logger.write("openalex_error", {"journal": journal, **exc.to_dict()})
            if exc.status == 429 or "Insufficient budget" in exc.response_text:
                break
        except Exception as exc:
            result["failed_count"] += 1
            result["errors"].append({"source": "openalex", "journal": journal, "type": type(exc).__name__, "message": str(exc)})
    return result


def ingest_crossref(conn, args: argparse.Namespace, target: int, seen_doi: set[str], seen_title: set[str], logger: RunLogger) -> dict[str, Any]:
    result = empty_result()
    if target <= 0:
        return result
    client = CrossrefClient(timeout=args.timeout)
    journals = journals_for_scope(args.scope)
    per_journal_goal = max(25, math.ceil(target / len(journals)) * 2)
    for journal in journals:
        if result["kept_count"] >= target:
            break
        papers, errors = client.fetch_journal_works(journal, args.from_date, args.to_date, target=per_journal_goal, rows=100, max_pages=30)
        result["fetched_count"] += len(papers)
        result["fetched_breakdown"]["crossref"] += len(papers)
        result["errors"].extend(errors)
        result["failed_count"] += len(errors)
        journal_kept = 0
        for paper in papers:
            stored = store_candidate(conn, paper, args.strict_condmat, seen_doi, seen_title)
            add_store_result(result, stored, "crossref")
            if stored.get("stored"):
                journal_kept += 1
            if result["kept_count"] >= target:
                break
        logger.write("crossref_journal", {"journal": journal.canonical_name, "fetched": len(papers), "kept": journal_kept, "errors": errors[:3]})
    return result


def ingest_arxiv(conn, args: argparse.Namespace, target: int, seen_doi: set[str], seen_title: set[str], logger: RunLogger) -> dict[str, Any]:
    result = empty_result()
    if target <= 0:
        return result
    max_results = min(5000, max(target * 2, target + 300))
    papers, failures = ArxivClient(timeout=args.timeout).fetch(args.from_date, args.to_date, max_results=max_results, categories=ARXIV_CATEGORIES)
    result["fetched_count"] += len(papers)
    result["fetched_breakdown"]["arxiv"] += len(papers)
    result["failed_count"] += failures
    if failures:
        result["errors"].append({"source": "arxiv", "failed_count": failures})
    kept = 0
    for paper in papers:
        stored = store_candidate(conn, paper, args.strict_condmat, seen_doi, seen_title)
        add_store_result(result, stored, "arxiv")
        if stored.get("stored"):
            kept += 1
        if result["kept_count"] >= target:
            break
    logger.write("arxiv", {"fetched": len(papers), "kept": kept, "failed_count": failures, "categories": ARXIV_CATEGORIES})
    return result


def empty_result() -> dict[str, Any]:
    return {"fetched_count": 0, "kept_count": 0, "deduped_count": 0, "failed_count": 0, "inserted": 0, "updated": 0, "source_breakdown": Counter(), "fetched_breakdown": Counter(), "errors": []}


def merge_counts(total: dict[str, int], item: dict[str, Any]) -> None:
    for key in ("fetched_count", "kept_count", "deduped_count", "failed_count", "inserted", "updated"):
        total[key] += int(item.get(key) or 0)


def add_store_result(result: dict[str, Any], stored: dict[str, Any], source: str) -> None:
    if stored.get("duplicate"):
        result["deduped_count"] += 1
    if stored.get("stored"):
        result["kept_count"] += 1
        result["source_breakdown"][source] += 1
        if stored.get("status") == "inserted":
            result["inserted"] += 1
        elif stored.get("status") == "updated":
            result["updated"] += 1


def store_candidate(conn, paper: dict[str, Any], strict_condmat: bool, seen_doi: set[str], seen_title: set[str]) -> dict[str, Any]:
    keep, confidence, reasons = classify_condmat(paper, strict_condmat=strict_condmat)
    if not keep:
        return {"stored": False, "duplicate": False, "confidence": confidence, "reasons": reasons}
    doi = (paper.get("doi") or "").lower()
    title_key = normalized_title_key(paper.get("title") or "")
    if doi and doi in seen_doi:
        return {"stored": False, "duplicate": True, "confidence": confidence, "reasons": reasons}
    if not doi and title_key and title_key in seen_title:
        return {"stored": False, "duplicate": True, "confidence": confidence, "reasons": reasons}
    if doi:
        seen_doi.add(doi)
    if title_key:
        seen_title.add(title_key)
    paper["data_mode"] = "real"
    paper["condmat_confidence"] = confidence
    paper["filter_reasons"] = reasons
    paper.setdefault("source_scope", "preprint" if paper.get("source") == "arxiv" else "published")
    status = upsert_paper(conn, paper)
    return {"stored": True, "duplicate": False, "status": status, "confidence": confidence, "reasons": reasons}


def classify_condmat(record: dict[str, Any], strict_condmat: bool = False) -> tuple[bool, str, list[str]]:
    reasons: list[str] = []
    keep, base_reasons = is_condensed_matter_record(record)
    reasons.extend(base_reasons)
    categories = [str(item) for item in record.get("categories", []) or []]
    if any(item.startswith("cond-mat.") for item in categories):
        return True, "high", [*reasons, "arxiv-cond-mat-direct"]
    if keep:
        confidence = "high" if any(reason.startswith("hard:") for reason in reasons) else "medium"
        return True, confidence, reasons
    if strict_condmat:
        return False, "excluded", [*reasons, "strict-filter-excluded"]

    concepts = {lookup_key(item) for item in record.get("concepts", []) or []}
    if any(hint in concept for concept in concepts for hint in OPENALEX_CONCEPT_HINTS):
        return True, "medium", [*reasons, "openalex-concept-relaxed"]

    text = lookup_key(" ".join(str(record.get(key, "")) for key in ("title", "abstract", "journal")))
    journal = str(record.get("journal") or "")
    if journal in RELAXED_JOURNALS and any(hint in text for hint in RELAXED_TEXT_HINTS):
        return True, "medium", [*reasons, "journal-relaxed-keyword"]
    return True, "low", [*reasons, "stored-low-confidence-for-review"]


def top_terms_snapshot(conn) -> dict[str, list[dict[str, Any]]]:
    output = {}
    for term_type, key in (("concept", "concepts"), ("material", "materials"), ("method", "methods")):
        output[key] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT pt.normalized_term AS term, COUNT(DISTINCT pt.paper_id) AS count
                FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
                WHERE p.data_mode='real' AND pt.term_type=? AND pt.display_eligible=1
                GROUP BY pt.normalized_term ORDER BY count DESC, term ASC LIMIT 30
                """,
                (term_type,),
            ).fetchall()
        ]
    return output


def start_ingest_run(conn, run_id: str, started_at: str, args: argparse.Namespace) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_runs
          (id, started_at, source, scope, baseline_from, baseline_to, status, config_json)
        VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
        """,
        (run_id, started_at, "real_quickstart", args.scope, args.from_date, args.to_date, json.dumps(vars(args), ensure_ascii=False)),
    )


def finish_ingest_run(conn, run_id: str, status: str, totals: dict[str, int], error_summary: str, args: argparse.Namespace) -> None:
    conn.execute(
        """
        UPDATE ingest_runs SET finished_at=?, status=?, fetched_count=?, kept_count=?, deduped_count=?, failed_count=?, error_summary=?
        WHERE id=?
        """,
        (utc_now(), status, totals["fetched_count"], totals["kept_count"], totals["deduped_count"], totals["failed_count"], error_summary, run_id),
    )


def summarize_errors(errors: list[dict[str, Any]], warnings: list[str]) -> str:
    parts = []
    if warnings:
        parts.append("warnings: " + "; ".join(warnings[:3]))
    if errors:
        parts.append("errors: " + json.dumps(errors[:5], ensure_ascii=False))
    return " | ".join(parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import the first batch of real condensed-matter metadata with OpenAlex -> Crossref/arXiv fallback.")
    parser.add_argument("--target", type=int, default=3000)
    parser.add_argument("--from", dest="from_date", default="2023-01-01")
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat())
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--prefer", choices=["openalex", "crossref"], default="openalex")
    parser.add_argument("--fallback", choices=["crossref_arxiv", "none"], default="crossref_arxiv")
    parser.add_argument("--strict-condmat", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
