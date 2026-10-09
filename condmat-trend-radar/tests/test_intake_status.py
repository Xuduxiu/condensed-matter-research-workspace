from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.integration.intake_status import (
    EVENT_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    file_sha256,
    summarize_intake_queue,
    task_receipt_path,
)


class IntakeStatusTests(unittest.TestCase):
    def test_receipts_and_lifecycle_are_aggregated_without_duplicate_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "inbox"
            inbox.mkdir()
            source = inbox / "download_tasks_20260713_120000.json"
            tasks = [
                {"task_id": "trend:one", "title": "One"},
                {"task_id": "trend:two", "title": "Two"},
            ]
            source.write_text(json.dumps(tasks), encoding="utf-8")
            source.with_suffix(".csv").write_text(
                "task_id,title\ntrend:one,One\ntrend:two,Two\n",
                encoding="utf-8",
            )
            receipt = task_receipt_path(source)
            receipt.parent.mkdir(parents=True)
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": RECEIPT_SCHEMA_VERSION,
                        "source_sha256": file_sha256(source),
                        "imported_at": "2026-07-13T12:01:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            event_dir = receipt.parent / "events"
            event_dir.mkdir()
            for index, (status, task_id) in enumerate(
                [
                    ("oa_resolved", "trend:one"),
                    ("downloaded", "trend:one"),
                    ("exported", "trend:one"),
                    ("exported", "trend:one"),
                ]
            ):
                (event_dir / f"event_{index}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": EVENT_SCHEMA_VERSION,
                            "status": status,
                            "task_id": task_id,
                            "source_path": str(source.resolve()),
                        }
                    ),
                    encoding="utf-8",
                )

            summary = summarize_intake_queue([inbox])

            self.assertEqual(summary["total_batches"], 1)
            self.assertEqual(summary["imported_batches"], 1)
            self.assertEqual(summary["pending_batches"], 0)
            self.assertEqual(summary["batches"][0]["task_count"], 2)
            self.assertTrue(summary["batches"][0]["imported"])
            self.assertEqual(
                summary["lifecycle_counts"],
                {
                    "oa_resolved": 1,
                    "downloaded": 1,
                    "exported": 1,
                    "event_count": 4,
                    "invalid_events": 0,
                },
            )

    def test_source_change_invalidates_receipt_and_invalid_event_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "inbox"
            inbox.mkdir()
            source = inbox / "download_tasks_20260713_130000.json"
            source.write_text('[{"task_id":"trend:one","title":"One"}]', encoding="utf-8")
            receipt = task_receipt_path(source)
            receipt.parent.mkdir(parents=True)
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": RECEIPT_SCHEMA_VERSION,
                        "source_sha256": file_sha256(source),
                        "imported_at": "2026-07-13T13:01:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            source.write_text('[{"task_id":"trend:two","title":"Two"}]', encoding="utf-8")
            event_dir = receipt.parent / "events"
            event_dir.mkdir()
            (event_dir / "broken.json").write_text("{", encoding="utf-8")

            summary = summarize_intake_queue([inbox])

            self.assertEqual(summary["imported_batches"], 0)
            self.assertEqual(summary["pending_batches"], 1)
            self.assertEqual(summary["batches"][0]["import_reason"], "source_changed")
            self.assertEqual(summary["lifecycle_counts"]["invalid_events"], 1)


if __name__ == "__main__":
    unittest.main()
