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

## Sources — the same real-world *document*

The crosswalk resolves a fourth kind of thing: not an actor but a **document**. A statute,
regulation, Federal Register notice, court opinion, filing or agency statement gets one minted id
(`xr_src_NNNN`), one canonical URL, one sha256, one point-in-time date, one Admiralty grade and one
archive copy. Registers and the New Gray ledgers cite the `xr_src` id instead of pasting URLs, so a
source cited in VI, in Sovereign and in a feature article resolves to one hash.

**The anti-vendoring rule carries over unchanged, and it is the design constraint.** A source node
stores facts about the document *as an object*: where it lives, when it was fetched, what its bytes
hash to, who published it, when. It **never** stores the document's content — no bytes, no quotes,
no summary of what it says. Blobs live in the consuming project (New Gray's `pins/`) or in the
archive copy; the node's hash verifies either. `pin()` refuses a `--blob-dir` that resolves inside
this repo, so the rule is enforced and not merely stated.

```
Source
  xr_id           xr_src_NNNN                      (must agree with kind)
  kind            "source"
  citation        str                              (as the document cites itself)
  title           str
  publisher       str | null
  canonical_url   str                              (http(s), and NEVER carrying an API key)
  fetcher         "ecfr" | "federalregister" | "govinfo" | "uscode" | "openfec"
                  | "courtlistener" | "manual"
  fetcher_verified bool                            (default FALSE — see below)
  verified_at     date | null                      (set iff fetcher_verified)
  point_in_time   date | null                      (eCFR "as of"; OLRC "laws in effect on")
  published_at    date | null                      (FR publication; opinion decision; statement)
  artifact        Artifact
  grade           Grade
  archives[]      ArchiveCopy
  cited_in[]      CitationRef
  supersedes      xr_src_NNNN | null               (an earlier pin of the SAME citation)
  merged_into     xr_src_NNNN | null               (duplicate resolution; same as Node)
  notes           str | null

Artifact
  sha256          str                              (64 lowercase hex)
  byte_length     int
  media_type      str                              (from Content-Type, parameters dropped)
  fetched_at      datetime                         (timezone-aware, normalized to UTC)
  drift_key       "sha256" | "last_amended"
  drift_value     str                              (the hash again, or the source credit's
                                                    latest date, ISO)

Grade             Admiralty: reliability A-F, credibility 1-6; `.code()` → "A1"
ArchiveCopy       service "wayback" | "perma" | "govinfo"; url; captured_at | null
CitationRef       register; local_id; ref_type (fixed "record_mention"); note | null
```

### `supersedes` vs `merged_into`

Two different failures, deliberately not one field:

- **`supersedes`** — the earlier pin was *right then*. 11 CFR Part 114 as of 2026-03-01 is a real
  document with a real hash; it is simply no longer current. Both pins stay valid and both stay
  reachable: `resolve_source(citation, as_of=...)` is how you reach the older text. It is also how
  you re-pin an **unchanged** document on purpose: invariant 9 refuses a second live pin of one
  document, and naming the earlier pin is the assertion that lifts it.
- **`merged_into`** — the loser was *wrong*: a duplicate, a bad URL, a mis-keyed citation. Same
  semantics as `Node.merged_into`. Losers are excluded from resolution at every `as_of`.

### ID scheme

`xr_src_NNNN`, minted here, zero-padded, monotonic, in the **same namespace** as `xr_holder_` /
`xr_org_` — an xr id identifies one thing, whatever kind it is. A source id can never land on a
`Node` (and vice versa) because each model requires its own prefix.

### `fetcher_verified` — has this fetcher ever been run for real?

A property of the **fetcher**, stamped from its module onto every record it mints, never retyped
per record. `false` means that fetcher's field mapping has not been exercised against the live API,
so a silent mapping error could be sitting in the record — a date read from the wrong key, a URL
built off an assumed base. It is **orthogonal to `grade`**: the Supreme Court is an A1 source
whether or not our CourtListener parse has ever been run, and conflating the two is how an
unverified parse acquires the authority of a verified source.

The default is `false`, so a new fetcher is untrusted until someone runs it. `verified_at` must be
set exactly when `fetcher_verified` is true — a bare claim with no date is unauditable, and a date
with no claim is a date about nothing. `courtlistener` ships `false` per decision 9. `manual` ships
`true` by a different route: a human typed every field, so there is no mapping that could be
silently wrong (which says nothing about whether they typed the *right* thing — no flag carries
that). `validate` prints the count and marks each such line.

### `cited_in` — which register records cite this document

`ref_type` is fixed to `record_mention`: a document is cited *by* a record, and is never a
register's identity record. Because that is a `Literal`, a sovereign `identity` ref is rejected by
the type itself — no `MENTION_ONLY_REGISTERS` interaction is involved. The `local_id` is validated
against the same `REGISTER_ID_PATTERNS` table the actor refs use.

### Source invariants (added to the list above)

7. `xr_id` matches `xr_src_\d{4}` and agrees with `kind`; `supersedes`/`merged_into` are `xr_src_`
   ids that resolve to known sources.
8. no two sources share a `(canonical_url, point_in_time)` — that is the same bytes pinned twice.
9. no two **live** sources share a `(normalize_citation(citation), drift_key, drift_value)` — that
   is the same document pinned twice under a URL or a date that happened to differ. A source is
   live when `merged_into` is null and no other source's `supersedes` names it, so `--supersedes`
   is the override: naming the earlier pin retires it, and the collision is gone.
10. `canonical_url` is http(s) and carries **no** API key. This repo is public, so a key
    interpolated into a stored URL would be a committed secret; fetchers store the key-free content
    URL and re-attach the key from the environment at fetch time.
11. the one-actor-per-`local_id` invariant does **not** apply to `cited_in`. Many documents may
    cite one record, and that is normal.

`cited_in` refs join the existing two reference-integrity layers for free (`iter_node_refs` yields
them after the node refs, so the pre-commit hook and `cross-repo.yml` both cover them), and a third
layer is added for the documents themselves: `.github/workflows/sources-drift.yml` re-fetches each
pinned document weekly and compares its drift key.

## Seed nodes

- `xr_holder_0001` Donald J. Trump → vi_official_0001 (identity), cp_person_0003 (identity, CP
  filer-anchor / not named-family / excluded_from_total), SC-007 (record_mention).
- `xr_org_0001` Palantir Technologies Inc. → external CIK 0001321655; vi_recipient_0002 (identity).

`data/sources/` ships empty (`.gitkeep` only): nothing can be pinned offline from a fixture without
inventing a hash, and hashes are never invented.
