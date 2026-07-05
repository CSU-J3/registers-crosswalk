from __future__ import annotations

import warnings
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .ids import REGISTER_ID_PATTERNS, XrId

Register = Literal["vi", "cp", "sovereign"]
RefType = Literal["identity", "record_mention"]
Scheme = Literal["cik", "uei"]

# Registers with no per-entity identity records yet: only record_mention refs are allowed. Drop a
# register from this set once it mints stable per-entity keys (e.g. sovereign_entities.json ids).
MENTION_ONLY_REGISTERS: frozenset[str] = frozenset({"sovereign"})


class XrModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExternalId(XrModel):
    # Public registrar facts only (external truth, not any register's work product). Closed scheme
    # set — extend deliberately. CIK/UEI ride here; they are NEVER the crosswalk primary key.
    scheme: Scheme
    value: str
    source_url: str | None = None


# The field below is named `register` so the JSON key and model_dump() output are both "register"
# (clean round-trip, no alias that could leak a different key). That name shadows ABCMeta.register,
# inherited via pydantic's metaclass; we never call it, so the shadow is harmless. Suppress only
# that one class-creation warning rather than aliasing the field.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "register" .* shadows an attribute',
        category=UserWarning,
    )

    class RegisterRef(XrModel):
        register: Register
        local_id: str
        ref_type: RefType
        # Display-only human label. NEVER a join key: the join-relevant type is fully carried by
        # (register, ref_type) plus the local_id's own namespace. Free text so it can state
        # register-specific nuance (e.g. CP's "filer, excluded_from_total") without a brittle enum.
        display_label: str
        note: str | None = None

        @model_validator(mode="after")
        def _check(self) -> RegisterRef:
            reg = self.register
            if reg in MENTION_ONLY_REGISTERS and self.ref_type != "record_mention":
                raise ValueError(
                    f"{reg} has no identity records yet; use ref_type='record_mention' "
                    f"(got {self.ref_type!r} for local_id {self.local_id!r})"
                )
            pat = REGISTER_ID_PATTERNS.get((reg, self.ref_type))
            if pat is None:
                raise ValueError(f"no local_id pattern registered for {reg}/{self.ref_type}")
            if not pat.match(self.local_id):
                raise ValueError(
                    f"{reg}/{self.ref_type} local_id {self.local_id!r} fails {pat.pattern}"
                )
            return self


class Node(XrModel):
    xr_id: XrId
    kind: Literal["holder", "org"]
    canonical_name: str
    external_ids: list[ExternalId] = []
    registers: list[RegisterRef]
    merged_into: XrId | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Node:
        if not self.xr_id.startswith(f"xr_{self.kind}_"):
            raise ValueError(f"xr_id {self.xr_id!r} disagrees with kind {self.kind!r}")
        if not self.registers:
            raise ValueError(f"{self.xr_id} has no register refs")
        return self
