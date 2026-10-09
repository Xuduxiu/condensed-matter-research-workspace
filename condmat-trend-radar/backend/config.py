from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_PROJECT_DATA_DIR = PROJECT_ROOT / "data"
PREFERRED_REAL_DATA_DIR = Path("G:/condmat-trend-radar")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


@lru_cache(maxsize=1)
def _local_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    candidates: list[Path] = []
    configured_file = os.getenv("CONDMAT_RADAR_CONFIG_FILE", "").strip()
    if configured_file:
        configured_path = Path(configured_file).expanduser()
        candidates.append(
            configured_path
            if configured_path.is_absolute()
            else PROJECT_ROOT / configured_path
        )
    else:
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidates.append(
                Path(local_app_data) / "CondMatRadar" / "config" / ".env.local"
            )
        candidates.extend([
            PROJECT_ROOT / ".env.local",
            PROJECT_ROOT / ".env",
            REPO_ROOT / ".env",
            PREFERRED_REAL_DATA_DIR / "secrets" / ".env.local",
        ])
    for path in candidates:
        for key, value in _parse_env_file(path).items():
            values.setdefault(key, value)
    return values


def config_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    local_values = _local_env_values()
    for name in names:
        value = local_values.get(name)
        if value and value.strip():
            return value.strip()
    return None


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    clean = value.strip()
    if len(clean) <= 4:
        return "****"
    return f"{clean[:4]}****"


def env_path(*names: str) -> Path | None:
    value = config_value(*names)
    if value:
        path = Path(value)
        return path if path.is_absolute() else REPO_ROOT / path
    return None


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@lru_cache(maxsize=1)
def data_dir() -> Path:
    configured = env_path("CONDMAT_RADAR_DATA_DIR", "COND_MAT_RADAR_DATA_DIR")
    if configured:
        return configured
    if PREFERRED_REAL_DATA_DIR.drive and Path(PREFERRED_REAL_DATA_DIR.drive + "/").exists():
        return PREFERRED_REAL_DATA_DIR
    return DEFAULT_PROJECT_DATA_DIR


def processed_dir() -> Path:
    return data_dir() / "processed"


def raw_dir() -> Path:
    return data_dir() / "raw"


def cache_dir() -> Path:
    return env_path("CONDMAT_RADAR_CACHE", "COND_MAT_RADAR_CACHE") or data_dir() / "cache"


def cache_subdir(name: str) -> Path:
    return cache_dir() / name


def export_dir() -> Path:
    return env_path("CONDMAT_RADAR_EXPORT", "COND_MAT_RADAR_EXPORT") or data_dir() / "exports"


def logs_dir() -> Path:
    return env_path("CONDMAT_RADAR_LOGS", "COND_MAT_RADAR_LOGS") or data_dir() / "logs"


def locks_dir() -> Path:
    return env_path("CONDMAT_RADAR_LOCKS", "COND_MAT_RADAR_LOCKS") or data_dir() / "locks"


def download_tasks_dir() -> Path:
    return data_dir() / "download_tasks"


def pdf_queue_dir() -> Path:
    return data_dir() / "pdf_queue"


@lru_cache(maxsize=1)
def db_path() -> Path:
    configured = env_path("CONDMAT_RADAR_DB", "COND_MAT_RADAR_DB")
    if configured:
        return configured
    if data_dir() == PREFERRED_REAL_DATA_DIR:
        return data_dir() / "condmat_radar.sqlite"
    return processed_dir() / "condmat_trends.sqlite"


def paper_downloader_root() -> Path:
    configured = env_path("PAPER_DOWNLOADER_ROOT")
    if configured:
        return configured
    intake_project = REPO_ROOT / "lab_paper_intake"
    return intake_project if intake_project.exists() else REPO_ROOT


def paper_downloader_inbox() -> Path:
    return env_path("PAPER_DOWNLOADER_INBOX", "PAPER_INTAKE_INBOX") or (
        paper_downloader_root() / "data" / "inbox" / "trend_radar_download_tasks"
    )


def paper_downloader_inboxes() -> list[Path]:
    candidates = [
        paper_downloader_inbox(),
        REPO_ROOT / "data" / "inbox" / "trend_radar_download_tasks",
    ]
    output: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.expanduser().resolve()).casefold()
        if key not in seen:
            seen.add(key)
            output.append(path)
    return output


def openalex_mailto() -> str | None:
    return config_value("OPENALEX_MAILTO", "CONDMAT_RADAR_OPENALEX_EMAIL")


def unpaywall_email() -> str | None:
    return config_value(
        "UNPAYWALL_EMAIL",
        "OPENALEX_MAILTO",
        "CONDMAT_RADAR_OPENALEX_EMAIL",
    )


def institutional_access_mode() -> str:
    """Return the explicitly configured institutional-access mode.

    Only IP-based access is supported. Browser cookies, SSO credentials and
    proxy passwords are deliberately outside the downloader boundary.
    """
    value = config_value(
        "CONDMAT_RADAR_INSTITUTIONAL_ACCESS",
        "CONDMAT_RADAR_CAMPUS_ACCESS",
    )
    return str(value or "disabled").strip().casefold()


def institutional_ip_enabled() -> bool:
    return institutional_access_mode() in {"ip", "ip_only", "campus_ip", "enabled", "true", "1"}


def openalex_api_key() -> str | None:
    return config_value("OPENALEX_API_KEY", "CONDMAT_RADAR_OPENALEX_API_KEY")


def deepseek_api_key() -> str | None:
    return config_value("DEEPSEEK_API_KEY", "CONDMAT_RADAR_DEEPSEEK_API_KEY")


def deepseek_base_url() -> str:
    return config_value("DEEPSEEK_BASE_URL", "CONDMAT_RADAR_DEEPSEEK_BASE_URL") or "https://api.deepseek.com"


def deepseek_model_fast() -> str:
    return config_value("DEEPSEEK_MODEL_FAST", "CONDMAT_RADAR_DEEPSEEK_MODEL_FAST") or "deepseek-v4-flash"


def deepseek_model_pro() -> str:
    return config_value("DEEPSEEK_MODEL_PRO", "CONDMAT_RADAR_DEEPSEEK_MODEL_PRO") or "deepseek-v4-pro"


def ensure_data_layout() -> dict[str, str]:
    paths = {
        "data": data_dir(),
        "db_parent": db_path().parent,
        "cache": cache_dir(),
        "cache_openalex": cache_subdir("openalex"),
        "cache_crossref": cache_subdir("crossref"),
        "cache_arxiv": cache_subdir("arxiv"),
        "raw": raw_dir(),
        "processed": processed_dir(),
        "exports": export_dir(),
        "logs": logs_dir(),
        "locks": locks_dir(),
        "download_tasks": download_tasks_dir(),
        "pdf_queue": pdf_queue_dir(),
        "secrets": data_dir() / "secrets",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {key: str(path) for key, path in paths.items()}
