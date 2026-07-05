from __future__ import annotations

from pathlib import Path

import pytest

from registers_crosswalk.join import (
    CPSection,
    EntityView,
    SovereignSection,
    VIConflictRef,
    VISection,
    join,
)
from registers_crosswalk.models import RegisterRef

DATA = Path(__file__).resolve().parents[1] / "data"


# Fake resolvers exercise the assembler (dispatch, typing, provenance) with no sibling checkout.
# The real resolvers are integration-tested once vi/cp/sovereign checkouts are present.
class FakeVI:
    register = "vi"

    def resolve(self, ref: RegisterRef, root: Path) -> VISection:
        return VISection(
            local_id=ref.local_id,
            ref_type=ref.ref_type,
            display_label=ref.display_label,
            note=ref.note,
            scored=True,
            excluded_from_total=False,
            counted_conflicts=[
                VIConflictRef(
                    conflict_id="vi_conflict_0001",
                    recipient_local_id="vi_recipient_0001",
                    recipient_name="The GEO Group, Inc.",
                    counting_status="counted",
                    holding_mode="managed_direct",
                ),
                VIConflictRef(
                    conflict_id="vi_conflict_0002",
                    recipient_local_id="vi_recipient_0002",
                    recipient_name="Palantir Technologies Inc.",
                    counting_status="counted",
                    holding_mode="managed_direct",
                ),
            ],
        )


class FakeCP:
    register = "cp"

    def resolve(self, ref: RegisterRef, root: Path) -> CPSection:
        return CPSection(
            local_id=ref.local_id,
            ref_type=ref.ref_type,
            display_label=ref.display_label,
            note=ref.note,
            raw={"cp_person_id": ref.local_id},
        )


class FakeSovereign:
    register = "sovereign"

    def resolve(self, ref: RegisterRef, root: Path) -> SovereignSection:
        return SovereignSection(
            local_id=ref.local_id,
            ref_type=ref.ref_type,
            display_label=ref.display_label,
            note=ref.note,
            record_id=ref.local_id,
            raw={"id": ref.local_id},
        )


FAKES = {r.register: r for r in (FakeVI(), FakeCP(), FakeSovereign())}
ROOTS = {
    "vi": Path("/nonexistent"),
    "cp": Path("/nonexistent"),
    "sovereign": Path("/nonexistent"),
}


def test_trump_resolves_all_three_axes() -> None:
    view = join("xr_holder_0001", FAKES, ROOTS, data_dir=DATA)
    assert isinstance(view, EntityView)
    assert view.canonical_name == "Donald J. Trump"
    assert view.source_state == "working_tree"
    by_register = {s.register: s for s in view.sections}
    assert set(by_register) == {"vi", "cp", "sovereign"}
    assert isinstance(by_register["vi"], VISection)
    assert isinstance(by_register["cp"], CPSection)
    assert isinstance(by_register["sovereign"], SovereignSection)


def test_vi_section_carries_counted_conflicts() -> None:
    view = join("xr_holder_0001", FAKES, ROOTS, data_dir=DATA)
    vi = next(s for s in view.sections if s.register == "vi")
    assert {c.conflict_id for c in vi.counted_conflicts} == {
        "vi_conflict_0001",
        "vi_conflict_0002",
    }
    assert all(c.counting_status == "counted" for c in vi.counted_conflicts)
    assert vi.scored is True


def test_sections_stay_typed_not_flattened() -> None:
    # The whole point of the JOIN: three heterogeneous relationships, never merged into one list.
    view = join("xr_holder_0001", FAKES, ROOTS, data_dir=DATA)
    sov = next(s for s in view.sections if s.register == "sovereign")
    assert sov.ref_type == "record_mention"
    assert sov.record_id == "SC-007"
    cp = next(s for s in view.sections if s.register == "cp")
    assert cp.ref_type == "identity"


def test_vi_only_org_has_single_section() -> None:
    # xr_org_0001 (Palantir) is VI-only: no fabricated empty CP/Sovereign sections.
    view = join("xr_org_0001", FAKES, ROOTS, data_dir=DATA)
    assert [s.register for s in view.sections] == ["vi"]
    assert view.kind == "org"


def test_committed_state_tag_passes_through() -> None:
    view = join("xr_holder_0001", FAKES, ROOTS, source_state="committed", data_dir=DATA)
    assert view.source_state == "committed"


def test_unknown_node_raises() -> None:
    with pytest.raises(KeyError):
        join("xr_holder_9999", FAKES, ROOTS, data_dir=DATA)


def test_missing_resolver_raises() -> None:
    with pytest.raises(KeyError):
        join("xr_holder_0001", {"vi": FakeVI()}, ROOTS, data_dir=DATA)
