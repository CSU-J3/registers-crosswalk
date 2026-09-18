"""U.S. Code (OLRC prelim) — a section as currently published at uscode.house.gov.

Verified live 2026-09-17: the section page carries "Text contains those laws in effect on
{Month D, YYYY}". That date is the document's point in time and its drift key.

Why `drift_key = "currency_date"` here and nowhere else: the page is re-rendered, so its bytes
change for reasons that have nothing to do with the law (markup, navigation, ads for the next
edition). Hashing it would cry drift every week and train the operator to ignore the check. The
hash is still recorded in the artifact — it just isn't the signal.

The currency date does NOT track the section, observed 2026-09-18: 52 U.S.C. § 30116 and
1 U.S.C. § 1 both stated "laws in effect on September 17, 2026", while the latest OLRC release
point affecting title 52 was Public Law 119-73 (2026-01-23) and the latest affecting title 1 was
Public Law 119-103 (2026-09-02). The date is site-wide and follows OLRC's publishing schedule, so
this key reports drift when nothing in the pinned section changed — the thing it was chosen to
avoid. Until the key is redesigned no `uscode` pin is committed; see docs/operations.md. Nothing
below has been changed on this account, so the module still ships unverified.

For a citation that must be byte-stable, pin the signed annual edition through the `govinfo`
fetcher instead (`USCODE-{year}-title{t}`).
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime

from ..models import Grade
from ..pin import FetchFn, PinSpec, default_fetch

NAME = "uscode"
HELP = "a U.S. Code section (OLRC prelim, uscode.house.gov)"
DRIFT_KEY = "currency_date"
# NOT yet exercised against the live API from this tree. The endpoint behaviour recorded in the
# docstring above came from the spec work, not from a run here, and a secondhand claim is not a
# verification — so every record this module mints says `fetcher_verified: false` on its face.
# Flip to True, dated the day it ran, on the first live `add` through this fetcher.
VERIFIED = False
VERIFIED_AT = None
PUBLISHER = "Office of the Law Revision Counsel"
VIEW = "https://uscode.house.gov/view.xhtml"

_CURRENCY = re.compile(rb"laws in effect on\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})")


def section_url(title: int | str, section: str) -> str:
    return f"{VIEW}?req=granuleid:USC-prelim-title{title}-section{section}&num=0&edition=prelim"


def currency_date(body: bytes) -> datetime:
    """The "laws in effect on" date the page states. Raises if the page doesn't state one."""
    m = _CURRENCY.search(body)
    if m is None:
        raise ValueError("no 'laws in effect on' currency date found in the page")
    text = re.sub(rb"\s+", b" ", m.group(1)).decode("utf-8")
    return datetime.strptime(text, "%B %d, %Y")


def drift_value(body: bytes) -> str:
    return currency_date(body).date().isoformat()


def spec(*, title: int | str, section: str, fetch: FetchFn = default_fetch) -> PinSpec:
    url = section_url(title, section)
    # The currency date is only knowable from the page itself, so spec() reads it. pin() fetches
    # again for the bytes it hashes; two reads of a public HTML page is the cheap way to keep the
    # metadata call on the same injectable fetch as everything else.
    body, _ = fetch(url, None)
    as_of = currency_date(body).date()
    citation = f"{title} U.S.C. § {section}"
    return PinSpec(
        fetcher=NAME,
        canonical_url=url,
        citation=citation,
        title=f"{citation} (prelim, laws in effect on {as_of.isoformat()})",
        publisher=PUBLISHER,
        point_in_time=as_of,
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=drift_value,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", required=True, help="U.S.C. title number, e.g. 52")
    parser.add_argument("--section", required=True, help="section number, e.g. 30116")


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    return spec(title=args.title, section=args.section, fetch=fetch)
