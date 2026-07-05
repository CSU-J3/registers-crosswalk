# registers-crosswalk — spec

A thin resolution layer **above** Connected-Procurement (CP), Sovereign-Connections (Sovereign),
and Vested-Interests (VI). It resolves the *same real-world actor* across the three registers and
holds the one canonical managed-account determination rule. It is the "layer above them, not by
merging" that each register's CLAUDE.md mandates for anti-bleed.

## What it stores — and what it must never store

Stores, per actor: a **stable minted crosswalk id** (`xr_holder_NNNN` / `xr_org_NNNN`), a **canonical
name**, **public registrar ids** (`external_ids[]`: CIK/UEI — external truth, not any register's
work), and **references into each register by that register's own local id** (`registers[]`), each
tagged with a `ref_type`.

Never stores (anti-bleed contract, enforced by the closed schema and by review): no revenue figures,
bands, holdings, obligations, or shares (VI); no filings, edges, nexus links, or watchlist state (CP);
no transactions, flows, or convergent-interest detail (Sovereign). The crosswalk points; it does not
copy. A consumer that wants a register's data dereferences the `local_id` against that register.

## ID scheme

`xr_holder_NNNN` / `xr_org_NNNN`, minted here, zero-padded, monotonic. Minted (not natural) because a
natural key (CIK) is absent for persons, absent for private/foreign recipients, non-unique when an
actor has both a CIK and a UEI, and unstable across restructurings. CIK/UEI ride in `external_ids[]`,
**never** as the primary key. Ports CP's `cp_entity_NNNN` + `external_ids[]` design.

## Node schema

```
Node
  xr_id           xr_holder_NNNN | xr_org_NNNN   (must agree with kind)
  kind            "holder" | "org"
  canonical_name  str
  external_ids[]  ExternalId                      (may be empty; typical for persons)
  registers[]     RegisterRef                     (>= 1)
  merged_into     xr_id | null                    (supersession; ported from CP)
  notes           str | null

ExternalId
  scheme          "cik" | "uei"                   (closed set)
  value           str
  source_url      str | null

RegisterRef
  register        "vi" | "cp" | "sovereign"
  local_id        str                             (validated per (register, ref_type))
  ref_type        "identity" | "record_mention"
  display_label   str                             (DISPLAY ONLY — never a join key)
  note            str | null
```

`display_label` is a human label only. The join-relevant type is fully carried by `(register,
ref_type)` and the `local_id`'s own namespace, so nothing should ever key on the label string; it is
free text precisely so it can state register-specific nuance (e.g. CP's "filer, excluded_from_total").

### `ref_type` — the identity/mention distinction

- **`identity`** — `local_id` *is* the actor's own record: `vi_official_0001`, `cp_person_0003`,
  `vi_recipient_0002`, `cp_entity_NNNN`, or a `sovereign_entities.json` org id once those exist.
- **`record_mention`** — `local_id` is a *record that references* the actor. Sovereign's normal case
  for individuals: `SC-007` is a record in which the Trump-family interest appears; Sovereign has no
  per-person identity record. Calling that `identity` would falsely assert Sovereign carries a Trump
  entity.

**Enforced:** a register listed in `MENTION_ONLY_REGISTERS` (currently `{sovereign}`) rejects any ref
whose `ref_type` is not `record_mention`. Sovereign cannot carry an `identity` ref until it mints
per-entity keys; drop it from the set and add a `(sovereign, identity)` pattern at that point.

## Per-register `local_id` patterns

| register | ref_type | pattern | source |
|---|---|---|---|
| vi | identity | `^vi_(official\|recipient)_\d{4}$` | VI officials / recipients |
| vi | record_mention | `^vi_conflict_\d{4}$` | VI conflict records |
| cp | identity | `^cp_(person\|entity)_\d{4}$` | CP persons / entities |
| cp | record_mention | `^cp_filing_\d{4}$` | CP filings |
| sovereign | record_mention | `^SC-\d{3}$` | Sovereign records |

There is deliberately no `(sovereign, identity)` row (see above).

## Validation (CI — `ruff` + `pytest`, matching the siblings)

1. `xr_id` matches `xr_(holder|org)_\d{4}` and agrees with `kind`.
2. each `registers[].local_id` matches the pattern for its `(register, ref_type)`.
3. sovereign (and any `MENTION_ONLY_REGISTERS`) refs are `record_mention`.
4. `external_ids[].scheme` in the closed set `{cik, uei}`.
5. `merged_into` (when set) resolves to a real `xr_id` in the data.
6. no duplicate `(register, local_id)` across nodes — one actor per local id.

Cross-repo existence is checked in two layers: `hooks/pre-commit` resolves each `local_id` against the
sibling **working copies** on disk (fast, skip-if-absent), and `.github/workflows/cross-repo.yml`
fetches each sibling at `main` and resolves against **committed state** (authoritative; requires the
`SIBLING_REPOS_TOKEN` secret to reach the private repos). The CI resolver's rules live in
`registers_crosswalk.refresolve`; the pre-commit hook mirrors the same register→location mapping and
must be kept in sync if a register grows a new identity namespace.

## Supersession

Ported from CP: on a merge the losing node stays with `merged_into` → the winner; the winner absorbs
the loser's `external_ids` and `registers`. Soft-supersede, never delete — preserve the audit trail.

## Seed nodes

- `xr_holder_0001` Donald J. Trump → vi_official_0001 (identity), cp_person_0003 (identity, CP
  filer-anchor / not named-family / excluded_from_total), SC-007 (record_mention).
- `xr_org_0001` Palantir Technologies Inc. → external CIK 0001321655; vi_recipient_0002 (identity).
