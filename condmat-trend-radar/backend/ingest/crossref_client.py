from __future__ import annotations

import html
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator

from backend.config import openalex_mailto
from backend.ingest.journal_registry import JournalEntry


CROSSREF_BASE = "https://api.crossref.org"
TAG_RE = re.compile(r"<[^>]+>")
TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
TRANSIENT_NETWORK_ERROR_TYPES = {
    "ConnectionAbortedError",
    "ConnectionError",
    "ConnectionResetError",
    "SSLError",
    "TimeoutError",
    "URLError",
    "timeout",
}


@dataclass(frozen=True)
class CrossrefPage:
    page: int
    cursor_used: str
    next_cursor: str
    fetched: int
    papers: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    duplicate_rate_vs_previous_page: float
    stop_reason: str = ""
    attempts: int = 1
    rows_used: int = 0
    row_fallback_from: int | None = None
    pagination_mode: str = "cursor"

    @property
    def cursor_used_prefix(self) -> str:
        return cursor_prefix(self.cursor_used)

    @property
    def next_cursor_prefix(self) -> str:
        return cursor_prefix(self.next_cursor)


class CrossrefClient:
    def __init__(
        self,
        timeout: int = 20,
        polite_delay: float = 1.0,
        mailto: str | None = None,
        max_retries: int = 2,
        retry_backoff_base: float = 0.75,
        retry_backoff_cap: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self.polite_delay = polite_delay
        self.mailto = mailto or openalex_mailto()
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base = max(0.0, float(retry_backoff_base))
        self.retry_backoff_cap = max(self.retry_backoff_base, float(retry_backoff_cap))

    def get_work(self, doi: str) -> dict[str, Any] | None:
        encoded = urllib.parse.quote(doi)
        url = f"{CROSSREF_BASE}/works/{encoded}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent()})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8")).get("message", {})
        if not payload:
            return None
        return self.normalize_work(payload, fallback_journal="")

    def fetch_journal_works(
        self,
        journal: JournalEntry,
        from_date: str,
        to_date: str,
        target: int = 150,
        rows: int = 100,
        max_pages: int = 30,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        output: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for page in self.iterate_crossref_works(
            journal,
            from_date,
            to_date,
            rows=rows,
            cursor="*",
            max_pages=max_pages,
            sleep_seconds=self.polite_delay,
        ):
            errors.extend(page.errors)
            for paper in page.papers:
                output.append(paper)
                if len(output) >= target:
                    break
            if len(output) >= target or page.stop_reason:
                break
        return output, errors

    def fetch_journal_page(
        self,
        journal: JournalEntry,
        from_date: str,
        to_date: str,
        cursor: str = "*",
        rows: int = 100,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        page = next(
            self.iterate_crossref_works(
                journal,
                from_date,
                to_date,
                rows=rows,
                cursor=cursor,
                max_pages=1,
                sleep_seconds=0,
            ),
            None,
        )
        if not page:
            return [], "", []
        return page.papers, page.next_cursor, page.errors

    def iterate_crossref_works(
        self,
        journal: JournalEntry,
        date_from: str,
        date_to: str,
        rows: int = 100,
        cursor: str = "*",
        max_pages: int = 50,
        sleep_seconds: float = 1.0,
        chunk_deadline: float | None = None,
        page_hook: Callable[[str, int], None] | None = None,
    ) -> Iterator[CrossrefPage]:
        cursor_used = cursor or "*"
        offset_mode = cursor_used.startswith("offset:")
        offset = parse_offset_cursor(cursor_used) if offset_mode else 0
        page_number = 0
        previous_signatures: set[str] = set()
        high_duplicate_pages = 0
        # Crossref accepts up to 1,000 records per cursor page. Keeping the
        # requested size (within the documented bound) prevents a page budget
        # from silently covering only one tenth of its advertised window.
        rows = min(1000, max(1, int(rows or 100)))
        if offset_mode:
            rows = min(rows, 250)
        while True:
            if max_pages and max_pages > 0 and page_number >= max_pages:
                break
            if chunk_deadline is not None and time.monotonic() >= chunk_deadline:
                break
            page_number += 1
            attempts = 0
            attempts_at_size = 0
            request_rows = rows
            row_fallback_from: int | None = None
            while True:
                attempts += 1
                attempts_at_size += 1
                if page_hook is not None:
                    page_hook("before", page_number)
                try:
                    try:
                        papers, next_cursor, errors, fetched = self._fetch_journal_page_raw(
                            journal,
                            date_from,
                            date_to,
                            cursor=cursor_used,
                            rows=request_rows,
                            offset=offset if offset_mode else None,
                        )
                    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
                        papers, next_cursor, fetched = [], "", 0
                        errors = [network_exception_error(journal.canonical_name, exc)]
                finally:
                    # Also run after a failed attempt so live cancellation is
                    # observed before entering retry backoff.
                    if page_hook is not None:
                        page_hook("after", page_number)
                if not errors:
                    rows = request_rows
                    break
                transient = all(is_transient_crossref_error(item) for item in errors)
                if transient and attempts_at_size <= self.max_retries:
                    delay = self._retry_delay(errors, attempts_at_size)
                    # If a provider asks for a long Retry-After, end this
                    # bounded attempt instead of retrying earlier than asked.
                    if delay is None:
                        break
                    if chunk_deadline is not None and time.monotonic() + delay >= chunk_deadline:
                        break
                    if delay > 0:
                        time.sleep(delay)
                    continue
                fallback_rows = crossref_row_fallback(request_rows)
                if (
                    not offset_mode
                    and row_fallback_from is None
                    and fallback_rows is not None
                    and is_crossref_cursor_npe(errors)
                ):
                    # Crossref has intermittently returned a server-side
                    # NullPointerException for a valid deep cursor at rows=1000.
                    # Retry that exact cursor with one bounded smaller page size.
                    row_fallback_from = request_rows
                    request_rows = fallback_rows
                    attempts_at_size = 0
                    continue
                if not offset_mode and is_crossref_cursor_npe(errors):
                    # If the provider rejects the same cursor even at the
                    # smaller size, replay this bounded (<10k) date/journal
                    # window through Crossref's offset pagination. Repository
                    # identity and the ingest seen-set make the replay idempotent.
                    offset_mode = True
                    offset = 0
                    cursor_used = "offset:0"
                    request_rows = min(request_rows, 250)
                    attempts_at_size = 0
                    continue
                break
            if errors and attempts > 1:
                errors = [
                    {
                        **item,
                        "attempts": attempts,
                        "retries_exhausted": attempts_at_size > self.max_retries,
                        **(
                            {
                                "row_fallback_from": row_fallback_from,
                                "rows_used": request_rows,
                                "pagination_mode": "offset" if offset_mode else "cursor",
                            }
                            if row_fallback_from is not None
                            else {}
                        ),
                    }
                    for item in errors
                ]
            current_signatures = {signature for paper in papers if (signature := paper_signature(paper))}
            overlap = len(current_signatures & previous_signatures) if previous_signatures else 0
            duplicate_rate = round(overlap / len(current_signatures), 4) if current_signatures else 0.0
            high_duplicate_pages = high_duplicate_pages + 1 if duplicate_rate > 0.95 else 0
            stop_reason = ""
            if errors:
                stop_reason = "error"
            elif fetched == 0:
                stop_reason = "items_empty"
            elif fetched < rows:
                stop_reason = "short_page"
            elif not next_cursor:
                stop_reason = "no_next_cursor"
            elif next_cursor == cursor_used:
                stop_reason = "next_cursor_not_advanced"
            elif high_duplicate_pages >= 2:
                stop_reason = "duplicate_rate_guard"
            elif max_pages and max_pages > 0 and page_number >= max_pages:
                stop_reason = "max_pages_per_chunk"
            elif chunk_deadline is not None and time.monotonic() >= chunk_deadline:
                stop_reason = "chunk_timeout"
            yield CrossrefPage(
                page=page_number,
                cursor_used=cursor_used,
                next_cursor=next_cursor,
                fetched=fetched,
                papers=papers,
                errors=errors,
                duplicate_rate_vs_previous_page=duplicate_rate,
                stop_reason=stop_reason,
                attempts=attempts,
                rows_used=request_rows,
                row_fallback_from=row_fallback_from,
                pagination_mode="offset" if offset_mode else "cursor",
            )
            if stop_reason:
                break
            previous_signatures = current_signatures
            cursor_used = next_cursor or cursor_used
            if offset_mode:
                offset = parse_offset_cursor(cursor_used)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    def _retry_delay(self, errors: list[dict[str, Any]], failed_attempt: int) -> float | None:
        exponential = min(
            self.retry_backoff_cap,
            self.retry_backoff_base * (2 ** max(0, failed_attempt - 1)),
        )
        retry_after = max(
            (float(item.get("retry_after_seconds") or 0.0) for item in errors),
            default=0.0,
        )
        if retry_after > self.retry_backoff_cap:
            return None
        return max(exponential, retry_after)

    def _fetch_journal_page_raw(
        self,
        journal: JournalEntry,
        from_date: str,
        to_date: str,
        cursor: str = "*",
        rows: int = 100,
        offset: int | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]], int]:
        errors: list[dict[str, Any]] = []
        issn = journal.issn_online or journal.issn_print
        params = {
            "filter": f"from-pub-date:{from_date},until-pub-date:{to_date},type:journal-article" + (f",issn:{issn}" if issn else ""),
            "rows": str(min(1000, max(1, rows))),
            "sort": "published",
            "order": "asc",
        }
        if offset is None:
            params["cursor"] = cursor or "*"
        elif offset >= 10_000:
            return (
                [],
                f"offset:{offset}",
                [
                    {
                        "journal": journal.canonical_name,
                        "type": "CrossrefOffsetLimit",
                        "message": "Crossref offset fallback reached the 10,000 record safety limit",
                        "transient": False,
                    }
                ],
                0,
            )
        else:
            params["offset"] = str(max(0, offset))
        if not issn:
            params["query.container-title"] = journal.canonical_name
        if self.mailto:
            params["mailto"] = self.mailto
        url = f"{CROSSREF_BASE}/works?{urllib.parse.urlencode(params)}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent(), "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                message = json.loads(response.read().decode("utf-8")).get("message", {})
            items = message.get("items", []) or []
            papers = [self.normalize_work(item, fallback_journal=journal.canonical_name) for item in items]
            next_position = (
                f"offset:{max(0, int(offset or 0)) + len(items)}"
                if offset is not None
                else str(message.get("next-cursor") or "")
            )
            return [paper for paper in papers if paper.get("title")], next_position, errors, len(items)
        except urllib.error.HTTPError as exc:
            retry_after = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            errors.append(
                {
                    "journal": journal.canonical_name,
                    "status": exc.code,
                    "response": exc.read().decode("utf-8", errors="replace")[:500],
                    "transient": exc.code in TRANSIENT_HTTP_STATUSES,
                    **({"retry_after_seconds": retry_after} if retry_after is not None else {}),
                }
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
            errors.append(network_exception_error(journal.canonical_name, exc))
        except Exception as exc:
            errors.append(
                {
                    "journal": journal.canonical_name,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "transient": False,
                }
            )
        return [], "", errors, 0

    def normalize_work(self, payload: dict[str, Any], fallback_journal: str) -> dict[str, Any]:
        doi = normalize_doi(payload.get("DOI"))
        published = payload.get("published-print") or payload.get("published-online") or payload.get("published") or payload.get("issued") or {}
        date = date_parts_to_date(published.get("date-parts") or [])
        title = clean_abstract(" ".join(payload.get("title") or []))
        journal = " ".join(payload.get("container-title") or []) or fallback_journal
        abstract = clean_abstract(payload.get("abstract") or "")
        url = payload.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        return {
            "id": f"doi:{doi}" if doi else stable_crossref_id(title, date),
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "publication_date": date,
            "journal": journal,
            "source": "crossref",
            "source_scope": "published",
            "data_mode": "real",
            "publisher": payload.get("publisher") or "",
            "subjects": payload.get("subject") or [],
            "reference_count": payload.get("reference-count") or 0,
            "license": payload.get("license") or [],
            "url": url,
            "is_open_access": bool(payload.get("license")),
            "oa_status": "license" if payload.get("license") else "",
            "raw_json": payload,
            "raw_crossref_json": payload,
        }

    def user_agent(self) -> str:
        if self.mailto:
            return f"condmat-trend-radar/0.1 (mailto:{self.mailto})"
        return "condmat-trend-radar/0.1"


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def network_exception_error(journal_name: str, exc: BaseException) -> dict[str, Any]:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    reason_type = type(reason).__name__
    return {
        "journal": journal_name,
        "type": type(exc).__name__,
        "reason_type": reason_type,
        "message": " ".join(str(exc).split())[:500],
        "transient": True,
    }


def is_transient_crossref_error(error: dict[str, Any]) -> bool:
    if "transient" in error:
        return bool(error.get("transient"))
    try:
        status = int(error.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status in TRANSIENT_HTTP_STATUSES:
        return True
    return str(error.get("type") or "") in TRANSIENT_NETWORK_ERROR_TYPES or str(
        error.get("reason_type") or ""
    ) in TRANSIENT_NETWORK_ERROR_TYPES


def parse_offset_cursor(value: str) -> int:
    if not value.startswith("offset:"):
        return 0
    try:
        return max(0, int(value.split(":", 1)[1]))
    except (TypeError, ValueError):
        return 0


def crossref_row_fallback(rows: int) -> int | None:
    if rows <= 100:
        return None
    candidate = max(100, min(250, rows // 4))
    return candidate if candidate < rows else None


def is_crossref_cursor_npe(errors: list[dict[str, Any]]) -> bool:
    for error in errors:
        try:
            status = int(error.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        detail = " ".join(
            str(error.get(key) or "")
            for key in ("response", "message", "type")
        ).lower()
        if status == 500 and (
            "nullpointerexception" in detail or "null pointer exception" in detail
        ):
            return True
    return False


def iterate_crossref_works(
    journal: JournalEntry,
    date_from: str,
    date_to: str,
    rows: int = 100,
    cursor: str = "*",
    max_pages: int = 50,
    sleep_seconds: float = 1.0,
    chunk_deadline: float | None = None,
    timeout: int = 20,
    page_hook: Callable[[str, int], None] | None = None,
) -> Iterator[CrossrefPage]:
    client = CrossrefClient(timeout=timeout, polite_delay=sleep_seconds)
    yield from client.iterate_crossref_works(
        journal,
        date_from,
        date_to,
        rows=rows,
        cursor=cursor,
        max_pages=max_pages,
        sleep_seconds=sleep_seconds,
        chunk_deadline=chunk_deadline,
        page_hook=page_hook,
    )


def cursor_prefix(value: str) -> str:
    return (value or "")[:40]


def paper_signature(paper: dict[str, Any]) -> str:
    doi = (paper.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = stable_title_key(str(paper.get("title") or ""))
    date = str(paper.get("publication_date") or "")
    return f"title:{title}|{date}" if title else ""


def stable_title_key(title: str) -> str:
    return "".join(ch.lower() for ch in title if ch.isalnum())


def clean_abstract(value: str) -> str:
    return html.unescape(" ".join(TAG_RE.sub(" ", value or "").split()))


def normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    doi = str(value).strip().lower()
    doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")
    return doi or None


def date_parts_to_date(parts: list[Any]) -> str:
    if not parts:
        return ""
    values = parts[0] if isinstance(parts[0], list) else parts
    if not values:
        return ""
    year = int(values[0])
    month = int(values[1]) if len(values) > 1 and values[1] else 1
    day = int(values[2]) if len(values) > 2 and values[2] else 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def stable_crossref_id(title: str, date: str) -> str:
    import hashlib

    normalized = "".join(ch.lower() for ch in f"{title}|{date}" if ch.isalnum())
    return "crossref:title:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
