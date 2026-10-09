from __future__ import annotations

import json
from typing import Any

from backend.config import db_path, export_dir
from backend.db.database import connect, init_db


API_CHUNK_STATUSES = ("completed", "failed", "pending", "running", "skipped")
DB_TO_API_STATUS = {"finished": "completed", "completed": "completed", "failed": "failed", "pending": "pending", "running": "running", "skipped": "skipped"}


def rows(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return row[0]


def latest_report_path() -> str:
    reports = sorted(export_dir().glob("full_corpus_report_*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    return str(reports[0]) if reports else ""


def chunk_counts(conn, run_id: str | None = None) -> dict[str, int]:
    counts = {status: 0 for status in API_CHUNK_STATUSES}
    counts["total"] = 0
    params: tuple[Any, ...] = ()
    where = ""
    if run_id:
        where = "WHERE run_id=?"
        params = (run_id,)
    for row in rows(conn, f"SELECT COALESCE(status, 'pending') AS status, COUNT(*) AS n FROM ingest_chunks {where} GROUP BY COALESCE(status, 'pending')", params):
        status = DB_TO_API_STATUS.get(str(row["status"] or "pending"), str(row["status"] or "pending"))
        if status in counts:
            counts[status] += int(row["n"] or 0)
        counts["total"] += int(row["n"] or 0)
    return counts


def compact(value: str, limit: int = 220) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "..."


def parse_error_items(error_summary: str) -> list[dict[str, Any]]:
    text = (error_summary or "").strip()
    if not text:
        return []
    if text.startswith("errors:"):
        text = text.split(":", 1)[1].strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def brief_error(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    summary = str(row.get("error_summary") or "")
    items = parse_error_items(summary)
    journal = row.get("journal") or "unknown journal"
    dates = f"{row.get('date_from') or '?'}..{row.get('date_to') or '?'}"
    if items:
        item = items[0]
        source = item.get("source") or "ingest"
        status = item.get("status")
        response = item.get("response") or ""
        detail = item.get("message") or item.get("error") or ""
        if response:
            try:
                response_json = json.loads(response)
                message = response_json.get("message") if isinstance(response_json, dict) else None
                if isinstance(message, dict):
                    detail = message.get("name") or message.get("description") or detail
                elif isinstance(message, str):
                    detail = message or detail
            except Exception:
                detail = response or detail
        if source == "crossref" and status:
            return compact(f"Crossref HTTP {status} while ingesting {journal} {dates}; see chunk log for raw response.")
        if status:
            return compact(f"{source} HTTP {status} while ingesting {journal} {dates}: {detail}")
        return compact(f"{source} error while ingesting {journal} {dates}: {detail}")
    return compact(summary)


def add_error_brief(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    output = dict(row)
    output["error_summary_brief"] = brief_error(output)
    return output


def collect_ingest_status() -> dict[str, Any]:
    with connect() as conn:
        init_db(conn)
        real_count = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='real'") or 0)
        mock_count = int(scalar(conn, "SELECT COUNT(*) FROM papers WHERE data_mode='mock'") or 0)
        current_run = conn.execute(
            """
            SELECT id, status, started_at, finished_at, error_summary, fetched_count, kept_count, deduped_count, failed_count
            FROM ingest_runs
            WHERE source='full_corpus'
            ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        current_run_id = current_run["id"] if current_run else ""
        current_counts = chunk_counts(conn, current_run_id) if current_run_id else {status: 0 for status in (*API_CHUNK_STATUSES, "total")}
        all_counts = chunk_counts(conn)
        last_error_row = conn.execute(
            """
            SELECT id, journal, date_from, date_to, error_summary, log_path, finished_at, started_at
            FROM ingest_chunks
            WHERE status='failed' OR COALESCE(error_summary, '') != ''
            ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1
            """
        ).fetchone()
        source_breakdown = rows(conn, "SELECT source, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY source ORDER BY count DESC")
        journal_breakdown = rows(conn, "SELECT journal, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY journal ORDER BY count DESC")
        year_breakdown = rows(conn, "SELECT year, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY year ORDER BY year")
        latest_chunks = rows(
            conn,
            """
            SELECT id, run_id, source, scope, journal, date_from, date_to, status, fetched_count, kept_count,
                   deduped_count, failed_count, error_summary, log_path
            FROM ingest_chunks
            ORDER BY COALESCE(finished_at, started_at, date_from) DESC LIMIT 12
            """,
        )
        return {
            "db_path": str(db_path()),
            "real_paper_count": real_count,
            "mock_paper_count": mock_count,
            "current_run_id": current_run_id,
            "current_run": dict(current_run) if current_run else None,
            "current_run_chunks": current_counts,
            "all_chunks": all_counts,
            "completed_chunks": all_counts.get("completed", 0),
            "failed_chunks": all_counts.get("failed", 0),
            "pending_chunks": all_counts.get("pending", 0),
            "running_chunks": all_counts.get("running", 0),
            "skipped_chunks": all_counts.get("skipped", 0),
            "source_breakdown": source_breakdown,
            "journal_breakdown": journal_breakdown,
            "year_breakdown": year_breakdown,
            "estimated_remaining_chunks": current_counts.get("pending", 0) + current_counts.get("failed", 0) + current_counts.get("running", 0),
            "last_error": add_error_brief(dict(last_error_row) if last_error_row else None),
            "latest_report_path": latest_report_path(),
            "recent_chunks": latest_chunks,
        }


def main() -> None:
    print(json.dumps(collect_ingest_status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()