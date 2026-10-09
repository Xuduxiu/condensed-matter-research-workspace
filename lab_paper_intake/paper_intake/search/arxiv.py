from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from ..config import Settings
from ..models import Paper, normalize_arxiv_id, normalize_doi


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def search_arxiv(
    query: str,
    settings: Settings,
    max_results: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[Paper]:
    response = httpx.get(
        ARXIV_API_URL,
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max(1, min(max_results, 100)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    papers = [_paper_from_entry(entry) for entry in root.findall(f"{ATOM_NS}entry")]
    if year_from or year_to:
        papers = [
            paper
            for paper in papers
            if paper.year
            and (year_from is None or paper.year >= year_from)
            and (year_to is None or paper.year <= year_to)
        ]
    return papers


def _paper_from_entry(entry: ET.Element) -> Paper:
    title = _text(entry, f"{ATOM_NS}title") or "Untitled"
    abstract = _text(entry, f"{ATOM_NS}summary")
    published = _text(entry, f"{ATOM_NS}published")
    year = _year_from_date(published)
    entry_id = _text(entry, f"{ATOM_NS}id")
    arxiv_id = normalize_arxiv_id(entry_id)
    pdf_url = _pdf_url(entry)
    doi = normalize_doi(_text(entry, f"{ARXIV_NS}doi"))
    journal = _text(entry, f"{ARXIV_NS}journal_ref")

    return Paper(
        title=re.sub(r"\s+", " ", title).strip(),
        authors=[
            _text(author, f"{ATOM_NS}name") or ""
            for author in entry.findall(f"{ATOM_NS}author")
            if _text(author, f"{ATOM_NS}name")
        ],
        year=year,
        journal=journal,
        doi=doi,
        arxiv_id=arxiv_id,
        abstract=re.sub(r"\s+", " ", abstract or "").strip() or None,
        url=entry_id,
        pdf_url=pdf_url,
        pdf_status="oa_available" if pdf_url else "metadata_only",
        source="arXiv",
        raw={
            "published": published,
            "categories": [
                category.attrib.get("term", "")
                for category in entry.findall(f"{ATOM_NS}category")
                if category.attrib.get("term")
            ],
        },
    )


def _text(entry: ET.Element, path: str) -> str | None:
    found = entry.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _year_from_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).year
    except ValueError:
        match = re.search(r"\d{4}", value)
        return int(match.group(0)) if match else None


def _pdf_url(entry: ET.Element) -> str | None:
    for link in entry.findall(f"{ATOM_NS}link"):
        if link.attrib.get("title") == "pdf":
            return link.attrib.get("href")
    entry_id = _text(entry, f"{ATOM_NS}id")
    arxiv_id = normalize_arxiv_id(entry_id)
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else None
