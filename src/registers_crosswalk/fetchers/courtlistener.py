"""CourtListener — a court opinion, by opinion-cluster id.

Verified live 2026-09-20 on cluster 1481640, Dunne v. United States, 138 F.2d 137 (8th Cir. 1943),
whose four captured responses the tests read. What the run established:

  * `{API}/clusters/{id}/` with `Authorization: Token {t}` returns the cluster. `date_filed` does
    sit on it ("1943-09-20"), confirming the first of the two 2026-09-17 assumptions. It is still
    read defensively: absent gives `published_at=None` rather than raising.
  * `sub_opinions[]` holds URLs, not objects, so the opinion costs a second call.
  * `citations[]` carries volume/reporter/page, and `citations[0]` gave "138 F.2d 137" — the
    ledger's own citation, unedited.

Two things the run corrected.

**A cluster carries no `court` key at all.** The deciding court is reachable only as
`cluster.docket`, a URL to the docket, whose own `court` is another URL. So the publisher costs
two further calls, ending at the court's `full_name` ("Court of Appeals for the Eighth Circuit").
Before this, every record would have been published by "CourtListener (Free Law Project)" — the
archive that served the file, not the court that decided the case. `PUBLISHER` survives only as
the fallback for a broken chain.

**An opinion can have no file at all.** Dunne's carries `download_url: null` AND
`local_path: null`; its text lives in the database (`html_lawbox`, `xml_harvard`) and text is not
a document. The document was on the CLUSTER, under `filepath_pdf_harvard` — the Harvard Caselaw
Access Project's page scan of the reporter volume. So the second 2026-09-17 assumption, that
`local_path` resolves under `storage.courtlistener.com/`, is STILL UNTESTED: nothing has yet been
pinned through that branch. It is left as written.

This is the normal case, not an edge: across the twenty hits of the Dunne search, every pre-1980
opinion had both file fields null, and only 1982-and-later ones carried a `download_url`. A 1943
opinion has no court PDF because in 1943 there was no such thing. See `_document` for the three
branches and their grades, and "Pre-1980 opinions" in docs/operations.md.

Four API calls per pin: cluster, opinion, docket, court. The account ceiling observed in this run
was 5 requests a minute.
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
# Exercised live 2026-09-20: a scratch `add --cluster-id 1481640 --archive` outside the repo ran
# the current code path end to end, and every field of the record it minted was compared against
# the four captured responses. Editing `spec()` voids this and resets it to False — the convention
# in docs/operations.md.
VERIFIED = True
VERIFIED_AT = date(2026, 9, 20)
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


def _document(cluster: dict, opinion: dict, cluster_id: int | str) -> tuple[str, Grade]:
    """Where the document lives, and what its provenance is worth.

    Three sources, in descending order of proximity to the publisher of record:

    * `opinion.download_url` — the court's own file. **A1**: the publisher of record itself.
    * `opinion.local_path` — CourtListener's mirror of a file it fetched from the court. **B2**:
      B because the Free Law Project is not the publisher, 2 because what it holds is a copy of
      whatever the court served on the day it was fetched, and nothing in the record lets a reader
      confirm that against the authority.
    * `cluster.filepath_pdf_harvard` — the Harvard Caselaw Access Project scan, served by the
      Free Law Project. **B1**: B for the same reason as `local_path`, an institutional archive
      rather than the publisher — but 1 rather than 2, because the document is a page image of the
      very reporter the citation names. "138 F.2d 137" resolves to a photograph of page 137 of
      volume 138 of the Federal Reporter, which a reader can check against any other copy of that
      volume. That is what separates the two B grades: a re-served file versus an image of the
      cited text.

    Pre-1980 opinions reach the third branch as a rule, not an exception — there is no court PDF
    because in 1943 there was no such thing (observed 2026-09-20, see docs/operations.md).
    """
    if opinion.get("download_url"):
        return opinion["download_url"], Grade(reliability="A", credibility=1)
    if opinion.get("local_path"):
        return urljoin(STORAGE, opinion["local_path"]), Grade(reliability="B", credibility=2)
    if cluster.get("filepath_pdf_harvard"):
        return (
            urljoin(STORAGE, cluster["filepath_pdf_harvard"]),
            Grade(reliability="B", credibility=1),
        )
    raise ValueError(
        f"cluster {cluster_id} has no document: the opinion carries neither download_url nor "
        "local_path, and the cluster has no filepath_pdf_harvard"
    )


def _court_name(cluster: dict, *, fetch: FetchFn, headers: Mapping[str, str]) -> str | None:
    """The deciding court's full name, or None when the chain to it is broken.

    A v4 cluster has no `court` key at all (observed 2026-09-20); the court is reachable only
    through `cluster.docket`, a URL to the docket, whose own `court` is a URL. Two more API
    calls, and they earn their place: without them every record this fetcher mints is published by
    "CourtListener (Free Law Project)", which is the archive that served the file, not the court
    that decided the case — a publisher field that is quietly wrong on every row.

    Returns None rather than guessing if either link is missing, so the caller can fall back
    visibly instead of inventing a court.
    """
    docket_url = cluster.get("docket")
    if not isinstance(docket_url, str) or not docket_url:
        return None
    docket = json.loads(fetch(docket_url, headers)[0])
    court = docket.get("court")
    # Tolerate either shape: a URL to fetch, or the court already inlined.
    if isinstance(court, str) and court:
        court = json.loads(fetch(court, headers)[0])
    if not isinstance(court, dict):
        return None
    return court.get("full_name") or None


def spec(
    *,
    cluster_id: int | str,
    citation: str | None = None,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    """Four API calls: cluster, opinion, docket, court.

    The document is settled before the last two are spent, so a cluster with nothing pinnable
    costs two calls rather than four.
    """
    headers = auth_headers(os.environ if env is None else env)
    body, _ = fetch(cluster_url(cluster_id), headers)
    cluster = json.loads(body)

    sub_opinions = cluster.get("sub_opinions", [])
    if not sub_opinions:
        raise ValueError(f"cluster {cluster_id} has no sub_opinions")
    body, _ = fetch(sub_opinions[0], headers)
    opinion = json.loads(body)

    url, grade = _document(cluster, opinion, cluster_id)

    filed = cluster.get("date_filed")
    case_name = cluster.get("case_name") or f"cluster {cluster_id}"
    return PinSpec(
        fetcher=NAME,
        canonical_url=url,
        citation=citation or _cite(cluster) or case_name,
        title=case_name,
        publisher=_court_name(cluster, fetch=fetch, headers=headers) or PUBLISHER,
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


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    return spec(cluster_id=args.cluster_id, citation=args.citation, fetch=fetch, env=env)
