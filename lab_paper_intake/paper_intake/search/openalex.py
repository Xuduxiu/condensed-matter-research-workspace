from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..models import Paper, normalize_arxiv_id, normalize_doi


OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def search_openalex(
    query: str,
    settings: Settings,
    max_results: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[Paper]:
    filters: list[str] = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")

    params: dict[str, Any] = {
        "search": query,
        "per-page": max(1, min(max_results, 200)),
        "sort": "relevance_score:desc",
    }
    if filters:
        params["filter"] = ",".join(filters)
    if settings.unpaywall_email:
        params["mailto"] = settings.unpaywall_email

    response = httpx.get(
        OPENALEX_WORKS_URL,
        params=params,
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return [_paper_from_work(work) for work in data.get("results", [])]


def _paper_from_work(work: dict[str, Any]) -> Paper:
    primary_location = work.get("primary_location") or {}
    best_oa_location = work.get("best_oa_location") or {}
    source = primary_location.get("source") or {}
    ids = work.get("ids") or {}

    doi = normalize_doi(work.get("doi") or ids.get("doi"))
    arxiv_id = normalize_arxiv_id(ids.get("arxiv") or _arxiv_from_locations(work))
    pdf_url = _best_pdf_url(work)

    return Paper(
        title=work.get("title") or "Untitled",
        authors=[
            item.get("author", {}).get("display_name", "")
            for item in work.get("authorships", [])
            if item.get("author", {}).get("display_name")
        ],
        year=work.get("publication_year"),
        journal=source.get("display_name") or _source_from_best_location(best_oa_location),
        doi=doi,
        arxiv_id=arxiv_id,
        abstract=_abstract_from_inverted_index(work.get("abstract_inverted_index")),
        url=ids.get("doi") or work.get("id"),
        pdf_url=pdf_url,
        pdf_status="oa_available" if pdf_url else "metadata_only",
        source="OpenAlex",
        citation_count=work.get("cited_by_count"),
        raw={
            "openalex_id": work.get("id"),
            "open_access": work.get("open_access"),
            "primary_location": primary_location,
            "best_oa_location": best_oa_location,
            "ids": ids,
        },
    )


def _abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    return " ".join(word for _, word in sorted(words))


def _best_pdf_url(work: dict[str, Any]) -> str | None:
    for location_key in ["best_oa_location", "primary_location"]:
        location = work.get(location_key) or {}
        for key in ["pdf_url", "landing_page_url"]:
            url = location.get(key)
            if url and (key == "pdf_url" or str(url).lower().endswith(".pdf")):
                return url
    for location in work.get("locations") or []:
        url = location.get("pdf_url")
        if url:
            return url
    open_access = work.get("open_access") or {}
    oa_url = open_access.get("oa_url")
    if oa_url and str(oa_url).lower().endswith(".pdf"):
        return oa_url
    return None


def _source_from_best_location(location: dict[str, Any]) -> str | None:
    source = location.get("source") or {}
    return source.get("display_name")


def _arxiv_from_locations(work: dict[str, Any]) -> str | None:
    urls: list[str] = []
    for location_key in ["best_oa_location", "primary_location"]:
        location = work.get(location_key) or {}
        urls.extend(
            str(location.get(key))
            for key in ["landing_page_url", "pdf_url"]
            if location.get(key)
        )
    for location in work.get("locations") or []:
        urls.extend(
            str(location.get(key))
            for key in ["landing_page_url", "pdf_url"]
            if location.get(key)
        )
    for url in urls:
        if "arxiv.org/" in url:
            return url
    return None
