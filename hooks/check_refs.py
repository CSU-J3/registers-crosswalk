#!/usr/bin/env python3
"""Local cross-repo reference check (not runnable in CI — the siblings are separate repos).

Confirms every registers[].local_id resolves to a real record in the sibling register's working copy
on disk. Siblings are expected as directories next to this repo:
  ../Vested-Interests  ../Connected-Procurement  ../Sovereign-Connections
If a sibling is absent, its refs are skipped with a warning rather than failing the commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECTS = REPO.parent
VI = PROJECTS / "Vested-Interests"
CP = PROJECTS / "Connected-Procurement"
SOV = PROJECTS / "Sovereign-Connections"


def _any_exists(*paths: Path) -> bool:
    return any(p.exists() for p in paths)


def resolves(register: str, ref_type: str, local_id: str) -> bool | None:
    """True/False if checkable; None if the sibling repo is absent (skip)."""
    if register == "vi":
        if not VI.exists():
            return None
        if ref_type == "identity":
            return _any_exists(
                VI / "data" / "officials" / f"{local_id}.json",
                VI / "data" / "recipients" / f"{local_id}.json",
            )
        return (VI / "data" / "conflicts" / f"{local_id}.json").exists()
    if register == "cp":
        if not CP.exists():
            return None
        if ref_type == "identity":
            return _any_exists(
                CP / "data" / "persons" / f"{local_id}.json",
                CP / "data" / "entities" / f"{local_id}.json",
            )
        return (CP / "data" / "filings" / f"{local_id}.json").exists()
    if register == "sovereign":
        if not SOV.exists():
            return None
        records = SOV / "web" / "data" / "records.json"
        return records.exists() and f'"{local_id}"' in records.read_text(encoding="utf-8")
    return False


def main() -> int:
    problems: list[str] = []
    skipped: set[str] = set()
    for sub in ("holders", "orgs"):
        d = REPO / "data" / sub
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            node = json.loads(p.read_text(encoding="utf-8"))
            for ref in node.get("registers", []):
                ok = resolves(ref["register"], ref["ref_type"], ref["local_id"])
                if ok is None:
                    skipped.add(ref["register"])
                elif not ok:
                    problems.append(
                        f"{node['xr_id']}: {ref['register']}:{ref['local_id']} "
                        f"({ref['ref_type']}) not found in sibling repo"
                    )
    for reg in sorted(skipped):
        print(f"pre-commit: sibling repo for {reg!r} not on disk; skipped its refs")
    if problems:
        print("pre-commit: dangling cross-register refs:")
        for pr in problems:
            print(f"  - {pr}")
        return 1
    print("pre-commit: all checkable cross-register refs resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
