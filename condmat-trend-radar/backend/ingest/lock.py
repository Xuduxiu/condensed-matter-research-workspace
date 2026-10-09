from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from backend.config import db_path, locks_dir
from backend.db.database import utc_now


class IngestLockError(RuntimeError):
    pass


class IngestLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or locks_dir() / "ingest.lock"
        self.acquired = False

    def acquire(self, *, force: bool = False, command: list[str] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            current = read_lock(self.path)
            pid = int(current.get("pid") or 0)
            if pid and is_pid_running(pid):
                raise IngestLockError(f"ingest lock is active: pid={pid}, path={self.path}")
            if not force:
                raise IngestLockError(f"stale ingest lock exists: {self.path}; rerun with --force to clear it")
            self.path.unlink(missing_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": utc_now(),
            "db_path": str(db_path()),
            "command": command or [],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired or not self.path.exists():
            return
        current = read_lock(self.path)
        if int(current.get("pid") or 0) == os.getpid():
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> "IngestLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def read_lock(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except Exception:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        payload: dict[str, Any] = {"raw": raw, "parse_error": True}
        pid_match = re.search(r'"pid"\s*:\s*(\d+)', raw)
        if pid_match:
            payload["pid"] = int(pid_match.group(1))
        command_match = re.search(r'"command"\s*:\s*\[(.*?)\]', raw, re.S)
        if command_match:
            payload["command_text"] = command_match.group(1)
        return payload

def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False