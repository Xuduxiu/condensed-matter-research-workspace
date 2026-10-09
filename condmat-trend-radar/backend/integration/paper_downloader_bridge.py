from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.analytics.stats import build_download_tasks, compare_papers_query, papers_query
from backend.config import db_path, export_dir, paper_downloader_inbox
from backend.db.database import connect, init_db
from backend.integration.detect_downloader_project import detect
from backend.integration.task_schema import TASK_FIELDS, TASK_SCHEMA_VERSION
from backend.nlp.normalize import normalize_term


def export_to_downloader(
    concepts: list[str] | None = None,
    mode: str = "or",
    from_month: str = "2015-01",
    to_month: str = "2026-07",
    scope: str = "core",
    min_momentum: float = 0.0,
    journal: str = "",
    limit: int = 100,
    include_arxiv: bool = False,
    output_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    selected = [normalize_term(item) for item in (concepts or []) if item.strip()]
    effective_scope = "all" if include_arxiv else scope
    with connect() as conn:
        init_db(conn)
        if len(selected) > 1:
            papers = compare_papers_query(conn, selected, mode=mode, from_month=from_month, to_month=to_month, scope=effective_scope, limit=max(limit, 250))
        elif len(selected) == 1:
            papers = papers_query(conn, concept=selected[0], from_date=from_month, to_date=to_month, scope=effective_scope, limit=max(limit, 250))
        else:
            papers = papers_query(conn, from_date=from_month, to_date=to_month, scope=effective_scope, limit=max(limit, 250))
    if journal:
        papers = [paper for paper in papers if paper.get("journal") == journal]
    tasks = build_download_tasks(papers, selected_concepts=selected)
    tasks = [task for task in tasks if float(task.get("momentum") or 0.0) >= min_momentum][:limit]

    target_dir = Path(output_dir) if output_dir else (export_dir() / "download_tasks_preview" if dry_run else paper_downloader_inbox())
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = target_dir / f"trend_radar_download_tasks_{stamp}.csv"
    json_path = target_dir / f"trend_radar_download_tasks_{stamp}.json"
    manifest_path = target_dir / "manifest.json"
    write_tasks(csv_path, json_path, tasks)
    manifest = {
        "schema_version": TASK_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_db": str(db_path()),
        "concept_filters": selected,
        "mode": mode,
        "from": from_month,
        "to": to_month,
        "scope": effective_scope,
        "task_count": len(tasks),
        "dry_run": dry_run,
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "task_count": len(tasks),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "manifest_path": str(manifest_path),
        "message": "已导出下载任务。请在自动论文下载项目中导入该任务文件。",
        "dry_run": dry_run,
    }


def write_tasks(csv_path: Path, json_path: Path, tasks: list[dict[str, Any]]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=TASK_FIELDS)
        writer.writeheader()
        for task in tasks:
            writer.writerow({field: task.get(field, "") for field in TASK_FIELDS})
    json_path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_downloader(root: str | Path) -> dict[str, Any]:
    return detect(root)
