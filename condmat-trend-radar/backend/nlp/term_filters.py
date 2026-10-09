from __future__ import annotations

import re

from .dictionaries import MATERIALS, METHODS, PHYSICS_CONCEPTS, SEED_CONCEPTS
from .material_registry import MATERIAL_REGISTRY
from .normalize import lookup_key, normalize_term

GENERIC_SINGLE_TERMS = {
    "quantum",
    "physics",
    "matter",
    "materials",
    "material",
    "band",
    "state",
    "states",
    "effect",
    "system",
    "systems",
    "order",
    "phase",
    "topological",
    "magnetic",
    "electronic",
    "electron",
    "hall",
    "chern",
    "weyl",
    "dirac",
    "superconducting",
    "superconductivity",
    "insulator",
    "semimetal",
}

GENERIC_PHRASES = {
    "graphene quantum",
    "quantum materials",
    "condensed matter",
    "matter physics",
    "physics quantum",
    "materials physics",
    "physics quantum materials",
    "matter physics quantum",
    "quantum materials van",
}

ALLOWED_PHRASES = {lookup_key(item) for item in (PHYSICS_CONCEPTS + SEED_CONCEPTS + MATERIALS + METHODS)}
ALLOWED_PHRASES.update({lookup_key(item) for item in MATERIAL_REGISTRY})
ALLOWED_PHRASES.update(
    {
        "quantum hall effect",
        "fractional quantum hall effect",
        "chern insulator",
        "weyl semimetal",
        "dirac semimetal",
        "topological superconductor",
        "topological insulator",
        "quantum oscillation",
        "quantum spin liquid",
    }
)

BROKEN_PATTERNS = (
    "physics quantum",
    "matter physics",
    "quantum materials van",
    "quantum materials",
)

FORMULA_DISPLAY_RE = re.compile(r"^(?:[A-Z][a-z]?[0-9]*){2,}[0-9]*$")


def display_eligibility(term: str, term_type: str = "concept", confidence: float = 1.0) -> tuple[int, str]:
    normalized = normalize_term(term)
    key = lookup_key(normalized)
    words = key.split()

    if not normalized or len(normalized.strip()) < 3:
        return 0, "too_short"
    if key in GENERIC_SINGLE_TERMS or key in GENERIC_PHRASES:
        if term_type == "method" and key in {"transport", "superconductivity"}:
            return 1, "method_dictionary"
        return 0, "generic_term"
    if any(pattern in key for pattern in BROKEN_PATTERNS) and key not in ALLOWED_PHRASES:
        return 0, "broken_ngram"
    if len(words) >= 2 and all(word in GENERIC_SINGLE_TERMS or word in {"van", "der", "waals"} for word in words) and key not in ALLOWED_PHRASES:
        return 0, "generic_ngram"
    if term_type == "material":
        if normalized in MATERIAL_REGISTRY or normalized in MATERIALS or FORMULA_DISPLAY_RE.match(normalized):
            return 1, "material"
        return 0, "weak_material_candidate"
    if term_type == "method":
        return (1, "method_dictionary") if normalized in METHODS else (0, "weak_method_candidate")
    if key in ALLOWED_PHRASES:
        return 1, "canonical_phrase"
    if FORMULA_DISPLAY_RE.match(normalized):
        return 1, "material_formula"
    if confidence < 0.7:
        return 0, "weak_candidate"
    return 0, "not_canonical"


def is_display_eligible(term: str, term_type: str = "concept", confidence: float = 1.0) -> bool:
    return bool(display_eligibility(term, term_type, confidence)[0])
