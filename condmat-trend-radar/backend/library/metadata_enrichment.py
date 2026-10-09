from __future__ import annotations

import json
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from backend.config import config_value
from backend.ingest.arxiv_client import ARXIV_ENDPOINT, parse_arxiv_feed
from backend.ingest.crossref_client import (
    TRANSIENT_HTTP_STATUSES,
    CrossrefClient,
    retry_after_seconds,
)
from backend.ingest.openalex_client import OpenAlexClient
from backend.library.local_search import refresh_search_entries
from backend.library.material_discovery import sync_materials_from_papers
from backend.library.repository import utc_now


BACKOFF_TABLE = "metadata_enrichment_backoff"
BACKOFF_INDEX = "idx_metadata_enrichment_backoff_due"
PAPER_RETRY_DELAYS = (30 * 60, 2 * 3600, 8 * 3600, 24 * 3600, 3 * 86400, 7 * 86400)
DEFAULT_PROVIDER_RETRY_SECONDS = 3600
SEMANTIC_SCHOLAR_MAX_RETRIES = 2
SEMANTIC_SCHOLAR_RETRY_BASE_SECONDS = 0.5
SEMANTIC_SCHOLAR_RETRY_CAP_SECONDS = 5.0
SEMANTIC_SCHOLAR_USER_AGENT = "condmat-trend-radar/2.5 (Semantic Scholar metadata enrichment)"
DEFAULT_ITEM_TIMEOUT_SECONDS = 20.0
DEFAULT_BATCH_TIMEOUT_SECONDS = 180.0


class ProviderRateLimitError(RuntimeError):
    def __init__(self, provider: str, retry_after_seconds: int) -> None:
        self.provider = provider
        self.retry_after_seconds = max(300, int(retry_after_seconds))
        super().__init__(f"{provider} rate limited; retry after {self.retry_after_seconds}s")


def ensure_metadata_enrichment_schema(connection: sqlite3.Connection) -> None:
    objects = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN (?, ?)",
            (BACKOFF_TABLE, BACKOFF_INDEX),
        )
    }
    if BACKOFF_TABLE in objects and BACKOFF_INDEX in objects:
        return
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BACKOFF_TABLE} (
            state_key TEXT PRIMARY KEY,
            state_type TEXT NOT NULL CHECK(state_type IN ('paper','provider')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL,
            next_attempt_at TEXT NOT NULL,
            status TEXT NOT NULL,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS {BACKOFF_INDEX} "
        f"ON {BACKOFF_TABLE}(state_type, next_attempt_at, last_attempt_at)"
    )


def _paper_state_key(paper_id: str) -> str:
    return f"paper:{paper_id}"


def _provider_state_key(provider: str) -> str:
    return f"provider:{provider}"


def _retry_at(delay_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds))).isoformat(timespec="seconds")


def _backoff_row(connection: sqlite3.Connection, state_key: str) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT * FROM {BACKOFF_TABLE} WHERE state_key=?",
        (state_key,),
    ).fetchone()


def _record_backoff(
    connection: sqlite3.Connection,
    *,
    state_key: str,
    state_type: str,
    delay_seconds: int,
    error: str,
) -> dict[str, Any]:
    current = _backoff_row(connection, state_key)
    attempt = int(current["attempt_count"] or 0) + 1 if current else 1
    now = utc_now()
    next_at = _retry_at(delay_seconds)
    connection.execute(
        f"""
        INSERT INTO {BACKOFF_TABLE}
        (state_key, state_type, attempt_count, last_attempt_at, next_attempt_at,
         status, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, 'cooldown', ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET
          state_type=excluded.state_type,
          attempt_count=excluded.attempt_count,
          last_attempt_at=excluded.last_attempt_at,
          next_attempt_at=excluded.next_attempt_at,
          status=excluded.status,
          last_error=excluded.last_error,
          updated_at=excluded.updated_at
        """,
        (state_key, state_type, attempt, now, next_at, error[:500], now),
    )
    return {"attempt_count": attempt, "next_attempt_at": next_at}


def _record_paper_backoff(connection: sqlite3.Connection, paper_id: str, error: str) -> dict[str, Any]:
    current = _backoff_row(connection, _paper_state_key(paper_id))
    attempt = int(current["attempt_count"] or 0) + 1 if current else 1
    delay = PAPER_RETRY_DELAYS[min(attempt - 1, len(PAPER_RETRY_DELAYS) - 1)]
    return _record_backoff(
        connection,
        state_key=_paper_state_key(paper_id),
        state_type="paper",
        delay_seconds=delay,
        error=error or "abstract unavailable from configured metadata providers",
    )


def _record_provider_backoff(
    connection: sqlite3.Connection,
    provider: str,
    retry_after_seconds: int,
    error: str,
) -> dict[str, Any]:
    return _record_backoff(
        connection,
        state_key=_provider_state_key(provider),
        state_type="provider",
        delay_seconds=max(DEFAULT_PROVIDER_RETRY_SECONDS, retry_after_seconds),
        error=error,
    )


def _clear_backoff(connection: sqlite3.Connection, state_key: str) -> None:
    connection.execute(f"DELETE FROM {BACKOFF_TABLE} WHERE state_key=?", (state_key,))


def _provider_available(connection: sqlite3.Connection, provider: str) -> bool:
    row = _backoff_row(connection, _provider_state_key(provider))
    return not row or str(row["next_attempt_at"] or "") <= utc_now()


def _retry_after_seconds(exc: urllib.error.HTTPError) -> int:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value and value.strip().isdigit():
        return max(300, int(value.strip()))
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(300, int((parsed - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    return DEFAULT_PROVIDER_RETRY_SECONDS


def _semantic_scholar_retry_delay(attempt: int, retry_after: float | None = None) -> float | None:
    """Return a bounded delay, or ``None`` when the server asks us to wait longer."""

    exponential = min(
        SEMANTIC_SCHOLAR_RETRY_CAP_SECONDS,
        SEMANTIC_SCHOLAR_RETRY_BASE_SECONDS * (2**attempt),
    )
    if retry_after is None:
        return exponential
    if retry_after > SEMANTIC_SCHOLAR_RETRY_CAP_SECONDS:
        return None
    return max(exponential, retry_after)


def _semantic_scholar_transient_status(status: int) -> bool:
    return status in TRANSIENT_HTTP_STATUSES or 500 <= status <= 599


def _best_local_abstract(connection: sqlite3.Connection, paper_id: str) -> str:
    row = connection.execute(
        """
        SELECT abstract FROM (
          SELECT COALESCE(abstract,'') AS abstract FROM papers WHERE id=?
          UNION ALL
          SELECT COALESCE(abstract,'') FROM paper_versions WHERE canonical_paper_id=?
        ) WHERE length(trim(abstract))>0 ORDER BY length(abstract) DESC LIMIT 1
        """,
        (paper_id, paper_id),
    ).fetchone()
    return str(row[0]) if row else ""


def _fetch_arxiv(arxiv_id: str, timeout: int) -> dict[str, Any] | None:
    clean = arxiv_id.split("v", 1)[0] if arxiv_id else ""
    if not clean:
        return None
    url = f"{ARXIV_ENDPOINT}?{urllib.parse.urlencode({'id_list': clean, 'max_results': '1'})}"
    request = urllib.request.Request(url, headers={"User-Agent": "condmat-trend-radar/0.3", "Accept": "application/atom+xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        items = parse_arxiv_feed(response.read())
    return items[0] if items else None


def _fetch_openalex(doi: str, openalex_id: str, timeout: int) -> dict[str, Any] | None:
    client = OpenAlexClient(timeout=timeout, polite_delay=0, max_retries=0, warn_if_missing_key=False)
    select = "id,doi,title,abstract_inverted_index,publication_date,primary_location,locations,open_access,authorships,concepts,cited_by_count,type"
    if doi:
        payload = client._get_json("/works", {"filter": f"doi:{doi}", "per-page": "1", "select": select})
        items = payload.get("results") or []
        return client._normalize_work(items[0], "") if items else None
    short = (openalex_id or "").rsplit("/", 1)[-1]
    if short:
        payload = client._get_json(f"/works/{short}", {"select": select})
        return client._normalize_work(payload, "") if payload else None
    return None


def _remaining_timeout(timeout: float, deadline: float | None) -> float:
    """Return a positive socket timeout without allowing work past a deadline."""

    if deadline is None:
        return max(0.1, float(timeout))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("metadata enrichment item time budget exhausted")
    return max(0.1, min(float(timeout), remaining))


def _fetch_semantic_scholar(
    doi: str,
    arxiv_id: str,
    timeout: float,
    *,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    identifier = f"DOI:{doi}" if doi else f"ARXIV:{arxiv_id.split('v', 1)[0]}" if arxiv_id else ""
    if not identifier:
        return None
    encoded = urllib.parse.quote(identifier, safe="")
    params = urllib.parse.urlencode({"fields": "title,abstract,url,openAccessPdf,externalIds"})
    request = urllib.request.Request(
        f"https://api.semanticscholar.org/graph/v1/paper/{encoded}?{params}",
        headers={"User-Agent": SEMANTIC_SCHOLAR_USER_AGENT, "Accept": "application/json"},
    )
    api_key = config_value("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")
    if api_key:
        request.add_header("x-api-key", api_key)
    for attempt in range(SEMANTIC_SCHOLAR_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request,
                timeout=_remaining_timeout(timeout, deadline),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            status = int(exc.code or 0)
            if not _semantic_scholar_transient_status(status):
                raise
            retry_after = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            delay = _semantic_scholar_retry_delay(attempt, retry_after)
            if attempt >= SEMANTIC_SCHOLAR_MAX_RETRIES or delay is None:
                if status == 429:
                    raise ProviderRateLimitError("semantic_scholar", _retry_after_seconds(exc)) from exc
                raise
            if delay > 0:
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise TimeoutError("metadata enrichment item time budget exhausted") from exc
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError):
            if attempt >= SEMANTIC_SCHOLAR_MAX_RETRIES:
                raise
            delay = _semantic_scholar_retry_delay(attempt)
            if delay and delay > 0:
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise TimeoutError("metadata enrichment item time budget exhausted")
                time.sleep(delay)
    abstract = str(payload.get("abstract") or "").strip()
    oa_pdf = payload.get("openAccessPdf") or {}
    pdf_url = str(oa_pdf.get("url") or "") if isinstance(oa_pdf, dict) else ""
    return {
        "title": str(payload.get("title") or ""),
        "abstract": abstract,
        "url": str(payload.get("url") or ""),
        "pdf_url": pdf_url,
        "is_open_access": bool(pdf_url),
    }


def enrich_paper_metadata(
    connection: sqlite3.Connection,
    paper_id: str,
    *,
    timeout: float = 15,
    item_timeout_seconds: float | None = DEFAULT_ITEM_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    ensure_metadata_enrichment_schema(connection)
    paper = connection.execute(
        "SELECT id, doi, arxiv_id, openalex_id, title, abstract FROM papers WHERE id=?",
        (paper_id,),
    ).fetchone()
    if not paper:
        raise LookupError("paper not found")
    versions = connection.execute(
        "SELECT id, doi, arxiv_id, title, abstract FROM paper_versions WHERE canonical_paper_id=? ORDER BY publication_date DESC",
        (paper_id,),
    ).fetchall()
    doi = str(paper["doi"] or next((row["doi"] for row in versions if row["doi"]), "") or "")
    arxiv_id = str(paper["arxiv_id"] or next((row["arxiv_id"] for row in versions if row["arxiv_id"]), "") or "")
    abstract = _best_local_abstract(connection, paper_id)
    source = "local"
    fetched: dict[str, Any] | None = None
    errors: list[str] = []
    deadline = (
        time.monotonic() + max(0.1, float(item_timeout_seconds))
        if item_timeout_seconds is not None
        else None
    )
    if not abstract:
        for name, loader in (
            ("arxiv", lambda provider_timeout: _fetch_arxiv(arxiv_id, provider_timeout) if arxiv_id else None),
            (
                "crossref",
                lambda provider_timeout: CrossrefClient(
                    timeout=provider_timeout, polite_delay=0, max_retries=0
                ).get_work(doi) if doi else None,
            ),
            (
                "openalex",
                lambda provider_timeout: _fetch_openalex(
                    doi, str(paper["openalex_id"] or ""), provider_timeout
                ),
            ),
            (
                "semantic_scholar",
                lambda provider_timeout: _fetch_semantic_scholar(
                    doi, arxiv_id, provider_timeout, deadline=deadline
                ),
            ),
        ):
            if name == "semantic_scholar" and not _provider_available(connection, name):
                errors.append("semantic_scholar: deferred by persistent provider cooldown")
                continue
            try:
                provider_timeout = _remaining_timeout(timeout, deadline)
                candidate = loader(provider_timeout)
                if name == "semantic_scholar":
                    _clear_backoff(connection, _provider_state_key(name))
                if candidate and str(candidate.get("abstract") or "").strip():
                    fetched = candidate
                    abstract = str(candidate["abstract"]).strip()
                    source = name
                    break
            except ProviderRateLimitError as exc:
                _record_provider_backoff(
                    connection,
                    exc.provider,
                    exc.retry_after_seconds,
                    str(exc),
                )
                errors.append(f"{name}: {exc}")
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
                if deadline is not None and time.monotonic() >= deadline:
                    break
    updated = False
    if abstract:
        now = utc_now()
        connection.execute(
            """
            UPDATE papers SET abstract=COALESCE(NULLIF(abstract,''), ?),
              pdf_url=COALESCE(NULLIF(pdf_url,''), ?),
              url=COALESCE(NULLIF(url,''), ?),
              is_open_access=MAX(COALESCE(is_open_access,0), ?), updated_at=? WHERE id=?
            """,
            (
                abstract,
                str((fetched or {}).get("pdf_url") or ""),
                str((fetched or {}).get("url") or ""),
                int(bool((fetched or {}).get("is_open_access"))),
                now,
                paper_id,
            ),
        )
        connection.execute(
            """
            UPDATE paper_versions SET abstract=COALESCE(NULLIF(abstract,''), ?),
              pdf_url=COALESCE(NULLIF(pdf_url,''), ?), url=COALESCE(NULLIF(url,''), ?),
              last_seen_at=? WHERE canonical_paper_id=?
            """,
            (abstract, str((fetched or {}).get("pdf_url") or ""), str((fetched or {}).get("url") or ""), now, paper_id),
        )
        refresh_search_entries(connection, [str(row["id"]) for row in versions])
        updated = True
    materials = sync_materials_from_papers(connection, paper_ids=[paper_id])
    return {
        "canonical_paper_id": paper_id,
        "abstract_available": bool(abstract),
        "abstract_length": len(abstract),
        "abstract_source": source,
        "updated": updated,
        "materials": materials,
        "errors": errors,
    }


def backfill_missing_abstracts(
    connection: sqlite3.Connection,
    *,
    limit: int = 5,
    timeout: float = 12,
    item_timeout_seconds: float | None = DEFAULT_ITEM_TIMEOUT_SECONDS,
    batch_timeout_seconds: float | None = DEFAULT_BATCH_TIMEOUT_SECONDS,
    progress_callback: Callable[[int, int, int, int, int, str, str], None] | None = None,
) -> dict[str, Any]:
    """Backfill abstracts and publish item-level, durably visible progress.

    Existing callers remain valid. Each item result is committed before the
    optional best-effort callback so dashboard polling never depends on a
    later item or on function return.
    """
    ensure_metadata_enrichment_schema(connection)
    now = utc_now()
    cooldown_skipped = int(connection.execute(
        f"""
        SELECT COUNT(*) FROM papers p
        JOIN {BACKOFF_TABLE} b ON b.state_key='paper:' || p.id AND b.state_type='paper'
        WHERE p.data_mode='real' AND p.condmat_view_eligible=1
          AND length(trim(COALESCE(p.abstract,'')))=0
          AND NOT EXISTS (
            SELECT 1 FROM paper_versions v WHERE v.canonical_paper_id=p.id
              AND length(trim(COALESCE(v.abstract,'')))>0
          )
          AND b.next_attempt_at>?
        """,
        (now,),
    ).fetchone()[0])
    rows = connection.execute(
        f"""
        SELECT p.id FROM papers p
        LEFT JOIN {BACKOFF_TABLE} b
          ON b.state_key='paper:' || p.id AND b.state_type='paper'
        WHERE p.data_mode='real' AND p.condmat_view_eligible=1
          AND length(trim(COALESCE(p.abstract,'')))=0
          AND NOT EXISTS (
            SELECT 1 FROM paper_versions v WHERE v.canonical_paper_id=p.id
              AND length(trim(COALESCE(v.abstract,'')))>0
          )
          AND (b.next_attempt_at IS NULL OR b.next_attempt_at<=?)
        ORDER BY CASE WHEN b.last_attempt_at IS NULL THEN 0 ELSE 1 END,
                 COALESCE(b.last_attempt_at,''),
                 COALESCE(p.publication_date, '') DESC,
                 COALESCE(p.cited_by_count, 0) DESC
        LIMIT ?
        """,
        (now, max(1, min(int(limit), 25))),
    ).fetchall()
    results: list[dict[str, Any]] = []
    processed = enriched = not_found = failed = 0
    total = len(rows)
    progress_callback_errors: list[str] = []
    batch_deadline = (
        time.monotonic() + max(0.1, float(batch_timeout_seconds))
        if batch_timeout_seconds is not None
        else None
    )
    budget_exhausted = False

    def publish_progress(current_paper_id: str, current_source: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                processed,
                total,
                enriched,
                not_found,
                failed,
                current_paper_id,
                current_source,
            )
        except Exception as exc:
            if len(progress_callback_errors) < 10:
                progress_callback_errors.append(
                    f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"
                )

    publish_progress("", "")
    for row in rows:
        if batch_deadline is not None and time.monotonic() >= batch_deadline:
            budget_exhausted = True
            break
        paper_id = str(row["id"])
        # Each enrichment may do several remote requests. Release writes from
        # the preceding record before beginning the next one.
        connection.commit()
        publish_progress(paper_id, "resolver")
        current_source = "resolver"
        try:
            remaining_batch = (
                max(0.1, batch_deadline - time.monotonic())
                if batch_deadline is not None
                else None
            )
            effective_item_timeout = item_timeout_seconds
            if remaining_batch is not None:
                effective_item_timeout = (
                    min(float(item_timeout_seconds), remaining_batch)
                    if item_timeout_seconds is not None
                    else remaining_batch
                )
            item_timeout_kwargs: dict[str, float] = {}
            if (
                effective_item_timeout is not None
                and (
                    item_timeout_seconds is None
                    or effective_item_timeout < float(item_timeout_seconds) - 0.01
                )
            ):
                item_timeout_kwargs["item_timeout_seconds"] = effective_item_timeout
            item = enrich_paper_metadata(
                connection,
                paper_id,
                timeout=timeout,
                **item_timeout_kwargs,
            )
            item_errors = [str(value) for value in (item.get("errors") or []) if value]
            if item.get("abstract_available"):
                _clear_backoff(connection, _paper_state_key(paper_id))
                enriched += 1
                current_source = str(item.get("abstract_source") or "local")
            else:
                error = "; ".join(item_errors)
                item["retry"] = _record_paper_backoff(connection, paper_id, error)
                if item_errors:
                    failed += 1
                    current_source = item_errors[0].split(":", 1)[0] or "resolver"
                else:
                    not_found += 1
                    current_source = "all_sources"
            results.append(item)
        except Exception as exc:
            failed += 1
            message = f"{type(exc).__name__}: {str(exc)[:180]}"
            retry = _record_paper_backoff(connection, paper_id, message)
            results.append({"canonical_paper_id": paper_id, "updated": False, "error": message, "retry": retry})
        # Make the completed item visible before publishing its counters. This
        # also commits the last item, which previously waited for caller exit.
        connection.commit()
        processed += 1
        publish_progress(paper_id, current_source)
    return {
        "requested": total,
        "total": total,
        "processed": processed,
        "enriched": enriched,
        "not_found": not_found,
        "failed": failed,
        "deferred": total - processed,
        "budget_exhausted": budget_exhausted,
        "cooldown_skipped": cooldown_skipped,
        "results": results,
        "progress_callback_errors": progress_callback_errors,
    }
