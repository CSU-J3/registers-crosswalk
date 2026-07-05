from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_XR_ID = re.compile(r"^xr_(holder|org)_\d{4}$")


def _xr_id(v: str) -> str:
    if not _XR_ID.match(v):
        raise ValueError(f"invalid xr id: {v!r} (want xr_holder_NNNN or xr_org_NNNN)")
    return v


XrId = Annotated[str, AfterValidator(_xr_id)]

# local_id patterns per (register, ref_type). There is deliberately no ("sovereign", "identity")
# entry: Sovereign has no per-entity identity records yet (individuals surface only inside SC-NNN
# records), so an identity ref for sovereign has nothing to match and is rejected. Add the entry
# once sovereign_entities.json mints stable per-entity keys.
REGISTER_ID_PATTERNS: dict[tuple[str, str], re.Pattern[str]] = {
    ("vi", "identity"): re.compile(r"^vi_(official|recipient)_\d{4}$"),
    ("vi", "record_mention"): re.compile(r"^vi_conflict_\d{4}$"),
    ("cp", "identity"): re.compile(r"^cp_(person|entity)_\d{4}$"),
    ("cp", "record_mention"): re.compile(r"^cp_filing_\d{4}$"),
    ("sovereign", "record_mention"): re.compile(r"^SC-\d{3}$"),
}
