from __future__ import annotations

import json
from pathlib import Path

from registers_crosswalk.join.resolvers import CPResolver
from registers_crosswalk.models import RegisterRef

# Synthetic CP tree (fake ids, real keys) so the resolver's filer-match and derived-relationship
# extraction are tested without copying CP records into the crosswalk repo.


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _cp_tree(root: Path) -> None:
    _write(
        root / "data" / "persons" / "cp_person_9003.json",
        {
            "person_id": "cp_person_9003",
            "canonical_name": "Test Filer",
            "last_updated_from_filing": "cp_filing_9003",
            "merged_into": None,
        },
    )
    _write(
        root / "data" / "filings" / "cp_filing_9003.json",
        {
            "filing_id": "cp_filing_9003",
            "filer_id": "cp_person_9003",
            "filing_type": "OGE_Form_278e_Termination",
            "source_filing_date": "2021-01-20",
            "change_type": "original",
            "replaces_filing_id": None,
            "interest_disclosed": {
                "report_type": "Termination",
                "filer_position": "Test Position",
                "selected_entries_recorded_in_registry": [
                    {"schedule": "Part 2", "registry_relationship": "cp_rel_9001"},
                    {
                        "schedule": "Part 5",
                        "registry_relationships": ["cp_rel_9002", "cp_rel_9003"],
                    },
                ],
            },
        },
    )
    # a filing by a different filer -> must not attach to cp_person_9003
    _write(
        root / "data" / "filings" / "cp_filing_9004.json",
        {"filing_id": "cp_filing_9004", "filer_id": "cp_person_9001"},
    )


def test_cp_resolver_types_filer_and_filings(tmp_path: Path) -> None:
    _cp_tree(tmp_path)
    ref = RegisterRef(
        register="cp",
        local_id="cp_person_9003",
        ref_type="identity",
        display_label="filer, excluded_from_total",
    )
    section = CPResolver().resolve(ref, tmp_path)

    assert section.filer is not None
    assert section.filer.canonical_name == "Test Filer"
    assert len(section.filings) == 1  # only the filing this person filed
    filing = section.filings[0]
    assert filing.filing_id == "cp_filing_9003"
    assert filing.filing_type == "OGE_Form_278e_Termination"
    assert filing.report_type == "Termination"
    assert filing.filer_position == "Test Position"
    assert filing.derived_relationship_ids == ["cp_rel_9001", "cp_rel_9002", "cp_rel_9003"]
    # scored/excluded stay None: CP records that at person level, not here.
    assert section.scored is None
    assert section.excluded_from_total is None


def test_cp_resolver_record_mention_loads_single_filing(tmp_path: Path) -> None:
    _cp_tree(tmp_path)
    ref = RegisterRef(
        register="cp",
        local_id="cp_filing_9003",
        ref_type="record_mention",
        display_label="a filing",
    )
    section = CPResolver().resolve(ref, tmp_path)
    assert section.filer is None
    assert [f.filing_id for f in section.filings] == ["cp_filing_9003"]
