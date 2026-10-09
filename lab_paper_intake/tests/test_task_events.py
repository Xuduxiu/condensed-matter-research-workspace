from __future__ import annotations

import json
import zipfile
from pathlib import Path

from paper_intake.config import Settings
from paper_intake.export_package import export_selected_package
from paper_intake.models import Paper
from paper_intake.task_events import (
    record_export_lifecycle,
    summarize_task_events,
)
from paper_intake.task_importer import import_task_file, summarize_task_queue


def settings() -> Settings:
    return Settings(None, "https://api.deepseek.com", "", None)


def linked_paper(source_path: Path, *, downloaded: bool = True) -> Paper:
    local_path = source_path.parent / "paper.pdf"
    if downloaded:
        local_path.write_bytes(b"%PDF-1.4\n" + b"x" * 256)
    return Paper(
        id="trend:event-paper",
        title="Lifecycle paper",
        doi="10.1000/lifecycle",
        pdf_url="https://example.org/lifecycle.pdf",
        pdf_status="downloaded" if downloaded else "oa_available",
        local_pdf_path=str(local_path) if downloaded else None,
        source="trend_radar:crossref",
        raw={
            "integration_source": "condmat-trend-radar",
            "task_source_path": str(source_path),
            "task": {"task_id": "trend:event-task"},
        },
    )


def test_append_only_lifecycle_events_are_aggregated_by_task(tmp_path) -> None:
    source_path = tmp_path / "download_tasks_20260713_070707.json"
    source_path.write_text("[]", encoding="utf-8")
    paper = linked_paper(source_path)

    first = record_export_lifecycle(
        [paper],
        export_id="run_0001",
        export_path=tmp_path / "run_0001.zip",
    )
    second = record_export_lifecycle(
        [paper],
        export_id="run_0002",
        export_path=tmp_path / "run_0002.zip",
    )
    summary = summarize_task_events(source_path)

    assert first.counts == {
        "oa_resolved": 1,
        "downloaded": 1,
        "exported": 1,
    }
    assert second.counts == first.counts
    assert len(first.event_paths) == 3
    assert summary["oa_resolved"] == 1
    assert summary["downloaded"] == 1
    assert summary["exported"] == 1
    assert summary["event_count"] == 6
    event_dir = source_path.parent / ".receipts" / "events"
    assert not list(event_dir.glob("*.tmp"))


def test_unlinked_paper_is_skipped_without_false_event(tmp_path) -> None:
    paper = Paper(title="Unlinked paper")

    result = record_export_lifecycle(
        [paper],
        export_id="run_unlinked",
        export_path=tmp_path / "run_unlinked.zip",
    )

    assert result.event_paths == []
    assert result.errors == []
    assert result.linked_papers == 0
    assert result.unlinked_papers == 1


def test_export_package_records_lifecycle_and_updates_manifest(tmp_path) -> None:
    source_path = tmp_path / "download_tasks_20260713_080808.json"
    source_path.write_text(
        json.dumps(
            [
                {
                    "task_id": "trend:export-event",
                    "title": "Export event paper",
                    "doi": "10.1000/export-event",
                    "pdf_url": "https://example.org/export-event.pdf",
                    "source": "crossref",
                    "download_priority": 80,
                }
            ]
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "papers.db"
    imported = import_task_file(source_path, db_path=db_path)
    existing_pdf = tmp_path / "existing.pdf"
    existing_pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 256)
    paper = imported.papers[0].model_copy(
        update={
            "pdf_status": "downloaded",
            "local_pdf_path": str(existing_pdf),
        }
    )

    result = export_selected_package(
        [paper],
        settings=settings(),
        exports_dir=tmp_path / "exports",
        db_path=db_path,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    queue = summarize_task_queue([tmp_path])

    assert len(result.lifecycle_event_paths) == 3
    assert result.lifecycle_event_errors == []
    assert manifest["lifecycle_events"]["counts"]["oa_resolved"] == 1
    assert manifest["lifecycle_events"]["counts"]["downloaded"] == 1
    assert manifest["lifecycle_events"]["counts"]["exported"] == 1
    assert manifest["files"]["lifecycle_events"] == "lifecycle_events.json"
    assert queue.lifecycle_counts["exported"] == 1
    assert queue.lifecycle_counts["downloaded"] == 1
    with zipfile.ZipFile(result.zip_path) as archive:
        assert "lifecycle_events.json" in archive.namelist()