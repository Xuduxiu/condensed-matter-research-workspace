from __future__ import annotations

import re
from pathlib import Path

import httpx

from .config import DB_PATH, PDF_DIR, Settings
from .db import record_pdf_download
from .models import Paper


def download_pdf(
    paper: Paper,
    settings: Settings,
    pdf_dir: Path = PDF_DIR,
    db_path: Path = DB_PATH,
    filename: str | None = None,
    local_pdf_path: str | None = None,
) -> Paper:
    data = paper.model_dump(mode="python")
    if not paper.pdf_url:
        data["pdf_status"] = "metadata_only"
        return Paper(**data)

    pdf_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = filename or _pdf_filename(paper)
    path = pdf_dir / safe_filename
    recorded_local_path = local_pdf_path or str(path)

    try:
        with httpx.stream(
            "GET",
            paper.pdf_url,
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
        ) as response:
            response.raise_for_status()
            with path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        if path.stat().st_size < 128:
            path.unlink(missing_ok=True)
            data["pdf_status"] = "failed"
            data["local_pdf_path"] = None
            record_pdf_download(paper.id, paper.pdf_url, recorded_local_path, "failed", db_path)
        else:
            data["pdf_status"] = "downloaded"
            data["local_pdf_path"] = recorded_local_path
            record_pdf_download(
                paper.id,
                paper.pdf_url,
                recorded_local_path,
                "downloaded",
                db_path,
            )
    except Exception:
        data["pdf_status"] = "failed"
        data["local_pdf_path"] = None
        record_pdf_download(paper.id, paper.pdf_url, recorded_local_path, "failed", db_path)
    return Paper(**data)


def download_pdfs(
    papers: list[Paper],
    settings: Settings,
    pdf_dir: Path = PDF_DIR,
    db_path: Path = DB_PATH,
) -> list[Paper]:
    return [download_pdf(paper, settings, pdf_dir, db_path) for paper in papers]


def extract_pdf_text(path: Path, max_pages: int = 3) -> str:
    try:
        import fitz
    except ImportError:
        return ""

    text_parts: list[str] = []
    with fitz.open(path) as document:
        for page in document[:max_pages]:
            text_parts.append(page.get_text())
    return "\n".join(text_parts)


def _pdf_filename(paper: Paper) -> str:
    if paper.doi:
        stem = paper.doi.replace("/", "_")
    elif paper.arxiv_id:
        stem = paper.arxiv_id.replace("/", "_")
    else:
        stem = paper.normalized_title[:80] or paper.id
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_")
    return f"{stem or paper.id}.pdf"