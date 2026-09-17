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

Per **source document** (`xr_src_NNNN`): a citation, a canonical URL, a sha256, a point-in-time
date, an Admiralty grade and an archive copy — facts about the document *as an object*, so a source
cited in VI, in Sovereign and in a New Gray ledger resolves to one hash.

It **never** copies a register's data — no revenue, holdings, filings, edges, or transactions — and
it never stores a pinned document's content: no bytes, no quotes, no summaries. The crosswalk
points; consumers dereference `local_id` against the source register, and blobs live in the
consuming project.

## Layout

    docs/crosswalk-spec.md                 # the schema, ref rules, anti-bleed contract
    docs/managed-account-determination.md  # canonical shared truth (QBT vs managed_direct)
    docs/operations.md                     # runbook: tokens, API keys, drift, resolver deps, chores
    data/holders/  data/orgs/              # xr_holder_NNNN / xr_org_NNNN nodes
    data/sources/                          # xr_src_NNNN pinned documents (ships empty)
    src/registers_crosswalk/               # pydantic models + registry + validate + pin
    src/registers_crosswalk/fetchers/      # one module per document source (eCFR, FR, GovInfo, …)
    tests/
    hooks/pre-commit                       # local cross-repo existence check (CI can't read siblings)

## Run

    pip install -e ".[dev]"
    ruff check . && ruff format --check .
    pytest -q
    python -m registers_crosswalk.validate     # loads + prints the resolved graph

## Pinning a source

    python -m registers_crosswalk.pin add ecfr --title 11 --part 114 --as-of 2026-09-14 \
        --cited-in vi:vi_conflict_0001

Fetches the document once, hashes it, writes `data/sources/xr_src_NNNN.json`, and prints the ledger
entry to paste into a New Gray source-links file. `--cited-in` requires `--archive`: a source a
register record depends on has to stay recoverable, and `add` refuses to write a record that the
read path would reject. `--blob-dir PATH` also writes the bytes to
`PATH/<sha256><ext>` — **outside this repo**, which is enforced, not merely asked. Other fetchers:
`federalregister`, `govinfo`, `uscode`, `openfec`, `courtlistener`, `manual`.

    python -m registers_crosswalk.pin check        # 0 clean, 1 drift, 2 missing key, 3 fetch failed
    python -m registers_crosswalk.pin ledger       # entries for the New Gray ledgers

A transport failure (exit 3) is deliberately not reported as drift (exit 1) — a dead endpoint and
a changed document demand opposite responses. Every record also carries `fetcher_verified`: false
means that fetcher's field mapping has never been exercised against the live API, so a silent
mapping error could be sitting in the record. `validate` counts them.

API keys (`GOVINFO_API_KEY`, `OPENFEC_API_KEY`, `COURTLISTENER_TOKEN`) come from the environment and
never enter a stored URL — see `docs/operations.md`, which also explains what each drift status
means and why a quiet repo can silently stop drift-checking.

## Reference integrity (three layers)

A `local_id` points into a separate repo and a `canonical_url` points outside all of them, so
existence is checked three ways:

1. **Pre-commit (local, fast)** — `hooks/pre-commit` runs `hooks/check_refs.py` against the sibling
   **working copies** on disk (`../Vested-Interests`, `../Connected-Procurement`,
   `../Sovereign-Connections`); absent siblings are skipped. Install:

       cp hooks/pre-commit .git/hooks/pre-commit    # then chmod +x on POSIX

2. **CI (authoritative, committed state)** — `.github/workflows/cross-repo.yml` fetches each sibling
   by URL at `main` (committed state, not a working tree) and asserts every `local_id` exists via
   `python -m registers_crosswalk.xref_ci`. It requires a repo secret **`SIBLING_REPOS_TOKEN`** (a
   fine-grained PAT, owner `CSU-J3`, the three sibling repos, Contents: read) to reach the private
   siblings. Pin a sibling by replacing `ref: main` with a commit SHA in the workflow.

3. **Source drift (weekly)** — `.github/workflows/sources-drift.yml` re-fetches every pinned
   document and compares its drift key. The only layer that reaches outside the four repos, and the
   only one that can go red because the *world* changed rather than because we did. For eCFR pins it
   also reads the amendment history, because a point-in-time URL keeps returning the old text after
   the law changes — a matching hash there proves nothing.

Both `cited_in` refs and `registers[]` refs flow through one `iter_node_refs`, so layers 1 and 2
cover pinned documents' citations with no change of their own.
