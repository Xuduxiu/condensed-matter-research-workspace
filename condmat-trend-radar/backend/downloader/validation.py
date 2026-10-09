from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PdfValidation:
    valid: bool
    failure_class: str | None
    message: str
    file_size: int
    page_count: int | None
    pdf_version: str | None
    encrypted: bool
    parser: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _invalid(
    failure_class: str,
    message: str,
    *,
    file_size: int,
    pdf_version: str | None = None,
    encrypted: bool = False,
    parser: str = "basic",
) -> PdfValidation:
    return PdfValidation(
        False,
        failure_class,
        message,
        file_size,
        None,
        pdf_version,
        encrypted,
        parser,
    )


def validate_pdf_file(path: Path, *, minimum_bytes: int = 256) -> PdfValidation:
    """Validate that a response is a readable, non-encrypted PDF document.

    The header may legally occur within the first 1024 bytes.  PyMuPDF then
    parses the cross-reference structure and opens the first page, preventing
    HTML error pages or a truncated file from being accepted merely because
    they start with ``%PDF-``.
    """
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return _invalid("file_io_error", str(exc), file_size=0)
    if file_size < minimum_bytes:
        return _invalid(
            "pdf_too_small",
            f"PDF is only {file_size} bytes",
            file_size=file_size,
        )
    try:
        with path.open("rb") as handle:
            prefix = handle.read(1024)
            handle.seek(max(0, file_size - 4096))
            suffix = handle.read()
    except OSError as exc:
        return _invalid("file_io_error", str(exc), file_size=file_size)

    marker = prefix.find(b"%PDF-")
    if marker < 0:
        return _invalid(
            "not_pdf_content",
            "PDF signature was not found in the first 1024 bytes",
            file_size=file_size,
        )
    version = prefix[marker + 5 : marker + 8].decode("ascii", errors="replace") or None
    if b"%%EOF" not in suffix:
        return _invalid(
            "truncated_pdf",
            "PDF end-of-file marker is missing",
            file_size=file_size,
            pdf_version=version,
        )

    try:
        import fitz  # PyMuPDF
    except ImportError:
        # The runtime declares PyMuPDF.  Retain a conservative structural
        # fallback so a minimal source checkout can still operate.
        structural = b"xref" in suffix or b"/XRef" in suffix or b"startxref" in suffix
        if not structural:
            return _invalid(
                "invalid_pdf_structure",
                "PDF cross-reference structure was not found",
                file_size=file_size,
                pdf_version=version,
            )
        return PdfValidation(True, None, "basic PDF structure is valid", file_size, None, version, False, "basic")

    try:
        with fitz.open(path) as document:
            encrypted = bool(document.needs_pass)
            if encrypted:
                return _invalid(
                    "encrypted_pdf",
                    "PDF requires a password",
                    file_size=file_size,
                    pdf_version=version,
                    encrypted=True,
                    parser="PyMuPDF",
                )
            page_count = int(document.page_count)
            if page_count < 1:
                return _invalid(
                    "empty_pdf",
                    "PDF contains no pages",
                    file_size=file_size,
                    pdf_version=version,
                    parser="PyMuPDF",
                )
            document.load_page(0)
    except Exception as exc:
        return _invalid(
            "invalid_pdf_structure",
            f"PyMuPDF could not parse the document: {type(exc).__name__}: {exc}",
            file_size=file_size,
            pdf_version=version,
            parser="PyMuPDF",
        )
    return PdfValidation(
        True,
        None,
        "PDF parsed successfully",
        file_size,
        page_count,
        version,
        False,
        "PyMuPDF",
    )
