"""One module per document source, each one knowing how to build a `PinSpec` for it.

A fetcher module exposes:

    NAME            the Fetcher literal it implements
    HELP            one-line CLI help
    DRIFT_KEY       what re-fetching compares ("sha256" or "currency_date")
    spec(...)       -> PinSpec, keyword-only, the API the tests drive
    add_arguments(parser) / spec_from_args(args, *, fetch)   the CLI adapter for spec()
    drift_value(body) -> str
    content_request(url, *, env) -> (url, headers)   optional; re-attaches an API key at fetch time
    amended_since(source, *, fetch) -> date | None   optional; eCFR only

Every metadata call goes through the same injectable `FetchFn` the pin itself uses, so tests never
touch the network. Endpoint behaviour recorded in these modules was verified live on 2026-09-17
unless a comment says otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from types import ModuleType

from ..models import DriftKey
from ..pin import MissingKey
from . import courtlistener, ecfr, federalregister, govinfo, manual, openfec, uscode

# CLI subcommand order: most-used first, `manual` last as the escape hatch.
_MODULES: dict[str, ModuleType] = {
    m.NAME: m for m in (ecfr, federalregister, govinfo, uscode, openfec, courtlistener, manual)
}
NAMES: tuple[str, ...] = tuple(_MODULES)

__all__ = [
    "NAMES",
    "MissingKey",
    "amended_since",
    "content_request",
    "drift_key",
    "drift_value",
    "get",
]


def get(name: str) -> ModuleType:
    try:
        return _MODULES[name]
    except KeyError:
        raise ValueError(f"unknown fetcher {name!r} (have {', '.join(NAMES)})") from None


def content_request(name: str, url: str, *, env: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """What to request to re-fetch a stored canonical_url, with any API key re-attached from env.

    Raises MissingKey when the fetcher needs a key the environment doesn't have — the caller
    reports that as its own status, never as "no drift".
    """
    fn = getattr(get(name), "content_request", None)
    if fn is None:
        return url, {}
    return fn(url, env=env)


def drift_value(name: str, body: bytes) -> str:
    return get(name).drift_value(body)


def drift_key(name: str) -> DriftKey:
    return get(name).DRIFT_KEY


def amended_since(name: str) -> Callable[..., date | None] | None:
    """The fetcher's amendment-history hook, if it has one (eCFR does; nothing else needs it)."""
    return getattr(get(name), "amended_since", None)
