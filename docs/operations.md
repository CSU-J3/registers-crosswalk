# Operations — registers-crosswalk

Runbook: what to do when something breaks or needs maintaining. The schema and the identity rules
live in `crosswalk-spec.md`; this file is operational only.

## SIBLING_REPOS_TOKEN (repo secret)

**What.** A fine-grained PAT stored as the repo secret `SIBLING_REPOS_TOKEN`, used *only* by
`.github/workflows/cross-repo.yml` to `actions/checkout` the three private sibling registers so their
committed `main` can be read. Nothing else uses it; the package and tests never touch it.

**Scope (keep minimal).**
- Resource owner: `CSU-J3`.
- Repository access: *Only select repositories* — `Vested-Interests`, `Connected-Procurement`,
  `Sovereign-Connections`, enumerated. Never "All repositories".
- Permissions: **Contents: Read-only**. GitHub auto-adds **Metadata: Read** (mandatory, unremovable,
  harmless). Nothing else.

**Symptom → cause.**
- `cross-repo-refs` fails at a *Fetch <sibling>* step with `Input required and not supplied: token`
  → the secret is unset/empty (never added, deleted, or the PAT expired). The `ci` job (ruff/pytest)
  is unaffected — only the committed-state check goes dark.
- `cross-repo-refs` fails at a *Fetch <sibling>* step with `403` / `Repository not found` → the
  secret exists but the PAT's repo-access list doesn't enumerate that sibling (or the token owner
  lost access to it). The failing **step name tells you which repo**. This is a grant problem, not a
  scope problem — fix the PAT's selected-repositories list, don't widen permissions.

**Rotate** (fine-grained PATs expire; plan for it):
1. Mint a replacement at <https://github.com/settings/personal-access-tokens/new> with the exact
   scope above.
2. `gh secret set SIBLING_REPOS_TOKEN --repo CSU-J3/registers-crosswalk` and paste when prompted —
   never put the token on a command line (shell history) or in a chat/PR.
3. Re-run: `gh run rerun <run-id> --repo CSU-J3/registers-crosswalk`, or push any commit.
4. Revoke the old PAT.

Without the secret the repo still develops fine locally — the pre-commit hook is working-tree based
and needs no token. Only the authoritative CI existence check is unavailable.

## Sovereign path dependency in the resolver

`registers_crosswalk.refresolve` resolves a Sovereign `record_mention` by scanning for the quoted id
inside **one file**: `web/data/records.json` in the Sovereign checkout (`_IN_FILE` in
`refresolve.py`). Operational consequences:

- `cross-repo.yml`'s Sovereign sparse-checkout **must** include `web/data`. If Sovereign relocates or
  renames `records.json`, every Sovereign ref silently reads *absent* and `cross-repo-refs` goes red
  with false negatives. Fix `_IN_FILE` in `refresolve.py` and the sparse path in `cross-repo.yml`
  **together** — they are one dependency in two files.
- The match is a substring scan (`"SC-007"` appears in the file), not a JSON-structural lookup. It is
  deliberately loose so it survives Sovereign schema churn; the cost is that an id appearing only in
  prose would count as present. Fine for an existence check — don't tighten it without a reason.

## No-vendoring rule (why nodes stay thin)

A node carries only: canonical name, `external_ids` (public registrar facts — CIK/UEI), and register
refs. **Never** copy a register's own data into it — no revenue, bands, holdings, obligations,
filings, edges, or transactions. When editing a node and you feel the urge to paste a number or a
fact out of VI/CP/Sovereign, stop: reference the `local_id` and let the consumer dereference. Two
reasons, both operational: (1) vendoring reintroduces the cross-register bleed the separate repos
exist to prevent; (2) a copied fact is a second source that goes stale silently when the register
updates. The crosswalk points; it does not hold.

## Sovereign is `record_mention`-only

The model rejects any Sovereign ref whose `ref_type` is not `record_mention`
(`MENTION_ONLY_REGISTERS` in `models.py`). Reason: Sovereign has no per-entity identity records —
individuals surface *inside* `SC-NNN` records, and `sovereign_entities.json` is not yet wired here as
a stable identity key. An `identity` ref for Sovereign would assert an identity record that does not
exist.

To lift this **when Sovereign mints stable per-entity keys**, change three files together:
1. remove `"sovereign"` from `MENTION_ONLY_REGISTERS` in `models.py`;
2. add a `("sovereign", "identity")` pattern to `REGISTER_ID_PATTERNS` in `ids.py`, plus the matching
   `_FILE_DIRS`/`_IN_FILE` entry in `refresolve.py`;
3. add the new file/dir to the Sovereign sparse-checkout in `cross-repo.yml`.

## Tracked chores

- **Node 20 → newer action majors.** `actions/checkout@v4` and `actions/setup-python@v5` currently
  run on Node 24 with a deprecation warning (Node 20 sunset, GitHub 2025-09-19). Bump to action
  majors that target Node 24 across `ci.yml` and `cross-repo.yml`. Warning only, non-urgent; do it in
  step with the sibling repos so all four move together.
