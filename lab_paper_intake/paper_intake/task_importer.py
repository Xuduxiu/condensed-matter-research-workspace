from __future__ import annotations

import csv
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH, candidate_inbox_dirs
from .db import upsert_papers
from .dedup import deduplicate_papers
from .models import Paper, normalize_arxiv_id, normalize_doi, normalize_title
from .task_events import summarize_task_events


SUPPORTED_JSON_PATTERNS = ("download_tasks_*.json", "trend_radar_download_tasks_*.json")
SUPPORTED_CSV_PATTERNS = ("download_tasks_*.csv", "trend_radar_download_tasks_*.csv")
RECEIPT_SCHEMA_VERSION = "paper-intake-task-receipt-v1"


@dataclass(frozen=True)
class TaskImportStatus:
    source_path: Path
    receipt_path: Path
    imported: bool
    imported_at: str | None = None
    reason: str = "receipt_missing"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "receipt_path": str(self.receipt_path),
            "imported": self.imported,
            "imported_at": self.imported_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TaskImportResult:
    source_path: Path
    papers: list[Paper]
    input_count: int
    skipped_count: int = 0
    warnings: list[str] = field(default_factory=list)
    receipt_path: Path | None = None
    previously_imported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "source_path": str(self.source_path),
            "input_count": self.input_count,
            "imported_count": len(self.papers),
            "skipped_count": self.skipped_count,
            "warnings": self.warnings,
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
            "previously_imported": self.previously_imported,
        }


@dataclass(frozen=True)
class TaskQueueSummary:
    total_batches: int
    imported_batches: int
    pending_batches: int
    invalid_batches: int
    latest_path: Path | None
    latest_imported: bool
    lifecycle_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_batches": self.total_batches,
            "imported_batches": self.imported_batches,
            "pending_batches": self.pending_batches,
            "invalid_batches": self.invalid_batches,
            "latest_path": str(self.latest_path) if self.latest_path else None,
            "latest_imported": self.latest_imported,
            "lifecycle_counts": dict(self.lifecycle_counts),
        }


def list_task_files(inbox_dirs: Iterable[Path] | None = None) -> list[Path]:
    batches: dict[tuple[str, str], Path] = {}
    for inbox in inbox_dirs or candidate_inbox_dirs():
        if not inbox.exists():
            continue
        for pattern in (*SUPPORTED_JSON_PATTERNS, *SUPPORTED_CSV_PATTERNS):
            for path in inbox.glob(pattern):
                if ".manifest." in path.name.lower():
                    continue
                key = (str(path.parent.resolve()).casefold(), path.stem.casefold())
                existing = batches.get(key)
                if existing is None or (
                    path.suffix.lower() == ".json" and existing.suffix.lower() != ".json"
                ):
                    batches[key] = path
    return sorted(
        batches.values(),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def find_latest_task_file(inbox_dirs: Iterable[Path] | None = None) -> Path | None:
    task_files = list_task_files(inbox_dirs)
    return task_files[0] if task_files else None


def task_receipt_path(path: str | Path) -> Path:
    source_path = Path(path).expanduser().resolve()
    return source_path.parent / ".receipts" / f"{source_path.name}.imported.json"


def task_import_status(path: str | Path) -> TaskImportStatus:
    source_path = Path(path).expanduser().resolve()
    receipt_path = task_receipt_path(source_path)
    if not receipt_path.exists():
        return TaskImportStatus(source_path, receipt_path, False)
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TaskImportStatus(
            source_path,
            receipt_path,
            False,
            reason="receipt_invalid",
        )
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return TaskImportStatus(
            source_path,
            receipt_path,
            False,
            imported_at=_text(payload.get("imported_at")) or None,
            reason="receipt_schema_mismatch",
        )
    try:
        current_hash = file_sha256(source_path)
    except OSError:
        return TaskImportStatus(
            source_path,
            receipt_path,
            False,
            imported_at=_text(payload.get("imported_at")) or None,
            reason="source_unreadable",
        )
    if payload.get("source_sha256") != current_hash:
        return TaskImportStatus(
            source_path,
            receipt_path,
            False,
            imported_at=_text(payload.get("imported_at")) or None,
            reason="source_changed",
        )
    return TaskImportStatus(
        source_path,
        receipt_path,
        True,
        imported_at=_text(payload.get("imported_at")) or None,
        reason="imported",
    )


def summarize_task_queue(
    inbox_dirs: Iterable[Path] | None = None,
) -> TaskQueueSummary:
    task_files = list_task_files(inbox_dirs)
    imported = 0
    invalid = 0
    latest_imported = False
    lifecycle_counts = {
        "oa_resolved": 0,
        "downloaded": 0,
        "exported": 0,
        "event_count": 0,
        "invalid_events": 0,
    }
    for index, path in enumerate(task_files):
        try:
            read_task_rows(path)
        except (OSError, ValueError, json.JSONDecodeError):
            invalid += 1
        status = task_import_status(path)
        if status.imported:
            imported += 1
        event_summary = summarize_task_events(path)
        for key in lifecycle_counts:
            lifecycle_counts[key] += int(event_summary.get(key, 0))
        if index == 0:
            latest_imported = status.imported
    return TaskQueueSummary(
        total_batches=len(task_files),
        imported_batches=imported,
        pending_batches=len(task_files) - imported,
        invalid_batches=invalid,
        latest_path=task_files[0] if task_files else None,
        latest_imported=latest_imported,
        lifecycle_counts=lifecycle_counts,
    )


def import_task_file(
    path: str | Path,
    *,
    db_path: Path = DB_PATH,
    selected: bool = True,
    write_receipt: bool = True,
) -> TaskImportResult:
    source_path = Path(path).expanduser().resolve()
    previous_status = task_import_status(source_path)
    rows = read_task_rows(source_path)
    papers: list[Paper] = []
    warnings: list[str] = []
    for index, row in enumerate(rows, start=1):
        try:
            paper = paper_from_task(row, selected=selected)
            paper = paper.model_copy(
                update={
                    "raw": {
                        **paper.raw,
                        "task_source_path": str(source_path),
                    }
                }
            )
            papers.append(paper)
        except ValueError as exc:
            warnings.append(f"row {index}: {exc}")
    papers = deduplicate_papers(papers)
    upsert_papers(papers, db_path)

    receipt_path: Path | None = None
    if write_receipt:
        try:
            receipt_path = write_import_receipt(
                source_path,
                db_path=db_path,
                papers=papers,
                input_count=len(rows),
                skipped_count=len(rows) - len(papers),
                warnings=warnings,
                selected=selected,
            )
        except OSError as exc:
            warnings.append(f"receipt write failed: {exc}")

    return TaskImportResult(
        source_path=source_path,
        papers=papers,
        input_count=len(rows),
        skipped_count=len(rows) - len(papers),
        warnings=warnings,
        receipt_path=receipt_path,
        previously_imported=previous_status.imported,
    )


def write_import_receipt(
    source_path: Path,
    *,
    db_path: Path,
    papers: list[Paper],
    input_count: int,
    skipped_count: int,
    warnings: list[str],
    selected: bool,
) -> Path:
    receipt_path = task_receipt_path(source_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_sha256": file_sha256(source_path),
        "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "database_path": str(Path(db_path).resolve()),
        "input_count": input_count,
        "imported_count": len(papers),
        "skipped_count": skipped_count,
        "selected": selected,
        "warnings": list(warnings),
        "paper_ids": [paper.id for paper in papers],
        "task_ids": [
            _text(paper.raw.get("task", {}).get("task_id")) or paper.id
            for paper in papers
        ],
    }
    temp_path = receipt_path.with_name(
        f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(receipt_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return receipt_path


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_task_rows(path: str | Path) -> list[dict[str, Any]]:
    source_path = Path(path)
    if not source_path.exists():
        raise FileNotFoundError(f"Task file does not exist: {source_path}")
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            payload = payload.get("tasks") or payload.get("items") or []
        if not isinstance(payload, list):
            raise ValueError("JSON task file must contain a list or a 'tasks' list.")
        return [dict(row) for row in payload if isinstance(row, dict)]
    if suffix == ".csv":
        with source_path.open("r", newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError(f"Unsupported task file type: {suffix or '<none>'}")


def paper_from_task(task: dict[str, Any], *, selected: bool = True) -> Paper:
    title = _text(task.get("title"))
    if not title:
        raise ValueError("missing title")
    doi = normalize_doi(_text(task.get("doi")) or None)
    arxiv_id = normalize_arxiv_id(_text(task.get("arxiv_id")) or None)
    task_id = _text(task.get("task_id")) or _stable_task_id(doi, arxiv_id, title)
    pdf_url = _text(task.get("pdf_url")) or None
    source_name = _text(task.get("source")) or "metadata"
    tags = _unique_values(
        _split_values(task.get("concepts"))
        + _split_values(task.get("materials"))
        + _split_values(task.get("methods"))
    )
    publication_date = _text(task.get("publication_date"))
    year = _year(publication_date)
    priority = _float(task.get("download_priority"))
    momentum = _float(task.get("momentum"))
    relevance_score = min(10.0, max(0.0, priority / 10.0)) if priority else None
    url = _text(task.get("url")) or _fallback_url(doi, arxiv_id)
    authors = _split_values(task.get("authors"))

    return Paper(
        id=task_id,
        title=title,
        authors=authors,
        year=year,
        journal=_text(task.get("journal")) or None,
        doi=doi,
        arxiv_id=arxiv_id,
        url=url,
        pdf_url=pdf_url,
        pdf_status="oa_available" if pdf_url else "metadata_only",
        source=f"trend_radar:{source_name}",
        relevance_score=relevance_score,
        relevance_reason=_text(task.get("reason")) or None,
        tags=tags,
        selected=selected,
        raw={
            "integration_source": "condmat-trend-radar",
            "task": task,
            "momentum": momentum,
            "download_priority": priority,
        },
    )


def _stable_task_id(doi: str | None, arxiv_id: str | None, title: str) -> str:
    identity = doi or arxiv_id or normalize_title(title)
    return f"trend:{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex[:16]}"


def _fallback_url(doi: str | None, arxiv_id: str | None) -> str | None:
    if doi:
        return f"https://doi.org/{doi}"
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _split_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    if not text:
        return []
    separator = ";" if ";" in text else ","
    return [part.strip() for part in text.split(separator) if part.strip()]


def _unique_values(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _year(publication_date: str) -> int | None:
    if len(publication_date) >= 4 and publication_date[:4].isdigit():
        value = int(publication_date[:4])
        if 1000 <= value <= 9999:
            return value
    return None


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0