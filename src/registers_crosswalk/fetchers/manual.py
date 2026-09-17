"""Manual — a document with no API behind it: an fec.gov statement, a case page, an NCSL table.

The caller supplies everything, including the grade, because nothing here can infer it: a press
statement on an agency's own site is not the same kind of evidence as a third-party table, and only
the person pinning it knows which they have.

HTML pinned this way is NOISY. Its drift key is the raw hash, so a nav change, a cookie banner or a
rotating build id reads as drift. That is the honest answer for a page with no version axis — but
if the document also exists as a PDF, or in eCFR/GovInfo/the Federal Register, pin it there instead
and get a stable hash.
"""

from __future__ import annotations

import argparse
from datetime import date

from ..models import Credibility, Grade, Reliability
from ..pin import FetchFn, PinSpec, default_fetch, sha256_hex

NAME = "manual"
HELP = "any other document; the caller supplies the metadata and the grade"
DRIFT_KEY = "sha256"
# True by a different route than the API fetchers: there is no field mapping here to get wrong.
# A human typed every field, so no silent parse error can be hiding in the record, which is
# exactly what `fetcher_verified` is asking about. Marking it False would file every hand-entered
# record under "unverified fetcher" and bury the one case that matters (courtlistener). Note this
# says nothing about whether the human typed the RIGHT thing — no flag can carry that.
VERIFIED = True
VERIFIED_AT = date(2026, 9, 17)


def spec(
    *,
    url: str,
    citation: str,
    title: str,
    publisher: str | None = None,
    point_in_time: date | None = None,
    published_at: date | None = None,
    reliability: Reliability = "B",
    credibility: Credibility = 2,
) -> PinSpec:
    return PinSpec(
        fetcher=NAME,
        canonical_url=url,
        citation=citation,
        title=title,
        publisher=publisher,
        point_in_time=point_in_time,
        published_at=published_at,
        grade=Grade(reliability=reliability, credibility=credibility),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", required=True)
    parser.add_argument("--citation", required=True, help="as the document cites itself")
    parser.add_argument("--title", required=True)
    parser.add_argument("--publisher", default=None)
    parser.add_argument("--point-in-time", type=date.fromisoformat, default=None)
    parser.add_argument("--published-at", type=date.fromisoformat, default=None)
    parser.add_argument("--reliability", default="B", choices=tuple("ABCDEF"))
    parser.add_argument("--credibility", type=int, default=2, choices=(1, 2, 3, 4, 5, 6))


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    del fetch  # nothing to look up; the caller already knows everything
    return spec(
        url=args.url,
        citation=args.citation,
        title=args.title,
        publisher=args.publisher,
        point_in_time=args.point_in_time,
        published_at=args.published_at,
        reliability=args.reliability,
        credibility=args.credibility,
    )
