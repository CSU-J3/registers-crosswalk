from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import RegisterRef
from ..registry import Crosswalk
from .models import EntityView, RegisterSection, SourceState


@runtime_checkable
class RegisterResolver(Protocol):
    """One per register, because VI, CP, and Sovereign store records three different ways. The
    resolver reads sibling files under `root` (a checkout of that register) — the same file-by-path
    approach refresolve/check_refs already use to cross repos, so no sibling package import and no
    re-implemented schema beyond the field keys each resolver documents."""

    register: str

    def resolve(self, ref: RegisterRef, root: Path) -> RegisterSection: ...


def join(
    xr_id: str,
    resolvers: dict[str, RegisterResolver],
    roots: dict[str, Path],
    *,
    source_state: SourceState = "working_tree",
    data_dir: Path | None = None,
) -> EntityView:
    """Resolve an xr_id into each referenced register's typed view.

    `roots` maps register -> checkout root. working_tree points them at local working trees;
    committed points them at fetched `main` (e.g. cross-repo.yml's _siblings/<reg>). The assembler
    itself is state-agnostic; source_state only tags which truth the roots represent.
    """
    node = Crosswalk(data_dir).nodes.get(xr_id)
    if node is None:
        raise KeyError(f"no crosswalk node {xr_id!r}")

    sections: list[RegisterSection] = []
    for ref in node.registers:
        resolver = resolvers.get(ref.register)
        if resolver is None:
            raise KeyError(f"no resolver registered for register {ref.register!r}")
        root = roots.get(ref.register)
        if root is None:
            raise KeyError(f"no checkout root given for register {ref.register!r}")
        sections.append(resolver.resolve(ref, root))

    return EntityView(
        xr_id=node.xr_id,
        canonical_name=node.canonical_name,
        kind=node.kind,
        source_state=source_state,
        sections=sections,
    )
