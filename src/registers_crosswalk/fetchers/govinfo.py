"""GovInfo — a GPO package or granule (signed U.S. Code editions, CFR annuals, bills, CREC).

Verified live 2026-09-17:
  * `https://api.govinfo.gov/packages/{pkg}/summary?api_key=...` returns `download.pdfLink`,
    `dateIssued` and `title`. Granules use the same shape under
    `/packages/{pkg}/granules/{granule}/summary`, so one code path covers both.
  * The key comes from `GOVINFO_API_KEY` (an api.data.gov key; the same key works for OpenFEC).

Decision 1: the key NEVER enters `canonical_url`. This repo is public, so a key interpolated into
a stored URL would be a committed secret. We store the key-free content URL and re-attach the key
from the environment at fetch time, here and in `content_request` when drift is checked.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import date
from urllib.parse import urlsplit

from ..models import Grade
from ..pin import FetchFn, MissingKey, PinSpec, default_fetch, sha256_hex

NAME = "govinfo"
HELP = "a GovInfo package or granule (GPO)"
DRIFT_KEY = "sha256"
# NOT yet exercised against the live API from this tree. The endpoint behaviour recorded in the
# docstring above came from the spec work, not from a run here, and a secondhand claim is not a
# verification — so every record this module mints says `fetcher_verified: false` on its face.
# Flip to True, dated the day it ran, on the first live `add` through this fetcher.
VERIFIED = False
VERIFIED_AT = None
PUBLISHER = "U.S. Government Publishing Office"
ENV_KEY = "GOVINFO_API_KEY"
API = "https://api.govinfo.gov/packages"
API_HOST = "api.govinfo.gov"
CONTENT = "https://www.govinfo.gov/content/pkg"


def summary_url(package: str, granule: str | None = None) -> str:
    if granule:
        return f"{API}/{package}/granules/{granule}/summary"
    return f"{API}/{package}/summary"


def content_url(package: str, granule: str | None = None) -> str:
    """The key-free public copy of the PDF the API's `download.pdfLink` points at."""
    return f"{CONTENT}/{package}/pdf/{granule or package}.pdf"


def _key(env: Mapping[str, str]) -> str:
    key = env.get(ENV_KEY)
    if not key:
        raise MissingKey(ENV_KEY, NAME)
    return key


def with_key(url: str, key: str) -> str:
    """Re-attach the API key for an actual request. Never store the result."""
    return f"{url}{'&' if '?' in url else '?'}api_key={key}"


def spec(
    *,
    package: str,
    granule: str | None = None,
    citation: str | None = None,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    key = _key(os.environ if env is None else env)
    body, _ = fetch(with_key(summary_url(package, granule), key), None)
    payload = json.loads(body)
    # `pdfLink` is read only to insist the API says a PDF exists. What is stored and fetched is
    # the content-host copy, which needs no key, so `check` needs no key either.
    if not (payload.get("download") or {}).get("pdfLink"):
        raise ValueError(f"govinfo summary for {granule or package} lists no PDF")
    issued = payload.get("dateIssued")
    return PinSpec(
        fetcher=NAME,
        canonical_url=content_url(package, granule),
        citation=citation or granule or package,
        title=payload["title"],
        publisher=PUBLISHER,
        published_at=date.fromisoformat(issued[:10]) if issued else None,
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def content_request(url: str, *, env: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """The key rides only to the API host. The content host serves the same bytes anonymously,
    so a pin stored there re-fetches with no key in the environment, as courtlistener's does."""
    if urlsplit(url).netloc != API_HOST:
        return url, {}
    return with_key(url, _key(env)), {}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", required=True, help="e.g. USCODE-2023-title52")
    parser.add_argument("--granule", default=None, help="e.g. USCODE-2023-title52-subtitleIII")
    parser.add_argument(
        "--citation", default=None, help="how the document cites itself (default: the id)"
    )


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    return spec(
        package=args.package, granule=args.granule, citation=args.citation, fetch=fetch, env=env
    )
