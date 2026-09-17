from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from .models import Node, Source

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_SUBDIRS = ("holders", "orgs")
_SOURCES_SUBDIR = "sources"

_PUNCT = re.compile(r"[.\u00a7]")
_SPACE = re.compile(r"\s+")


def normalize_citation(citation: str) -> str:
    """Fold the punctuation styles the same citation is written in, so "11 CFR Part 114" and
    "11 C.F.R. Part 114" match. Applied to BOTH sides of a comparison, never stored."""
    return _SPACE.sub(" ", _PUNCT.sub("", citation)).strip().casefold()


def duplicate_of(
    sources: Mapping[str, Source], canonical_url: str, point_in_time: date | None
) -> str | None:
    """The xr_id already pinning this exact document version, or None.

    The single definition of "same document version": one URL at one point_in_time is one set of
    bytes, so a second pin of it is a duplicate rather than a new version. Both callers go through
    here — `pin add` to refuse before it fetches or archives anything, and
    `check_source_invariants` to enforce the rule on load — so the cheap early check and the
    authoritative one cannot drift apart.
    """
    for source in sources.values():
        if source.canonical_url == canonical_url and source.point_in_time == point_in_time:
            return source.xr_id
    return None


def check_source_invariants(
    sources: Mapping[str, Source], nodes: Mapping[str, Node] | None = None
) -> None:
    """Every cross-record rule the LOAD path enforces on sources. Raises ValueError on the first.

    Module-level, and taking the mappings explicitly, so `pin add` can run a would-be record
    through exactly these rules against "existing sources plus this one" BEFORE writing it. One
    implementation, one set of messages: that is what makes it impossible for the write path to
    produce a file the read path then refuses.
    """
    nodes = nodes or {}
    for source in sources.values():
        # Nodes and sources share one xr_ namespace.
        if source.xr_id in nodes:
            raise ValueError(f"duplicate xr_id {source.xr_id!r} (also a node)")
        # Source links resolve within the source namespace.
        for field in ("supersedes", "merged_into"):
            other = getattr(source, field)
            if other is not None and other not in sources:
                raise ValueError(
                    f"{source.xr_id} {field} {other!r} which is not a known source xr_id"
                )
        # A cited document must be recoverable. Once a register record points at a source, the
        # canonical URL is no longer the only reader of it — an argument now depends on that text,
        # and if the publisher replaces it with nothing archived, the citation degrades to a hash
        # that proves something changed and cannot say what it said.
        if source.cited_in and not source.archives:
            raise ValueError(f"{source.xr_id} is cited but has no archive copy")
    # One pin per (document, version), decided by duplicate_of so this rule has exactly one
    # definition. Each record is tested against the ones already accepted; the wording differs
    # from `add`'s because at load time neither record is "the new one", so both get named.
    accepted: dict[str, Source] = {}
    for source in sources.values():
        other = duplicate_of(accepted, source.canonical_url, source.point_in_time)
        if other is not None:
            raise ValueError(
                f"{source.canonical_url} at point_in_time {source.point_in_time} is pinned "
                f"by both {other} and {source.xr_id}"
            )
        accepted[source.xr_id] = source


class Crosswalk:
    """Loads crosswalk nodes and enforces cross-node invariants that a single record can't see."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
        self.nodes: dict[str, Node] = {}
        for sub in _SUBDIRS:
            d = self.data_dir / sub
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                node = Node.model_validate_json(p.read_text(encoding="utf-8"))
                if node.xr_id in self.nodes:
                    raise ValueError(f"duplicate xr_id {node.xr_id!r} (also in another file)")
                self.nodes[node.xr_id] = node
        # Sources (pinned documents) share the one xr_ id namespace with nodes but are a separate
        # kind of thing: an actor node resolves a person or org, a source resolves a document.
        self.sources: dict[str, Source] = {}
        d = self.data_dir / _SOURCES_SUBDIR
        if d.exists():
            for p in sorted(d.glob("*.json")):
                source = Source.model_validate_json(p.read_text(encoding="utf-8"))
                if source.xr_id in self.sources or source.xr_id in self.nodes:
                    raise ValueError(f"duplicate xr_id {source.xr_id!r} (also in another file)")
                self.sources[source.xr_id] = source
        self._check_invariants()

    def _check_invariants(self) -> None:
        # merged_into (when set) must resolve to a known node.
        for node in self.nodes.values():
            if node.merged_into is not None and node.merged_into not in self.nodes:
                raise ValueError(
                    f"{node.xr_id} merged_into {node.merged_into!r} which is not a known xr_id"
                )
        # A given (register, local_id) maps to at most one node: one actor can't be two identities.
        # NOTE: Source.cited_in deliberately does NOT enter this dict. Many documents may cite the
        # same record (a single conflict record can cite a statute, a regulation and an opinion),
        # so a repeated (register, local_id) across sources is normal, not a collision.
        seen: dict[tuple[str, str], str] = {}
        for node in self.nodes.values():
            for ref in node.registers:
                key = (ref.register, ref.local_id)
                if key in seen:
                    raise ValueError(
                        f"local_id {ref.register}:{ref.local_id} appears on both "
                        f"{seen[key]} and {node.xr_id}"
                    )
                seen[key] = node.xr_id
        check_source_invariants(self.sources, self.nodes)

    def resolve(self, register: str, local_id: str) -> Node | None:
        """Return the node an external (register, local_id) reference resolves to, or None."""
        for node in self.nodes.values():
            for ref in node.registers:
                if ref.register == register and ref.local_id == local_id:
                    return node
        return None

    def resolve_source(self, citation: str, *, as_of: date | None = None) -> Source | None:
        """Return the pin a citation resolves to, or None.

        Without `as_of`: the latest pin, ranked by point_in_time, falling back to published_at, then
        to the fetch date; ties break on the higher xr_id (the later mint). With `as_of`: the latest
        pin whose point_in_time is on or before that date — only pins that HAVE a point_in_time are
        eligible, since a document with no version axis has no answer to "as of when".

        `merged_into` losers are excluded (they were wrong). Superseded pins stay reachable:
        `as_of` is how you reach the history of an amended text.
        """
        want = normalize_citation(citation)
        candidates = [
            s
            for s in self.sources.values()
            if s.merged_into is None and normalize_citation(s.citation) == want
        ]
        if as_of is not None:
            candidates = [
                s for s in candidates if s.point_in_time is not None and s.point_in_time <= as_of
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: (_rank_date(s), s.xr_id))


def _rank_date(source: Source) -> date:
    if source.point_in_time is not None:
        return source.point_in_time
    if source.published_at is not None:
        return source.published_at
    return source.artifact.fetched_at.date()
