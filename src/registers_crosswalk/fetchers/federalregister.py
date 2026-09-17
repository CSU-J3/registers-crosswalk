"""Federal Register — a published notice, rule or proposed rule, by FR document number.

Verified live 2026-09-17:
  * `/api/v1/documents/{docnum}.json` returns `pdf_url` (a govinfo.gov URL), `publication_date`,
    `citation` ("60 FR 7862") and `title`. No API key is needed for the document endpoint.
  * Coverage reaches back to 1995: FR Doc. 95-3162 resolves to 60 FR 7862.
  * The `pdf_url` and the constructed govinfo fallback
    `https://www.govinfo.gov/content/pkg/FR-{date}/pdf/{docnum}.pdf` return byte-identical PDFs,
    so falling back does not change the hash.

A published FR document is never amended in place: it has a `published_at` and no point_in_time.
A later document that changes the rule is its own citation and its own pin.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from ..models import Grade
from ..pin import FetchFn, PinSpec, default_fetch, sha256_hex

NAME = "federalregister"
HELP = "a Federal Register document by FR document number"
DRIFT_KEY = "sha256"
# NOT yet exercised against the live API from this tree. The endpoint behaviour recorded in the
# docstring above came from the spec work, not from a run here, and a secondhand claim is not a
# verification — so every record this module mints says `fetcher_verified: false` on its face.
# Flip to True, dated the day it ran, on the first live `add` through this fetcher.
VERIFIED = False
VERIFIED_AT = None
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


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    return spec(document_number=args.document_number, fetch=fetch)
