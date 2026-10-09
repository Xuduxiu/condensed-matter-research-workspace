from __future__ import annotations

from .config import Settings
from .models import Paper
from .search.unpaywall import resolve_unpaywall_pdf


def resolve_pdf(paper: Paper, settings: Settings) -> Paper:
    data = paper.model_dump(mode="python")

    if paper.arxiv_id:
        data["pdf_url"] = f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf"
        data["pdf_status"] = "oa_available"
        return Paper(**data)

    if paper.pdf_url:
        data["pdf_status"] = "oa_available"
        return Paper(**data)

    if paper.doi:
        try:
            unpaywall_url = resolve_unpaywall_pdf(paper.doi, settings)
        except Exception:
            unpaywall_url = None
        if unpaywall_url:
            data["pdf_url"] = unpaywall_url
            data["pdf_status"] = "oa_available"
            return Paper(**data)

    openalex_url = _openalex_oa_url(paper)
    if openalex_url:
        data["pdf_url"] = openalex_url
        data["pdf_status"] = "oa_available"
    else:
        data["pdf_status"] = "metadata_only"
    return Paper(**data)


def resolve_pdfs(papers: list[Paper], settings: Settings) -> list[Paper]:
    return [resolve_pdf(paper, settings) for paper in papers]


def _openalex_oa_url(paper: Paper) -> str | None:
    raw = paper.raw or {}
    for location_key in ["best_oa_location", "primary_location"]:
        location = raw.get(location_key) or {}
        pdf_url = location.get("pdf_url")
        if pdf_url:
            return pdf_url
    open_access = raw.get("open_access") or {}
    oa_url = open_access.get("oa_url")
    if oa_url and str(oa_url).lower().endswith(".pdf"):
        return oa_url
    return None
