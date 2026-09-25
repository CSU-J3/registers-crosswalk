"""One module per document source, each one knowing how to build a `PinSpec` for it.

A fetcher module exposes:

    NAME            the Fetcher literal it implements
    HELP            one-line CLI help
    DRIFT_KEY       what re-fetching compares ("sha256" or "last_amended")
    VERIFIED        whether this fetcher has been run live against the real endpoint
    VERIFIED_AT     the date of that run, or None
    ENV_KEY         optional; the environment variable holding this fetcher's API key
    REQUIRES_ARCHIVE  optional; True refuses an `add` through this fetcher without --archive
    CHECK_SUPERSEDED  optional; True has `pin check` re-test this fetcher's superseded pins too
    spec(...)       -> PinSpec, keyword-only, the API the tests drive
    add_arguments(parser) / spec_from_args(args, *, fetch, env)   the CLI adapter for spec()
    drift_value(body) -> str
    content_request(url, *, env) -> (url, headers)   optional; re-attaches an API key at fetch time
    amended_since(source, *, fetch) -> date | None   optional; eCFR only

A fetcher that can look a document UP as well as fetch it also exposes:

    SEARCH_TYPES    the --type values its search accepts; absent means it has no search
    search(query, *, doc_type, fetch, env) -> list[SearchHit]
    add_command(hit, doc_type) -> str | None   the `pin add` that would pin that hit

`spec_from_args` takes `env` on every module, whether or not its `spec()` reads a key from one, so
a caller can hand the adapter a private mapping without knowing which fetchers need one. None means
`os.environ`, which is what the CLI passes. `registers_crosswalk.console` passes the `.env` it read
itself, so running the console never modifies the process environment.

Every metadata call goes through the same injectable `FetchFn` the pin itself uses, so tests never
touch the network. Each module's docstring dates the endpoint behaviour it records. A date on a
claim inherited from a handoff is the handoff's, kept as given, and is not a verification: what a
run from this tree covered is what `VERIFIED` and `VERIFIED_AT` record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from types import ModuleType

from ..models import DriftKey
from ..pin import MissingKey, SearchHit
from . import courtlistener, ecfr, fecfiling, federalregister, govinfo, manual, openfec, uscode

# CLI subcommand order: most-used first, `manual` last as the escape hatch.
_MODULES: dict[str, ModuleType] = {
    m.NAME: m
    for m in (ecfr, federalregister, govinfo, uscode, openfec, fecfiling, courtlistener, manual)
}
NAMES: tuple[str, ...] = tuple(_MODULES)
# The subset `pin search` offers. Derived, never hand-listed, so adding SEARCH_TYPES to a module
# is the only thing a new searchable fetcher has to do.
SEARCHABLE: tuple[str, ...] = tuple(n for n, m in _MODULES.items() if hasattr(m, "SEARCH_TYPES"))

__all__ = [
    "NAMES",
    "SEARCHABLE",
    "MissingKey",
    "add_command",
    "amended_since",
    "content_request",
    "drift_key",
    "drift_value",
    "get",
    "search",
    "search_types",
]


def get(name: str) -> ModuleType:
    try:
        return _MODULES[name]
    except KeyError:
        raise ValueError(f"unknown fetcher {name!r} (have {', '.join(NAMES)})") from None


def search_types(name: str) -> tuple[str, ...]:
    """The `--type` values this fetcher's search accepts; empty when it has no search at all."""
    return tuple(getattr(get(name), "SEARCH_TYPES", ()))


def search(
    name: str,
    query: str,
    *,
    doc_type: str,
    fetch: Callable[..., tuple[bytes, str]],
    env: Mapping[str, str],
) -> list[SearchHit]:
    """Look a document up by name or number. Read-only: no fetcher's search writes anything."""
    fn = getattr(get(name), "search", None)
    if fn is None:
        raise ValueError(f"{name} has no search (searchable: {', '.join(SEARCHABLE)})")
    return fn(query, doc_type=doc_type, fetch=fetch, env=env)


def add_command(name: str, hit: SearchHit, doc_type: str) -> str | None:
    """The `pin add` that would pin `hit`, or None when the hit is not directly pinnable."""
    fn = getattr(get(name), "add_command", None)
    return None if fn is None else fn(hit, doc_type)


def content_request(name: str, url: str, *, env: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """What to request to re-fetch a stored canonical_url, with any API key re-attached from env.

    Raises MissingKey when the fetcher needs a key the environment doesn't have — the caller
    reports that as its own status, never as "no drift".
    """
    fn = getattr(get(name), "content_request", None)
    if fn is None:
        return url, {}
    return fn(url, env=env)


def credential(name: str, env: Mapping[str, str]) -> str | None:
    """The fetcher's key as `env` holds it, or None if it has none or none is set.

    For masking, not for sending: `check` and `blobs` re-fetch a pin on a keyed host with the key
    attached by `content_request`, and this is the value to take out of any error that request
    raises before the error is reported.
    """
    var = getattr(get(name), "ENV_KEY", None)
    return (env.get(var) or None) if var else None


def drift_value(name: str, body: bytes) -> str:
    return get(name).drift_value(body)


def drift_key(name: str) -> DriftKey:
    return get(name).DRIFT_KEY


def requires_archive(name: str) -> bool:
    """Whether a pin through this fetcher is refused without --archive.

    True where the canonical URL cannot reproduce what was pinned once the document moves: uscode
    serves whatever is current, with no version axis, so an unarchived uscode pin drifts straight
    to "DRIFT unrecoverable (no archive)".
    """
    return bool(getattr(get(name), "REQUIRES_ARCHIVE", False))


def check_superseded(name: str) -> bool:
    """Whether `pin check` re-tests this fetcher's SUPERSEDED pins by default, not only live ones.

    True only where a superseded pin's canonical URL still serves exactly what was pinned. An FEC
    filing is: an amendment is a new filing with its own file number and URL, and the original
    never changes. The default is False, and ecfr and uscode must keep it. A superseded eCFR pin was
    superseded because its part was amended, so it would report AMENDED forever. A uscode URL serves
    only the current text, so the retired pin would read as DRIFT.
    """
    return bool(getattr(get(name), "CHECK_SUPERSEDED", False))


def amended_since(name: str) -> Callable[..., date | None] | None:
    """The fetcher's amendment-history hook, if it has one (eCFR does; nothing else needs it)."""
    return getattr(get(name), "amended_since", None)
