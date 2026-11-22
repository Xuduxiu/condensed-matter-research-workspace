#!/usr/bin/env python3
"""Independent finite diagnostic for the conjectured ring spectrum.

This is intentionally a direct bit-position implementation. It does not call
the composition construction or the Lean implementation, and it is not the
proof of the theorem for arbitrary length.
"""
from __future__ import annotations

import argparse
from itertools import combinations
import json
from math import comb
from pathlib import Path


def disagreements(word: list[int], distance: int) -> int:
    n = len(word)
    return sum(word[i] != word[(i + distance) % n] for i in range(n))


def predicted_spectrum(m: int) -> set[tuple[int, int]]:
    return {(2 * m, 0)} | {
        (2 * r, k)
        for r in range(1, m)
        for k in range(4, 4 * min(r, m - r) + 1, 2)
    }


def check(max_m: int) -> dict:
    rows = []
    examples = {}
    total = 0
    for m in range(1, max_m + 1):
        seen = set()
        count = 0
        for ones in combinations(range(2 * m), m):
            word = [0] * (2 * m)
            for i in ones:
                word[i] = 1
            pair = (disagreements(word, 1), disagreements(word, 2))
            seen.add(pair)
            count += 1
            if m == 6:
                examples.setdefault(f"{pair[0]},{pair[1]}", "".join(map(str, word)))
        expected = predicted_spectrum(m)
        if seen != expected or count != comb(2 * m, m):
            raise RuntimeError({"m": m, "missing": sorted(expected - seen),
                                "unexpected": sorted(seen - expected), "words": count})
        rows.append({"m": m, "length": 2 * m, "words_checked": count,
                     "attainable_pairs": [list(p) for p in sorted(seen)], "status": "PASS"})
        total += count
    return {"kind": "finite_diagnostic_not_general_proof", "max_m": max_m,
            "words_checked": total, "rows": rows, "length_12_examples": examples,
            "status": "PASS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-m", type=int, default=9)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_m <= 12:
        parser.error("choose 1 <= max-m <= 12; this diagnostic is deliberately bounded")
    result = check(args.max_m)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("kind", "max_m", "words_checked", "status")}))
