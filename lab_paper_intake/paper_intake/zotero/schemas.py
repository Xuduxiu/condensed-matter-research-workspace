from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoteroStatus:
    connected: bool
    error_message: str = ""
    base_url: str = "http://localhost:23119/api"