"""The api.data.gov key on a metadata call: attached in one place, and kept out of every error.

govinfo, openfec and fecfiling send their key the way api.data.gov asks for it, as the `api_key`
query parameter, so the key is part of the request URL, and the standard library copies that URL
into its exceptions. `HTTPError` keeps it as `url` and `filename`, `http.client.InvalidURL` quotes
the whole path and query in its message, and a `UnicodeEncodeError` holds the request line as
`object`. One f-string, log line or traceback of any of them prints the key, and on a CI runner
that is a public log. `keyed_url` is the one place a key is put into a URL, and `keyed_fetch`,
which every keyed metadata call goes through, lets no exception out with the key still inside.
The one keyed URL fetched anywhere else is govinfo's `content_request`, for a pin stored on its
API host, which no pin is; `check` and `blobs` mask the key out of what they report about it.

Every value in the query is URL-encoded here, operator input included. Before this, a MUR number
typed as `MUR 8098` went into the URL raw, and `http.client` refused the request line with an
`InvalidURL` that quoted the key.

When api.data.gov refuses the key, `keyed_fetch` says so by name: a `CredentialFailure` whose
message is the one line an operator needs, `CREDENTIAL FAILURE <fetcher> <status> <code>`.
Before this, a disabled, invalid or missing key was a bare `HTTP Error 403: Forbidden`, identical
for all three, with the code that told them apart left unread in the response body.
"""

from __future__ import annotations

import json
import re
import urllib.error
from collections.abc import Callable, Mapping
from email.message import Message
from urllib.parse import quote, quote_plus, urlencode, urlsplit

# What http.client refuses in a request line (`_contains_disallowed_url_pchar_re`), plus anything
# non-ASCII, which it cannot encode. Either makes it raise an error that repeats the whole URL.
_UNSENDABLE = re.compile(r"[\x00-\x20\x7f]|[^\x00-\x7f]")

# The hosts that take an api.data.gov key, and the codes api.data.gov answers with when it refuses
# one (https://api.data.gov/docs/developer-manual/#general-web-service-errors).
KEYED_HOSTS = frozenset({"api.govinfo.gov", "api.open.fec.gov"})
CREDENTIAL_CODES = frozenset({"API_KEY_DISABLED", "API_KEY_INVALID", "API_KEY_MISSING"})
# What is echoed from a response body is only ever a code of this shape, never free text.
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_BODY_LIMIT = 64 * 1024


class CredentialFailure(Exception):
    """api.data.gov refused the key: a 401 or 403 from a keyed host, or an `API_KEY_*` code.

    Its message is the whole report, `CREDENTIAL FAILURE <fetcher> <status> <code>`, with `UNKNOWN`
    when the body names no code. It is defined here, in a module that never runs as `__main__`, so
    it is the same class wherever it is caught. It is not an OSError: nothing that retries a
    transport failure retries a refused key, which stays refused.
    """

    def __init__(self, fetcher: str, status: int, code: str) -> None:
        super().__init__(f"CREDENTIAL FAILURE {fetcher} {status} {code}")
        self.fetcher = fetcher
        self.status = status
        self.code = code


def keyed_url(base: str, params: Mapping[str, object], key: str) -> str:
    """`base` with `params` and then the key as its query, every value URL-encoded. Never store it.

    `base` itself is not encoded, so it is refused if it could not go on the wire as it stands:
    the error that would follow names the whole URL, key included. The refusal names `base`,
    which has no key in it.
    """
    if _UNSENDABLE.search(base):
        raise ValueError(f"not a sendable URL, so no key is attached to it: {base!r}")
    query = urlencode({**params, "api_key": key})
    return f"{base}{'&' if '?' in base else '?'}{query}"


def keyed_fetch(
    fetch: Callable[..., tuple[bytes, str]],
    base: str,
    params: Mapping[str, object],
    *,
    key: str,
    fetcher: str,
) -> tuple[bytes, str]:
    """Request `base` with the key attached, and let no exception out with the key inside it.

    An exception keeps its type and its traceback, so every `except OSError` and `except
    ValueError` that matched before still matches. Only the key is gone, from the exception and
    from anything chained to it, so there is no unredacted original left to print either.

    The one exception that changes type is a refusal of the key itself, which becomes a
    `CredentialFailure` naming `fetcher`, the status and api.data.gov's code. It is raised `from
    None`: the HTTPError behind it has nothing more to say, and is redacted all the same. Nothing
    retries it.
    """
    url = keyed_url(base, params, key)
    try:
        return fetch(url, None)
    except urllib.error.HTTPError as exc:
        failure = _credential_failure(exc, fetcher=fetcher, host=urlsplit(base).hostname)
        redact(exc, key)
        if failure is None:
            raise
        raise failure from None
    except Exception as exc:
        redact(exc, key)
        raise


def _credential_failure(
    exc: urllib.error.HTTPError, *, fetcher: str, host: str | None
) -> CredentialFailure | None:
    code = _error_code(exc)
    if (exc.code in (401, 403) and host in KEYED_HOSTS) or code in CREDENTIAL_CODES:
        return CredentialFailure(fetcher, exc.code, code or "UNKNOWN")
    return None


def _error_code(exc: urllib.error.HTTPError) -> str | None:
    """api.data.gov's `error.code`, or None. The body is read once, bounded, and decoded first:
    `default_fetch` asks for gzip, so an error body may come back compressed."""
    from .pin import _decoded  # here, not at the top: pin imports this module

    try:
        payload = json.loads(_decoded(exc.read(_BODY_LIMIT), exc.headers))
        code = payload["error"]["code"]
    except Exception:  # noqa: BLE001 - no readable code is an answer: UNKNOWN
        return None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


def redact(exc: BaseException, secret: str) -> BaseException:
    """Mask `secret`, raw or URL-encoded, everywhere `exc` or its chain can hold it. In place.

    Each occurrence becomes asterisks of the same length, so a `UnicodeEncodeError`'s `start` and
    `end` still point at the character that failed. The places searched are `args`, the instance
    attributes (`HTTPError.url`, its headers, the `url` of the response it wraps), and the slots
    the C exceptions keep outside them (`OSError.filename`, `UnicodeError.object`,
    `URLError.reason`), then the same again for every exception reachable through `__cause__`,
    `__context__` or `reason`.

    Not searched: a response body, which holds the key only if the server echoes it and which
    nothing here prints, and frame locals, which only a locals-capturing traceback shows.
    """
    forms = {secret, quote_plus(secret), quote(secret, safe="")} - {""}
    seen: set[int] = set()
    todo: list[object] = [exc]
    while todo:
        e = todo.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen.add(id(e))
        e.args = tuple(_masked(a, forms, todo) for a in e.args)
        for name, value in list(vars(e).items()):
            if isinstance(value, Message):
                _mask_headers(value, forms)
                continue
            masked = _masked(value, forms, todo)
            if masked is not value:
                setattr(e, name, masked)
            elif isinstance(getattr(value, "url", None), str):
                # HTTPError's `fp` and `file` are the http.client response, which keeps the URL too.
                value.url = _masked(value.url, forms, todo)
        for name in ("filename", "filename2", "strerror", "object", "reason"):
            try:
                value = getattr(e, name)
            except AttributeError:
                continue
            masked = _masked(value, forms, todo)
            if masked is not value:
                try:
                    setattr(e, name, masked)
                except (AttributeError, TypeError):
                    pass  # a read-only property derived from something masked above
        todo += [e.__cause__, e.__context__]
    return exc


def _masked(value: object, forms: set[str], todo: list[object]) -> object:
    if isinstance(value, BaseException):
        todo.append(value)
        return value
    if isinstance(value, str):
        out = value
        for form in forms:
            out = out.replace(form, "*" * len(form))
        return value if out == value else out
    if isinstance(value, bytes | bytearray):
        out = bytes(value)
        for form in forms:
            encoded = form.encode("utf-8", "replace")
            out = out.replace(encoded, b"*" * len(encoded))
        return value if out == value else type(value)(out)
    if isinstance(value, tuple | list):
        items = [_masked(v, forms, todo) for v in value]
        if all(a is b for a, b in zip(items, value, strict=True)):
            return value
        return type(value)(items)
    return value


def _mask_headers(headers: Message, forms: set[str]) -> None:
    # A redirect's Location can repeat the query. Rewritten only when something matched, and then
    # all at once, so the headers keep their order and their duplicates.
    items = headers.items()
    masked = [(name, _masked(value, forms, [])) for name, value in items]
    if all(m is v for (_, m), (_, v) in zip(masked, items, strict=True)):
        return
    for name in {name for name, _ in items}:
        del headers[name]
    for name, value in masked:
        headers[name] = value
