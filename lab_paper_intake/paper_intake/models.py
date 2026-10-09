from __future__ import annotations

import math
import re
import unicodedata
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    value = unicodedata.normalize("NFKD", title)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(the|a|an|and|or|of|for|in|on|to|with|by)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class ChineseSummary(BaseModel):
    problem: str = ""
    method: str = ""
    key_results: str = ""
    relation_to_lab: str = ""
    reading_priority: Literal["High", "Medium", "Low"] = "Medium"

    def has_content(self) -> bool:
        return any(
            part.strip()
            for part in [
                self.problem,
                self.method,
                self.key_results,
                self.relation_to_lab,
            ]
        )


class Paper(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    normalized_title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    abstract: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    local_pdf_path: str | None = None
    pdf_status: Literal["downloaded", "oa_available", "metadata_only", "failed"] = (
        "metadata_only"
    )
    source: str = ""
    citation_count: int | None = None
    relevance_score: float | None = None
    relevance_reason: str | None = None
    tags: list[str] = Field(default_factory=list)
    chinese_summary: ChineseSummary = Field(default_factory=ChineseSummary)
    selected: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, **data: Any) -> None:
        if not data.get("normalized_title") and data.get("title"):
            data["normalized_title"] = normalize_title(str(data["title"]))
        if data.get("doi"):
            data["doi"] = normalize_doi(str(data["doi"]))
        if data.get("arxiv_id"):
            data["arxiv_id"] = normalize_arxiv_id(str(data["arxiv_id"]))
        if data.get("local_pdf_path") == "":
            data["local_pdf_path"] = None
        super().__init__(**data)

    @property
    def summary_status(self) -> str:
        return "llm_generated" if self.chinese_summary.has_content() else "no_llm"

    def export_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["authors"] = "; ".join(self.authors)
        data["tags"] = "; ".join(self.tags)
        data["chinese_summary"] = " | ".join(
            part
            for part in [
                self.chinese_summary.problem,
                self.chinese_summary.method,
                self.chinese_summary.key_results,
                self.chinese_summary.relation_to_lab,
                self.chinese_summary.reading_priority,
            ]
            if part
        )
        data["summary_status"] = self.summary_status
        data.pop("raw", None)
        return _clean_export_values(data)


class SearchPlan(BaseModel):
    main_topics: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    exclude_terms: list[str] = Field(default_factory=list)


class SummaryResult(BaseModel):
    relevance_score: float = 0.0
    relevance_reason: str = ""
    tags: list[str] = Field(default_factory=list)
    chinese_summary: ChineseSummary = Field(default_factory=ChineseSummary)


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    value = doi.strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    value = value.strip().strip(".")
    return value.lower() or None


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    if not arxiv_id:
        return None
    value = arxiv_id.strip()
    value = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", value, flags=re.I)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = value.split("v")[0] if re.search(r"v\d+$", value) else value
    return value or None


def _clean_export_values(data: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, float) and math.isnan(value):
            cleaned[key] = ""
        elif value is None:
            cleaned[key] = ""
        else:
            cleaned[key] = value
    return cleaned