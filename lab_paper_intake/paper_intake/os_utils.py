from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


def open_path(path: str) -> tuple[bool, str]:
    target = Path(path)
    if not target.exists():
        return False, f"Path does not exist: {target}"
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", str(target)], check=True)
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - UI helper must not crash Streamlit
        return False, str(exc)
