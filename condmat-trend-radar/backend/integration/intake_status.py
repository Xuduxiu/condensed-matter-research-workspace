from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from backend.config import paper_downloader_inboxes


TASK_PATTERNS = (
    "download_tasks_*.json",
    "trend_radar_download_tasks_*.json",
    "download_tasks_*.csv",
    "trend_radar_download_tasks_*.csv",
)
RECEIPT_SCHEMA_VERSION = "paper-intake-task-receipt-v1"
EVENT_SCHEMA_VERSION = "paper-intake-task-event-v1"
LIFECYCLE_STATUSES = ("oa_resolved", "downloaded", "exported")


def list_task_files(inbox_dirs: Iterable[Path] | None = None) -> list[Path]:
    batches: dict[tuple[str, str], Path] = {}
    for inbox in inbox_dirs or paper_downloader_inboxes():
        if not inbox.exists():
            continue
        for pattern in TASK_PATTERNS:
            for path in inbox.glob(pattern):
                if ".manifest." in path.name.lower():
                    continue
                key = (_path_key(path.parent), path.stem.casefold())
                existing = batches.get(key)
                if existing is None or (
                    path.suffix.lower() == ".json"
                    and existing.suffix.lower() != ".json"
                ):
                    batches[key] = path
    return sorted(
        batches.values(),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def task_receipt_path(source_path: str | Path) -> Path:
    source = Path(source_path).expanduser().resolve()
    return source.parent / ".receipts" / f"{source.name}.imported.json"


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_intake_queue(
    inbox_dirs: Iterable[Path] | None = None,
    *,
    max_batches: int = 50,
) -> dict[str, Any]:
    inboxes = _dedupe_paths(inbox_dirs or paper_downloader_inboxes())
    task_files = list_task_files(inboxes)
    event_index, lifecycle_counts = _index_lifecycle_events(task_files)
    imported_batches = 0
    invalid_batches = 0
    batches: list[dict[str, Any]] = []

    for source in task_files:
        task_count, task_error = _task_count(source)
        if task_error:
            invalid_batches += 1
        receipt = _receipt_status(source)
        if receipt["imported"]:
            imported_batches += 1
        source_lifecycle = event_index.get(
            _path_key(source),
            _empty_lifecycle_counts(),
        )
        batches.append(
            {
                "source_file": source.name,
                "source_path": str(source),
                "task_count": task_count,
                "task_error": task_error,
                **receipt,
                "lifecycle_counts": source_lifecycle,
            }
        )

    latest = batches[0] if batches else None
    return {
        "ok": True,
        "inbox_paths": [str(path) for path in inboxes],
        "total_batches": len(task_files),
        "imported_batches": imported_batches,
        "pending_batches": len(task_files) - imported_batches,
        "invalid_batches": invalid_batches,
        "latest_path": latest["source_path"] if latest else None,
        "latest_imported": bool(latest and latest["imported"]),
        "lifecycle_counts": lifecycle_counts,
        "batches": batches[: max(0, max_batches)],
    }


def _receipt_status(source: Path) -> dict[str, Any]:
    receipt_path = task_receipt_path(source)
    base = {
        "receipt_path": str(receipt_path),
        "imported": False,
        "imported_at": None,
        "import_reason": "receipt_missing",
    }
    if not receipt_path.exists():
        return base
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**base, "import_reason": "receipt_invalid"}
    imported_at = _text(payload.get("imported_at")) or None
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return {
            **base,
            "imported_at": imported_at,
            "import_reason": "receipt_schema_mismatch",
        }
    try:
        current_hash = file_sha256(source)
    except OSError:
        return {
            **base,
            "imported_at": imported_at,
            "import_reason": "source_unreadable",
        }
    if payload.get("source_sha256") != current_hash:
        return {
            **base,
            "imported_at": imported_at,
            "import_reason": "source_changed",
        }
    return {
        **base,
        "imported": True,
        "imported_at": imported_at,
        "import_reason": "imported",
    }


def _task_count(path: Path) -> tuple[int | None, str | None]:
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                payload = payload.get("tasks") or payload.get("items") or []
            if not isinstance(payload, list):
                return None, "invalid_json_shape"
            return sum(1 for row in payload if isinstance(row, dict)), None
        if path.suffix.lower() == ".csv":
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                return sum(1 for _ in csv.DictReader(handle)), None
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        return None, type(exc).__name__
    return None, "unsupported_file_type"


def _index_lifecycle_events(
    task_files: list[Path],
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    known_sources = {_path_key(path): path for path in task_files}
    per_source_ids = {
        key: {status: set() for status in LIFECYCLE_STATUSES}
        for key in known_sources
    }
    per_source_events = {key: 0 for key in known_sources}
    global_ids = {status: set() for status in LIFECYCLE_STATUSES}
    event_count = 0
    invalid_events = 0
    event_dirs = _dedupe_paths(
        source.parent / ".receipts" / "events" for source in task_files
    )

    for event_dir in event_dirs:
        if not event_dir.exists():
            continue
        for event_path in sorted(event_dir.glob("*.json")):
            try:
                payload = json.loads(event_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                invalid_events += 1
                continue
            status = _text(payload.get("status"))
            task_id = _text(payload.get("task_id"))
            source_value = _text(payload.get("source_path"))
            if (
                payload.get("schema_version") != EVENT_SCHEMA_VERSION
                or status not in LIFECYCLE_STATUSES
                or not task_id
                or not source_value
            ):
                invalid_events += 1
                continue
            source_key = _path_key(Path(source_value).expanduser())
            if source_key not in known_sources:
                continue
            per_source_ids[source_key][status].add(task_id)
            per_source_events[source_key] += 1
            global_ids[status].add(task_id)
            event_count += 1

    per_source = {
        key: {
            **{
                status: len(status_task_ids)
                for status, status_task_ids in status_ids.items()
            },
            "event_count": per_source_events[key],
            "invalid_events": 0,
        }
        for key, status_ids in per_source_ids.items()
    }
    aggregate = {
        **{status: len(task_ids) for status, task_ids in global_ids.items()},
        "event_count": event_count,
        "invalid_events": invalid_events,
    }
    return per_source, aggregate


def _empty_lifecycle_counts() -> dict[str, int]:
    return {
        **{status: 0 for status in LIFECYCLE_STATUSES},
        "event_count": 0,
        "invalid_events": 0,
    }


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    output: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _path_key(path)
        if key not in seen:
            seen.add(key)
            output.append(path)
    return output


def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve()).casefold()


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
