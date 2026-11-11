from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import translation_routes as api
from backend.api.main import create_app
from backend.db.database import connect, init_db
from backend.library import translations as tr
from backend.library.repository import LibraryRepository
from backend.llm.dsh_translation_bridge import TranslationBridgeError
from backend.migrations.unified_library import apply_unified_schema


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "notes.sqlite"
        self.enterContext(patch("backend.db.database.ensure_data_layout"))
        with connect(self.path) as db:
            self.assertFalse(tr.automatic_enabled(db))
            init_db(db)
            apply_unified_schema(db)
            tr.ensure_translation_schema(db)
            tr.ensure_translation_schema(db)
        self.calls = []
        self.worker = tr.TranslationWorker(self.path, self.translate)
        self.enterContext(patch.object(api, "_worker", self.worker))
        self.enterContext(patch.object(api, "connect", self.db))

    def db(self):
        return connect(self.path)

    def translate(self, items):
        self.calls.append(items)
        return {
            "items": [
                {
                    "id": r["id"],
                    "title_zh": "中文标题" if r["title"] else "",
                    "abstract_zh": "对应的中文摘要" if r["abstract"] else "",
                }
                for r in reversed(items)
            ],
            "provider": "test",
            "model": "fixture",
            "usage": {"input_tokens": 60, "output_tokens": 30, "total_tokens": 90},
        }

    def seed(self, ident, title, abstract="", mock=False):
        with self.db() as db:
            result = LibraryRepository(db).upsert_version(
                {"title": title, "abstract": abstract, "doi": "10.1000/" + ident},
                source="crossref",
                source_record_id=ident,
            )
            if mock:
                db.execute("UPDATE papers SET data_mode='mock' WHERE id=?", (result.canonical_paper_id,))
            return result.paper_version_id

    def submit(self, ids=None, **kwargs):
        with self.db() as db:
            return tr.submit_job(
                db,
                "selected" if ids else "all",
                ids or [],
                100,
                kwargs.pop("batch_size", 8),
                kwargs.pop("max_chars", 16000),
                **kwargs,
            )

    def auto(self, enabled=True, available=True):
        with self.db() as db:
            return tr.set_automatic(db, enabled, None, None, available)

    def state(self, job):
        with self.db() as db:
            return tr.job_status(db, job["id"])

    def test_read_only_estimate_excludes_mock_counts_versions_and_duplicate_fields(self):
        a = self.seed("a", "Graphene transport", "")
        self.seed("b", "Graphene transport", "")
        self.seed("c", "二维材料的结构性质", "硫化物中的电子分布")
        self.seed("mock", "Test fixture from another corpus", mock=True)
        with self.db() as db:
            est = tr.estimate(db, "all", [], 100, 8, 16000)
            self.assertEqual(
                (est["total"], est["skipped"], est["needs_translation"], est["unique_inputs"]), (3, 1, 2, 1)
            )
            self.assertEqual(est["estimated_requests"], 1)
            self.assertEqual(est["projection_100k"]["versions"], 100000)
            self.assertFalse(tr.automatic_enabled(db))
            self.assertEqual(db.execute("SELECT count(*) FROM translation_jobs").fetchone()[0], 0)
            self.assertEqual(tr.get_version(db, a)["status"], "missing")
        self.assertFalse(self.worker.step())

    def test_active_version_priority_idempotency_cache_source_change_and_usage(self):
        a = self.seed("a", "Graphene transport")
        b = self.seed("b", "Molybdenum disulfide")
        self.seed("c", "Graphene transport")
        job = self.submit()
        self.assertEqual(self.submit()["id"], job["id"])
        priority = self.submit([b], priority=True)
        self.assertTrue(self.worker.step())
        self.assertEqual([r["title"] for r in self.calls[0]], ["Molybdenum disulfide"])
        self.assertEqual(self.state(priority)["actual_usage"]["total_tokens"], 90)
        self.assertTrue(self.worker.step())
        self.assertFalse(self.worker.step())
        self.assertEqual(self.state(job)["completed"], 3)
        with self.db() as db:
            cached = tr.read_cached_translations(db, [{"paper_version_id": a}, {"paper_version_id": b}])
            self.assertEqual(cached[a]["title_zh"], "中文标题")
            db.execute(
                "UPDATE paper_versions SET title='New title of this paper', abstract='A new abstract' WHERE id=?",
                (a,),
            )
        with self.db() as db:
            self.assertNotIn(a, tr.read_cached_translations(db, [{"paper_version_id": a}]))
            self.assertEqual(tr.get_version(db, a)["status"], "missing")
            self.assertEqual(tr.get_version(db, b)["provider"], "test")

    def test_auto_offline_seed_recovery_incremental_changes_and_missing_abstract(self):
        a = self.seed("a", "Measured magnetoresistance")
        state = self.auto(True, available=False)
        self.assertTrue(state["enabled"] and state["blocked"])
        self.assertEqual(state["pending"], 1)
        self.assertFalse(self.worker.step())
        with self.db() as db:
            db.execute("UPDATE translation_policy SET retry_at=0")
        self.assertTrue(self.worker.step())
        self.assertEqual(self.state({"id": tr.AUTO_JOB})["completed"], 1)
        self.auto(False)
        self.seed("b", "另一篇中文论文", "不用重复翻译")
        self.seed("demo", "Demo corpus entry", mock=True)
        with self.db() as db:
            for abstract in ("First draft", "Revised abstract with transport data"):
                db.execute("UPDATE paper_versions SET abstract=? WHERE id=?", (abstract, a))
            self.assertEqual(tr.automatic_status(db)["dirty"], 3)
        self.assertFalse(self.worker.step())
        self.auto(True)
        self.assertTrue(self.worker.step())
        self.assertEqual(self.calls[-1][0]["title"], "")
        self.assertEqual(self.calls[-1][0]["abstract"], "Revised abstract with transport data")
        self.assertEqual(len(self.calls), 2)
        self.auto(True)
        self.assertFalse(self.worker.step())
        with self.db() as db:
            status = tr.automatic_status(db)
            self.assertEqual(
                (status["total"], status["completed"], status["skipped"], status["dirty"]), (2, 1, 1, 0)
            )
            self.assertEqual(
                db.execute("SELECT title,abstract FROM paper_versions WHERE id=?", (a,)).fetchone()[0],
                "Measured magnetoresistance",
            )

    def test_parallel_claim_lease_recovery_gates_and_racing_write(self):
        self.seed("a", "Only available version")
        job = self.submit()
        with self.db() as db:
            old, _ = tr._claim(db, 1000)
        with self.db() as db:
            other, _ = tr._claim(db, 1100)
        self.assertIsNone(other)
        with self.db() as db:
            new, _ = tr._claim(db, 1000 + tr.LEASE_SECONDS + 1)
        self.assertNotEqual(new["lease"], old["lease"])
        for batch in (old, new):
            reply = self.translate(batch["items"])
            with self.db() as db:
                tr._finish(db, batch, reply, tr._validated(batch["items"], reply), 1500)
        self.assertEqual(self.state(job)["completed"], 1)
        self.assertEqual(self.state(job)["actual_usage"]["calls"], 1)

    def test_quota_pauses_all_without_consuming_next_rows_and_recovers(self):
        self.seed("a", "First English paper")
        b = self.seed("b", "Second English paper")
        job = self.submit(batch_size=1)
        paused = self.submit([b])
        with self.db() as db:
            tr.control_job(db, paused["id"], "pause")

        def fail(_items):
            raise TranslationBridgeError("QUOTA", "secret should never be surfaced", False)

        self.worker.translate = fail
        self.assertTrue(self.worker.step())
        status = self.state(job)
        self.assertEqual((status["status"], status["pending"], status["failed"]), ("paused", 2, 0))
        self.assertNotIn("secret", status["error"])
        self.assertFalse(self.worker.step())
        with self.db() as db:
            db.execute("UPDATE translation_policy SET retry_at=0")
        self.worker.translate = self.translate
        self.assertTrue(self.worker.step())
        self.assertTrue(self.worker.step())
        with self.db() as db:
            self.assertEqual(
                db.execute("SELECT paused FROM translation_jobs WHERE id=?", (paused["id"],)).fetchone()[0], 1
            )

    def test_invalid_structured_reply_is_split_and_good_records_complete(self):
        for i in range(3):
            self.seed(str(i), "Sample English " + str(i))
        job = self.submit()
        bad_single_attempts = []

        def sometimes(items):
            if len(items) > 1 or items[0]["title"].endswith("1"):
                if len(items) == 1:
                    bad_single_attempts.append(items[0]["id"])
                raise TranslationBridgeError("INVALID_OUTPUT", "private detail")
            return self.translate(items)

        self.worker.translate = sometimes
        for _ in range(10):
            with self.db() as db:
                db.execute("UPDATE translation_work SET retry_at=0")
            if not self.worker.step():
                break
        result = self.state(job)
        self.assertEqual(len(bad_single_attempts), tr.MAX_ATTEMPTS)
        self.assertEqual((result["completed"], result["failed"], result["status"]), (2, 1, "partial"))
        self.worker.translate = self.translate
        with self.db() as db:
            tr.control_job(db, job["id"], "retry")
        self.assertTrue(self.worker.step())
        self.assertEqual(self.state(job)["completed"], 3)

    def test_transient_single_output_retries_and_real_usage_is_preserved(self):
        self.seed("a", "Measured resistivity under pressure")
        job = self.submit()

        def truncated(_items):
            raise TranslationBridgeError(
                "TRUNCATED",
                "partial result",
                False,
                model="fixture",
                usage={"input_tokens": 20, "output_tokens": 15, "total_tokens": 35},
            )

        self.worker.translate = truncated
        self.assertTrue(self.worker.step())
        self.assertEqual(self.state(job)["pending"], 1)
        self.assertFalse(self.worker.step())
        self.worker.translate = self.translate
        with self.db() as db:
            db.execute("UPDATE translation_work SET retry_at=0")
        self.assertTrue(self.worker.step())
        result = self.state(job)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["actual_usage"]["total_tokens"], 125)

    def test_character_limit_shared_fields_and_oversized_work_is_not_truncated(self):
        a = self.seed("a", "Graphene transport", "metallic " * 160)
        self.seed("b", "Graphene transport", "quantum " * 160)
        low = self.submit(max_chars=1000)
        self.assertEqual((low["failed"], low["pending"]), (2, 0))
        self.assertFalse(self.worker.step())
        high = self.submit(max_chars=16000)
        self.assertTrue(self.worker.step())
        payload = self.calls[-1]
        self.assertEqual(sum(bool(r["title"]) for r in payload), 1)
        self.assertEqual(sum(bool(r["abstract"]) for r in payload), 2)
        self.assertEqual(self.state(high)["completed"], 2)
        self.assertEqual(self.state(low)["completed"], 2)
        with self.db() as db:
            self.assertEqual(tr.get_version(db, a)["abstract_zh"], "对应的中文摘要")

    def test_language_detection_and_get_without_provider(self):
        for source in ("Измерение тока", "Υπεραγωγιμότητα", "ナノ材料の電子輸送", "Graphene transport"):
            self.assertIsNotNone(tr._field_key("title", source), source)
        for source in ("二维 NbSe2 中的电流", "HfTe2", "α + β", ""):
            self.assertIsNone(tr._field_key("title", source), source)
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/api/library/translations/provider").json()["model"], "dsh")
            self.assertFalse(client.get("/api/library/translations/automatic").json()["enabled"])
            a = self.seed("a", "Graphene")
            self.assertEqual(client.get("/api/library/translations/versions/" + a).json()["status"], "missing")
            self.assertEqual(
                client.post("/api/library/translations/estimate", json={"scope": "all"}).json()["total"], 1
            )
            self.assertEqual(
                client.post("/api/library/translations/jobs", json={"scope": "selected"}).status_code, 422
            )
            with patch.object(api, "_worker", None):
                self.assertEqual(client.post("/api/library/translations/versions/" + a).status_code, 503)
                self.assertTrue(
                    client.put("/api/library/translations/automatic", json={"enabled": True}).json()["blocked"]
                )
        self.assertFalse(self.calls)

    def test_stop_while_awaiting_model_requeues_without_system_block(self):
        self.seed("a", "Transport at low temperature")
        self.auto()

        def shutting_down(_items):
            self.worker.request_stop()
            raise TranslationBridgeError("CANCELLED", "closing")

        self.worker.translate = shutting_down
        self.assertTrue(self.worker.step())
        with self.db() as db:
            status = tr.automatic_status(db)
            self.assertEqual((status["blocked"], status["pending"], status["running"]), (False, 1, 0))
            self.assertEqual(db.execute("SELECT attempts FROM translation_work").fetchone()[0], 0)
        reopened = tr.TranslationWorker(self.path, self.translate)
        self.assertTrue(reopened.step())
        self.assertEqual(self.state({"id": tr.AUTO_JOB})["completed"], 1)

    def test_imported_version_with_slash_routes_to_single_paper(self):
        original = self.seed("a", "Graphene edge transport")
        version = "legacy:arXiv/cond-mat/0123456"
        with self.db() as db:
            db.execute(
                """INSERT INTO paper_versions(id,canonical_paper_id,version_type,title,source,source_record_id,first_seen_at,last_seen_at)
                   SELECT ?,canonical_paper_id,version_type,title,'legacy','slash-id',first_seen_at,last_seen_at
                   FROM paper_versions WHERE id=?""",
                (version, original),
            )
        url = "/api/library/translations/versions/legacy%3AarXiv%2Fcond-mat%2F0123456"
        with TestClient(create_app()) as client:
            self.assertEqual(client.get(url).json()["version_id"], version)
            job = client.post(url)
            self.assertEqual(job.status_code, 200)
            self.assertEqual(job.json()["total"], 1)

    def test_queued_revision_rebound_mock_removed_and_trigger_accepts_outer_conflict_policy(self):
        a = self.seed("a", "Outdated original title")
        b = self.seed("b", "Another title")
        job = self.submit()
        self.auto()
        with self.db() as db:
            db.execute("UPDATE OR ABORT paper_versions SET title='A newer title' WHERE id=?", (a,))
            db.execute("UPDATE OR ABORT paper_versions SET title='Final corrected title' WHERE id=?", (a,))
            db.execute(
                "UPDATE papers SET data_mode='mock' WHERE id=(SELECT canonical_paper_id FROM paper_versions WHERE id=?)",
                (b,),
            )
        for _ in range(5):
            if not self.worker.step():
                break
        self.assertEqual([item["title"] for call in self.calls for item in call], ["Final corrected title"])
        self.assertEqual(self.state(job)["completed"], 1)
        with self.db() as db:
            self.assertEqual(tr.automatic_status(db)["total"], 1)

    def test_429_applies_global_cooldown_and_usage_is_not_invented(self):
        self.seed("a", "Measurement of current")
        self.seed("b", "Different spectrum")
        job = self.submit(batch_size=1)

        def throttled(_items):
            raise TranslationBridgeError("RATE_LIMIT", "429", True)

        self.worker.translate = throttled
        self.assertTrue(self.worker.step())
        self.assertFalse(self.worker.step())
        usage = self.state(job)["actual_usage"]
        self.assertEqual((usage["calls"], usage["reported_calls"], usage["total_tokens"]), (1, 0, None))
        with self.db() as db:
            self.assertGreater(db.execute("SELECT retry_at FROM translation_policy").fetchone()[0], 0)

    def test_deferred_host_notification_observes_committed_automatic_switch(self):
        observed = []

        def persisted(enabled):
            with self.db() as db:
                observed.append((enabled, tr.automatic_enabled(db)))

        self.worker.on_auto_change = persisted
        with TestClient(create_app()) as client:
            for enabled in (True, False):
                self.assertEqual(
                    client.put("/api/library/translations/automatic", json={"enabled": enabled}).status_code,
                    200,
                )
        self.assertEqual(observed, [(True, True), (False, False)])

    def test_cached_empty_field_and_formula_reply_are_accepted(self):
        items = [
            {"id": "a", "title": "", "abstract": "Ordinary English prose"},
            {"id": "b", "title": "Nb Se 2 α β", "abstract": ""},
        ]
        result = tr._validated(
            items,
            {
                "items": [
                    {"id": "b", "title_zh": "Nb Se 2 α β", "abstract_zh": ""},
                    {"id": "a", "title_zh": "", "abstract_zh": "中文摘要"},
                ]
            },
        )
        self.assertEqual(result, {"b": {"title": "Nb Se 2 α β"}, "a": {"abstract": "中文摘要"}})

    def test_native_math_copy_is_cached_without_model_or_token_accounting(self):
        formula = r"$\operatorname{argmin}_{x}\left[\mathrm{spin energy}(x)\right]$"
        version = self.seed("pure-math", formula)
        job = self.submit([version])
        seen = []

        def local_copy(items):
            seen.append(items)
            return {
                "no_inference": True,
                "provider": None,
                "model": None,
                "usage": None,
                "items": [
                    {"id": row["id"], "title_zh": row["title"], "abstract_zh": row["abstract"]} for row in items
                ],
            }

        self.worker.translate = local_copy
        self.assertTrue(self.worker.step())
        self.assertEqual(len(seen), 1)
        with self.db() as db:
            view = tr.get_version(db, version)
            self.assertEqual((view["status"], view["title_zh"], view["abstract_zh"]), ("ready", formula, None))
            self.assertEqual((view["provider"], view["model"]), (None, None))
            self.assertEqual(db.execute("SELECT attempts FROM translation_work").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM translation_usage").fetchone()[0], 0)
        self.assertEqual(self.state(job)["actual_usage"]["calls"], 0)
        self.assertEqual(self.submit([version])["completed"], 1)
        self.assertFalse(self.worker.step())

    def test_native_math_copy_rejects_mismatched_text_batch_and_inference_metadata(self):
        formula = r"$\text{spin energy}$"
        inputs = [{"id": "a", "title": formula, "abstract": ""}]
        row = {"id": "a", "title_zh": formula, "abstract_zh": ""}
        receipt = {"no_inference": True, "provider": None, "model": None, "usage": None, "items": [row]}
        for invalid in (
            {"items": [{**row, "title_zh": formula + " "}]},
            {"items": [{**row, "abstract_zh": "新增文字"}]},
            {"items": [{**row, "id": "another-version"}]},
            {"items": [row, row]},
            {"items": [{"id": "a", "title_zh": formula}]},
            {"usage": {"total_tokens": 0}},
            {"provider": "fixture"},
            {"model": "fixture"},
            {"no_inference": 1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(tr._InvalidReply):
                tr._validated(inputs, {**receipt, **invalid})
        with self.assertRaises(tr._InvalidReply):
            tr._validated(inputs + [{**inputs[0], "id": "b"}], receipt)
        with self.assertRaises(tr._InvalidReply):
            tr._validated(inputs, {key: value for key, value in receipt.items() if key != "usage"})
        with self.assertRaises(tr._InvalidReply):
            tr._validated(inputs, {**receipt, "no_inference": False})

    def test_persisted_diagnostics_distinguish_host_and_python_without_raw_text(self):
        self.seed("a", "Graphene transport")
        job = self.submit()

        def invalid_json(_items):
            raise TranslationBridgeError("INVALID_OUTPUT", "secret plaintext", diagnostic="invalid_json")

        self.worker.translate = invalid_json
        self.assertTrue(self.worker.step())
        with self.db() as db:
            db.execute("UPDATE translation_work SET retry_at=0")
        self.worker.translate = lambda _items: {"items": [{"id": "private detail"}]}
        self.assertTrue(self.worker.step())
        result = self.state(job)
        codes = {
            (r["error_code"], r["diagnostic"], r["attempted_inputs"]) for r in result["failure_diagnostics"]
        }
        self.assertEqual(codes, {("INVALID_OUTPUT", "invalid_json", 1), ("INVALID_RESPONSE", "id_mismatch", 1)})
        self.assertNotIn("secret", str(result))
        self.assertNotIn("private detail", str(result))

    def test_usage_migration_keeps_pre_diagnostic_calls_unknown(self):
        self.seed("old", "Quantum transport")
        job = self.submit()
        with self.db() as db:
            for name in ("outcome", "error_code", "diagnostic", "batch_size"):
                db.execute("ALTER TABLE translation_usage DROP COLUMN " + name)
            db.execute(
                """INSERT INTO translation_usage(call_id,job_id,reported,total_tokens,finished_at)
                          VALUES('legacy',?,1,80,1500)""",
                (job["id"],),
            )
        with self.db() as db:
            tr.ensure_translation_schema(db)
            tr.ensure_translation_schema(db)
        result = self.state(job)
        self.assertEqual(result["actual_usage"]["historical_unknown_outcomes"], 1)
        self.assertEqual(result["actual_usage"]["total_tokens"], 80)
        self.assertEqual(result["failure_diagnostics"], [])
