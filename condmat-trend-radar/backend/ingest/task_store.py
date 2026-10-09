from __future__ import annotations

import json
import traceback
from argparse import Namespace
from pathlib import Path
from typing import Any

from backend.config import logs_dir
from backend.db.database import connect, init_db, utc_now
from backend.ingest_real_quickstart import run as run_real_quickstart


def tasks_dir() -> Path:
    path = logs_dir() / "ingest_tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_path(task_id: str) -> Path:
    return tasks_dir() / f"{task_id}.json"


def write_task(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = {**payload, "task_id": task_id, "updated_at": utc_now()}
    task_path(task_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_task(task_id: str) -> dict[str, Any] | None:
    path = task_path(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))




def existing_real_count() -> int:
    with connect() as conn:
        init_db(conn)
        return int(conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"])


def finished_existing_task(task_id: str, config: dict[str, Any], real_count: int) -> dict[str, Any]:
    now = utc_now()
    return write_task(
        task_id,
        {
            "status": "finished",
            "fetched_count": 0,
            "kept_count": 0,
            "source_breakdown": {"openalex": 0, "crossref": 0, "arxiv": 0},
            "error_summary": f"已有 {real_count} 篇真实 metadata，未重复导入。",
            "log_path": "",
            "config": config,
            "started_at": now,
            "finished_at": now,
            "real_paper_count_after": real_count,
        },
    )
def create_task(task_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return write_task(
        task_id,
        {
            "status": "running",
            "fetched_count": 0,
            "kept_count": 0,
            "source_breakdown": {"openalex": 0, "crossref": 0, "arxiv": 0},
            "error_summary": "",
            "log_path": "",
            "config": config,
            "started_at": utc_now(),
        },
    )


def run_task(task_id: str, config: dict[str, Any]) -> None:
    try:
        target = int(config.get("target") or 3000)
        real_count = existing_real_count()
        if real_count >= target:
            finished_existing_task(task_id, config, real_count)
            return
        write_task(task_id, {**(read_task(task_id) or {}), "status": "running"})
        args = Namespace(
            target=target,
            from_date=str(config.get("from") or config.get("from_date") or "2023-01-01"),
            to_date=str(config.get("to") or config.get("to_date") or "2026-07-10"),
            scope=str(config.get("scope") or "core"),
            prefer=str(config.get("prefer") or "openalex"),
            fallback=str(config.get("fallback") or "crossref_arxiv"),
            strict_condmat=bool(config.get("strict_condmat", False)),
            timeout=int(config.get("timeout") or 20),
        )
        result = run_real_quickstart(args, task_id=task_id)
        write_task(task_id, {**result, "status": "finished" if result.get("kept_count", 0) > 0 else "failed"})
    except Exception as exc:
        write_task(
            task_id,
            {
                **(read_task(task_id) or {}),
                "status": "failed",
                "error_summary": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
