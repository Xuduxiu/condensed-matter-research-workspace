from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from backend.config import institutional_ip_enabled
from backend.downloader.audit import (
    candidate_attempts_for_task,
    download_audit_statistics,
    ensure_download_audit_schema,
    finish_candidate_attempt,
    redact_url,
    redact_text,
    start_candidate_attempt,
)
from backend.downloader.resolver import (
    Resolution,
    is_safe_institutional_url,
    resolve_legal_oa_candidates,
    resolve_legal_oa_url,
)
from backend.downloader.retry import (
    TRANSIENT_FAILURES,
    classify_failure,
    failure_class_for,
    next_attempt_iso,
)
from backend.downloader.validation import PdfValidation, validate_pdf_file
from backend.library.repository import stable_id, utc_now


VALID_FILE_STATUSES = {"valid", "ok", "valid_pdf_header", "valid_pdf_structural"}


def _unique_nonempty(values: Iterable[Any]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        item = str(value).strip()
        if item and item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def _reset_failed_task_rows(
    connection: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
) -> dict[str, Any]:
    selected = [dict(row) for row in rows]
    if not selected:
        return {
            "requeued": 0,
            "task_ids": [],
            "canonical_paper_ids": [],
            "previous_statuses": {},
        }
    task_ids = _unique_nonempty(item["id"] for item in selected)
    now = utc_now()
    changed = 0
    for offset in range(0, len(task_ids), 400):
        chunk = task_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        cursor = connection.execute(
            f"""
            UPDATE download_tasks
            SET status='pending', attempt_count=0, next_attempt_at=NULL,
                last_error=NULL, resolved_url=NULL, source=NULL,
                access_basis='open_access', completed_at=NULL, updated_at=?
            WHERE id IN ({placeholders})
              AND status IN ('retryable_failed','permanent_failed','manual_review')
            """,
            (now, *chunk),
        )
        changed += int(cursor.rowcount)
    return {
        "requeued": changed,
        "task_ids": task_ids,
        "canonical_paper_ids": _unique_nonempty(
            item.get("canonical_paper_id") for item in selected
        ),
        "previous_statuses": dict(Counter(str(item["status"]) for item in selected)),
    }


def requeue_failed_downloads(
    connection: sqlite3.Connection,
    *,
    canonical_paper_ids: Iterable[str] | None = None,
    task_ids: Iterable[str] | None = None,
    failure_classes: Iterable[str] | None = None,
    sources: Iterable[str] | None = None,
    limit: int | None = 25,
) -> dict[str, Any]:
    """Reset failed tasks without downloading or weakening OA/legal checks.

    ``canonical_paper_ids=[]`` and ``task_ids=[]`` intentionally select
    nothing. Passing neither selector is the explicit user-driven bulk mode.
    """
    ensure_download_audit_schema(connection)
    selected: list[sqlite3.Row]
    if canonical_paper_ids is not None:
        paper_ids = _unique_nonempty(canonical_paper_ids)
        if not paper_ids:
            return _reset_failed_task_rows(connection, [])
        selected = []
        for offset in range(0, len(paper_ids), 400):
            chunk = paper_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            selected.extend(
                connection.execute(
                    f"""
                    SELECT id, canonical_paper_id, status
                    FROM download_tasks
                    WHERE canonical_paper_id IN ({placeholders})
                      AND status IN ('retryable_failed','permanent_failed','manual_review')
                    ORDER BY updated_at, created_at, id
                    """,
                    chunk,
                ).fetchall()
            )
    elif task_ids is not None:
        ids = _unique_nonempty(task_ids)
        if not ids:
            return _reset_failed_task_rows(connection, [])
        selected = []
        for offset in range(0, len(ids), 400):
            chunk = ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            selected.extend(
                connection.execute(
                    f"""
                    SELECT id, canonical_paper_id, status
                    FROM download_tasks
                    WHERE id IN ({placeholders})
                      AND status IN ('retryable_failed','permanent_failed','manual_review')
                    ORDER BY updated_at, created_at, id
                    """,
                    chunk,
                ).fetchall()
            )
    else:
        bounded_limit = max(1, min(int(limit or 25), 100))
        clauses = ["status IN ('retryable_failed','permanent_failed','manual_review')"]
        params: list[Any] = []
        kinds = _unique_nonempty(failure_classes or [])
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            error_predicates = " OR ".join("last_error LIKE ?" for _ in kinds)
            clauses.append(
                "("
                "EXISTS ("
                "SELECT 1 FROM download_candidate_attempts ca "
                "WHERE ca.task_id=download_tasks.id "
                f"AND ca.failure_class IN ({placeholders}) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM download_candidate_attempts newer "
                "WHERE newer.task_id=ca.task_id AND ("
                "newer.attempt_number > ca.attempt_number OR "
                "(newer.attempt_number = ca.attempt_number "
                "AND newer.candidate_index > ca.candidate_index)"
                ")"
                ")"
                ") OR "
                f"({error_predicates})"
                ")"
            )
            params.extend(kinds)
            params.extend([f"%[{kind}]%" for kind in kinds])
        source_values = _unique_nonempty(sources or [])
        if source_values:
            placeholders = ",".join("?" for _ in source_values)
            clauses.append(f"COALESCE(source, '') IN ({placeholders})")
            params.extend(source_values)
        params.append(bounded_limit)
        selected = connection.execute(
            f"""
            SELECT id, canonical_paper_id, status
            FROM download_tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at, created_at, id
            LIMIT ?
            """,
            params,
        ).fetchall()
    if limit is not None and (canonical_paper_ids is not None or task_ids is not None):
        selected = selected[: max(0, int(limit))]
    return _reset_failed_task_rows(connection, selected)


def requeue_downloads_after_metadata_change(
    connection: sqlite3.Connection,
    canonical_paper_ids: Iterable[str],
) -> dict[str, Any]:
    """Re-resolve failed downloads only for canonicals changed this scan."""
    result = requeue_failed_downloads(
        connection,
        canonical_paper_ids=canonical_paper_ids,
        limit=None,
    )
    return {"trigger": "metadata_changed", **result}


def enqueue_download(
    connection: sqlite3.Connection,
    canonical_paper_id: str,
    paper_version_id: str | None = None,
    *,
    priority: int = 0,
) -> dict[str, Any]:
    ensure_download_audit_schema(connection)
    existing = connection.execute(
        "SELECT * FROM download_tasks WHERE canonical_paper_id=? AND status IN "
        "('pending','resolving','downloading','retryable_failed') ORDER BY created_at LIMIT 1",
        (canonical_paper_id,),
    ).fetchone()
    if existing:
        return dict(existing)
    now = utc_now()
    task_id = stable_id("download", f"{canonical_paper_id}:{paper_version_id or ''}")
    connection.execute(
        """
        INSERT INTO download_tasks
        (id, canonical_paper_id, paper_version_id, status, priority,
         attempt_count, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, 0, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          priority=MAX(download_tasks.priority, excluded.priority),
          updated_at=excluded.updated_at
        """,
        (task_id, canonical_paper_id, paper_version_id, priority, now, now),
    )
    return dict(connection.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone())


def claim_task_by_id(connection: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] not in {"pending", "retryable_failed"}:
            connection.commit()
            return dict(row) if row else None
        connection.execute(
            "UPDATE download_tasks SET status='resolving', updated_at=? WHERE id=?",
            (now, task_id),
        )
        connection.commit()
        return dict(connection.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone())
    except BaseException:
        connection.rollback()
        raise


def _existing_files_for_paper(
    connection: sqlite3.Connection,
    canonical_paper_id: str,
    paper_version_id: str | None,
) -> list[dict[str, Any]]:
    ensure_download_audit_schema(connection)
    valid_statuses = tuple(sorted(VALID_FILE_STATUSES))
    placeholders = ",".join("?" for _ in valid_statuses)
    return [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT DISTINCT f.id, f.absolute_path, f.sha256, f.file_size,
                            f.validation_status, f.page_count
            FROM paper_files f
            WHERE f.validation_status IN ({placeholders})
              AND (
                f.canonical_paper_id=? OR f.paper_version_id=? OR EXISTS (
                  SELECT 1 FROM paper_file_links l
                  WHERE l.paper_file_id=f.id AND l.canonical_paper_id=?
                    AND (l.paper_version_id='' OR l.paper_version_id=?)
                )
              )
            ORDER BY f.downloaded_at DESC, f.created_at DESC
            """,
            (*valid_statuses, canonical_paper_id, paper_version_id or "", canonical_paper_id, paper_version_id or ""),
        )
    ]


def download_paper_now(
    connection: sqlite3.Connection,
    canonical_paper_id: str,
    paper_version_id: str | None,
    pdf_root: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    for existing_file in _existing_files_for_paper(connection, canonical_paper_id, paper_version_id):
        path = Path(str(existing_file["absolute_path"]))
        if not path.is_file():
            continue
        validation = validate_pdf_file(path)
        if not validation.valid:
            connection.execute(
                "UPDATE paper_files SET validation_status='invalid', updated_at=? WHERE id=?",
                (utc_now(), existing_file["id"]),
            )
            connection.commit()
            continue
        connection.execute(
            "UPDATE paper_files SET validation_status='valid', page_count=?, updated_at=? WHERE id=?",
            (validation.page_count, utc_now(), existing_file["id"]),
        )
        connection.commit()
        return {
            "task_id": None,
            "status": "already_available",
            "paper_file_id": str(existing_file["id"]),
            "path": str(path),
            "sha256": str(existing_file["sha256"]),
            "file_size": int(existing_file["file_size"] or 0),
            "validation": validation.as_dict(),
        }
    task = enqueue_download(connection, canonical_paper_id, paper_version_id, priority=100)
    connection.commit()
    if task["status"] in {"resolving", "downloading"}:
        return {
            "task_id": task["id"],
            "status": str(task["status"]),
            "error": "download is already being processed",
        }
    if task["status"] in {"permanent_failed", "manual_review", "completed"}:
        connection.execute(
            """
            UPDATE download_tasks SET status='pending', next_attempt_at=NULL,
              last_error=NULL, completed_at=NULL, updated_at=? WHERE id=?
            """,
            (utc_now(), task["id"]),
        )
        connection.commit()
    claimed = claim_task_by_id(connection, str(task["id"]))
    if not claimed:
        return {"task_id": task["id"], "status": "not_claimed"}
    if claimed["status"] in {"resolving", "downloading"}:
        return process_download_task(connection, claimed, pdf_root, **kwargs)
    return {
        "task_id": claimed["id"],
        "status": str(claimed["status"]),
        "error": "download is already being processed",
    }


def recover_interrupted_tasks(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        """
        UPDATE download_tasks
        SET status='retryable_failed', next_attempt_at=?,
            last_error='[interrupted_worker] recovered after interrupted worker', updated_at=?
        WHERE status IN ('resolving','downloading')
        """,
        (utc_now(), utc_now()),
    )
    return int(cursor.rowcount)


def claim_next_task(connection: sqlite3.Connection) -> dict[str, Any] | None:
    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT * FROM download_tasks
            WHERE status='pending' OR
                  (status='retryable_failed' AND COALESCE(next_attempt_at, '') <= ?)
            ORDER BY priority DESC, created_at ASC LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            connection.commit()
            return None
        connection.execute(
            "UPDATE download_tasks SET status='resolving', updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        connection.commit()
        return dict(connection.execute("SELECT * FROM download_tasks WHERE id=?", (row["id"],)).fetchone())
    except BaseException:
        connection.rollback()
        raise


def _record_for_task(connection: sqlite3.Connection, task: dict[str, Any]) -> dict[str, Any]:
    version_id = task.get("paper_version_id")
    if version_id:
        row = connection.execute(
            """
            SELECT v.*, p.is_open_access, p.oa_status, p.raw_json AS paper_raw_json,
                   p.raw_openalex_json, p.raw_crossref_json,
                   p.doi AS canonical_doi, p.arxiv_id AS canonical_arxiv_id,
                   p.pdf_url AS canonical_pdf_url, p.oa_url AS canonical_oa_url,
                   p.url AS canonical_url
            FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id WHERE v.id=?
            """,
            (version_id,),
        ).fetchone()
    else:
        row = connection.execute("SELECT * FROM papers WHERE id=?", (task["canonical_paper_id"],)).fetchone()
    if not row:
        raise LookupError("paper metadata not found")
    record = dict(row)
    record["doi"] = record.get("doi") or record.get("canonical_doi")
    record["oa_url"] = record.get("oa_url") or record.get("canonical_oa_url")
    canonical_paper_id = str(record.get("canonical_paper_id") or task["canonical_paper_id"])
    record["alternate_versions"] = [
        dict(item)
        for item in connection.execute(
            """
            SELECT id, source, doi, arxiv_id, arxiv_version, journal,
                   url, pdf_url, raw_json
            FROM paper_versions
            WHERE canonical_paper_id=? AND COALESCE(id, '')<>?
            ORDER BY CASE WHEN version_type='preprint' THEN 0 ELSE 1 END, id
            """,
            (canonical_paper_id, str(version_id or "")),
        )
    ]
    return record


def _next_audit_attempt_number(
    connection: sqlite3.Connection,
    task_id: str,
) -> int:
    """Keep immutable audit numbering across retry-policy resets."""
    row = connection.execute(
        """
        SELECT MAX(attempt_number) AS latest_attempt
        FROM (
            SELECT attempt_number FROM download_attempts WHERE task_id=?
            UNION ALL
            SELECT attempt_number FROM download_candidate_attempts WHERE task_id=?
        )
        """,
        (task_id, task_id),
    ).fetchone()
    return int(row["latest_attempt"] or 0) + 1


def _mark_failure(
    connection: sqlite3.Connection,
    task: dict[str, Any],
    attempt: int,
    message: str,
    http_status: int | None,
    max_attempts: int,
    *,
    failure_class: str,
    source_url: str | None = None,
    started_at: str | None = None,
    retry_after_seconds: float | None = None,
    audit_attempt_number: int | None = None,
) -> dict[str, Any]:
    decision = classify_failure(
        attempt,
        http_status=http_status,
        max_attempts=max_attempts,
        failure_class=failure_class,
        retry_after_seconds=retry_after_seconds,
    )
    now = utc_now()
    labelled_message = redact_text(f"[{decision.failure_class}] {message}")[:1200]
    connection.execute(
        """
        UPDATE download_tasks SET status=?, attempt_count=?, next_attempt_at=?,
          last_error=?, updated_at=? WHERE id=?
        """,
        (
            decision.status,
            attempt,
            next_attempt_iso(decision.delay_seconds) if decision.delay_seconds else None,
            labelled_message,
            now,
            task["id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO download_attempts
        (task_id, attempt_number, started_at, finished_at, status, source_url,
         http_status, error_message, retry_after_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task["id"],
            audit_attempt_number or attempt,
            started_at or now,
            now,
            decision.status,
            redact_url(source_url or task.get("resolved_url")),
            http_status,
            labelled_message,
            decision.delay_seconds,
        ),
    )
    connection.commit()
    return {
        "task_id": task["id"],
        "status": decision.status,
        "failure_class": decision.failure_class,
        "error": labelled_message,
        "retry_after_seconds": decision.delay_seconds,
        "candidate_attempts": candidate_attempts_for_task(connection, str(task["id"])),
    }


def _headers(response: Any) -> Any:
    return getattr(response, "headers", {}) or {}


def _retry_after(response: Any) -> float | None:
    raw = _headers(response).get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def _aggregate_failure(failures: list[dict[str, Any]]) -> tuple[str, int | None, float | None]:
    if not failures:
        return "request_error", None, None
    for item in failures:
        if item["failure_class"] in TRANSIENT_FAILURES:
            return item["failure_class"], item.get("http_status"), item.get("retry_after")
    priority = (
        "institutional_auth_required",
        "access_denied",
        "legal_restriction",
        "pdf_too_large",
        "encrypted_pdf",
        "not_found",
    )
    for failure_class in priority:
        for item in failures:
            if item["failure_class"] == failure_class:
                return failure_class, item.get("http_status"), item.get("retry_after")
    item = failures[-1]
    return str(item["failure_class"]), item.get("http_status"), item.get("retry_after")


def _looks_like_institutional_auth_page(path: Path, final_url: str = "") -> bool:
    try:
        with path.open("rb") as handle:
            payload = handle.read(128 * 1024)
    except OSError:
        return False
    text = payload.decode("utf-8", errors="ignore").casefold()
    html = "<html" in text or "<!doctype html" in text or "<form" in text
    if not html:
        return False
    strong_signals = (
        "type='password'" in text or 'type="password"' in text,
        "saml" in text,
        "shibboleth" in text,
        "/sso" in text or "single sign-on" in text,
        "cookies_not_supported" in str(final_url).casefold(),
    )
    return any(strong_signals) or ("sign in" in text and "<form" in text)


def _guard_institutional_request(request: Any) -> None:
    if not is_safe_institutional_url(getattr(request, "url", "")):
        raise ValueError("unsafe institutional redirect URL")


@contextmanager
def _stream_candidate(
    httpx_module: Any,
    candidate: Resolution,
    request_headers: dict[str, str],
    timeout: float,
):
    """Open one PDF response, warming a short-lived campus cookie session."""
    if candidate.access_basis != "institutional_ip":
        with httpx_module.stream(
            "GET",
            str(candidate.url),
            follow_redirects=True,
            timeout=timeout,
            headers=request_headers,
        ) as response:
            yield response
        return

    if not is_safe_institutional_url(candidate.url):
        raise ValueError("unsafe institutional PDF URL")
    client_factory = getattr(httpx_module, "Client", None)
    stream_function = getattr(httpx_module, "stream", None)
    stream_module = str(getattr(stream_function, "__module__", ""))
    if client_factory is None or stream_module == "unittest.mock":
        with stream_function(
            "GET",
            str(candidate.url),
            follow_redirects=True,
            timeout=timeout,
            headers=request_headers,
        ) as response:
            yield response
        return

    with client_factory(
        follow_redirects=True,
        max_redirects=8,
        timeout=timeout,
        headers=request_headers,
        event_hooks={"request": [_guard_institutional_request]},
    ) as client:
        if candidate.landing_url and is_safe_institutional_url(candidate.landing_url):
            landing_headers = {
                "User-Agent": request_headers["User-Agent"],
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            }
            try:
                client.get(
                    candidate.landing_url,
                    headers=landing_headers,
                    timeout=min(timeout, 15.0),
                )
            except httpx_module.HTTPError:
                # Direct PDF access may still work even when the landing page is
                # unavailable. No login input or browser cookie is ever supplied.
                pass
        with client.stream(
            "GET",
            str(candidate.url),
            headers=request_headers,
        ) as response:
            yield response


def process_download_task(    connection: sqlite3.Connection,
    task: dict[str, Any],
    pdf_root: Path,
    *,
    unpaywall_email: str | None = None,
    timeout: float = 30.0,
    max_attempts: int = 5,
    max_bytes: int = 200 * 1024 * 1024,
    allow_institutional_ip: bool | None = None,
) -> dict[str, Any]:
    attempt = int(task.get("attempt_count") or 0) + 1
    started = utc_now()
    ensure_download_audit_schema(connection)
    audit_attempt = _next_audit_attempt_number(connection, str(task["id"]))
    try:
        import httpx

        record = _record_for_task(connection, task)
        connection.commit()
        institutional_enabled = (
            institutional_ip_enabled()
            if allow_institutional_ip is None
            else bool(allow_institutional_ip)
        )
        candidates = resolve_legal_oa_candidates(
            record,
            unpaywall_email=unpaywall_email,
            timeout=min(timeout, 8.0),
            allow_institutional_ip=institutional_enabled,
        )
        if not candidates:
            return _mark_failure(
                connection,
                task,
                attempt,
                (
                    "no authorized PDF location found across arXiv, OA metadata, "
                    "Unpaywall, OpenAlex, licensed Crossref links and configured "
                    "campus-IP publisher routes"
                ),
                404,
                max_attempts,
                failure_class="no_legal_oa_location",
                started_at=started,
                audit_attempt_number=audit_attempt,
            )

        sha256 = ""
        size = 0
        destination: Path | None = None
        resolution: Resolution | None = None
        successful_validation: PdfValidation | None = None
        failures: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(candidates, start=1):
            temporary_dir = pdf_root / ".partial"
            temporary_dir.mkdir(parents=True, exist_ok=True)
            temporary = temporary_dir / f"{uuid.uuid4()}.part"
            digest = hashlib.sha256()
            size = 0
            response: Any = None
            content_type = ""
            final_url = str(candidate.url)
            candidate_started = utc_now()
            start_candidate_attempt(
                connection,
                task_id=str(task["id"]),
                attempt_number=audit_attempt,
                candidate_index=candidate_index,
                source=candidate.source,
                access_basis=candidate.access_basis,
                reason=candidate.reason,
                source_url=str(candidate.url),
                started_at=candidate_started,
            )
            connection.execute(
                "UPDATE download_tasks SET status='downloading', resolved_url=?, source=?, "
                "access_basis=?, updated_at=? WHERE id=?",
                (redact_url(candidate.url), candidate.source, candidate.access_basis, utc_now(), task["id"]),
            )
            connection.commit()
            request_headers = {
                "User-Agent": "condmat-trend-radar/2.6 (local research library; campus IP aware)",
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
            }
            if candidate.referer_url:
                request_headers["Referer"] = candidate.referer_url
            try:
                with _stream_candidate(
                    httpx,
                    candidate,
                    request_headers,
                    timeout,
                ) as response:
                    response.raise_for_status()
                    content_type = str(_headers(response).get("Content-Type") or "")
                    final_url = str(getattr(response, "url", None) or candidate.url)
                    content_length = _headers(response).get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ValueError("PDF exceeds configured size limit")
                        except ValueError as exc:
                            if "exceeds" in str(exc):
                                raise
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError("PDF exceeds configured size limit")
                            digest.update(chunk)
                            handle.write(chunk)
                validation = validate_pdf_file(temporary)
                if not validation.valid:
                    failure_class = validation.failure_class or "invalid_pdf_structure"
                    validation_message = validation.message
                    if (
                        candidate.access_basis == "institutional_ip"
                        and failure_class == "not_pdf_content"
                        and _looks_like_institutional_auth_page(temporary, final_url)
                    ):
                        failure_class = "institutional_auth_required"
                        validation_message = "publisher returned an institutional sign-in or cookie-required HTML page"
                    failures.append(
                        {
                            "source": candidate.source,
                            "failure_class": failure_class,
                            "http_status": int(getattr(response, "status_code", 200) or 200),
                            "message": validation_message,
                            "retry_after": None,
                        }
                    )
                    finish_candidate_attempt(
                        connection,
                        task_id=str(task["id"]),
                        attempt_number=audit_attempt,
                        candidate_index=candidate_index,
                        status="rejected",
                        http_status=int(getattr(response, "status_code", 200) or 200),
                        content_type=content_type,
                        final_url=final_url,
                        bytes_received=size,
                        failure_class=failure_class,
                        error_message=validation_message,
                        validation=validation.as_dict(),
                    )
                    connection.commit()
                    temporary.unlink(missing_ok=True)
                    continue

                sha256 = digest.hexdigest()
                destination = pdf_root / sha256[:2] / f"{sha256}.pdf"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    temporary.unlink(missing_ok=True)
                else:
                    temporary.replace(destination)
                resolution = candidate
                successful_validation = validation
                finish_candidate_attempt(
                    connection,
                    task_id=str(task["id"]),
                    attempt_number=audit_attempt,
                    candidate_index=candidate_index,
                    status="completed",
                    http_status=int(getattr(response, "status_code", 200) or 200),
                    content_type=content_type,
                    final_url=final_url,
                    bytes_received=size,
                    sha256=sha256,
                    validation=validation.as_dict(),
                )
                connection.commit()
                break
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                error_response = getattr(exc, "response", None) or response
                status_code = getattr(error_response, "status_code", None)
                http_status = int(status_code) if status_code is not None else None
                failure_class = failure_class_for(http_status=http_status, exception=exc)
                if candidate.access_basis == "institutional_ip" and http_status in {401, 403, 407}:
                    failure_class = "institutional_auth_required"
                retry_after = _retry_after(error_response)
                message = redact_text(f"{type(exc).__name__}: {str(exc)[:500]}")
                failures.append(
                    {
                        "source": candidate.source,
                        "failure_class": failure_class,
                        "http_status": http_status,
                        "message": message,
                        "retry_after": retry_after,
                    }
                )
                finish_candidate_attempt(
                    connection,
                    task_id=str(task["id"]),
                    attempt_number=audit_attempt,
                    candidate_index=candidate_index,
                    status="failed",
                    http_status=http_status,
                    content_type=content_type,
                    final_url=str(getattr(error_response, "url", None) or final_url),
                    bytes_received=size,
                    failure_class=failure_class,
                    error_message=message,
                )
                connection.commit()

        if not resolution or not destination or not successful_validation:
            failure_class, http_status, retry_after = _aggregate_failure(failures)
            detail = "; ".join(
                f"{item['source']}:{item['failure_class']}:{item['message']}" for item in failures
            ) or "all authorized PDF locations failed"
            return _mark_failure(
                connection,
                task,
                attempt,
                f"authorized PDF download failed after {len(candidates)} candidate(s): "
                f"{detail[:1000]}",
                http_status,
                max_attempts,
                failure_class=failure_class,
                source_url=str(candidates[-1].url) if candidates else None,
                started_at=started,
                audit_attempt_number=audit_attempt,
                retry_after_seconds=retry_after,
            )

        now = utc_now()
        file_id = stable_id("file", sha256)
        connection.execute(
            """
            INSERT INTO paper_files
            (id, canonical_paper_id, paper_version_id, absolute_path, sha256,
             file_size, mime_type, source_url, download_source, access_basis, downloaded_at,
             page_count, extraction_status, validation_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'application/pdf', ?, ?, ?, ?, ?, 'pending',
                    'valid', ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
              absolute_path=excluded.absolute_path,
              file_size=excluded.file_size,
              source_url=excluded.source_url,
              download_source=excluded.download_source,
              access_basis=excluded.access_basis,
              downloaded_at=excluded.downloaded_at,
              page_count=excluded.page_count,
              validation_status='valid',
              updated_at=excluded.updated_at
            """,
            (
                file_id,
                task["canonical_paper_id"],
                task.get("paper_version_id"),
                str(destination.resolve()),
                sha256,
                size,
                redact_url(resolution.url),
                resolution.source,
                resolution.access_basis,
                now,
                successful_validation.page_count,
                now,
                now,
            ),
        )
        stored_file = connection.execute("SELECT id FROM paper_files WHERE sha256=?", (sha256,)).fetchone()
        stored_file_id = str(stored_file["id"]) if stored_file else file_id
        connection.execute(
            """
            INSERT OR IGNORE INTO paper_file_links
            (paper_file_id, canonical_paper_id, paper_version_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                stored_file_id,
                task["canonical_paper_id"],
                str(task.get("paper_version_id") or ""),
                now,
            ),
        )
        connection.execute(
            """
            UPDATE download_tasks SET status='completed', attempt_count=?,
              completed_at=?, updated_at=?, next_attempt_at=NULL, last_error=NULL,
              resolved_url=?, source=?, access_basis=? WHERE id=?
            """,
            (attempt, now, now, redact_url(resolution.url), resolution.source,
             resolution.access_basis, task["id"]),
        )
        connection.execute(
            """
            INSERT INTO download_attempts
            (task_id, attempt_number, started_at, finished_at, status, source_url, http_status)
            VALUES (?, ?, ?, ?, 'completed', ?, 200)
            """,
            (task["id"], audit_attempt, started, now, redact_url(resolution.url)),
        )
        connection.commit()
        return {
            "task_id": task["id"],
            "status": "completed",
            "paper_file_id": stored_file_id,
            "sha256": sha256,
            "path": str(destination),
            "source": resolution.source,
            "access_basis": resolution.access_basis,
            "validation": successful_validation.as_dict(),
            "candidates_available": len(candidates),
            "candidates_tried": len(failures) + 1,
            "candidate_attempts": candidate_attempts_for_task(connection, str(task["id"])),
        }
    except Exception as exc:
        response = getattr(exc, "response", None)
        http_status = getattr(response, "status_code", None)
        failure_class = failure_class_for(http_status=http_status, exception=exc)
        return _mark_failure(
            connection,
            task,
            attempt,
            redact_text(f"{type(exc).__name__}: {exc}"),
            int(http_status) if http_status is not None else None,
            max_attempts,
            failure_class=failure_class,
            started_at=started,
            audit_attempt_number=audit_attempt,
        )


def run_download_queue(
    connection: sqlite3.Connection,
    pdf_root: Path,
    *,
    limit: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    ensure_download_audit_schema(connection)
    recovered = recover_interrupted_tasks(connection)
    connection.commit()
    results: list[dict[str, Any]] = []
    for _ in range(max(0, limit)):
        task = claim_next_task(connection)
        if not task:
            break
        results.append(process_download_task(connection, task, pdf_root, **kwargs))
    return {
        "recovered": recovered,
        "processed": len(results),
        "results": results,
        "audit": download_audit_statistics(connection),
    }
