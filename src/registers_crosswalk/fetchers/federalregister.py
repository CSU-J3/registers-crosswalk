"""Federal Register — a published notice, rule or proposed rule, by FR document number.

Verified live 2026-09-18 against FR Doc. 95-3162:
  * `/api/v1/documents/{docnum}.json` returns `pdf_url` (a govinfo.gov URL), `publication_date`,
    `citation` ("60 FR 7862") and `title`, among 48 top-level keys. No API key is needed for the
    document endpoint.
  * Coverage reaches back to 1995: FR Doc. 95-3162 resolves to 60 FR 7862.
  * The fallback was NOT exercised. For this document the API's own `pdf_url` is character-for-
    character the URL `fallback_pdf_url()` builds, so the two agree trivially and the live run says
    nothing about a document whose `pdf_url` differs. The construction is covered by a test that
    nulls `pdf_url` in a copy of the captured response; that it reproduces a real govinfo URL is
    an observation about this document only.

A published FR document is never amended in place: it has a `published_at` and no point_in_time.
A later document that changes the rule is its own citation and its own pin.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import date

from ..models import Grade
from ..pin import FetchFn, PinSpec, default_fetch, sha256_hex

NAME = "federalregister"
HELP = "a Federal Register document by FR document number"
DRIFT_KEY = "sha256"
# Exercised against the live API on 2026-09-18 (UTC) through the current code path: a scratch
# `add federalregister --document-number 95-3162` outside the repo, whose response is captured at
# tests/fixtures/federalregister_document_95-3162_2026-09-18.json and which the tests now read.
# Every field spec() maps was checked against that capture. The record it minted and the committed
# xr_src_0004 hash the same 193264-byte PDF. Any further edit to spec() or its parsing resets this
# to False — see the convention in docs/operations.md.
VERIFIED = True
VERIFIED_AT = date(2026, 9, 18)
PUBLISHER = "Office of the Federal Register"
API = "https://www.federalregister.gov/api/v1/documents/{}.json"


def metadata_url(document_number: str) -> str:
    return API.format(document_number)


def fallback_pdf_url(document_number: str, publication_date: date) -> str:
    return (
        f"https://www.govinfo.gov/content/pkg/FR-{publication_date.isoformat()}"
        f"/pdf/{document_number}.pdf"
    )


def spec(*, document_number: str, fetch: FetchFn = default_fetch) -> PinSpec:
    body, _ = fetch(metadata_url(document_number), None)
    payload = json.loads(body)
    published_at = date.fromisoformat(payload["publication_date"])
    return PinSpec(
        fetcher=NAME,
        canonical_url=payload.get("pdf_url") or fallback_pdf_url(document_number, published_at),
        citation=payload["citation"],
        title=payload["title"],
        publisher=PUBLISHER,
        published_at=published_at,
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--document-number", required=True, help="FR document number, e.g. 95-3162")


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    return spec(document_number=args.document_number, fetch=fetch)
