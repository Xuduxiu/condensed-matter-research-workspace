from __future__ import annotations

from .dictionaries import COND_MAT_KEYWORDS, EXCLUDE_TERMS, HARD_KEEP_TERMS
from .normalize import lookup_key


COND_MAT_ARXIV_PREFIXES = (
    "cond-mat.",
    "physics.app-ph",
)


def is_condensed_matter_record(record: dict) -> tuple[bool, list[str]]:
    """Return whether a metadata record should enter the local corpus."""
    reasons: list[str] = []
    text_parts = [
        record.get("title", ""),
        record.get("abstract", ""),
        record.get("journal", ""),
        record.get("source_name", ""),
        " ".join(record.get("concepts", []) or []),
        " ".join(record.get("categories", []) or []),
        " ".join(record.get("subjects", []) or []),
    ]
    text = lookup_key(" ".join(str(part) for part in text_parts))
    categories = [str(cat) for cat in record.get("categories", []) or []]

    if any(cat.startswith(COND_MAT_ARXIV_PREFIXES) for cat in categories):
        reasons.append("arxiv-cond-mat")
        return True, reasons

    for term in HARD_KEEP_TERMS:
        if lookup_key(term) in text:
            reasons.append(f"hard:{term}")
            return True, reasons

    keep = False
    for term in COND_MAT_KEYWORDS:
        if lookup_key(term) in text:
            reasons.append(f"keyword:{term}")
            keep = True
            break

    if keep:
        return True, reasons

    for term in EXCLUDE_TERMS:
        if lookup_key(term) in text:
            reasons.append(f"exclude:{term}")
            return False, reasons

    return False, reasons
