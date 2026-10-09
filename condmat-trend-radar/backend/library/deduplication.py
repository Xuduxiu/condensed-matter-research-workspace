from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping
from urllib.parse import unquote


def normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized)
    normalized = re.sub(r"^doi:\s*", "", normalized)
    normalized = normalized.strip().strip(".")
    return normalized or None


def parse_arxiv_id(value: Any) -> tuple[str | None, int | None]:
    if not value:
        return None, None
    normalized = unquote(str(value).strip())
    normalized = re.sub(r"^arxiv:\s*", "", normalized, flags=re.I)
    normalized = re.sub(
        r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/",
        "",
        normalized,
        flags=re.I,
    )
    normalized = normalized.split("?", 1)[0].split("#", 1)[0]
    normalized = re.sub(r"\.pdf$", "", normalized, flags=re.I)
    match = re.match(r"^(?P<base>.+?)(?:v(?P<version>\d+))?$", normalized)
    if not match:
        return normalized or None, None
    version = int(match.group("version")) if match.group("version") else None
    return match.group("base") or None, version


def normalize_title(value: Any) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_author(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z]+", text))


def author_names(record: Mapping[str, Any]) -> list[str]:
    values = record.get("authors") or record.get("author_names") or []
    if isinstance(values, str):
        values = [item.strip() for item in re.split(r"[;,]", values) if item.strip()]
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        return []
    output: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            display = (
                value.get("display_name")
                or value.get("name")
                or value.get("author", {}).get("display_name")
            )
        else:
            display = value
        normalized = normalize_author(display)
        if normalized:
            output.append(normalized)
    return output


def publication_year(record: Mapping[str, Any]) -> int | None:
    raw = record.get("year")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    for key in ("publication_date", "submitted_date", "updated_date"):
        value = str(record.get(key) or "")
        if len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return None


@dataclass(frozen=True)
class MatchDecision:
    matched: bool
    auto_merge: bool
    rule: str
    confidence: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "auto_merge": self.auto_merge,
            "rule": self.rule,
            "confidence": self.confidence,
            "reason": self.reason,
        }


NO_MATCH = MatchDecision(False, False, "none", 0.0, "no reliable identity match")


def external_id_conflict(
    incoming: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    incoming_doi = normalize_doi(incoming.get("doi"))
    candidate_doi = normalize_doi(candidate.get("doi"))
    incoming_arxiv, _ = parse_arxiv_id(incoming.get("arxiv_id"))
    candidate_arxiv, _ = parse_arxiv_id(candidate.get("arxiv_id"))
    return bool(
        (incoming_doi and candidate_doi and incoming_doi != candidate_doi)
        or (
            incoming_arxiv
            and candidate_arxiv
            and incoming_arxiv.lower() != candidate_arxiv.lower()
        )
    )


def decide_match(
    incoming: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> MatchDecision:
    incoming_doi = normalize_doi(incoming.get("doi"))
    candidate_doi = normalize_doi(candidate.get("doi"))
    if incoming_doi and candidate_doi and incoming_doi == candidate_doi:
        return MatchDecision(True, True, "doi_exact", 1.0, "normalized DOI is identical")

    incoming_arxiv, _ = parse_arxiv_id(incoming.get("arxiv_id"))
    candidate_arxiv, _ = parse_arxiv_id(candidate.get("arxiv_id"))
    if (
        incoming_arxiv
        and candidate_arxiv
        and incoming_arxiv.lower() == candidate_arxiv.lower()
    ):
        return MatchDecision(True, True, "arxiv_exact", 0.99, "base arXiv id is identical")

    incoming_ref = str(incoming.get("journal_reference") or "").lower()
    candidate_ref = str(candidate.get("journal_reference") or "").lower()
    if candidate_doi and candidate_doi in incoming_ref:
        return MatchDecision(
            True,
            True,
            "arxiv_journal_reference_doi",
            0.98,
            "incoming journal reference explicitly contains candidate DOI",
        )
    if incoming_doi and incoming_doi in candidate_ref:
        return MatchDecision(
            True,
            True,
            "arxiv_journal_reference_doi",
            0.98,
            "candidate journal reference explicitly contains incoming DOI",
        )

    incoming_title = normalize_title(incoming.get("title"))
    candidate_title = normalize_title(candidate.get("title"))
    if not incoming_title or not candidate_title:
        return NO_MATCH
    similarity = SequenceMatcher(None, incoming_title, candidate_title).ratio()
    if similarity < 0.94:
        return NO_MATCH
    if external_id_conflict(incoming, candidate):
        return MatchDecision(
            True,
            False,
            "title_external_id_conflict",
            similarity,
            "similar title but DOI/arXiv identities conflict",
        )

    incoming_authors = author_names(incoming)
    candidate_authors = author_names(candidate)
    first_author_match = bool(
        incoming_authors
        and candidate_authors
        and incoming_authors[0] == candidate_authors[0]
    )
    incoming_year = publication_year(incoming)
    candidate_year = publication_year(candidate)
    year_exact = bool(
        incoming_year is not None
        and candidate_year is not None
        and incoming_year == candidate_year
    )
    author_overlap = 0.0
    if incoming_authors and candidate_authors:
        left, right = set(incoming_authors), set(candidate_authors)
        author_overlap = len(left & right) / max(1, len(left | right))

    if incoming_title == candidate_title and first_author_match and year_exact:
        confidence = min(0.97, 0.90 + similarity * 0.04 + author_overlap * 0.03)
        return MatchDecision(
            True,
            True,
            "title_author_year",
            confidence,
            "exact normalized title with matching first author and year",
        )
    return MatchDecision(
        True,
        False,
        "title_similarity_review",
        similarity,
        "title similarity is insufficient for an automatic merge",
    )


def choose_best_match(
    incoming: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, MatchDecision]:
    ranked: list[tuple[float, bool, Mapping[str, Any], MatchDecision]] = []
    for candidate in candidates:
        decision = decide_match(incoming, candidate)
        if decision.matched:
            ranked.append(
                (decision.confidence, decision.auto_merge, candidate, decision)
            )
    if not ranked:
        return None, NO_MATCH
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, _, candidate, decision = ranked[0]
    return candidate, decision
