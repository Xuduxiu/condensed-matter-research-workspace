from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MaterialEntry:
    canonical: str
    aliases: tuple[str, ...] = ()
    material_family: str = "quantum material"
    is_platform_material: bool = False


MATERIAL_REGISTRY: dict[str, MaterialEntry] = {
    "hBN": MaterialEntry(
        canonical="hBN",
        aliases=("h-BN", "h BN", "hexagonal boron nitride", "hexagonal BN", "boron nitride", "BN encapsulation"),
        material_family="2D platform material",
        is_platform_material=True,
    ),
    "graphene": MaterialEntry(
        canonical="graphene",
        aliases=("monolayer graphene",),
        material_family="2D platform material",
        is_platform_material=True,
    ),
    "bilayer graphene": MaterialEntry(canonical="bilayer graphene", aliases=("BLG",), material_family="graphene", is_platform_material=True),
    "trilayer graphene": MaterialEntry(canonical="trilayer graphene", material_family="graphene", is_platform_material=True),
    "twisted bilayer graphene": MaterialEntry(canonical="twisted bilayer graphene", aliases=("TBG",), material_family="graphene", is_platform_material=True),
    "multilayer graphene": MaterialEntry(canonical="multilayer graphene", material_family="graphene", is_platform_material=True),
    "WSe2": MaterialEntry(canonical="WSe2", material_family="TMD"),
    "MoS2": MaterialEntry(canonical="MoS2", material_family="TMD"),
    "MoSe2": MaterialEntry(canonical="MoSe2", material_family="TMD"),
    "MoTe2": MaterialEntry(canonical="MoTe2", material_family="TMD"),
    "WTe2": MaterialEntry(canonical="WTe2", material_family="TMD"),
    "NbSe2": MaterialEntry(canonical="NbSe2", material_family="TMD"),
    "TaS2": MaterialEntry(canonical="TaS2", material_family="TMD"),
    "ZrTe5": MaterialEntry(canonical="ZrTe5", material_family="topological semimetal / quantum material"),
    "HfTe5": MaterialEntry(canonical="HfTe5", material_family="topological semimetal / quantum material"),
    "FeSe": MaterialEntry(canonical="FeSe", material_family="iron-based superconductor"),
    "FeTeSe": MaterialEntry(canonical="FeTeSe", material_family="iron-based superconductor"),
    "MnBi2Te4": MaterialEntry(canonical="MnBi2Te4", material_family="magnetic topological material"),
    "Cd3As2": MaterialEntry(canonical="Cd3As2", material_family="Dirac semimetal"),
    "TaAs": MaterialEntry(canonical="TaAs", material_family="Weyl semimetal"),
    "AV3Sb5": MaterialEntry(canonical="AV3Sb5", material_family="kagome metal"),
    "CsV3Sb5": MaterialEntry(canonical="CsV3Sb5", material_family="kagome metal"),
    "KV3Sb5": MaterialEntry(canonical="KV3Sb5", material_family="kagome metal"),
    "RbV3Sb5": MaterialEntry(canonical="RbV3Sb5", material_family="kagome metal"),
    "cuprate": MaterialEntry(canonical="cuprate", material_family="cuprate superconductor"),
    "nickelate": MaterialEntry(canonical="nickelate", material_family="nickelate superconductor"),
    "kagome metal": MaterialEntry(canonical="kagome metal", material_family="kagome metal"),
    "transition metal dichalcogenide": MaterialEntry(canonical="transition metal dichalcogenide", aliases=("TMD", "TMDs"), material_family="TMD"),
}


ALIAS_TO_CANONICAL: dict[str, str] = {}
for entry in MATERIAL_REGISTRY.values():
    ALIAS_TO_CANONICAL[entry.canonical.lower()] = entry.canonical
    for alias in entry.aliases:
        ALIAS_TO_CANONICAL[alias.lower().replace("-", " ")] = entry.canonical
        ALIAS_TO_CANONICAL[alias.lower()] = entry.canonical


def canonical_material(value: str) -> str | None:
    key = " ".join((value or "").strip().split()).lower()
    if key in ALIAS_TO_CANONICAL:
        return ALIAS_TO_CANONICAL[key]
    key_no_dash = key.replace("-", " ")
    return ALIAS_TO_CANONICAL.get(key_no_dash)


def is_platform_material(value: str) -> bool:
    entry = MATERIAL_REGISTRY.get(value)
    return bool(entry and entry.is_platform_material)
