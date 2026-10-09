from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is installed from requirements
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = _runtime_base_dir()
WORKSPACE_ROOT = PROJECT_ROOT.parent


def candidate_env_paths(base_dir: Path | None = None) -> list[Path]:
    root = base_dir or PROJECT_ROOT
    paths = [root / "config" / ".env", root / ".env", root.parent / ".env"]
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve() if path.exists() else path
        if resolved not in seen:
            deduped.append(path)
            seen.add(resolved)
    return deduped


def load_runtime_env(base_dir: Path | None = None) -> None:
    # Highest-priority files are loaded first. Explicit process variables always win.
    for path in candidate_env_paths(base_dir):
        load_dotenv(path, override=False)


def _configured_path(name: str, default: Path) -> Path:
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else WORKSPACE_ROOT / path


load_runtime_env()
DATA_DIR = _configured_path("PAPER_INTAKE_DATA_DIR", PROJECT_ROOT / "data")
PDF_DIR = _configured_path("PAPER_INTAKE_PDF_DIR", DATA_DIR / "pdfs")
EXPORT_DIR = _configured_path("PAPER_INTAKE_EXPORT_DIR", DATA_DIR / "exports")
DB_PATH = _configured_path("PAPER_INTAKE_DB", DATA_DIR / "papers.db")
INBOX_DIR = _configured_path(
    "PAPER_INTAKE_INBOX",
    DATA_DIR / "inbox" / "trend_radar_download_tasks",
)


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)


def candidate_inbox_dirs() -> list[Path]:
    """Return the current inbox followed by compatible legacy locations."""
    candidates = [
        INBOX_DIR,
        WORKSPACE_ROOT / "data" / "inbox" / "trend_radar_download_tasks",
        PROJECT_ROOT / "data" / "inbox" / "trend_radar_download_tasks",
    ]
    output: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved not in seen:
            output.append(path)
            seen.add(resolved)
    return output


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    unpaywall_email: str | None
    request_timeout_seconds: float = 30.0

    @property
    def llm_available(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_model)


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return default
    return value


def get_settings() -> Settings:
    ensure_data_dirs()

    return Settings(
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=(
            os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        ).rstrip("/"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL") or "",
        unpaywall_email=os.getenv("UNPAYWALL_EMAIL") or None,
        request_timeout_seconds=_env_float(
            "PAPER_INTAKE_REQUEST_TIMEOUT_SECONDS",
            30.0,
            minimum=1.0,
            maximum=300.0,
        ),
    )
