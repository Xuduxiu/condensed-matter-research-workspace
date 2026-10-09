from __future__ import annotations

import json
import os

from paper_intake.cli import main as cli_main
from paper_intake.db import fetch_papers
from paper_intake.task_importer import (
    find_latest_task_file,
    import_task_file,
    list_task_files,
    paper_from_task,
    summarize_task_queue,
    task_import_status,
)


def sample_task() -> dict[str, object]:
    return {
        "task_id": "trend:abc123",
        "doi": "https://doi.org/10.1000/Example",
        "title": "A useful ZrTe5 paper",
        "journal": "Nature Physics",
        "publication_date": "2025-03-04",
        "source": "crossref",
        "concepts": "ZrTe5; topological insulator",
        "materials": "ZrTe5",
        "methods": "ARPES; DFT",
        "momentum": 12.5,
        "download_priority": 85,
        "reason": "matched ZrTe5; has DOI",
        "status": "pending",
    }


def test_paper_from_task_maps_priority_tags_and_identity() -> None:
    paper = paper_from_task(sample_task())

    assert paper.id == "trend:abc123"
    assert paper.doi == "10.1000/example"
    assert paper.year == 2025
    assert paper.relevance_score == 8.5
    assert paper.tags == ["ZrTe5", "topological insulator", "ARPES", "DFT"]
    assert paper.source == "trend_radar:crossref"
    assert paper.selected is True
    assert paper.url == "https://doi.org/10.1000/example"


def test_import_task_file_is_idempotent_and_writes_atomic_receipt(tmp_path) -> None:
    task_path = tmp_path / "download_tasks_20260713_010101.json"
    task_path.write_text(json.dumps([sample_task()]), encoding="utf-8")
    db_path = tmp_path / "papers.db"

    first = import_task_file(task_path, db_path=db_path)
    second = import_task_file(task_path, db_path=db_path)
    status = task_import_status(task_path)

    assert first.input_count == 1
    assert first.previously_imported is False
    assert first.receipt_path and first.receipt_path.exists()
    assert second.previously_imported is True
    assert second.papers[0].id == first.papers[0].id
    assert len(fetch_papers(db_path)) == 1
    assert status.imported is True
    assert status.reason == "imported"
    assert not list(first.receipt_path.parent.glob("*.tmp"))


def test_changed_source_invalidates_previous_receipt(tmp_path) -> None:
    task_path = tmp_path / "download_tasks_20260713_010102.json"
    task_path.write_text(json.dumps([sample_task()]), encoding="utf-8")
    import_task_file(task_path, db_path=tmp_path / "papers.db")

    changed = sample_task()
    changed["title"] = "Changed title"
    task_path.write_text(json.dumps([changed]), encoding="utf-8")
    status = task_import_status(task_path)

    assert status.imported is False
    assert status.reason == "source_changed"


def test_find_latest_task_file_prefers_newest_and_ignores_manifest(tmp_path) -> None:
    old = tmp_path / "download_tasks_20260712_010101.json"
    new = tmp_path / "download_tasks_20260713_010101.json"
    manifest = tmp_path / "download_tasks_20260714_010101.manifest.json"
    old.write_text("[]", encoding="utf-8")
    new.write_text("[]", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    os.utime(manifest, (3, 3))

    assert find_latest_task_file([tmp_path]) == new


def test_json_csv_pair_counts_as_one_batch_and_prefers_json(tmp_path) -> None:
    json_path = tmp_path / "download_tasks_20260713_020202.json"
    csv_path = tmp_path / "download_tasks_20260713_020202.csv"
    json_path.write_text(json.dumps([sample_task()]), encoding="utf-8")
    csv_path.write_text("task_id,title\ntrend:csv,CSV duplicate\n", encoding="utf-8")
    os.utime(json_path, (1, 1))
    os.utime(csv_path, (2, 2))

    files = list_task_files([tmp_path])
    import_task_file(json_path, db_path=tmp_path / "papers.db")
    summary = summarize_task_queue([tmp_path])

    assert files == [json_path]
    assert summary.total_batches == 1
    assert summary.imported_batches == 1
    assert summary.pending_batches == 0
    assert summary.latest_imported is True


def test_corrupt_receipt_is_reported_as_pending(tmp_path) -> None:
    task_path = tmp_path / "download_tasks_20260713_030303.json"
    task_path.write_text(json.dumps([sample_task()]), encoding="utf-8")
    result = import_task_file(task_path, db_path=tmp_path / "papers.db")
    assert result.receipt_path is not None
    result.receipt_path.write_text("{broken", encoding="utf-8")

    status = task_import_status(task_path)
    summary = summarize_task_queue([tmp_path])

    assert status.imported is False
    assert status.reason == "receipt_invalid"
    assert summary.pending_batches == 1


def test_invalid_task_batch_is_counted(tmp_path) -> None:
    task_path = tmp_path / "download_tasks_20260713_040404.json"
    task_path.write_text("{broken", encoding="utf-8")

    summary = summarize_task_queue([tmp_path])

    assert summary.total_batches == 1
    assert summary.invalid_batches == 1
    assert summary.pending_batches == 1


def test_import_csv_task_file(tmp_path) -> None:
    task_path = tmp_path / "trend_radar_download_tasks_20260713_050505.csv"
    task_path.write_text(
        "task_id,title,doi,publication_date,concepts,download_priority\n"
        "trend:csv1,CSV paper,10.1000/csv,2024-01-02,ZrTe5; DFT,60\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "csv_papers.db"

    result = import_task_file(task_path, db_path=db_path, selected=False)

    assert result.input_count == 1
    assert result.papers[0].doi == "10.1000/csv"
    assert result.papers[0].tags == ["ZrTe5", "DFT"]
    assert result.papers[0].selected is False
    assert result.receipt_path and result.receipt_path.exists()

def test_queue_status_cli_reports_machine_readable_json(tmp_path, capsys) -> None:
    task_path = tmp_path / "download_tasks_20260713_060606.json"
    task_path.write_text(json.dumps([sample_task()]), encoding="utf-8")

    exit_code = cli_main(["queue-status", "--inbox", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["total_batches"] == 1
    assert payload["pending_batches"] == 1
    assert payload["latest_imported"] is False
