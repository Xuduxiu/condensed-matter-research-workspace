from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Paper


EVENT_SCHEMA_VERSION = "paper-intake-task-event-v1"
LIFECYCLE_STATUSES = ("oa_resolved", "downloaded", "exported")


@dataclass(frozen=True)
class LifecycleWriteResult:
    event_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    linked_papers: int = 0
    unlinked_papers: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded_count": len(self.event_paths),
            "counts": dict(self.counts),
            "linked_papers": self.linked_papers,
            "unlinked_papers": self.unlinked_papers,
            "errors": list(self.errors),
        }


def task_event_dir(source_path: str | Path) -> Path:
    source = Path(source_path).expanduser().resolve()
    return source.parent / ".receipts" / "events"


def paper_task_link(paper: Paper) -> tuple[Path, str] | None:
    raw = paper.raw or {}
    source_value = raw.get("task_source_path")
    task = raw.get("task") or {}
    task_id = str(task.get("task_id") or paper.id).strip()
    if not source_value or not task_id:
        return None
    return Path(str(source_value)).expanduser().resolve(), task_id


def record_lifecycle_event(
    paper: Paper,
    status: str,
    *,
    export_id: str,
    export_path: str | Path,
    details: dict[str, Any] | None = None,
) -> Path:
    if status not in LIFECYCLE_STATUSES:
        raise ValueError(f"Unsupported lifecycle status: {status}")
    link = paper_task_link(paper)
    if link is None:
        raise ValueError("Paper is not linked to a Trend Radar task.")
    source_path, task_id = link
    if not source_path.exists():
        raise FileNotFoundError(f"Task source does not exist: {source_path}")

    event_id = str(uuid.uuid4())
    event_dir = task_event_dir(source_path)
    event_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "created_at": created_at,
        "status": status,
        "task_id": task_id,
        "paper_id": paper.id,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "export_id": export_id,
        "export_path": str(Path(export_path).resolve()),
        "pdf_status": paper.pdf_status,
        "details": details or {},
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    event_path = event_dir / f"{timestamp}_{event_id}.json"
    temp_path = event_path.with_name(f".{event_path.name}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(event_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return event_path


def record_export_lifecycle(
    papers: list[Paper],
    *,
    export_id: str,
    export_path: str | Path,
) -> LifecycleWriteResult:
    event_paths: list[Path] = []
    errors: list[str] = []
    counts: dict[str, int] = {status: 0 for status in LIFECYCLE_STATUSES}
    linked_papers = 0
    unlinked_papers = 0

    for paper in papers:
        if paper_task_link(paper) is None:
            unlinked_papers += 1
            continue
        linked_papers += 1
        statuses: list[str] = []
        if paper.pdf_url:
            statuses.append("oa_resolved")
        if paper.pdf_status == "downloaded":
            statuses.append("downloaded")
        statuses.append("exported")
        for status in statuses:
            try:
                event_path = record_lifecycle_event(
                    paper,
                    status,
                    export_id=export_id,
                    export_path=export_path,
                    details={
                        "doi": paper.doi,
                        "arxiv_id": paper.arxiv_id,
                        "local_pdf_path": paper.local_pdf_path,
                    },
                )
            except (OSError, ValueError) as exc:
                errors.append(f"{paper.id}:{status}: {exc}")
                continue
            event_paths.append(event_path)
            counts[status] += 1

    return LifecycleWriteResult(
        event_paths=event_paths,
        errors=errors,
        counts=counts,
        linked_papers=linked_papers,
        unlinked_papers=unlinked_papers,
    )


def read_task_events(source_path: str | Path) -> tuple[list[dict[str, Any]], int]:
    source = Path(source_path).expanduser().resolve()
    event_dir = task_event_dir(source)
    if not event_dir.exists():
        return [], 0
    events: list[dict[str, Any]] = []
    invalid = 0
    for path in sorted(event_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid += 1
            continue
        if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
            invalid += 1
            continue
        try:
            payload_source = Path(str(payload.get("source_path") or "")).resolve()
        except OSError:
            invalid += 1
            continue
        if payload_source != source:
            continue
        events.append(payload)
    return events, invalid


def summarize_task_events(source_path: str | Path) -> dict[str, int]:
    events, invalid = read_task_events(source_path)
    task_ids: dict[str, set[str]] = {
        status: set() for status in LIFECYCLE_STATUSES
    }
    for event in events:
        status = str(event.get("status") or "")
        task_id = str(event.get("task_id") or "")
        if status in task_ids and task_id:
            task_ids[status].add(task_id)
    return {
        **{status: len(task_ids[status]) for status in LIFECYCLE_STATUSES},
        "event_count": len(events),
        "invalid_events": invalid,
    }