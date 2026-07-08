from __future__ import annotations

import os
from pathlib import Path

import pytest

from registers_crosswalk.join import (
    CPResolver,
    CPSection,
    SovereignResolver,
    SovereignSection,
    VIResolver,
    VISection,
    join,
)

pytestmark = pytest.mark.integration

# Real-checkout gate. The unit resolver tests and tests/test_join_real_resolvers.py all own
# synthetic tmp_path trees, so a sibling renaming a field its resolver reads (federal_revenue_share
# -> ...) keeps them green: the fixture holds the old key. This runs the real VI/CP/Sovereign
# resolvers against LIVE sibling checkouts under _siblings/{vi,cp,sovereign} and asserts the seed
# nodes still resolve, so a rename shows up red here. Reading a gitignored checkout (or a symlink to
# a working tree) is reading, not vendoring — nothing sibling-owned ever enters the crosswalk's
# committed tree. Skips the whole module when the checkouts aren't present, so a plain clone stays
# green.

REGISTERS = ("vi", "cp", "sovereign")


def _source_state() -> str:
    # A provenance tag, not behaviour: working_tree = local checkouts, committed = fetched main
    # (what cross-repo.yml sets). It only rides onto the EntityView; resolvers read the same files.
    return os.environ.get("XR_SOURCE_STATE", "working_tree")


@pytest.fixture(scope="module")
def roots() -> dict[str, Path]:
    base = Path(os.environ.get("XR_SIBLINGS_DIR", "_siblings")).resolve()
    dirs = {reg: base / reg for reg in REGISTERS}
    missing = [reg for reg, d in dirs.items() if not d.is_dir()]
    if missing:
        pytest.skip(f"siblings not present under {base} (missing: {', '.join(missing)})")
    return dirs


@pytest.fixture(scope="module")
def resolvers() -> dict[str, object]:
    return {"vi": VIResolver(), "cp": CPResolver(), "sovereign": SovereignResolver()}


@pytest.fixture(scope="module")
def holder_view(roots: dict[str, Path], resolvers: dict[str, object]):
    # data_dir=None -> the crosswalk's own packaged data/ (first-party node records).
    return join("xr_holder_0001", resolvers, roots, source_state=_source_state(), data_dir=None)


def test_source_state_rides_onto_view(holder_view) -> None:
    assert holder_view.source_state == _source_state()


def test_holder_resolves_all_three_registers(holder_view) -> None:
    by_register = {s.register: s for s in holder_view.sections}
    assert set(by_register) == {"vi", "cp", "sovereign"}  # pin: exact section set
    assert isinstance(by_register["vi"], VISection)
    assert isinstance(by_register["cp"], CPSection)
    assert isinstance(by_register["sovereign"], SovereignSection)
    # pin: ref_types are design decisions (sovereign stays record_mention until it mints keys).
    assert by_register["vi"].ref_type == "identity"
    assert by_register["cp"].ref_type == "identity"
    assert by_register["sovereign"].ref_type == "record_mention"


def test_vi_counts_both_geo_and_palantir(holder_view) -> None:
    vi = next(s for s in holder_view.sections if s.register == "vi")
    assert vi.counted_conflicts  # floor: non-empty, never an exact count
    by_recipient = {c.recipient_local_id: c for c in vi.counted_conflicts}
    # xr_holder_0001 carries two counted VI conflicts; assert both, not one.
    assert {"vi_recipient_0001", "vi_recipient_0002"} <= set(by_recipient)
    for rid in ("vi_recipient_0001", "vi_recipient_0002"):
        conflict = by_recipient[rid]
        assert conflict.counting_status == "counted"
        assert conflict.holding_mode == "managed_direct"  # pin: stable categorical
        assert conflict.source_mode == "disclosed"  # pin: stable categorical
        # never pin the share itself: it's an as-of and a new 10-K moves it (GEO went 62 -> 67).
        assert conflict.federal_revenue_share is not None


def test_cp_resolves_filer_and_own_filing(holder_view) -> None:
    cp = next(s for s in holder_view.sections if s.register == "cp")
    assert cp.filer is not None
    assert cp.filer.person_id == "cp_person_0003"  # pin: identity
    filings = {f.filing_id: f for f in cp.filings}
    assert "cp_filing_0003" in filings  # pin: the filing he filed
    filing = filings["cp_filing_0003"]
    assert filing.report_type == "Termination"  # pin: stable categorical
    assert filing.derived_relationship_ids  # floor: non-empty, count is a parser output
    # CP records scored/excluded at the cp_rel level, not on the person section.
    assert cp.scored is None
    assert cp.excluded_from_total is None


def test_sovereign_resolves_record_mention(holder_view) -> None:
    sov = next(s for s in holder_view.sections if s.register == "sovereign")
    assert sov.record_id == "SC-007"  # pin: identity
    assert sov.record is not None
    assert sov.record.business  # populated
    assert sov.record.frameworks  # populated
    assert sov.record.primary_source_count >= 1  # floor: sources accrete; a collapse to 0 is drift


def test_vi_only_org_resolves_single_real_section(
    roots: dict[str, Path], resolvers: dict[str, object]
) -> None:
    # xr_org_0001 (Palantir) is VI-only against real VI data: one section, no fabricated siblings.
    view = join("xr_org_0001", resolvers, roots, source_state=_source_state(), data_dir=None)
    assert view.kind == "org"
    assert [s.register for s in view.sections] == ["vi"]
    assert isinstance(view.sections[0], VISection)
