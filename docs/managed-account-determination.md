# Managed-account determination (canonical)

Cited by Connected-Procurement, Sovereign-Connections, and Vested-Interests. The accounts are the
**same** across the three registers, so the rule that classifies them lives once, here, above them —
not in three heads where they could drift.

## The rule

An equity interest held through an account is classified by whether the account severs the official's
knowledge and control:

- **`blind_trust`** — a Qualified Blind Trust (QBT) under 5 CFR 2634 subpart D. **Excluded** on
  severed knowledge: the official cannot know the holdings, so cannot act on them.
- **`managed_direct`** — any non-blind arrangement: discretionary management, automated/algorithmic
  management, or a model portfolio. **Counts.** Delegated management is not severed knowledge; the
  interest remains attributable to the official.

## Determination source (load-bearing)

Set the mode from **how the OGE Form 278e labels the account** — not from:

- the holder's or the defense's characterization, or
- press coverage. An outlet calling an account "blind" is editorializing, not a subpart D
  certification.

A QBT must be certified as such under subpart D. Absent that certification on the filing, a
discretionary or managed account is `managed_direct`.

## Worked example — Palantir (VI `vi_holding_0002`; crosswalk `xr_org_0001`)

Trump's Palantir position sits in a managed/automated account labeled that way on the 278e, not a
subpart D QBT. One outlet described it as "blind"; that characterization is not a QBT certification.
Mode: **`managed_direct` → counts** (VI `vi_conflict_0002` is counted).

## How registers cite this

By stable path plus section anchor, e.g.
`registers-crosswalk/docs/managed-account-determination.md#the-rule`. Do not restate the rule inside a
register — cite it, so there is one source of truth.

## Change control

Determinations here are dated. A new filing that re-labels an account, or a newly produced QBT
certification, supersedes the prior determination; the losing determination stays in history. Getting
this wrong propagates to all three registers, so changes are made here and only here.
