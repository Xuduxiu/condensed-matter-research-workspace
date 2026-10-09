from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from backend.config import openalex_api_key as configured_openalex_api_key
from backend.config import openalex_mailto as configured_openalex_mailto
from backend.library.deduplication import normalize_doi, parse_arxiv_id


@dataclass(frozen=True)
class Resolution:
    url: str | None
    source: str
    legal_open_access: bool
    reason: str
    access_basis: str = "open_access"
    landing_url: str | None = None
    referer_url: str | None = None


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _merge_mappings(base: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mappings(current, value)
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _raw_metadata(record: Mapping[str, Any]) -> Mapping[str, Any]:
    merged: dict[str, Any] = {}
    for key in ("paper_raw_json", "raw_openalex_json", "raw_crossref_json", "raw_json", "raw"):
        value = _mapping(record.get(key))
        if value:
            merged = _merge_mappings(merged, value)
    return merged


def _openalex_urls_from_payload(raw: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for location_key in ("best_oa_location", "primary_location"):
        location = _mapping(raw.get(location_key))
        value = location.get("pdf_url")
        explicitly_open = location_key == "best_oa_location" or bool(
            location.get("is_oa") or location.get("license")
        )
        if value and explicitly_open:
            urls.append(str(value))
    locations = raw.get("locations") or []
    if isinstance(locations, list):
        for item in locations:
            location = _mapping(item)
            value = location.get("pdf_url")
            if value and (location.get("is_oa") or location.get("license")):
                urls.append(str(value))
    access = _mapping(raw.get("open_access"))
    value = access.get("oa_url")
    if value and str(value).lower().split("?", 1)[0].endswith(".pdf"):
        urls.append(str(value))
    return urls


def _openalex_urls(record: Mapping[str, Any]) -> list[str]:
    return _openalex_urls_from_payload(_raw_metadata(record))


def _crossref_cc_pdf_urls_from_payload(raw: Mapping[str, Any]) -> list[str]:
    """Return direct PDF links only when Crossref exposes a CC licence."""
    licenses = raw.get("license") or []
    if not isinstance(licenses, list):
        return []
    is_cc = any(
        "creativecommons.org" in str(
            _mapping(item).get("URL") or _mapping(item).get("url") or ""
        ).lower()
        for item in licenses
    )
    if not is_cc:
        return []
    links = raw.get("link") or []
    if not isinstance(links, list):
        return []
    urls: list[str] = []
    for item in links:
        link = _mapping(item)
        url = str(link.get("URL") or link.get("url") or "").strip()
        content_type = str(link.get("content-type") or link.get("content_type") or "").lower()
        if url and "pdf" in content_type:
            urls.append(url)
    return urls


def _crossref_cc_pdf_urls(record: Mapping[str, Any]) -> list[str]:
    return _crossref_cc_pdf_urls_from_payload(_raw_metadata(record))


def _crossref_pdf_urls_from_payload(raw: Mapping[str, Any]) -> list[str]:
    """Return publisher-declared PDF links without asserting Open Access."""
    links = raw.get("link") or []
    if not isinstance(links, list):
        return []
    urls: list[str] = []
    for item in links:
        link = _mapping(item)
        url = str(link.get("URL") or link.get("url") or "").strip()
        content_type = str(link.get("content-type") or link.get("content_type") or "").casefold()
        if url and "pdf" in content_type:
            urls.append(url)
    return urls


def _crossref_institutional_pdf_urls(record: Mapping[str, Any]) -> list[str]:
    raw = _raw_metadata(record)
    oa_urls = set(_crossref_cc_pdf_urls_from_payload(raw))
    return [url for url in _crossref_pdf_urls_from_payload(raw) if url not in oa_urls]


def _unpaywall_urls(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("is_oa"):
        return []
    locations: list[Mapping[str, Any]] = []
    best = _mapping(payload.get("best_oa_location"))
    if best:
        locations.append(best)
    raw_locations = payload.get("oa_locations") or []
    if isinstance(raw_locations, list):
        locations.extend(_mapping(item) for item in raw_locations)
    urls: list[str] = []
    for location in locations:
        value = location.get("url_for_pdf")
        if not value:
            fallback = location.get("url")
            if fallback and str(fallback).lower().split("?", 1)[0].endswith(".pdf"):
                value = fallback
        if value:
            urls.append(str(value))
    return urls


def _provider_json(
    url: str,
    *,
    params: Mapping[str, str] | None,
    timeout: float,
) -> Mapping[str, Any]:
    import httpx

    response = httpx.get(
        url,
        params=dict(params or {}),
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": "condmat-trend-radar/2.4 (legal OA resolver)",
            "Accept": "application/json",
        },
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, Mapping) else {}


def _live_openalex_urls(
    doi: str,
    *,
    timeout: float,
    api_key: str | None,
    mailto: str | None,
) -> list[str]:
    params: dict[str, str] = {}
    if api_key:
        params["api_key"] = api_key
    if mailto:
        params["mailto"] = mailto
    payload = _provider_json(
        f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/')}",
        params=params,
        timeout=timeout,
    )
    return _openalex_urls_from_payload(payload)


def _live_crossref_urls(doi: str, *, timeout: float, mailto: str | None) -> list[str]:
    params = {"mailto": mailto} if mailto else None
    payload = _provider_json(
        f"https://api.crossref.org/works/{quote(doi, safe='')}",
        params=params,
        timeout=timeout,
    )
    return _crossref_cc_pdf_urls_from_payload(_mapping(payload.get("message")))


def _live_crossref_institutional_urls(
    doi: str,
    *,
    timeout: float,
    mailto: str | None,
) -> list[str]:
    params = {"mailto": mailto} if mailto else None
    payload = _provider_json(
        f"https://api.crossref.org/works/{quote(doi, safe='')}",
        params=params,
        timeout=timeout,
    )
    message = _mapping(payload.get("message"))
    oa_urls = set(_crossref_cc_pdf_urls_from_payload(message))
    return [url for url in _crossref_pdf_urls_from_payload(message) if url not in oa_urls]


def is_safe_institutional_url(value: Any) -> bool:
    """Allow only public-looking HTTPS URLs for campus-IP candidates."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return "." in hostname
    return bool(address.is_global)


def _add_candidate(    candidates: list[Resolution],
    seen: set[str],
    url: Any,
    source: str,
    reason: str,
    *,
    legal_open_access: bool = True,
    access_basis: str = "open_access",
    landing_url: str | None = None,
    referer_url: str | None = None,
) -> None:
    clean = str(url or "").strip()
    if not clean or clean in seen:
        return
    if not clean.lower().startswith(("http://", "https://")):
        return
    if access_basis == "institutional_ip":
        if not is_safe_institutional_url(clean):
            return
        landing_url = (
            landing_url if is_safe_institutional_url(landing_url) else None
        )
        referer_url = (
            referer_url if is_safe_institutional_url(referer_url) else landing_url
        )
    seen.add(clean)
    candidates.append(
        Resolution(
            clean,
            source,
            legal_open_access,
            reason,
            access_basis=access_basis,
            landing_url=landing_url,
            referer_url=referer_url,
        )
    )


def _publisher_institutional_candidates(doi: str) -> list[tuple[str, str, str | None]]:
    """Known publisher PDF routes that may honor campus source-IP access."""
    clean = normalize_doi(doi) or ""
    lower = clean.casefold()
    if lower.startswith("10.1103/"):
        return [
            (
                f"https://link.aps.org/pdf/{clean}",
                "aps_institutional_ip",
                f"https://link.aps.org/doi/{clean}",
            )
        ]
    if lower.startswith("10.1038/"):
        suffix = clean.split("/", 1)[1]
        landing = f"https://www.nature.com/articles/{suffix}"
        return [(f"{landing}.pdf", "nature_institutional_ip", landing)]
    if lower.startswith("10.1126/"):
        landing = f"https://www.science.org/doi/{clean}"
        return [
            (
                f"https://www.science.org/doi/pdf/{clean}?download=true",
                "science_institutional_ip",
                landing,
            )
        ]
    if lower.startswith("10.1007/"):
        return [
            (
                f"https://link.springer.com/content/pdf/{clean}.pdf",
                "springer_institutional_ip",
                f"https://link.springer.com/article/{clean}",
            )
        ]
    if lower.startswith("10.1088/"):
        landing = f"https://iopscience.iop.org/article/{clean}"
        return [(f"{landing}/pdf", "iop_institutional_ip", landing)]
    if lower.startswith("10.1002/"):
        return [
            (
                f"https://onlinelibrary.wiley.com/doi/pdfdirect/{clean}",
                "wiley_institutional_ip",
                f"https://onlinelibrary.wiley.com/doi/{clean}",
            )
        ]
    return []


def resolve_legal_oa_candidates(
    record: Mapping[str, Any],
    *,
    unpaywall_email: str | None = None,
    openalex_api_key: str | None = None,
    openalex_mailto: str | None = None,
    timeout: float = 20.0,
    allow_institutional_ip: bool = False,
) -> list[Resolution]:
    """Return authorized PDF candidates in deterministic preference order.

    Explicit OA/licensed locations always come first. When the caller opts in,
    publisher PDF routes that may honor institutional source-IP access follow
    with ``access_basis='institutional_ip'``. This does not automate SSO,
    import browser cookies, bypass a paywall, or relabel subscriptions as OA.
    """
    candidates: list[Resolution] = []
    seen: set[str] = set()

    arxiv_ids: list[tuple[str, int | None, str]] = []
    for key, source in (("arxiv_id", "arxiv"), ("canonical_arxiv_id", "arxiv_canonical")):
        arxiv_id, arxiv_version = parse_arxiv_id(record.get(key))
        if arxiv_id and not arxiv_version and key == "arxiv_id":
            try:
                arxiv_version = int(record.get("arxiv_version") or 0) or None
            except (TypeError, ValueError):
                arxiv_version = None
        if arxiv_id:
            arxiv_ids.append((arxiv_id, arxiv_version, source))
    alternates = record.get("alternate_versions") or []
    if isinstance(alternates, list):
        for alternate in alternates:
            item = _mapping(alternate)
            arxiv_id, arxiv_version = parse_arxiv_id(item.get("arxiv_id"))
            if arxiv_id and not arxiv_version:
                try:
                    arxiv_version = int(item.get("arxiv_version") or 0) or None
                except (TypeError, ValueError):
                    arxiv_version = None
            if arxiv_id:
                arxiv_ids.append((arxiv_id, arxiv_version, "arxiv_related_version"))
            alternate_source = str(item.get("source") or "").strip().lower()
            if alternate_source == "arxiv":
                _add_candidate(
                    candidates,
                    seen,
                    item.get("pdf_url"),
                    "arxiv_related_pdf",
                    "official arXiv PDF from related source version",
                )
            for url in _openalex_urls(item):
                _add_candidate(
                    candidates,
                    seen,
                    url,
                    "openalex_related_version",
                    "OpenAlex OA location from related source version",
                )
            for url in _crossref_cc_pdf_urls(item):
                _add_candidate(
                    candidates,
                    seen,
                    url,
                    "crossref_related_cc",
                    "Crossref Creative Commons PDF from related source version",
                )
    for arxiv_id, arxiv_version, source in arxiv_ids:
        if arxiv_version:
            _add_candidate(
                candidates,
                seen,
                f"https://arxiv.org/pdf/{arxiv_id}v{arxiv_version}.pdf",
                source,
                "exact arXiv version",
            )
        _add_candidate(
            candidates,
            seen,
            f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            source,
            "latest arXiv version",
        )
        _add_candidate(
            candidates,
            seen,
            f"https://export.arxiv.org/pdf/{arxiv_id}.pdf",
            "arxiv_export",
            "official arXiv export endpoint fallback",
        )

    oa_flag = bool(record.get("is_open_access")) or str(record.get("oa_status") or "").lower() in {
        "gold",
        "green",
        "hybrid",
        "bronze",
        "oa",
        "open",
        "license",
    }
    if oa_flag:
        _add_candidate(
            candidates,
            seen,
            record.get("pdf_url"),
            str(record.get("source") or "metadata"),
            "metadata marks location open access",
        )
        _add_candidate(
            candidates,
            seen,
            record.get("canonical_pdf_url"),
            "canonical_metadata",
            "canonical metadata marks location open access",
        )
        for key, source in (("oa_url", "metadata_oa_url"), ("canonical_oa_url", "canonical_oa_url")):
            oa_url = str(record.get(key) or "").strip()
            if oa_url.lower().split("?", 1)[0].endswith(".pdf"):
                _add_candidate(
                    candidates,
                    seen,
                    oa_url,
                    source,
                    "explicit OA URL points directly to a PDF",
                )
        for url in _openalex_urls(record):
            _add_candidate(candidates, seen, url, "openalex", "OpenAlex OA location")

    local_crossref_urls = _crossref_cc_pdf_urls(record)
    for url in local_crossref_urls:
        _add_candidate(
            candidates,
            seen,
            url,
            "crossref_cc_license",
            "Crossref Creative Commons PDF link",
        )

    doi = normalize_doi(record.get("doi"))
    if doi and unpaywall_email:
        try:
            payload = _provider_json(
                f"https://api.unpaywall.org/v2/{quote(doi, safe='')}",
                params={"email": unpaywall_email},
                timeout=timeout,
            )
            for url in _unpaywall_urls(payload):
                _add_candidate(candidates, seen, url, "unpaywall", "Unpaywall legal OA location")
        except Exception:
            pass

    # Refresh a lone direct-metadata URL as well as an empty result. A stale
    # canonical ``pdf_url`` previously suppressed every live provider lookup,
    # so one dead link could make an otherwise available OA paper fail.
    direct_metadata_reasons = {
        "metadata marks location open access",
        "canonical metadata marks location open access",
    }
    needs_live_lookup = bool(
        doi
        and (
            not candidates
            or all(candidate.reason in direct_metadata_reasons for candidate in candidates)
        )
    )
    candidates_before_live = len(candidates)
    if needs_live_lookup:
        try:
            for url in _live_openalex_urls(
                doi,
                timeout=timeout,
                api_key=openalex_api_key or configured_openalex_api_key(),
                mailto=openalex_mailto or configured_openalex_mailto(),
            ):
                _add_candidate(candidates, seen, url, "openalex_live", "live OpenAlex OA location")
        except Exception:
            pass
    if needs_live_lookup and len(candidates) == candidates_before_live:
        try:
            for url in _live_crossref_urls(
                doi,
                timeout=timeout,
                mailto=openalex_mailto or configured_openalex_mailto(),
            ):
                _add_candidate(
                    candidates,
                    seen,
                    url,
                    "crossref_live_cc",
                    "live Crossref Creative Commons PDF link",
                )
        except Exception:
            pass

    if allow_institutional_ip:
        landing_url = str(record.get("canonical_url") or record.get("url") or
                          (f"https://doi.org/{doi}" if doi else ""))
        institutional_urls = _crossref_institutional_pdf_urls(record)
        if not institutional_urls and doi and not candidates:
            try:
                institutional_urls = _live_crossref_institutional_urls(
                    doi,
                    timeout=timeout,
                    mailto=openalex_mailto or configured_openalex_mailto(),
                )
            except Exception:
                institutional_urls = []
        for url in institutional_urls:
            _add_candidate(
                candidates,
                seen,
                url,
                "crossref_institutional_ip",
                "publisher PDF available through institutional IP access",
                legal_open_access=False,
                access_basis="institutional_ip",
                landing_url=landing_url,
                referer_url=landing_url,
            )
        publisher_candidates = _publisher_institutional_candidates(doi) if doi else []
        for url, source, publisher_landing in publisher_candidates:
            _add_candidate(
                candidates,
                seen,
                url,
                source,
                "publisher PDF route available through institutional IP access",
                legal_open_access=False,
                access_basis="institutional_ip",
                landing_url=publisher_landing,
                referer_url=publisher_landing,
            )
    return candidates


def resolve_legal_oa_url(
    record: Mapping[str, Any],
    *,
    unpaywall_email: str | None = None,
    openalex_api_key: str | None = None,
    openalex_mailto: str | None = None,
    timeout: float = 20.0,
    allow_institutional_ip: bool = False,
) -> Resolution:
    candidates = resolve_legal_oa_candidates(
        record,
        unpaywall_email=unpaywall_email,
        openalex_api_key=openalex_api_key,
        openalex_mailto=openalex_mailto,
        timeout=timeout,
        allow_institutional_ip=allow_institutional_ip,
    )
    return candidates[0] if candidates else Resolution(
        None,
        "none",
        False,
        "no legal open-access PDF location found",
    )


def looks_like_pdf_content_type(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.search(r"application/(pdf|octet-stream)", value, re.I))
