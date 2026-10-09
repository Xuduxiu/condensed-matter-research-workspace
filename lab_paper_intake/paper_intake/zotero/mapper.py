from __future__ import annotations

import html
import math
import re
from typing import Any

from paper_intake.models import ChineseSummary, Paper, normalize_title


def paper_to_zotero_item(
    paper: Paper,
    collection_key: str | None = None,
    include_tags: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {"itemType": _item_type(paper)}
    _set_if_value(item, "title", paper.title)
    _set_if_value(item, "abstractNote", paper.abstract)
    _set_if_value(item, "date", str(paper.year) if paper.year else None)
    _set_if_value(item, "publicationTitle", paper.journal)
    _set_if_value(item, "DOI", paper.doi)
    _set_if_value(item, "url", paper.url)
    if paper.arxiv_id and item["itemType"] == "preprint":
        item["repository"] = "arXiv"
        item["archiveID"] = paper.arxiv_id

    creators = authors_to_zotero_creators(paper.authors)
    if creators:
        item["creators"] = creators

    tags = _zotero_tags(paper) if include_tags else []
    if tags:
        item["tags"] = [{"tag": tag} for tag in tags]

    if collection_key:
        item["collections"] = [collection_key]
    return _drop_empty(item)


def authors_to_zotero_creators(authors: list[str]) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    seen: set[str] = set()
    for author in authors:
        value = _clean_text(author)
        if not value:
            continue
        key = _author_key(value)
        if key in seen:
            continue
        seen.add(key)
        creator = _split_author(value)
        creator["creatorType"] = "author"
        creators.append(creator)
    return creators


def build_zotero_note_html(paper: Paper, export_id: str | None = None) -> str:
    summary = paper.chinese_summary
    if isinstance(summary, ChineseSummary) and summary.has_content():
        problem = summary.problem
        method = summary.method
        key_results = summary.key_results
        relation = summary.relation_to_lab
        priority = summary.reading_priority
    elif isinstance(summary, str) and summary.strip():
        problem = summary
        method = key_results = relation = ""
        priority = ""
    else:
        problem = "未生成中文摘要。"
        method = key_results = relation = ""
        priority = getattr(summary, "reading_priority", "")

    parts = [
        "<h2>Lab Paper Intake 中文摘要</h2>",
        "<h3>研究问题</h3>",
        f"<p>{_escape(problem or '未生成中文摘要。')}</p>",
        "<h3>方法</h3>",
        f"<p>{_escape(method)}</p>",
        "<h3>关键结果</h3>",
        f"<p>{_escape(key_results)}</p>",
        "<h3>与实验室课题关系</h3>",
        f"<p>{_escape(relation)}</p>",
        "<h3>相关性判断</h3>",
        "<ul>",
        f"<li>相关性评分：{_escape(_value_text(paper.relevance_score))}</li>",
        f"<li>相关性理由：{_escape(paper.relevance_reason or '')}</li>",
        f"<li>阅读优先级：{_escape(priority or '')}</li>",
        "</ul>",
        "<h3>来源</h3>",
        "<ul>",
        f"<li>DOI：{_escape(paper.doi or '')}</li>",
        f"<li>arXiv：{_escape(paper.arxiv_id or '')}</li>",
        f"<li>PDF URL：{_escape(paper.pdf_url or '')}</li>",
        f"<li>本地 PDF：{_escape(paper.local_pdf_path or '')}</li>",
        f"<li>Export ID：{_escape(export_id or '')}</li>",
        "</ul>",
    ]
    return "\n".join(parts)


def _item_type(paper: Paper) -> str:
    if paper.journal or paper.doi:
        return "journalArticle"
    if paper.arxiv_id:
        return "preprint"
    return "document"


def _zotero_tags(paper: Paper) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in [*paper.tags, "Lab Paper Intake"]:
        value = _clean_text(tag)
        if value and value.lower() not in seen:
            seen.add(value.lower())
            tags.append(value)
    if paper.summary_status == "llm_generated" and "llm summary" not in seen:
        tags.append("LLM Summary")
    return tags


def _split_author(author: str) -> dict[str, str]:
    if "," in author:
        last, first = [part.strip() for part in author.split(",", 1)]
        if first and last:
            return {"firstName": first, "lastName": last}
    parts = author.split()
    if len(parts) >= 2 and not re.fullmatch(r"[A-Z](?:\.)?", parts[-1]):
        return {"firstName": " ".join(parts[:-1]), "lastName": parts[-1]}
    return {"name": author}


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
    initials = "".join(part[0] for part in parts[:-1])
    return f"{parts[-1]}:{initials}"


def _set_if_value(target: dict[str, Any], key: str, value: Any) -> None:
    if _has_value(value):
        target[key] = value


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if _has_value(value)}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().lower() != "nan"
    return True


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip()
    return "" if value.lower() in {"none", "nan"} else value


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _value_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def normalized_title_for_zotero(title: str | None) -> str:
    return normalize_title(title)
