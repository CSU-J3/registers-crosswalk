from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from .models import DriftKey, Node, Source

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_SUBDIRS = ("holders", "orgs")
_SOURCES_SUBDIR = "sources"

_PUNCT = re.compile(r"[.\u00a7]")
_SPACE = re.compile(r"\s+")


def normalize_citation(citation: str) -> str:
    """Fold the punctuation styles the same citation is written in, so "11 CFR Part 114" and
    "11 C.F.R. Part 114" match. Applied to BOTH sides of a comparison, never stored."""
    return _SPACE.sub(" ", _PUNCT.sub("", citation)).strip().casefold()


def title_adds_anything(source: Source) -> bool:
    """Whether the title says something the citation does not.

    Most fetchers build the title as the citation plus the version qualifier — "11 CFR Part 114,
    as of 2026-09-14", "52 U.S.C. § 30116 (prelim, laws in effect on 2026-09-18)" — so printing
    both set the citation down twice on every ecfr and uscode row. A Federal Register title
    ("Personal Use of Campaign Funds") or a case name ("Dunne v. United States") is the document's
    own name and is the case this predicate exists to keep.

    Compared through `normalize_citation`, not raw, because a title and its citation routinely
    disagree on punctuation style — "11 C.F.R. Part 114" against "11 CFR Part 114, as of …" — and
    a raw prefix test would call those two different strings and print the citation twice in two
    spellings, which is the duplication at its worst.

    Lives here, next to the comparison it is built on, because two callers ask this question of
    the same source — the status page's document cell and `to_ledger_markdown` — and a page that
    suppressed a title the ledger printed (or the reverse) would be describing one pin two ways.
    """
    return not normalize_citation(source.title).startswith(normalize_citation(source.citation))


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


def superseded_ids(sources: Mapping[str, Source]) -> set[str]:
    """The ids retired by some other pin naming them in `supersedes`."""
    return {s.supersedes for s in sources.values() if s.supersedes is not None}


def live_sources(sources: Mapping[str, Source]) -> dict[str, Source]:
    """The pins that still assert something about the world today, keyed by xr_id.

    The single definition of LIVE, because three readers ask it and a pin that counted as live for
    one and not another would mean the duplicate rule, the load-path invariant and `pin check` were
    each describing a different repo. A pin drops out for one of two reasons, and they are not the
    same reason: a `merged_into` loser was *wrong* (two records for one document, and this is the
    one we do not keep), while a pin another one names in `supersedes` was *right when it was made*
    and is now history. Neither still claims to be the current text, which is what live means.

    Liveness is a property of the whole set, never of a record on its own: nothing in a superseded
    pin's own file says it was retired — only the later pin's `supersedes` does — so every caller
    has to hand over every source, not the subset it happens to be walking.
    """
    superseded = superseded_ids(sources)
    return {
        xr_id: s
        for xr_id, s in sources.items()
        if s.merged_into is None and xr_id not in superseded
    }


def unverified_sources(sources: Mapping[str, Source]) -> list[Source]:
    """Pins minted by a fetcher whose field mapping has never been exercised against the live API.

    One definition, because two readers now ask it: `validate` counts them on the terminal line and
    the status page would otherwise re-derive the same predicate from the same field.
    """
    return [s for s in sources.values() if not s.fetcher_verified]


def unarchived_sources(sources: Mapping[str, Source]) -> list[Source]:
    """Pins with no archive copy: if the publisher replaces the document, the text is gone.

    Same reason as above for living here rather than in either caller.
    """
    return [s for s in sources.values() if not s.archives]


def same_document_of(
    sources: Mapping[str, Source], citation: str, drift_key: DriftKey, drift_value: str
) -> str | None:
    """The xr_id of the LIVE pin already holding this document, or None.

    The second definition of "already pinned", and the one `duplicate_of` cannot see: a document is
    the same document when its citation and its drift value agree, whatever URL or point_in_time it
    was reached by. uscode is why this is needed — its canonical URL has no version axis and its
    point_in_time is OLRC's site-wide "laws in effect on" date, which moves every few days without
    the section changing, so the URL rule waves a re-pin of an unchanged section straight through.

    Only LIVE pins count, as `live_sources` defines live: a superseded pin is history, which is
    exactly the chain this rule must not forbid. That is also where the override lives — naming a
    pin with `--supersedes` makes it not live, so the collision disappears. No separate force
    flag, because superseding is the assertion the operator is actually making.

    Both callers go through here — `pin add` to refuse before it archives anything, and
    `check_source_invariants` to enforce the rule on load — so the shortcut and the authoritative
    check cannot drift apart.
    """
    want = normalize_citation(citation)
    for source in live_sources(sources).values():
        if (
            normalize_citation(source.citation) == want
            and source.artifact.drift_key == drift_key
            and source.artifact.drift_value == drift_value
        ):
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
    # One LIVE pin per document, decided by same_document_of for the same reason: one definition.
    # `live_sources` is handed every source rather than the walk's accumulator, because a later
    # pin's `supersedes` retires an earlier one and file order says nothing about which came first.
    live: dict[str, Source] = {}
    for source in live_sources(sources).values():
        artifact = source.artifact
        other = same_document_of(live, source.citation, artifact.drift_key, artifact.drift_value)
        if other is not None:
            raise ValueError(
                f"{source.citation} with {artifact.drift_key} {artifact.drift_value} is live on "
                f"both {other} and {source.xr_id}: the same document pinned twice"
            )
        live[source.xr_id] = source


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
