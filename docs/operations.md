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
fetch time. CI needs no API secret: every stored canonical URL is key-free, so `check` sends no
credential. The api.data.gov key behind `GOVINFO_API_KEY` and `OPENFEC_API_KEY` lives only in the
local `.env` and is not an Actions secret. A govinfo pin stored on `api.govinfo.gov` would need
`GOVINFO_API_KEY` wired back into `sources-drift.yml` and `status-page.yml` on purpose; openfec
and fecfiling never attach their key in `check`, so a pin stored on `api.open.fec.gov` would need
code before it needed a secret. `COURTLISTENER_TOKEN` stays wired in both as an optional secret,
unset (below).

| env var | needed for | where to get it |
|---|---|---|
| `GOVINFO_API_KEY` | `govinfo` metadata (the summary call only) — never `check` | api.data.gov |
| `OPENFEC_API_KEY` | `openfec` legal search and `fecfiling` filings metadata (the metadata calls only) — never `check` | api.data.gov — the same key works for both |
| `COURTLISTENER_TOKEN` | `courtlistener` **search and `add`** — never `check` (see below) | courtlistener.com account |
| `WAYBACK_ACCESS_KEY` + `WAYBACK_SECRET_KEY` | authenticated Save Page Now (SPN2) | archive.org account |

**A key never enters a stored URL.** This repo is public, so an `?api_key=...` interpolated into a
`canonical_url` would be a committed secret — published the moment it is pushed, and live until
someone notices and rotates it. So the fetchers store the key-free content URL (govinfo's
`www.govinfo.gov/content/pkg/...` copy, OpenFEC's joined `fec.gov` URL) and use the key only for
the metadata call. This is enforced three ways, not just documented: `Source` refuses a `canonical_url`
whose query carries a credential parameter (`api_key`, `access_key`, `token`, `key`, `sig`,
`signature`, `secret`, or any `x-amz-*` — so an S3 presigned URL is refused too), `pin()` refuses
one before it makes any request, and a test asserts no file under `data/sources/` contains a key.

If a key does leak into a stored URL: rotate it first (assume it is burned), then fix the fetcher,
then re-pin. Deleting the file is not enough — it is in the git history.

**Nor does a key reach an error.** api.data.gov takes its key as the `api_key` query parameter, and
urllib and http.client copy the request URL into their exceptions: `HTTPError` keeps it as `url`
and `filename`, `InvalidURL` quotes the whole path and query in its message. So govinfo, openfec
and fecfiling build every keyed URL with `apikey.keyed_url` and make every keyed metadata call
through `apikey.keyed_fetch`, which URL-encodes every value in the query and masks the key in any
exception that leaves the call, keeping its type so every handler still matches. The one
exception is a refusal of the key itself, a 401 or 403 from a keyed host or an `API_KEY_*` code,
which leaves as a `CredentialFailure` (exit 4, *Source drift: what red means*, below). `check` and
`blobs` re-fetch a pin on a keyed host with its key attached, and mask it out of the detail they
report; no pin is on one. Before `keyed_fetch`, `pin add openfec --number "MUR 8098"` put the space
into the URL raw, and http.client refused the request line with an `InvalidURL` that printed the
key. `tests/test_apikey.py` fails transports on purpose, quoting the URL they were given, and
checks stdout, stderr, the printed traceback and the console's JSON for a sentinel key. What it
does not cover: a locals-capturing traceback (`pytest -l`, rich, Sentry) still shows the key in the
frames that held it.

Local use: export the keys in your shell, or keep them in an untracked `.env` you source. Never put
a key on a command line you'll push, and never paste one into a PR or chat.

**One api.data.gov key per project.** That is the ruling across CSU-J3: a project that calls an
api.data.gov API must hold a key no other project uses, so one project's traffic can't disable
another's fetchers. The rule exists because the key under `CONGRESS_API_KEY` was shared with
psephos and api.data.gov disabled it for both (the record is in the `openfec` section, below).

**No LegiScan key.** This repo holds none and makes no LegiScan call. A future unit that wants
state-legislation data reads psephos's published `data/state_bills.json` (CC BY 4.0, attributed)
and never registers a second LegiScan key: a second registered key is what LegiScan bans for.

**Secret audits.** The standing rule, in the text psephos adopted on 2026-09-24:

> Secret audits search by variable name or compare against a hash of the value. They never match
> on the value itself, never snapshot process lists while a secret is in flight, and stay inside
> the repos and services the handoff names. "Anywhere" means the project's own surfaces, not the
> user folder, registry, Recycle Bin, or mail cache.

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
- **The one exception is narrow:** a `spec()` edit keeps `VERIFIED` only when a test proves
  byte-identical requests for every recorded verification input. `tests/test_verified_requests.py`
  is that test for govinfo, openfec and fecfiling, which kept the flag when their keyed calls
  moved onto `apikey.keyed_fetch` on 2026-09-25. It replays each verification run's captures
  through `pin add` and the keyless `check` that followed, and compares every request, keyed and
  keyless, in order, against the old URL templates kept verbatim as the oracle. Then it compares
  old and new over generated inputs: from the unreserved set `[A-Za-z0-9._~-]` where the old
  template put input into the URL raw, and on any input where it already encoded it. If it ever
  fails, the fetcher it names goes back to `False`.

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
Harvard scan is served by a third party, so these pins carry an archive as a matter of practice:
**archived at pin time with `--archive`, or with `pin archive` promptly after.** The 8th Circuit's
1943 volume is not going to change; where it lives might.

The second route exists because `add --archive` is all-or-nothing — the capture must succeed or
the pin is not written — and a Wayback outage should not cost a good fetch of a document whose
archive is practice rather than requirement. `pin archive xr_src_NNNN` adds the capture afterwards
without re-fetching the document or re-deciding anything about it, through the same
reuse-or-capture path `add` uses, and rewrites only the record's `archives` field. It refuses a
pin that already has one. "Promptly" is the whole of the discipline here: a courtlistener pin left
unarchived is a pin whose URL may move before anyone notices, and nothing enforces it but this
sentence and the console's list of pins with no archive copy.

**`uscode` keeps `--archive` at pin time, and that is not negotiable.** Its URL serves whatever is
current, with no version axis, so the canonical URL cannot reproduce the text that was pinned: an
unarchived uscode pin is unrecoverable the moment the section is amended, and there is no later
moment at which `pin archive` could capture what it should have captured. `REQUIRES_ARCHIVE`
refuses the pin up front for exactly that reason.

## `openfec` was verified live on 2026-09-21, and MURs 8098 and 8111 are pinned

A scratch `add openfec --number 8098 --type murs --document 100512215` outside the repo ran the
current code path end to end against the FEC's certification of the 6-0 dismissal vote in MUR 8098,
one of the two Cory Mills (FL-07) matters the claims ledger cites as "dismissed 6-0, documents A1"
with no pin behind them. Every mapped field was compared against the captured search response, and
`check` came back clean. Six documents are committed, `xr_src_0008` through `xr_src_0013`, three per
matter; the certification carries the same sha256 as the scratch record. The fetcher now ships
`VERIFIED = True`, `VERIFIED_AT = 2026-09-21`.

**The MUR number parameter is `case_no`, and `mur_no` was worse than wrong.** `mur_no` is what the
module had carried since 2026-09-17, marked unverified because no MUR had been pinned. The endpoint
does not reject it. It **ignores** it, answers 200, and returns the unfiltered first page of all
7,670 matters — twenty records, none of them the one asked for. `_pick_document` walks whatever it
is handed, so the pin would have gone through: a document belonging to some unrelated MUR, stored
under the citation `FEC MUR 8098`, with a real hash and a real URL and nothing anywhere on the
record to say it was the wrong document. Nothing would have failed. `check` would have stayed green
forever, because the hash of the wrong document is stable too.

This is the strongest case in this repo for why `fetcher_verified` is a flag on the *record* rather
than a note in a docstring. A silent filter failure produces a confident, self-consistent, durable
lie, and the only thing standing between that and the ledger is somebody running the thing once and
reading the answer.

**A MUR repeats its categories, so documents are selected by id.** `--document <document_id>` names
`documents[].document_id`; `--category` stays for advisory opinions, whose `Final Opinion` is
unique, and the two flags are mutually exclusive. The reason is visible in both matters: each one's
`Certifications` category holds two documents — the certification of the 6-0 dismissal and a later
certification substituting a treasurer's name — and the first-match rule could reach only one of
them. `add_command` prints `--document <document_id>` for MURs for the same reason.

**The three categories a MUR actually has**, observed across both matters (15 documents in 8098, 32
in 8111):

| category | what it holds | what a claim needs it for |
|---|---|---|
| `Certifications` | the Commission's vote certifications | **the vote** — the disposition and its date and tally |
| `General Counsel Reports, Briefs, Notifications and Responses` | the First General Counsel's Report, and the `Notification to …` letters closing the file | **the reasoning** (the FGCR) and **the disposition as served** (the notifications) |
| `Complaint, Responses, Designation of Counsel and Extensions of Time` | the complaint, notifications of complaint, designations of counsel, extensions, respondents' responses | the allegations as made — one party's assertion, not the Commission's finding |

Two things a reader expecting FEC practice will look for and not find. **There is no Factual and
Legal Analysis and no Statement of Reasons** in either matter's `documents[]`, although the 6-0 vote
expressly approves an FLA. **There is no closing-letter category**: the letters closing the file are
`Notification to …` documents dated 2024-09-05, sitting in the General Counsel bucket beside the
FGCR. So "the vote, the reasoning and the disposition" is three documents from two categories, which
is what each matter's three pins are.

**Dates come from `documents[].document_date`.** A MUR record carries `open_date` and `close_date`
and no `issue_date` at all, so the advisory opinion's record-level fallback never fires. Reading the
record date instead would have dated the First General Counsel's Report 2023-01-11, the day the
matter opened, rather than 2024-06-13, the day it was written.

**`documents[].length` is a free cross-check.** It equalled the fetched byte length exactly on all
six documents. Nothing in the code reads it; it is worth knowing when a fetch looks wrong.

**The record's `name` is not a caption.** It is the primary respondent — `Cory Mills` — and it is
the same string for both matters, so it distinguishes nothing. The citation therefore stays
`FEC MUR {n}`, which is how the matters are cited anyway, and the respondents ride onto the ledger
line through each document's own description. `--citation` exists for the cases where that is not
enough.

**`check` re-fetches fec.gov with no key, and CI needs no new secret.** The key authorizes the
legal-search call and nothing else: `documents[].url` is relative, joins against
`https://www.fec.gov`, and the PDF is served anonymously. This was proved before anything was
pinned — a keyless fetch of both certifications returned 200, `application/pdf`, and byte lengths
matching the API's own. `openfec` has no `content_request` for exactly this reason. `govinfo`
keeps one, but it attaches the key only to the API host, which no govinfo pin stores (below).

**The key itself is worth a note.** `OPENFEC_API_KEY` is an api.data.gov key and any api.data.gov
key works — but they are account-level and can be disabled account-wide. The first attempt at this
run used the same value as `CONGRESS_API_KEY` and got `403 API_KEY_DISABLED` on every call. The
value now in `.env` is the one `GOVINFO_API_KEY` uses. If openfec starts returning 403, `pin add`
and `pin search` read api.data.gov's code out of the body themselves and print it, `CREDENTIAL
FAILURE openfec 403 <code>`, exit 4 (*Source drift: what red means*, below). A rate limit is not
one of those: api.data.gov answers `OVER_RATE_LIMIT` with a 429, which stays the HTTPError it was.

**The disabled key was shared with psephos.** The value under `CONGRESS_API_KEY` was the
api.data.gov key this repo shared with psephos, and api.data.gov disabled it for both projects
between 16:56 and 21:15 UTC on 2026-09-16 (`403 API_KEY_DISABLED`). This repo never read
`CONGRESS_API_KEY` itself (there is no Congress.gov fetcher); the 403 above is the only place the
disabling shows up here. From git: the switch to the `GOVINFO_API_KEY` value is recorded on
2026-09-21 (`6b01c05`), and `OPENFEC_API_KEY` was copied from it the same day. The first live
keyed runs on that value are openfec on 2026-09-21, govinfo on 2026-09-22 and fecfiling on
2026-09-23. The 2026-09-17 "verified live" comments in `openfec.py` and `fetchers/__init__.py` are
not runs in this repo: `b9afc01`, the commit that added the fetchers, shipped openfec and govinfo
unverified because those notes "came from the spec work, not from a run here", and they are no
evidence about either key. What followed from the disabling — the one-key-per-project rule, no
LegiScan key, and the secret-audit rule — is under *Source API keys*, above.

**The free-text `search()` is still unexercised.** The run went through `spec()` by number, which is
a different query shape; `q=` has never been asked of the live endpoint. What the captures do cover
is `_hit`'s parse, since a search hit and a `spec()` record read the same MUR record. Treat the
query itself as unproven.

## `govinfo` was verified live on 2026-09-22, and the 2024 edition of § 30116 is pinned

Discovery came first, read-only: `USCODE-2025-title52` is 404 and `USCODE-2024-title52` is the
newest edition, issued 2024-12-31. § 30116 is granule
`USCODE-2024-title52-subtitleIII-chap301-subchapI-sec30116`, a `LEAF`. A scratch `add govinfo`
outside the repo then ran the current code path. Its citation, title, `published_at` (against
`dateIssued`), canonical URL, sha256 and byte length all matched the captures, and a `check` with
no key in the environment exited 0. The committed pin is `xr_src_0014`, with the same sha256 as the
scratch record. The fetcher now ships `VERIFIED = True`, `VERIFIED_AT = 2026-09-22`, and its tests
read the captured summaries.

**`check` re-fetches govinfo with no key, and CI needs no GovInfo secret.** The summary's
`download.pdfLink` is on `api.govinfo.gov` and wants the key. The same PDF is served anonymously at
`www.govinfo.gov/content/pkg/{package}/pdf/{granule or package}.pdf`. The two were fetched and
compared before anything was pinned: byte-identical for the § 30116 granule (154865 bytes) and for
the whole title 52 package (673832 bytes). So `spec()` stores the content URL and never the
`pdfLink`, and `content_request` attaches the key only to a URL on the API host. That is the same
shape as courtlistener's "check re-fetches the document without a token". The key is still needed
for `add govinfo`, which reads the summary.

**Two things cost a probe each.** The granules listing pages by `offsetMark`
(`?offsetMark=*&pageSize=100`, then `nextPage`), not by a numeric `offset`. And a section's number
is in its `granuleId` (`...-sec30116`), not its `title`, which is the bare heading. A search of
titles for "30116" finds nothing. The module docstring says both.

## `fecfiling` was verified live on 2026-09-23, and the Osborn For Senate reports are pinned

The essay's filing ledger says Osborn For Senate (`C00901355`) paid Helix Campaigns and Fight
Agency, sourced at B3. The upgrade to A1 is the committee's own filed report. Discovery was
read-only, into a scratch dir: the processed Schedule B rows to find the reports, then
`/v1/filings/?file_number=N` for each report's metadata, then a keyless fetch of the documents.
A scratch `add fecfiling --file-number 1903438 --document fec` ran the current code path. Its URL,
`published_at` (against `receipt_date`), sha256 and byte length matched the captured metadata and an
independent keyless fetch, and a `check` with no key in the environment exited 0. The fetcher now
ships `VERIFIED = True`, `VERIFIED_AT = 2026-09-23`. The tests read the two metadata captures under
`tests/fixtures/fecfiling_filings_*`, which are committee-level. The Schedule A and B pages and the
`.fec` contents are NOT captured: they carry individuals' names and addresses.

**Processed rows find a report; they are never the source.** `/schedules/schedule_b/` is the FEC's
coded copy of what was filed, and it is re-coded. Its `amendment_indicator` disagreed with the
filing's own on all five reports that itemized Helix Campaigns (`C` on reports that are `A`, `A` on
reports that are `N`, `N` on one that is `A`). So a Schedule B row is used to learn which file
numbers to read, and the pin is the report as filed.

**The filing is the evidence, the amendments included.** Every report in a chain is pinned, the
original first, each later one with `--supersedes` the previous pin of the same document kind, and
the default `check` still re-tests every filing in the chain (see "A superseded filing is still
checked" below). They are not clutter: the amendments re-described the Helix payments ("digital
fundraising" and "digital consulting" became "Digital Advertising") without changing an amount or
a date. Eighteen pins, `xr_src_0026` through `xr_src_0043`: nine filings across five reports (Q2,
Q3 and year-end 2025, Q1 and pre-primary 2026), a `.fec` and a PDF each. Each pin's notes carry the
chain's chronology, and a PDF carries the same notes as its `.fec` twin: `original, received
2025-07-15; itemizes Helix Campaigns`, or `amendment 2 of 2, received 2026-01-31; amends FEC file
1920944 (received 2025-10-15), original 1903438 (received 2025-07-15); itemizes Helix Campaigns`.

**A `.fec` is listed as `.fec`.** docquery serves it as `binary/octet-stream`, which says nothing
about the file, so the manifest and `--blob-dir` take the extension from the URL's own suffix when
the served type is generic. A specific type still wins, and no earlier pin's name changed.

**The `.fec` is the filing; the PDF is a rendering of it.** The `.fec` file is what the committee
submitted, and it is the primary object. The PDF is the FEC's image of it. If a PDF pin reports
DRIFT while the `.fec` pin of the same filing stays OK, the FEC re-rendered the image; the filing
did not change. An amendment never shows up as drift: it is a new filing with a new file number.

**Wayback could not capture docquery on 2026-09-23.** SPN2 answered
`error:invalid-host-resolution` ("Cannot resolve host docquery.fec.gov") for the first `.fec`, then
`error:not-found` (HTTP 404) for every other `.fec` and PDF, while docquery served each of them to
this tree with a 200 within the same minute. One PDF, `xr_src_0027`, already had a capture from
2025-07-28 whose bytes hash to the pin, and the reuse path adopted it. The other seventeen went in
unarchived, per the courtlistener fallback, and are listed for `pin archive`. They are public FEC
records that can be fetched again by file number, and each pin's sha256 is what any copy has to
match.

**The manifest lists what can be checked; the record lists what was pinned.**
`pin ledger --format manifest` prints `sha256sum` lines only for pins whose drift key is `sha256`,
the ones whose bytes can be re-obtained and checked, and names the rest on stderr. It is the same
list, byte for byte, as the `MANIFEST.sha256` that `pin blobs` writes for the same pins.
`pin ledger --format record` lists every pin: sha256, id, fetch time (UTC), drift key, `manifest`
or `record-only`, and citation. The three U.S. Code prelims (`xr_src_0005`, `xr_src_0024`,
`xr_src_0056`) are record-only. Their pages vary per request, so the bytes they hash to exist
nowhere, and their Wayback copies are their preservation copies. Both formats end lines with
`\n` on every platform, because a Windows redirect of `print()` wrote `\r\n`, and
`sha256sum -c` read the `\r` as part of each filename.

**The preservation copy.** For FEC filings, the preservation copy is New Gray's hashed blob, saved
under its manifest name; the FEC is the repository of record, with a statutory retention floor of
ten years from receipt, five for filings relating solely to House candidates (52 U.S.C.
§ 30111(a)(5); xr_src_0057, 2024 ed.; xr_src_0056, prelim). This command makes that copy:

    python -m registers_crosswalk.pin blobs --out <New Gray's pins directory>

For every pin (or the ones named with `--only`), it re-fetches the canonical URL through the
fetcher's own request path and writes the bytes under the pin's manifest name, but only if they
hash to the pin. A download that doesn't match writes nothing and is reported. A file that's
already there is never overwritten: identical bytes are left as they are, and different bytes are
reported. Then it writes `MANIFEST.sha256`, listing every file in that directory that hashes to its
pin, so `sha256sum -c MANIFEST.sha256` passes there. The manifest only ever grows. If the one
already there lists a line the run can't keep (a copy that changed or went missing, or a file this
command didn't write), it is left exactly as it is, the line is reported, and the run fails. The
uscode prelims always mismatch, because their pages vary per request; their Wayback copies are
their preservation copies, and those mismatches don't fail the run. The first run into New Gray's
pins directory was on 2026-09-24 (UTC). It wrote 54 files, all 30 FEC filings among them, and 3
weren't written: the expected mismatches of the uscode prelims `xr_src_0005`, `xr_src_0024` and
`xr_src_0056`. `sha256sum -c --strict MANIFEST.sha256` then passed there, 54 OK.

**Fight Agency is not a payee of this committee.** No Schedule B row names it: not in the 1,878
processed rows, not in the 866 raw e-file rows, and not in any of the nine filings. The name
appears once, in a Schedule A receipt, as a contributor's employer. That finding went back to the
essay chat. Nothing is pinned for it.

**`check` re-fetches docquery with no key, and CI needs no new secret.** The key authorizes the
filings metadata call only. `fecfiling` has no `content_request`, as `openfec` has none, and `spec()`
refuses to store any URL that is not a bare `https://docquery.fec.gov/...` URL.

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
(Mondays 12:00 UTC) and on `workflow_dispatch`. By default it checks **every live pin** — live as
`registry.live_sources` defines it, the same predicate the duplicate rule and the load-path
invariants use: not a `merged_into` loser, and not named by another pin's `supersedes` — plus the
superseded pins described next. `--all` adds the ones that default skips, so a run can ask whether
the documents behind the history are still reachable.

**A superseded filing is still checked.** A fetcher module may declare `CHECK_SUPERSEDED = True`,
and `check` then also re-fetches that fetcher's superseded pins by default. Only `fecfiling` does.
A superseded filing, the original or an earlier amendment, is a separate filing with its own file
number and URL, and it never changes; the record cites it alongside the amendment that replaced
it, so its bytes are worth re-testing. `ecfr`
and `uscode` keep the default `False`, for two different reasons. A superseded eCFR pin was
superseded because its part was amended, so it would report `AMENDED` forever. A uscode URL serves
only the current text, so a retired pin would read as `DRIFT`. On 2026-09-23 this took the default
from the 49 live pins to all 57. The eight added are the superseded Osborn For Senate pins: four
filings (1903438, 1920944, 1921751, 1967383), a `.fec` and a PDF each.

It used to check the *latest pin per citation*, and that was wrong for any citation naming a
proceeding rather than a text. One FEC MUR is one citation over several documents — the
certification of the vote, the First General Counsel's Report, the notification closing the file —
which are not versions of each other, so "latest" silently dropped all but one of them from every
drift run. On the six MUR pins currently in `data/`, four were going unchecked.

**Exit code → cause.**
- **0 — clean.** Every checked pin still matches. Nothing to do.
- **1 — the check ran and found a problem with the document.** Either the drift key changed
  (`DRIFT`), the eCFR part was amended since the pin (`AMENDED`), or the document no longer states
  what we parse out of it (`ERROR` — uscode dropped its source credit, an API changed shape). All
  three are real findings about the world, and all three need a human to look and re-pin.
- **2 — the check could not run: an API key isn't set** (`KEY_MISSING`). A configuration problem
  on our side, not a finding. The failing line names the env var. In CI only `COURTLISTENER_TOKEN`
  is wired; any other key has to be wired into the workflows on purpose first (*Source API keys*,
  above). Set it and re-run; until then you know nothing about those sources.
- **3 — the check could not run: the transport failed** (`FETCH_FAILED`) — timeout, non-2xx, DNS,
  connection refused. Also not a finding. Usually transient, so re-run before investigating; if it
  persists, a 404 on a canonical URL means the document moved, which *is* a finding.
  `check` retries these once, after one 30-second wait per run, so a reported one failed twice.
- **4 — `pin add` or `pin search` only: api.data.gov refused the key.** One line on stderr,
  `CREDENTIAL FAILURE <fetcher> <status> <code>`, and nothing written; the console shows the same
  line in its pane and prints it on its terminal. `check` never exits 4: it makes no keyed
  metadata call, and no pin sits on a keyed host. It is not retried: a refused key stays refused.
  (A key that is not set at all is exit 2 from `add` and `search`, after one line naming the
  variable.) The code is api.data.gov's, echoed only when it is shaped like one:
  - `API_KEY_DISABLED`: api.data.gov has turned the key off account-wide, for every project that
    holds it. Stop using it, and get a new one for this project alone (*Source API keys*, above).
  - `API_KEY_INVALID`: the value is not a key api.data.gov knows: a typo, a truncation, a stray
    quote. Compare its hash prefix against the one you expect, never the value itself.
  - `API_KEY_MISSING`: the request reached api.data.gov with no key although one was set. Check
    the variable in-process for surrounding whitespace or quotes, by its length and hash prefix,
    never by printing it.
  - `UNKNOWN`: a 401 or 403 whose body names no code. The tool keeps only the code, so to read the
    rest, repeat the call in a Python session through `pin.default_fetch` and print the
    `HTTPError`'s body, never its URL.
  - any other code: api.data.gov's own, echoed because it came with a 401 or 403 from a keyed
    host. Look it up in api.data.gov's developer manual before changing anything.

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

**The signed annual edition is pinned beside it, as `xr_src_0014`.** `xr_src_0005` is the prelim
text: its URL serves only the current version, which is why its drift key had to be redesigned
twice and why it cannot stand without an archive. The 2024 annual edition of the same section,
through `govinfo`, is a fixed published PDF with one sha256 that never moves. Neither supersedes
the other. They are two documents of the same law with different provenance, and the register
holds both because the difference between "what the section says today" and "what the signed
edition printed" is the kind of fact this repo exists to record.

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

**Save Page Now reuses a capture under an hour old, so an archive may predate its fetch by up to an
hour.** Ask SPN2 to capture a URL it captured within the last hour and it does not capture it
again: it answers `success` with the OLDER capture's timestamp and says so in `message` ("The same
snapshot had been made 18 minutes ago. You can make new capture of this URL after 1 hour").
Observed on 2026-09-20 against `harvard_pdf/102372.pdf` — `duration_sec: 0.52`, `resources: []`,
nothing fetched. `archive()` treats that answer like any new capture: it fetches the `id_` copy at
the timestamp Save Page Now reported and attaches it only if that copy reproduces the pin (below).
But it means `archives[0].captured_at` can be up to an hour
EARLIER than `artifact.fetched_at` on a freshly written record, and a second pin of one document
inside that hour carries the first pin's capture. Neither is a fault; read a capture that predates
its fetch as "this is the copy Wayback already had", not as a clock problem.

**Every capture is checked against its pin before it is attached, reused or new.** The rule is
one sentence: a capture is attached only if its `id_` copy, fetched at its exact 14-digit
timestamp, reproduces the pin's drift value. For a fixed document that is the sha256 of the bytes.
For a U.S. Code prelim it is the section's last amendment, because the page carries per-request
session data and its bytes never match twice; the prelims are the one case where the match is by
date, not by bytes. The `id_` form serves the archived response as it was, not wrapped in
Wayback's toolbar. The exact timestamp matters because Wayback redirects a timestamp it doesn't
hold to the nearest capture it does, so drift values are compared only when the capture it served,
read from the final URL and `Memento-Datetime`, is the one asked for.

So `archive()` asks first. It lists the URL's captures from the CDX API, newest first, and reuses
the first that reproduces the pin. It skips any capture whose CDX digest already failed, since a
digest names a payload. A capture of that URL is not enough on its own: a URL that served a
different document last year has a capture too. If none reproduces the pin, Save Page Now is asked
for a new capture, and that capture is fetched back at the timestamp it reported:
- If it's served and reproduces the pin, it is attached.
- If it's served and doesn't, it is refused ("new capture does not reproduce the pin's
  `<drift key>`").
- If nothing is stored at that timestamp yet (a 404, or a redirect to a different capture, even
  one whose bytes would match), it is tried three times over ten minutes, then reported as
  "capture not stored at returned timestamp" with nothing attached.
- If the last try fails for another reason (a timeout, or an HTTP error other than 404), it is
  reported as "new capture could not be checked", since that says nothing about what is stored.

The listing is an optimisation, so a broken CDX API can't refuse a pin; it just means the long way
round. A 5xx or 429 from it is retried the way Save Page Now's are: on 2026-09-24 it answered 503
twice in a row and then served the listing.

Why the new capture is checked too: on 2026-09-22 Save Page Now reported captures for
`xr_src_0012` and `xr_src_0013` at timestamps the CDX API still didn't hold two days later, and
Wayback served both only by redirecting to a 2026-07-11 capture. Nothing checked them before the
records were written. A read-only check of all twenty attached captures on 2026-09-24 found those
two and no others: the other eighteen reproduce their pins at their exact timestamps.

**`pin archive --repair xr_src_NNNN`** re-verifies a pin's attached capture at its exact
timestamp. If it verifies, nothing changes. If Wayback serves another capture for that timestamp,
answers 404 for it, or the bytes don't reproduce the pin, the newest existing capture that does
replaces the entry, and only the record's `archives` field is rewritten. Nothing changes, and the
reason is reported, if no existing capture can be verified, if the attached capture can't be
reached at all, or if it isn't a Wayback capture with a timestamp (a Perma.cc or GovInfo copy is
left alone). It never asks for a new capture.

**A 5xx from Wayback is retried; a 4xx is not.** `POST /save` answered 503 with an HTML "Internet
Archive: Temporarily Offline" page at 19:22 UTC on 2026-09-20 and was serving normally by 19:23.
Before the retry, a blip that short refused an otherwise good pin — and for `uscode`, where
`--archive` is required, the pin could not be made at all until someone ran it again by hand.
`archive()` now retries a 5xx three times over about a minute. **429 is retried too, on its own
budget:** it is not a fault but a rate, so `Retry-After` is honoured when the response carries one
and 60 seconds used only when it does not, for up to three tries. Any other 4xx is Wayback saying
no — a URL it will not take, a credential it will not accept — and asking again cannot change that
answer, so it is not retried. When it does give up, the reason travels back: `add_source` prints
`archive step failed: <status, status_ext and message as SPN2 gave them, or the HTTP code>`, so a
transient outage and a permanently refused URL no longer read identically.

## The console is `add` with a page on it, not a second way to pin

`python -m registers_crosswalk.console` serves one page on `127.0.0.1:8765` over the code paths
that already exist. It is the write side of the register — it is the only thing in this repo that
creates records without a terminal — so the rules it runs under are worth stating plainly.

**One function, not two code paths.** `pin.add_source` holds everything `pin add` does from the
`duplicate_of` early exit to the write: both duplicate rules, the archive step and its failure
mode, `check_source_invariants`, and the write itself. `_cmd_add` is a thin caller that keeps only
what is genuinely about the command line — the two refusals decided from flags alone, and turning
`args` into a `PinSpec`. The console calls the same `add_source`. Every refusal the page shows is
the CLI's own string, which is why `tests/test_pin_cli.py` asserts those messages and
`tests/test_console.py` asserts the console reimplements none of them.

The one guard the console owns rather than borrows is `REQUIRES_ARCHIVE`, because `_cmd_add`
refuses that from the flags before there is a spec to hand over. The console **forces** the
archive on rather than refusing the pin: the operator's intent is unambiguous, and refusing would
only make them tick a box they were never offered a choice about. A `uscode` pin through the
console is archived whatever the checkbox says, and there is a test that sends `archive: false`
and asserts the capture happened anyway.

**Unverified fetchers can be searched, not pinned through.** `/api/resolve` answers 409 for a
fetcher whose `VERIFIED` is false, naming this file. Search is allowed for the same reason it is
harmless: it reads somebody else's index and writes nothing. It is `spec()` — the call that
decides *which* document gets pinned and at *what grade* — that a first live run has to witness,
with a human reading the output. That is what the terminal is for, and it is why the console
cannot be the place any fetcher is exercised for the first time. See
`fetcher_verified`: the convention for flipping it, above.

**Keys: it reads `.env` itself and never modifies the environment.** `load_dotenv` parses the file
into a private mapping that is passed as `env` to `fetchers.search`, to the fetcher's
`spec_from_args`, and to `archive`. `os.environ` is untouched, so nothing the console runs leaks a
key into a child process or into a later command in the same shell. `spec_from_args` takes `env`
on **every** fetcher module, whether or not its `spec()` reads a key, so a caller can hand the
adapter a private mapping without knowing which fetchers need one; `None` means `os.environ`,
which is what the CLI passes and why its behaviour is unchanged.

The mapping is bound into the archiver too. `add_source` calls `archive_fn(url)` and passes
nothing else, so an unbound `archive` would take `env=None`, read `os.environ`, find no Wayback
keys there — this module never puts them there — and fall back to the anonymous save. Every console
pin did exactly that until `Console` began defaulting `archive_fn` to `functools.partial(archive,
env=self.env)`. If you add another call that needs a key, bind it the same way and give it a test
that asserts the credential actually went out.

The page shows a key's variable NAME and whether it is set. No value reaches the page, any API
response, or the log — the request logger is silenced outright, because a request line carries the
search query and the headers carry the session token. `tests/test_console.py` puts sentinel values
in a fake `.env` and asserts they appear in no byte the server sends.

**The session token.** One `secrets.token_urlsafe(32)` per run, rendered into the page and required
on every `/api/` request. A page on another origin can still send a request — the browser will —
but it cannot read the token, so it cannot drive the console. Combined with binding the loopback
interface, that is the whole of the defence, and for a server with one user on it that is enough.
The console is never run in CI, never bound to another interface, and never put behind a tunnel.

**Pacing.** CourtListener throttles a new free account to 5 API requests a minute; one resolve
spends four. A page with buttons lets the operator click faster than typing ever did, so the
ceiling is enforced in code: `PACING = {"www.courtlistener.com": 12.0}` seconds, held by a lock
because the server is threaded. `storage.courtlistener.com` serves the pinned PDF, is not the API,
is not counted against the quota, and is not paced. The clock and sleep are injectable, so the
test asserts the spacing without spending it.

The wait is reported while it happens. A resolve is one blocking POST that can sit for forty
seconds, which looked exactly like a hang, so the page polls `/api/progress` on a second channel
and shows `call 2 of 4, waiting 12 s for CourtListener's rate limit`. The record is scratch state
for one request, keyed by an id the page mints and dropped when the resolve ends; the resolve is
identified by a thread-local, because two browser tabs must not write into one another's record.
`PACED_LABEL` and `SPEC_CALLS` in `console.py` are display only — an unknown host falls back to its
hostname and an unknown call count drops the "of N", while pacing itself is driven entirely by
`PACING`.

**`data/` is written only by a click.** Starting the console writes nothing. Resolving writes
nothing — it builds a `PinSpec` and holds it in memory so Pin can reuse it without spending four
more API calls. Only Pin writes, and what it writes is one record; the page then shows the ledger
entry, the manifest line, and the `git add` that starts the commit. The footer lists anything
uncommitted under `data/sources/`, because the failure mode of a GUI is a pin made and forgotten.

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
- **`sources-drift.yml`'s red badge keeps its meaning.** It stays the check to watch. This job is
  a second reader of the same answer, not a replacement — which is also why the section above
  still describes the only drift runbook there is.

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
