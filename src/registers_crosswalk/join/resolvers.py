from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import RegisterRef
from .models import (
    CPFiler,
    CPFiling,
    CPSection,
    SovereignRecord,
    SovereignSection,
    VIConflictRef,
    VISection,
    VISectorAuthority,
)


def _load_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class VIResolver:
    """VI is ours and these keys are confirmed against the VI conflict/recipient/holding schema
    (2026-07-05). A conflict names its official/recipient/holding by id; holding_mode lives on the
    holding, and federal_revenue_share/source_mode on the recipient, so resolving one conflict reads
    three files under the VI root. Reads stay defensive: a missing side-file leaves fields None.

    Given an identity ref (vi_official_NNNN), return the counted conflicts naming that official.
    """

    register = "vi"

    def resolve(self, ref: RegisterRef, root: Path) -> VISection:
        conflicts_dir = root / "data" / "conflicts"
        recipients_dir = root / "data" / "recipients"
        holdings_dir = root / "data" / "holdings"
        counted: list[VIConflictRef] = []

        if conflicts_dir.is_dir():
            for path in sorted(conflicts_dir.glob("vi_conflict_*.json")):
                conflict = _load_json(path) or {}
                if conflict.get("official_id") != ref.local_id:
                    continue
                if conflict.get("counting_status") != "counted":
                    continue
                counted.append(self._conflict_ref(conflict, path, recipients_dir, holdings_dir))

        return VISection(
            local_id=ref.local_id,
            ref_type=ref.ref_type,
            display_label=ref.display_label,
            note=ref.note,
            scored=True,
            excluded_from_total=False,
            counted_conflicts=counted,
        )

    def _conflict_ref(
        self,
        conflict: dict[str, Any],
        path: Path,
        recipients_dir: Path,
        holdings_dir: Path,
    ) -> VIConflictRef:
        recipient_id = conflict.get("recipient_id")
        holding_id = conflict.get("holding_id")
        recipient = _load_json(recipients_dir / f"{recipient_id}.json") if recipient_id else None
        holding = _load_json(holdings_dir / f"{holding_id}.json") if holding_id else None
        recipient = recipient or {}
        holding = holding or {}

        sector = conflict.get("sector_authority") or {}
        sector_authority = (
            VISectorAuthority(
                covers=bool(sector.get("covers", False)),
                office=sector.get("office"),
                authority_basis=sector.get("authority_basis"),
                determination_date=sector.get("determination_date"),
            )
            if sector
            else None
        )

        return VIConflictRef(
            conflict_id=conflict.get("id") or path.stem,
            recipient_local_id=recipient_id or "",
            recipient_name=recipient.get("name"),
            counting_status=conflict.get("counting_status", "counted"),
            holding_id=holding_id,
            holding_mode=holding.get("holding_mode"),
            federal_revenue_share=recipient.get("federal_revenue_share"),
            source_mode=recipient.get("source_mode"),
            determination_date=conflict.get("determination_date"),
            as_of=conflict.get("as_of"),
            sector_authority=sector_authority,
        )


class CPResolver:
    """Keys confirmed against the CP person/filing schema (2026-07-05). For an identity ref
    (cp_person_NNNN) load the person and the filings they filed (filer_id match). A cp_entity_NNNN
    identity ref has no schema yet, so it rides in raw; a record_mention (cp_filing_NNNN) loads that
    one filing. Note for xr_holder_0001: cp_person_0003 filed cp_filing_0003 (2021
    Termination 278, trust-pattern schema test) and is not a scored named-family member; his derived
    cp_rel_* relationships are excluded_from_total. So this is filer, not awardee links.
    """

    register = "cp"

    def resolve(self, ref: RegisterRef, root: Path) -> CPSection:
        filings_dir = root / "data" / "filings"
        filer: CPFiler | None = None
        filings: list[CPFiling] = []
        raw: dict[str, Any] | None = None

        if ref.ref_type == "record_mention":
            filing = _load_json(filings_dir / f"{ref.local_id}.json")
            if isinstance(filing, dict):
                filings.append(self._filing(filing))
        elif ref.local_id.startswith("cp_person_"):
            person = _load_json(root / "data" / "persons" / f"{ref.local_id}.json") or {}
            filer = CPFiler(
                person_id=ref.local_id,
                canonical_name=person.get("canonical_name"),
                last_updated_from_filing=person.get("last_updated_from_filing"),
                merged_into=person.get("merged_into"),
            )
            filings = self._filings_by_filer(filings_dir, ref.local_id)
        else:  # cp_entity_NNNN: schema not in hand, carry raw
            raw = _load_json(root / "data" / "entities" / f"{ref.local_id}.json")

        return CPSection(
            local_id=ref.local_id,
            ref_type=ref.ref_type,
            display_label=ref.display_label,
            note=ref.note,
            filer=filer,
            filings=filings,
            raw=raw,
        )

    def _filings_by_filer(self, filings_dir: Path, person_id: str) -> list[CPFiling]:
        if not filings_dir.is_dir():
            return []
        out: list[CPFiling] = []
        for path in sorted(filings_dir.glob("cp_filing_*.json")):
            filing = _load_json(path)
            if isinstance(filing, dict) and filing.get("filer_id") == person_id:
                out.append(self._filing(filing))
        return out

    @staticmethod
    def _filing(filing: dict[str, Any]) -> CPFiling:
        interest = filing.get("interest_disclosed") or {}
        rel_ids: list[str] = []
        for entry in interest.get("selected_entries_recorded_in_registry") or []:
            if entry.get("registry_relationship"):
                rel_ids.append(entry["registry_relationship"])
            rel_ids.extend(entry.get("registry_relationships") or [])
        return CPFiling(
            filing_id=filing.get("filing_id") or "",
            filing_type=filing.get("filing_type"),
            source_filing_date=filing.get("source_filing_date"),
            filer_position=interest.get("filer_position"),
            report_year=interest.get("report_year"),
            report_type=interest.get("report_type"),
            change_type=filing.get("change_type"),
            replaces_filing_id=filing.get("replaces_filing_id"),
            derived_relationship_ids=rel_ids,
        )


class SovereignResolver:
    """Keys confirmed against records.json (2026-07-05): a flat list of SC-NNN records. Match on
    `id` and type the record. Still record_mention only: sovereign_entities.json keys sovereign
    counterparties (PIF, MGX, ...), not the individuals who surface inside SC records, so the
    per-person identity upgrade isn't unlocked by it.
    """

    register = "sovereign"
    RECORDS_FILE = ("web", "data", "records.json")

    def resolve(self, ref: RegisterRef, root: Path) -> SovereignSection:
        records = _load_json(root.joinpath(*self.RECORDS_FILE))
        found = None
        if isinstance(records, list):
            found = next(
                (r for r in records if isinstance(r, dict) and r.get("id") == ref.local_id),
                None,
            )
        return SovereignSection(
            local_id=ref.local_id,
            ref_type=ref.ref_type,
            display_label=ref.display_label,
            note=ref.note,
            record_id=ref.local_id,
            record=self._record(found) if found else None,
        )

    @staticmethod
    def _record(record: dict[str, Any]) -> SovereignRecord:
        sources = record.get("primary_sources")
        sources = sources if isinstance(sources, list) else []
        return SovereignRecord(
            id=record.get("id") or "",
            business=record.get("business"),
            family_member=record.get("family_member"),
            scope=record.get("scope"),
            source=record.get("source"),
            period=record.get("period"),
            frameworks=record.get("frameworks") or [],
            evidence_category=record.get("evidence_category") or [],
            documented_amount=record.get("documented_amount"),
            summary=record.get("summary"),
            primary_source_count=len(sources),
            primary_sources=sources,
        )
