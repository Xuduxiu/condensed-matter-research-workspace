# Epitaph Fireworks

Project ID: `EFS0P4M9CQ4SFYNN`.

Read `README.md`, `docs/PROOF.md`, and `verification/result.json` before changing the project. Work from this directory. The rest of the repository contains separate projects.

## Goal

Classify exactly the pairs of nearest-neighbour and next-nearest-neighbour disagreement counts of balanced finite binary rings, and verify the full theorem in Lean 4.19.0. Both directions matter: every word obeys the classification, and every admitted pair has a word realizing it.

## Proof requirements

- Keep the statistics defined on actual Boolean words. A formula on run lengths is an intermediate result until its connection to those words is proved.
- Cover arbitrary positive ring size, including the two-site ring. Finite enumeration is a diagnostic and does not prove the general theorem.
- Use Lean/Std only unless an additional dependency has a clear mathematical need. Pin any new dependency.
- All shipped Lean modules must compile with warnings treated as errors. Do not use unfinished proofs, additional axioms, native decision procedures, or code that bypasses kernel checking.
- Preserve the top-level theorem statement. A change to definitions or assumptions must be explained in the mathematical notes and re-reviewed.
- Run `python3 scripts/verify.py` from a clean build before declaring a milestone verified. Keep the generated logs, theorem axiom report, and source hashes.
- Say precisely which theorem passed. Do not label finite tests, an unexecuted proof sketch, or a conjecture as verified.

## Attribution and continuation

The current result is a small combinatorial classification and formalization. No claim of mathematical priority or first formalization is made. Cite the related run/autocorrelation literature. Record AI assistance accurately.

`docs/NEXT.md` contains potential research beyond this theorem. These are separate work items and do not change the acceptance criteria for the present result.
