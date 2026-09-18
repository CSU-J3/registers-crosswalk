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

**Rotate** (fine-grained PATs expire; plan for it):
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

**`SIBLING_REPOS_TOKEN` expires:** _(not recorded — read it from the settings page above and fill
this in; the secret was set 2026-07-05, and fine-grained PATs cap at 366 days, so it is on or
before 2027-07-06)_

Why this matters more than it used to: `main` now requires `cross-repo-refs` and
`join-integration`, both of which check out the private siblings with this token, and
`enforce_admins` is on. An expired PAT is therefore a repo-wide merge freeze that cannot be
overridden from the UI, on PRs that have nothing to do with the siblings.

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
| `COURTLISTENER_TOKEN` | `courtlistener` cluster/opinion API | courtlistener.com account |
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
  what we parse out of it (`ERROR` — uscode dropped its currency line, an API changed shape). All
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
  an HTML page pinned through `manual`, it is usually just noise (see below).
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
fetcher that dodges this: its drift key is the "laws in effect on" date the page states, so markup
churn is ignored and only an actual advance of the text reports drift.

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
  majors that target Node 24 across `ci.yml` and `cross-repo.yml`. Warning only, non-urgent; do it in
  step with the sibling repos so all four move together.
