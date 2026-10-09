from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks
from pydantic import ValidationError

from backend.api.library_routes import DailyRequest, api_daily_run
from backend.db.database import init_db
from backend.migrations.unified_library import apply_unified_schema
from backend.scheduler.daily_update import (
    DailyOptions,
    _record_cursor_attempt,
    _scan_window,
    create_daily_run,
    run_daily_update,
)


def bootstrap_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        init_db(connection)
        apply_unified_schema(connection)
        connection.commit()
    finally:
        connection.close()


class ScanWindowPolicyTests(unittest.TestCase):
    def test_full_uses_fixed_inclusive_lookback_and_live_uses_watermark(self) -> None:
        today = date(2026, 8, 9)
        cursor = {"last_successful_cursor": "2026-08-07"}

        full = _scan_window(
            DailyOptions(scan_mode="full", historical_days=180),
            cursor,
            today_value=today,
        )
        live = _scan_window(
            DailyOptions(scan_mode="live", historical_days=730),
            cursor,
            today_value=today,
        )

        self.assertEqual(
            full,
            (
                (today - timedelta(days=179)).isoformat(),
                today.isoformat(),
                "fixed_historical_lookback",
                180,
            ),
        )
        self.assertEqual(
            live,
            (
                "2026-08-07",
                "2026-08-09",
                "safe_watermark_incremental",
                None,
            ),
        )

    def test_full_dry_run_and_queued_report_expose_real_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            bootstrap_database(database)
            today = date.today()
            options = DailyOptions(
                dry_run=True,
                scan_mode="full",
                historical_days=730,
                skip_network=True,
            )

            dry_run = run_daily_update(options, database=database)
            queued_id = create_daily_run(
                DailyOptions(dry_run=False, scan_mode="full", historical_days=730),
                database=database,
                trigger_type="test_full_window",
            )
            connection = sqlite3.connect(database)
            report = json.loads(
                connection.execute(
                    "SELECT report_json FROM daily_runs WHERE id=?",
                    (queued_id,),
                ).fetchone()[0]
            )
            connection.close()

        expected_from = (today - timedelta(days=729)).isoformat()
        self.assertEqual(dry_run["date_from"], expected_from)
        self.assertEqual(dry_run["date_to"], today.isoformat())
        self.assertEqual(dry_run["historical_days"], 730)
        self.assertEqual(dry_run["window_policy"], "fixed_historical_lookback")
        self.assertEqual(report["date_from"], expected_from)
        self.assertEqual(report["date_to"], today.isoformat())
        self.assertEqual(report["historical_days"], 730)


class FullRunWindowPropagationTests(unittest.TestCase):
    def test_full_window_reaches_ingest_coverage_and_final_report(self) -> None:
        captured: dict[str, object] = {}
        coverage_windows: list[tuple[str | None, str | None]] = []

        def fake_ingest(args: argparse.Namespace) -> dict[str, object]:
            captured["baseline_from"] = args.baseline_from
            captured["baseline_to"] = args.baseline_to
            captured["resume"] = args.resume
            return {
                "status": "ok",
                "fetched_count": 0,
                "kept_count": 0,
                "inserted_count": 0,
                "updated_count": 0,
                "changed_count": 0,
                "deduped_count": 0,
                "failed_count": 0,
                "affected_paper_ids": [],
                "metadata_changed_paper_ids": [],
                "source_counts": {},
                "errors": [],
            }

        def fake_coverage(*_args, **kwargs):
            coverage_windows.append((kwargs.get("window_from"), kwargs.get("window_to")))
            return {"status": "ok"}

        empty_backfill = {
            "requested": 0,
            "completed": 0,
            "not_found": 0,
            "failed": 0,
            "identity_conflicts": 0,
            "paper_version_ids": [],
            "canonical_paper_ids": [],
            "results": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite"
            bootstrap_database(database)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            _record_cursor_attempt(
                connection,
                "openalex_crossref_arxiv",
                status="completed",
                cursor=(date.today() - timedelta(days=1)).isoformat(),
            )
            connection.commit()
            connection.close()

            with (
                patch("backend.scheduler.daily_update.run_real_ingest", side_effect=fake_ingest),
                patch("backend.scheduler.daily_update.run_source_coverage_audit", side_effect=fake_coverage),
                patch("backend.scheduler.daily_update.run_source_backfill", return_value=empty_backfill),
                patch("backend.scheduler.daily_update.locks_dir", return_value=root / "locks"),
            ):
                result = run_daily_update(
                    DailyOptions(
                        dry_run=False,
                        scan_mode="full",
                        historical_days=180,
                        download_limit=0,
                        abstract_backfill_limit=0,
                    ),
                    database=database,
                    pdf_root=root / "pdf",
                    trigger_type="test_full_window",
                )

        expected_to = date.today().isoformat()
        expected_from = (date.today() - timedelta(days=179)).isoformat()
        self.assertEqual(captured["baseline_from"], expected_from)
        self.assertEqual(captured["baseline_to"], expected_to)
        self.assertTrue(captured["resume"])
        self.assertEqual(coverage_windows, [(expected_from, expected_to), (expected_from, expected_to)])
        self.assertEqual(result["report"]["date_from"], expected_from)
        self.assertEqual(result["report"]["date_to"], expected_to)
        self.assertEqual(result["report"]["historical_days"], 180)
        self.assertEqual(result["report"]["window_policy"], "fixed_historical_lookback")


class DailyWindowApiTests(unittest.TestCase):
    def test_api_bounds_and_maps_full_lookback_days(self) -> None:
        with self.assertRaises(ValidationError):
            DailyRequest(scan_mode="full", full_lookback_days=29)
        with self.assertRaises(ValidationError):
            DailyRequest(scan_mode="full", full_lookback_days=3651)

        payload = DailyRequest(
            apply=False,
            scan_mode="full",
            full_lookback_days=730,
        )
        with patch(
            "backend.api.library_routes.run_daily_update",
            return_value={"status": "PASS"},
        ) as run:
            result = api_daily_run(payload, BackgroundTasks())

        self.assertEqual(result["status"], "PASS")
        options = run.call_args.args[0]
        self.assertEqual(options.scan_mode, "full")
        self.assertEqual(options.historical_days, 730)
        self.assertEqual(DailyRequest().full_lookback_days, 180)


if __name__ == "__main__":
    unittest.main()