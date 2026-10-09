from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from backend.library.repository import stable_id, utc_now


def discover_pdf_paths(roots: Iterable[Path]) -> list[Path]:
    paths: dict[str, Path] = {}
    for root in roots:
        if root.is_file() and root.suffix.lower() == ".pdf":
            paths[str(root.resolve()).casefold()] = root.resolve()
        elif root.is_dir():
            for path in root.rglob("*.pdf"):
                paths[str(path.resolve()).casefold()] = path.resolve()
    return sorted(paths.values(), key=lambda item: str(item).casefold())


def hash_pdf(path: Path) -> tuple[str, int, bool]:
    digest = hashlib.sha256()
    size = 0
    header = b""
    with path.open("rb") as handle:
        header = handle.read(5)
        digest.update(header)
        size += len(header)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, header == b"%PDF-"


def _legacy_downloads(intake_db: Path | None) -> dict[str, dict[str, Any]]:
    if not intake_db or not intake_db.is_file():
        return {}
    uri = f"file:{intake_db.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        output: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM downloaded_pdfs ORDER BY id"):
            value = dict(row)
            local_path = str(row["local_path"] or "")
            output[local_path.casefold()] = value
            output[Path(local_path).name.casefold()] = value
        return output
    finally:
        connection.close()


def _target_for_download(
    connection: sqlite3.Connection,
    download: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not download:
        return None, None
    try:
        row = connection.execute(
            "SELECT canonical_paper_id, paper_version_id FROM legacy_id_aliases "
            "WHERE source_project='legacy_intake' AND legacy_id=?",
            (download.get("paper_id"),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (str(row[0]), str(row[1])) if row else (None, None)


def _extract_text(path: Path) -> tuple[str, int | None, str | None]:
    try:
        import fitz
    except ImportError:
        return "", None, "PyMuPDF is not installed"
    try:
        parts: list[str] = []
        with fitz.open(path) as document:
            page_count = len(document)
            for page in document:
                parts.append(page.get_text())
        return "\n".join(parts), page_count, None
    except Exception as exc:
        return "", None, f"{type(exc).__name__}: {exc}"


def import_existing_pdfs(
    connection: sqlite3.Connection,
    roots: Iterable[Path],
    destination_root: Path,
    *,
    intake_db: Path | None = None,
    dry_run: bool = True,
    extract_text: bool = True,
) -> dict[str, Any]:
    files = discover_pdf_paths(roots)
    downloads = _legacy_downloads(intake_db)
    hashes: dict[str, list[str]] = {}
    invalid: list[str] = []
    plans: list[dict[str, Any]] = []
    for path in files:
        sha256, size, valid = hash_pdf(path)
        hashes.setdefault(sha256, []).append(str(path))
        if not valid:
            invalid.append(str(path))
        download = downloads.get(str(path).casefold()) or downloads.get(path.name.casefold())
        canonical_id, version_id = _target_for_download(connection, download)
        destination = destination_root / sha256[:2] / f"{sha256}.pdf"
        plans.append(
            {
                "source": str(path),
                "destination": str(destination),
                "sha256": sha256,
                "size_bytes": size,
                "valid_pdf_header": valid,
                "legacy_download_id": download.get("id") if download else None,
                "legacy_paper_id": download.get("paper_id") if download else None,
                "canonical_paper_id": canonical_id,
                "paper_version_id": version_id,
                "source_url": download.get("pdf_url") if download else None,
            }
        )
    result = {
        "status": "PASS" if not invalid else "PARTIAL",
        "dry_run": dry_run,
        "files_seen": len(files),
        "unique_hashes": len(hashes),
        "duplicate_copies": len(files) - len(hashes),
        "duplicate_groups": sum(1 for group in hashes.values() if len(group) > 1),
        "invalid_pdf_headers": invalid,
        "linked_to_versions": sum(1 for plan in plans if plan["paper_version_id"]),
        "plans": plans,
    }
    if dry_run:
        return result
    destination_root.mkdir(parents=True, exist_ok=True)
    imported_hashes: set[str] = set()
    text_extracted = 0
    for plan in plans:
        if not plan["valid_pdf_header"]:
            continue
        sha256 = str(plan["sha256"])
        source = Path(plan["source"])
        destination = Path(plan["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(".pdf.part")
            shutil.copy2(source, temporary)
            copied_hash, _, copied_valid = hash_pdf(temporary)
            if copied_hash != sha256 or not copied_valid:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"copied PDF validation failed: {source}")
            temporary.replace(destination)
        file_id = stable_id("file", sha256)
        now = utc_now()
        connection.execute(
            """
            INSERT INTO paper_files
            (id, canonical_paper_id, paper_version_id, absolute_path, sha256,
             file_size, mime_type, source_url, download_source, downloaded_at,
             extraction_status, validation_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'application/pdf', ?, 'legacy_intake', ?,
                    'pending', 'valid_pdf_header', ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
              canonical_paper_id=COALESCE(paper_files.canonical_paper_id, excluded.canonical_paper_id),
              paper_version_id=COALESCE(paper_files.paper_version_id, excluded.paper_version_id),
              source_url=COALESCE(paper_files.source_url, excluded.source_url),
              updated_at=excluded.updated_at
            """,
            (
                file_id,
                plan["canonical_paper_id"],
                plan["paper_version_id"],
                str(destination.resolve()),
                sha256,
                plan["size_bytes"],
                plan["source_url"],
                now,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO paper_file_sources
            (paper_file_id, legacy_path, source_project, source_record_id,
             source_url, observed_at)
            VALUES (?, ?, 'lab_paper_intake', ?, ?, ?)
            """,
            (
                file_id,
                str(source),
                str(plan["legacy_download_id"] or ""),
                plan["source_url"],
                now,
            ),
        )
        if extract_text and sha256 not in imported_hashes:
            text, page_count, error = _extract_text(destination)
            if text:
                text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    INSERT OR REPLACE INTO paper_file_text
                    (paper_file_id, extractor, extractor_version, text_content,
                     text_sha256, page_count, extracted_at, error_message)
                    VALUES (?, 'PyMuPDF', NULL, ?, ?, ?, ?, NULL)
                    """,
                    (file_id, text, text_sha, page_count, now),
                )
                connection.execute(
                    "UPDATE paper_files SET extraction_status='completed', page_count=? WHERE id=?",
                    (page_count, file_id),
                )
                text_extracted += 1
            elif error:
                connection.execute(
                    "UPDATE paper_files SET extraction_status='unavailable' WHERE id=?",
                    (file_id,),
                )
        imported_hashes.add(sha256)
    result["unique_files_imported"] = len(imported_hashes)
    result["text_extracted"] = text_extracted
    return result
