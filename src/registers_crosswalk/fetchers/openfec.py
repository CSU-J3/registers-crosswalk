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
from urllib.parse import urljoin

from ..models import Grade
from ..pin import FetchFn, MissingKey, PinSpec, default_fetch, sha256_hex

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


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--number", required=True, help="AO or MUR number, e.g. 2023-01")
    parser.add_argument(
        "--type", dest="doc_type", choices=tuple(_NUMBER_PARAM), default="advisory_opinions"
    )
    parser.add_argument("--category", default="Final Opinion", help="documents[].category to pin")


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    return spec(number=args.number, doc_type=args.doc_type, category=args.category, fetch=fetch)
