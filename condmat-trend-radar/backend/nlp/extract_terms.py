from __future__ import annotations

import re
from collections import OrderedDict

from .dictionaries import (
    BN_CONTEXT_TERMS,
    MATERIALS,
    METHODS,
    PHYSICS_CONCEPTS,
    SEED_CONCEPTS,
    SYNONYMS,
)
from .material_extract import extract_materials
from .normalize import lookup_key, normalize_term
from .term_filters import display_eligibility

TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?|moiré|vdW|dHvA", re.IGNORECASE)


def extract_terms(title: str, abstract: str) -> list[dict]:
    text_title = title or ""
    text_abstract = abstract or ""
    combined = f"{text_title}. {text_abstract}"
    combined_key = lookup_key(combined)
    found: "OrderedDict[tuple[str, str], dict]" = OrderedDict()

    def add(term: str, term_type: str, confidence: float, source: str = "dictionary") -> None:
        normalized = normalize_term(term)
        eligible, reason = display_eligibility(normalized, term_type, confidence)
        key = (normalized, term_type)
        if key in found:
            found[key]["confidence"] = max(found[key]["confidence"], confidence)
            found[key]["display_eligible"] = max(found[key]["display_eligible"], eligible)
            if eligible:
                found[key]["display_reason"] = reason
            return
        found[key] = {
            "term": term,
            "term_type": term_type,
            "normalized_term": normalized,
            "confidence": confidence,
            "display_eligible": eligible,
            "display_reason": reason,
            "source": source,
        }

    # 1. Canonical physics phrase dictionary exact match.
    for phrase in sorted(set(PHYSICS_CONCEPTS + SEED_CONCEPTS), key=len, reverse=True):
        if phrase_in_text(phrase, text_title):
            add(phrase, "concept", 1.0, "physics_dictionary_title")
        elif phrase_in_text(phrase, text_abstract):
            add(phrase, "concept", 0.72, "physics_dictionary_abstract")

    # 2. Synonym-normalized phrases.
    for alias, canonical in SYNONYMS.items():
        if phrase_in_text(alias, combined):
            term_type = "material" if canonical in MATERIALS else "concept"
            add(canonical, term_type, 0.78, "synonym")

    # 3. Material formula and material registry detector.
    for item in extract_materials(text_title, text_abstract):
        add(item["term"], "material", float(item.get("confidence", 0.8)), "material_detector")

    # 4. Method dictionary exact match.
    for method in sorted(set(METHODS), key=len, reverse=True):
        if phrase_in_text(method, text_title):
            add(method, "method", 0.9, "method_dictionary_title")
        elif phrase_in_text(method, text_abstract):
            add(method, "method", 0.62, "method_dictionary_abstract")

    # 5. Curated material dictionary fallback for entries not covered by regex/registry.
    for material in sorted(set(MATERIALS), key=len, reverse=True):
        if phrase_in_text(material, text_title):
            add(material, "material", 0.92, "material_dictionary_title")
        elif phrase_in_text(material, text_abstract):
            add(material, "material", 0.66, "material_dictionary_abstract")

    # 6. Ambiguous raw BN only weakly maps to hBN when 2D/vdW context exists.
    if phrase_in_text("BN", combined) and any(context in combined_key for context in BN_CONTEXT_TERMS):
        add("hBN", "material", 0.45, "contextual_bn")

    # OpenAlex concepts remain a weak metadata signal in raw_json; they are not expanded into n-grams here.
    return sorted(found.values(), key=lambda item: (-item["confidence"], item["normalized_term"]))


def phrase_in_text(phrase: str, text: str) -> bool:
    if not phrase or not text:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(phrase).replace(r"\ ", r"\s+").replace(r"\-", r"[-\s]?") + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None
