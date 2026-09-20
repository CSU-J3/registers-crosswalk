"""OpenFEC legal search — advisory opinions and MURs.

Verified live 2026-09-17 for advisory opinions:
  * `https://api.open.fec.gov/v1/legal/search/?type=advisory_opinions&ao_no={n}&api_key=...`
    returns records carrying `documents[]`, each with a `category` and a RELATIVE `url`.
  * The document to pin is the one with `category == "Final Opinion"`; the other categories are
    drafts and requests, which are different documents with different hashes.
  * The relative `url` joins against `https://www.fec.gov`, which serves the PDF without a key —
    so unlike govinfo, the stored canonical_url needs no key re-attached to re-fetch it.
  * The key comes from `OPENFEC_API_KEY` (the same api.data.gov key as govinfo) and, per
    decision 1, is used for the SEARCH call only. It never enters canonical_url.

MURs take the same shape with `type=murs`; the number parameter name for MURs is NOT verified
(2026-09-17 — no MUR was pinned), so treat `--type murs` as provisional.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import date
from urllib.parse import urlencode, urljoin

from ..models import Grade
from ..pin import FetchFn, MissingKey, PinSpec, SearchHit, default_fetch, sha256_hex

NAME = "openfec"
HELP = "an FEC advisory opinion or MUR document (OpenFEC legal search)"
DRIFT_KEY = "sha256"
# NOT yet exercised against the live API from this tree. The endpoint behaviour recorded in the
# docstring above came from the spec work, not from a run here, and a secondhand claim is not a
# verification — so every record this module mints says `fetcher_verified: false` on its face.
# Flip to True, dated the day it ran, on the first live `add` through this fetcher.
VERIFIED = False
VERIFIED_AT = None
PUBLISHER = "Federal Election Commission"
ENV_KEY = "OPENFEC_API_KEY"
SEARCH = "https://api.open.fec.gov/v1/legal/search/"
FEC_BASE = "https://www.fec.gov"

_NUMBER_PARAM = {"advisory_opinions": "ao_no", "murs": "mur_no"}
_CITATION = {"advisory_opinions": "FEC Advisory Opinion {}", "murs": "FEC MUR {}"}


def _key(env: Mapping[str, str]) -> str:
    key = env.get(ENV_KEY)
    if not key:
        raise MissingKey(ENV_KEY, NAME)
    return key


def search_url(number: str, doc_type: str, key: str) -> str:
    """The metadata query. Carries the key, so it is requested and then forgotten."""
    return f"{SEARCH}?type={doc_type}&{_NUMBER_PARAM[doc_type]}={number}&api_key={key}"


def _records(payload: dict, doc_type: str) -> list[dict]:
    # The response keys its hits by type; `results` is accepted as a fallback shape.
    hits = payload.get(doc_type) or payload.get("results") or []
    return [h for h in hits if isinstance(h, dict)]


def _pick_document(records: list[dict], category: str) -> tuple[dict, dict]:
    for record in records:
        for document in record.get("documents", []):
            if document.get("category") == category:
                return record, document
    raise ValueError(f"no document with category {category!r} in the search result")


def _as_date(value: str | None) -> date | None:
    # Date placement varies across record/document; read it defensively rather than assert a shape.
    return date.fromisoformat(value[:10]) if value else None


def spec(
    *,
    number: str,
    doc_type: str = "advisory_opinions",
    category: str = "Final Opinion",
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    key = _key(os.environ if env is None else env)
    body, _ = fetch(search_url(number, doc_type, key), None)
    record, document = _pick_document(_records(json.loads(body), doc_type), category)
    return PinSpec(
        fetcher=NAME,
        canonical_url=urljoin(FEC_BASE, document["url"]),
        citation=_CITATION[doc_type].format(number),
        title=document.get("description") or record.get("name") or f"{doc_type} {number}",
        publisher=PUBLISHER,
        published_at=_as_date(document.get("date") or record.get("issue_date")),
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


# --------------------------------------------------------------------------- search

# NOT exercised against the live API (2026-09-20): the ledger this repo pins for cites no MUR and
# no advisory opinion, so there was no document to run a first search against and no capture to
# test the parse on. Same standing as `spec` above, and the same `VERIFIED = False` covers both —
# a record minted through this module still says on its face that nobody has run it. What IS
# tested offline is the one thing a capture is not needed for: that the query URL is built
# correctly and that the key never leaves it for a hit.
SEARCH_TYPES = ("advisory_opinions", "murs")


def free_text_search_url(query: str, doc_type: str, key: str) -> str:
    """The free-text query. Carries the key, like `search_url`, and is likewise never stored."""
    return f"{SEARCH}?{urlencode({'q': query, 'type': doc_type, 'api_key': key})}"


def _hit(record: dict, doc_type: str) -> SearchHit:
    # Defensive throughout, for the same reason as courtlistener's: this is somebody else's index.
    number = record.get("no") or record.get("ao_no") or record.get("mur_no") or ""
    # The hit's own public page on fec.gov, built from the record's relative url when it has one.
    # NEVER the search URL: that one carries the api_key.
    relative = record.get("url")
    return SearchHit(
        identifier=str(number),
        label=record.get("name") or record.get("description") or "",
        court_or_office=PUBLISHER,
        date=record.get("issue_date") or record.get("date") or None,
        docket_or_number=str(number) or None,
        citation=_CITATION[doc_type].format(number) if number else None,
        url=urljoin(FEC_BASE, relative) if relative else None,
    )


def search(
    query: str,
    *,
    doc_type: str = "advisory_opinions",
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> list[SearchHit]:
    """Respondent or matter name in, AO/MUR numbers out. Never writes, never pins."""
    key = _key(os.environ if env is None else env)
    body, _ = fetch(free_text_search_url(query, doc_type, key), None)
    return [_hit(r, doc_type) for r in _records(json.loads(body), doc_type)]


def add_command(hit: SearchHit, doc_type: str = "advisory_opinions") -> str | None:
    """The `pin add` that would pin this hit.

    A MUR carries several documents and `add` needs to be told which, so the command it prints
    leaves `--category` for the caller to fill from what the search showed. Advisory opinions have
    a settled default (`Final Opinion`), so theirs is complete as printed.
    """
    if not hit.identifier:
        return None
    base = f"pin add openfec --number {hit.identifier} --type {doc_type}"
    return base if doc_type == "advisory_opinions" else f'{base} --category "<category>"'


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--number", required=True, help="AO or MUR number, e.g. 2023-01")
    parser.add_argument(
        "--type", dest="doc_type", choices=tuple(_NUMBER_PARAM), default="advisory_opinions"
    )
    parser.add_argument("--category", default="Final Opinion", help="documents[].category to pin")


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    return spec(
        number=args.number, doc_type=args.doc_type, category=args.category, fetch=fetch, env=env
    )
