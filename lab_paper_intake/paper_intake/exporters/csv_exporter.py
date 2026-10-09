from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..models import Paper


CSV_COLUMNS = [
    "id",
    "title",
    "normalized_title",
    "authors",
    "year",
    "journal",
    "doi",
    "arxiv_id",
    "abstract",
    "url",
    "pdf_url",
    "local_pdf_path",
    "pdf_status",
    "source",
    "citation_count",
    "relevance_score",
    "relevance_reason",
    "tags",
    "chinese_summary",
    "summary_status",
    "selected",
]


def export_csv(papers: list[Paper], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [paper.export_dict() for paper in papers]
    frame = pd.DataFrame(rows)
    for column in CSV_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    frame = frame[CSV_COLUMNS]
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path