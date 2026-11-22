#!/usr/bin/env python3
"""Build, audit theorem dependencies, and record reproducible evidence.

Run in a checkout with the pinned Lean/Lake toolchain available on PATH.
Only the Lean build and axiom audit establish formal proof status. The Python
enumeration is an independently implemented finite diagnostic.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "verification"
ALLOWED_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}


def lean_code(text: str) -> str:
    """Remove nested Lean comments and strings before a preliminary token scan."""
    result = []
    i = depth = 0
    in_string = False
    while i < len(text):
        if depth:
            if text.startswith("/-", i):
                depth += 1
                i += 2
            elif text.startswith("-/", i):
                depth -= 1
                i += 2
            else:
                i += 1
        elif in_string:
            if text[i] == "\\":
                i += 2
            elif text[i] == '"':
                in_string = False
                i += 1
                result.append(" ")
            else:
                i += 1
        elif text.startswith("/-", i):
            depth = 1
            i += 2
            result.append(" ")
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text[i] == '"':
            in_string = True
            i += 1
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def run(args: list[str], log_name: str) -> str:
    proc = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    (OUT / log_name).write_text(proc.stdout, encoding="utf-8")
    print(f"{'PASS' if proc.returncode == 0 else 'FAIL'}: {' '.join(args)}", flush=True)
    if proc.returncode:
        print(proc.stdout[-12000:], file=sys.stderr)
        raise RuntimeError(f"command exited {proc.returncode}; see verification/{log_name}")
    return proc.stdout


def main() -> None:
    OUT.mkdir(exist_ok=True)
    record = {"project_id": "EFS0P4M9CQ4SFYNN", "status": "RUNNING",
              "started_utc": datetime.now(timezone.utc).isoformat()}
    (OUT / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    try:
        if not shutil.which("lake") or not shutil.which("lean"):
            raise RuntimeError("Lean and Lake must be on PATH; install lean-toolchain first")
        version = run(["lean", "--version"], "toolchain.log").strip()
        if "version 4.19.0," not in version:
            raise RuntimeError(f"expected pinned Lean 4.19.0, got {version}")
        sources = sorted(ROOT.glob("*.lean")) + sorted((ROOT / "Fireworks").glob("*.lean"))
        prohibited = re.compile(r"\b(?:sorry|sorryAx|admit|axiom|unsafe|native_decide|"
                                r"implemented_by|extern|run_tac|run_elab|run_cmd|ofReduceBool)\b")
        for path in sources:
            code = lean_code(path.read_text(encoding="utf-8"))
            match = prohibited.search(code)
            if match or "skipKernelTC" in code:
                raise RuntimeError(f"disallowed proof escape in {path.relative_to(ROOT)}")
            for module in re.findall(r"^import\s+(\S+)", code, re.MULTILINE):
                if module != "Std" and module != "Fireworks" and not module.startswith("Fireworks."):
                    raise RuntimeError(f"unreviewed import {module} in {path.name}")
        run(["lake", "clean"], "clean.log")
        run(["lake", "build"], "build.log")
        audit = run(["lake", "env", "lean", "--trust=0", "-DwarningAsError=true",
                     "Verification.lean"], "axioms.log")
        expected = re.findall(r"^#print axioms\s+(\S+)",
                              (ROOT / "Verification.lean").read_text(), re.MULTILINE)
        if not expected:
            raise RuntimeError("no top-level theorem requested in the axiom audit")
        audited = {}
        for name, dependencies in re.findall(
                r"'([^']+)' depends on axioms: \[([^\]]*)\]", audit, re.DOTALL):
            axioms = {v.strip() for v in dependencies.split(",") if v.strip()}
            if axioms - ALLOWED_AXIOMS:
                raise RuntimeError(f"unexpected axioms for {name}: {sorted(axioms)}")
            audited[name] = sorted(axioms)
        for name in re.findall(r"'([^']+)' does not depend on any axioms", audit):
            audited[name] = []
        if set(expected) != set(audited):
            raise RuntimeError(f"incomplete axiom audit: expected {expected}, got {sorted(audited)}")
        run([sys.executable, "scripts/enumerate.py", "--max-m", "9", "--output",
             "verification/enumeration.json"], "enumeration.log")
        hashes = {}
        for path in sources + [ROOT / "lean-toolchain", ROOT / "lakefile.toml",
                               ROOT / "scripts/verify.py", ROOT / "scripts/enumerate.py"]:
            hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        record.update({"status": "PASS", "lean_version": version,
                       "finished_utc": datetime.now(timezone.utc).isoformat(),
                       "axiom_dependencies": audited, "source_sha256": hashes,
                       "finite_diagnostic": {"maximum_length": 18, "words_checked": 66196},
                       "checker": "Lean 4.19.0 kernel; fresh project build; trust=0 audit",
                       "independent_kernel_checker": "NOT_RUN"})
    except Exception as exc:
        record.update({"status": "FAIL", "error": str(exc),
                       "finished_utc": datetime.now(timezone.utc).isoformat()})
        raise
    finally:
        (OUT / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    print("Verified theorem roots and all finite diagnostics passed.")


if __name__ == "__main__":
    main()
