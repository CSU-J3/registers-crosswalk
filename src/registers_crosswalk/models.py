from __future__ import annotations

import re
import warnings
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from .ids import REGISTER_ID_PATTERNS, SRC_ID, XrId

Register = Literal["vi", "cp", "sovereign"]
RefType = Literal["identity", "record_mention"]
Scheme = Literal["cik", "uei"]

# Registers with no per-entity identity records yet: only record_mention refs are allowed. Drop a
# register from this set once it mints stable per-entity keys (e.g. sovereign_entities.json ids).
MENTION_ONLY_REGISTERS: frozenset[str] = frozenset({"sovereign"})


class XrModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalId(XrModel):
    # Public registrar facts only (external truth, not any register's work product). Closed scheme
    # set — extend deliberately. CIK/UEI ride here; they are NEVER the crosswalk primary key.
    scheme: Scheme
    value: str
    source_url: str | None = None


# The field below is named `register` so the JSON key and model_dump() output are both "register"
# (clean round-trip, no alias that could leak a different key). That name shadows ABCMeta.register,
# inherited via pydantic's metaclass; we never call it, so the shadow is harmless. Suppress only
# that one class-creation warning rather than aliasing the field.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "register" .* shadows an attribute',
        category=UserWarning,
    )

    class RegisterRef(XrModel):
        register: Register
        local_id: str
        ref_type: RefType
        # Display-only human label. NEVER a join key: the join-relevant type is fully carried by
        # (register, ref_type) plus the local_id's own namespace. Free text so it can state
        # register-specific nuance (e.g. CP's "filer, excluded_from_total") without a brittle enum.
        display_label: str
        note: str | None = None

        @model_validator(mode="after")
        def _check(self) -> RegisterRef:
            reg = self.register
            if reg in MENTION_ONLY_REGISTERS and self.ref_type != "record_mention":
                raise ValueError(
                    f"{reg} has no identity records yet; use ref_type='record_mention' "
                    f"(got {self.ref_type!r} for local_id {self.local_id!r})"
                )
            pat = REGISTER_ID_PATTERNS.get((reg, self.ref_type))
            if pat is None:
                raise ValueError(f"no local_id pattern registered for {reg}/{self.ref_type}")
            if not pat.match(self.local_id):
                raise ValueError(
                    f"{reg}/{self.ref_type} local_id {self.local_id!r} fails {pat.pattern}"
                )
            return self


class Node(XrModel):
    xr_id: XrId
    kind: Literal["holder", "org"]
    canonical_name: str
    external_ids: list[ExternalId] = []
    registers: list[RegisterRef]
    merged_into: XrId | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Node:
        if not self.xr_id.startswith(f"xr_{self.kind}_"):
            raise ValueError(f"xr_id {self.xr_id!r} disagrees with kind {self.kind!r}")
        if not self.registers:
            raise ValueError(f"{self.xr_id} has no register refs")
        return self


# ---------------------------------------------------------------------------
# Sources: the same real-world DOCUMENT, resolved across registers.
#
# A source node stores facts ABOUT the document as an object — where it lives, when it was fetched,
# what its bytes hash to, who published it, when. It NEVER stores the document's content: no bytes,
# no quotes, no summary of what it says. Same anti-bleed rule the actor nodes follow: the crosswalk
# points; it does not hold. Blobs live in the consuming project or in the archive copy, and the
# artifact hash verifies either.
# ---------------------------------------------------------------------------

Fetcher = Literal[
    "ecfr", "federalregister", "govinfo", "uscode", "openfec", "courtlistener", "manual"
]
# Admiralty grading: source reliability A-F, information credibility 1-6. "A1" is an official
# primary text from its publisher of record.
Reliability = Literal["A", "B", "C", "D", "E", "F"]
Credibility = Literal[1, 2, 3, 4, 5, 6]
# What re-fetching compares. "sha256": the bytes must be identical. "last_amended": the markup is
# expected to churn (uscode.house.gov re-renders), so the signal is the latest date in the
# section's source credit — the parenthetical listing the enacting law and every law that amended
# it — which moves only when a law amends that section. The hash is only advisory there.
# "currency_date" was the uscode key until 2026-09-18, when the "laws in effect on" date it read
# was shown to be site-wide rather than per-section; it is deliberately not accepted any more.
DriftKey = Literal["sha256", "last_amended"]
ArchiveService = Literal["wayback", "perma", "govinfo"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Decision 1 (2026-09-17): this repo is public, so a URL we STORE must never carry a credential.
# govinfo and openfec build metadata URLs with ?api_key=; fetchers store the key-free content URL
# and re-attach the key from env at fetch time.
# Matches a query PARAMETER NAME that is, or ends in, a credential word. The optional
# `[^=&]*[^a-z]` prefix is what makes the boundary work: it lets "api_key", "access-key" and
# "auth_token" match while "monkey" and "turkey" do not, because the character before the
# credential word has to be a non-letter. Deliberately broad — refusing a URL is loud and fixable,
# leaking a key is neither.
_URL_SECRET = re.compile(
    r"(?i)[?&](?:[^=&]*[^a-z])?"
    r"(api_?key|access_?key|token|key|sig|signature|secret|x-amz-[a-z-]*)="
)


def check_public_url(url: str) -> str:
    """Return `url` if it is an http(s) URL carrying no API key; raise ValueError otherwise."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"canonical_url must be http(s): {url!r}")
    m = _URL_SECRET.search(url)
    if m is not None:
        raise ValueError(
            f"canonical_url carries a credential ({m.group(1)}=...); this repo is public. "
            "Store the key-free content URL and re-attach the key from env at fetch time."
        )
    return url


def _sha256_hex(v: str) -> str:
    if not _SHA256.match(v):
        raise ValueError(f"sha256 must be 64 lowercase hex chars, got {v!r}")
    return v


def _aware_utc(v: datetime) -> datetime:
    # A naive timestamp is ambiguous across the machines that run `pin`; normalize to UTC so two
    # pins of the same document are comparable.
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError(f"timestamp must be timezone-aware, got naive {v!r}")
    return v.astimezone(UTC)


Sha256 = Annotated[str, AfterValidator(_sha256_hex)]
AwareUtc = Annotated[datetime, AfterValidator(_aware_utc)]


class Grade(XrModel):
    reliability: Reliability
    credibility: Credibility

    def code(self) -> str:
        """The Admiralty code as normally written: "A1"."""
        return f"{self.reliability}{self.credibility}"


class Artifact(XrModel):
    """What the fetch found. Facts about the bytes — never the bytes."""

    sha256: Sha256
    byte_length: int
    media_type: str
    fetched_at: AwareUtc
    drift_key: DriftKey = "sha256"
    # The value re-fetching compares against: the sha256 again, or the source credit's latest date.
    drift_value: str


class ArchiveCopy(XrModel):
    service: ArchiveService
    url: str
    captured_at: datetime | None = None


# Same `register` field-name shadow as RegisterRef above — see that comment for why it is harmless
# and why we suppress the one warning rather than aliasing the field.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "register" .* shadows an attribute',
        category=UserWarning,
    )

    class CitationRef(XrModel):
        """A register record that cites this document.

        `ref_type` is fixed to "record_mention": a document is cited BY a record, it is never a
        register's identity record. Because that is a Literal, a sovereign `identity` ref is
        rejected by the type itself and no MENTION_ONLY_REGISTERS interaction is needed.
        """

        register: Register
        local_id: str
        ref_type: Literal["record_mention"]
        note: str | None = None

        @model_validator(mode="after")
        def _check(self) -> CitationRef:
            pat = REGISTER_ID_PATTERNS.get((self.register, self.ref_type))
            if pat is None:
                raise ValueError(
                    f"no local_id pattern registered for {self.register}/{self.ref_type}"
                )
            if not pat.match(self.local_id):
                raise ValueError(
                    f"{self.register}/{self.ref_type} local_id {self.local_id!r} "
                    f"fails {pat.pattern}"
                )
            return self


class Source(XrModel):
    xr_id: XrId
    kind: Literal["source"]
    # As the document cites itself: "11 C.F.R. Part 114", "60 FR 7862", "52 U.S.C. § 30116".
    # Matched through registry.normalize_citation, so punctuation style doesn't have to agree.
    citation: str
    title: str
    publisher: str | None = None
    # Key-free (decision 1). The document's own home, not a mirror.
    canonical_url: str
    fetcher: Fetcher
    # Whether THIS FETCHER's field mapping has been exercised against the live API, stamped from
    # the fetcher module onto every record it mints. It is a property of the fetcher, never a
    # judgement about the document — orthogonal to `grade`, which rates the SOURCE (the Supreme
    # Court is A1 whether or not our CourtListener parse was ever run). False means a silent
    # mapping error could be sitting in this record: a date read from the wrong key, a URL built
    # off an assumed base. Defaults False so a new fetcher is untrusted until someone runs it.
    fetcher_verified: bool = False
    verified_at: date | None = None
    # The version axis: eCFR's "as of", OLRC's "laws in effect on". None where the document has no
    # point-in-time semantics (a Federal Register notice is published once and never amended).
    point_in_time: date | None = None
    published_at: date | None = None
    artifact: Artifact
    grade: Grade
    archives: list[ArchiveCopy] = []
    cited_in: list[CitationRef] = []
    # An earlier pin of the SAME citation that this one replaces (an amendment chain). Both stay
    # valid: the earlier pin is still the right answer for an `as_of` before the amendment.
    supersedes: XrId | None = None
    # Duplicate resolution, same semantics as Node.merged_into: the loser is wrong, not merely old.
    merged_into: XrId | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Source:
        if not self.xr_id.startswith("xr_src_"):
            raise ValueError(f"xr_id {self.xr_id!r} disagrees with kind {self.kind!r}")
        for field in ("supersedes", "merged_into"):
            other = getattr(self, field)
            if other is not None and not SRC_ID.match(other):
                raise ValueError(f"{field} {other!r} is not an xr_src_NNNN id")
        if not self.citation.strip():
            raise ValueError(f"{self.xr_id} has an empty citation")
        # The claim and its date travel together: a bare `true` with no date is an assertion nobody
        # can audit, and a date with no claim is a date about nothing.
        if self.fetcher_verified and self.verified_at is None:
            raise ValueError(f"{self.xr_id} is fetcher_verified but carries no verified_at date")
        if not self.fetcher_verified and self.verified_at is not None:
            raise ValueError(
                f"{self.xr_id} carries verified_at {self.verified_at} but fetcher_verified is False"
            )
        check_public_url(self.canonical_url)
        return self
