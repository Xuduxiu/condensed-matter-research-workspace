from __future__ import annotations


class ZoteroLocalError(RuntimeError):
    """Raised when the Zotero Local API cannot complete a requested action."""


class ZoteroConnectionError(ZoteroLocalError):
    """Raised when Zotero desktop is not reachable on the local API port."""
