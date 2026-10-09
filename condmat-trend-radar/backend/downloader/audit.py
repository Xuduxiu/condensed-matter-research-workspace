from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.library.repository import utc_now


SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "awsaccesskeyid",
    "auth",
    "authorization",
    "code",
    "credential",
    "googleaccessid",
    "jsessionid",
    "key",
    "key-pair-id",
    "password",
    "policy",
    "relaystate",
    "samlresponse",
    "secret",
    "session",
    "sessionid",
    "sig",
    "signature",
    "state",
    "ticket",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
    "x-goog-credential",
    "x-goog-signature",
}


def redact_url(url: Any) -> str:
    """Keep an auditable URL without persisting credentials or signed tokens."""
    clean = str(url or "").strip()
    if not clean:
        return ""
    try:
        parsed = urlsplit(clean)
        safe_query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.casefold()
            sensitive = normalized in SENSITIVE_QUERY_KEYS or normalized.startswith("x-amz-")
            safe_query.append((key, "***REDACTED***" if sensitive else value))
        hostname = parsed.hostname or ""
        safe_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        if parsed.port is not None:
            safe_host = f"{safe_host}:{parsed.port}"
        return urlunsplit((parsed.scheme, safe_host, parsed.path, urlencode(safe_query), ""))
    except ValueError:
        return clean.split("?", 1)[0]


def redact_text(value: Any) -> str:
    """Redact credential-like query values from diagnostic text."""
    import re

    text = str(value or "")
    text = re.sub(r"https?://[^\s'\"<>]+", lambda match: redact_url(match.group(0)), text)
    for key in SENSITIVE_QUERY_KEYS:
        text = re.sub(
            rf"(?i)({re.escape(key)}\s*[=:]\s*)[^&\s,;]+",
            rf"\1***REDACTED***",
            text,
        )
    return text


def ensure_download_audit_schema(connection: sqlite3.Connection) -> None:
    """Install additive download audit/link tables on old and new databases."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS download_candidate_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            candidate_index INTEGER NOT NULL,
            candidate_source TEXT NOT NULL,
            access_basis TEXT NOT NULL DEFAULT 'open_access',
            candidate_reason TEXT,
            source_url TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            http_status INTEGER,
            content_type TEXT,
            final_url TEXT,
            bytes_received INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            failure_class TEXT,
            error_message TEXT,
            validation_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(task_id, attempt_number, candidate_index),
            FOREIGN KEY(task_id) REFERENCES download_tasks(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_candidate_task "
        "ON download_candidate_attempts(task_id, attempt_number, candidate_index)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_download_candidate_outcome "
        "ON download_candidate_attempts(status, candidate_source, failure_class)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_file_links (
            paper_file_id TEXT NOT NULL,
            canonical_paper_id TEXT NOT NULL,
            paper_version_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(paper_file_id, canonical_paper_id, paper_version_id),
            FOREIGN KEY(paper_file_id) REFERENCES paper_files(id) ON DELETE CASCADE,
            FOREIGN KEY(canonical_paper_id) REFERENCES papers(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_file_links_paper "
        "ON paper_file_links(canonical_paper_id, paper_version_id)"
    )
    for table_name in ("download_candidate_attempts", "download_tasks", "paper_files"):
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not table_exists:
            continue
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}
        if "access_basis" not in columns:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN access_basis TEXT NOT NULL DEFAULT 'open_access'"
            )


def start_candidate_attempt(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    attempt_number: int,
    candidate_index: int,
    source: str,
    access_basis: str = "open_access",
    reason: str,
    source_url: str,
    started_at: str | None = None,
) -> None:
    ensure_download_audit_schema(connection)
    connection.execute(
        """
        INSERT INTO download_candidate_attempts
        (task_id, attempt_number, candidate_index, candidate_source,
         access_basis, candidate_reason, source_url, started_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'downloading')
        ON CONFLICT(task_id, attempt_number, candidate_index) DO UPDATE SET
          candidate_source=excluded.candidate_source,
          access_basis=excluded.access_basis,
          candidate_reason=excluded.candidate_reason,
          source_url=excluded.source_url,
          started_at=excluded.started_at,
          finished_at=NULL,
          status='downloading',
          http_status=NULL,
          content_type=NULL,
          final_url=NULL,
          bytes_received=0,
          sha256=NULL,
          failure_class=NULL,
          error_message=NULL,
          validation_json='{}'
        """,
        (
            task_id,
            attempt_number,
            candidate_index,
            source,
            access_basis,
            reason,
            redact_url(source_url),
            started_at or utc_now(),
        ),
    )


def finish_candidate_attempt(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    attempt_number: int,
    candidate_index: int,
    status: str,
    http_status: int | None = None,
    content_type: str | None = None,
    final_url: str | None = None,
    bytes_received: int = 0,
    sha256: str | None = None,
    failure_class: str | None = None,
    error_message: str | None = None,
    validation: Mapping[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        UPDATE download_candidate_attempts
        SET finished_at=?, status=?, http_status=?, content_type=?, final_url=?,
            bytes_received=?, sha256=?, failure_class=?, error_message=?,
            validation_json=?
        WHERE task_id=? AND attempt_number=? AND candidate_index=?
        """,
        (
            utc_now(),
            status,
            http_status,
            str(content_type or "")[:200],
            redact_url(final_url),
            max(0, int(bytes_received or 0)),
            sha256,
            failure_class,
            redact_text(error_message)[:1000] or None,
            json.dumps(dict(validation or {}), ensure_ascii=False, sort_keys=True),
            task_id,
            attempt_number,
            candidate_index,
        ),
    )


def candidate_attempts_for_task(connection: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    ensure_download_audit_schema(connection)
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM download_candidate_attempts
            WHERE task_id=? ORDER BY attempt_number, candidate_index
            """,
            (task_id,),
        )
    ]
    for row in rows:
        try:
            row["validation"] = json.loads(row.pop("validation_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            row["validation"] = {}
    return rows


def download_audit_statistics(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_download_audit_schema(connection)
    by_source = [
        dict(row)
        for row in connection.execute(
            """
            SELECT candidate_source AS source,
                   COALESCE(NULLIF(access_basis, ''), 'open_access') AS access_basis,
                   COUNT(*) AS attempts,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS successes,
                   ROUND(100.0 * SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                         / NULLIF(COUNT(*), 0), 1) AS success_rate
            FROM download_candidate_attempts
            GROUP BY candidate_source, COALESCE(NULLIF(access_basis, ''), 'open_access')
            ORDER BY attempts DESC, source
            """
        )
    ]
    failure_classes = [
        dict(row)
        for row in connection.execute(
            """
            SELECT COALESCE(NULLIF(failure_class, ''), 'unknown') AS failure_class,
                   COUNT(*) AS count
            FROM download_candidate_attempts
            WHERE status<>'completed'
            GROUP BY COALESCE(NULLIF(failure_class, ''), 'unknown')
            ORDER BY count DESC, failure_class
            """
        )
    ]
    task_outcomes = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM download_tasks GROUP BY status"
        )
    }
    return {
        "candidate_sources": by_source,
        "failure_classes": failure_classes,
        "task_outcomes": task_outcomes,
    }
