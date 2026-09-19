"""U.S. Code (OLRC prelim) — a section as currently published at uscode.house.gov.

The section page carries two dates, and they answer different questions.

"Text contains those laws in effect on {Month D, YYYY}" is the document's own claim about itself,
so it is `point_in_time` and part of the title. It is NOT the drift signal. Observed 2026-09-18,
that date is site-wide rather than per-section: 52 U.S.C. § 30116 and 1 U.S.C. § 1 both stated
"laws in effect on September 17, 2026", while the latest OLRC release point affecting title 52 was
Public Law 119-73 (2026-01-23) and the latest affecting title 1 was Public Law 119-103
(2026-09-02). It follows OLRC's publishing schedule, so keying drift to it reported a change for
sections nobody had touched — the exact failure the key was chosen to avoid.

`drift_key = "last_amended"` is the latest date in the section's SOURCE CREDIT: the parenthetical
after the text listing the enacting law and every law that has amended the section. That date moves
only when a law amends this section. Markup churn is ignored, and the hash is still recorded in the
artifact — it just isn't the signal.

Scoped to the credit element on purpose. Notes below the text cite later laws that did NOT amend
the section (effective-date notes, termination provisions); on 2026-09-18 the 1 U.S.C. § 1 page
carried 35 dates later than its credit's own latest. A date-shaped token whose month form this
parser does not know is an ERROR, not a dropped date: dropping one would lower the latest date and
hide the amendment it belongs to. Markup observed that day, on both pages:
exactly one `<p class="source-credit">` per section page, with `<a>` and `<statuteAtLarge>` nested
inside and no nested `<p>`. Pre-1957 laws are cited as chapters ("July 30, 1947, ch. 388") and carry
no Pub. L. number, so the parser keys on dates, in the Bluebook month forms the credit uses.

What this key misses: an editorial change to the text or the notes that adds no law to the source
credit. The archive copy is what holds the pinned text, which is why REQUIRES_ARCHIVE is set.

Verification status: NOT verified live. The parser was checked against two pages captured
2026-09-18 (52 U.S.C. 30116 -> 2014-12-16, 1 U.S.C. 1 -> 2012-12-28), and the 1 U.S.C. 1 capture is
the fixture the tests read. That is evidence about parsing, not about minting: no record has been
minted through this code path. The first live `add` needs `--archive` and is pending, blocked on
2026-09-18 by a Wayback 500 (see docs/operations.md).

For a citation that must be byte-stable, pin the signed annual edition through the `govinfo`
fetcher instead (`USCODE-{year}-title{t}`).
"""

from __future__ import annotations

import argparse
import html
import re
from datetime import date, datetime

from ..models import Grade
from ..pin import FetchFn, PinSpec, default_fetch

NAME = "uscode"
HELP = "a U.S. Code section (OLRC prelim, uscode.house.gov)"
DRIFT_KEY = "last_amended"
# NOT yet exercised against the live API from this tree: a capture is not an `add`. See the
# verification-status paragraph in the docstring above. Flip to True, dated the day it ran, on the
# first live `add` through this fetcher.
VERIFIED = False
VERIFIED_AT = None
# The canonical URL has no version axis: it serves whatever OLRC currently publishes. Once the
# section is amended, nothing can reproduce the pinned text except the archive copy, so a pin
# without one is refused rather than written and regretted.
REQUIRES_ARCHIVE = True
PUBLISHER = "Office of the Law Revision Counsel"
VIEW = "https://uscode.house.gov/view.xhtml"

_CURRENCY = re.compile(rb"laws in effect on\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})")
# The source credit is one <p class="source-credit"> holding the whole parenthetical. Observed
# 2026-09-18 on 52 U.S.C. 30116 and 1 U.S.C. 1: exactly one such element per section page, with
# <a> and <statuteAtLarge> nested inside it but no nested <p>, so a non-greedy match to </p> takes
# the element whole.
_SOURCE_CREDIT = re.compile(rb'<p[^>]*class="source-credit"[^>]*>(.*?)</p>', re.S)
_TAG = re.compile(rb"<[^>]+>")
# Bluebook month forms, as the credit writes them. "May", "June" and "July" are never abbreviated.
_MONTHS = {
    "Jan.": 1, "Feb.": 2, "Mar.": 3, "Apr.": 4, "May": 5, "June": 6,
    "July": 7, "Aug.": 8, "Sept.": 9, "Sep.": 9, "Oct.": 10, "Nov.": 11, "Dec.": 12,
}  # fmt: skip
# "Sept." is the Bluebook form and what the credits observed on 2026-09-18 use; "Sep." is accepted
# because it costs nothing and an unrecognised month is now an error rather than a silent skip.
# Sept. must precede Sep. only for readability — the alternation backtracks correctly either way.
_CREDIT_DATE = re.compile(
    r"\b(Jan\.|Feb\.|Mar\.|Apr\.|May|June|July|Aug\.|Sept\.|Sep\.|Oct\.|Nov\.|Dec\.)"
    r"\s+(\d{1,2}),\s+(\d{4})"
)
# Anything SHAPED like a credit date, whatever the month form. What _CREDIT_DATE parses must
# account for every one of these; a leftover is a month spelling this parser does not know, and
# silently dropping it would lower the maximum and hide an amendment.
_DATE_SHAPED = re.compile(r"\b([A-Z][a-zA-Z]{1,4}\.?)\s+(\d{1,2}),\s+(\d{4})")


def section_url(title: int | str, section: str) -> str:
    return f"{VIEW}?req=granuleid:USC-prelim-title{title}-section{section}&num=0&edition=prelim"


def currency_date(body: bytes) -> datetime:
    """The "laws in effect on" date the page states. Raises if the page doesn't state one."""
    m = _CURRENCY.search(body)
    if m is None:
        raise ValueError("no 'laws in effect on' currency date found in the page")
    text = re.sub(rb"\s+", b" ", m.group(1)).decode("utf-8")
    return datetime.strptime(text, "%B %d, %Y")


def source_credit(body: bytes) -> str:
    """The section's source credit as plain text. Raises if the page doesn't carry one."""
    m = _SOURCE_CREDIT.search(body)
    if m is None:
        raise ValueError("no source-credit element found in the page")
    text = html.unescape(_TAG.sub(b" ", m.group(1)).decode("utf-8"))
    return re.sub(r"\s+", " ", text).strip()


def last_amended(body: bytes) -> date:
    """The latest date in the section's source credit.

    Scoped to that element on purpose. Notes below the text cite later laws that did NOT amend the
    section — effective-date notes, termination provisions — and 1 U.S.C. 1 carried 35 such dates
    on 2026-09-18, every one of them later than its credit's own latest.
    """
    credit = source_credit(body)
    parsed = list(_CREDIT_DATE.finditer(credit))
    shaped = list(_DATE_SHAPED.finditer(credit))
    if len(shaped) > len(parsed):
        spans = {m.span() for m in parsed}
        token = next(m.group(0) for m in shaped if m.span() not in spans)
        raise ValueError(
            f"unreadable date in the source credit: {token!r}. Its month form is not one this "
            "parser knows, and dropping it would lower the latest date and hide an amendment."
        )
    dates = [date(int(m[3]), _MONTHS[m[1]], int(m[2])) for m in parsed]
    if not dates:
        raise ValueError(f"no dates found in the source credit: {credit[:120]!r}")
    return max(dates)


def drift_value(body: bytes) -> str:
    return last_amended(body).isoformat()


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
