from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

from .models import Paper, normalize_arxiv_id, normalize_doi, normalize_title


def deduplicate_papers(papers: Iterable[Paper], fuzzy_threshold: float = 0.94) -> list[Paper]:
    merged: list[Paper] = []
    doi_index: dict[str, int] = {}
    arxiv_index: dict[str, int] = {}
    title_index: dict[str, int] = {}

    for incoming in papers:
        incoming.normalized_title = incoming.normalized_title or normalize_title(
            incoming.title
        )
        incoming.authors = _clean_author_list(incoming.authors)
        doi = normalize_doi(incoming.doi)
        arxiv_id = normalize_arxiv_id(incoming.arxiv_id)
        title = incoming.normalized_title

        match_idx = None
        if doi and doi in doi_index:
            match_idx = doi_index[doi]
        elif arxiv_id and arxiv_id in arxiv_index:
            match_idx = arxiv_index[arxiv_id]
        elif title and title in title_index:
            match_idx = title_index[title]
        else:
            match_idx = _find_fuzzy_title_match(title, merged, fuzzy_threshold)

        if match_idx is None:
            new_idx = len(merged)
            merged.append(incoming)
            _index_paper(incoming, new_idx, doi_index, arxiv_index, title_index)
        else:
            merged[match_idx] = merge_papers(merged[match_idx], incoming)
            _index_paper(merged[match_idx], match_idx, doi_index, arxiv_index, title_index)

    return merged


def _find_fuzzy_title_match(
    title: str,
    papers: list[Paper],
    threshold: float,
) -> int | None:
    if not title or len(title) < 16:
        return None
    for idx, paper in enumerate(papers):
        candidate = paper.normalized_title
        if not candidate or len(candidate) < 16:
            continue
        if SequenceMatcher(None, title, candidate).ratio() >= threshold:
            return idx
    return None


def _index_paper(
    paper: Paper,
    idx: int,
    doi_index: dict[str, int],
    arxiv_index: dict[str, int],
    title_index: dict[str, int],
) -> None:
    doi = normalize_doi(paper.doi)
    arxiv_id = normalize_arxiv_id(paper.arxiv_id)
    if doi:
        doi_index[doi] = idx
    if arxiv_id:
        arxiv_index[arxiv_id] = idx
    if paper.normalized_title:
        title_index[paper.normalized_title] = idx


def merge_papers(primary: Paper, secondary: Paper) -> Paper:
    base = _better_metadata(primary, secondary)
    other = secondary if base is primary else primary
    data = base.model_dump(mode="python")

    for field in [
        "journal",
        "doi",
        "arxiv_id",
        "abstract",
        "url",
        "pdf_url",
        "local_pdf_path",
        "source",
        "citation_count",
        "relevance_score",
        "relevance_reason",
    ]:
        if not data.get(field) and getattr(other, field):
            data[field] = getattr(other, field)

    if not data.get("year") and other.year:
        data["year"] = other.year
    if other.citation_count and (
        data.get("citation_count") is None
        or other.citation_count > int(data.get("citation_count") or 0)
    ):
        data["citation_count"] = other.citation_count

    data["authors"] = _select_author_list(primary, secondary)
    data["tags"] = _merge_list(base.tags, other.tags)
    data["selected"] = base.selected or other.selected
    data["raw"] = {**other.raw, **base.raw}

    if other.pdf_status == "downloaded" or data.get("pdf_status") == "downloaded":
        data["pdf_status"] = "downloaded"
        data["local_pdf_path"] = data.get("local_pdf_path") or other.local_pdf_path
    elif data.get("pdf_status") == "metadata_only" and other.pdf_status != "metadata_only":
        data["pdf_status"] = other.pdf_status

    return Paper(**data)


def _better_metadata(a: Paper, b: Paper) -> Paper:
    return a if _metadata_score(a) >= _metadata_score(b) else b


def _metadata_score(paper: Paper) -> int:
    score = 0
    for value in [
        paper.title,
        paper.authors,
        paper.year,
        paper.journal,
        paper.doi,
        paper.arxiv_id,
        paper.abstract,
        paper.url,
        paper.pdf_url,
        paper.local_pdf_path,
        paper.citation_count,
    ]:
        if value:
            score += 1
    if paper.abstract:
        score += min(len(paper.abstract) // 500, 4)
    if paper.pdf_status == "downloaded":
        score += 3
    return score


def _select_author_list(a: Paper, b: Paper) -> list[str]:
    first = _clean_author_list(a.authors)
    second = _clean_author_list(b.authors)
    if not first:
        return second
    if not second:
        return first

    # For duplicate records of the same paper, do not concatenate author lists.
    # Pick the strongest metadata source/list to avoid mixed initials + full names.
    scored = sorted(
        [(a, first), (b, second)],
        key=lambda item: _author_list_score(item[0], item[1]),
        reverse=True,
    )
    return scored[0][1]


def _author_list_score(paper: Paper, authors: list[str]) -> tuple[int, int, int, int]:
    source = (paper.source or "").lower()
    if "crossref" in source or "publisher" in source:
        source_score = 3
    elif "openalex" in source:
        source_score = 2
    elif "arxiv" in source:
        source_score = 1
    else:
        source_score = 0
    complete_names = sum(1 for author in authors if _author_completeness(author) >= 2)
    reasonable_length = 1 if 0 < len(authors) <= 80 else 0
    return (source_score, reasonable_length, complete_names, len(authors))


def _clean_author_list(authors: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for author in authors:
        normalized = _normalize_author_display(author)
        if not normalized:
            continue
        key = _author_identity_key(normalized)
        if key and key not in seen:
            seen.add(key)
            cleaned.append(normalized)
    return cleaned


def _normalize_author_display(author: str) -> str:
    value = re.sub(r"\s+", " ", author or "").strip()
    value = re.sub(r"\s+,\s+", ", ", value)
    return value


def _author_identity_key(author: str) -> str:
    value = author.lower().replace(".", " ")
    tokens = re.findall(r"[a-z]+", value)
    if not tokens:
        return ""
    if "," in author:
        surname = re.findall(r"[a-z]+", author.split(",", 1)[0].lower())
        last = surname[-1] if surname else tokens[0]
    else:
        last = tokens[-1]
    initials = "".join(token[0] for token in tokens[:-1])
    return f"{last}:{initials[:3]}"


def _author_completeness(author: str) -> int:
    tokens = re.findall(r"[A-Za-z]+", author.replace(",", " "))
    full_tokens = [token for token in tokens if len(token) > 1]
    return len(full_tokens)


def _merge_list(first: list[str], second: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            values.append(item)
    return values