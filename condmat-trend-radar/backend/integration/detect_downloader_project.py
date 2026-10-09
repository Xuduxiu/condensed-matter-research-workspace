from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DETECT_NAMES = {"app.py", "requirements.txt", "README.md", "README_for_user.md"}
INPUT_HINTS = {"download", "pdf", "ris", "zotero", "exports", "manifest", "streamlit", "gradio", "fastapi"}
SKIP_DIRS = {".git", ".venv", "__pycache__", "releases", "dist", "build", "node_modules"}


def detect(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    candidates = []
    if not root_path.exists():
        return {"found": False, "root": str(root_path), "candidates": [], "message": "root_not_found"}
    for path in root_path.iterdir():
        if not path.is_dir() or path.name in SKIP_DIRS:
            continue
        score, files, hints = score_project(path)
        if score:
            candidates.append({"path": str(path), "score": score, "entry_files": files, "hints": sorted(hints)})
    if not candidates:
        score, files, hints = score_project(root_path)
        if score:
            candidates.append({"path": str(root_path), "score": score, "entry_files": files, "hints": sorted(hints)})
    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0] if candidates else None
    return {
        "found": bool(best),
        "candidate_project_path": best["path"] if best else None,
        "detected_entry_files": best["entry_files"] if best else [],
        "supported_input_formats": supported_formats(best["hints"] if best else []),
        "recommended_export_path": str(Path(best["path"]) / "data" / "inbox" / "trend_radar_download_tasks") if best else None,
        "candidates": candidates[:8],
    }


def score_project(path: Path) -> tuple[int, list[str], set[str]]:
    files = []
    hints: set[str] = set()
    score = 0
    for name in DETECT_NAMES:
        if (path / name).exists():
            files.append(name)
            score += 3
    search_files = []
    for pattern in ["*.py", "*.md", "*.txt"]:
        search_files.extend(list(path.glob(pattern))[:20])
    for file_path in search_files[:60]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for hint in INPUT_HINTS:
            if hint in text or hint in file_path.name.lower():
                hints.add(hint)
                score += 1
    if (path / "data" / "exports").exists():
        hints.add("exports")
        score += 3
    if (path / "data" / "pdfs").exists():
        hints.add("pdf")
        score += 3
    return score, files, hints


def supported_formats(hints: list[str] | set[str]) -> list[str]:
    output = ["csv", "json", "manifest"]
    hints_set = set(hints)
    if "ris" in hints_set or "zotero" in hints_set:
        output.append("ris/zotero-import")
    if "pdf" in hints_set:
        output.append("open-access-pdf-queue")
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect a local automatic paper downloader project.")
    parser.add_argument("--root", required=True)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(detect(parse_args().root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
