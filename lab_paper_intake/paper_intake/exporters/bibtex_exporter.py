from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from ..models import Paper


def export_bibtex(papers: list[Paper], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(papers_to_bibtex(papers), encoding="utf-8")
    return path


def papers_to_bibtex(papers: list[Paper]) -> str:
    used_keys: set[str] = set()
    entries = [_paper_to_bibtex(paper, used_keys) for paper in papers]
    return "\n\n".join(entries) + ("\n" if entries else "")


def citation_key_for_paper(paper: Paper, used_keys: set[str] | None = None) -> str:
    used = used_keys if used_keys is not None else set()
    first_author = "unknown"
    if paper.authors:
        first_author = _author_key_part(paper.authors[0])
    title_word = "paper"
    for word in re.findall(r"[A-Za-z0-9]+", paper.title):
        if len(word) > 2:
            title_word = word
            break
    base = _slug(f"{first_author}_{paper.year or 'nd'}_{title_word}")
    key = base
    suffix = 2
    while key in used:
        key = f"{base}_{suffix}"
        suffix += 1
    used.add(key)
    return key


def _paper_to_bibtex(paper: Paper, used_keys: set[str]) -> str:
    entry_type = "article" if paper.journal else "misc"
    key = citation_key_for_paper(paper, used_keys)
    fields: list[tuple[str, Any]] = [
        ("title", paper.title),
        ("author", " and ".join(_dedupe_authors(paper.authors)) if paper.authors else None),
        ("year", paper.year),
        ("journal", paper.journal if entry_type == "article" else None),
        ("doi", paper.doi),
        ("url", paper.url or paper.pdf_url),
        ("abstract", paper.abstract),
        ("eprint", paper.arxiv_id),
        ("archivePrefix", "arXiv" if paper.arxiv_id else None),
        ("file", _bibtex_file_path(paper.local_pdf_path)),
    ]
    body = ",\n".join(
        f"  {name} = {{{_format_bibtex_value(name, value)}}}"
        for name, value in fields
        if _present(value)
    )
    return f"@{entry_type}{{{key},\n{body}\n}}"


def _author_key_part(author: str) -> str:
    if "," in author:
        candidate = author.split(",", 1)[0]
    else:
        parts = author.split()
        candidate = parts[-1] if parts else author
    return candidate


def _dedupe_authors(authors: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for author in authors:
        cleaned = " ".join((author or "").split())
        key = _author_identity_key(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            values.append(cleaned)
    return values


def _author_identity_key(author: str) -> str:
    value = author.lower().replace(".", " ")
    tokens = re.findall(r"[a-z]+", value)
    if not tokens:
        return ""
    if "," in author:
        surname = re.findall(r"[a-z]+", author.split(",", 1)[0].lower())
        last = surname[-1] if surname else tokens[0]
        rest = re.findall(r"[a-z]+", author.split(",", 1)[1].lower())
    else:
        last = tokens[-1]
        rest = tokens[:-1]
    initials = "".join(token[0] for token in rest)
    return f"{last}:{initials[:3]}"


def _bibtex_file_path(path: str | None) -> str | None:
    if not path:
        return None
    return path.replace("\\", "/")

def _format_bibtex_value(name: str, value: object) -> str:
    text = str(value)
    if name == "file":
        return text.replace("\\", "/")
    return _escape_bibtex(text)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"none", "nan"}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "", value.replace("-", "_"))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned.lower() or "paper"


def _escape_bibtex(value: str) -> str:
    return (
        value.replace("\\", "\\textbackslash{}")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("&", "\\&")
        .replace("%", "\\%")
        .replace("$", "\\$")
        .replace("#", "\\#")
        .replace("_", "\\_")
    )