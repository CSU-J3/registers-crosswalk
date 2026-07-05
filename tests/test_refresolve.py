import json
from pathlib import Path

import pytest

from registers_crosswalk.refresolve import check_refs, iter_node_refs, resolve_ref


def test_resolve_vi_identity(tmp_path):
    (tmp_path / "data/officials").mkdir(parents=True)
    (tmp_path / "data/officials/vi_official_0001.json").write_text("{}", encoding="utf-8")
    assert resolve_ref("vi", "identity", "vi_official_0001", tmp_path)
    assert not resolve_ref("vi", "identity", "vi_official_9999", tmp_path)


def test_resolve_cp_identity_either_dir(tmp_path):
    (tmp_path / "data/entities").mkdir(parents=True)
    (tmp_path / "data/entities/cp_entity_0004.json").write_text("{}", encoding="utf-8")
    assert resolve_ref("cp", "identity", "cp_entity_0004", tmp_path)


def test_resolve_sovereign_record_mention_scans_records_json(tmp_path):
    (tmp_path / "web/data").mkdir(parents=True)
    (tmp_path / "web/data/records.json").write_text('[{"id": "SC-007"}]', encoding="utf-8")
    assert resolve_ref("sovereign", "record_mention", "SC-007", tmp_path)
    assert not resolve_ref("sovereign", "record_mention", "SC-999", tmp_path)


def test_resolve_unknown_rule_raises(tmp_path):
    with pytest.raises(ValueError, match="no resolution rule"):
        resolve_ref("sovereign", "identity", "PIF", tmp_path)


def _seed_data(dir_: Path) -> Path:
    data = dir_ / "data"
    (data / "holders").mkdir(parents=True)
    node = {
        "xr_id": "xr_holder_0001",
        "kind": "holder",
        "canonical_name": "x",
        "registers": [
            {
                "register": "vi",
                "local_id": "vi_official_0001",
                "ref_type": "identity",
                "display_label": "x",
            }
        ],
    }
    (data / "holders/xr_holder_0001.json").write_text(json.dumps(node), encoding="utf-8")
    return data


def test_iter_node_refs(tmp_path):
    data = _seed_data(tmp_path)
    refs = list(iter_node_refs(data))
    assert refs == [("xr_holder_0001", "vi", "identity", "vi_official_0001")]


def test_check_refs_reports_missing_then_resolves(tmp_path):
    data = _seed_data(tmp_path)
    vi_root = tmp_path / "vi"
    vi_root.mkdir()
    problems = check_refs(data, {"vi": vi_root})
    assert problems and "vi_official_0001" in problems[0]

    (vi_root / "data/officials").mkdir(parents=True)
    (vi_root / "data/officials/vi_official_0001.json").write_text("{}", encoding="utf-8")
    assert check_refs(data, {"vi": vi_root}) == []


def test_check_refs_skips_register_absent_from_roots(tmp_path):
    data = _seed_data(tmp_path)
    # no "vi" root supplied -> its ref is skipped, not failed
    assert check_refs(data, {}) == []
