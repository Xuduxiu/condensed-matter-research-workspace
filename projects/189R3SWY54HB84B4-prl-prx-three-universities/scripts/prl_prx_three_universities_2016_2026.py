#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2016--2025 PRL/PRX university publication audit.

Source: OpenAlex public API. Strictly distinguish affiliation participation
from explicit corresponding authorship, and never silently treat unknown
corresponding-author metadata as an observed zero.

Install:  pip install requests
Run:      python scripts/prl_prx_three_universities_2016_2026.py
Optional: export OPENALEX_API_KEY=your_api_key

Outputs in current directory:
    prl_prx_annual_2016_2025.csv
    prl_prx_papers_audit_2016_2025.csv
    prl_prx_quality_2016_2025.txt
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE = "https://api.openalex.org"
YEARS = range(2016, 2026)
SCHOOLS = {
    "SUSTech": {"name": "南方科技大学", "ror": "049tv2d57"},
    "UTokyo": {"name": "东京大学", "ror": "057zh3y96"},
    "Kyoto": {"name": "京都大学", "ror": "02kpeqv85"},
}
JOURNALS = {
    "PRL": {"issn": "0031-9007", "title": "physical review letters"},
    "PRX": {"issn": "2160-3308", "title": "physical review x"},
}
REPORT = Path(".")
API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()
API_EMAIL = os.environ.get("OPENALEX_EMAIL", "").strip()
# Corrections/errata do not count as primary research papers.
NOT_RESEARCH = re.compile(
    r"^(?:erratum|corrigendum|correction|publisher['’]s note|"
    r"retraction|withdrawal|comment on|reply to|editorial|"
    r"expression of concern)\s*[:\-—\(]", re.I
)

session = requests.Session()
session.headers.update({"User-Agent": "ThreeUniversityPRLPRXAudit/1.0 (academic bibliometrics)"})


def api_get(endpoint: str, params: dict | None = None) -> dict:
    args = dict(params or {})
    if API_KEY:
        args["api_key"] = API_KEY
    if API_EMAIL:
        args["mailto"] = API_EMAIL
    for attempt in range(7):
        try:
            res = session.get(BASE + endpoint, params=args, timeout=65)
            if res.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2**attempt, 32))
                continue
            res.raise_for_status()
            return res.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 6:
                raise
            time.sleep(min(2**attempt, 32))
    raise RuntimeError("OpenAlex refused requests after repeated retries. "
                       "See https://help.openalex.org/api/authentication/")


def plain_id(raw: str) -> str:
    return str(raw or "").rstrip("/").split("/")[-1]


def find_institution_id(ror_short: str) -> str:
    ror_url = "https://ror.org/" + ror_short
    data = api_get("/institutions", {"filter": "ror:" + ror_url, "per_page": 25})
    found = [r for r in data.get("results", [])
             if (r.get("ror") == ror_url or r.get("ids", {}).get("ror") == ror_url)]
    if len(found) != 1:
        raise ValueError(f"ROR {ror_url} did not resolve uniquely: "
                         f"{[(r.get('display_name'), r.get('id')) for r in data.get('results', [])]}")
    return plain_id(found[0]["id"])


def listed_papers_for_school(inst_id: str):
    # One call stream covers both PRL and PRX across ten years.
    criteria = ",".join([
        "publication_year:2016-2025",
        "type:article",
        "authorships.institutions.id:" + inst_id,
        "primary_location.source.issn:0031-9007|2160-3308",
    ])
    cursor, page = "*", 0
    while cursor:
        response = api_get("/works", {"filter": criteria, "per_page": 100,
                                      "cursor": cursor})
        page += 1
        print(f"  page {page}: {len(response.get('results', []))} records "
              f"(total {response.get('meta', {}).get('count', '?')})", flush=True)
        for item in response.get("results", []):
            yield item
        new_cursor = response.get("meta", {}).get("next_cursor")
        if not new_cursor or new_cursor == cursor:
            break
        if not API_KEY:
            raise RuntimeError(
                "OpenAlex requires a verified free API key to page beyond the first page. "
                "Set OPENALEX_API_KEY; see https://help.openalex.org/api/authentication/. "
                "Refusing incomplete multi-page counts."
            )
        cursor = new_cursor
        time.sleep(0.18)


def journal_key(work: dict) -> str | None:
    source = (work.get("primary_location") or {}).get("source") or {}
    issns = set(source.get("issn") or [])
    name = (source.get("display_name") or "").strip().lower()
    for key, meta in JOURNALS.items():
        if name == meta["title"] or meta["issn"] in issns:
            return key
    return None


def doi_key(work: dict) -> str:
    doi = (work.get("doi") or "").strip().lower()
    if doi:
        return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    return plain_id(work.get("id"))


def get_complete_work_if_needed(work: dict, institution_id: str) -> dict:
    authorships = work.get("authorships") or []
    # OpenAlex /works list records contain up to the first 100 authors;
    # retrieve the full single-work record for large collaborations.
    n = work.get("authorships_count", 0) or 0
    if len(authorships) >= 100 or n > len(authorships):
        return api_get("/works/" + plain_id(work["id"]))
    # Fallback: if institution filter matched, but truncated affiliations
    # inexplicably don't include this institution, fetch the full record.
    if not any(institution_id in {plain_id(i.get("id"))
                                  for i in a.get("institutions", [])}
               for a in authorships):
        return api_get("/works/" + plain_id(work["id"]))
    return work


def is_exact_institution(authorship: dict, institution_id: str) -> bool:
    return any(plain_id(inst.get("id")) == institution_id
               for inst in authorship.get("institutions", []))


def corresponding_status(work: dict, institution_id: str):
    authorships = work.get("authorships") or []
    correspondents = [a for a in authorships if a.get("is_corresponding") is True]
    if any(is_exact_institution(a, institution_id) for a in correspondents):
        return "explicit", correspondents

    # The authorship list is complete by this point. We deliberately do NOT
    # count the OpenAlex fallback corresponding_institution_ids if there is
    # no actual corresponding author: it can be an inferred institution.
    if not correspondents:
        return "unknown_no_corresponding_author", []
    if any(not a.get("institutions") for a in correspondents):
        return "unknown_corresponding_affiliation", correspondents
    return "not_corresponding", correspondents


def csv_write(path: Path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run():
    school_ids = {}
    for key, school in SCHOOLS.items():
        school_ids[key] = find_institution_id(school["ror"])
        print("Resolved:", school["name"], school_ids[key], flush=True)

    metrics = defaultdict(lambda: {"participating": 0, "corresponding": 0,
                                  "unknown": 0, "not_corresponding": 0})
    details = []
    seen = set()
    skipped = []
    known_data = {}
    for school_key, school in SCHOOLS.items():
        print("Fetching", school["name"], flush=True)
        school_records = 0
        for item in listed_papers_for_school(school_ids[school_key]):
            jkey = journal_key(item)
            year = item.get("publication_year")
            if jkey not in JOURNALS or year not in YEARS:
                skipped.append((school_key, item.get("doi"), "journal/year mismatch"))
                continue
            title = item.get("title") or item.get("display_name") or ""
            if NOT_RESEARCH.search(title):
                skipped.append((school_key, item.get("doi"), "non-research title"))
                continue
            doi = doi_key(item)
            unique = (school_key, year, jkey, doi)
            if unique in seen:
                continue
            seen.add(unique)
            school_records += 1

            if doi in known_data:
                full = known_data[doi]
            else:
                full = get_complete_work_if_needed(item, school_ids[school_key])
                known_data[doi] = full
            status, corr = corresponding_status(full, school_ids[school_key])
            entry = metrics[year, jkey, school_key]
            entry["participating"] += 1
            if status == "explicit":
                entry["corresponding"] += 1
            elif status.startswith("unknown"):
                entry["unknown"] += 1
            else:
                entry["not_corresponding"] += 1
            corr_data = []
            for author in corr:
                name = (author.get("author") or {}).get("display_name") or author.get("raw_author_name")
                aff = " | ".join(author.get("raw_affiliation_strings") or [])
                institutes = "; ".join(i.get("display_name") or ""
                                        for i in author.get("institutions") or [])
                corr_data.append(f"{name} [{institutes}] ({aff})")
            details.append({
                "year": year, "journal": jkey, "school": school["name"],
                "doi": doi, "title": title,
                "published_date_openalex": full.get("publication_date"),
                "participation": 1,
                "corresponding_explicit": 1 if status == "explicit" else 0,
                "corresponding_status": status,
                "corresponding_author_and_affiliations": " || ".join(corr_data),
                "openalex_work_id": full.get("id"),
                "paper_url": ("https://doi.org/" + doi if doi.startswith("10.") else ""),
            })

        if school_records == 0:
            raise RuntimeError(
                f"No matching research articles for {school['name']}; "
                "possible API filter or identifier failure. Refusing zero-filled output."
            )

    records = []
    for year in YEARS:
        row = {"year": year}
        for sch_key in SCHOOLS:
            for journal in JOURNALS:
                base = f"{sch_key}_{journal}"
                m = metrics[year, journal, sch_key]
                row[f"{base}_participation"] = m["participating"]
                row[f"{base}_corresponding_explicit"] = m["corresponding"]
                row[f"{base}_unknown_corr_metadata"] = m["unknown"]
        records.append(row)

    fieldnames = ["year"] + [f"{s}_{j}_{m}" for s in SCHOOLS for j in JOURNALS
                                for m in ("participation", "corresponding_explicit",
                                          "unknown_corr_metadata")]
    csv_write(REPORT / "prl_prx_annual_2016_2025.csv", fieldnames, records)
    details.sort(key=lambda r: (r["year"], r["journal"], r["school"], r["doi"]))
    csv_write(REPORT / "prl_prx_papers_audit_2016_2025.csv",
              list(details[0]) if details else
              ["year", "journal", "school", "doi", "title", "published_date_openalex",
               "participation", "corresponding_explicit", "corresponding_status",
               "corresponding_author_and_affiliations", "openalex_work_id", "paper_url"],
              details)

    report = [
        "PRL / PRX, 2016–2025: three-university bibliographic extraction",
        "Run UTC: " + datetime.now(timezone.utc).isoformat(),
        "Source: OpenAlex API. Check each DOI on the APS website for formal publication.",
        "Source ISSN: PRL 0031-9007; PRX 2160-3308. Other PRX-family journals excluded.",
        "Institution IDs (resolved at runtime): " + json.dumps(school_ids, ensure_ascii=False),
        "Unit: one paper per institution per journal/year, deduplicated by DOI.",
        "Only works with type=article, excluding title-identified errata/corrections/etc.",
        "Participation: OpenAlex linked the work to the university's exact institution ID.",
        "Corresponding (explicit): an authorship marked is_corresponding=true links to the university.",
        "Unknown: corresponding-author metadata are missing or have missing affiliations.",
        "Unknown cases are NOT treated as confirmed non-corresponding.",
        "OpenAlex inferred corresponding_institution_ids without corresponding authors are NOT counted.",
        "If an author lists two university affiliations, each university can receive a correspondence count.",
        "This is a computational estimate, NOT a 100% manually APS-verified count.",
        "To claim exact results, audit every 'unknown' and ambiguous entry at the article/PDF source.",
        "OpenAlex can also miss an institution affiliation entirely; compare publisher or WoS/Scopus exports.",
        "Only exact parent university ID matched; child institutes/hospitals are not automatically rolled up.",
        "",
    ]
    for sk, school in SCHOOLS.items():
        for journal in JOURNALS:
            total = {k: sum(metrics[y, journal, sk][k] for y in YEARS)
                     for k in ("participating", "corresponding", "unknown")}
            report.append(f"{school['name']} {journal}: {total}")
    report.append("\nSkipped records requiring manual review:\n")
    report.extend(" | ".join(str(v or "") for v in row) for row in skipped)
    (REPORT / "prl_prx_quality_2016_2025.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report[:20]))
    print("\nGenerated all 3 files. Unknown correspondence rows must be audited.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print("FAILED without creating a verified 10-year result:", repr(exc), file=sys.stderr)
        raise