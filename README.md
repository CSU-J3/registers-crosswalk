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
    src/registers_crosswalk/               # pydantic models + registry + validate + pin + status
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
`federalregister`, `govinfo`, `uscode`, `openfec`, `fecfiling`, `courtlistener`, `manual`.

A signed U.S. Code edition comes through GovInfo by package and granule. A section's number is
in the granule id, not its title:

    python -m registers_crosswalk.pin add govinfo --package USCODE-2024-title52 \
        --granule USCODE-2024-title52-subtitleIII-chap301-subchapI-sec30116 \
        --citation "52 U.S.C. § 30116 (2024 ed.)"

`add govinfo` needs `GOVINFO_API_KEY` for the summary call. What it stores is the key-free
`www.govinfo.gov` copy, so `check` needs no key.

An FEC matter carries several documents and repeats their categories, so one is named by the
API's own id:

    python -m registers_crosswalk.pin add openfec --number 8098 --type murs         --document 100512215 --notes "MUR 8098: the certification of the 6-0 dismissal"

`pin search openfec <name> --type murs` prints the matter numbers; the ids are in each matter's
`documents[]`. `--category` is the other way to name a document and is what advisory opinions use
(`--type advisory_opinions`, one `Final Opinion` each); the two are mutually exclusive. The
citation defaults to `FEC MUR 8098` and `--citation` overrides it.

A committee's report as filed is named by its FEC file number, and each filing has two documents:
the `.fec` file the committee submitted and the FEC's image PDF of it.

    python -m registers_crosswalk.pin add fecfiling --file-number 1920944 --document fec \
        --citation "Osborn For Senate, Form 3 Q2 2025-06-30, FEC file 1920944" \
        --supersedes xr_src_0026 \
        --notes "amendment 1 of 2, received 2025-10-15; amends FEC file 1903438 (received 2025-07-15), the original; itemizes Helix Campaigns"

An amendment is a new filing with its own number, so it is a new pin that supersedes the last one
of the same document kind. The processed Schedule B rows are only for finding the file numbers; the
pin is the filing itself. `add fecfiling` needs `OPENFEC_API_KEY` for the metadata call; docquery
serves both documents without one, so `check` needs no key.

    python -m registers_crosswalk.pin search <fetcher> <query>   # identifiers, then the add to paste
    python -m registers_crosswalk.pin check        # 0 clean, 1 drift, 2 missing key, 3 fetch failed
    python -m registers_crosswalk.pin ledger       # entries for the New Gray ledgers (--format manifest|record)
    python -m registers_crosswalk.pin archive --repair xr_src_NNNN   # re-verify an attached capture
    python -m registers_crosswalk.pin blobs --out DIR   # verified copies + MANIFEST.sha256, outside the repo
    python -m registers_crosswalk.pin note xr_src_NNNN "<label>"   # offline; rewrites only notes

A transport failure (exit 3) is deliberately not reported as drift (exit 1) — a dead endpoint and
a changed document demand opposite responses. Every record also carries `fetcher_verified`: false
means that fetcher's field mapping has never been exercised against the live API, so a silent
mapping error could be sitting in the record. `validate` counts them.

API keys (`GOVINFO_API_KEY`, `OPENFEC_API_KEY`, `COURTLISTENER_TOKEN`) come from the environment and
never enter a stored URL — see `docs/operations.md`, which also explains what each drift status
means and why a quiet repo can silently stop drift-checking.

## Archiving a pin after the fact

    python -m registers_crosswalk.pin archive xr_src_0006

`add --archive` is all-or-nothing: the capture has to succeed or the pin is not written. That is
right where the archive is a requirement — a `uscode` pin without one is unrecoverable the moment
the section changes — but it means a Wayback outage can cost a good fetch of a document whose
archive is practice rather than requirement. This is the other order: pin now, archive when the
service is up.

It does not re-fetch the document and does not re-decide anything about it. It goes through the
same reuse-or-capture path `add` uses — an existing capture whose `id_` copy, fetched at its exact
timestamp, reproduces the pin's drift value (the sha256 for a fixed document, the last amendment
for a U.S. Code prelim) is adopted without asking Save Page Now for anything, and a new capture is
checked the same way before it is attached — and rewrites only the record's `archives`
field, so the diff is the one fact that changed. It refuses a pin that already has an archive, an
unknown id, and a failed capture (with the reason). The console lists every pin with no archive
copy and offers the same thing as a button.

## Finding a document

    python -m registers_crosswalk.pin search courtlistener "Dunne v. United States"

    1481640  1943-09-20  Dunne v. United States  12195  138 F.2d 137
    ...
    pin add courtlistener --cluster-id 1481640 --archive

Case name or docket number in, identifiers out, and the last line is the `add` that pins the first
hit. `--type dockets` searches dockets instead of opinions (a lookup aid: a docket id is not a
cluster id, so those hits print no `add`). `--json` prints the hits as JSON. `search` never writes
anything and never touches `data/`; the API key is used for the query and then forgotten.

## Console

    python -m registers_crosswalk.console

A local page over `search` and `add`: type a case name, see what document would be pinned and at
what grade, click Pin. It serves on `127.0.0.1:8765` and opens a browser; `--no-browser` skips
that, `--port N` moves it, `--data-dir DIR` points it somewhere other than this repo's `data/`.

**It is the write side, so it is local only.** The server binds the loopback interface and nothing
else, the page is `noindex`, and every `/api/` request carries a session token minted at startup
and rendered into the page — so a page on another origin cannot drive it. It is never run in CI
and never exposed beyond `127.0.0.1`.

**Nothing in it bypasses a rule `add` enforces.** Search goes through the same `fetchers.search`,
resolving goes through the fetcher's own `spec_from_args`, and pinning goes through
`pin.add_source` — the function `pin add` itself calls. Every refusal you see on the page is the
CLI's own message, in the CLI's own words, because it is the same string. A fetcher that has not
been verified live can be *searched* but not *pinned* through: the first live `spec()` run goes
through the terminal, where its output is read.

**It reads `.env` itself and never modifies the environment.** Keys are parsed into a private
mapping that is passed to the calls that need one. The page shows only a variable's *name* and
whether it is set, never a value.

It also lists any pin with no archive copy and offers **Archive now** on each, which calls the
same `pin archive` code path.

**A pin is still a commit.** Clicking Pin writes one record to `data/sources/` and nothing more;
the page shows the ledger entry, the manifest line, and the `git add` that starts the commit. The
footer lists anything uncommitted under `data/sources/`, so a pin made and then forgotten is
visible on the page that made it.

Requests to `www.courtlistener.com` are spaced 12 seconds apart, whatever the operator clicks —
that is CourtListener's 5-a-minute ceiling for a free account, enforced by the code rather than by
restraint. The PDF host is not the API and is not paced.

## Status page

    python -m registers_crosswalk.status --out build/status/index.html

Generates one read-only HTML page from `data/`: every pinned document with the version that was
pinned, its archive copy, a link to the publisher's own copy, and a button that puts its
source-links ledger entry on the clipboard. Plus the actors resolved across registers and which
fetchers have been run live. Add `--drift-json PATH` (the output of `pin check --json`) to stamp
each row with the latest check; without it the page says it was not checked.

It writes exactly one file, into gitignored `build/`. **The page is never committed**, the build
never touches the network, and it never reads `.env` — only `pin check` fetches anything.

`.github/workflows/status-page.yml` runs the check, builds the page from its JSON and deploys it
to GitHub Pages on every push to `main`, twenty minutes after the weekly drift cron, and on
`workflow_dispatch`. It exits with the check's own code, so a drifted pin is a red run *and* a red
page rather than either alone. Published at `https://csu-j3.github.io/registers-crosswalk/`.
One-time setup, admin only: **Settings → Pages → Source → GitHub Actions**; until that is set the
deploy step fails and nothing else is wrong.

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
