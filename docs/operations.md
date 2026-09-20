# Operations — registers-crosswalk

Runbook: what to do when something breaks or needs maintaining. The schema and the identity rules
live in `crosswalk-spec.md`; this file is operational only.

## Every date in this repo is the UTC day

Record fields (`fetched_at`, `point_in_time`, `verified_at`), `VERIFIED_AT` constants, and fixture
capture filenames all use the UTC calendar day — never the local one. The repo is maintained from
UTC-6, so for six hours of every day the two disagree, and a run at 18:43 local is already the next
day on the wire. Mixing them puts two clocks in one record: that is how a pin ends up looking a day
older than the capture it was verified against, and there is no way to tell afterwards which line
used which clock.

`fetched_at` is written by `datetime.now(tz=UTC)` and is the anchor; everything a run produces is
dated to agree with it.

Dates that record someone ELSE's observation keep the date they were given. Re-dating a claim
inherited from a handoff would assert an observation on a day it did not happen.

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

**Rotate**:
1. Mint a replacement at <https://github.com/settings/personal-access-tokens/new> with the exact
   scope above.
2. `gh secret set SIBLING_REPOS_TOKEN --repo CSU-J3/registers-crosswalk` and paste when prompted —
   never put the token on a command line (shell history) or in a chat/PR.
3. Re-run: `gh run rerun <run-id> --repo CSU-J3/registers-crosswalk`, or push any commit.
4. Revoke the old PAT.
5. **Record the new expiry date in the line below.** It is the one fact about this token that no
   API can return: `CSU-J3` is a user account, not an org, so `/orgs/{org}/personal-access-tokens`
   does not exist for it and a fine-grained PAT's expiry is visible only at
   <https://github.com/settings/personal-access-tokens>. Nothing reads this line automatically —
   it is here so the next person does not have to go looking, and so an expiry can be seen coming
   rather than discovered when CI goes red.

**`SIBLING_REPOS_TOKEN` expires:** never — it was minted with no expiration, read from the
settings page above on 2026-09-18. The secret itself was last set 2026-07-05, per the `updated_at`
that `gh api repos/CSU-J3/registers-crosswalk/actions/secrets` returns; that is when the secret was
written here, and it says nothing about the PAT behind it.

Why this matters more than it used to: `main` now requires `cross-repo-refs` and
`join-integration`, both of which check out the private siblings with this token, and
`enforce_admins` is on. A token this repo cannot use is a repo-wide merge freeze that cannot be
overridden from the UI, on PRs that have nothing to do with the siblings. No expiry closes one
route into that freeze, not the freeze itself: GitHub automatically revokes a fine-grained PAT
that has gone unused for a year, or that is pushed to a public repository or gist —
<https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types>.
`cross-repo.yml` is this token's only consumer, and it runs on `push`, `pull_request` and
`workflow_dispatch`, never on a schedule, so a year with none of those revokes the token and the
next PR after that is frozen. That is the same failure class as "Scheduled workflows go dark on a
quiet repo" below: a quiet repo disarming its own machinery on a clock nobody is watching.

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

## Source API keys (and why none of them may enter a stored URL)

Pinning documents needs four optional credentials. All four are read from the **environment** at
fetch time; in CI they are repo secrets wired into `.github/workflows/sources-drift.yml`.

| env var | needed for | where to get it |
|---|---|---|
| `GOVINFO_API_KEY` | `govinfo` metadata **and** re-fetching the stored PDF | api.data.gov |
| `OPENFEC_API_KEY` | `openfec` legal search (the metadata call only) | api.data.gov — the same key works for both |
| `COURTLISTENER_TOKEN` | `courtlistener` **search and `add`** — never `check` (see below) | courtlistener.com account |
| `WAYBACK_ACCESS_KEY` + `WAYBACK_SECRET_KEY` | authenticated Save Page Now (SPN2) | archive.org account |

**A key never enters a stored URL.** This repo is public, so an `?api_key=...` interpolated into a
`canonical_url` would be a committed secret — published the moment it is pushed, and live until
someone notices and rotates it. So the fetchers store the key-free content URL (govinfo's
`download.pdfLink`, OpenFEC's joined `fec.gov` URL) and re-attach the key from the environment on
each request. This is enforced three ways, not just documented: `Source` refuses a `canonical_url`
whose query carries a credential parameter (`api_key`, `access_key`, `token`, `key`, `sig`,
`signature`, `secret`, or any `x-amz-*` — so an S3 presigned URL is refused too), `pin()` refuses
one before it makes any request, and a test asserts no file under `data/sources/` contains a key.

If a key does leak into a stored URL: rotate it first (assume it is burned), then fix the fetcher,
then re-pin. Deleting the file is not enough — it is in the git history.

Local use: export the keys in your shell, or keep them in an untracked `.env` you source. Never put
a key on a command line you'll push, and never paste one into a PR or chat.

**`COURTLISTENER_TOKEN` is a local key, not a CI one.** It is needed for `pin search
courtlistener` and for `pin add courtlistener`, which read the v4 API. It is **not** needed by
`check`: what a courtlistener pin stores is a public file — a court's own PDF, or a scan on
`storage.courtlistener.com` — so re-fetching it sends no credential. `content_request` attaches
the token only to URLs under the API, and nothing stores one of those. A courtlistener pin is
therefore checkable from a CI runner with no secret set, the same as an eCFR or Federal Register
pin, and `sources-drift` does not need this key to stay green.

CourtListener throttles new accounts to **5 API requests a minute**, and a single `add` spends
four (cluster, opinion, docket, court). Space runs accordingly; a `search` costs one.

## `fetcher_verified`: the convention for flipping it

Each fetcher module carries `VERIFIED` / `VERIFIED_AT`, stamped onto every record it mints. The
rule, and it is the whole point of the field:

- **A fetcher ships `False`.** A claim in a handoff, a spec, or a docstring is not a verification.
  Only a run is.
- **It flips to `True` individually, on a live `add` through that fetcher in this tree**, dated the
  day it ran. One fetcher at a time — exercising `ecfr` says nothing about `openfec`.
- **Any edit to a fetcher's `spec()` or its parsing resets it to `False`** until someone runs it
  live again. This includes changing a URL template, a field name read out of a response, a date
  parse, or a fallback. The flag records "this code path has been run against the real API", and
  the moment the code path changes, the old run no longer covers it.

**How a first live run actually goes.** A verifying `add` straight into `data/` cannot work.
`spec()` stamps the record when it is minted, the stamp is never retyped per record, and the flag
still says `False` at that moment — so the run would verify the fetcher and leave behind a record
saying it hadn't been. The order that does work:

1. The first `add` for a fetcher runs with `--data-dir` pointed outside the repo, and every raw
   response it reads is captured there, untrimmed and dated, per the fixture rules below.
2. Each field `spec()` maps is checked against those captures. A mismatch is a parse bug, and
   fixing it edits `spec()`, which voids the run — the corrected path has never been exercised.
3. Only then do `VERIFIED` / `VERIFIED_AT` flip, to the scratch run's UTC day, and the fetcher's
   tests move onto the captures.
4. The real record is minted into `data/` with every date pinned rather than `latest`, and its
   drift value is compared with the scratch record's. A difference there is a document that moved
   between the two runs, not a working fetcher.
5. The scratch record's fields go in the PR body. The scratch dir is evidence nobody else can
   check, so it has to be quoted to be reviewable.

`xr_src_0001` and `xr_src_0002` are how this was learned: the run that verified `ecfr` minted them
while the module still said `False`, and they had to be re-minted before they could be committed.

`manual` is `True` by a different route: a human types every field, so there is no mapping that can
be silently wrong. That says nothing about whether they typed the *right* thing — no flag can carry
that.

`validate` counts unverified records and tags each line. Nothing blocks on the flag; it exists so a
reader can never mistake an unexercised parse for a checked one.

**Fixtures for external APIs are captured from a live response and dated, never authored.** A
fixture the code's author wrote tests the author's assumptions, not the API. They agree by
construction, so the test passes and proves nothing. This is not hypothetical here: the first
version of the eCFR amendment narrowing matched on a `section` key, with a hand-written fixture
that supplied one. The live endpoint has never returned that key — a provision below subpart level
is identified by `identifier` plus `type` — so the filter excluded every entry and a section pin
could never report `AMENDED`. The tests were green throughout.

The rules that follow from it:

- Capture live, store under `tests/fixtures/` with the capture date in the filename, and load it;
  don't paste an abbreviated version into the test module.
- Trim nothing. A capture is evidence; an edited capture is an assumption again.
- Assert the key set, so an invented field and a dropped field both fail (see
  `test_captured_fixture_matches_the_observed_live_key_set`).
- Need an error case the live response doesn't contain? **Mutate a copy of the capture** — blank a
  field, append an entry — rather than inventing a payload shape.
- When a capture stops matching the live API, **re-capture it**. Editing it by hand to make the
  suite green converts a real finding about an upstream change into a hidden assumption.
- This is the same discipline `fetcher_verified` enforces at the record level: a claim about an
  external system only counts if it came from that system.

**Second worked example, same root cause.** The `/titles.json` fixture was also authored: a
two-entry dict with the two titles the test happened to need. The live index carries all 50, so the
"unknown title" test used title 42 — which is a real CFR title, and would have matched against the
real response. The authored fixture agreed with the test's assumption and hid that too. The capture
forced the test onto title 99, which is not a CFR title and cannot silently start existing.

**A behaviour with no test guarding it is a behaviour that will regress silently.** Demonstrating a
fix by hand in a terminal proves it works today and nothing more. Two rules follow:

- When a fix lands, ask which test fails if it is reverted. If the answer is none, the fix is not
  finished. `removed`-entry handling was correct for a whole commit with nothing watching it,
  because every removed entry in the capture was a part-level appendix and the narrowed paths were
  never exercised.
- Prove a guard has teeth before trusting it: reinstate the old behaviour, confirm the new test
  fails and that nothing unrelated does, then restore. A test that passes both ways guards nothing.
- Where the live capture cannot produce the case, mutate a copy to produce it, and assert the
  mutation stayed inside the observed key set so it cannot drift into fiction.

## Searching: `pin search`

    python -m registers_crosswalk.pin search courtlistener "Dunne v. United States"
    python -m registers_crosswalk.pin search courtlistener "Phang v. Blanche" --type dockets

A case name or a respondent goes in; the identifier `add` needs comes out. Each line is
`identifier  date  label  docket_or_number  citation`, and the last line is the `pin add` that
would pin the first hit, ready to paste. `--json` prints the hits instead.

**It only reads.** `search` writes nothing, touches `data/` not at all, and answers a question
about the publisher's index rather than about what this repo holds. The API key is used for the
query and then forgotten: it is never stored, never printed, and never reaches a hit's `url` —
the same rule as `canonical_url` (decision 1, above).

Searchable fetchers are the ones that expose `SEARCH_TYPES`; `pin search --help` lists them and
the `--type` values each accepts. `courtlistener` takes `opinions` (the default) and `dockets`.
A docket hit is a lookup aid, not a pin: its id is a docket id and `add` wants a CLUSTER id, so
the command prints "not pinnable directly" rather than an `add` that would fetch the wrong thing.
`openfec` exposes `advisory_opinions` and `murs`, but its search has never been run — see below.

## `courtlistener` was verified live on 2026-09-20, and 138 F.2d 137 is pinned

A scratch `add courtlistener --cluster-id 1481640 --archive` outside the repo ran the current code
path end to end against *Dunne v. United States*, 138 F.2d 137 (8th Cir. 1943) — the first court
opinion New Gray's source-links ledger cites. Every field of the record it minted was compared
against the four captured responses. The committed pin is `xr_src_0006`, archived, carrying the
same sha256 as the scratch record. The fetcher now ships `VERIFIED = True`,
`VERIFIED_AT = 2026-09-20`.

The run cost two `spec()` corrections, which is exactly what a first live run is for.

**A cluster has no `court` key.** Not null — absent. The deciding court is reachable only as
`cluster.docket`, a URL to the docket, whose own `court` is a URL to the court, whose `full_name`
is the answer. `spec()` therefore makes **four API calls**: cluster, opinion, docket, court. Before
the fix, `cluster.get("court") or PUBLISHER` fell through silently and every courtlistener record
would have been published by "CourtListener (Free Law Project)" — the archive that served the
file, not the court that decided the case. That is the kind of wrong a test cannot catch, because
the code did exactly what it said; it just said the wrong thing.

**Pre-1980 opinions have no court PDF, and pin through the Harvard scan.** Dunne's opinion carries
`download_url: null` *and* `local_path: null`. Its text is in CourtListener's database
(`html_lawbox`, `xml_harvard`), and text in a database is not a document you can hash. The document
was on the CLUSTER all along, under `filepath_pdf_harvard` — the Harvard Caselaw Access Project's
page scan of the reporter volume, served from `storage.courtlistener.com`.

This is the rule for old cases, not an edge. Across the twenty hits the Dunne search returned,
**every pre-1980 opinion had both file fields null**; only 1982-and-later ones carried a
`download_url`, and those pointed at `bulk.resource.org` or the court's own site. A 1943 opinion
has no court PDF because in 1943 there was no such thing. Since the ledger's court citations are
almost entirely historical — 1932 to 1969, plus one 1994 — without this branch `courtlistener`
could pin essentially nothing the essay actually cites.

So `_document` has three branches, and they are graded apart on purpose:

| branch | grade | why |
|---|---|---|
| `opinion.download_url` | **A1** | the court's own file: the publisher of record |
| `cluster.filepath_pdf_harvard` | **B1** | B — an institutional archive is not the publisher. 1 — the document is a page image of the very reporter the citation names, so "138 F.2d 137" resolves to a photograph of page 137 of volume 138, checkable against any other copy of that volume |
| `opinion.local_path` | **B2** | B for the same reason. 2 — a copy of whatever the court served the day CourtListener fetched it, with nothing in the record letting a reader confirm it against the authority |

The Harvard scan outranks the mirror on credibility, not reliability: same custodian, better
evidence. A cluster with none of the three raises rather than pinning the case name.

**`local_path` is still unverified.** Dunne did not reach that branch, so the 2026-09-17 assumption
that it resolves under `storage.courtlistener.com/` has never been exercised against the live API.
The tests cover the branch by mutating a copy of the capture, which tests our code, not theirs.
A pin through a modern opinion would close it.

**courtlistener pins are archived by policy, not by code.** `REQUIRES_ARCHIVE` stays off — a court
PDF is static, like a Federal Register one — but court websites reorganise URLs often and the
Harvard scan is served by a third party, so these pins are made with `--archive` as a matter of
practice. The 8th Circuit's 1943 volume is not going to change; where it lives might.

**`openfec` remains unverified.** The ledger cites no MUR and no advisory opinion — its only FEC
citation is a committee data page, which the legal-search endpoint does not serve — so there was
no document to run a first search or a first pin against. `search()` exists for it and is covered
only by the test a capture is not needed for: that the query URL is built correctly and that the
key never leaves it. Treat both halves of that module as unexercised until a real MUR turns up.

## `add` cannot write a record that fails to load

Before writing, `pin add` runs the would-be record through
`registry.check_source_invariants` — the same function `Crosswalk` applies on load — against every
existing source plus the new one. On failure it prints the invariant's own message, writes nothing,
and exits non-zero. One rule, one implementation, one message: there is no second copy of the
checks inside the CLI that could drift from the authoritative set.

Consequences worth knowing:

- **`--cited-in` requires `--archive`**, and is refused up front with `cited sources must carry an
  archive copy; pass --archive`. It does *not* imply `--archive`: archiving is a separate act with
  its own failure mode, and performing it silently would hide that from you.
- **`--archive` is a requirement, not a courtesy.** If the capture fails, nothing is written and
  the message names the archive step. Re-run when the service is back.
- **A duplicate `(canonical_url, point_in_time)` is caught twice**, and deliberately so. An early
  `registry.duplicate_of` call runs right after the crosswalk loads, before the document is
  fetched and before anything is sent to archive.org, so re-pinning a version we already hold
  costs nothing and does not ask a third party to capture a URL that is already pinned. The full
  `check_source_invariants` still runs before the write. Both go through the same
  `duplicate_of()`, so the shortcut cannot disagree with the authority — it is one rule read
  twice, not two rules.
- **There are two duplicate rules, and they catch different things.** Same URL at the same
  `point_in_time` is the same *bytes* pinned twice. Same citation at the same `drift_value` — over
  live pins only — is the same *document* pinned twice, under a URL or a date that happened to
  differ. The second runs through `registry.same_document_of` and it too is read twice, by the
  same `check_source_invariants` and by an early exit in `add`.
- **Where each one runs, and why there.** The URL rule runs before `pin()` fetches, because
  nothing the fetch could return would change its answer. The same-document rule cannot: a
  `drift_value` is only knowable once the document has been read. So it runs *after* the fetch and
  *before* `archive_fn` — a refused pin should still cost no Save Page Now capture, which is slow,
  rate-limited, and leaves a public artifact behind for a record that was never written.
- **`--supersedes` is the override, and it is not a flag.** The rule counts live pins, and naming
  the earlier pin makes it not live. So `add ... --supersedes xr_src_NNNN` re-pins an unchanged
  document on purpose, and the refusal message says so and names the id to pass. There is no
  `--force` and no `--allow-duplicate`: superseding is the assertion the operator is actually
  making. Two *different* sections that share a `drift_value` — one law amended both, so both
  credits end on the same date — are not duplicates, because their citations differ.
- A refused `add` may still have written the content-addressed blob to `--blob-dir`. That file
  lives outside this repo, is named by its own hash, and is rewritten identically on the retry.

## Source drift: what red means

`.github/workflows/sources-drift.yml` runs `python -m registers_crosswalk.pin check` weekly
(Mondays 12:00 UTC) and on `workflow_dispatch`. By default it checks the **latest pin per citation**
(superseded pins and `merged_into` losers are skipped); `--all` forces every pin.

**Exit code → cause.**
- **0 — clean.** Every checked pin still matches. Nothing to do.
- **1 — the check ran and found a problem with the document.** Either the drift key changed
  (`DRIFT`), the eCFR part was amended since the pin (`AMENDED`), or the document no longer states
  what we parse out of it (`ERROR` — uscode dropped its source credit, an API changed shape). All
  three are real findings about the world, and all three need a human to look and re-pin.
- **2 — the check could not run: an API key isn't set** (`KEY_MISSING`). A configuration problem
  on our side, not a finding. The failing line names the env var. Fix the secret and re-run; until
  then you know nothing about those sources.
- **3 — the check could not run: the transport failed** (`FETCH_FAILED`) — timeout, non-2xx, DNS,
  connection refused. Also not a finding. Usually transient, so re-run before investigating; if it
  persists, a 404 on a canonical URL means the document moved, which *is* a finding.

**Why 3 is separate from 1**, and the rule to keep: a dead endpoint must never be reported as a
changed document. They demand opposite responses — one is "wait and retry", the other is "read the
new text and re-pin" — and a checker that conflates them teaches you to ignore both. In the code
this is the split between `except OSError` (every urllib transport error subclasses it) and
everything else. When a run mixes statuses, the precedence is **2 > 3 > 1 > 0**: fix the
environment, then the network, and only then read the drift answers, which are not trustworthy
until every source was actually reachable.

Each line is one of six statuses, and they mean genuinely different things:

- **`OK`** — the document still matches what was pinned. Nothing to do.
- **`AMENDED`** (eCFR only) — the bytes at the point-in-time URL are unchanged, *but the part has
  been substantively amended since the pin*. This is the status that matters most and the one a
  pure hash check would miss entirely: the eCFR point-in-time URL keeps returning the old text
  forever, so a green hash proves nothing about whether the law changed. Action: pin the new `as_of`
  and set `--supersedes` to the old id. Do **not** edit the old pin — an argument that cited the
  old text still cites the old text.
- **`DRIFT`** — the drift key changed. For a PDF or an XML snapshot that is alarming: a document
  that was supposed to be immutable was replaced, so go look at what changed before re-pinning. For
  an HTML page pinned through `manual`, it is usually just noise (see below). For a `uscode` pin it
  is neither: the drift key is the latest date in the section's source credit, so a later date
  means a law amended that section. Read what changed, pin the new text, and set `--supersedes` to
  the old id — the same response as `AMENDED`, reached by a different route.
- **`KEY_MISSING`** — the check could not run because an API key isn't set. The run fails on
  purpose: a check that quietly skipped the sources it couldn't reach would report green while
  telling you nothing.
- **`FETCH_FAILED`** — the document could not be reached at all: timeout, non-2xx, DNS failure,
  connection refused. Exit 3, never exit 1, because this says nothing about whether the document
  changed. Re-run first; a 404 that persists means the document moved, which *is* a finding.
- **`ERROR`** — the fetch worked but the parse blew up: a page that no longer states its currency
  date, an API that changed shape. Exit 1, because the document really did change under us — just
  not in a way the drift key could measure.

**HTML sources are noisy.** A page pinned through `manual` has its raw hash as its drift key, so a
nav change, a cookie banner or a rotating build id reads as DRIFT. That is the honest answer for a
page with no version axis, but before pinning HTML check whether the same document exists as a PDF
or in eCFR / GovInfo / the Federal Register, and pin it there instead. `uscode` is the one HTML
fetcher that was meant to dodge this: its drift key is the "laws in effect on" date the page
states, so markup churn is ignored.

**That date is site-wide, not per-title — observed 2026-09-18.** 52 U.S.C. § 30116 and
1 U.S.C. § 1 both read "laws in effect on September 17, 2026", though the latest OLRC release
point affecting title 52 was Public Law 119-73 (2026-01-23) and the latest affecting title 1 was
Public Law 119-103 (2026-09-02). The current release point, Public Law 119-108 (2026-09-11),
affects titles 26, 31 and 40 — neither of theirs — and the stated date is later than it besides.
So the date follows OLRC's publishing schedule, not the section's text, and a `uscode` pin would
report `DRIFT` every time OLRC republishes, whether or not its section changed. That is the failure
the drift key was chosen to avoid. It opened a second hole on the write side: a URL with no version
axis plus a date that moves on its own means `add uscode`, re-run after any republish, cleared the
`(canonical_url, point_in_time)` rule and minted a second pin of an unamended section. The
same-document rule above is what closed it.

**The key was redesigned on 2026-09-18.** `uscode` now uses `last_amended`: the latest date in the
section's source credit, the parenthetical after the text that lists the enacting law and every law
that has amended the section. That date moves only when a law amends that section, which is what
the currency date was wrongly assumed to do. `currency_date` is retired — `Artifact` rejects it, so
the disproven key cannot return through a record or a fetcher. The stated "laws in effect on" date
is still read and still supplies `point_in_time` and the title: it is the document's own claim
about itself. It is simply not the signal.

The parse is scoped to the source-credit element, never the whole page. Notes below the text cite
later laws that did not amend the section, and on 2026-09-18 the 1 U.S.C. § 1 page carried 35 such
dates later than its credit's own latest.

**What it misses, and why `--archive` is now enforced in code.** A `last_amended` pin does not
notice an editorial change to the text or notes that adds no law to the credit. What holds the
pinned text is the archive copy, so `uscode` sets `REQUIRES_ARCHIVE` and `add uscode` without
`--archive` is refused before any fetch, with `uscode pins must carry an archive copy; pass
--archive`. Once the section is amended the canonical URL serves the new text and cannot reproduce
what was pinned, so an unarchived `uscode` pin would go straight to `DRIFT unrecoverable (no
archive)`. A Federal Register PDF is static and an eCFR point-in-time URL is dated, so neither
needs one.

**`uscode` was verified live on 2026-09-19, and § 30116 is pinned.** A scratch
`add uscode --title 52 --section 30116 --archive` outside the repo minted a record through the
current code path — `point_in_time` 2026-09-18 from the page's own currency sentence,
`drift_value` 2014-12-16 from the source credit's latest date — and `check` on it exited 0. The
committed pin is `xr_src_0005`, archived, carrying the same `drift_value`. The fetcher now ships
`VERIFIED = True`, `VERIFIED_AT = 2026-09-19`. SPN2 answered the real pin with the capture the
scratch run had just made, so the record's `captured_at` precedes its `fetched_at`: expected, and
not a defect, because what the archive preserves is the credit date and the text, not the bytes,
which vary from request to request.

**The header on the GET was not enough; `archive()` had to speak SPN2.** On 2026-09-18 Wayback's
unauthenticated `GET /save/` answered HTTP 500 for the § 30116 URL three times over twenty minutes,
while a control save of `https://example.com` returned 429 — that history is why the keys were
obtained. With the keys set, the same `GET /save/` carrying `Authorization: LOW …` answered 500
again on 2026-09-19 (`X-location: save-sync`), so the authenticated path was never a header on the
old request. `archive()` now posts to SPN2 (`POST /save` with `url=`, then polls
`GET /save/status/{job_id}` until `status` is `success` and reads `timestamp`) whenever both keys
are set, and falls back to the anonymous synchronous save when they are not. The first capture
through that path succeeded in about twenty seconds. The POST and the poll go through injectable
callables, so the tests for success, pending-then-success, error and timeout stay offline.

The keys live in a gitignored `.env` read into the process environment for a run. A `uscode` pin
needs them there: `REQUIRES_ARCHIVE` refuses the pin without a capture, and the synchronous save
path 500s for these URLs with or without an Authorization header, so an unkeyed run cannot produce
one. Neither key belongs in CI: nothing in the workflows archives, so an unkeyed CI checkout takes
the anonymous path and never needs it. Perma.cc stays a TODO in `archive()` rather than the next
step — a keyed capture now works, so there is nothing for it to rescue.

## The status page is generated, never committed

`.github/workflows/status-page.yml` runs `pin check --json`, feeds the JSON to
`python -m registers_crosswalk.status`, and publishes the one HTML file it writes to GitHub
Pages. Reproduce it locally with `python -m registers_crosswalk.status --out
build/status/index.html` (add `--drift-json` if you have a check's JSON to hand).

- **Generated, not tracked.** The page lands in `build/`, which is gitignored, and in the Pages
  artifact in CI. No HTML is committed, so there is nothing in the tree that can go stale and
  nothing to regenerate by hand after a `pin add`.
- **It reads two things and nothing else:** `data/` and the check's JSON. It has no fetch of its
  own and never reads `.env`. Everything it shows is already public in this repository, which is
  what makes publishing it a non-decision.
- **The workflow exits with the check's code.** The check step is `continue-on-error` and its exit
  code is carried to the last step, which re-raises it. The page is therefore built and deployed
  whatever the check found: a red check is a red run *and* a red page at once, and the page can
  never show green under a failing check. That is the whole reason the check and the build sit in
  one job rather than two.
- **A drifted pin with no archive is the row to read first.** The archive column says
  `none, drift unrecoverable` there, because the digest has become the only surviving evidence
  that the old text existed and it cannot say what it said. Everything above about `uscode` and
  `REQUIRES_ARCHIVE` is aimed at never seeing that line.
- **Pages must be switched on once, by hand:** Settings → Pages → Source → **GitHub Actions**.
  Until then the deploy step fails and nothing else is wrong.
- **`sources-drift.yml` is untouched.** It stays the check to watch, and its red badge keeps its
  meaning. This job is a second reader of the same answer, not a replacement — which is also why
  the section above still describes the only drift runbook there is.

## Scheduled workflows go dark on a quiet repo

**GitHub disables a `schedule:` trigger after 60 days with no repository activity.** It does not
fail; it simply stops running, and `sources-drift` reports nothing rather than reporting green.
This repo is exactly the kind that goes quiet — the crosswalk is small and stable, and a two-month
lull between commits is normal — so the drift check is genuinely at risk of switching itself off
in the period you most need it.

- **Symptom.** No `sources-drift` runs in the Actions tab for weeks, with no red. Absence of red is
  the tell; there is no notification.
- **Restart.** The workflow carries `workflow_dispatch`, so run it by hand — Actions →
  *sources-drift* → *Run workflow*, or `gh workflow run sources-drift.yml --repo
  CSU-J3/registers-crosswalk`. GitHub also emails the repo admin before disabling, and a manual run
  or any commit resets the 60-day clock.
- **Check it is still armed.** `gh workflow list --repo CSU-J3/registers-crosswalk` shows the
  workflow's state; anything other than `active` means the schedule is off.
- Treat a long quiet spell as a reason to dispatch the job manually, not as evidence that nothing
  drifted.

## Tracked chores

- **Node 20 → newer action majors.** `actions/checkout@v4` and `actions/setup-python@v5` currently
  run on Node 24 with a deprecation warning (Node 20 sunset, GitHub 2025-09-19). Bump to action
  majors that target Node 24 across every workflow (`ci.yml`, `cross-repo.yml`,
  `sources-drift.yml`, `status-page.yml`). Warning only, non-urgent; do it in
  step with the sibling repos so all four move together.
