"""eCFR — a CFR part, subpart or section as it read on a given date.

Verified live 2026-09-17:
  * `/api/versioner/v1/full/{as_of}/title-{t}.xml?part={p}` returns the part's XML as of that date.
    It answers 406 unless the request will accept gzip, which `pin.default_fetch` always sends.
    Adding `&section=` or `&subpart=` narrows it to that provision — four distinct documents with
    four distinct hashes, so the granularity is a property of the pinned document, not a display
    choice. Pinning the part when the argument cites the section substitutes a different document.
  * `/api/versioner/v1/versions/title-{t}.json?part={p}` returns `content_versions[]`, each with
    `amendment_date`, `substantive`, `removed`, and the `subpart`/`section` it applies to. Cosmetic
    (non-substantive) entries are exactly why the amendment check filters on `substantive`: a typo
    correction is not an amendment.
  * The `full` endpoint serves dates beyond a title's `latest_issue_date`, but not beyond eCFR's
    own publication lag — "today" is routinely a 404 (verified 2026-09-17). `/titles.json` carries
    a per-title `latest_issue_date`, which is what `--as-of latest` resolves against; it differs
    sharply by title (title 5 was 2026-09-15 while title 11 was 2026-06-08 on the same day).

The point-in-time URL is stable by construction: it keeps returning the same bytes after the
provision is amended. So a matching hash proves nothing about whether the law changed, and the
amendment history has to be read separately — that is what `amended_since` is for.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date

from ..models import Grade, Source
from ..pin import FetchFn, PinSpec, default_fetch, sha256_hex

NAME = "ecfr"
HELP = "a CFR part, subpart or section at a point in time (eCFR versioner)"
DRIFT_KEY = "sha256"
# Reset to False by the convention in docs/operations.md: spec() and its URL building changed when
# --section/--subpart landed, so the earlier live run no longer covers this code path. Flips back
# on the first live `add` through the new arguments, dated the day it runs.
VERIFIED = False
VERIFIED_AT = None
PUBLISHER = "Office of the Federal Register"
API = "https://www.ecfr.gov/api/versioner/v1"
# What the operator types to mean "whatever eCFR has published most recently for this title".
# It is an INPUT ONLY: it is resolved to a concrete date before any URL is built, so neither
# canonical_url nor point_in_time can ever carry it. A record saying "latest" would be a
# record that means something different every time it is read.
LATEST = "latest"

_FULL_URL = re.compile(
    r"/full/(?P<as_of>\d{4}-\d{2}-\d{2})/title-(?P<title>\d+)\.xml\?part=(?P<part>[^&]+)"
    r"(?:&section=(?P<section>[^&]+))?(?:&subpart=(?P<subpart>[^&]+))?"
)


def _target(
    part: str | None, section: str | None, subpart: str | None
) -> tuple[str, str | None, str | None, str]:
    """Resolve the three mutually exclusive selectors to (part, section, subpart, citation tail).

    A section carries its own part ("2640.202" lives in part 2640) and a subpart is written
    "PART/LETTER" ("2634/D"), so the part is always derivable — the versioner wants `part=` present
    in every case, with the narrower selector added alongside it.
    """
    given = [name for name, v in (("part", part), ("section", section), ("subpart", subpart)) if v]
    if len(given) != 1:
        raise ValueError(f"give exactly one of part/section/subpart, got {given or 'none'}")

    if part:
        return part, None, None, f"Part {part}"
    if section:
        head = section.split(".", 1)[0]
        if head == section:
            raise ValueError(f"section expects a full number like 2640.202, got {section!r}")
        return head, section, None, section
    assert subpart is not None
    head, sep, letter = subpart.partition("/")
    if not sep or not head or not letter:
        raise ValueError(f"subpart expects PART/LETTER like 2634/D, got {subpart!r}")
    return head, None, letter, f"{head} subpart {letter}"


def full_url(
    title: int | str,
    part: str,
    as_of: date,
    *,
    section: str | None = None,
    subpart: str | None = None,
) -> str:
    url = f"{API}/full/{as_of.isoformat()}/title-{title}.xml?part={part}"
    if section:
        url += f"&section={section}"
    if subpart:
        url += f"&subpart={subpart}"
    return url


def versions_url(title: int | str, part: str) -> str:
    return f"{API}/versions/title-{title}.json?part={part}"


def titles_url() -> str:
    return f"{API}/titles.json"


def latest_issue_date(title: int | str, *, fetch: FetchFn = default_fetch) -> date:
    """The most recent issue date eCFR has published for a title.

    Per title, not global: eCFR reissues a title when it changes, so titles drift apart by months.
    Asking for today's date instead is a 404 — the service runs a publication lag of a day or two.
    """
    body, _ = fetch(titles_url(), None)
    for entry in json.loads(body).get("titles", []):
        if str(entry.get("number")) == str(title):
            issued = entry.get("latest_issue_date")
            if not issued:
                raise ValueError(f"title {title} has no latest_issue_date in the eCFR index")
            return date.fromisoformat(issued)
    raise ValueError(f"title {title} not found in the eCFR titles index")


def resolve_as_of(title: int | str, as_of: date | str, *, fetch: FetchFn = default_fetch) -> date:
    """Turn the caller's --as-of into a concrete date. Everything downstream sees only a date."""
    if as_of == LATEST:
        return latest_issue_date(title, fetch=fetch)
    if isinstance(as_of, date):
        return as_of
    return date.fromisoformat(as_of)


def spec(
    *,
    title: int | str,
    part: str | None = None,
    section: str | None = None,
    subpart: str | None = None,
    as_of: date | str,
    fetch: FetchFn = default_fetch,
) -> PinSpec:
    # Resolved first, so every line below this one deals in a concrete date.
    as_of = resolve_as_of(title, as_of, fetch=fetch)
    resolved_part, section, subpart, tail = _target(part, section, subpart)
    citation = f"{title} CFR {tail}"
    return PinSpec(
        fetcher=NAME,
        canonical_url=full_url(title, resolved_part, as_of, section=section, subpart=subpart),
        citation=citation,
        title=f"{citation}, as of {as_of.isoformat()}",
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


def latest_amendment(
    title: int | str,
    part: str,
    *,
    section: str | None = None,
    subpart: str | None = None,
    fetch: FetchFn = default_fetch,
) -> date | None:
    """The most recent SUBSTANTIVE amendment date for a provision, or None if it has none.

    Narrowed to the section or subpart when the pin is narrower than the part. Without that, an
    amendment anywhere in a 270 KB part would report a pinned 10 KB section as amended — a false
    positive, and the fastest way to teach an operator to ignore the AMENDED status.
    """
    body, _ = fetch(versions_url(title, part), None)
    payload = json.loads(body)
    dates = []
    for v in payload.get("content_versions", []):
        if not v.get("substantive") or v.get("removed") or not v.get("amendment_date"):
            continue
        if section is not None and v.get("section") != section:
            continue
        if subpart is not None and v.get("subpart") != subpart:
            continue
        dates.append(date.fromisoformat(v["amendment_date"]))
    return max(dates) if dates else None


def amended_since(source: Source, *, fetch: FetchFn = default_fetch) -> date | None:
    """The latest substantive amendment if the provision was amended since this pin, else None."""
    if source.point_in_time is None:
        return None
    m = _FULL_URL.search(source.canonical_url)
    if m is None:
        return None
    latest = latest_amendment(
        m.group("title"),
        m.group("part"),
        section=m.group("section"),
        subpart=m.group("subpart"),
        fetch=fetch,
    )
    if latest is not None and latest > source.point_in_time:
        return latest
    return None


def _as_of_arg(value: str) -> date | str:
    if value == LATEST:
        return LATEST
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f'expected YYYY-MM-DD or "{LATEST}", got {value!r}'
        ) from exc


def add_arguments(parser: argparse.ArgumentParser) -> None:
    # --title is the CFR TITLE NUMBER (5 for the OGE regulations), not the document's name.
    parser.add_argument("--title", required=True, help="CFR title number, e.g. 5")
    # Exactly one selector: each names a different document with a different hash.
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--part", help="a whole CFR part, e.g. 2640")
    target.add_argument("--section", help="a single section, e.g. 2640.202")
    target.add_argument("--subpart", metavar="PART/LETTER", help="a subpart, e.g. 2634/D")
    parser.add_argument(
        "--as-of",
        required=True,
        type=_as_of_arg,
        help='point in time as YYYY-MM-DD, or "latest" for this title\'s newest published issue',
    )


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    # `fetch` is used only when --as-of is "latest"; a concrete date needs no metadata call.
    return spec(
        title=args.title,
        part=args.part,
        section=args.section,
        subpart=args.subpart,
        as_of=args.as_of,
        fetch=fetch,
    )
