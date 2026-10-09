from __future__ import annotations

import html
import re
from collections import OrderedDict

from .material_registry import ALIAS_TO_CANONICAL, canonical_material
from .normalize import normalize_term

# Curated aliases remain useful, but formula recognition is deliberately generic so
# the radar is not limited to a pre-created list of materials.
ELEMENTS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra",
    "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr", "Rf", "Db",
    "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
}
FORMULA_TOKEN_RE = re.compile(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)")
GENERIC_FORMULA_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z][a-z]?(?:\d+(?:\.\d+)?)?){2,}(?![A-Za-z0-9])")
FORMULA_STOPLIST = {"H2O", "CO2", "O2", "N2", "NH3", "CH4", "NO", "NO2"}
FORMULA_ALLOWLIST = {"YBCO"}
SYNTHETIC_ELEMENTS = {
    "Tc", "Pm", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr", "Rf", "Db",
    "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
}
SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")

LAYERED_PATTERNS = [
    "twisted bilayer graphene",
    "bilayer graphene",
    "trilayer graphene",
    "multilayer graphene",
    "hexagonal boron nitride",
    "hexagonal BN",
    "h-BN",
    "hBN",
]
FAMILY_PATTERNS = [
    "transition metal dichalcogenide",
    "kagome metal",
    "cuprate",
    "nickelate",
    "TMD",
]


def valid_dynamic_formula(value: str) -> bool:
    clean = (value or "").strip()
    if clean in FORMULA_ALLOWLIST:
        return True
    if clean in FORMULA_STOPLIST or len(clean) < 4 or len(clean) > 32:
        return False
    pieces = list(FORMULA_TOKEN_RE.finditer(clean))
    if len(pieces) < 2 or "".join(piece.group(0) for piece in pieces) != clean:
        return False
    if any(piece.group(1) not in ELEMENTS for piece in pieces):
        return False
    if any(piece.group(1) in SYNTHETIC_ELEMENTS for piece in pieces):
        return False
    if any(piece.group(2) for piece in pieces):
        return True
    # Formula-like acronyms are the main false-positive source. Digit-free
    # formulae are kept only when compact (GaAs, FeSe, CrSBr, CoFeB).
    if len(pieces) > 3:
        return False
    if len(pieces) == 3 and pieces[-1].group(1) == "Cs" and pieces[0].group(1) != "Cs":
        return False
    return len(clean) >= 4


def _merge_formula_subscript(match: re.Match[str]) -> str:
    """Join stoichiometric subscripts but drop a trailing polymorph index.

    A token such as AgBeF4 is already a complete, valid formula and its `_4`
    suffix labels a polymorph rather than adding another fluorine count.  Bases
    ending in an element symbol (SrTiO_3) and partial formulas (Bi_2Te_3) retain
    their true stoichiometric subscripts.  Decimal suffixes keep the historical
    behavior because they can encode non-stoichiometry rather than a phase ID.
    """
    base, subscript = match.group(1), match.group(2)
    if base[-1:].isdigit() and subscript.isdigit() and valid_dynamic_formula(base):
        return base
    return f"{base}{subscript}"


def normalize_formula_markup(value: str) -> str:
    """Flatten common publisher formula markup before applying the NER regex."""
    text = html.unescape(value or "").translate(SUBSCRIPT_TRANSLATION)
    # MathML emitted by Crossref/publisher feeds: <msub><mi>SrTiO</mi><mn>3</mn></msub>.
    text = re.sub(
        r"<[^>]*msub[^>]*>\s*<[^>]*mi[^>]*>\s*([A-Za-z][A-Za-z0-9]*)\s*</[^>]*mi>\s*<[^>]*mn[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</[^>]*mn>\s*</[^>]*msub>",
        _merge_formula_subscript,
        text,
        flags=re.IGNORECASE,
    )
    # LaTeX/JATS variants: ${\mathrm{SrTiO}}_{3}$, SrTiO$_3$, SrTiO<sub>3</sub>.
    text = re.sub(r"\{?\\(?:mathrm|text)\{([A-Za-z][A-Za-z0-9]*)\}\}?\s*_\{([0-9]+(?:\.[0-9]+)?)\}", _merge_formula_subscript, text)
    text = re.sub(r"([A-Z][A-Za-z0-9]*)\s*\$?_\{?([0-9]+(?:\.[0-9]+)?)\}?\$?", _merge_formula_subscript, text)
    text = re.sub(r"([A-Z][A-Za-z0-9]*)\s*<sub[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</sub>", _merge_formula_subscript, text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Some publisher plaintext separates subscripts with spaces: MoSe 2,
    # Bi 2 Se 3, MnBi 2 Te 4. Join only element-shaped sequences.
    text = re.sub(
        r"(?<![A-Za-z0-9])([A-Z][a-z]?)\s+([0-9.]+)\s+([A-Z][a-z]?)\s+([0-9.]+)(?![A-Za-z0-9])",
        r"\1\2\3\4",
        text,
    )
    text = re.sub(r"(?<![A-Za-z0-9])((?:[A-Z][a-z]?){2,})\s+([0-9.]+)(?![A-Za-z0-9])", r"\1\2", text)
    for _ in range(3):
        text = re.sub(
            r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9]*\d)\s+((?:[A-Z][a-z]?)+)\s*([0-9.]+)(?![A-Za-z0-9])",
            r"\1\2\3",
            text,
        )
    return text


def extract_materials(title: str, abstract: str) -> list[dict]:
    text_title = normalize_formula_markup(title or "")
    text_abstract = normalize_formula_markup(abstract or "")
    found: "OrderedDict[str, dict]" = OrderedDict()

    def add(term: str, confidence: float, detector: str) -> None:
        registry_name = canonical_material(term)
        canonical = registry_name or (term if valid_dynamic_formula(term) else normalize_term(term))
        if canonical in found:
            found[canonical]["confidence"] = max(found[canonical]["confidence"], confidence)
            if confidence >= found[canonical]["confidence"]:
                found[canonical]["detector"] = detector
            return
        found[canonical] = {
            "term": term,
            "term_type": "material",
            "normalized_term": canonical,
            "confidence": confidence,
            "detector": detector,
        }

    for text, base_confidence, field in (
        (text_title, 0.98, "title"),
        (text_abstract, 0.74, "abstract"),
    ):
        for match in GENERIC_FORMULA_RE.finditer(text):
            formula = match.group(0)
            if valid_dynamic_formula(formula):
                add(formula, base_confidence, f"formula_ner_{field}")
        for phrase in LAYERED_PATTERNS + FAMILY_PATTERNS:
            if phrase_in_text(phrase, text):
                add(phrase, base_confidence, f"registry_phrase_{field}")
        for alias, canonical in ALIAS_TO_CANONICAL.items():
            if len(alias) >= 4 and phrase_in_text(alias, text):
                add(canonical, base_confidence, f"registry_alias_{field}")
    return list(found.values())


def phrase_in_text(phrase: str, text: str) -> bool:
    if not phrase or not text:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(phrase).replace(r"\ ", r"\s+").replace(r"\-", r"[-\s]?") + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None