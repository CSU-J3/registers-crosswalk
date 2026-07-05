# registers-crosswalk

A thin resolution layer **above** three separate registers — Connected-Procurement (CP),
Sovereign-Connections (Sovereign), and Vested-Interests (VI). It resolves the *same real-world actor*
across them and holds the one canonical managed-account determination rule they all cite.

It exists so cross-register identity lives in one place **without merging the registers** — the
anti-bleed separation (`vi_` / `cp_` / `SC-` prefixes, distinct scopes) stays intact.

## What it stores — and never stores

Per actor: a stable minted id (`xr_holder_NNNN` / `xr_org_NNNN`), a canonical name, public registrar
ids (`external_ids[]`: CIK/UEI), and **references into each register by that register's own local id**,
each tagged `identity` (the local id *is* the actor) or `record_mention` (the local id is a record
that *mentions* the actor).

It **never** copies a register's data — no revenue, holdings, filings, edges, or transactions. The
crosswalk points; consumers dereference `local_id` against the source register.

## Layout

    docs/crosswalk-spec.md                 # the schema, ref rules, anti-bleed contract
    docs/managed-account-determination.md  # canonical shared truth (QBT vs managed_direct)
    docs/operations.md                     # runbook: SIBLING_REPOS_TOKEN, resolver deps, chores
    data/holders/  data/orgs/              # xr_holder_NNNN / xr_org_NNNN nodes
    src/registers_crosswalk/               # pydantic models + registry + validate
    tests/
    hooks/pre-commit                       # local cross-repo existence check (CI can't read siblings)

## Run

    pip install -e ".[dev]"
    ruff check . && ruff format --check .
    pytest -q
    python -m registers_crosswalk.validate     # loads + prints the resolved graph

## Cross-repo reference integrity (two layers)

A `local_id` points into a separate repo, so existence is checked two ways:

1. **Pre-commit (local, fast)** — `hooks/pre-commit` runs `hooks/check_refs.py` against the sibling
   **working copies** on disk (`../Vested-Interests`, `../Connected-Procurement`,
   `../Sovereign-Connections`); absent siblings are skipped. Install:

       cp hooks/pre-commit .git/hooks/pre-commit    # then chmod +x on POSIX

2. **CI (authoritative, committed state)** — `.github/workflows/cross-repo.yml` fetches each sibling
   by URL at `main` (committed state, not a working tree) and asserts every `local_id` exists via
   `python -m registers_crosswalk.xref_ci`. It requires a repo secret **`SIBLING_REPOS_TOKEN`** (a
   fine-grained PAT, owner `CSU-J3`, the three sibling repos, Contents: read) to reach the private
   siblings. Pin a sibling by replacing `ref: main` with a commit SHA in the workflow.
