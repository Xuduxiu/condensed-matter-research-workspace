from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, EXPORT_DIR, Settings
from .db import record_pdf_download, sync_paper_pdf_status, upsert_papers
from .exporters import export_bibtex, export_csv, export_markdown, export_ris
from .exporters.bibtex_exporter import citation_key_for_paper
from .models import Paper
from .pdf_downloader import download_pdf
from .task_events import record_export_lifecycle


@dataclass(frozen=True)
class ExportPackageResult:
    export_id: str
    export_dir: Path
    zip_path: Path
    manifest_path: Path
    csv_path: Path
    bibtex_path: Path
    ris_path: Path
    markdown_path: Path
    papers: list[Paper]
    lifecycle_event_paths: list[Path] = field(default_factory=list)
    lifecycle_event_errors: list[str] = field(default_factory=list)


def export_selected_package(
    papers: list[Paper],
    settings: Settings,
    prompt: str = "",
    language: str = "zh",
    download_pdfs: bool = False,
    exports_dir: Path = EXPORT_DIR,
    db_path: Path = DB_PATH,
) -> ExportPackageResult:
    exports_dir.mkdir(parents=True, exist_ok=True)
    export_id, export_dir = create_export_run_dir(exports_dir)
    pdf_dir = export_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    synced = sync_paper_pdf_status(papers, db_path)
    # The download ledger now has a real foreign key. Persist canonical papers
    # before recording any copied or downloaded PDF, then merge final statuses.
    upsert_papers(synced, db_path)
    prepared = _prepare_pdfs_for_export(
        synced,
        settings=settings,
        export_dir=export_dir,
        pdf_dir=pdf_dir,
        download_missing=download_pdfs,
        db_path=db_path,
    )
    upsert_papers(prepared, db_path)

    csv_path = export_csv(prepared, export_dir / "selected_papers.csv")
    bibtex_path = export_bibtex(prepared, export_dir / "selected_papers.bib")
    ris_path = export_ris(prepared, export_dir / "selected_papers.ris")
    markdown_path = export_markdown(
        prepared,
        export_dir / "selected_summary.md",
        language=language,
    )
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _build_manifest(
                export_id=export_id,
                prompt=prompt,
                language=language,
                papers=prepared,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    zip_path = _zip_export_dir(export_dir)
    lifecycle = record_export_lifecycle(
        prepared,
        export_id=export_id,
        export_path=zip_path,
    )
    lifecycle_summary_path = export_dir / "lifecycle_events.json"
    lifecycle_payload = lifecycle.as_dict()
    lifecycle_summary_path.write_text(
        json.dumps(lifecycle_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["lifecycle_events"] = lifecycle_summary_path.name
    manifest["lifecycle_events"] = {
        **lifecycle_payload,
        "summary_file": lifecycle_summary_path.name,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    zip_path = _zip_export_dir(export_dir)
    _write_latest_pointer(exports_dir, export_id)
    return ExportPackageResult(
        export_id=export_id,
        export_dir=export_dir,
        zip_path=zip_path,
        manifest_path=manifest_path,
        csv_path=csv_path,
        bibtex_path=bibtex_path,
        ris_path=ris_path,
        markdown_path=markdown_path,
        papers=prepared,
        lifecycle_event_paths=lifecycle.event_paths,
        lifecycle_event_errors=lifecycle.errors,
    )


def create_export_run_dir(exports_dir: Path = EXPORT_DIR) -> tuple[str, Path]:
    exports_dir.mkdir(parents=True, exist_ok=True)
    next_index = _next_run_index(exports_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    while True:
        export_id = f"run_{next_index:04d}_{timestamp}"
        export_dir = exports_dir / export_id
        if not export_dir.exists():
            export_dir.mkdir(parents=True)
            return export_id, export_dir
        next_index += 1


def clean_export_runs(exports_dir: Path = EXPORT_DIR) -> list[Path]:
    if not exports_dir.exists():
        return []
    removed: list[Path] = []
    for path in exports_dir.iterdir():
        if path.name == ".gitkeep":
            continue
        if path.is_dir() and path.name.startswith("run_"):
            shutil.rmtree(path)
            removed.append(path)
        elif path.is_file() and path.name.startswith("run_") and path.suffix == ".zip":
            path.unlink()
            removed.append(path)
    return removed


def _prepare_pdfs_for_export(
    papers: list[Paper],
    settings: Settings,
    export_dir: Path,
    pdf_dir: Path,
    download_missing: bool,
    db_path: Path,
) -> list[Paper]:
    prepared: list[Paper] = []
    used_filenames: set[str] = set()
    fingerprint_to_path: dict[str, str] = {}

    for index, paper in enumerate(papers, start=1):
        fingerprint = _paper_fingerprint(paper)
        if fingerprint in fingerprint_to_path:
            relative_path = fingerprint_to_path[fingerprint]
            prepared.append(
                paper.model_copy(
                    update={
                        "pdf_status": "downloaded",
                        "local_pdf_path": relative_path,
                    }
                )
            )
            continue

        filename = safe_pdf_filename(index, paper, used_filenames)
        relative_path = f"pdfs/{filename}"
        destination = pdf_dir / filename
        existing_path = _existing_local_pdf_path(paper, export_dir)

        if existing_path and existing_path.exists():
            shutil.copyfile(existing_path, destination)
            updated = paper.model_copy(
                update={"pdf_status": "downloaded", "local_pdf_path": relative_path}
            )
            record_pdf_download(
                paper.id,
                paper.pdf_url or "",
                relative_path,
                "downloaded",
                db_path,
            )
        elif download_missing and paper.pdf_url:
            updated = download_pdf(
                paper,
                settings,
                pdf_dir=pdf_dir,
                db_path=db_path,
                filename=filename,
                local_pdf_path=relative_path,
            )
        else:
            updated = paper

        if updated.pdf_status == "downloaded" and updated.local_pdf_path:
            fingerprint_to_path[fingerprint] = updated.local_pdf_path
        prepared.append(updated)
    return prepared


def safe_pdf_filename(index: int, paper: Paper, used_filenames: set[str] | None = None) -> str:
    used = used_filenames if used_filenames is not None else set()
    first_author = _safe_part(_first_author_slug(paper)) or "unknown"
    year = str(paper.year or "nd")
    title = _safe_part(_short_title_slug(paper.title)) or "paper"
    stem = f"{index:02d}_{first_author}_{year}_{title}"
    stem = stem[:116].rstrip("._-")
    filename = f"{stem}.pdf"
    suffix = 2
    while filename.lower() in used:
        suffix_text = f"_{suffix}"
        filename = f"{stem[:116 - len(suffix_text)]}{suffix_text}.pdf"
        suffix += 1
    used.add(filename.lower())
    return filename


def _first_author_slug(paper: Paper) -> str:
    if not paper.authors:
        return "unknown"
    author = paper.authors[0]
    if "," in author:
        author = author.split(",", 1)[0]
    else:
        parts = author.split()
        author = parts[-1] if parts else author
    return author


def _short_title_slug(title: str) -> str:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", title.lower()) if len(word) > 2]
    return "_".join(words[:5]) or "paper"


def _safe_part(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned.lower()


def _existing_local_pdf_path(paper: Paper, export_dir: Path) -> Path | None:
    if not paper.local_pdf_path:
        return None
    path = Path(paper.local_pdf_path)
    if path.is_absolute():
        return path if path.exists() else None

    candidate_paths = [
        export_dir / path,
        export_dir.parent.parent / path,
    ]
    if path.parts and path.parts[0].lower() == "pdfs":
        candidate_paths.extend(
            sorted(
                export_dir.parent.glob(f"run_*/{path.as_posix()}"),
                key=lambda candidate: candidate.stat().st_mtime,
                reverse=True,
            )
        )
    for candidate in candidate_paths:
        if candidate.exists():
            return candidate
    return None


def _paper_fingerprint(paper: Paper) -> str:
    if paper.doi:
        return f"doi:{paper.doi}"
    if paper.arxiv_id:
        return f"arxiv:{paper.arxiv_id}"
    return f"title:{paper.normalized_title or paper.title.lower()}"


def _build_manifest(
    export_id: str,
    prompt: str,
    language: str,
    papers: list[Paper],
) -> dict[str, object]:
    used_keys: set[str] = set()
    paper_entries = []
    for index, paper in enumerate(papers, start=1):
        paper_entries.append(
            {
                "index": index,
                "title": paper.title,
                "doi": paper.doi,
                "arxiv_id": paper.arxiv_id,
                "citation_key": citation_key_for_paper(paper, used_keys),
                "pdf_status": paper.pdf_status,
                "pdf_file": paper.local_pdf_path if paper.pdf_status == "downloaded" else None,
                "source": paper.source,
            }
        )
    return {
        "export_id": export_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt": prompt,
        "language": language,
        "selected_count": len(papers),
        "files": {
            "csv": "selected_papers.csv",
            "bibtex": "selected_papers.bib",
            "ris": "selected_papers.ris",
            "markdown": "selected_summary.md",
            "pdf_dir": "pdfs/",
        },
        "zotero_import": {
            "mode": "ris_manual",
            "ris_file": "selected_papers.ris",
            "summary_file": "selected_summary.md",
            "pdf_dir": "pdfs/",
            "reason": (
                "Zotero Local API /api endpoints are used only for connection checks "
                "in v0.3. Full write import is deferred to a future Web API or "
                "Zotero plugin integration."
            ),
        },
        "papers": paper_entries,
    }


def _zip_export_dir(export_dir: Path) -> Path:
    zip_path = export_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in export_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(export_dir).as_posix())
    return zip_path


def _write_latest_pointer(exports_dir: Path, export_id: str) -> None:
    (exports_dir / "latest.txt").write_text(export_id + "\n", encoding="utf-8")


def _next_run_index(exports_dir: Path) -> int:
    max_index = 0
    for path in exports_dir.iterdir():
        match = re.match(r"run_(\d{4})_", path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return max_index + 1