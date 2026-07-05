from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

# Canonical register -> location mapping for resolving a local_id inside a checkout of a register.
# The fast pre-commit hook (hooks/check_refs.py) mirrors this against working-tree siblings; this
# module is what CI uses against freshly-fetched committed checkouts. Keep the two in sync if a
# register grows a new identity namespace.

# (register, ref_type) -> directories under the repo root where "<local_id>.json" would live.
_FILE_DIRS: dict[tuple[str, str], tuple[str, ...]] = {
    ("vi", "identity"): ("data/officials", "data/recipients"),
    ("vi", "record_mention"): ("data/conflicts",),
    ("cp", "identity"): ("data/persons", "data/entities"),
    ("cp", "record_mention"): ("data/filings",),
}

# (register, ref_type) -> a single file the local_id must appear in (quoted). Sovereign has no
# per-record files; records live inside one JSON array.
_IN_FILE: dict[tuple[str, str], str] = {
    ("sovereign", "record_mention"): "web/data/records.json",
}


def resolve_ref(register: str, ref_type: str, local_id: str, root: Path) -> bool:
    """True iff local_id exists in the register checkout rooted at `root`."""
    key = (register, ref_type)
    if key in _FILE_DIRS:
        return any((root / d / f"{local_id}.json").is_file() for d in _FILE_DIRS[key])
    if key in _IN_FILE:
        p = root / _IN_FILE[key]
        return p.is_file() and f'"{local_id}"' in p.read_text(encoding="utf-8")
    raise ValueError(f"no resolution rule for {register}/{ref_type}")


def iter_node_refs(data_dir: Path) -> Iterator[tuple[str, str, str, str]]:
    """Yield (xr_id, register, ref_type, local_id) for every ref in every node."""
    for sub in ("holders", "orgs"):
        d = data_dir / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            node = json.loads(p.read_text(encoding="utf-8"))
            for ref in node.get("registers", []):
                yield node["xr_id"], ref["register"], ref["ref_type"], ref["local_id"]


def check_refs(data_dir: Path, roots: dict[str, Path]) -> list[str]:
    """Return problem strings (empty = all refs resolve). `roots` maps register -> checkout root.

    A register absent from `roots` is skipped (its refs are not checked) — the caller decides
    whether a missing register is acceptable (pre-commit: yes; CI: no, it passes all three).
    """
    problems: list[str] = []
    for xr_id, register, ref_type, local_id in iter_node_refs(data_dir):
        root = roots.get(register)
        if root is None:
            continue
        if not resolve_ref(register, ref_type, local_id, root):
            problems.append(f"{xr_id}: {register}:{local_id} ({ref_type}) not found under {root}")
    return problems
