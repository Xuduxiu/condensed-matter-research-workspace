"""Private newline-delimited JSON RPC with the DSH process that owns this workspace."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, BinaryIO
from uuid import uuid4

from backend.llm.translation_diagnostics import VALIDATION_DIAGNOSTICS


class TranslationBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, *, model=None, provider=None, usage=None,
                 diagnostic=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.model = model
        self.provider = provider
        self.usage = usage
        self.diagnostic = diagnostic if isinstance(diagnostic, str) and diagnostic in VALIDATION_DIAGNOSTICS else None


@dataclass
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


class DshTranslationBridge:
    def __init__(self, source: BinaryIO, sink: BinaryIO, on_disconnect: Callable[[], None],
                 on_activate: Callable[[], None] | None = None):
        self._source = source
        self._sink = sink
        self._on_disconnect = on_disconnect
        self._on_activate = on_activate
        self._lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self._status_lock = threading.Lock()
        self._status_at = float("-inf")
        self._status: dict[str, Any] = {}

    def start(self) -> None:
        threading.Thread(target=self._read, name="dsh-translation-rpc", daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self._source:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "paper-desk:activate-ui" and self._on_activate is not None:
                    self._on_activate()
                    continue
                if message.get("type") != "paper-desk:translation-result":
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    continue
                with self._lock:
                    pending = self._pending.get(request_id)
                    if pending and not pending.done.is_set():
                        pending.response = message
                        pending.done.set()
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._on_disconnect()
            finally:
                self.close()

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with self._output_lock:
                self._sink.write(payload)
                self._sink.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.close()
            raise TranslationBridgeError("MODEL_UNAVAILABLE", "DSH 模型连接已关闭。") from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for pending in self._pending.values():
                pending.done.set()

    def _call(self, op: str, timeout: float, **params: Any) -> dict[str, Any]:
        request_id = uuid4().hex
        pending = _Pending()
        with self._lock:
            if self._closed:
                raise TranslationBridgeError("MODEL_UNAVAILABLE", "DSH 模型连接未建立。")
            self._pending[request_id] = pending
        try:
            self.send({"type": "paper-desk:translation", "id": request_id, "op": op, **params})
            if not pending.done.wait(timeout):
                self.send({"type": "paper-desk:translation", "id": request_id, "op": "cancel"})
                raise TranslationBridgeError("TIMEOUT", "DSH 模型响应超时。", True)
            reply = pending.response
            if not reply:
                raise TranslationBridgeError("MODEL_UNAVAILABLE", "DSH 模型连接已断开。")
            if reply.get("ok") is True and isinstance(reply.get("result"), dict):
                return reply["result"]
            error = reply.get("error") or {}
            if not isinstance(error, dict):
                error = {}
            raise TranslationBridgeError(str(error.get("code") or "PROVIDER_ERROR"),
                                         str(error.get("message") or "DSH 未返回可用结果。"),
                                         error.get("retryable") is True, model=error.get("model"),
                                         provider=error.get("provider"), usage=error.get("usage"),
                                         diagnostic=error.get("diagnostic"))
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            if time.monotonic() - self._status_at >= 5:
                try:
                    self._status = self._call("status", 5)
                except TranslationBridgeError as exc:
                    self._status = {"available": False, "provider": None, "model": None, "reason": exc.code}
                self._status_at = time.monotonic()
            return dict(self._status)

    def available(self) -> bool:
        return self.status().get("available") is True

    def translate(self, items: list[dict[str, str]]) -> dict[str, Any]:
        # Let the native adapter finish its 180-second deadline and report any usage.
        # This RPC deadline still precedes expiry of the SQLite worker lease.
        return self._call("translate", 195, items=items)
