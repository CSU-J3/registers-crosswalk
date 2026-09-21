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

MURs take the same shape with `type=murs`. Read live off MURs 8098 and 8111 on 2026-09-21,
before any of them was pinned:
  * the number parameter is `case_no`, NOT `mur_no`. `mur_no` is not rejected — it is ignored, and
    the response comes back 200 with the unfiltered first page of all 7,670 matters.
  * hits sit under `payload["murs"]`; `total_murs` is the count.
  * a MUR's `documents[]` repeat their categories — both of 8098's certifications are
    `Certifications` — so a document is named by `document_id`, which is what `--document` is for.
  * the document's date is `documents[].document_date`. A MUR record has `open_date` and
    `close_date` and no `issue_date` at all.
  * the record's `name` is the primary respondent ("Cory Mills"), not a captioned matter name, and
    it is the same string for both matters — which is why the citation stays `FEC MUR {n}` and
    `--citation` exists for the cases where that is not enough.

Reading an endpoint is not running one: `spec()` has still not been exercised end to end from this
tree, so `VERIFIED` stays False until it is.
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
# NOT yet exercised against the live API from this tree. The MUR shapes recorded in the docstring
# above were read off the search endpoint, which is not the same as running `spec()` and reading
# what it minted — so every record this module mints still says `fetcher_verified: false` on its
# face. Flip to True, dated the day it ran, on the first live `add` through this fetcher.
VERIFIED = False
VERIFIED_AT = None
PUBLISHER = "Federal Election Commission"
ENV_KEY = "OPENFEC_API_KEY"
SEARCH = "https://api.open.fec.gov/v1/legal/search/"
FEC_BASE = "https://www.fec.gov"

# Observed 2026-09-21 on both MURs this module was first run against. `mur_no` is NOT rejected
# by the endpoint, it is IGNORED: the response comes back 200 with the unfiltered first page of all
# 7,670 matters, and `_pick_document` would have walked it and pinned a document belonging to some
# other MUR under the citation it was asked for. That is the failure this module's `VERIFIED` flag
# existed to catch, and it is why the parameter name is now the observed one rather than the
# plausible one.
_NUMBER_PARAM = {"advisory_opinions": "ao_no", "murs": "case_no"}
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


def _pick_document(
    records: list[dict], *, category: str | None = None, document_id: str | None = None
) -> tuple[dict, dict]:
    """The one document to pin, by the API's own id or by category.

    By id because a MUR's categories are not unique: the two matters this module was first run
    against carry three categories between them and 47 documents, so "Certifications" names two
    documents in each and the first-match rule could reach only one of them. An advisory opinion
    has one `Final Opinion` and keeps the category default.
    """
    for record in records:
        for document in record.get("documents", []):
            if document_id is not None:
                if str(document.get("document_id")) == str(document_id):
                    return record, document
            elif document.get("category") == category:
                return record, document
    if document_id is not None:
        raise ValueError(f"no document with document_id {document_id!r} in the search result")
    raise ValueError(f"no document with category {category!r} in the search result")


def _as_date(value: str | None) -> date | None:
    # Date placement varies across record/document; read it defensively rather than assert a shape.
    return date.fromisoformat(value[:10]) if value else None


def spec(
    *,
    number: str,
    doc_type: str = "advisory_opinions",
    category: str | None = None,
    document_id: str | None = None,
    citation: str | None = None,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    if document_id is None and category is None:
        category = "Final Opinion"
    key = _key(os.environ if env is None else env)
    body, _ = fetch(search_url(number, doc_type, key), None)
    records = _records(json.loads(body), doc_type)
    record, document = _pick_document(records, category=category, document_id=document_id)
    return PinSpec(
        fetcher=NAME,
        canonical_url=urljoin(FEC_BASE, document["url"]),
        citation=citation or _CITATION[doc_type].format(number),
        title=document.get("description") or record.get("name") or f"{doc_type} {number}",
        publisher=PUBLISHER,
        # `document_date` is the MUR shape, observed 2026-09-21; `date` is what the advisory
        # opinion fixture carries. `issue_date` is on the RECORD, not the document, and no MUR
        # record has one — read all three rather than assert one shape.
        published_at=_as_date(
            document.get("document_date") or document.get("date") or record.get("issue_date")
        ),
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


# --------------------------------------------------------------------------- search

# NOT exercised against the live API (2026-09-21): `q=` has never been asked of the live endpoint.
# What the MUR captures do let these tests assert is `_hit`'s parse, since a search hit and a
# `spec()` record read the same MUR record — so the fields are covered even though the query is
# not.
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
    leaves `--document` for the caller to fill from the `document_id` the search showed. It names
    the id rather than the category because a MUR's categories repeat — both of 8098's
    certifications are `Certifications` — so a category does not identify one document. Advisory
    opinions have a settled default (`Final Opinion`), so theirs is complete as printed.
    """
    if not hit.identifier:
        return None
    base = f"pin add openfec --number {hit.identifier} --type {doc_type}"
    return base if doc_type == "advisory_opinions" else f"{base} --document <document_id>"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--number", required=True, help="AO or MUR number, e.g. 2023-01")
    parser.add_argument(
        "--type", dest="doc_type", choices=tuple(_NUMBER_PARAM), default="advisory_opinions"
    )
    # One document per record, named one of two ways. A MUR repeats its categories, so id is the
    # only way to reach its second certification; an advisory opinion has one `Final Opinion` and
    # keeps the category default. Mutually exclusive because naming both would mean deciding which
    # one loses, and there is no reading of "this document and also that one" worth guessing at.
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--category", help="documents[].category to pin (default: Final Opinion)")
    which.add_argument("--document", dest="document_id", help="documents[].document_id to pin")
    parser.add_argument(
        "--citation",
        help="override the citation (default: FEC Advisory Opinion {n} / FEC MUR {n})",
    )


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    return spec(
        number=args.number,
        doc_type=args.doc_type,
        category=args.category,
        document_id=args.document_id,
        citation=args.citation,
        fetch=fetch,
        env=env,
    )
