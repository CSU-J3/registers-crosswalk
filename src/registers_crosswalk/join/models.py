from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import RefType

SourceState = Literal["working_tree", "committed"]


class JoinModel(BaseModel):
    # Output DTOs: forbid extras to catch typos, but not frozen (assembled incrementally).
    model_config = ConfigDict(extra="forbid")


class VISectorAuthority(JoinModel):
    """Gate-1 per-nexus justification carried on the VI conflict: does the office reach the
    recipient's sector, and on what basis. A counting condition, so it's part of the typed view."""

    covers: bool
    office: str | None = None
    authority_basis: str | None = None
    determination_date: str | None = None


class VIConflictRef(JoinModel):
    """Typed because VI is ours. Keys confirmed against the VI conflict/recipient/holding schema
    (2026-07-05). One conflict names recipient and holding by id, so the resolver reads three files:
    holding_mode is on the holding; federal_revenue_share and source_mode are on the recipient."""

    conflict_id: str
    recipient_local_id: str
    recipient_name: str | None = None
    counting_status: str
    holding_id: str | None = None
    holding_mode: str | None = None
    federal_revenue_share: float | None = None
    source_mode: str | None = None
    determination_date: str | None = None
    as_of: str | None = None
    sector_authority: VISectorAuthority | None = None


class CPFiling(JoinModel):
    """A 278e filing, as CP records it. Derived relationships are the cp_rel_* ids CP attributes to
    this filing; their excluded_from_total status lives on the cp_rel records, not here."""

    filing_id: str
    filing_type: str | None = None
    source_filing_date: str | None = None
    filer_position: str | None = None
    report_year: int | None = None
    report_type: str | None = None
    change_type: str | None = None
    replaces_filing_id: str | None = None
    derived_relationship_ids: list[str] = Field(default_factory=list)


class CPFiler(JoinModel):
    person_id: str
    canonical_name: str | None = None
    last_updated_from_filing: str | None = None
    merged_into: str | None = None


class SovereignRecord(JoinModel):
    id: str
    business: str | None = None
    family_member: str | None = None
    scope: str | None = None
    source: str | None = None
    period: str | None = None
    frameworks: list[str] = Field(default_factory=list)
    evidence_category: list[int] = Field(default_factory=list)
    documented_amount: str | None = None
    summary: str | None = None
    primary_source_count: int = 0
    primary_sources: list[dict[str, Any]] = Field(default_factory=list)


class RegisterSection(JoinModel):
    # `register` is declared on each concrete section (VISection/CPSection/SovereignSection) as a
    # fixed Literal, which also discriminates the Section union. Kept off the base so subclasses
    # don't shadow a parent field.
    local_id: str
    ref_type: RefType
    # Carried verbatim from the crosswalk ref. Never parsed for control flow (free text, and the
    # crosswalk forbids it as a join key); it rides along so the human meaning is present.
    display_label: str
    note: str | None = None
    # None where the register's record schema isn't mapped yet, so we don't assert a value we can't
    # source. VI sets these; CP/Sovereign leave them None until their schemas land.
    scored: bool | None = None
    excluded_from_total: bool | None = None


# `register` is a real field (it must show in JSON output and it discriminates the Section union),
# but naming a field `register` shadows ABCMeta.register inherited via pydantic's metaclass. That
# method is never called here, so suppress only that one class-creation warning — the same idiom the
# crosswalk's own RegisterRef uses for the identical case.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "register" .* shadows an attribute',
        category=UserWarning,
    )

    class VISection(RegisterSection):
        register: Literal["vi"] = "vi"
        counted_conflicts: list[VIConflictRef] = Field(default_factory=list)

    class CPSection(RegisterSection):
        register: Literal["cp"] = "cp"
        # identity ref (cp_person_NNNN): the filer plus the filings they filed. record_mention
        # (cp_filing_NNNN): the single filing. raw holds a cp_entity ref or anything unmodeled.
        # scored/excluded_from_total stay None: CP records that at the cp_rel level, not here, and
        # the crosswalk ref's display_label already states the filer status.
        filer: CPFiler | None = None
        filings: list[CPFiling] = Field(default_factory=list)
        raw: dict[str, Any] | None = None

    class SovereignSection(RegisterSection):
        register: Literal["sovereign"] = "sovereign"
        record_id: str
        # The SC-NNN record, typed. record_mention only until sovereign_entities.json mints
        # per-person keys (it keys sovereign counterparties like PIF/MGX today, a separate axis).
        record: SovereignRecord | None = None
        raw: dict[str, Any] | None = None


Section = VISection | CPSection | SovereignSection


class EntityView(JoinModel):
    xr_id: str
    canonical_name: str
    kind: Literal["holder", "org"]
    # The one truth this whole view reports: working_tree = local checkouts (may hold uncommitted
    # records), committed = fetched main (what cross-repo.yml checks). Set by the caller's roots.
    source_state: SourceState
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    sections: list[Section] = Field(default_factory=list)
