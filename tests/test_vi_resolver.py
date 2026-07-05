from __future__ import annotations

import json
from pathlib import Path

from registers_crosswalk.join.resolvers import VIResolver
from registers_crosswalk.models import RegisterRef

# Synthetic fixtures (fake ids/values, real VI keys) built in tmp_path. This tests VIResolver's real
# cross-file read and counted-only filter without copying VI's records into the crosswalk repo (the
# crosswalk never vendors sibling data).


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _vi_tree(root: Path) -> None:
    _write(
        root / "data" / "conflicts" / "vi_conflict_9001.json",
        {
            "id": "vi_conflict_9001",
            "official_id": "vi_official_9001",
            "holding_id": "vi_holding_9001",
            "recipient_id": "vi_recipient_9001",
            "sector_authority": {
                "covers": True,
                "office": "Fake Office",
                "authority_basis": "fake basis",
                "determination_date": "2026-01-01",
            },
            "counting_status": "counted",
            "determination_date": "2026-01-01",
            "as_of": "Fake FY test",
        },
    )
    # same official but documented-not-counted -> must be filtered out
    _write(
        root / "data" / "conflicts" / "vi_conflict_9002.json",
        {
            "id": "vi_conflict_9002",
            "official_id": "vi_official_9001",
            "recipient_id": "vi_recipient_9002",
            "counting_status": "documented",
        },
    )
    # counted but a different official -> must be filtered out
    _write(
        root / "data" / "conflicts" / "vi_conflict_9003.json",
        {
            "id": "vi_conflict_9003",
            "official_id": "vi_official_0002",
            "recipient_id": "vi_recipient_9001",
            "counting_status": "counted",
        },
    )
    _write(
        root / "data" / "recipients" / "vi_recipient_9001.json",
        {
            "id": "vi_recipient_9001",
            "name": "Acme Fake Corp",
            "federal_revenue_share": 0.5,
            "source_mode": "disclosed",
        },
    )
    _write(
        root / "data" / "holdings" / "vi_holding_9001.json",
        {"id": "vi_holding_9001", "holding_mode": "managed_direct"},
    )


def _ref() -> RegisterRef:
    return RegisterRef(
        register="vi",
        local_id="vi_official_9001",
        ref_type="identity",
        display_label="covered official (test)",
    )


def test_vi_resolver_maps_across_conflict_recipient_holding(tmp_path: Path) -> None:
    _vi_tree(tmp_path)
    section = VIResolver().resolve(_ref(), tmp_path)

    assert section.scored is True
    assert len(section.counted_conflicts) == 1  # counted + right official only
    conflict = section.counted_conflicts[0]
    assert conflict.conflict_id == "vi_conflict_9001"
    assert conflict.recipient_local_id == "vi_recipient_9001"
    assert conflict.recipient_name == "Acme Fake Corp"
    assert conflict.holding_mode == "managed_direct"  # from the holding file
    assert conflict.federal_revenue_share == 0.5  # from the recipient file
    assert conflict.source_mode == "disclosed"
    assert conflict.sector_authority is not None
    assert conflict.sector_authority.covers is True
    assert conflict.sector_authority.office == "Fake Office"


def test_vi_resolver_degrades_when_side_files_absent(tmp_path: Path) -> None:
    # A conflict with no recipient/holding files present: still returned, side fields None.
    _write(
        tmp_path / "data" / "conflicts" / "vi_conflict_9001.json",
        {
            "id": "vi_conflict_9001",
            "official_id": "vi_official_9001",
            "holding_id": "vi_holding_9001",
            "recipient_id": "vi_recipient_9001",
            "counting_status": "counted",
        },
    )
    section = VIResolver().resolve(_ref(), tmp_path)
    conflict = section.counted_conflicts[0]
    assert conflict.recipient_name is None
    assert conflict.holding_mode is None
    assert conflict.federal_revenue_share is None
    assert conflict.sector_authority is None


def test_vi_resolver_empty_when_no_conflicts_dir(tmp_path: Path) -> None:
    section = VIResolver().resolve(_ref(), tmp_path)
    assert section.counted_conflicts == []
