from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config import ensure_data_layout, locks_dir, logs_dir
from backend.db.database import connect, init_db, utc_now
from backend.ingest.lock import is_pid_running, read_lock


RECOVERABLE_MARKS = {"pending"}


def recovery_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = logs_dir() / "recovery" / f"ingest_recover_{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_log(path: Path, event: str, payload: dict[str, Any]) -> None:
    record = {"ts": utc_now(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def lock_state() -> dict[str, Any]:
    path = locks_dir() / "ingest.lock"
    if not path.exists():
        return {"path": str(path), "exists": False, "pid": 0, "active": False, "payload": {}}
    payload = read_lock(path)
    pid = int(payload.get("pid") or 0)
    active = bool(pid and is_pid_running(pid))
    return {"path": str(path), "exists": True, "pid": pid, "active": active, "payload": payload}


def running_chunks() -> list[dict[str, Any]]:
    with connect() as conn:
        init_db(conn)
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, run_id, journal, date_from, date_to, fetched_count, kept_count, deduped_count, failed_count, started_at, log_path
                FROM ingest_chunks
                WHERE status='running'
                ORDER BY COALESCE(started_at, date_from)
                """
            ).fetchall()
        ]


def mark_running_chunks_pending(chunks: list[dict[str, Any]], reason: str) -> int:
    if not chunks:
        return 0
    ids = [chunk["id"] for chunk in chunks]
    with connect() as conn:
        init_db(conn)
        conn.executemany(
            """
            UPDATE ingest_chunks
            SET status='pending', finished_at=NULL,
                error_summary=TRIM(COALESCE(error_summary, '') || CASE WHEN COALESCE(error_summary, '')='' THEN '' ELSE '; ' END || ?)
            WHERE id=? AND status='running'
            """,
            [(reason, chunk_id) for chunk_id in ids],
        )
        return int(conn.total_changes)


def recover(mark: str, force: bool) -> dict[str, Any]:
    ensure_data_layout()
    log_path = recovery_log_path()
    lock = lock_state()
    chunks = running_chunks()
    write_log(log_path, "recover_started", {"mark": mark, "force": force, "lock": lock, "running_chunks": chunks})
    if lock["active"]:
        result = {
            "status": "blocked_active_ingest",
            "message": f"active ingest lock pid={lock['pid']}; no chunks changed",
            "lock": lock,
            "running_chunks": chunks,
            "marked_chunks": 0,
            "log_path": str(log_path),
        }
        write_log(log_path, "recover_blocked", result)
        return result
    if mark not in RECOVERABLE_MARKS:
        raise ValueError(f"unsupported --mark: {mark}")
    if lock["exists"] and force:
        Path(lock["path"]).unlink(missing_ok=True)
        lock["removed"] = True
    elif lock["exists"] and not force:
        result = {
            "status": "blocked_stale_lock",
            "message": "stale ingest lock exists; rerun with --force to remove it",
            "lock": lock,
            "running_chunks": chunks,
            "marked_chunks": 0,
            "log_path": str(log_path),
        }
        write_log(log_path, "recover_blocked", result)
        return result
    marked = mark_running_chunks_pending(chunks, "recovered: no active ingest process; marked pending")
    result = {
        "status": "ok",
        "lock": lock,
        "running_chunks_before": len(chunks),
        "marked_chunks": marked,
        "log_path": str(log_path),
    }
    write_log(log_path, "recover_finished", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover stale full-ingest chunks without deleting paper data.")
    parser.add_argument("--mark", choices=sorted(RECOVERABLE_MARKS), default="pending")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(recover(**vars(parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()