import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from backend.llm.dsh_translation_bridge import DshTranslationBridge, TranslationBridgeError


class BridgeTests(unittest.TestCase):
    def setUp(self):
        host_read, child_write = os.pipe()
        child_read, host_write = os.pipe()
        self.requests = os.fdopen(host_read, "rb")
        self.replies = os.fdopen(host_write, "wb")
        self.source = os.fdopen(child_read, "rb")
        self.sink = os.fdopen(child_write, "wb")
        self.disconnect = threading.Event()
        self.activate = threading.Event()
        self.bridge = DshTranslationBridge(self.source, self.sink, self.disconnect.set, self.activate.set)
        self.bridge.start()
        self.pool = ThreadPoolExecutor(max_workers=2)

    def tearDown(self):
        self.replies.close()
        self.bridge.close()
        self.assertTrue(self.disconnect.wait(2))
        self.pool.shutdown()
        for file in (self.requests, self.source, self.sink):
            file.close()

    def reply(self, content):
        self.replies.write(json.dumps(content, ensure_ascii=False).encode() + b"\n")
        self.replies.flush()

    def test_interleaved_replies_and_activation(self):
        calls = [self.pool.submit(self.bridge.translate, [{"id": value, "title": "MoS₂", "abstract": ""}])
                 for value in ["a", "b"]]
        requests = [json.loads(self.requests.readline()), json.loads(self.requests.readline())]
        self.reply({"type": "paper-desk:activate-ui"})
        self.assertTrue(self.activate.wait(2))
        for request in reversed(requests):
            self.assertEqual(request["items"][0]["title"], "MoS₂")
            self.reply({"type": "paper-desk:translation-result", "id": request["id"], "ok": True,
                        "result": {"items": [request["items"][0]["id"]]}})
        self.assertEqual([call.result(2)["items"] for call in calls], [["a"], ["b"]])

    def test_timeout_cancels_and_disconnect_releases_pending_request(self):
        with self.assertRaises(TranslationBridgeError) as timeout:
            self.bridge._call("status", 0.01)
        first, cancel = json.loads(self.requests.readline()), json.loads(self.requests.readline())
        self.assertEqual((cancel["op"], cancel["id"]), ("cancel", first["id"]))
        self.assertEqual(timeout.exception.code, "TIMEOUT")
        waiting = self.pool.submit(self.bridge.translate, [])
        self.requests.readline()
        self.replies.close()
        with self.assertRaises(TranslationBridgeError) as closed:
            waiting.result(2)
        self.assertEqual(closed.exception.code, "MODEL_UNAVAILABLE")

    def test_account_error_preserves_returned_usage_and_model(self):
        waiting = self.pool.submit(self.bridge.translate, [])
        request = json.loads(self.requests.readline())
        self.reply({"type": "paper-desk:translation-result", "id": request["id"], "ok": False,
                    "error": {"code": "QUOTA", "message": "额度不足", "retryable": False,
                              "model": "fixture", "usage": {"input_tokens": 41}}})
        with self.assertRaises(TranslationBridgeError) as caught:
            waiting.result(2)
        self.assertEqual((caught.exception.code, caught.exception.model, caught.exception.usage["input_tokens"]),
                         ("QUOTA", "fixture", 41))

    def test_only_known_validation_diagnostics_cross_the_bridge(self):
        for incoming, expected in [("id_mismatch", "id_mismatch"), ("invalid_delimiters", "invalid_delimiters"),
                                   ("untrusted provider text", None), ([], None)]:
            with self.subTest(incoming=incoming):
                waiting = self.pool.submit(self.bridge.translate, [])
                request = json.loads(self.requests.readline())
                self.reply({"type": "paper-desk:translation-result", "id": request["id"], "ok": False,
                            "error": {"code": "INVALID_OUTPUT", "diagnostic": incoming}})
                with self.assertRaises(TranslationBridgeError) as caught:
                    waiting.result(2)
                self.assertEqual(caught.exception.diagnostic, expected)
