import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from registers_crosswalk.models import Node, RegisterRef
from registers_crosswalk.registry import Crosswalk

ROOT = Path(__file__).resolve().parents[1]


def _xw() -> Crosswalk:
    return Crosswalk(ROOT / "data")


def _ref(**over):
    base = dict(register="vi", local_id="vi_official_0001", ref_type="identity", display_label="x")
    base.update(over)
    return RegisterRef(**base)


def _ref_dict(**over) -> dict:
    base = dict(register="vi", local_id="vi_official_0001", ref_type="identity", display_label="x")
    base.update(over)
    return base


def _write_node(d: Path, **node) -> None:
    (d / "holders").mkdir(parents=True, exist_ok=True)
    (d / "orgs").mkdir(parents=True, exist_ok=True)
    sub = "holders" if node["kind"] == "holder" else "orgs"
    (d / sub / f"{node['xr_id']}.json").write_text(json.dumps(node), encoding="utf-8")


def test_loads_seed_nodes():
    assert set(_xw().nodes) == {"xr_holder_0001", "xr_org_0001"}


def test_resolve_by_local_id():
    xw = _xw()
    assert xw.resolve("vi", "vi_official_0001").xr_id == "xr_holder_0001"
    assert xw.resolve("cp", "cp_person_0003").xr_id == "xr_holder_0001"
    assert xw.resolve("sovereign", "SC-007").xr_id == "xr_holder_0001"
    assert xw.resolve("vi", "vi_recipient_0002").xr_id == "xr_org_0001"
    assert xw.resolve("vi", "vi_official_9999") is None


def test_palantir_cik_in_external_ids_not_key():
    org = _xw().nodes["xr_org_0001"]
    assert [e.value for e in org.external_ids if e.scheme == "cik"] == ["0001321655"]


def test_register_ref_dumps_json_key_register_not_source_register():
    # load -> dump -> the emitted key is "register" (never "source_register"), and it reloads.
    node = _xw().nodes["xr_holder_0001"]
    dumped = node.model_dump()
    for ref in dumped["registers"]:
        assert "register" in ref
        assert "source_register" not in ref
    assert '"register"' in node.model_dump_json()
    assert Node.model_validate(dumped).xr_id == "xr_holder_0001"


def test_sovereign_identity_ref_rejected():
    # Sovereign has no identity records yet; an identity ref must not validate.
    with pytest.raises(ValidationError):
        _ref(register="sovereign", local_id="PIF", ref_type="identity")


def test_sovereign_record_mention_ok():
    ref = _ref(register="sovereign", local_id="SC-007", ref_type="record_mention")
    assert ref.local_id == "SC-007"


def test_bad_local_id_pattern_rejected():
    # a filing id is record_mention, not identity, for CP
    with pytest.raises(ValidationError):
        _ref(register="cp", local_id="cp_filing_0003", ref_type="identity")


def test_kind_must_agree_with_id():
    with pytest.raises(ValidationError):
        Node(xr_id="xr_org_0002", kind="holder", canonical_name="x", registers=[_ref()])


def test_unknown_external_scheme_rejected():
    with pytest.raises(ValidationError):
        Node(
            xr_id="xr_org_0003",
            kind="org",
            canonical_name="x",
            external_ids=[{"scheme": "lei", "value": "z"}],
            registers=[_ref(register="vi", local_id="vi_recipient_0009")],
        )


def test_duplicate_local_id_across_nodes_rejected(tmp_path):
    ref = _ref_dict()
    _write_node(
        tmp_path, xr_id="xr_holder_0001", kind="holder", canonical_name="A", registers=[ref]
    )
    _write_node(
        tmp_path, xr_id="xr_holder_0002", kind="holder", canonical_name="B", registers=[ref]
    )
    with pytest.raises(ValueError, match="appears on both"):
        Crosswalk(tmp_path)


def test_dangling_merged_into_rejected(tmp_path):
    _write_node(
        tmp_path,
        xr_id="xr_holder_0001",
        kind="holder",
        canonical_name="A",
        registers=[_ref_dict()],
        merged_into="xr_holder_9999",
    )
    with pytest.raises(ValueError, match="not a known xr_id"):
        Crosswalk(tmp_path)
