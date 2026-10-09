from __future__ import annotations

import re

from .dictionaries import MATERIALS, METHODS, PHYSICS_CONCEPTS, SEED_CONCEPTS, SYNONYMS


def fold_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def lookup_key(text: str) -> str:
    return fold_text(text).lower().replace("-", " ")


def normalize_term(term: str) -> str:
    cleaned = fold_text(term).strip(" .,;:()[]{}")
    key = lookup_key(cleaned)
    compact = key.replace(" ", "")
    if cleaned in SYNONYMS:
        return SYNONYMS[cleaned]
    if key in SYNONYMS:
        return SYNONYMS[key]
    if compact in SYNONYMS:
        return SYNONYMS[compact]
    for canonical in PHYSICS_CONCEPTS + MATERIALS + METHODS + SEED_CONCEPTS:
        if lookup_key(canonical) == key:
            return canonical
    return canonical_case(cleaned)

def canonical_case(term: str) -> str:
    known = {
        "wse2": "WSe2",
        "mos2": "MoS2",
        "mose2": "MoSe2",
        "mote2": "MoTe2",
        "wte2": "WTe2",
        "nbse2": "NbSe2",
        "tas2": "TaS2",
        "fese": "FeSe",
        "fetese": "FeTeSe",
        "av3sb5": "AV3Sb5",
        "zrte5": "ZrTe5",
        "hfte5": "HfTe5",
        "cd3as2": "Cd3As2",
        "mnbi2te4": "MnBi2Te4",
        "cri3": "CrI3",
        "rucl3": "RuCl3",
        "hbn": "hBN",
        "arpes": "ARPES",
        "stm": "STM",
        "sts": "STS",
        "moke": "MOKE",
        "thz": "THz",
        "dft": "DFT",
        "dmft": "DMFT",
    }
    key = term.lower().replace(" ", "")
    if key in known:
        return known[key]
    return term[:1].upper() + term[1:] if term.islower() else term


def month_range(start_month: str, end_month: str) -> list[str]:
    year, month = [int(part) for part in start_month.split("-")]
    end_year, end_mon = [int(part) for part in end_month.split("-")]
    months: list[str] = []
    while (year, month) <= (end_year, end_mon):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def months_between(start_month: str, end_month: str) -> int:
    sy, sm = [int(part) for part in start_month.split("-")]
    ey, em = [int(part) for part in end_month.split("-")]
    return (ey - sy) * 12 + (em - sm)
