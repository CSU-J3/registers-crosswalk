"""eCFR — a CFR part as it read on a given date.

Verified live 2026-09-17:
  * `/api/versioner/v1/full/{as_of}/title-{t}.xml?part={p}` returns the part's XML as of that date.
    It answers 406 unless the request will accept gzip, which `pin.default_fetch` always sends.
  * `/api/versioner/v1/versions/title-{t}.json?part={p}` returns `content_versions[]`, each with
    `amendment_date`, `substantive` and `removed`. Cosmetic (non-substantive) entries are exactly
    why the amendment check filters on `substantive`: a typo correction is not an amendment.

The point-in-time URL is stable by construction: it keeps returning the same bytes after the part
is amended. So a matching hash proves nothing about whether the law changed, and the amendment
history has to be read separately — that is what `amended_since` is for.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date

from ..models import Grade, Source
from ..pin import FetchFn, PinSpec, default_fetch, sha256_hex

NAME = "ecfr"
HELP = "a CFR part at a point in time (eCFR versioner)"
DRIFT_KEY = "sha256"
# Field mapping exercised against the live API on 2026-09-17; every record this module mints
# carries that claim as `fetcher_verified` / `verified_at`.
VERIFIED = True
VERIFIED_AT = date(2026, 9, 17)
PUBLISHER = "Office of the Federal Register"
API = "https://www.ecfr.gov/api/versioner/v1"

_FULL_URL = re.compile(
    r"/full/(?P<as_of>\d{4}-\d{2}-\d{2})/title-(?P<title>\d+)\.xml\?part=(?P<part>[^&]+)"
)


def full_url(title: int | str, part: str, as_of: date) -> str:
    return f"{API}/full/{as_of.isoformat()}/title-{title}.xml?part={part}"


def versions_url(title: int | str, part: str) -> str:
    return f"{API}/versions/title-{title}.json?part={part}"


def spec(*, title: int | str, part: str, as_of: date) -> PinSpec:
    return PinSpec(
        fetcher=NAME,
        canonical_url=full_url(title, part, as_of),
        citation=f"{title} CFR Part {part}",
        title=f"{title} CFR Part {part}, as of {as_of.isoformat()}",
        publisher=PUBLISHER,
        point_in_time=as_of,
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def latest_amendment(title: int | str, part: str, *, fetch: FetchFn = default_fetch) -> date | None:
    """The most recent SUBSTANTIVE amendment date for a part, or None if it has none."""
    body, _ = fetch(versions_url(title, part), None)
    payload = json.loads(body)
    dates = [
        date.fromisoformat(v["amendment_date"])
        for v in payload.get("content_versions", [])
        if v.get("substantive") and not v.get("removed") and v.get("amendment_date")
    ]
    return max(dates) if dates else None


def amended_since(source: Source, *, fetch: FetchFn = default_fetch) -> date | None:
    """The latest substantive amendment if the part has been amended since this pin, else None."""
    if source.point_in_time is None:
        return None
    m = _FULL_URL.search(source.canonical_url)
    if m is None:
        return None
    latest = latest_amendment(m.group("title"), m.group("part"), fetch=fetch)
    if latest is not None and latest > source.point_in_time:
        return latest
    return None


def add_arguments(parser: argparse.ArgumentParser) -> None:
    # --title is the CFR TITLE NUMBER (11 for the FEC's regulations), not the document's name.
    parser.add_argument("--title", required=True, help="CFR title number, e.g. 11")
    parser.add_argument("--part", required=True, help="CFR part, e.g. 114")
    parser.add_argument(
        "--as-of", required=True, type=date.fromisoformat, help="point in time, YYYY-MM-DD"
    )


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    del fetch  # the URL is built from the arguments; no metadata call needed
    return spec(title=args.title, part=args.part, as_of=args.as_of)
