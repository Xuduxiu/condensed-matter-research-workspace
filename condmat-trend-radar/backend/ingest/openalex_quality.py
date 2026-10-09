from __future__ import annotations

import json
import sqlite3
import urllib.parse
from typing import Any, Iterable, Mapping

from backend.library.deduplication import normalize_doi


FORMAL_SOURCE_TYPES = {"journal", "conference", "book series"}
FORMAL_CROSSREF_TYPES = {
    "journal-article",
    "proceedings-article",
    "book-chapter",
    "monograph",
    "peer-review",
}
TRUSTED_PREPRINT_SOURCE_NAMES = {"arxiv"}
TRUSTED_PREPRINT_HOSTS = {"arxiv.org", "export.arxiv.org"}
GENERIC_REPOSITORY_NAMES = {
    "zenodo",
    "figshare",
    "osf preprints",
    "open science framework",
    "researchgate",
}
GENERIC_REPOSITORY_HOSTS = {
    "zenodo.org",
    "figshare.com",
    "osf.io",
    "researchgate.net",
}
REPOSITORY_DOI_PREFIXES = (
    "10.5281/zenodo.",
    "10.6084/m9.figshare.",
    "10.17605/osf.io/",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _raw_record(record: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = record.get("raw_json")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return record


def _host(value: Any) -> str:
    try:
        return (urllib.parse.urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, candidates: set[str]) -> bool:
    return any(host == candidate or host.endswith("." + candidate) for candidate in candidates)


def _source_name(source: Mapping[str, Any]) -> str:
    return str(source.get("display_name") or source.get("name") or "").strip()


def _source_type(source: Mapping[str, Any]) -> str:
    return str(source.get("type") or "").strip().lower()


def _location_source(location: Any) -> Mapping[str, Any]:
    return _mapping(_mapping(location).get("source"))


def _location_hosts(location: Any) -> set[str]:
    item = _mapping(location)
    return {
        host
        for host in (
            _host(item.get("landing_page_url")),
            _host(item.get("pdf_url")),
        )
        if host
    }


def _is_trusted_preprint(source: Mapping[str, Any], location: Any) -> bool:
    name = _source_name(source).casefold()
    hosts = _location_hosts(location)
    return name in TRUSTED_PREPRINT_SOURCE_NAMES or any(
        _host_matches(host, TRUSTED_PREPRINT_HOSTS) for host in hosts
    )


def _is_generic_repository(source: Mapping[str, Any], location: Any) -> bool:
    source_type = _source_type(source)
    name = _source_name(source).casefold()
    hosts = _location_hosts(location)
    return (
        source_type == "repository"
        or name in GENERIC_REPOSITORY_NAMES
        or any(_host_matches(host, GENERIC_REPOSITORY_HOSTS) for host in hosts)
    )


def openalex_source_quality(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return an auditable venue-quality gate for one OpenAlex work.

    This is deliberately separate from the condensed-matter topic gate.  A
    generic repository copy remains useful for discovery and downloading, but
    does not become evidence for a field-wide hotspot merely because OpenAlex
    assigned it a high topic score.
    """

    raw = _raw_record(record)
    primary_location = _mapping(raw.get("primary_location")) or _mapping(record.get("primary_location"))
    primary_source = _location_source(primary_location)
    if not primary_source:
        primary_source = {
            "id": record.get("primary_source_id"),
            "display_name": record.get("primary_source_name") or record.get("journal"),
            "type": record.get("primary_source_type"),
        }
    locations = raw.get("locations") if isinstance(raw.get("locations"), list) else record.get("locations")
    location_items = list(locations) if isinstance(locations, list) else []
    if primary_location and primary_location not in location_items:
        location_items.insert(0, primary_location)

    primary_name = _source_name(primary_source)
    primary_type = _source_type(primary_source)
    primary_id = str(primary_source.get("id") or "").strip()
    trusted_preprint = _is_trusted_preprint(primary_source, primary_location)
    repository_primary = _is_generic_repository(primary_source, primary_location)

    scholarly_locations: list[dict[str, str]] = []
    for location in location_items:
        source = _location_source(location)
        source_type = _source_type(source)
        if source_type not in FORMAL_SOURCE_TYPES:
            continue
        scholarly_locations.append(
            {
                "id": str(source.get("id") or ""),
                "name": _source_name(source),
                "type": source_type,
            }
        )

    doi = normalize_doi(record.get("doi") or raw.get("doi")) or ""
    type_crossref = str(
        record.get("openalex_type_crossref")
        or record.get("type_crossref")
        or raw.get("type_crossref")
        or ""
    ).strip().lower()
    repository_doi = any(doi.startswith(prefix) for prefix in REPOSITORY_DOI_PREFIXES)
    formal_crossref_doi = bool(
        doi and not repository_doi and type_crossref in FORMAL_CROSSREF_TYPES
    )
    formal_venue_evidence = bool(scholarly_locations or formal_crossref_doi)

    excluded = bool(repository_primary and not trusted_preprint and not formal_venue_evidence)
    if excluded:
        decision = "exclude_generic_repository_without_formal_version"
    elif trusted_preprint:
        decision = "allow_trusted_preprint"
    elif formal_venue_evidence:
        decision = "allow_formal_scholarly_venue"
    elif primary_type in FORMAL_SOURCE_TYPES:
        decision = "allow_primary_scholarly_venue"
    else:
        # Missing provider source metadata is not treated as proof of low
        # quality.  The scientific text/topic gate remains authoritative.
        decision = "allow_no_explicit_repository_evidence"

    return {
        "primary_source_id": primary_id,
        "primary_source_name": primary_name,
        "primary_source_type": primary_type or "unknown",
        "repository_primary": repository_primary,
        "trusted_preprint": trusted_preprint,
        "type_crossref": type_crossref,
        "doi": doi,
        "repository_doi": repository_doi,
        "formal_crossref_doi": formal_crossref_doi,
        "formal_scholarly_locations": scholarly_locations,
        "formal_venue_evidence": formal_venue_evidence,
        "eligible_for_hotspot_source_gate": not excluded,
        "decision": decision,
        "policy": "repository_requires_arxiv_or_formal_scholarly_version_v1",
    }


def source_quality_reason(evidence: Mapping[str, Any]) -> str:
    source_type = str(evidence.get("primary_source_type") or "unknown")
    source_name = str(evidence.get("primary_source_name") or "unknown").strip().casefold()
    source_name = "-".join(source_name.split())[:80] or "unknown"
    if not evidence.get("eligible_for_hotspot_source_gate"):
        return f"openalex-source-excluded:{source_type}:{source_name}:no-formal-version"
    return f"openalex-source-accepted:{source_type}:{source_name}"


def _version_quality(version: Mapping[str, Any]) -> tuple[bool, str, bool]:
    source = str(version.get("source") or "").strip().lower()
    arxiv_id = str(version.get("arxiv_id") or "").strip()
    if source == "arxiv" or arxiv_id:
        return True, "arxiv-version", False
    if source == "crossref":
        raw = _raw_record(version)
        crossref_type = str(raw.get("type") or "").strip().lower()
        journal = str(version.get("journal") or "").strip().casefold()
        formal_crossref = bool(
            normalize_doi(version.get("doi"))
            and crossref_type in FORMAL_CROSSREF_TYPES
            and journal
            and journal not in GENERIC_REPOSITORY_NAMES
        )
        return (
            formal_crossref,
            "crossref-formal-version" if formal_crossref else "crossref-non-scholarly-record",
            False,
        )
    if source != "openalex":
        return False, "non-qualifying-version", False
    evidence = openalex_source_quality(version)
    is_repository = bool(evidence["repository_primary"])
    if evidence["trusted_preprint"]:
        return True, "openalex-trusted-preprint", is_repository
    if evidence["formal_venue_evidence"] or (
        evidence["primary_source_type"] in FORMAL_SOURCE_TYPES
    ):
        return True, "openalex-formal-venue", is_repository
    return False, str(evidence["decision"]), is_repository


def reclassify_openalex_repository_quality(
    conn: sqlite3.Connection,
    *,
    canonical_ids: Iterable[str] | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Idempotently remove repository-only canonicals from hotspot statistics.

    Papers and paper_versions are never deleted.  A canonical is downgraded
    only when it has an explicit generic OpenAlex repository observation and
    no arXiv, Crossref-published, journal, or proceedings evidence.
    """

    subset_requested = canonical_ids is not None
    identities = list(
        dict.fromkeys(
            canonical_id
            for item in (canonical_ids if canonical_ids is not None else ())
            if (canonical_id := str(item or "").strip())
        )
    )
    base_result: dict[str, Any] = {
        "processed_canonicals": 0,
        "repository_candidates": 0,
        "downgraded": 0,
        "already_excluded": 0,
        "preserved_with_scholarly_version": 0,
        "retained_source_versions": 0,
        "dry_run": bool(dry_run),
        "selection_mode": "subset" if subset_requested else "full_corpus",
        "subset_size": len(identities) if subset_requested else None,
        "policy": "repository_requires_arxiv_or_formal_scholarly_version_v1",
    }
    if subset_requested and not identities:
        return {
            **base_result,
            "skipped": True,
            "reason": "empty_canonical_subset",
        }
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_versions'"
    ).fetchone() is None:
        return {**base_result, "skipped": True, "reason": "paper_versions_table_missing"}

    fetch_size = max(1, int(batch_size or 500))
    parameters: tuple[Any, ...] = tuple(identities)
    placeholders = ",".join("?" for _ in identities)
    count_scope = (
        f" AND v.canonical_paper_id IN ({placeholders})" if identities else ""
    )
    candidate_scope = (
        f" AND repository.canonical_paper_id IN ({placeholders})" if identities else ""
    )
    count_cursor: sqlite3.Cursor | None = None
    version_cursor: sqlite3.Cursor | None = None
    function_name = f"_openalex_repository_marker_{id(conn):x}"

    def rows(cursor: sqlite3.Cursor) -> Iterable[dict[str, Any]]:
        columns = [str(item[0]) for item in cursor.description or []]
        while True:
            batch = cursor.fetchmany(fetch_size)
            if not batch:
                return
            for row in batch:
                yield dict(row) if isinstance(row, Mapping) else dict(zip(columns, row))

    def repository_marker(
        raw_json: Any,
        doi: Any,
        arxiv_id: Any,
        journal: Any,
        url: Any,
        pdf_url: Any,
    ) -> int:
        item = {
            "source": "openalex",
            "raw_json": raw_json,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
        }
        return int(_version_quality(item)[2])

    # The deterministic marker lets SQLite materialize only canonical IDs for
    # exact repository candidates. Raw provider JSON never accumulates in
    # Python, and SQLite may spill the compact ID set to its temp store.
    conn.create_function(function_name, 6, repository_marker, deterministic=True)
    result = base_result
    try:
        # Preserve the historical processed_canonicals meaning without a
        # Python set or SQLite COUNT(DISTINCT) temp B-tree.
        count_cursor = conn.execute(
            f"""
            SELECT v.canonical_paper_id
            FROM paper_versions AS v INDEXED BY idx_paper_versions_canonical
            WHERE v.source IN ('openalex','crossref','arxiv'){count_scope}
            ORDER BY v.canonical_paper_id
            """,
            parameters,
        )
        previous_id: str | None = None
        for item in rows(count_cursor):
            canonical_id = str(item["canonical_paper_id"])
            if canonical_id != previous_id:
                result["processed_canonicals"] += 1
                previous_id = canonical_id
        count_cursor.close()
        count_cursor = None

        # MATERIALIZED stores only candidate IDs, not raw_json. The outer scan
        # is forced through the canonical index, so one canonical is reduced at
        # a time without a whole-result sort or grouped dictionary.
        version_cursor = conn.execute(
            f"""
            WITH repository_candidates(canonical_paper_id) AS MATERIALIZED (
                SELECT DISTINCT repository.canonical_paper_id
                FROM paper_versions repository
                WHERE repository.source='openalex'{candidate_scope}
                  AND (
                      COALESCE(repository.raw_json,'') LIKE '%repository%'
                      OR COALESCE(repository.raw_json,'') LIKE '%zenodo%'
                      OR COALESCE(repository.raw_json,'') LIKE '%figshare%'
                      OR COALESCE(repository.raw_json,'') LIKE '%osf.io%'
                      OR COALESCE(repository.raw_json,'') LIKE '%osf preprints%'
                      OR COALESCE(repository.raw_json,'') LIKE '%open science framework%'
                      OR COALESCE(repository.raw_json,'') LIKE '%researchgate%'
                      OR lower(COALESCE(repository.journal,'')) IN (
                          'zenodo','figshare','osf preprints',
                          'open science framework','researchgate'
                      )
                      OR COALESCE(repository.url,'') LIKE '%zenodo.org%'
                      OR COALESCE(repository.url,'') LIKE '%figshare.com%'
                      OR COALESCE(repository.url,'') LIKE '%osf.io%'
                      OR COALESCE(repository.url,'') LIKE '%researchgate.net%'
                      OR COALESCE(repository.pdf_url,'') LIKE '%zenodo.org%'
                      OR COALESCE(repository.pdf_url,'') LIKE '%figshare.com%'
                      OR COALESCE(repository.pdf_url,'') LIKE '%osf.io%'
                      OR COALESCE(repository.pdf_url,'') LIKE '%researchgate.net%'
                      OR lower(COALESCE(repository.doi,'')) LIKE '10.5281/zenodo.%'
                      OR lower(COALESCE(repository.doi,'')) LIKE '10.6084/m9.figshare.%'
                      OR lower(COALESCE(repository.doi,'')) LIKE '10.17605/osf.io/%'
                  )
                  AND {function_name}(
                      repository.raw_json,
                      repository.doi,
                      repository.arxiv_id,
                      repository.journal,
                      repository.url,
                      repository.pdf_url
                  )=1
            )
            SELECT v.id, v.canonical_paper_id, v.version_type, v.doi,
                   v.arxiv_id, v.journal, v.source, v.source_record_id,
                   v.url, v.pdf_url, v.raw_json,
                   p.id AS paper_exists_id,
                   p.condmat_view_eligible AS paper_eligible
            FROM paper_versions AS v INDEXED BY idx_paper_versions_canonical
            LEFT JOIN papers p ON p.id=v.canonical_paper_id
            WHERE v.source IN ('openalex','crossref','arxiv')
              AND EXISTS (
                  SELECT 1 FROM repository_candidates candidate
                  WHERE candidate.canonical_paper_id=v.canonical_paper_id
              )
            ORDER BY v.canonical_paper_id
            """,
            parameters,
        )

        current_id: str | None = None
        version_count = 0
        has_repository = False
        has_qualifying = False
        repository_reason = ""
        paper_exists = False
        paper_eligible = 0
        pending_updates: list[tuple[str, str]] = []

        def finish_canonical() -> None:
            nonlocal version_count, has_repository, has_qualifying
            nonlocal repository_reason, paper_exists, paper_eligible
            if current_id is None or not has_repository:
                return
            result["repository_candidates"] += 1
            result["retained_source_versions"] += version_count
            if has_qualifying:
                result["preserved_with_scholarly_version"] += 1
                return
            if not paper_exists:
                return
            if not paper_eligible:
                result["already_excluded"] += 1
                return
            result["downgraded"] += 1
            if dry_run:
                return
            pending_updates.append((repository_reason, current_id))
            if len(pending_updates) >= fetch_size:
                conn.executemany(
                    "UPDATE papers SET condmat_view_eligible=0, "
                    "condmat_view_reason=? WHERE id=?",
                    pending_updates,
                )
                pending_updates.clear()

        for item in rows(version_cursor):
            canonical_id = str(item["canonical_paper_id"])
            if canonical_id != current_id:
                finish_canonical()
                current_id = canonical_id
                version_count = 0
                has_repository = False
                has_qualifying = False
                repository_reason = ""
                paper_exists = bool(item.get("paper_exists_id"))
                paper_eligible = int(item.get("paper_eligible") or 0)
            version_count += 1
            allowed, _reason, is_repository = _version_quality(item)
            has_qualifying = has_qualifying or allowed
            if is_repository:
                has_repository = True
                if not repository_reason:
                    repository_reason = source_quality_reason(openalex_source_quality(item))
        finish_canonical()
        if pending_updates and not dry_run:
            conn.executemany(
                "UPDATE papers SET condmat_view_eligible=0, "
                "condmat_view_reason=? WHERE id=?",
                pending_updates,
            )
        return result
    finally:
        if count_cursor is not None:
            count_cursor.close()
        if version_cursor is not None:
            version_cursor.close()
        conn.create_function(function_name, 6, None)
