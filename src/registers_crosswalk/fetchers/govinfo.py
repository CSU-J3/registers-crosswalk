"""GovInfo — a GPO package or granule (signed U.S. Code editions, CFR annuals, bills, CREC).

Verified live 2026-09-22 against the 2024 edition of 52 U.S.C. § 30116:
  * `https://api.govinfo.gov/packages/{pkg}/summary?api_key=...` returns `title`, `dateIssued` and
    `download.pdfLink`. A granule uses the same shape under
    `/packages/{pkg}/granules/{granule}/summary`, so one code path covers both. The captures are
    `tests/fixtures/govinfo_summary_*_2026-09-22.json`.
  * `download.pdfLink` is on the API host (`api.govinfo.gov/packages/.../pdf`) and needs the key.
    The same PDF is served anonymously at `www.govinfo.gov/content/pkg/{pkg}/pdf/{granule or
    pkg}.pdf`, and the two were byte-identical for the § 30116 granule (154865 bytes) and for the
    whole USCODE-2024-title52 package (673832 bytes). So `spec()` stores and fetches the content
    URL, and the key is used for the summary call and nothing else: `check` re-fetches a govinfo
    pin with no key in the environment, and CI needs no GovInfo secret.
  * The key comes from `GOVINFO_API_KEY`, an api.data.gov key. Any live api.data.gov key works;
    see docs/operations.md for the one that was disabled account-wide.

Two things that each cost a probe to learn, for whoever looks a granule up next:
  * The granules listing pages by `offsetMark` (`?offsetMark=*&pageSize=100`, then the reply's
    `nextPage`), not by a numeric `offset`.
  * A section's number is in its `granuleId` (`...-subchapI-sec30116`), not its `title`, which is
    the bare heading ("Limitations on contributions and expenditures"). Search the ids.

Decision 1: the key NEVER enters `canonical_url`. This repo is public, so a key interpolated into
a stored URL would be a committed secret. `content_request` re-attaches it only for a URL on the
API host, which no pin minted by this module has.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import date
from urllib.parse import quote, urlsplit

from ..apikey import keyed_fetch, keyed_url
from ..models import Grade
from ..pin import FetchFn, PinSpec, default_fetch, read_key, sha256_hex

NAME = "govinfo"
HELP = "a GovInfo package or granule (GPO)"
DRIFT_KEY = "sha256"
# Exercised against the live API on 2026-09-22 (UTC) through the current code path: a scratch
# `add govinfo --package USCODE-2024-title52 --granule ...-sec30116` outside the repo, whose
# summary responses are captured at tests/fixtures/govinfo_summary_*_2026-09-22.json and which the
# tests now read. Every field spec() maps was checked against that capture, and a keyless `check`
# of the scratch record exited 0. Any further edit to spec() or its parsing resets this to False —
# see the convention in docs/operations.md — unless a test proves the requests byte-identical for
# every recorded verification input. The move onto `apikey.keyed_fetch`, and reading the key
# through `pin.read_key`, both on 2026-09-25, kept it that way: tests/test_verified_requests.py.
VERIFIED = True
VERIFIED_AT = date(2026, 9, 22)
PUBLISHER = "U.S. Government Publishing Office"
ENV_KEY = "GOVINFO_API_KEY"
API = "https://api.govinfo.gov/packages"
API_HOST = "api.govinfo.gov"
CONTENT = "https://www.govinfo.gov/content/pkg"


def summary_url(package: str, granule: str | None = None) -> str:
    """The summary endpoint, each id quoted as one path segment. No key: `keyed_fetch` adds it."""
    if granule:
        return f"{API}/{quote(package, safe='')}/granules/{quote(granule, safe='')}/summary"
    return f"{API}/{quote(package, safe='')}/summary"


def content_url(package: str, granule: str | None = None) -> str:
    """The key-free public copy of the PDF the API's `download.pdfLink` points at."""
    return f"{CONTENT}/{package}/pdf/{granule or package}.pdf"


def _key(env: Mapping[str, str]) -> str:
    return read_key(env, ENV_KEY, NAME)


def spec(
    *,
    package: str,
    granule: str | None = None,
    citation: str | None = None,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    key = _key(os.environ if env is None else env)
    body, _ = keyed_fetch(fetch, summary_url(package, granule), {}, key=key, fetcher=NAME)
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
    return keyed_url(url, {}, _key(env)), {}


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
