from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable


ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
ARXIV_CATEGORIES = [
    "cond-mat.dis-nn",
    "cond-mat.mes-hall",
    "cond-mat.mtrl-sci",
    "cond-mat.other",
    "cond-mat.quant-gas",
    "cond-mat.soft",
    "cond-mat.stat-mech",
    "cond-mat.str-el",
    "cond-mat.supr-con",
]


@dataclass
class ArxivClient:
    timeout: int = 12
    polite_delay: float = 3.0
    last_error: dict[str, Any] | None = field(default=None, init=False)
    last_fetched_count: int = field(default=0, init=False)

    def fetch(
        self,
        from_date: str,
        to_date: str,
        max_results: int = 150,
        categories: list[str] | None = None,
        max_pages: int = 100,
        page_hook: Callable[[str, int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch a complete updated-date window with bounded pagination.

        Sorting by ``lastUpdatedDate`` captures both newly submitted papers and
        revisions of older preprints.  Pagination continues until a short page
        or until the oldest update is before the requested window.
        """
        self.last_error = None
        self.last_fetched_count = 0
        query = " OR ".join(f"cat:{cat}" for cat in (categories or ARXIV_CATEGORIES))
        page_size = min(200, max(50, int(max_results or 150)))
        output: dict[str, dict[str, Any]] = {}
        for page in range(max(1, min(int(max_pages or 100), 200))):
            # The scheduler supplies a cooperative-cancellation hook. Keep it
            # outside the network exception handler so cancellation is never
            # misreported as an arXiv transport failure.
            if page_hook is not None:
                page_hook("before", page)
            params = {
                "search_query": query,
                "sortBy": "lastUpdatedDate",
                "sortOrder": "descending",
                "start": str(page * page_size),
                "max_results": str(page_size),
            }
            try:
                url = f"{ARXIV_ENDPOINT}?{urllib.parse.urlencode(params)}"
                request = urllib.request.Request(url, headers={"User-Agent": "condmat-trend-radar/0.1", "Accept": "application/atom+xml"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    fetched_papers = parse_arxiv_feed(response.read())
            except Exception as exc:
                message = " ".join(str(exc).split())[:300]
                self.last_error = {
                    "error": "arxiv_fetch_failed",
                    "type": type(exc).__name__,
                    "message": message or "request failed without a message",
                    "page": page,
                    "records_preserved": len(output),
                }
                return list(output.values()), 1
            self.last_fetched_count += len(fetched_papers)
            if page_hook is not None:
                page_hook("after", page)
            for paper in fetched_papers:
                if _record_in_window(paper, from_date, to_date):
                    identity = str(paper.get("arxiv_id") or paper.get("id") or "")
                    current = output.get(identity)
                    if not current or int(paper.get("arxiv_version") or 0) >= int(current.get("arxiv_version") or 0):
                        output[identity] = paper
            if len(fetched_papers) < page_size:
                return list(output.values()), 0
            update_dates = [str(paper.get("updated_date") or "")[:10] for paper in fetched_papers]
            update_dates = [value for value in update_dates if value]
            if update_dates and min(update_dates) < from_date:
                return list(output.values()), 0
            if self.polite_delay:
                time.sleep(self.polite_delay)
        # Reaching the safety cap while every fetched update is still inside
        # the window is not a successful complete scan.  Preserve the records,
        # report PARTIAL, and keep the scheduler watermark unchanged.
        self.last_error = {
            "error": "arxiv_page_limit_reached",
            "type": "CoverageLimit",
            "message": "arXiv pagination limit reached before leaving the requested update window",
            "page": max(1, min(int(max_pages or 100), 200)),
            "records_preserved": len(output),
        }
        return list(output.values()), 1

    def fetch_by_id(self, arxiv_id: str) -> dict[str, Any] | None:
        """Fetch the latest version of one stable arXiv identifier."""
        base, _ = _split_arxiv_version(str(arxiv_id or "").strip())
        if not base:
            return None
        params = {"id_list": base, "max_results": "1"}
        try:
            url = f"{ARXIV_ENDPOINT}?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(url, headers={"User-Agent": "condmat-trend-radar/0.1", "Accept": "application/atom+xml"})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                papers = parse_arxiv_feed(response.read())
            if self.polite_delay:
                time.sleep(self.polite_delay)
            for paper in papers:
                if str(paper.get("arxiv_id") or "").lower() == base.lower():
                    return paper
            return None
        except Exception as exc:
            self.last_error = {
                "error": "arxiv_fetch_by_id_failed",
                "type": type(exc).__name__,
                "message": " ".join(str(exc).split())[:300],
            }
            raise

def parse_arxiv_feed(xml: bytes) -> list[dict[str, Any]]:
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(xml)
    papers: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        arxiv_url = text(entry.find("atom:id", ns))
        versioned_arxiv_id = arxiv_url.rsplit("/", 1)[-1] if arxiv_url else ""
        arxiv_id, arxiv_version = _split_arxiv_version(versioned_arxiv_id)
        doi = text(entry.find("arxiv:doi", ns))
        categories = [item.attrib.get("term", "") for item in entry.findall("atom:category", ns) if item.attrib.get("term")]
        primary_node = entry.find("arxiv:primary_category", ns)
        primary_category = primary_node.attrib.get("term", "") if primary_node is not None else ""
        published = text(entry.find("atom:published", ns))
        updated = text(entry.find("atom:updated", ns))
        papers.append(
            {
                "id": f"arxiv:{arxiv_id}",
                "arxiv_id": arxiv_id,
                "arxiv_version": arxiv_version,
                "doi": doi,
                "title": text(entry.find("atom:title", ns)),
                "abstract": text(entry.find("atom:summary", ns)),
                "publication_date": published[:10],
                "submitted_date": published[:10],
                "updated_date": updated[:10],
                "journal": "arXiv",
                "source": "arxiv",
                "source_scope": "preprint",
                "data_mode": "real",
                "pdf_url": f"https://arxiv.org/pdf/{versioned_arxiv_id}" if versioned_arxiv_id else "",
                "is_open_access": True,
                "oa_status": "arxiv",
                "authors": [text(author.find("atom:name", ns)) for author in entry.findall("atom:author", ns)],
                "primary_category": primary_category,
                "categories": categories,
                "url": arxiv_url,
                "raw_json": {
                    "arxiv_id": arxiv_id,
                    "arxiv_version": arxiv_version,
                    "published": published,
                    "updated": updated,
                    "primary_category": primary_category,
                    "categories": categories,
                },
            }
        )
    return papers


def _split_arxiv_version(value: str) -> tuple[str, int | None]:
    base, marker, version = value.rpartition("v")
    if marker and base and version.isdigit():
        return base, int(version)
    return value, None


def _record_in_window(paper: dict[str, Any], from_date: str, to_date: str) -> bool:
    submitted = str(paper.get("submitted_date") or paper.get("publication_date") or "")[:10]
    updated = str(paper.get("updated_date") or "")[:10]
    return any(from_date <= value <= to_date for value in (submitted, updated) if value)


def text(node: ET.Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""