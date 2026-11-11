"""Durable Chinese translations for real library versions. Original metadata stays in its source tables."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from backend.db.database import connect
from backend.llm.translation_diagnostics import VALIDATION_DIAGNOSTICS

DEFAULT_BATCH = 8
DEFAULT_CHARS = 16000
AUTO_JOB = "automatic-zh"
LEASE_SECONDS = 240
MAX_ATTEMPTS = 3
_HAN = re.compile(r"[\u3400-\u9fff]")
_KANA_HANGUL = re.compile(r"[\u3041-\u30ff\uac00-\ud7a3]")
_FORMULA = re.compile(r"(?:[A-Z][a-z]?[\d₀-₉]*|[\s()\[\]+−·./₋-])+")
_SOURCE = """SELECT v.id, v.title, v.abstract FROM paper_versions v
             JOIN papers p ON p.id=v.canonical_paper_id WHERE p.data_mode='real'"""


def ensure_translation_schema(db: sqlite3.Connection) -> None:
    # Each statement participates in the caller's transaction (unlike executescript).
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    # Earlier installs used OR IGNORE inside the trigger, which an outer OR ABORT
    # import can override. Upgrade just those definitions, keeping their dirty rows.
    for name in ("translation_dirty_insert", "translation_dirty_update", "translation_dirty_corpus"):
        old = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if old and "INSERT OR IGNORE INTO translation_dirty" in old[0]:
            db.execute("DROP TRIGGER " + name)
    for statement in (
        """CREATE TABLE IF NOT EXISTS translation_fields (
            field_key TEXT PRIMARY KEY, translated TEXT NOT NULL, provider TEXT, model TEXT, saved_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS translation_work (
            hash TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL, title_key TEXT, abstract_key TEXT,
            status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, lease TEXT, lease_until REAL, error TEXT, provider TEXT, model TEXT,
            batch_hint INTEGER NOT NULL DEFAULT 32)""",
        "CREATE INDEX IF NOT EXISTS translation_work_queue ON translation_work(status, priority DESC, retry_at)",
        "CREATE INDEX IF NOT EXISTS translation_work_lease ON translation_work(lease) WHERE lease IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS translation_jobs (
            id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, scope TEXT NOT NULL, batch_size INTEGER NOT NULL,
            max_input_chars INTEGER NOT NULL, priority INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
            error TEXT, created_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS translation_job_versions (
            job_id TEXT NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES paper_versions(id) ON DELETE CASCADE,
            hash TEXT NOT NULL REFERENCES translation_work(hash), oversized INTEGER NOT NULL, phase TEXT NOT NULL,
            PRIMARY KEY(job_id,version_id))""",
        "CREATE INDEX IF NOT EXISTS translation_job_hash ON translation_job_versions(hash,job_id,oversized)",
        "CREATE INDEX IF NOT EXISTS translation_job_phase ON translation_job_versions(job_id,phase,hash)",
        "CREATE INDEX IF NOT EXISTS translation_member_version ON translation_job_versions(version_id)",
        """CREATE TRIGGER IF NOT EXISTS translation_work_phase AFTER UPDATE OF status ON translation_work
            WHEN NEW.status<>OLD.status BEGIN UPDATE translation_job_versions SET phase=CASE
                WHEN NEW.status IN ('ready','skipped') THEN NEW.status WHEN oversized=1 THEN 'oversized'
                ELSE NEW.status END WHERE hash=NEW.hash; END""",
        """CREATE TABLE IF NOT EXISTS translation_usage (
            call_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, provider TEXT, model TEXT, reported INTEGER NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, finished_at REAL NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS translation_usage_job ON translation_usage(job_id)",
        """CREATE TABLE IF NOT EXISTS translation_policy (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, seeded INTEGER NOT NULL,
            retry_at REAL NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            block_code TEXT, block_count INTEGER NOT NULL DEFAULT 0)""",
        "INSERT OR IGNORE INTO translation_policy(id,enabled,seeded) VALUES (1,0,0)",
        """CREATE TABLE IF NOT EXISTS translation_dirty (
            version_id TEXT PRIMARY KEY REFERENCES paper_versions(id) ON DELETE CASCADE)""",
        """CREATE TRIGGER IF NOT EXISTS translation_dirty_insert AFTER INSERT ON paper_versions
            WHEN (SELECT seeded FROM translation_policy WHERE id=1)=1 BEGIN
            INSERT INTO translation_dirty VALUES(NEW.id) ON CONFLICT(version_id) DO NOTHING; END""",
        """CREATE TRIGGER IF NOT EXISTS translation_dirty_update AFTER UPDATE OF title,abstract,canonical_paper_id ON paper_versions
            WHEN (NEW.title IS NOT OLD.title OR NEW.abstract IS NOT OLD.abstract OR NEW.canonical_paper_id IS NOT OLD.canonical_paper_id)
            AND (SELECT seeded FROM translation_policy WHERE id=1)=1 BEGIN
            INSERT INTO translation_dirty VALUES(NEW.id) ON CONFLICT(version_id) DO NOTHING; END""",
        """CREATE TRIGGER IF NOT EXISTS translation_dirty_corpus AFTER UPDATE OF data_mode ON papers
            WHEN NEW.data_mode IS NOT OLD.data_mode AND (SELECT seeded FROM translation_policy WHERE id=1)=1 BEGIN
            INSERT INTO translation_dirty SELECT id FROM paper_versions WHERE canonical_paper_id=NEW.id
            ON CONFLICT(version_id) DO NOTHING; END""",
    ):
        db.execute(statement)
    existing = {row[1] for row in db.execute("PRAGMA table_info(translation_usage)")}
    for name, kind in (
        ("outcome", "TEXT"),
        ("error_code", "TEXT"),
        ("diagnostic", "TEXT"),
        ("batch_size", "INTEGER"),
    ):
        if name not in existing:
            db.execute(f"ALTER TABLE translation_usage ADD COLUMN {name} {kind}")


def _chunks(items: list, size: int = 300) -> Iterator[list]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _placeholders(items: list) -> str:
    return ",".join("?" for _ in items)


def _hash(*values: str) -> str:
    value = json.dumps(["zh-Hans-v1", *values], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: str | None) -> str:
    return unicodedata.normalize("NFC", (value or "").replace("\r\n", "\n").replace("\r", "\n").strip())


def _field_key(kind: str, text: str) -> str | None:
    if not text or _FORMULA.fullmatch(text):
        return None
    if not _KANA_HANGUL.search(text):
        han = len(_HAN.findall(text))
        foreign = sum(char.isalpha() for char in text) - han
        # Chinese prose commonly contains Latin materials, Greek symbols and units.
        if foreign <= 4 * han or not re.search(r"[^\W\d_]{2,}", text):
            return None
    return _hash(kind, text)


def _source(row: sqlite3.Row) -> dict:
    title, abstract = _text(row["title"]), _text(row["abstract"])
    return {
        "version_id": row["id"],
        "hash": _hash("version", title, abstract),
        "title": title,
        "abstract": abstract,
        "title_key": _field_key("title", title),
        "abstract_key": _field_key("abstract", abstract),
    }


def _source_chunks(
    db: sqlite3.Connection, scope: str, ids: list[str], limit: int = 100
) -> Iterator[list[dict]]:
    if scope == "selected":
        for chunk in _chunks(sorted(set(ids))):
            query = _SOURCE + " AND v.id IN (" + _placeholders(chunk) + ") ORDER BY v.id"
            yield [_source(row) for row in db.execute(query, chunk)]
        return
    if scope == "recent":
        query = (
            _SOURCE
            + " ORDER BY COALESCE(NULLIF(v.publication_date,''),v.submitted_date,v.first_seen_at) DESC,v.id LIMIT ?"
        )
        cursor = db.execute(query, (limit,))
    else:
        cursor = db.execute(_SOURCE + " ORDER BY v.id")
    while rows := cursor.fetchmany(300):
        yield [_source(row) for row in rows]


def _fields(db: sqlite3.Connection, sources: Iterable[dict]) -> dict[str, str]:
    keys = sorted({row[key] for row in sources for key in ("title_key", "abstract_key") if row[key]})
    result = {}
    for batch in _chunks(keys):
        result.update(
            db.execute(
                "SELECT field_key,translated FROM translation_fields WHERE field_key IN ("
                + _placeholders(batch)
                + ")",
                batch,
            ).fetchall()
        )
    return result


def _missing(row: dict, fields: dict) -> tuple[str, str]:
    return tuple(
        row[name] if row[name + "_key"] and row[name + "_key"] not in fields else ""
        for name in ("title", "abstract")
    )


def _ready_status(row: dict, fields: dict) -> str:
    if not row["title_key"] and not row["abstract_key"]:
        return "skipped"
    return "queued" if any(_missing(row, fields)) else "ready"


def _view(row: dict, fields: dict, work: dict | None = None) -> dict:
    state = _ready_status(row, fields)
    if state == "queued":
        state = (work or {}).get("status", "missing")
    result = {
        "version_id": row["version_id"],
        "source_hash": row["hash"],
        "status": state,
        "error": (work or {}).get("error"),
        "provider": (work or {}).get("provider"),
        "model": (work or {}).get("model"),
    }
    for name in ("title", "abstract"):
        key = row[name + "_key"]
        result[name + "_zh"] = fields.get(key) if key else row[name] or None
    return result


def read_cached_translations(db: sqlite3.Connection, rows: Iterable[dict]) -> dict[str, dict]:
    """Read one page by version id; changed metadata can never display another revision's translation."""
    ids = [row["paper_version_id"] for row in rows if row["paper_version_id"]]
    if not ids or not db.execute("SELECT 1 FROM sqlite_master WHERE name='translation_fields'").fetchone():
        return {}
    result = {}
    for batch in _source_chunks(db, "selected", ids):
        fields = _fields(db, batch)
        for row in batch:
            view = _view(row, fields)
            if view["title_zh"] or view["abstract_zh"]:
                result[row["version_id"]] = view
    return result


def get_version(db: sqlite3.Connection, version_id: str) -> dict:
    rows = next(_source_chunks(db, "selected", [version_id]))
    if not rows:
        raise KeyError(version_id)
    row = rows[0]
    work = db.execute(
        "SELECT status,error,provider,model FROM translation_work WHERE hash=?", (row["hash"],)
    ).fetchone()
    view = _view(row, _fields(db, rows), dict(work) if work else None)
    if (
        view["status"] == "queued"
        and db.execute(
            """SELECT min(oversized) FROM translation_job_versions WHERE hash=?""", (row["hash"],)
        ).fetchone()[0]
        == 1
    ):
        view.update(status="failed", error="单篇原文超过本任务输入字符上限")
    return view


def estimate(
    db: sqlite3.Connection, scope: str, ids: list[str], limit: int, batch_size: int, max_chars: int
) -> dict:
    total = cached = skipped = needed = oversized = chars = groups = count = group_chars = unique = 0
    seen_fields: set[str] = set()
    seen_docs: set[str] = set()
    for rows in _source_chunks(db, scope, ids, limit):
        fields = _fields(db, rows)
        for row in rows:
            total += 1
            state = _ready_status(row, fields)
            if state == "ready":
                cached += 1
                continue
            if state == "skipped":
                skipped += 1
                continue
            needed += 1
            title, abstract = _missing(row, fields)
            if len(title) + len(abstract) > max_chars:
                oversized += 1
                continue
            if row["hash"] in seen_docs:
                continue
            seen_docs.add(row["hash"])
            n = 0
            for name, value in (("title", title), ("abstract", abstract)):
                key = row[name + "_key"]
                if value and key not in seen_fields:
                    seen_fields.add(key)
                    n += len(value)
            if not n:
                continue
            if count == batch_size or (count and n + group_chars > max_chars):
                groups += 1
                count = group_chars = 0
            count += 1
            unique += 1
            chars += n
            group_chars += n
    groups += bool(count)
    if scope == "selected" and total != len(set(ids)):
        raise KeyError("选中的论文版本不存在或不是实际资料库记录")
    input_tokens = math.ceil(chars / 4) + 160 * groups + 35 * unique
    output_tokens = math.ceil(chars / 3) + 35 * unique
    return {
        "total": total,
        "cached": cached,
        "skipped": skipped,
        "needs_translation": needed,
        "oversized": oversized,
        "unique_inputs": unique,
        "input_chars": chars,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_requests": groups,
        "batch_size": batch_size,
        "max_input_chars": max_chars,
        "estimate_basis": {
            "input_chars_per_token": 4,
            "source_chars_per_output_token": 3,
            "input_overhead_per_request": 160,
            "overhead_per_item": 35,
            "excludes_retries": True,
        },
        "projection_100k": {
            "versions": 100000,
            "based_on_versions": total,
            "estimated_requests": math.ceil(groups * 100000 / total),
            "estimated_input_tokens": math.ceil(input_tokens * 100000 / total),
            "estimated_output_tokens": math.ceil(output_tokens * 100000 / total),
        }
        if total
        else None,
    }


def _register(db: sqlite3.Connection, rows: list[dict], job_id: str, priority: int, max_chars: int) -> set[str]:
    fields = _fields(db, rows)
    work, members, payable = [], [], set()
    for row in rows:
        state = _ready_status(row, fields)
        size = sum(map(len, _missing(row, fields)))
        work.append(
            (row["hash"], row["title"], row["abstract"], row["title_key"], row["abstract_key"], state, priority)
        )
        over = int(size > max_chars)
        members.append((job_id, row["version_id"], over, over, row["hash"]))
        if state == "queued" and size <= max_chars:
            payable.add(row["hash"])
    db.executemany(
        """INSERT INTO translation_work(hash,title,abstract,title_key,abstract_key,status,priority)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET priority=max(priority,excluded.priority),
        status=CASE WHEN excluded.status='ready' AND status!='running' THEN 'ready' ELSE status END
        WHERE excluded.priority>priority OR (excluded.status='ready' AND status IN ('failed','queued'))""",
        work,
    )
    db.executemany(
        """INSERT INTO translation_job_versions(job_id,version_id,hash,oversized,phase)
        SELECT ?,?,hash,?,CASE WHEN status IN ('ready','skipped') THEN status WHEN ?=1 THEN 'oversized' ELSE status END
        FROM translation_work WHERE hash=? ON CONFLICT(job_id,version_id) DO UPDATE SET
        hash=excluded.hash,oversized=excluded.oversized,phase=excluded.phase""",
        members,
    )
    return payable


def _new_job(db: sqlite3.Connection, job_id: str, scope: str, batch_size: int, max_chars: int, priority: int):
    db.execute(
        """INSERT INTO translation_jobs(id,scope,batch_size,max_input_chars,priority,created_at)
        VALUES(?,?,?,?,?,?)""",
        (job_id, scope, batch_size, max_chars, priority, time.time()),
    )


def submit_job(
    db: sqlite3.Connection,
    scope: str,
    ids: list[str],
    limit: int,
    batch_size: int,
    max_chars: int,
    *,
    priority: bool = False,
    available: bool = True,
) -> dict:
    db.execute("BEGIN IMMEDIATE")
    job_id = uuid.uuid4().hex
    _new_job(db, job_id, scope, batch_size, max_chars, int(priority))
    signature = hashlib.sha256(f"{scope}/{batch_size}/{max_chars}/{int(priority)}".encode())
    count = 0
    chargeable = False
    for rows in _source_chunks(db, scope, ids, limit):
        payable = _register(db, rows, job_id, int(priority), max_chars)
        if payable:
            chargeable = True
        for row in rows:
            signature.update(json.dumps([row["version_id"], row["hash"]], ensure_ascii=False).encode("utf-8"))
        count += len(rows)
    if scope == "selected" and count != len(set(ids)):
        raise KeyError("选中的论文版本不存在或不是实际资料库记录")
    existing = db.execute(
        "SELECT id FROM translation_jobs WHERE fingerprint=?", (signature.hexdigest(),)
    ).fetchone()
    if chargeable and not available and (not existing or priority):
        raise RuntimeError("宿主翻译模型暂不可用")
    if existing:
        db.execute("DELETE FROM translation_jobs WHERE id=?", (job_id,))
        job_id = existing[0]
    else:
        db.execute("UPDATE translation_jobs SET fingerprint=? WHERE id=?", (signature.hexdigest(), job_id))
    if priority:
        _retry(db, job_id)
        db.execute("UPDATE translation_policy SET retry_at=0 WHERE id=1")
    return job_status(db, job_id)


def job_status(db: sqlite3.Connection, job_id: str) -> dict:
    row = db.execute("SELECT * FROM translation_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    counts = dict(
        db.execute(
            "SELECT phase,count(*) FROM translation_job_versions WHERE job_id=? GROUP BY phase", (job_id,)
        )
    )
    total = sum(counts.values())
    pending, running = counts.get("queued", 0), counts.get("running", 0)
    failed = counts.get("failed", 0) + counts.get("oversized", 0)
    status = (
        ("paused" if row["paused"] else "running" if running else "queued")
        if pending or running
        else ("partial" if failed else "completed")
    )
    error = row["error"]
    if not error and failed:
        reason = db.execute(
            """SELECT CASE WHEN m.oversized THEN '单篇原文超过本任务输入字符上限' ELSE w.error END
            FROM translation_job_versions m JOIN translation_work w ON m.hash=w.hash
            WHERE m.job_id=? AND m.phase IN ('failed','oversized') LIMIT 1""",
            (job_id,),
        ).fetchone()
        error = reason[0] if reason else None
    usage = dict(
        db.execute(
            """SELECT count(*) AS calls, coalesce(sum(reported),0) AS reported_calls,
        count(CASE WHEN outcome IS NULL THEN 1 END) AS historical_unknown_outcomes,
        sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,sum(total_tokens) AS total_tokens,
        sum(cache_read_tokens) AS cache_read_tokens,sum(cache_write_tokens) AS cache_write_tokens
        FROM translation_usage WHERE job_id=?""",
            (job_id,),
        ).fetchone()
    )
    diagnostics = [
        dict(item)
        for item in db.execute(
            """SELECT error_code,diagnostic,count(*) AS calls,sum(batch_size) AS attempted_inputs,
                  max(finished_at) AS latest_at FROM translation_usage
           WHERE job_id=? AND outcome='failure' GROUP BY error_code,diagnostic ORDER BY calls DESC,error_code""",
            (job_id,),
        )
    ]
    return {
        "id": job_id,
        "scope": row["scope"],
        "status": status,
        "total": total,
        "completed": counts.get("ready", 0),
        "skipped": counts.get("skipped", 0),
        "pending": pending,
        "running": running,
        "failed": failed,
        "error": error,
        "batch_size": row["batch_size"],
        "max_input_chars": row["max_input_chars"],
        "created_at": row["created_at"],
        "actual_usage": usage,
        "failure_diagnostics": diagnostics,
    }


def _retry(db: sqlite3.Connection, job_id: str):
    db.execute(
        """UPDATE translation_work SET status='queued',error=NULL,attempts=0,retry_at=0
        WHERE status='failed' AND hash IN (SELECT hash FROM translation_job_versions WHERE job_id=? AND oversized=0)""",
        (job_id,),
    )
    db.execute("UPDATE translation_jobs SET paused=0,error=NULL WHERE id=?", (job_id,))


def control_job(db: sqlite3.Connection, job_id: str, action: str) -> dict:
    db.execute("BEGIN IMMEDIATE")
    if action == "retry":
        _retry(db, job_id)
    else:
        db.execute(
            "UPDATE translation_jobs SET paused=?,error=NULL WHERE id=?", (int(action == "pause"), job_id)
        )
    if job_id == AUTO_JOB:
        db.execute("UPDATE translation_policy SET enabled=? WHERE id=1", (int(action != "pause"),))
    if action != "pause":
        db.execute("UPDATE translation_policy SET retry_at=0 WHERE id=1")
    return job_status(db, job_id)


def automatic_status(db: sqlite3.Connection) -> dict:
    policy = db.execute("SELECT * FROM translation_policy WHERE id=1").fetchone()
    if not policy["seeded"]:
        return {
            "enabled": False,
            "blocked": False,
            "error": None,
            "job_id": None,
            "batch_size": DEFAULT_BATCH,
            "max_input_chars": DEFAULT_CHARS,
            "total": 0,
            "pending": 0,
            "running": 0,
            "failed": 0,
            "completed": 0,
            "skipped": 0,
            "dirty": 0,
        }
    result = job_status(db, AUTO_JOB)
    paused = db.execute("SELECT paused FROM translation_jobs WHERE id=?", (AUTO_JOB,)).fetchone()[0]
    return {
        **result,
        "job_id": AUTO_JOB,
        "enabled": bool(policy["enabled"]),
        "blocked": bool(policy["enabled"] and paused),
        "dirty": db.execute("SELECT COUNT(*) FROM translation_dirty").fetchone()[0],
    }


def set_automatic(
    db: sqlite3.Connection, enabled: bool, batch_size: int | None, max_chars: int | None, available: bool = True
) -> dict:
    db.execute("BEGIN IMMEDIATE")
    policy = db.execute("SELECT * FROM translation_policy WHERE id=1").fetchone()
    if not policy["seeded"] and not enabled:
        return automatic_status(db)
    if not policy["seeded"]:
        _new_job(db, AUTO_JOB, "automatic", batch_size or DEFAULT_BATCH, max_chars or DEFAULT_CHARS, 0)
        for rows in _source_chunks(db, "all", []):
            _register(db, rows, AUTO_JOB, 0, max_chars or DEFAULT_CHARS)
        db.execute("UPDATE translation_policy SET seeded=1 WHERE id=1")
    elif max_chars is not None:
        current = db.execute("SELECT max_input_chars FROM translation_jobs WHERE id=?", (AUTO_JOB,)).fetchone()[
            0
        ]
        if current != max_chars:
            db.execute(
                """INSERT OR IGNORE INTO translation_dirty SELECT version_id FROM translation_job_versions
                          WHERE job_id=?""",
                (AUTO_JOB,),
            )
    db.execute("UPDATE translation_policy SET enabled=? WHERE id=1", (int(enabled),))
    db.execute(
        """UPDATE translation_jobs SET paused=?,error=NULL,batch_size=coalesce(?,batch_size),
                  max_input_chars=coalesce(?,max_input_chars) WHERE id=?""",
        (int(not enabled), batch_size, max_chars, AUTO_JOB),
    )
    if enabled:
        if available:
            db.execute("UPDATE translation_policy SET retry_at=0,block_code=NULL,block_count=0 WHERE id=1")
        else:
            _block(db, "MODEL_UNAVAILABLE", time.time())
    return automatic_status(db)


def automatic_enabled(db: sqlite3.Connection) -> bool:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='translation_policy'"
    ).fetchone():
        return False
    row = db.execute("SELECT enabled FROM translation_policy WHERE id=1").fetchone()
    return bool(row and row[0])


def _flush_dirty(db: sqlite3.Connection, max_chars: int) -> int:
    dirty = [row[0] for row in db.execute("SELECT version_id FROM translation_dirty LIMIT 256")]
    if not dirty:
        return 0
    rows = next(_source_chunks(db, "selected", dirty))
    valid = {row["version_id"] for row in rows}
    db.executemany(
        "DELETE FROM translation_job_versions WHERE job_id=? AND version_id=?",
        [(AUTO_JOB, vid) for vid in dirty if vid not in valid],
    )
    _register(db, rows, AUTO_JOB, 0, max_chars)
    db.executemany("DELETE FROM translation_dirty WHERE version_id=?", [(vid,) for vid in dirty])
    return len(dirty)


_ACTIVE_JOB = """SELECT j.* FROM translation_jobs j WHERE paused=0 AND EXISTS (
    SELECT 1 FROM translation_job_versions m WHERE m.job_id=j.id AND m.phase='queued')
    ORDER BY priority DESC, created_at, id"""


def _auto_dirty(db: sqlite3.Connection):
    return db.execute(
        """SELECT j.max_input_chars FROM translation_jobs j,translation_policy p
        WHERE j.id=? AND p.id=1 AND p.enabled AND NOT j.paused AND EXISTS(SELECT 1 FROM translation_dirty)""",
        (AUTO_JOB,),
    ).fetchone()


def _claim(db: sqlite3.Connection, now: float) -> tuple[dict | None, bool]:
    # Cheap read-only idle path, including a 100k-version paused job.
    dirty = _auto_dirty(db)
    expired = db.execute(
        "SELECT 1 FROM translation_work WHERE status='running' AND lease_until<=? LIMIT 1", (now,)
    ).fetchone()
    if not dirty and not expired and not db.execute(_ACTIVE_JOB + " LIMIT 1").fetchone():
        return None, False
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        """UPDATE translation_work SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
        lease=NULL,lease_until=NULL,error='上一次模型请求未返回可保存的结果'
        WHERE status='running' AND lease_until<=?""",
        (MAX_ATTEMPTS, now),
    )
    dirty = _auto_dirty(db)
    progressed = bool(dirty and _flush_dirty(db, dirty[0]))
    if db.execute("SELECT 1 FROM translation_work WHERE status='running' LIMIT 1").fetchone():
        return None, progressed
    if db.execute("SELECT retry_at FROM translation_policy WHERE id=1").fetchone()[0] > now:
        return None, progressed
    for job in db.execute(_ACTIVE_JOB):
        candidates = db.execute(
            """SELECT w.*,v.id AS version_id,v.title AS live_title,
            v.abstract AS live_abstract,p.data_mode FROM translation_job_versions m
            JOIN translation_work w ON w.hash=m.hash JOIN paper_versions v ON v.id=m.version_id
            JOIN papers p ON p.id=v.canonical_paper_id WHERE m.job_id=? AND m.phase='queued'
            AND w.retry_at<=? ORDER BY m.hash LIMIT ?""",
            (job["id"], now, job["batch_size"]),
        ).fetchall()
        current = {}
        for candidate in candidates:
            if candidate["data_mode"] != "real":
                db.execute(
                    "DELETE FROM translation_job_versions WHERE job_id=? AND version_id=?",
                    (job["id"], candidate["version_id"]),
                )
                progressed = True
            elif (_text(candidate["live_title"]), _text(candidate["live_abstract"])) != (
                candidate["title"],
                candidate["abstract"],
            ):
                # A queued snapshot can outlive an edit/import even when automatic mode is off.
                updated = _source(
                    {
                        "id": candidate["version_id"],
                        "title": candidate["live_title"],
                        "abstract": candidate["live_abstract"],
                    }
                )
                _register(db, [updated], job["id"], job["priority"], job["max_input_chars"])
                progressed = True
            else:
                current[candidate["hash"]] = dict(candidate)
        rows = list(current.values())
        if not rows:
            continue
        fields = _fields(db, rows)
        seen: set[str] = set()
        items, selected = [], []
        chars = 0
        for row in rows:
            if _ready_status(row, fields) == "ready":
                db.execute("UPDATE translation_work SET status='ready',error=NULL WHERE hash=?", (row["hash"],))
                progressed = True
                continue
            title, abstract = _missing(row, fields)
            title = title if row["title_key"] not in seen else ""
            abstract = abstract if row["abstract_key"] not in seen else ""
            size = len(title) + len(abstract)
            if chars + size > job["max_input_chars"] or len(selected) >= min(
                row["batch_hint"], rows[0]["batch_hint"]
            ):
                break
            if size:
                items.append({"id": row["hash"], "title": title, "abstract": abstract})
                if title:
                    seen.add(row["title_key"])
                if abstract:
                    seen.add(row["abstract_key"])
            chars += size
            selected.append(row)
        if not items:
            continue
        lease = uuid.uuid4().hex
        db.executemany(
            """UPDATE translation_work SET status='running',attempts=attempts+1,lease=?,
            lease_until=?,error=NULL WHERE hash=? AND status='queued'""",
            [(lease, now + LEASE_SECONDS, row["hash"]) for row in selected],
        )
        return {"lease": lease, "job": job["id"], "items": items, "rows": selected}, True
    return None, progressed


class _InvalidReply(Exception):
    code = "INVALID_RESPONSE"
    retryable = True

    def __init__(self, diagnostic: str):
        self.diagnostic = diagnostic
        super().__init__(diagnostic)


def _validated(items: list[dict], response: dict) -> dict:
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise _InvalidReply("invalid_envelope")
    copy_only = response.get("no_inference", False)
    if not isinstance(copy_only, bool):
        raise _InvalidReply("invalid_envelope")
    if copy_only and (
        len(items) != 1
        or len(response["items"]) != 1
        or any(key not in response or response[key] is not None for key in ("provider", "model", "usage"))
    ):
        raise _InvalidReply("invalid_envelope")
    expected = {item["id"]: item for item in items}
    result = {}
    for row in response["items"]:
        if not isinstance(row, dict):
            raise _InvalidReply("invalid_fields")
        key = row.get("id")
        if not isinstance(key, str) or key not in expected:
            raise _InvalidReply("id_mismatch")
        if key in result:
            raise _InvalidReply("duplicate_id")
        if copy_only and set(row) != {"id", "title_zh", "abstract_zh"}:
            raise _InvalidReply("invalid_fields")
        values = {}
        for name in ("title", "abstract"):
            original = expected[key][name]
            value = row.get(name + "_zh")
            if copy_only:
                # The private native adapter owns the math segmentation. Its local-copy
                # receipt may bypass language checks only for exact source strings,
                # including empty fields, spacing, TeX escapes and MathML.
                if value != original:
                    raise _InvalidReply("math_literal_changed")
                if original:
                    values[name] = original
                continue
            if not original:
                continue
            if not isinstance(value, str) or not value.strip():
                raise _InvalidReply("empty_mismatch")
            text = value.strip()
            # Chemical notation can have many space-separated tokens with no
            # words to translate. Keep the same conservative prose rule as the host.
            plain = re.sub(r"<[^>]*>", " ", original)
            if not _HAN.search(text) and len(re.findall(r"\b[a-zA-Z]*[a-z]{2,}[a-zA-Z]*\b", plain)) >= 2:
                raise _InvalidReply("missing_han")
            values[name] = text
        result[key] = values
    if result.keys() != expected.keys():
        raise _InvalidReply("id_mismatch")
    return result


def _record_usage(db, batch, provider, model, usage, now, *, outcome="success", code=None, diagnostic=None):
    fields = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens")
    values = [usage.get(key) if isinstance(usage, dict) else None for key in fields]
    values = [value if type(value) is int and 0 <= value < 2**63 else None for value in values]
    db.execute(
        """INSERT OR IGNORE INTO translation_usage(call_id,job_id,provider,model,reported,
        input_tokens,output_tokens,total_tokens,cache_read_tokens,cache_write_tokens,finished_at,
        outcome,error_code,diagnostic,batch_size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            batch["lease"],
            batch["job"],
            provider,
            model,
            int(any(v is not None for v in values)),
            *values,
            now,
            outcome,
            code,
            diagnostic,
            len(batch["items"]),
        ),
    )


def _finish(db, batch, response, validated, now):
    db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT 1 FROM translation_work WHERE lease=?", (batch["lease"],)).fetchone():
        return
    provider, model = response.get("provider"), response.get("model")
    copy_only = response.get("no_inference") is True
    if not copy_only:
        _record_usage(db, batch, provider, model, response.get("usage"), now)
    values = []
    for row in batch["rows"]:
        for kind, value in validated.get(row["hash"], {}).items():
            values.append((row[kind + "_key"], value, provider, model, now))
    db.executemany("INSERT OR IGNORE INTO translation_fields VALUES(?,?,?,?,?)", values)
    db.execute(
        """UPDATE translation_work SET status='ready',error=NULL,lease=NULL,lease_until=NULL,
                provider=?,model=?,attempts=max(0,attempts-?) WHERE lease=?""",
        (provider, model, int(copy_only), batch["lease"]),
    )
    if copy_only:
        return  # Local copying cannot establish that a previously blocked model has recovered.
    db.execute("UPDATE translation_jobs SET paused=0,error=NULL WHERE paused AND error IS NOT NULL")
    db.execute(
        "UPDATE translation_policy SET retry_at=0,consecutive_failures=0,block_code=NULL,block_count=0 WHERE id=1"
    )


_ERRORS = {
    "INVALID_INPUT": "宿主拒绝了无效的翻译输入",
    "PROVIDER_ERROR": "宿主模型请求失败",
    "AUTH": "宿主模型凭据不可用",
    "QUOTA": "宿主模型配额不足",
    "MODEL_UNAVAILABLE": "宿主模型暂不可用",
    "RATE_LIMIT": "模型服务暂时限流",
    "TIMEOUT": "模型请求超时",
    "CANCELLED": "模型请求中止",
    "CONTEXT_TOO_LARGE": "模型输入超过上下文窗口",
    "TRUNCATED": "模型输出被截断",
    "INVALID_OUTPUT": "宿主模型输出校验未通过",
    "INVALID_RESPONSE": "本地译文完整性校验未通过",
}


def _block(db, code: str, now: float):
    old = db.execute("SELECT block_count,block_code FROM translation_policy WHERE id=1").fetchone()
    count = old[0] + 1 if old[1] in (code, None) else 1
    delay = min(21600, (600 if code in ("AUTH", "QUOTA") else 30) * 2 ** min(10, count - 1))
    db.execute(
        """UPDATE translation_jobs SET paused=1,error=? WHERE paused=0 AND
        (id=? OR EXISTS(SELECT 1 FROM translation_job_versions m WHERE m.job_id=translation_jobs.id AND m.phase='queued'))""",
        (_ERRORS.get(code, "宿主模型暂不可用"), AUTO_JOB),
    )
    db.execute(
        """UPDATE translation_policy SET consecutive_failures=0,retry_at=?,block_code=?,block_count=? WHERE id=1""",
        (now + delay, code, count),
    )


def _fail(db, batch, exc: Exception, now: float, closing: bool, response=None):
    db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT 1 FROM translation_work WHERE lease=?", (batch["lease"],)).fetchone():
        return
    code = getattr(exc, "code", "PROVIDER_ERROR")
    message = _ERRORS.get(code, "模型请求失败")
    diagnostic = getattr(exc, "diagnostic", None)
    diagnostic = diagnostic if isinstance(diagnostic, str) and diagnostic in VALIDATION_DIAGNOSTICS else None
    if diagnostic:
        message += "（" + VALIDATION_DIAGNOSTICS[diagnostic] + "）"
    response = response if isinstance(response, dict) else {}
    _record_usage(
        db,
        batch,
        response.get("provider") or getattr(exc, "provider", None),
        response.get("model") or getattr(exc, "model", None),
        response.get("usage") or getattr(exc, "usage", None),
        now,
        outcome="failure",
        code=code if code in _ERRORS else "PROVIDER_ERROR",
        diagnostic=diagnostic,
    )
    if closing:
        db.execute(
            """UPDATE translation_work SET status='queued',lease=NULL,lease_until=NULL,retry_at=0,
                    attempts=max(0,attempts-1) WHERE lease=?""",
            (batch["lease"],),
        )
        return
    shape_error = code in {
        "CONTEXT_TOO_LARGE",
        "TRUNCATED",
        "INVALID_OUTPUT",
        "INVALID_INPUT",
        "INVALID_RESPONSE",
    }
    if shape_error and len(batch["items"]) > 1:
        db.execute(
            """UPDATE translation_work SET status='queued',lease=NULL,lease_until=NULL,retry_at=0,
            attempts=max(0,attempts-1),batch_hint=min(batch_hint,?),error=? WHERE lease=?""",
            (max(1, len(batch["items"]) // 2), message, batch["lease"]),
        )
        return
    failures = db.execute("SELECT consecutive_failures FROM translation_policy WHERE id=1").fetchone()[0] + 1
    systemic = code in {"AUTH", "QUOTA", "MODEL_UNAVAILABLE", "CANCELLED"} or (
        failures >= MAX_ATTEMPTS and code in {"RATE_LIMIT", "TIMEOUT", "PROVIDER_ERROR"}
    )
    # A model may omit an id or truncate a response on one attempt but succeed
    # on the next. A single input that exceeds the model context cannot recover this way.
    retryable = code in {"INVALID_OUTPUT", "INVALID_RESPONSE", "TRUNCATED"} or (
        code not in {"INVALID_INPUT", "CONTEXT_TOO_LARGE"} and bool(getattr(exc, "retryable", False))
    )
    if systemic:
        db.execute(
            """UPDATE translation_work SET status='queued',lease=NULL,lease_until=NULL,retry_at=0,
                    attempts=max(0,attempts-1),error=? WHERE lease=?""",
            (message, batch["lease"]),
        )
        _block(db, code, now)
        return
    delay = (30 if code == "RATE_LIMIT" else 4) * 2 ** min(failures - 1, 4)
    db.execute(
        """UPDATE translation_work SET status=CASE WHEN ? AND attempts<? THEN 'queued' ELSE 'failed' END,
        lease=NULL,lease_until=NULL,retry_at=?,error=? WHERE lease=?""",
        (int(retryable), MAX_ATTEMPTS, now + delay, message, batch["lease"]),
    )
    db.execute(
        "UPDATE translation_policy SET consecutive_failures=?,retry_at=? WHERE id=1",
        (failures, now + delay if code in {"RATE_LIMIT", "TIMEOUT", "PROVIDER_ERROR"} else 0),
    )


class TranslationWorker:
    def __init__(
        self,
        db_path: Path,
        translate: Callable,
        *,
        model: str = "dsh",
        available: Callable[[], bool] | None = None,
        provider_status: Callable | None = None,
        on_auto_change: Callable[[bool], None] | None = None,
    ):
        self.db_path, self.translate, self.model = db_path, translate, model
        self._available, self._provider_status = available, provider_status
        self.on_auto_change = on_auto_change
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def provider_status(self) -> dict:
        if self._provider_status is not None:
            return self._provider_status()
        ok = self._available() if self._available is not None else True
        return {
            "available": bool(ok),
            "provider": None,
            "model": self.model if ok else None,
            "reason": None if ok else "MODEL_UNAVAILABLE",
        }

    def available(self) -> bool:
        return bool(self.provider_status().get("available"))

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="paper-translation", daemon=True)
            self._thread.start()

    def notify(self):
        self._wake.set()

    def request_stop(self):
        self._stopping.set()
        self.notify()

    def close(self):
        self.request_stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)

    def step(self) -> bool:
        if self._stopping.is_set():
            return False
        with connect(self.db_path) as db:
            blocked = db.execute(
                """SELECT block_code,retry_at FROM translation_policy WHERE id=1 AND block_code IS NOT NULL
                AND retry_at<=? AND EXISTS(SELECT 1 FROM translation_jobs WHERE paused AND error IS NOT NULL)""",
                (time.time(),),
            ).fetchone()
        if blocked:
            available = self.available()
            with connect(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                # Recheck after host capability I/O. Other DSH processes may have recovered the block.
                same = db.execute(
                    "SELECT 1 FROM translation_policy WHERE id=1 AND block_code=? AND retry_at=?",
                    (blocked[0], blocked[1]),
                ).fetchone()
                if same and available:
                    db.execute(
                        "UPDATE translation_jobs SET paused=0,error=NULL WHERE paused AND error IS NOT NULL"
                    )
                    db.execute("UPDATE translation_policy SET block_code=NULL,retry_at=0 WHERE id=1")
                elif same:
                    _block(db, "MODEL_UNAVAILABLE", time.time())
        with connect(self.db_path) as db:
            batch, progressed = _claim(db, time.time())
        if batch is None:
            return progressed
        response = None
        try:
            response = self.translate(batch["items"])
            values = _validated(batch["items"], response)
        except Exception as exc:  # noqa: BLE001 -- an adapter failure must settle the durable lease
            with connect(self.db_path) as db:
                _fail(db, batch, exc, time.time(), self._stopping.is_set(), response)
        else:
            with connect(self.db_path) as db:
                _finish(db, batch, response, values, time.time())
        return True

    def _run(self):
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                if self.step():
                    continue
            except sqlite3.OperationalError as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "Translation scheduling unavailable (%s)", exc.sqlite_errorname
                )
            self._wake.wait(3)
