from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha1
from typing import Any, Callable

from backend.config import openalex_mailto
from backend.ingest.arxiv_client import ARXIV_CATEGORIES, ARXIV_ENDPOINT, parse_arxiv_feed
from backend.ingest.crossref_client import CROSSREF_BASE, CrossrefClient
from backend.ingest.openalex_client import OpenAlexClient
from backend.library.deduplication import normalize_doi, normalize_title, parse_arxiv_id


RemoteFetcher = Callable[[str, int], list[dict[str, Any]]]


def search_openalex(query: str, limit: int) -> list[dict[str, Any]]:
    client = OpenAlexClient(timeout=12, max_retries=1, polite_delay=0, warn_if_missing_key=False)
    payload = client._get_json(
        "/works",
        {
            "search": query,
            "filter": "type:article",
            "per-page": str(max(1, min(limit, 50))),
            "select": (
                "id,doi,title,abstract_inverted_index,publication_date,primary_location,"
                "locations,open_access,authorships,concepts,cited_by_count,type"
            ),
        },
    )
    return [client._normalize_work(item, "") for item in payload.get("results", []) if item.get("title")]


def search_crossref(query: str, limit: int) -> list[dict[str, Any]]:
    client = CrossrefClient(timeout=12, polite_delay=0)
    params = {
        "query.bibliographic": query,
        "rows": str(max(1, min(limit, 50))),
        "filter": "type:journal-article",
        "sort": "relevance",
        "order": "desc",
    }
    mailto = openalex_mailto()
    if mailto:
        params["mailto"] = mailto
    url = f"{CROSSREF_BASE}/works?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": client.user_agent(), "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=client.timeout) as response:
        message = json.loads(response.read().decode("utf-8")).get("message", {})
    return [
        client.normalize_work(item, fallback_journal="")
        for item in message.get("items", [])
        if item.get("title")
    ]


def search_arxiv(query: str, limit: int) -> list[dict[str, Any]]:
    clean = " ".join(query.split())
    categories = " OR ".join(f"cat:{category}" for category in ARXIV_CATEGORIES)
    search_query = f'all:"{clean.replace(chr(34), "")}" AND ({categories})'
    params = {
        "search_query": search_query,
        "sortBy": "relevance",
        "sortOrder": "descending",
        "start": "0",
        "max_results": str(max(1, min(limit, 50))),
    }
    url = f"{ARXIV_ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "condmat-trend-radar/0.3", "Accept": "application/atom+xml"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return parse_arxiv_feed(response.read())


FETCHERS: dict[str, RemoteFetcher] = {
    "openalex": search_openalex,
    "crossref": search_crossref,
    "arxiv": search_arxiv,
}


def source_record_id(record: dict[str, Any]) -> str:
    source = str(record.get("source") or "remote")
    explicit = str(record.get("openalex_id") or record.get("id") or "").strip()
    doi = normalize_doi(record.get("doi"))
    arxiv_id, _version = parse_arxiv_id(record.get("arxiv_id"))
    if explicit:
        return explicit
    if doi:
        return f"doi:{doi}"
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    title = normalize_title(record.get("title"))
    return f"{source}:title:{title[:180]}"


def dedup_key(record: dict[str, Any]) -> str:
    doi = normalize_doi(record.get("doi"))
    if doi:
        return f"doi:{doi}"
    arxiv_id, _version = parse_arxiv_id(record.get("arxiv_id"))
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    title = normalize_title(record.get("title"))
    authors = record.get("authors") or []
    first_author = re.sub(r"\W+", "", str(authors[0] if authors else "").casefold())
    return f"title:{title}:{first_author}"


def record_is_condmat(record: dict[str, Any]) -> bool:
    categories = [str(item).casefold() for item in record.get("categories") or []]
    if any(item.startswith("cond-mat") for item in categories):
        return True
    concepts = [
        str(item.get("display_name") if isinstance(item, dict) else item).casefold()
        for item in (record.get("concepts") or record.get("subjects") or [])
    ]
    evidence = " ".join(concepts + [str(record.get("journal") or "").casefold()])
    markers = (
        "condensed matter",
        "materials science",
        "superconduct",
        "quantum material",
        "solid state",
        "nanotechnology",
    )
    return any(marker in evidence for marker in markers)


def import_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": source_record_id(record),
        "openalex_id": record.get("openalex_id") or "",
        "doi": normalize_doi(record.get("doi")) or "",
        "arxiv_id": parse_arxiv_id(record.get("arxiv_id"))[0] or "",
        "title": str(record.get("title") or "")[:1000],
        "abstract": str(record.get("abstract") or "")[:20000],
        "publication_date": str(record.get("publication_date") or "")[:10],
        "submitted_date": str(record.get("submitted_date") or "")[:10],
        "journal": str(record.get("journal") or "")[:500],
        "source": str(record.get("source") or "remote")[:40],
        "source_scope": str(record.get("source_scope") or "")[:40],
        "authors": [str(item)[:300] for item in (record.get("authors") or [])[:200]],
        "concepts": [
            str(item.get("display_name") if isinstance(item, dict) else item)[:300]
            for item in (record.get("concepts") or record.get("subjects") or [])[:100]
        ],
        "categories": [str(item)[:100] for item in (record.get("categories") or [])[:100]],
        "url": str(record.get("url") or "")[:2000],
        "pdf_url": str(record.get("pdf_url") or "")[:2000],
        "is_open_access": bool(record.get("is_open_access") or record.get("pdf_url")),
        "oa_status": str(record.get("oa_status") or "")[:100],
        "cited_by_count": int(record.get("cited_by_count") or 0),
        "data_mode": "real",
    }


def _local_match(connection: sqlite3.Connection, record: dict[str, Any]) -> str | None:
    doi = normalize_doi(record.get("doi"))
    if doi:
        row = connection.execute(
            """
            SELECT canonical_paper_id FROM paper_versions WHERE doi=?
            UNION ALL SELECT id FROM papers WHERE doi=? LIMIT 1
            """,
            (doi, doi),
        ).fetchone()
        if row:
            return str(row[0])
    arxiv_id, _version = parse_arxiv_id(record.get("arxiv_id"))
    if arxiv_id:
        row = connection.execute(
            """
            SELECT canonical_paper_id FROM paper_versions WHERE lower(arxiv_id)=lower(?)
            UNION ALL SELECT id FROM papers WHERE lower(arxiv_id)=lower(?) LIMIT 1
            """,
            (arxiv_id, arxiv_id),
        ).fetchone()
        if row:
            return str(row[0])
    return None


def _public_item(connection: sqlite3.Connection, record: dict[str, Any], sources: list[str]) -> dict[str, Any]:
    payload = import_payload(record)
    local_id = _local_match(connection, record)
    stable_key = dedup_key(record).encode("utf-8")
    remote_id = f"remote:{payload['source']}:{sha1(stable_key).hexdigest()[:20]}"
    return {
        "remote_id": remote_id,
        "paper_version_id": remote_id,
        "canonical_paper_id": local_id or remote_id,
        "local_canonical_paper_id": local_id,
        "is_local": bool(local_id),
        "remote": True,
        "sources": sources,
        "version_type": "preprint" if payload["source"] == "arxiv" else "published",
        "title": payload["title"],
        "abstract": payload["abstract"],
        "doi": payload["doi"] or None,
        "arxiv_id": payload["arxiv_id"] or None,
        "journal": payload["journal"],
        "publication_date": payload["publication_date"],
        "submitted_date": payload["submitted_date"],
        "year": int(payload["publication_date"][:4]) if payload["publication_date"][:4].isdigit() else None,
        "source": payload["source"],
        "url": payload["url"],
        "pdf_url": payload["pdf_url"],
        "is_open_access": payload["is_open_access"],
        "cited_by_count": payload["cited_by_count"],
        "downloaded": False,
        "download_status": None,
        "extraction_status": None,
        "authors": payload["authors"],
        "materials": [],
        "topics": payload["concepts"][:10] or payload["categories"][:10],
        "condmat_evidence": record_is_condmat(payload),
        "import_record": payload,
    }


def unified_remote_search(
    connection: sqlite3.Connection,
    query: str,
    *,
    sources: list[str] | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    clean = " ".join(query.split())
    if len(clean) < 2:
        return {"items": [], "count": 0, "query": clean, "sources": {}, "notice": "query_too_short"}
    requested = [name for name in (sources or list(FETCHERS)) if name in FETCHERS]
    per_source = max(3, min(25, (limit + max(1, len(requested)) - 1) // max(1, len(requested)) + 3))
    fetched: dict[str, list[dict[str, Any]]] = {}
    statuses: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(requested) or 1)) as executor:
        futures = {executor.submit(FETCHERS[name], clean, per_source): name for name in requested}
        for future in as_completed(futures):
            name = futures[future]
            try:
                records = future.result()
                fetched[name] = records
                statuses[name] = {"status": "ok", "count": len(records)}
            except Exception as exc:
                fetched[name] = []
                statuses[name] = {
                    "status": "error",
                    "count": 0,
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                }
    merged: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for name in requested:
        for record in fetched.get(name, []):
            record["source"] = str(record.get("source") or name)
            key = dedup_key(record)
            if key not in merged:
                merged[key] = (record, [name])
                continue
            current, source_names = merged[key]
            source_names.append(name)
            for field in ("abstract", "doi", "arxiv_id", "pdf_url", "url", "journal"):
                if not current.get(field) and record.get(field):
                    current[field] = record[field]
            current["is_open_access"] = bool(current.get("is_open_access") or record.get("is_open_access"))
            current["cited_by_count"] = max(
                int(current.get("cited_by_count") or 0),
                int(record.get("cited_by_count") or 0),
            )
    items = [_public_item(connection, record, source_names) for record, source_names in merged.values()]
    items.sort(
        key=lambda item: (
            bool(item["is_local"]),
            item.get("publication_date") or "",
            int(item.get("cited_by_count") or 0),
        ),
        reverse=True,
    )
    items = items[: max(1, min(limit, 100))]
    return {
        "items": items,
        "count": len(items),
        "query": clean,
        "sources": statuses,
        "deduplicated": sum(len(value) for value in fetched.values()) - len(items),
    }
