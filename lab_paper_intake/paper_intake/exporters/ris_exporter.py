from __future__ import annotations

import html
import math
import re
from pathlib import Path
from typing import Any

from ..models import Paper


def export_ris(papers: list[Paper], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(papers_to_ris(papers), encoding="utf-8")
    return path


def papers_to_ris(papers: list[Paper]) -> str:
    return "\n".join(_paper_to_ris(paper) for paper in papers) + ("\n" if papers else "")


def _paper_to_ris(paper: Paper) -> str:
    lines = ["TY  - JOUR"]
    for author in _dedupe_authors(paper.authors):
        _append(lines, "AU", author)
    _append(lines, "TI", paper.title)
    _append(lines, "PY", paper.year)
    _append(lines, "JO", paper.journal)
    _append(lines, "DO", paper.doi)
    _append(lines, "UR", paper.url)
    _append(lines, "L1", paper.local_pdf_path or paper.pdf_url)
    _append(lines, "AB", paper.abstract)
    for tag in _dedupe_values(paper.tags):
        _append(lines, "KW", tag)
    summary_text = _summary_text(paper)
    _append(lines, "N1", f"中文摘要：{summary_text}" if summary_text else None)
    _append(lines, "N1", f"相关性评分：{paper.relevance_score}" if _present(paper.relevance_score) else None)
    _append(lines, "N1", f"相关性理由：{paper.relevance_reason}" if _present(paper.relevance_reason) else None)
    _append(lines, "N1", f"阅读优先级：{paper.chinese_summary.reading_priority}" if _present(paper.chinese_summary.reading_priority) else None)
    _append(lines, "N1", f"本地 PDF：{paper.local_pdf_path}" if _present(paper.local_pdf_path) else None)
    lines.append("ER  -")
    return "\n".join(lines)


def _append(lines: list[str], tag: str, value: Any) -> None:
    if _present(value):
        lines.append(f"{tag}  - {_clean(str(value))}")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"none", "nan"}


def _summary_text(paper: Paper) -> str:
    summary = paper.chinese_summary
    return "；".join(
        _clean(part)
        for part in [
            summary.problem,
            summary.method,
            summary.key_results,
            summary.relation_to_lab,
        ]
        if _present(part)
    )


def _dedupe_values(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            output.append(cleaned)
    return output


def _dedupe_authors(authors: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for author in authors:
        cleaned = _clean(author)
        key = _author_key(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            values.append(cleaned)
    return values


def _author_key(author: str) -> str:
    if "," in author:
        last, first = [part.strip().lower() for part in author.split(",", 1)]
        last_parts = [part for part in re.sub(r"[^a-z0-9 ]+", " ", last).split() if part]
        first_parts = [part for part in re.sub(r"[^a-z0-9 ]+", " ", first).split() if part]
        if last_parts:
            return f"{last_parts[-1]}:{''.join(part[0] for part in first_parts)}"
    value = re.sub(r"[^a-z0-9 ]+", " ", author.lower())
    parts = [part for part in value.split() if part]
    if not parts:
        return ""
    return f"{parts[-1]}:{''.join(part[0] for part in parts[:-1])}"


def _clean(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())