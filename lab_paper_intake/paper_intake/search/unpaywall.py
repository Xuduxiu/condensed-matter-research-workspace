from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..models import normalize_doi


def resolve_unpaywall_pdf(doi: str, settings: Settings) -> str | None:
    email = settings.unpaywall_email
    normalized = normalize_doi(doi)
    if not email or not normalized:
        return None

    response = httpx.get(
        f"https://api.unpaywall.org/v2/{normalized}",
        params={"email": email},
        timeout=settings.request_timeout_seconds,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    if not data.get("is_oa"):
        return None
    return _best_pdf_url(data)


def _best_pdf_url(data: dict[str, Any]) -> str | None:
    best = data.get("best_oa_location") or {}
    if best.get("url_for_pdf"):
        return best["url_for_pdf"]
    for location in data.get("oa_locations") or []:
        if location.get("url_for_pdf"):
            return location["url_for_pdf"]
    return None
