from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.downloader.audit import ensure_download_audit_schema
from backend.downloader.validation import validate_pdf_file
from backend.library.repository import utc_now


VALID_FILE_STATUSES = ("ok", "valid", "valid_pdf_header", "valid_pdf_structural")


def _clean(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _attachment_reference(record: dict[str, Any]) -> str:
    packaged = str(record.get("zotero_attachment_path") or "").strip()
    if packaged:
        return packaged.replace("\\", "/")
    value = record.get("local_pdf_path")
    if not value:
        return ""
    path = Path(str(value)).expanduser().resolve()
    try:
        return path.as_uri()
    except ValueError:
        return str(path)


def _safe_filename(value: str, fallback: str = "paper") -> str:
    clean = re.sub(r"[^\w\-. ]+", "_", value, flags=re.UNICODE).strip(" ._")
    return (clean[:100] or fallback).strip()


def _authors(connection: sqlite3.Connection, version_id: str) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT COALESCE(NULLIF(pa.raw_name, ''), a.display_name)
            FROM paper_authors pa JOIN authors a ON a.id=pa.author_id
            WHERE pa.paper_version_id=? ORDER BY pa.author_position
            """,
            (version_id,),
        )
    ]


def _best_valid_pdf(
    connection: sqlite3.Connection,
    canonical_paper_id: str,
    version_id: str,
) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in VALID_FILE_STATUSES)
    rows = connection.execute(
        f"""
        SELECT DISTINCT f.* FROM paper_files f
        WHERE f.validation_status IN ({placeholders})
          AND (
            f.paper_version_id=? OR f.canonical_paper_id=? OR EXISTS (
              SELECT 1 FROM paper_file_links l
              WHERE l.paper_file_id=f.id AND l.canonical_paper_id=?
                AND (l.paper_version_id='' OR l.paper_version_id=?)
            )
          )
        ORDER BY CASE WHEN f.paper_version_id=? THEN 0 ELSE 1 END,
                 f.downloaded_at DESC, f.created_at DESC
        """,
        (*VALID_FILE_STATUSES, version_id, canonical_paper_id, canonical_paper_id, version_id, version_id),
    ).fetchall()
    for row in rows:
        item = dict(row)
        path = Path(str(item.get("absolute_path") or ""))
        if not path.is_file():
            continue
        validation = validate_pdf_file(path)
        if not validation.valid:
            connection.execute(
                "UPDATE paper_files SET validation_status='invalid', updated_at=? WHERE id=?",
                (utc_now(), item["id"]),
            )
            continue
        connection.execute(
            "UPDATE paper_files SET validation_status='valid', page_count=?, updated_at=? WHERE id=?",
            (validation.page_count, utc_now(), item["id"]),
        )
        item["validation"] = validation.as_dict()
        return item
    return None


def selected_records(connection: sqlite3.Connection, version_ids: Iterable[str]) -> list[dict[str, Any]]:
    ensure_download_audit_schema(connection)
    records: list[dict[str, Any]] = []
    for version_id in dict.fromkeys(str(item) for item in version_ids if str(item)):
        row = connection.execute(
            """
            SELECT v.*, p.year
            FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id
            WHERE v.id=?
            """,
            (version_id,),
        ).fetchone()
        if row:
            record = dict(row)
            record["authors"] = _authors(connection, version_id)
            paper_file = _best_valid_pdf(connection, str(record["canonical_paper_id"]), version_id)
            record["local_pdf_path"] = paper_file.get("absolute_path") if paper_file else None
            record["paper_file_id"] = paper_file.get("id") if paper_file else None
            record["pdf_sha256"] = paper_file.get("sha256") if paper_file else None
            record["pdf_file_size"] = int(paper_file.get("file_size") or 0) if paper_file else 0
            record["pdf_access_basis"] = paper_file.get("access_basis") if paper_file else None
            records.append(record)
    return records


def ris_text(records: Iterable[dict[str, Any]]) -> str:
    lines: list[str] = []
    for record in records:
        lines.extend(["TY  - JOUR", f"TI  - {_clean(record.get('title'))}"])
        for author in record.get("authors") or []:
            lines.append(f"AU  - {_clean(author)}")
        if record.get("journal"):
            lines.append(f"JO  - {_clean(record['journal'])}")
        if record.get("year"):
            lines.append(f"PY  - {record['year']}")
        if record.get("doi"):
            lines.append(f"DO  - {_clean(record['doi'])}")
        if record.get("url"):
            lines.append(f"UR  - {_clean(record['url'])}")
        if record.get("abstract"):
            lines.append(f"AB  - {_clean(record['abstract'])}")
        attachment = _attachment_reference(record)
        if attachment:
            lines.append(f"L1  - {attachment}")
        lines.extend(["ER  -", ""])
    return "\r\n".join(lines)


def bibtex_text(records: Iterable[dict[str, Any]]) -> str:
    entries: list[str] = []
    for index, record in enumerate(records, 1):
        first = re.sub(r"\W+", "", (record.get("authors") or ["paper"])[0].split()[-1])
        key = f"{first or 'paper'}{record.get('year') or ''}_{index}"
        fields = {
            "title": record.get("title"),
            "author": " and ".join(record.get("authors") or []),
            "journal": record.get("journal"),
            "year": record.get("year"),
            "doi": record.get("doi"),
            "url": record.get("url"),
            "file": _attachment_reference(record),
        }
        body = ",\n".join(
            f"  {name} = {{{_clean(value)}}}" for name, value in fields.items() if value
        )
        entries.append(f"@article{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def export_citation_package(
    connection: sqlite3.Connection,
    version_ids: Iterable[str],
    output_root: Path,
) -> dict[str, Any]:
    records = selected_records(connection, version_ids)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = output_root / f"zotero_import_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    pdf_dir = output / "pdf"
    ris_path = output / "selected_papers.ris"
    bib_path = output / "selected_papers.bib"
    csv_path = output / "selected_papers.csv"
    md_path = output / "selected_summary.md"
    readme_path = output / "README_IMPORT.txt"
    zip_path = output / "zotero_package.zip"

    attachments: list[dict[str, Any]] = []
    export_records: list[dict[str, Any]] = []
    for index, source_record in enumerate(records, 1):
        record = dict(source_record)
        local = Path(str(record.get("local_pdf_path") or ""))
        if local.is_file():
            pdf_dir.mkdir(parents=True, exist_ok=True)
            archive_path = f"pdf/{index:03d}_{_safe_filename(str(record.get('title') or 'paper'))}.pdf"
            staged_path = output / Path(archive_path)
            shutil.copy2(local, staged_path)
            staged_validation = validate_pdf_file(staged_path)
            if staged_validation.valid:
                record["zotero_attachment_path"] = archive_path
                attachments.append(
                    {
                        "paper_version_id": record["id"],
                        "paper_file_id": record.get("paper_file_id"),
                        "archive_path": archive_path,
                        "sha256": record.get("pdf_sha256"),
                        "file_size": staged_path.stat().st_size,
                        "page_count": staged_validation.page_count,
                        "validation": "valid",
                        "access_basis": record.get("pdf_access_basis") or "open_access",
                        "redistribution_allowed": (record.get("pdf_access_basis") or "open_access") == "open_access",
                    }
                )
            else:
                staged_path.unlink(missing_ok=True)
        export_records.append(record)

    ris_path.write_bytes(ris_text(export_records).encode("utf-8"))
    bib_path.write_text(bibtex_text(export_records), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "paper_version_id",
            "title",
            "authors",
            "year",
            "journal",
            "doi",
            "url",
            "local_pdf_path",
            "packaged_pdf_path",
            "pdf_access_basis",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in export_records:
            writer.writerow(
                {
                    "paper_version_id": record["id"],
                    "title": record.get("title"),
                    "authors": "; ".join(record.get("authors") or []),
                    "year": record.get("year"),
                    "journal": record.get("journal"),
                    "doi": record.get("doi"),
                    "url": record.get("url"),
                    "local_pdf_path": record.get("local_pdf_path"),
                    "packaged_pdf_path": record.get("zotero_attachment_path"),
                    "pdf_access_basis": record.get("pdf_access_basis"),
                }
            )
    md_path.write_text(
        "# Selected papers\n\n" + "\n".join(f"- {record.get('title')}" for record in export_records),
        encoding="utf-8",
    )
    readme_path.write_text(
        "Zotero 导入说明\r\n"
        "1. 必须先完整解压 zotero_package.zip。\r\n"
        "2. 保持 selected_papers.ris 与 pdf 文件夹的相对位置不变。\r\n"
        "3. 在 Zotero 中选择 文件 -> 导入，打开 selected_papers.ris。\r\n"
        "4. RIS 的 L1 字段使用相对路径，Zotero 会把对应 PDF 作为附件导入。\r\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 3,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paper_count": len(export_records),
        "attachment_count": len(attachments),
        "missing_attachment_count": len(export_records) - len(attachments),
        "institutional_access_notice": (
            "institutional_ip attachments are for the authorized user's personal research use; do not redistribute"),
        "zotero_import": {
            "mode": "ris_with_packaged_relative_pdf",
            "ris_file": ris_path.name,
            "pdf_link_field": "L1",
            "attachment_path_base": "directory containing selected_papers.ris",
            "requires_zip_extraction": True,
            "reason": (
                "Every successful structurally valid PDF is copied into pdf/ "
                "and linked by a portable relative RIS path"
            ),
        },
        "attachments": attachments,
        "files": [
            ris_path.name,
            bib_path.name,
            csv_path.name,
            md_path.name,
            readme_path.name,
            zip_path.name,
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (ris_path, bib_path, csv_path, md_path, readme_path, manifest_path):
            archive.write(path, path.name)
        for attachment in attachments:
            staged_path = output / Path(str(attachment["archive_path"]))
            archive.write(staged_path, str(attachment["archive_path"]))
    with zipfile.ZipFile(zip_path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise OSError(f"Zotero package ZIP validation failed at {bad_member}")
    connection.commit()
    return {
        "package_id": output.name,
        "output_dir": str(output),
        "paper_count": len(export_records),
        "attachment_count": len(attachments),
        "missing_attachment_count": len(export_records) - len(attachments),
        "ris_path": str(ris_path),
        "zip_path": str(zip_path),
        "manifest_path": str(manifest_path),
        "ris_filename": ris_path.name,
        "zip_filename": zip_path.name,
        "zotero_mode": "ris_with_packaged_relative_pdf",
    }
