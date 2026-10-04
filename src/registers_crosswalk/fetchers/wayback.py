"""Wayback — a page as one Wayback Machine capture served it, for a page whose live bytes never
hold still.

A news story or a party statement served as HTML changes under its own URL even when its text does
not. On 2026-10-03 the DSCC's statement page carried a Cloudflare script holding the second of the
request, the Bangor Daily News stamped the request time into its asset URLs, and the three pages
compared with their July captures had changed their menus and "latest posts" lists while their
text stood still. Such a page cannot hold a `manual` pin: no two fetches hash the same, and no
fresh capture reproduces the fetch. One capture can. Its bytes are fixed once taken, and it shows
what the page said that day, which is what a citation of a news page is for.

So the pin is that capture. `canonical_url` is the publisher's URL, which is what the document is
and what a reader cites. The bytes are the capture's raw `id_` response at exactly `--timestamp`,
the point in time is the capture's UTC day, and the capture itself is the pin's archive copy.
Wayback redirects a timestamp it does not hold to the nearest one it does, so `pin()` refuses a
capture not served at exactly that timestamp, by the rule the weekly archive check applies, rather
than name one capture and hash another.

Graded as a mirror, the rule courtlistener's branches set (docs/operations.md): an institutional
archive is not the publisher, so reliability is B at best and A is refused. Credibility is the
caller's: 1 where the text is confirmed against the live page or other outlets, 2 by default.

`pin check` never fetches these pins. A capture cannot change, and whether it is still served is
the weekly archive check's question (`pin check --archives`), asked there on Wayback's pacing.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from ..models import Credibility, Grade, Reliability
from ..pin import FetchFn, PinSpec, default_fetch, sha256_hex

NAME = "wayback"
HELP = "a page as one Wayback capture served it; the capture is its archive copy (no --archive)"
DRIFT_KEY = "sha256"
# A pin's bytes are the capture's: `check` reports it FIXED without fetching, `blobs` reads it from
# the capture, and the ledger says so. See `fetchers.fixed_bytes`.
FIXED_BYTES = True
# Ships False, like every fetcher: its code path (the capture URL, the served-timestamp refusal,
# the archive copy it attaches) has not yet been run against Wayback from this tree. It flips on
# the first live `add`, run scratch-first per docs/operations.md, dated the day it ran.
VERIFIED = False
VERIFIED_AT: date | None = None
# The mirror rule: an institutional archive is not the publisher, so a capture is never A.
RELIABILITIES: tuple[Reliability, ...] = ("B", "C", "D", "E", "F")
_TIMESTAMP = re.compile(r"\d{14}")


def capture_timestamp(value: str) -> str:
    """A capture's timestamp as Wayback lists it: 14 digits that name a real instant (no 13th
    month, no 61st minute). Raises ValueError, which argparse reports as a usage error."""
    if not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"--timestamp must be 14 digits, YYYYMMDDhhmmss, not {value!r}")
    datetime.strptime(value, "%Y%m%d%H%M%S")
    return value


def spec(
    *,
    url: str,
    timestamp: str,
    citation: str,
    title: str,
    publisher: str | None = None,
    published_at: date | None = None,
    reliability: Reliability = "B",
    credibility: Credibility = 2,
) -> PinSpec:
    captured = datetime.strptime(capture_timestamp(timestamp), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    if reliability not in RELIABILITIES:
        raise ValueError(
            f"a Wayback capture is graded as a mirror: reliability B at best, not {reliability}"
        )
    # The capture's own URL here would make the record cite the archive instead of the publisher.
    if urlsplit(url).hostname == "web.archive.org":
        raise ValueError(f"--url is the publisher's URL as the capture records it, not {url!r}")
    return PinSpec(
        fetcher=NAME,
        canonical_url=url,
        citation=citation,
        title=title,
        publisher=publisher,
        point_in_time=captured.date(),
        published_at=published_at,
        grade=Grade(reliability=reliability, credibility=credibility),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
        capture_timestamp=timestamp,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url", required=True, help="the publisher's URL, as the capture records it"
    )
    parser.add_argument(
        "--timestamp",
        required=True,
        type=capture_timestamp,
        help="the capture's, YYYYMMDDhhmmss, as Wayback lists it",
    )
    parser.add_argument("--citation", required=True, help="as the document cites itself")
    parser.add_argument("--title", required=True)
    parser.add_argument("--publisher", default=None)
    parser.add_argument("--published-at", type=date.fromisoformat, default=None)
    parser.add_argument("--reliability", default="B", choices=RELIABILITIES)
    parser.add_argument("--credibility", type=int, default=2, choices=(1, 2, 3, 4, 5, 6))


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    del fetch, env  # nothing to look up: the capture is read in pin(), through its own seam
    return spec(
        url=args.url,
        timestamp=args.timestamp,
        citation=args.citation,
        title=args.title,
        publisher=args.publisher,
        published_at=args.published_at,
        reliability=args.reliability,
        credibility=args.credibility,
    )
