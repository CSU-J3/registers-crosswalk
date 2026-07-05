#!/usr/bin/env python3
"""Local pre-commit reference check (fast, working-tree based).

Confirms each registers[].local_id resolves in the sibling register's WORKING COPY on disk
(../Vested-Interests, ../Connected-Procurement, ../Sovereign-Connections). Absent siblings are
skipped. This is the fast local layer; the authoritative committed-state check runs in CI
(.github/workflows/cross-repo.yml). Both callers share one register->location mapping in
registers_crosswalk.refresolve — this hook only supplies working-tree roots.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from registers_crosswalk.refresolve import check_refs  # noqa: E402

PROJECTS = REPO.parent
CANDIDATES = {
    "vi": PROJECTS / "Vested-Interests",
    "cp": PROJECTS / "Connected-Procurement",
    "sovereign": PROJECTS / "Sovereign-Connections",
}


def main() -> int:
    roots = {reg: p for reg, p in CANDIDATES.items() if p.exists()}
    for reg in CANDIDATES:
        if reg not in roots:
            print(f"pre-commit: sibling repo for {reg!r} not on disk; skipped its refs")
    problems = check_refs(REPO / "data", roots)
    if problems:
        print("pre-commit: dangling cross-register refs (working tree):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("pre-commit: all checkable cross-register refs resolve (working tree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
