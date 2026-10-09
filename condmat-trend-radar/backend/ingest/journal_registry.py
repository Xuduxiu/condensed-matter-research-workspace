from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JournalEntry:
    canonical_name: str
    aliases: tuple[str, ...]
    issn_print: str
    issn_online: str
    scope: str = "core"
    weight: float = 2.0

    @property
    def issns(self) -> tuple[str, ...]:
        return tuple(item for item in (self.issn_print, self.issn_online) if item)


CORE_JOURNAL_REGISTRY: tuple[JournalEntry, ...] = (
    JournalEntry("Physical Review Letters", ("Phys. Rev. Lett.", "PRL"), "0031-9007", "1079-7114", "core", 3.0),
    JournalEntry("Physical Review X", ("Phys. Rev. X", "PRX"), "", "2160-3308", "core", 3.0),
    JournalEntry("Nature", (), "0028-0836", "1476-4687", "core", 3.0),
    JournalEntry("Science", (), "0036-8075", "1095-9203", "core", 3.0),
    JournalEntry("Nature Physics", (), "1745-2473", "1745-2481", "core", 2.8),
    JournalEntry("Nature Materials", (), "1476-1122", "1476-4660", "core", 2.8),
    JournalEntry("Nature Nanotechnology", (), "1748-3387", "1748-3395", "core", 2.6),
    JournalEntry("Nature Communications", (), "", "2041-1723", "core", 2.1),
    JournalEntry("Science Advances", (), "", "2375-2548", "core", 2.0),
    JournalEntry("npj Quantum Materials", (), "", "2397-4648", "core", 1.8),
)

CONTEXT_JOURNAL_REGISTRY: tuple[JournalEntry, ...] = (
    JournalEntry("Physical Review B", ("Phys. Rev. B", "PRB"), "2469-9950", "2469-9969", "context", 2.0),
    JournalEntry("Nano Letters", (), "1530-6984", "1530-6992", "context", 2.0),
    JournalEntry("ACS Nano", (), "1936-0851", "1936-086X", "context", 2.0),
    JournalEntry("Advanced Materials", (), "0935-9648", "1521-4095", "context", 2.0),
    JournalEntry("2D Materials", (), "", "2053-1583", "context", 2.0),
    JournalEntry("Advanced Functional Materials", (), "1616-301X", "1616-3028", "context", 2.0),
    JournalEntry("Materials Today Physics", (), "", "2542-5293", "context", 2.0),
    JournalEntry("npj 2D Materials and Applications", (), "", "2397-7132", "context", 2.0),
)

ALL_JOURNAL_REGISTRY: tuple[JournalEntry, ...] = CORE_JOURNAL_REGISTRY + CONTEXT_JOURNAL_REGISTRY


def journals_for_scope(scope: str = "core") -> list[JournalEntry]:
    normalized = (scope or "core").lower()
    if normalized == "core":
        return list(CORE_JOURNAL_REGISTRY)
    if normalized == "context":
        return list(CONTEXT_JOURNAL_REGISTRY)
    if normalized in {"core_context", "all"}:
        return list(ALL_JOURNAL_REGISTRY)
    return list(CORE_JOURNAL_REGISTRY)


def find_journal(name: str) -> JournalEntry | None:
    needle = name.strip().lower()
    for entry in ALL_JOURNAL_REGISTRY:
        names = (entry.canonical_name, *entry.aliases)
        if any(needle == item.lower() for item in names):
            return entry
    return None