"""CourtListener — a court opinion, by opinion-cluster id.

UNVERIFIED (decision 9, 2026-09-17): no COURTLISTENER_TOKEN was available when this was written,
so nothing below was exercised against the live API. Two things in particular are assumptions:
  * that `date_filed` sits on the cluster (read defensively here — absent gives published_at=None
    rather than raising);
  * that `local_path` resolves under `https://storage.courtlistener.com/`.
Confirm both on the first real pin and delete this notice.

What is intended:
  * `https://www.courtlistener.com/api/rest/v4/clusters/{id}/` with `Authorization: Token {t}`.
  * `sub_opinions[]` holds URLs, not objects, so the opinion itself costs a second call each.
  * Prefer the court's own `download_url` (A1 — the publisher of record) over CourtListener's
    `local_path` mirror (B2 — a faithful copy, but a copy).
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

NAME = "courtlistener"
HELP = "a court opinion by CourtListener cluster id"
DRIFT_KEY = "sha256"
# Decision 9: NOT exercised against the live API — no token was available. Every record this
# module mints says so on its face (`fetcher_verified: false`), so an unverified parse can never
# pass for a checked one. Flip both constants on the first confirmed pin and delete the notice
# above.
VERIFIED = False
VERIFIED_AT = None
PUBLISHER = "CourtListener (Free Law Project)"
ENV_KEY = "COURTLISTENER_TOKEN"
SITE = "https://www.courtlistener.com"
API = f"{SITE}/api/rest/v4"
STORAGE = "https://storage.courtlistener.com/"


def cluster_url(cluster_id: int | str) -> str:
    return f"{API}/clusters/{cluster_id}/"


def _token(env: Mapping[str, str]) -> str:
    token = env.get(ENV_KEY)
    if not token:
        raise MissingKey(ENV_KEY, NAME)
    return token


def auth_headers(env: Mapping[str, str]) -> dict[str, str]:
    return {"Authorization": f"Token {_token(env)}"}


def _cite(cluster: dict) -> str | None:
    for c in cluster.get("citations", []):
        volume, reporter, page = c.get("volume"), c.get("reporter"), c.get("page")
        if volume and reporter and page:
            return f"{volume} {reporter} {page}"
    return None


def spec(
    *,
    cluster_id: int | str,
    citation: str | None = None,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    headers = auth_headers(os.environ if env is None else env)
    body, _ = fetch(cluster_url(cluster_id), headers)
    cluster = json.loads(body)

    sub_opinions = cluster.get("sub_opinions", [])
    if not sub_opinions:
        raise ValueError(f"cluster {cluster_id} has no sub_opinions")
    body, _ = fetch(sub_opinions[0], headers)
    opinion = json.loads(body)

    download_url = opinion.get("download_url")
    if download_url:
        # The court's own PDF: the publisher of record.
        url, grade = download_url, Grade(reliability="A", credibility=1)
    elif opinion.get("local_path"):
        url, grade = urljoin(STORAGE, opinion["local_path"]), Grade(reliability="B", credibility=2)
    else:
        raise ValueError(
            f"opinion for cluster {cluster_id} has neither download_url nor local_path"
        )

    filed = cluster.get("date_filed")
    case_name = cluster.get("case_name") or f"cluster {cluster_id}"
    return PinSpec(
        fetcher=NAME,
        canonical_url=url,
        citation=citation or _cite(cluster) or case_name,
        title=case_name,
        publisher=cluster.get("court") or PUBLISHER,
        published_at=date.fromisoformat(filed[:10]) if filed else None,
        grade=grade,
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def content_request(url: str, *, env: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """What to request to re-fetch a stored `canonical_url`, and what to send with it.

    **`check` needs no token.** A pinned document never lives under the API — what gets stored is
    a court's own PDF or a file on `storage.courtlistener.com`, and both are public. So re-fetching
    one asks for no credential, and a courtlistener pin stays checkable from a CI runner with no
    secret set — the same standing as an eCFR or Federal Register pin.

    That was already true by accident, because a fetcher with no `content_request` falls through
    to `(url, {})`. Accidental is not a guarantee: nothing said it, nothing tested it, and the
    first stored URL that happened to sit under the API would have started returning 401 in CI
    with no explanation. This states the rule and the tests hold it.

    The token is attached only to an API URL, where it is required. Nothing stores one today; the
    branch is there so that if anything ever does, it is authenticated rather than quietly 401.
    """
    if url.startswith(f"{API}/"):
        return url, auth_headers(env)
    return url, {}


# --------------------------------------------------------------------------- search

# What `pin search courtlistener <query> --type X` accepts, and the single letter v4's search
# endpoint wants for it. Opinions are the default because an opinion is the thing this fetcher
# can actually pin; a docket is a lookup aid — it tells you the case exists and what it is called,
# and its id is not a cluster id.
SEARCH_TYPES = ("opinions", "dockets")
_SEARCH_TYPE_PARAM = {"opinions": "o", "dockets": "d"}


def search_url(query: str, doc_type: str = "opinions") -> str:
    return f"{API}/search/?{urlencode({'q': query, 'type': _SEARCH_TYPE_PARAM[doc_type]})}"


def _first_citation(result: dict) -> str | None:
    """v4 search returns `citation` as a list of reporter strings; tolerate a bare string too."""
    cite = result.get("citation")
    if isinstance(cite, list):
        return next((c for c in cite if c), None)
    return cite or None


def _hit(result: dict, doc_type: str) -> SearchHit:
    # Read every field defensively. A search result is somebody else's index, and a shape change
    # there should degrade a column to blank rather than blow up the command.
    if doc_type == "dockets":
        identifier = result.get("docket_id")
    else:
        identifier = result.get("cluster_id") or result.get("id")
    # Opinion results carry `absolute_url`; docket results call the same thing
    # `docket_absolute_url` (observed 2026-09-20). One key per result type, so read both.
    absolute = result.get("absolute_url") or result.get("docket_absolute_url")
    return SearchHit(
        identifier="" if identifier is None else str(identifier),
        label=result.get("caseName") or result.get("case_name") or "",
        court_or_office=result.get("court") or result.get("court_id"),
        date=(result.get("dateFiled") or result.get("date_filed") or None),
        docket_or_number=result.get("docketNumber") or result.get("docket_number"),
        citation=_first_citation(result),
        url=urljoin(SITE, absolute) if absolute else None,
    )


def search(
    query: str,
    *,
    doc_type: str = "opinions",
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> list[SearchHit]:
    """Case name or docket number in, identifiers out. Never writes, never pins."""
    headers = auth_headers(os.environ if env is None else env)
    body, _ = fetch(search_url(query, doc_type), headers)
    payload = json.loads(body)
    results = payload.get("results") or []
    return [_hit(r, doc_type) for r in results if isinstance(r, dict)]


def add_command(hit: SearchHit, doc_type: str = "opinions") -> str | None:
    """The `pin add` that would pin this hit, or None when the hit is not directly pinnable.

    A docket hit carries a docket id, and `add` takes a CLUSTER id — the two are different keys
    into CourtListener, so offering an `add` built from one would hand back a command that pins
    the wrong thing or nothing. Searching again with the case name and no `--type` is the route.
    """
    if doc_type == "dockets" or not hit.identifier:
        return None
    return f"pin add courtlistener --cluster-id {hit.identifier} --archive"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cluster-id", required=True, help="CourtListener opinion cluster id")
    parser.add_argument("--citation", default=None, help="override the reporter citation")


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    return spec(cluster_id=args.cluster_id, citation=args.citation, fetch=fetch)
