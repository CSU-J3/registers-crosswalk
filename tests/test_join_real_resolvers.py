from __future__ import annotations

import json
from pathlib import Path

import pytest

from registers_crosswalk.join import (
    CPResolver,
    CPSection,
    EntityView,
    SovereignResolver,
    SovereignSection,
    VIResolver,
    VISection,
    join,
)

# Wiring test: the REAL VI/CP/Sovereign resolvers through join(), which the FAKE resolvers in
# test_join.py never exercise. The data is still synthetic — trees built in tmp_path, keyed to the
# ACTUAL local_ids the seed crosswalk nodes reference (vi_official_0001, cp_person_0003, SC-007,
# vi_recipient_0002) — so this proves the three resolvers compose through node load -> dispatch ->
# each resolver's real cross-file reads -> typed EntityView, without re-proving each resolver's
# internals (the per-resolver tests already own that). The complementary real-checkout gate that
# reads live sibling data lives in tests/integration/test_join_siblings.py; a synthetic tree that
# owns its own schema can't catch a sibling renaming a field, which is what that gate is for.

DATA = Path(__file__).resolve().parents[1] / "data"


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _vi_checkout(root: Path) -> None:
    # vi_official_0001 (the seed node's VI ref) has one counted conflict naming a recipient/holding,
    # plus a documented-not-counted conflict that must be filtered out of the joined view.
    _write(
        root / "data" / "conflicts" / "vi_conflict_0001.json",
        {
            "id": "vi_conflict_0001",
            "official_id": "vi_official_0001",
            "recipient_id": "vi_recipient_0001",
            "holding_id": "vi_holding_0001",
            "counting_status": "counted",
            "sector_authority": {"covers": True, "office": "Fake Office"},
            "as_of": "Fake FY test",
        },
    )
    _write(
        root / "data" / "conflicts" / "vi_conflict_0009.json",
        {
            "id": "vi_conflict_0009",
            "official_id": "vi_official_0001",
            "recipient_id": "vi_recipient_0009",
            "counting_status": "documented",
        },
    )
    _write(
        root / "data" / "recipients" / "vi_recipient_0001.json",
        {"id": "vi_recipient_0001", "name": "Acme Fake Corp", "federal_revenue_share": 0.42},
    )
    _write(
        root / "data" / "holdings" / "vi_holding_0001.json",
        {"id": "vi_holding_0001", "holding_mode": "managed_direct"},
    )
    # vi_recipient_0002 (xr_org_0001's VI ref) resolved as an identity ref: no conflict names it as
    # the official, so the joined VI section is scored with zero counted conflicts.


def _cp_checkout(root: Path) -> None:
    # cp_person_0003 (the seed node's CP ref) is the filer of cp_filing_0003; a filing by another
    # filer must not attach.
    _write(
        root / "data" / "persons" / "cp_person_0003.json",
        {
            "person_id": "cp_person_0003",
            "canonical_name": "Test Filer Anchor",
            "last_updated_from_filing": "cp_filing_0003",
            "merged_into": None,
        },
    )
    _write(
        root / "data" / "filings" / "cp_filing_0003.json",
        {
            "filing_id": "cp_filing_0003",
            "filer_id": "cp_person_0003",
            "filing_type": "OGE_Form_278e_Termination",
            "interest_disclosed": {
                "report_type": "Termination",
                "filer_position": "Test Position",
                "selected_entries_recorded_in_registry": [
                    {"registry_relationship": "cp_rel_0001"},
                    {"registry_relationships": ["cp_rel_0002"]},
                ],
            },
        },
    )
    _write(
        root / "data" / "filings" / "cp_filing_0099.json",
        {"filing_id": "cp_filing_0099", "filer_id": "cp_person_0099"},
    )


def _sovereign_checkout(root: Path) -> None:
    # SC-007 (the seed node's Sovereign ref) plus an unrelated record that must not match.
    _write(
        root / "web" / "data" / "records.json",
        [
            {"id": "SC-001", "business": "Unrelated Co"},
            {
                "id": "SC-007",
                "business": "World Liberty Fake Financial",
                "family_member": "Test Family (beneficial interests)",
                "scope": "LIVE",
                "frameworks": ["EMOL", "OGE"],
                "evidence_category": [1, 2],
                "primary_sources": [{"label": "Test filing", "category": 1}],
            },
        ],
    )


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    vi, cp, sov = tmp_path / "vi", tmp_path / "cp", tmp_path / "sovereign"
    _vi_checkout(vi)
    _cp_checkout(cp)
    _sovereign_checkout(sov)
    return {"vi": vi, "cp": cp, "sovereign": sov}


REAL = {"vi": VIResolver(), "cp": CPResolver(), "sovereign": SovereignResolver()}


def test_holder_joins_real_resolvers_across_three_checkouts(roots: dict[str, Path]) -> None:
    view = join("xr_holder_0001", REAL, roots, data_dir=DATA)

    assert isinstance(view, EntityView)
    assert view.canonical_name == "Donald J. Trump"
    assert view.source_state == "working_tree"
    by_register = {s.register: s for s in view.sections}
    assert set(by_register) == {"vi", "cp", "sovereign"}
    assert isinstance(by_register["vi"], VISection)
    assert isinstance(by_register["cp"], CPSection)
    assert isinstance(by_register["sovereign"], SovereignSection)


def test_vi_section_resolves_counted_conflict_from_checkout(roots: dict[str, Path]) -> None:
    view = join("xr_holder_0001", REAL, roots, data_dir=DATA)
    vi = next(s for s in view.sections if s.register == "vi")

    # documented conflict filtered out; only the counted one survives, with recipient/holding
    # side-files read across three directories.
    assert [c.conflict_id for c in vi.counted_conflicts] == ["vi_conflict_0001"]
    conflict = vi.counted_conflicts[0]
    assert conflict.recipient_local_id == "vi_recipient_0001"
    assert conflict.recipient_name == "Acme Fake Corp"
    assert conflict.holding_mode == "managed_direct"
    assert conflict.federal_revenue_share == 0.42
    assert conflict.sector_authority is not None
    assert conflict.sector_authority.covers is True


def test_cp_section_resolves_filer_and_own_filing(roots: dict[str, Path]) -> None:
    view = join("xr_holder_0001", REAL, roots, data_dir=DATA)
    cp = next(s for s in view.sections if s.register == "cp")

    assert cp.filer is not None
    assert cp.filer.person_id == "cp_person_0003"
    assert cp.filer.canonical_name == "Test Filer Anchor"
    assert [f.filing_id for f in cp.filings] == ["cp_filing_0003"]  # not the other filer's
    assert cp.filings[0].derived_relationship_ids == ["cp_rel_0001", "cp_rel_0002"]


def test_sovereign_section_resolves_record_mention(roots: dict[str, Path]) -> None:
    view = join("xr_holder_0001", REAL, roots, data_dir=DATA)
    sov = next(s for s in view.sections if s.register == "sovereign")

    assert sov.ref_type == "record_mention"  # never upgraded to identity
    assert sov.record_id == "SC-007"
    assert sov.record is not None
    assert sov.record.business == "World Liberty Fake Financial"
    assert sov.record.frameworks == ["EMOL", "OGE"]
    assert sov.record.primary_source_count == 1


def test_vi_only_org_joins_single_real_section(roots: dict[str, Path]) -> None:
    # xr_org_0001 (Palantir) references only vi_recipient_0002 as an identity ref. No conflict names
    # it as the official, so the real VIResolver returns a scored, empty-conflict view — and no
    # CP/Sovereign sections are fabricated.
    view = join("xr_org_0001", REAL, roots, data_dir=DATA)

    assert view.kind == "org"
    assert [s.register for s in view.sections] == ["vi"]
    vi = view.sections[0]
    assert isinstance(vi, VISection)
    assert vi.local_id == "vi_recipient_0002"
    assert vi.scored is True
    assert vi.counted_conflicts == []


def test_committed_source_state_passes_through_real_join(roots: dict[str, Path]) -> None:
    view = join("xr_holder_0001", REAL, roots, source_state="committed", data_dir=DATA)
    assert view.source_state == "committed"
