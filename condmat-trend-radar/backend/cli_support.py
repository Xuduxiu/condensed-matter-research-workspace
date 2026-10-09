from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def add_execution_arguments(
    parser: argparse.ArgumentParser,
    *,
    supports_apply: bool,
) -> None:
    if supports_apply:
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--apply", action="store_true", help="Commit changes")
        group.add_argument("--dry-run", action="store_true", help="Plan and validate without writes (default)")
    else:
        parser.add_argument("--dry-run", action="store_true", help="Explicit read-only mode")
    parser.add_argument("--verbose", action="store_true", help="Include record-level details")
    parser.add_argument("--log-file", type=Path, help="Write the complete JSON result to this file")


def emit_result(result: dict[str, Any], args: argparse.Namespace) -> None:
    full_text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    log_file = getattr(args, "log_file", None)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(full_text + "\n", encoding="utf-8")
    display = result
    if not getattr(args, "verbose", False):
        display = dict(result)
        plans = display.pop("plans", None)
        if plans is not None:
            display["plan_detail_count"] = len(plans)
            display["plan_details_omitted"] = True
    print(json.dumps(display, ensure_ascii=False, indent=2, default=str))
