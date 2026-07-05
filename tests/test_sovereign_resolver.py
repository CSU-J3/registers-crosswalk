from __future__ import annotations

import json
from pathlib import Path

from registers_crosswalk.join.resolvers import SovereignResolver
from registers_crosswalk.models import RegisterRef

# Synthetic records.json (fake ids, real keys). Confirms id-match and record typing without copying
# Sovereign data into the crosswalk repo.


def _sovereign_tree(root: Path) -> None:
    records = [
        {"id": "SC-900", "business": "Other Co"},
        {
            "id": "SC-907",
            "business": "Test Financial",
            "family_member": "Test Person (beneficial interests)",
            "scope": "LIVE",
            "source": "MULTI",
            "period": "2024-PRES",
            "frameworks": ["EMOL", "OGE", "208"],
            "evidence_category": [1, 2, 3],
            "documented_amount": "$500M test stake",
            "summary": "A test summary.",
            "primary_sources": [
                {"type": "oge_278e", "label": "Test filing", "category": 1},
                {"label": "Test doc", "category": 2},
            ],
        },
    ]
    path = root / "web" / "data" / "records.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


def _ref() -> RegisterRef:
    return RegisterRef(
        register="sovereign",
        local_id="SC-907",
        ref_type="record_mention",
        display_label="beneficial interest (test)",
    )


def test_sovereign_resolver_types_record(tmp_path: Path) -> None:
    _sovereign_tree(tmp_path)
    section = SovereignResolver().resolve(_ref(), tmp_path)

    assert section.record_id == "SC-907"
    assert section.record is not None
    record = section.record
    assert record.business == "Test Financial"
    assert record.frameworks == ["EMOL", "OGE", "208"]
    assert record.evidence_category == [1, 2, 3]
    assert record.documented_amount == "$500M test stake"
    assert record.primary_source_count == 2
    assert len(record.primary_sources) == 2


def test_sovereign_resolver_missing_record_is_none(tmp_path: Path) -> None:
    (tmp_path / "web" / "data").mkdir(parents=True)
    (tmp_path / "web" / "data" / "records.json").write_text("[]", encoding="utf-8")
    section = SovereignResolver().resolve(_ref(), tmp_path)
    assert section.record is None
    assert section.record_id == "SC-907"
