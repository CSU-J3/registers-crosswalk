import json
from datetime import date
from pathlib import Path

import pytest

from registers_crosswalk.refresolve import check_refs, iter_node_refs
from registers_crosswalk.registry import Crosswalk, normalize_citation

ARTIFACT = {
    "sha256": "a" * 64,
    "byte_length": 10,
    "media_type": "application/xml",
    "fetched_at": "2026-09-17T12:00:00Z",
    "drift_key": "sha256",
    "drift_value": "a" * 64,
}
GRADE = {"reliability": "A", "credibility": 1}

ECFR_URL = "https://www.ecfr.gov/api/versioner/v1/full/{}/title-11.xml?part=114"


def _write_source(data: Path, xr_id: str, **over) -> None:
    (data / "sources").mkdir(parents=True, exist_ok=True)
    source = {
        "xr_id": xr_id,
        "kind": "source",
        "citation": "11 C.F.R. Part 114",
        "title": "11 CFR Part 114",
        "canonical_url": ECFR_URL.format("2026-09-14"),
        "fetcher": "ecfr",
        "point_in_time": "2026-09-14",
        "artifact": {**ARTIFACT, "sha256": xr_id[-1] * 64, "drift_value": xr_id[-1] * 64},
        "grade": dict(GRADE),
    }
    source.update(over)
    (data / "sources" / f"{xr_id}.json").write_text(json.dumps(source), encoding="utf-8")


def _write_node(data: Path, xr_id: str, local_id: str = "vi_official_0001") -> None:
    (data / "holders").mkdir(parents=True, exist_ok=True)
    node = {
        "xr_id": xr_id,
        "kind": "holder",
        "canonical_name": "x",
        "registers": [
            {
                "register": "vi",
                "local_id": local_id,
                "ref_type": "identity",
                "display_label": "x",
            }
        ],
    }
    (data / "holders" / f"{xr_id}.json").write_text(json.dumps(node), encoding="utf-8")


def test_loads_sources(tmp_path):
    _write_source(tmp_path, "xr_src_0001")
    xw = Crosswalk(tmp_path)
    assert set(xw.sources) == {"xr_src_0001"}
    assert xw.sources["xr_src_0001"].point_in_time == date(2026, 9, 14)


def test_seed_data_dir_still_loads_with_an_empty_sources_dir():
    # data/sources/ ships as .gitkeep only; the real data dir must still load.
    xw = Crosswalk(Path(__file__).resolve().parents[1] / "data")
    assert set(xw.nodes) == {"xr_holder_0001", "xr_org_0001"}
    assert xw.sources == {}


def test_duplicate_source_id_across_files_rejected(tmp_path):
    # Nodes and sources share ONE xr_ id namespace. A source can't actually take a node's id (the
    # models pin each kind to its own prefix — see test_source_models), so what the registry guard
    # has to catch is two files claiming the same id.
    _write_source(tmp_path, "xr_src_0001")
    (tmp_path / "sources/copy.json").write_text(
        (tmp_path / "sources/xr_src_0001.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate xr_id"):
        Crosswalk(tmp_path)


def test_duplicate_url_at_same_point_in_time_rejected(tmp_path):
    _write_source(tmp_path, "xr_src_0001")
    _write_source(tmp_path, "xr_src_0002")
    with pytest.raises(ValueError, match="is pinned by both"):
        Crosswalk(tmp_path)


def test_same_url_at_a_different_point_in_time_allowed(tmp_path):
    _write_source(tmp_path, "xr_src_0001")
    _write_source(
        tmp_path,
        "xr_src_0002",
        canonical_url=ECFR_URL.format("2026-03-01"),
        point_in_time="2026-03-01",
    )
    assert len(Crosswalk(tmp_path).sources) == 2


def test_dangling_supersedes_rejected(tmp_path):
    _write_source(tmp_path, "xr_src_0001", supersedes="xr_src_9999")
    with pytest.raises(ValueError, match="not a known source xr_id"):
        Crosswalk(tmp_path)


def test_dangling_merged_into_rejected(tmp_path):
    _write_source(tmp_path, "xr_src_0001", merged_into="xr_src_9999")
    with pytest.raises(ValueError, match="not a known source xr_id"):
        Crosswalk(tmp_path)


def test_two_documents_may_cite_the_same_record(tmp_path):
    # The one-actor-per-local-id invariant is about actors. Many documents citing one conflict
    # record is normal, and must not trip it.
    cited = [{"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"}]
    archived = [{"service": "wayback", "url": "https://web.archive.org/web/1/x"}]
    _write_source(tmp_path, "xr_src_0001", cited_in=cited, archives=archived)
    _write_source(
        tmp_path,
        "xr_src_0002",
        citation="52 U.S.C. § 30116",
        canonical_url="https://uscode.house.gov/view.xhtml?req=x&num=0&edition=prelim",
        point_in_time=None,
        cited_in=cited,
        archives=archived,
    )
    assert len(Crosswalk(tmp_path).sources) == 2


def test_normalize_citation_folds_punctuation_and_case():
    assert normalize_citation("11 C.F.R. Part 114") == normalize_citation("11 cfr  part 114")
    assert normalize_citation("52 U.S.C. § 30116") == normalize_citation("52 USC 30116")


def test_resolve_source_matches_across_citation_styles(tmp_path):
    _write_source(tmp_path, "xr_src_0001")
    xw = Crosswalk(tmp_path)
    assert xw.resolve_source("11 CFR Part 114").xr_id == "xr_src_0001"
    assert xw.resolve_source("11 c.f.r. part 114").xr_id == "xr_src_0001"
    assert xw.resolve_source("12 CFR Part 114") is None


def test_resolve_source_picks_the_latest_pin(tmp_path):
    _write_source(
        tmp_path,
        "xr_src_0001",
        canonical_url=ECFR_URL.format("2026-03-01"),
        point_in_time="2026-03-01",
    )
    _write_source(tmp_path, "xr_src_0002", supersedes="xr_src_0001")
    assert Crosswalk(tmp_path).resolve_source("11 CFR Part 114").xr_id == "xr_src_0002"


def test_resolve_source_as_of_reaches_the_superseded_version(tmp_path):
    _write_source(
        tmp_path,
        "xr_src_0001",
        canonical_url=ECFR_URL.format("2026-03-01"),
        point_in_time="2026-03-01",
    )
    _write_source(tmp_path, "xr_src_0002", supersedes="xr_src_0001")
    xw = Crosswalk(tmp_path)
    assert xw.resolve_source("11 CFR Part 114", as_of=date(2026, 6, 1)).xr_id == "xr_src_0001"
    assert xw.resolve_source("11 CFR Part 114", as_of=date(2026, 9, 14)).xr_id == "xr_src_0002"
    assert xw.resolve_source("11 CFR Part 114", as_of=date(2026, 1, 1)) is None


def test_resolve_source_as_of_ignores_pins_with_no_point_in_time(tmp_path):
    _write_source(
        tmp_path,
        "xr_src_0001",
        citation="60 FR 7862",
        canonical_url="https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf",
        fetcher="federalregister",
        point_in_time=None,
        published_at="1995-02-09",
    )
    xw = Crosswalk(tmp_path)
    assert xw.resolve_source("60 FR 7862").xr_id == "xr_src_0001"
    assert xw.resolve_source("60 FR 7862", as_of=date(2026, 1, 1)) is None


def test_resolve_source_excludes_merged_losers(tmp_path):
    _write_source(tmp_path, "xr_src_0001")
    _write_source(
        tmp_path,
        "xr_src_0002",
        canonical_url=ECFR_URL.format("2026-03-01"),
        point_in_time="2026-03-01",
        merged_into="xr_src_0001",
    )
    xw = Crosswalk(tmp_path)
    assert xw.resolve_source("11 CFR Part 114").xr_id == "xr_src_0001"
    assert xw.resolve_source("11 CFR Part 114", as_of=date(2026, 3, 1)) is None


def test_iter_node_refs_yields_nodes_first_then_sources(tmp_path):
    data = tmp_path / "data"
    _write_node(data, "xr_holder_0001")
    _write_source(
        data,
        "xr_src_0001",
        cited_in=[{"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"}],
    )
    assert list(iter_node_refs(data)) == [
        ("xr_holder_0001", "vi", "identity", "vi_official_0001"),
        ("xr_src_0001", "vi", "record_mention", "vi_conflict_0001"),
    ]


def test_check_refs_flags_a_dangling_citation(tmp_path):
    # Teaching iter_node_refs about sources is what gives BOTH the pre-commit hook and the
    # committed-state CI check source coverage, with no edit to either caller.
    data = tmp_path / "data"
    _write_source(
        data,
        "xr_src_0001",
        cited_in=[{"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"}],
    )
    vi_root = tmp_path / "vi"
    (vi_root / "data/conflicts").mkdir(parents=True)
    problems = check_refs(data, {"vi": vi_root})
    assert problems and "xr_src_0001" in problems[0] and "vi_conflict_0001" in problems[0]

    (vi_root / "data/conflicts/vi_conflict_0001.json").write_text("{}", encoding="utf-8")
    assert check_refs(data, {"vi": vi_root}) == []


def test_a_cited_source_must_have_an_archive(tmp_path):
    # Once a register record cites a pin, an argument depends on that text. If the publisher
    # replaces it and nothing was archived, the citation degrades to a hash that proves something
    # changed and cannot say what it said.
    cited = [{"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"}]
    _write_source(tmp_path, "xr_src_0001", cited_in=cited)
    with pytest.raises(ValueError, match="xr_src_0001 is cited but has no archive copy"):
        Crosswalk(tmp_path)


def test_an_uncited_source_may_be_unarchived(tmp_path):
    # Dormant until cited: pinning without archiving is allowed, citing without archiving is not.
    _write_source(tmp_path, "xr_src_0001")
    assert Crosswalk(tmp_path).sources["xr_src_0001"].archives == []


def test_a_cited_source_with_an_archive_loads(tmp_path):
    _write_source(
        tmp_path,
        "xr_src_0001",
        cited_in=[{"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"}],
        archives=[{"service": "wayback", "url": "https://web.archive.org/web/1/x"}],
    )
    assert len(Crosswalk(tmp_path).sources) == 1


# ------------------------------------------- the shared pre-write / load-time invariant check


def _source_obj(xr_id="xr_src_0001", **over):
    from registers_crosswalk.models import Source

    base = {
        "xr_id": xr_id,
        "kind": "source",
        "citation": "11 C.F.R. Part 114",
        "title": "11 CFR Part 114",
        "canonical_url": ECFR_URL.format("2026-09-14"),
        "fetcher": "ecfr",
        "point_in_time": "2026-09-14",
        "artifact": dict(ARTIFACT),
        "grade": dict(GRADE),
    }
    base.update(over)
    return Source.model_validate(base)


def test_pre_write_check_rejects_cited_without_archive():
    # `pin add` runs this exact function over "existing sources plus the new one" before writing,
    # which is why the write path cannot produce a file the read path rejects.
    from registers_crosswalk.registry import check_source_invariants

    new = _source_obj(
        cited_in=[{"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"}]
    )
    with pytest.raises(ValueError, match="xr_src_0001 is cited but has no archive copy"):
        check_source_invariants({new.xr_id: new})


def test_pre_write_check_rejects_a_duplicate_against_existing_sources():
    from registers_crosswalk.registry import check_source_invariants

    existing = _source_obj("xr_src_0001")
    new = _source_obj("xr_src_0002")  # same canonical_url and point_in_time
    with pytest.raises(ValueError, match="is pinned by both xr_src_0001 and xr_src_0002"):
        check_source_invariants({existing.xr_id: existing, new.xr_id: new})


def test_pre_write_check_passes_a_clean_addition():
    from registers_crosswalk.registry import check_source_invariants

    existing = _source_obj("xr_src_0001")
    new = _source_obj(
        "xr_src_0002",
        canonical_url=ECFR_URL.format("2026-03-01"),
        point_in_time="2026-03-01",
    )
    check_source_invariants({existing.xr_id: existing, new.xr_id: new})
