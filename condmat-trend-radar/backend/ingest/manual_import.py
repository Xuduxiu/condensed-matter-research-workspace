from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def load_manual_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("papers", [])
        return [normalize_manual_record(item) for item in payload]
    if path.suffix.lower() == ".csv":
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return [normalize_manual_record(row) for row in csv.DictReader(handle)]
    raise ValueError(f"Unsupported manual import file: {path}")


def normalize_manual_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id") or row.get("paper_id"),
        "doi": row.get("doi") or row.get("DOI"),
        "title": row.get("title") or row.get("Title") or "",
        "abstract": row.get("abstract") or row.get("Abstract") or "",
        "publication_date": row.get("publication_date") or row.get("date") or row.get("published") or "",
        "journal": row.get("journal") or row.get("source") or "",
        "source": "manual",
        "url": row.get("url") or row.get("URL") or "",
        "cited_by_count": int(row.get("cited_by_count") or row.get("citations") or 0),
        "raw_json": row,
    }
