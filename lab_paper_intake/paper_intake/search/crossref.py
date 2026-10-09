from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..models import Paper, normalize_doi


CROSSREF_WORKS_URL = "https://api.crossref.org/works"


def fetch_crossref_by_doi(doi: str, settings: Settings) -> Paper | None:
    normalized = normalize_doi(doi)
    if not normalized:
        return None
    response = httpx.get(
        f"{CROSSREF_WORKS_URL}/{normalized}",
        timeout=settings.request_timeout_seconds,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    message = response.json().get("message", {})
    return _paper_from_message(message)


def search_crossref(
    query: str,
    settings: Settings,
    max_results: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[Paper]:
    filters: list[str] = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    params: dict[str, Any] = {
        "query.bibliographic": query,
        "rows": max(1, min(max_results, 50)),
    }
    if filters:
        params["filter"] = ",".join(filters)
    response = httpx.get(
        CROSSREF_WORKS_URL,
        params=params,
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    items = response.json().get("message", {}).get("items", [])
    return [_paper_from_message(item) for item in items if item.get("title")]


def _paper_from_message(item: dict[str, Any]) -> Paper:
    title_values = item.get("title") or ["Untitled"]
    container = item.get("container-title") or []
    authors = []
    for author in item.get("author") or []:
        name = " ".join(
            part
            for part in [author.get("given"), author.get("family")]
            if part
        )
        if name:
            authors.append(name)
    return Paper(
        title=title_values[0],
        authors=authors,
        year=_crossref_year(item),
        journal=container[0] if container else None,
        doi=normalize_doi(item.get("DOI")),
        abstract=item.get("abstract"),
        url=item.get("URL"),
        source="Crossref",
        citation_count=item.get("is-referenced-by-count"),
        raw={"crossref_type": item.get("type")},
    )


def _crossref_year(item: dict[str, Any]) -> int | None:
    for key in ["published-print", "published-online", "published", "created"]:
        parts = item.get(key, {}).get("date-parts")
        if parts and parts[0]:
            return int(parts[0][0])
    return None
