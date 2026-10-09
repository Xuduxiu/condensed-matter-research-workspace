from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from paper_intake.config import EXPORT_DIR
from paper_intake.export_package import clean_export_runs


def main() -> int:
    removed = clean_export_runs(EXPORT_DIR)
    if not removed:
        print("No old export run folders found.")
        return 0
    print("Removed export runs:")
    for path in removed:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())