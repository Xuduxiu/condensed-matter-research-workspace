from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import ANY, patch

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.db.database import init_db
from backend.library.workbench import ensure_workbench_schema
from backend.llm.paper_analysis import (
    DeepSeekRequestError,
    generate_radar_brief,
    get_latest_radar_brief,
)
from backend.migrations.unified_library import apply_unified_schema


def radar_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    init_db(connection)
    apply_unified_schema(connection)
    ensure_workbench_schema(connection)
    connection.commit()
    return connection


def route_connect(path: Path):
    @contextmanager
    def _connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    return _connect


def add_paper(
    connection: sqlite3.Connection,
    paper_id: str,
    paper_date: str,
    *,
    data_mode: str = "real",
    eligible: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO papers
        (id, title, abstract, publication_date, source, data_mode,
         condmat_view_eligible, cited_by_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'test', ?, ?, 3, ?, ?)
        """,
        (
            paper_id,
            f"Local title {paper_id}",
            f"Local abstract for {paper_id}",
            paper_date,
            data_mode,
            int(eligible),
            paper_date,
            paper_date,
        ),
    )


class FakeResponse:
    def __init__(self, result: dict) -> None:
        self.result = result

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {"message": {"content": json.dumps(self.result, ensure_ascii=False)}}
            ]
        }


class FakeClient:
    def __init__(self, result: dict) -> None:
        self.response = FakeResponse(result)
        self.posts: list[dict] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response


class RadarBriefTests(unittest.TestCase):
    def test_manual_generation_filters_local_evidence_and_reuses_hash_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = radar_connection(Path(directory) / "radar.sqlite")
            today = date.today()
            add_paper(connection, "paper:eligible", (today - timedelta(days=1)).isoformat())
            add_paper(connection, "paper:future", (today + timedelta(days=1)).isoformat())
            add_paper(connection, "paper:old", (today - timedelta(days=10)).isoformat())
            add_paper(connection, "paper:mock", today.isoformat(), data_mode="mock")
            add_paper(connection, "paper:ineligible", today.isoformat(), eligible=False)
            now = today.isoformat()
            connection.execute(
                """INSERT INTO materials
                   (id, canonical_name, material_family, created_at, updated_at)
                   VALUES ('material:nbse2', 'NbSe2', 'tmd', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO paper_materials
                   (canonical_paper_id, material_id, confidence, source, created_at)
                   VALUES ('paper:eligible', 'material:nbse2', 0.9, 'local-test', ?)""",
                (now,),
            )
            connection.execute(
                "INSERT INTO topics (id, canonical_name, topic_type, created_at) VALUES ('topic:sc', 'superconductivity', 'concept', ?)",
                (now,),
            )
            connection.execute(
                """INSERT INTO paper_topics
                   (canonical_paper_id, topic_id, confidence, source, created_at)
                   VALUES ('paper:eligible', 'topic:sc', 0.8, 'local-test', ?)""",
                (now,),
            )
            connection.commit()

            model_result = {
                "headline": "近期凝聚态雷达",
                "summary_zh": "本地证据显示一个值得阅读的方向。",
                "rising_topics": ["superconductivity"],
                "notable_materials": ["NbSe2"],
                "papers_to_read": [
                    {
                        "canonical_paper_id": "paper:eligible",
                        "title": "Model supplied title",
                        "reason": "摘要与本地材料标注完整。",
                    }
                ],
                "caveats": ["样本窗口较短。"],
            }
            fake = FakeClient(model_result)
            with (
                patch("backend.llm.paper_analysis.deepseek_api_key", return_value="test-secret"),
                patch("backend.llm.paper_analysis.deepseek_model_fast", return_value="test-model"),
                patch("backend.llm.paper_analysis.httpx.Client", return_value=fake),
            ):
                generated = generate_radar_brief(connection, days=7, limit=5)

            self.assertTrue(generated["available"])
            self.assertFalse(generated["cached"])
            self.assertFalse(generated["evidence"]["quality"]["hotspot_reliable"])
            self.assertEqual(generated["analysis"]["rising_topics"], [])
            self.assertIn("不足以判断", generated["analysis"]["headline"])
            self.assertEqual(len(fake.posts), 1)
            request_context = json.loads(
                fake.posts[0]["json"]["messages"][1]["content"]
            )
            self.assertEqual(
                [item["canonical_paper_id"] for item in request_context["papers"]],
                ["paper:eligible"],
            )
            self.assertEqual(
                request_context["papers"][0]["materials_extracted_locally"],
                ["NbSe2"],
            )
            self.assertEqual(
                request_context["papers"][0]["topics_extracted_locally"],
                ["superconductivity"],
            )
            self.assertEqual(
                generated["analysis"]["papers_to_read"][0]["title"],
                "Local title paper:eligible",
            )

            with (
                patch("backend.llm.paper_analysis.deepseek_api_key", return_value=None),
                patch("backend.llm.paper_analysis.deepseek_model_fast", return_value="test-model"),
                patch("backend.llm.paper_analysis.httpx.Client") as no_network,
            ):
                cached = generate_radar_brief(connection, days=7, limit=5)
            self.assertTrue(cached["cached"])
            no_network.assert_not_called()

            row = connection.execute(
                "SELECT canonical_paper_id, analysis_type FROM analysis_results"
            ).fetchone()
            self.assertIsNone(row["canonical_paper_id"])
            self.assertEqual(row["analysis_type"], "deepseek_radar_brief")
            self.assertTrue(get_latest_radar_brief(connection, days=7)["available"])
            self.assertFalse(get_latest_radar_brief(connection, days=30)["available"])
            connection.close()

    def test_strict_response_rejects_paper_outside_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = radar_connection(Path(directory) / "radar.sqlite")
            add_paper(connection, "paper:eligible", date.today().isoformat())
            connection.commit()
            invalid = {
                "headline": "Radar",
                "summary_zh": "Summary",
                "rising_topics": [],
                "notable_materials": [],
                "papers_to_read": [
                    {
                        "canonical_paper_id": "paper:not-local",
                        "title": "Unknown",
                        "reason": "Invalid reference",
                    }
                ],
                "caveats": [],
            }
            fake = FakeClient(invalid)
            with (
                patch("backend.llm.paper_analysis.deepseek_api_key", return_value="test-secret"),
                patch("backend.llm.paper_analysis.httpx.Client", return_value=fake),
            ):
                with self.assertRaises(DeepSeekRequestError):
                    generate_radar_brief(connection, days=7, limit=5)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0],
                0,
            )
            connection.close()

    def test_api_get_is_cache_only_and_post_accepts_bounded_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite"
            connection = radar_connection(database)
            connection.close()
            generated = {
                "available": False,
                "cached": False,
                "request": {"days": 14, "tier": "pro", "limit": 5},
                "analysis": None,
            }
            with (
                patch("backend.api.workbench_routes.connect", route_connect(database)),
                patch(
                    "backend.api.workbench_routes.generate_radar_brief",
                    return_value=generated,
                ) as generate,
                TestClient(create_app()) as client,
            ):
                empty = client.get("/api/library/ai/radar-brief", params={"days": 7})
                invalid = client.get("/api/library/ai/radar-brief", params={"days": 6})
                response = client.post(
                    "/api/library/ai/radar-brief",
                    json={"days": 14, "tier": "pro", "force": True, "limit": 5},
                )

            self.assertEqual(empty.status_code, 200)
            self.assertFalse(empty.json()["available"])
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(response.status_code, 200)
            generate.assert_called_once_with(
                ANY,
                days=14,
                tier="pro",
                force=True,
                limit=5,
            )


if __name__ == "__main__":
    unittest.main()