from __future__ import annotations

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from backend.config import logs_dir, openalex_api_key, openalex_mailto
from backend.nlp.dictionaries import CORE_JOURNALS


OPENALEX_BASE = "https://api.openalex.org"
OPENALEX_CONDMAT_SUBFIELD_ID = "3104"
OPENALEX_DISCOVERY_TYPES = ("article", "preprint", "review")
OPENALEX_DISCOVERY_SELECT = ",".join(
    (
        "id",
        "doi",
        "title",
        "abstract_inverted_index",
        "publication_date",
        "primary_location",
        "locations",
        "open_access",
        "authorships",
        "concepts",
        "topics",
        "primary_topic",
        "keywords",
        "cited_by_count",
        "referenced_works",
        "related_works",
        "type",
        "type_crossref",
    )
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MISSING_KEY_WARNING = "OpenAlex API key is missing. Current API may return 429/0 daily budget. Set OPENALEX_API_KEY in .env."


class OpenAlexHTTPError(RuntimeError):
    def __init__(self, url: str, params: dict[str, str], status: int | None, response_text: str, retry_count: int, message: str) -> None:
        super().__init__(message)
        self.url = url
        self.params = params
        self.status = status
        self.response_text = response_text[:500]
        self.retry_count = retry_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": redact_url(self.url),
            "params": redact_params(self.params),
            "status": self.status,
            "response_text_first_500": self.response_text,
            "retry_count": self.retry_count,
            "message": str(self),
        }


@dataclass
class OpenAlexClient:
    email: str | None = None
    api_key: str | None = None
    timeout: int = 20
    polite_delay: float = 0.12
    max_retries: int = 5
    warn_if_missing_key: bool = True

    def __post_init__(self) -> None:
        self.email = self.email or openalex_mailto() or os.getenv("CONDMAT_RADAR_OPENALEX_EMAIL") or None
        self.api_key = self.api_key or openalex_api_key()
        if self.warn_if_missing_key and not self.api_key:
            print(MISSING_KEY_WARNING, file=sys.stderr)

    def fetch_journal_works(
        self,
        journal: str,
        from_date: str,
        to_date: str,
        per_page: int = 200,
        max_pages: int = 2,
        cursor: str = "*",
    ) -> list[dict[str, Any]]:
        source_id = self.find_source_id(journal)
        if not source_id:
            return []
        works: list[dict[str, Any]] = []
        next_cursor = cursor or "*"
        page_limit = max_pages if max_pages and max_pages > 0 else 10_000
        for _page in range(page_limit):
            payload = self.fetch_journal_page(journal, source_id, from_date, to_date, per_page=per_page, cursor=next_cursor)
            results = payload.get("results", [])
            works.extend(self._normalize_work(item, journal) for item in results)
            next_cursor = payload.get("meta", {}).get("next_cursor")
            if not next_cursor or not results:
                break
            time.sleep(self.polite_delay)
        return works

    def fetch_journal_page(
        self,
        journal: str,
        source_id: str,
        from_date: str,
        to_date: str,
        per_page: int = 200,
        cursor: str = "*",
    ) -> dict[str, Any]:
        filters = ",".join(
            [
                f"from_publication_date:{from_date}",
                f"to_publication_date:{to_date}",
                f"locations.source.id:{source_id}",
                "type:article",
            ]
        )
        return self._get_json(
            "/works",
            {
                "filter": filters,
                "per-page": str(min(100, max(1, per_page))),
                "cursor": cursor or "*",
                "select": "id,doi,title,abstract_inverted_index,publication_date,primary_location,locations,open_access,authorships,concepts,cited_by_count,referenced_works,type,type_crossref",
                "sort": "publication_date:asc",
            },
        )

    def fetch_condensed_matter_page(
        self,
        from_date: str,
        to_date: str,
        per_page: int = 100,
        cursor: str = "*",
    ) -> dict[str, Any]:
        """Fetch one cursor page for the whole OpenAlex condensed-matter subfield.

        The caller owns cursor persistence and completion decisions. Keeping this
        method page-shaped makes partial runs auditable and prevents a failed page
        from being mistaken for a completed date window.
        """
        filters = ",".join(
            (
                f"topics.subfield.id:{OPENALEX_CONDMAT_SUBFIELD_ID}",
                f"from_publication_date:{from_date}",
                f"to_publication_date:{to_date}",
                f"type:{'|'.join(OPENALEX_DISCOVERY_TYPES)}",
            )
        )
        return self._get_json(
            "/works",
            {
                "filter": filters,
                "per-page": str(min(100, max(1, per_page))),
                "cursor": cursor or "*",
                "select": OPENALEX_DISCOVERY_SELECT,
                "sort": "publication_date:asc",
            },
        )

    def count_works(self, journal: str, from_date: str, to_date: str) -> int:
        source_id = self.find_source_id(journal)
        if not source_id:
            return 0
        payload = self._get_json(
            "/works",
            {
                "filter": f"from_publication_date:{from_date},to_publication_date:{to_date},locations.source.id:{source_id},type:article",
                "per-page": "1",
                "select": "id",
            },
        )
        return int(payload.get("meta", {}).get("count") or 0)

    def fetch_many_journals(
        self,
        from_date: str,
        to_date: str,
        journals: Iterable[str] = CORE_JOURNALS,
        per_page: int = 200,
        max_pages: int = 1,
    ) -> tuple[list[dict[str, Any]], int]:
        papers: list[dict[str, Any]] = []
        failures = 0
        for journal in journals:
            try:
                papers.extend(self.fetch_journal_works(journal, from_date=from_date, to_date=to_date, per_page=per_page, max_pages=max_pages))
            except Exception as exc:
                failures += 1
                if isinstance(exc, OpenAlexHTTPError):
                    self._write_error_log(exc)
        return papers, failures

    def get_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Fetch one exact DOI record for cross-source coverage backfill."""
        normalized = str(doi or "").strip().lower()
        normalized = normalized.removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")
        if not normalized:
            return None
        payload = self._get_json(
            "/works",
            {
                "filter": f"doi:{normalized}",
                "per-page": "5",
                "select": "id,doi,title,abstract_inverted_index,publication_date,primary_location,locations,open_access,authorships,concepts,cited_by_count,referenced_works,type,type_crossref",
            },
        )
        for work in payload.get("results", []) or []:
            candidate = str(work.get("doi") or "").strip().lower()
            candidate = candidate.removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")
            if candidate == normalized:
                return self._normalize_work(work, "")
        return None

    def find_source_id(self, journal: str) -> str | None:
        payload = self._get_json("/sources", {"search": journal, "per-page": "5"})
        best = None
        for item in payload.get("results", []):
            name = item.get("display_name") or ""
            if name.lower() == journal.lower():
                best = item
                break
            if not best and journal.lower() in name.lower():
                best = item
        if not best:
            return None
        return str(best.get("id", "")).removeprefix("https://openalex.org/")

    def _get_json(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        request_params = dict(params)
        if self.email:
            request_params["mailto"] = self.email
        if self.api_key:
            request_params["api_key"] = self.api_key
        url = f"{OPENALEX_BASE}{endpoint}?{urllib.parse.urlencode(request_params)}"
        headers = {"User-Agent": "condmat-trend-radar/0.1", "Accept": "application/json"}
        last_error: OpenAlexHTTPError | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                response_text = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = OpenAlexHTTPError(url, request_params, exc.code, response_text, attempt, f"OpenAlex HTTP {exc.code}")
                retry_after = exc.headers.get("Retry-After")
                retry_after_seconds = float(retry_after) if retry_after and retry_after.isdigit() else 0.0
                budget_exhausted = "Insufficient budget" in response_text or retry_after_seconds > 300
                if exc.code not in RETRY_STATUSES or budget_exhausted or attempt >= self.max_retries:
                    self._write_error_log(last_error)
                    raise last_error from exc
                delay = retry_after_seconds if retry_after_seconds else min(30.0, 2.0 ** attempt)
                time.sleep(min(delay, 30.0))
            except urllib.error.URLError as exc:
                last_error = OpenAlexHTTPError(url, request_params, None, str(exc)[:500], attempt, "OpenAlex connection failed")
                if attempt >= self.max_retries:
                    self._write_error_log(last_error)
                    raise last_error from exc
                time.sleep(min(30.0, 2.0 ** attempt))
        if last_error:
            self._write_error_log(last_error)
            raise last_error
        raise RuntimeError("OpenAlex request failed without error context")

    def _write_error_log(self, error: OpenAlexHTTPError) -> None:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"openalex_error_{stamp}.log"
        path.write_text(json.dumps(error.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _normalize_work(self, work: dict[str, Any], fallback_journal: str) -> dict[str, Any]:
        location = work.get("primary_location") or {}
        source = location.get("source") or {}
        journal = source.get("display_name") or fallback_journal
        doi = work.get("doi")
        authors = [
            item.get("author", {}).get("display_name")
            for item in work.get("authorships", [])
            if item.get("author", {}).get("display_name")
        ]
        topics = work.get("topics") or []
        primary_topic = work.get("primary_topic") or {}
        keywords = work.get("keywords") or []
        concepts: list[str] = []
        seen_concepts: set[str] = set()
        concept_items = [*(work.get("concepts") or []), *topics]
        if primary_topic:
            concept_items.append(primary_topic)
        for item in concept_items:
            name = item.get("display_name") if isinstance(item, dict) else None
            normalized_name = str(name or "").strip()
            identity = normalized_name.casefold()
            if not normalized_name or identity in seen_concepts:
                continue
            seen_concepts.add(identity)
            concepts.append(normalized_name)
        open_access = work.get("open_access") or {}
        pdf_url = location.get("pdf_url") or open_access.get("oa_url") or ""
        url = location.get("landing_page_url") or pdf_url or work.get("id")
        abstract = inverted_index_to_text(work.get("abstract_inverted_index"))
        work_type = work.get("type") or "article"
        primary_source_type = str(source.get("type") or "").strip().lower()
        return {
            "id": work.get("id"),
            "openalex_id": work.get("id"),
            "doi": doi,
            "title": html.unescape(work.get("title") or ""),
            "abstract": abstract,
            "publication_date": work.get("publication_date") or "",
            "journal": journal,
            "source": "openalex",
            "source_scope": "preprint" if work_type == "preprint" else "published",
            "data_mode": "real",
            "authors": authors,
            "concepts": concepts,
            "topics": topics,
            "primary_topic": primary_topic,
            "keywords": keywords,
            "locations": work.get("locations") or [],
            "cited_by_count": work.get("cited_by_count") or 0,
            "referenced_works": work.get("referenced_works") or [],
            "url": url,
            "pdf_url": pdf_url,
            "is_open_access": bool(open_access.get("is_oa") or pdf_url),
            "oa_status": open_access.get("oa_status") or "",
            "type": work_type,
            "openalex_type_crossref": work.get("type_crossref") or "",
            "primary_source_id": source.get("id") or "",
            "primary_source_name": source.get("display_name") or "",
            "primary_source_type": primary_source_type or "unknown",
            "raw_json": work,
        }


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [(key, "***REDACTED***" if key == "api_key" else value) for key, value in query]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query), parsed.fragment))


def redact_params(params: dict[str, str]) -> dict[str, str]:
    redacted = dict(params)
    if redacted.get("api_key"):
        redacted["api_key"] = "***REDACTED***"
    return redacted


def inverted_index_to_text(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, offsets in index.items():
        for offset in offsets:
            positions[int(offset)] = word
    return " ".join(positions[pos] for pos in sorted(positions))
