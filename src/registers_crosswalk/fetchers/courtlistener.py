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
from urllib.parse import urljoin

from ..models import Grade
from ..pin import FetchFn, MissingKey, PinSpec, default_fetch, sha256_hex

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
API = "https://www.courtlistener.com/api/rest/v4"
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


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cluster-id", required=True, help="CourtListener opinion cluster id")
    parser.add_argument("--citation", default=None, help="override the reporter citation")


def spec_from_args(args: argparse.Namespace, *, fetch: FetchFn = default_fetch) -> PinSpec:
    return spec(cluster_id=args.cluster_id, citation=args.citation, fetch=fetch)
