"""Pin a document: fetch it once, hash it, and mint a source node that POINTS at it.

Nothing here ever writes a fetched document's bytes into this repo. `pin()` refuses a `blob_dir`
that resolves inside the repo root, and a `Source` holds only facts about the document as an object
(where it lives, when it was fetched, what it hashes to, who published it, when). Blobs live in the
consuming project or in the archive copy; the artifact hash verifies either.

CLI: `python -m registers_crosswalk.pin {add,archive,note,search,check,ledger}`.
"""

from __future__ import annotations

import argparse
import functools
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urljoin

from .ids import SRC_ID
from .models import (
    ArchiveCopy,
    Artifact,
    CitationRef,
    DriftKey,
    Fetcher,
    Grade,
    Source,
    XrModel,
    check_public_url,
)
from .registry import (
    DATA_DIR,
    Crosswalk,
    check_source_invariants,
    duplicate_of,
    live_sources,
    same_document_of,
    title_adds_anything,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# A fetch is always injectable so tests never touch the network (working rule 5).
# (url, headers) -> (body, media_type)
FetchFn = Callable[[str, Mapping[str, str] | None], tuple[bytes, str]]
# archive() needs RESPONSE HEADERS rather than a body (Wayback answers in Content-Location), so it
# gets its own injection point of a different shape. Same rule, different signature.
HeadersFn = Callable[[str, Mapping[str, str] | None], Mapping[str, str]]
# SPN2 answers in a JSON body rather than in response headers, so the keyed path needs a third
# injection point. (url, headers, data) -> parsed JSON; `data` None is a GET, bytes is a POST.
JsonFn = Callable[[str, Mapping[str, str] | None, bytes | None], Mapping[str, object]]
# Polling has to wait between attempts; tests pass a fake so the suite neither sleeps nor drifts.
SleepFn = Callable[[float], None]
# The CLI's archiving step, injected for the same reason: tests stay offline. `archive()` answers
# an ArchiveFailure when there is no capture; a plain None is still accepted from an injected fake
# and normalised by `add_source`, so a test double stays a one-liner.
#
# Called as `archive_fn(url, expected_sha256=...)`. The hash is what lets an already-archived
# document skip Save Page Now, and it is keyword-only so a fake that does not care can take
# `**_` and ignore it.
ArchiveFn = Callable[..., "ArchiveCopy | ArchiveFailure | None"]

_UA = "registers-crosswalk/0.1 (+https://github.com/CSU-J3/registers-crosswalk)"
# eCFR's versioner returns 406 without an Accept-Encoding the client will take (verified
# 2026-09-17), so gzip is requested on every fetch and decompressed here. The hash is ALWAYS over
# the decompressed bytes, so a server switching its transfer encoding is not drift.
_BASE_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip"}

_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/html": ".html",
    "application/json": ".json",
    "text/plain": ".txt",
}
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class MissingKey(RuntimeError):
    """A fetcher needs an API key from the environment and it isn't set."""

    def __init__(self, env_var: str, fetcher: str) -> None:
        super().__init__(f"{fetcher}: environment variable {env_var} is not set")
        self.env_var = env_var
        self.fetcher = fetcher


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _media_type(content_type: str | None) -> str:
    """The bare media type, parameters dropped: "text/html; charset=utf-8" -> "text/html"."""
    if not content_type:
        return "application/octet-stream"
    return content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"


def default_fetch(
    url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 60
) -> tuple[bytes, str]:
    """Fetch `url` and return (decompressed body, media type). Follows redirects."""
    req = urllib.request.Request(url, headers={**_BASE_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        media_type = _media_type(resp.headers.get("Content-Type"))
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw)
    return raw, media_type


@dataclass(frozen=True)
class PinSpec:
    """Everything a fetcher knows about a document before its bytes are read.

    A frozen dataclass rather than an XrModel because `drift_value` is a callable, and typing that
    on a pydantic model would mean turning on `arbitrary_types_allowed` repo-wide (decision 2). A
    PinSpec never serializes; only the `Source` it produces does.
    """

    fetcher: Fetcher
    # Stored on the Source. Key-free, always (decision 1).
    canonical_url: str
    citation: str
    title: str
    grade: Grade
    # Reads the fetched body and returns the value re-fetching will compare against.
    drift_value: Callable[[bytes], str]
    # What to actually request, when that differs from what we store (govinfo re-attaches its key
    # here). Never stored, never printed. None means "fetch canonical_url".
    fetch_url: str | None = None
    drift_key: DriftKey = "sha256"
    # Copied from the fetcher module's VERIFIED / VERIFIED_AT onto every record it mints, so the
    # claim is set once per fetcher rather than re-typed per record.
    fetcher_verified: bool = False
    verified_at: date | None = None
    publisher: str | None = None
    point_in_time: date | None = None
    published_at: date | None = None
    headers: Mapping[str, str] | None = None


@dataclass(frozen=True)
class SearchHit:
    """One candidate document from a fetcher's search, in the shape `add` needs next.

    The point of `search` is the round trip: a case name or a respondent goes in, and what comes
    back is the identifier `add` takes — a cluster id, an AO or MUR number. Everything else on the
    hit is there so a human can tell two hits apart before pinning one.

    A frozen dataclass for the same reason as `PinSpec`: it never serializes to `data/`. It is a
    view of somebody else's search index, not a fact this repo stores.

    `url` is the hit's own public page where the API gives one. It is NEVER a URL carrying a key:
    openfec's search call is authenticated, and the key is used for the query and then forgotten
    (decision 1, `docs/operations.md`).
    """

    identifier: str
    label: str
    court_or_office: str | None = None
    date: str | None = None
    docket_or_number: str | None = None
    citation: str | None = None
    url: str | None = None


def _extension(media_type: str) -> str:
    return _EXTENSIONS.get(media_type, ".bin")


def _check_blob_dir(blob_dir: Path) -> Path:
    """A blob dir belongs to the CONSUMING project. Refuse anything inside this repo."""
    resolved = blob_dir.expanduser().resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise ValueError(
            f"blob_dir {resolved} is inside {REPO_ROOT}; this repo never stores document bytes. "
            "Point --blob-dir at the consuming project (e.g. New Gray's pins/)."
        )
    return resolved


def pin(
    spec: PinSpec,
    *,
    next_id: str,
    fetch: FetchFn = default_fetch,
    blob_dir: Path | None = None,
    now: datetime | None = None,
) -> Source:
    """Fetch the document `spec` describes and return the `Source` node for it.

    With `blob_dir` (a path outside this repo), the bytes are also written to
    `<blob_dir>/<sha256><ext>` if that file is not already there — content-addressed, so pinning
    the same bytes twice writes once.
    """
    check_public_url(spec.canonical_url)
    target = _check_blob_dir(blob_dir) if blob_dir is not None else None

    body, media_type = fetch(spec.fetch_url or spec.canonical_url, spec.headers)
    if not body:
        # An empty body hashes to a perfectly valid-looking sha256; pinning it would record a
        # failed fetch as a document. Refuse rather than mint a hash of nothing.
        raise ValueError(f"{spec.citation}: fetch returned an empty body; nothing to pin")

    source = Source(
        xr_id=next_id,
        kind="source",
        citation=spec.citation,
        title=spec.title,
        publisher=spec.publisher,
        canonical_url=spec.canonical_url,
        fetcher=spec.fetcher,
        fetcher_verified=spec.fetcher_verified,
        verified_at=spec.verified_at,
        point_in_time=spec.point_in_time,
        published_at=spec.published_at,
        artifact=Artifact(
            sha256=sha256_hex(body),
            byte_length=len(body),
            media_type=media_type,
            fetched_at=now or datetime.now(tz=UTC),
            drift_key=spec.drift_key,
            drift_value=spec.drift_value(body),
        ),
        grade=spec.grade,
    )
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        blob = target / f"{source.artifact.sha256}{_extension(media_type)}"
        if not blob.exists():
            blob.write_bytes(body)
    return source


# --------------------------------------------------------------------------- archiving

_WAYBACK_SAVE = "https://web.archive.org/save/"
# SPN2: POST the url, then poll the job. The anonymous GET above is the synchronous save path,
# which answered 500 for the uscode section URL on 2026-09-18 (anonymous) and again on 2026-09-19
# with keys — see docs/operations.md. SPN2 is the interface Wayback documents for keyholders.
_WAYBACK_SPN2 = "https://web.archive.org/save"
_WAYBACK_SPN2_STATUS = "https://web.archive.org/save/status/"
# Appended to every failure of the anonymous path, the one a run without keys takes.
_ANONYMOUS_PATH = (
    "no WAYBACK keys in this environment, so the anonymous save path was used; "
    "it has answered 500 for these hosts before"
)
_WAYBACK_TS = re.compile(r"/web/(\d{14})/")
# How long to wait between polls of an SPN2 job, and the shape of the wait. Injected in tests so
# they neither sleep nor reach the network.
_SPN2_POLL_SECONDS = 5.0
# Waits between retries of a 5xx, in order: three retries over about a minute. Wayback's
# "Temporarily Offline" blips are short — one on 2026-09-20 was over inside a minute — so the
# schedule is shaped to outlast one without making a failed pin take appreciably longer to fail.
_ARCHIVE_RETRY_WAITS: tuple[float, ...] = (10.0, 20.0, 30.0)
# 429 is a different animal from 5xx and gets its own budget. It is not a fault, it is the service
# telling us the rate: the wait comes from Retry-After when the response carries one, so the only
# number we invent is the fallback. Three tries, because a rate limit that has not lifted after two
# waits is not going to lift inside this pin.
_RATE_LIMIT_TRIES = 3
_RATE_LIMIT_WAIT = 60.0
# Where to ask whether a capture already exists. Read-only, and not part of Save Page Now: asking
# costs nothing and takes no capture.
_WAYBACK_AVAILABLE = "https://archive.org/wayback/available"


def _response_headers(
    url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 90
) -> Mapping[str, str]:
    req = urllib.request.Request(url, headers={**_BASE_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return dict(resp.headers)


def _json_call(
    url: str,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    *,
    timeout: float = 90,
) -> Mapping[str, object]:
    """POST (with `data`) or GET `url` and parse the JSON reply. No gzip: these bodies are tiny."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return value
    return None


def _capture_from_timestamp(url: str, timestamp: str) -> ArchiveCopy:
    return ArchiveCopy(
        service="wayback",
        url=f"https://web.archive.org/web/{timestamp}/{url}",
        captured_at=datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC),
    )


@dataclass(frozen=True)
class ArchiveFailure:
    """Why a capture did not happen, in words fit to print after "archive step failed:".

    `archive()` used to answer None for everything — service down, url refused, still pending —
    and `add_source` turned every one of those into the same sentence. A transient 503 and a URL
    Wayback will never take are not the same problem, and the operator can only act on the first
    one; saying which is which is the whole point of this type.
    """

    reason: str


def _spn2_reason(payload: Mapping[str, object]) -> str:
    """The failure as SPN2 itself described it: status, status_ext and message, when present."""
    bits = []
    for field in ("status", "status_ext", "message"):
        value = payload.get(field)
        if value:
            bits.append(f"{field}={value}")
    return ", ".join(bits) if bits else "no reason given"


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """The Retry-After header as seconds, accepting both forms the spec allows.

    Delta-seconds is what Wayback sends in practice; the HTTP-date form is parsed too rather than
    ignored, because ignoring it would silently substitute our own number for the one the service
    actually asked for, which is the opposite of honouring it.
    """
    value = (exc.headers.get("Retry-After") if exc.headers else None) or ""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())


def _retrying(
    call: Callable[[], object],
    *,
    sleep_fn: SleepFn,
    waits: Sequence[float] = _ARCHIVE_RETRY_WAITS,
    rate_limit_tries: int = _RATE_LIMIT_TRIES,
) -> object:
    """Run `call`, retrying a 5xx or a 429. Each has its own budget, because each means something
    different.

    **5xx.** Save Page Now answers 503 with an HTML "Internet Archive: Temporarily Offline" page
    during short outages — one was observed lasting under a minute on 2026-09-20, with the very
    next request succeeding. Before this, that blip propagated as a bare failure and refused an
    otherwise good pin, which for `uscode` (where --archive is required) meant the pin could not
    be made at all until someone tried again by hand.

    **429.** Not a fault but a rate: the service is telling us when to come back, so `Retry-After`
    is honoured when it is sent and `_RATE_LIMIT_WAIT` used only when it is not.

    Any other 4xx is Wayback saying no — a url it will not take, a bad credential — and asking
    again cannot change the answer.
    """
    server_failures = 0
    rate_limit_tries_used = 0
    while True:
        try:
            return call()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                rate_limit_tries_used += 1
                if rate_limit_tries_used >= rate_limit_tries:
                    raise
                sleep_fn(_retry_after(exc) or _RATE_LIMIT_WAIT)
                continue
            if exc.code < 500:
                raise
            server_failures += 1
            if server_failures > len(waits):
                raise
            sleep_fn(waits[server_failures - 1])


def _existing_capture(
    url: str,
    expected_sha256: str,
    *,
    json_fn: JsonFn,
    fetch_fn: FetchFn,
) -> ArchiveCopy | None:
    """A capture Wayback already holds whose BYTES are the ones we pinned, or None.

    Asking first is worth a request because Save Page Now will not take a new capture of a url it
    captured in the last hour anyway — it answers `success` with the older capture's timestamp
    (observed 2026-09-20: `duration_sec: 0.52`, `resources: []`, nothing fetched). So for a
    document already in the archive, the SPN2 round trip buys nothing and costs a write against a
    rate-limited service. This route skips it.

    The hash is the whole safety of it. An availability hit says only that SOMETHING was captured
    at that url, which is a different claim from "the bytes we pinned are recoverable" — a url
    that served a different document last year has a capture, and reusing it would attach a
    recoverable copy of the wrong thing to the record. So the capture's own bytes are fetched
    through the `id_` form (which serves the original response, without Wayback's banner or any
    rewriting) and hashed, and anything but an exact match falls through to a real capture.

    Best-effort in both directions: any failure here returns None and the caller asks SPN2, which
    is what it would have done anyway. A broken availability API must not be able to refuse a pin.
    """
    try:
        payload = json_fn(f"{_WAYBACK_AVAILABLE}?{urlencode({'url': url})}", None, None)
        snapshot = ((payload.get("archived_snapshots") or {}) or {}).get("closest") or {}
        if not isinstance(snapshot, Mapping):
            return None
        timestamp = snapshot.get("timestamp")
        if not snapshot.get("available") or not timestamp:
            return None
        # `id_` asks for the archived response as it was served. Without it Wayback returns the
        # document wrapped in its own toolbar and with links rewritten, which would never hash to
        # what we pinned and would make this check always fail.
        body, _ = fetch_fn(f"https://web.archive.org/web/{timestamp}id_/{url}", None)
    except Exception:  # noqa: BLE001 - an optimisation may not raise; fall through to SPN2
        return None
    if sha256_hex(body) != expected_sha256:
        return None
    return _capture_from_timestamp(url, str(timestamp))


def _archive_spn2(
    url: str,
    auth: Mapping[str, str],
    *,
    json_fn: JsonFn,
    sleep_fn: SleepFn,
    timeout: float,
) -> ArchiveCopy | ArchiveFailure:
    """Capture `url` through Wayback's SPN2 job interface: POST the url, then poll the job.

    Raises nothing of its own; `archive()` owns the best-effort contract. `timeout` is the whole
    poll budget, not a per-request one — a job that is still pending when it runs out is a failed
    capture, the same as an error, because the caller has a pin to write or refuse now.

    A `success` here does NOT mean a capture was just taken. SPN2 reuses a capture under an hour
    old and says so in `message`, returning that older capture's timestamp — which is a real
    capture of the same bytes, so it is accepted, but it is why an archive can predate the fetch
    it belongs to. See docs/operations.md.
    """
    headers = {**auth, "Accept": "application/json"}
    started = _retrying(
        lambda: json_fn(
            _WAYBACK_SPN2,
            {**headers, "Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"url": url}).encode("utf-8"),
        ),
        sleep_fn=sleep_fn,
    )
    assert isinstance(started, Mapping)  # noqa: S101 - json_fn's contract
    job_id = started.get("job_id")
    if not job_id:
        # SPN2 refuses some urls outright (robots, a host it will not fetch) and says so here
        # instead of handing back a job.
        return ArchiveFailure(f"save refused {url}: {_spn2_reason(started)}")
    waited = 0.0
    while waited < timeout:
        sleep_fn(_SPN2_POLL_SECONDS)
        waited += _SPN2_POLL_SECONDS
        state = _retrying(
            lambda: json_fn(f"{_WAYBACK_SPN2_STATUS}{job_id}", headers, None), sleep_fn=sleep_fn
        )
        assert isinstance(state, Mapping)  # noqa: S101 - json_fn's contract
        status = state.get("status")
        if status == "pending":
            continue
        if status != "success":
            # "error", or a status this code does not know: either way there is no capture.
            return ArchiveFailure(f"save job for {url} ended: {_spn2_reason(state)}")
        timestamp = state.get("timestamp")
        if not timestamp:
            return ArchiveFailure(f"save job for {url} reported success with no timestamp")
        return _capture_from_timestamp(str(state.get("original_url") or url), str(timestamp))
    return ArchiveFailure(f"save job for {url} still pending after {timeout:.0f}s")


def archive(
    url: str,
    *,
    expected_sha256: str | None = None,
    headers_fn: HeadersFn | None = None,
    json_fn: JsonFn | None = None,
    fetch_fn: FetchFn | None = None,
    sleep_fn: SleepFn | None = None,
    timeout: float = 90,
    env: Mapping[str, str] | None = None,
) -> ArchiveCopy | ArchiveFailure:
    """Ask the Wayback Machine to capture `url`; return the capture, or why there isn't one.

    Best-effort by design: archiving is a courtesy copy, not the pin. No failure raises — a slow
    or broken archive can't cost you a good fetch — but every failure now says what it was, so
    `add_source` can print a reason the operator can act on rather than one sentence for all of
    them. A transient 5xx or a 429 is retried first; see `_retrying`.

    With `expected_sha256`, an existing capture whose bytes hash to it is returned as-is and no
    capture is requested at all — see `_existing_capture`. Without it there is no reuse, because
    reuse is conditional on the bytes matching and there would be nothing to match them against.

    Two interfaces, chosen by whether WAYBACK_ACCESS_KEY/WAYBACK_SECRET_KEY are both set. With
    keys, the documented SPN2 job interface: POST the url, poll the job, read `timestamp` from the
    success reply. Without them, the anonymous synchronous save, which answers in
    `Content-Location`. The keyed path is not merely the anonymous one with a header: sending the
    Authorization header on that GET was tried against the uscode section URL on 2026-09-19 and
    answered 500, as the anonymous GET had the day before.

    `env` defaults to `os.environ`, and a caller holding keys OUTSIDE the environment must pass
    its own mapping — `registers_crosswalk.console` does, and a pin made through it took the
    anonymous path for as long as it did not.

    TODO: Perma.cc as a second service (needs an API key and a registrar account).
    """
    env = os.environ if env is None else env
    sleep_fn = sleep_fn or time.sleep
    json_call = json_fn or functools.partial(_json_call, timeout=timeout)

    # Ask before asking for a capture. Only with a hash to check it against: an archive is reused
    # when its BYTES are the ones we pinned, never merely because a capture of that url exists.
    # Without `expected_sha256` there is nothing to check, so there is no reuse.
    if expected_sha256:
        existing = _existing_capture(
            url,
            expected_sha256,
            json_fn=json_call,
            fetch_fn=fetch_fn or functools.partial(default_fetch, timeout=timeout),
        )
        if existing is not None:
            return existing

    auth: dict[str, str] = {}
    access, secret = env.get("WAYBACK_ACCESS_KEY"), env.get("WAYBACK_SECRET_KEY")
    if access and secret:
        auth["Authorization"] = f"LOW {access}:{secret}"
    try:
        if auth:
            return _archive_spn2(
                url,
                auth,
                json_fn=json_call,
                sleep_fn=sleep_fn,
                timeout=timeout,
            )
        fn = headers_fn or functools.partial(_response_headers, timeout=timeout)
        headers = _retrying(lambda: fn(f"{_WAYBACK_SAVE}{url}", None), sleep_fn=sleep_fn)
        assert isinstance(headers, Mapping)  # noqa: S101 - headers_fn's contract
        location = _header(headers, "Content-Location")
        if location:
            captured_at = None
            m = _WAYBACK_TS.search(location)
            if m is not None:
                captured_at = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            return ArchiveCopy(
                service="wayback",
                url=urljoin("https://web.archive.org", location),
                captured_at=captured_at,
            )
        failure = ArchiveFailure(f"anonymous save of {url} returned no Content-Location")
    except urllib.error.HTTPError as exc:
        # 5xx and 429 have already been retried by the time they reach here.
        if exc.code == 429:
            failure = ArchiveFailure(
                f"HTTP 429 from the Wayback Machine for {url}: still rate limited after "
                f"{_RATE_LIMIT_TRIES} tries"
            )
        else:
            failure = ArchiveFailure(f"HTTP {exc.code} from the Wayback Machine for {url}")
    except Exception as exc:  # noqa: BLE001 - best-effort contract: nothing here may raise
        failure = ArchiveFailure(f"{type(exc).__name__}: {exc}")
    # A keyless run looks exactly like a Wayback outage from the outside: the anonymous save has
    # answered 500 for uscode.house.gov and storage.courtlistener.com while SPN2 took the same urls
    # minutes later. Say which path failed, so the fix (load the keys) is not mistaken for a wait.
    if not auth:
        failure = ArchiveFailure(f"{failure.reason}; {_ANONYMOUS_PATH}")
    return failure


# --------------------------------------------------------------------------- drift

# A transport failure is NOT drift. Every urllib transport error — HTTPError (non-2xx), URLError
# (refused), gaierror (DNS), TimeoutError — subclasses OSError, so that one type cleanly separates
# "we could not reach the document" from "we read the document and it had changed".
_TRANSPORT = OSError

DriftStatus = Literal["ok", "drift", "amended", "key_missing", "fetch_failed", "error"]

# status -> process exit code. A check that could not RUN (2, 3) is reported separately from a
# check that ran and found a problem (1); conflating them is how a dead endpoint gets mistaken for
# a changed document. Precedence when a run mixes statuses: 2 > 3 > 1 > 0 — fix the environment
# first, then the transport, and only then read the drift answers, which are meaningless until
# every source was actually reachable.
_EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "drift": 1,
    "amended": 1,
    # A parse failure means the document no longer states what we read from it (uscode dropped its
    # source credit, an API changed shape). The fetch worked, so it is a content problem: exit 1.
    "error": 1,
    "key_missing": 2,
    "fetch_failed": 3,
}
# Reporting precedence, which is NOT numeric order: 2 and 3 outrank 1 because they mean the check
# never ran. Keyed by exit code.
_EXIT_RANK: dict[int, int] = {0: 0, 1: 1, 3: 2, 2: 3}


class DriftReport(XrModel):
    xr_id: str
    citation: str
    status: DriftStatus
    drift_key: DriftKey
    expected: str
    actual: str | None = None
    detail: str | None = None
    # Whether the pin has an archive copy. Drift on an archived source is recoverable — you can
    # still read the old text and diff it. Drift on an unarchived one is not: the digest becomes
    # the only surviving evidence that the old text existed, and it cannot tell you what it said.
    # Same exit code, very different situation, so the row says which.
    archived: bool = True


def check(
    source: Source, *, fetch: FetchFn = default_fetch, env: Mapping[str, str] | None = None
) -> DriftReport:
    """Re-fetch a pinned document and compare it against what was pinned.

    Compares the fetcher's own drift key, not always the hash: uscode.house.gov re-renders its
    markup, so for those sources the parsed "laws in effect on" date is the signal. For eCFR
    sources a matching hash is not the end of it — the point-in-time URL keeps returning the same
    bytes after the part is amended, so the amendment history is checked too.
    """
    # Imported here, not at module scope: the fetchers import PinSpec and default_fetch from this
    # module, so a top-level import either way round would be circular.
    from . import fetchers

    base = {
        "xr_id": source.xr_id,
        "citation": source.citation,
        "drift_key": source.artifact.drift_key,
        "expected": source.artifact.drift_value,
        "archived": bool(source.archives),
    }
    try:
        url, headers = fetchers.content_request(
            source.fetcher, source.canonical_url, env=os.environ if env is None else env
        )
    except MissingKey as exc:
        return DriftReport(**base, status="key_missing", detail=str(exc))
    # The fetch and the parse are caught separately on purpose: whatever the fetch callable raises
    # is a transport problem, whatever the parse raises is a content problem, and they get
    # different exit codes.
    try:
        body, _ = fetch(url, headers)
    except _TRANSPORT as exc:
        return DriftReport(**base, status="fetch_failed", detail=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return DriftReport(**base, status="error", detail=f"{type(exc).__name__}: {exc}")
    try:
        actual = fetchers.drift_value(source.fetcher, body)
    except Exception as exc:
        return DriftReport(**base, status="error", detail=f"{type(exc).__name__}: {exc}")

    if actual != source.artifact.drift_value:
        return DriftReport(**base, status="drift", actual=actual)

    amended_since = fetchers.amended_since(source.fetcher)
    if amended_since is not None:
        try:
            amended = amended_since(source, fetch=fetch)
        except _TRANSPORT as exc:
            return DriftReport(
                **base,
                status="fetch_failed",
                actual=actual,
                detail=f"amendment history unreachable: {type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            return DriftReport(
                **base, status="error", actual=actual, detail=f"{type(exc).__name__}: {exc}"
            )
        if amended is not None:
            return DriftReport(
                **base,
                status="amended",
                actual=actual,
                detail=f"amended since pin: latest substantive amendment {amended.isoformat()}",
            )
    return DriftReport(**base, status="ok", actual=actual)


# --------------------------------------------------------------------------- emitting


def _slug(citation: str) -> str:
    return _SLUG_STRIP.sub("-", citation.casefold()).strip("-")


def to_ledger_markdown(source: Source) -> str:
    """One entry in the New Gray source-links house style (three lines)."""
    when = source.published_at or source.point_in_time
    head_bits = [b for b in (source.publisher, when.isoformat() if when else None) if b]
    head = ", ".join(head_bits) if head_bits else "undated"
    # `fetched_at` is UTC, and the day it names can be tomorrow for a reader west of Greenwich, so
    # the entry says which day it means. The document dates beside it are dates, not instants.
    retrieved = f"Retrieved {source.artifact.fetched_at:%Y-%m-%d} UTC"
    # Fetchers with a point in time put it in the title too ("11 CFR Part 114, as of 2026-09-14"),
    # so appending it unconditionally printed the same date twice on one line.
    if source.point_in_time is not None and source.point_in_time.isoformat() not in source.title:
        retrieved += f", as of {source.point_in_time.isoformat()}"
    archive_url = source.archives[0].url if source.archives else "pending"
    # A title that already opens with its citation carries it ("11 CFR Part 114, as of
    # 2026-09-14"), and appending it again would print it twice. A title that does not — a Federal
    # Register document's name, a case name — would otherwise leave the entry naming a document it
    # never cites, which is the one thing a source-links entry has to do. Same predicate as the
    # status page's document cell, from one definition, so the page and the ledger cannot end up
    # describing the same pin two ways.
    document = f"{source.title}, {source.citation}" if title_adds_anything(source) else source.title
    return (
        f"- **{head} ({source.grade.code()})** — {document}. "
        f"{retrieved}; sha256 {source.artifact.sha256[:16]}…; {source.xr_id}.\n"
        f"  {source.canonical_url}\n"
        f"  Archive: {archive_url}"
    )


def to_manifest_line(source: Source) -> str:
    """One line in `sha256sum` format, for verifying a consuming project's blob directory.

    The filename leads with the id because citations are not unique: the six MUR pins share two
    citations, and a manifest naming one file three times cannot pass `sha256sum -c`. The id also
    travels with the file into a `pins/` directory, so no trailing comment is needed to say which
    pin a line is — and `sha256sum` would read such a comment as part of the filename.
    """
    name = f"{source.xr_id}-{_slug(source.citation)}{_extension(source.artifact.media_type)}"
    return f"{source.artifact.sha256}  {name}"


# --------------------------------------------------------------------------- CLI


def _sources_dir(data_dir: Path) -> Path:
    return data_dir / "sources"


def next_source_id(data_dir: Path) -> str:
    """Mint the next id: the highest existing xr_src_NNNN plus one, zero-padded."""
    highest = 0
    for p in _sources_dir(data_dir).glob("xr_src_*.json"):
        if SRC_ID.match(p.stem):
            highest = max(highest, int(p.stem.rsplit("_", 1)[1]))
    return f"xr_src_{highest + 1:04d}"


def _src_id_arg(value: str) -> str:
    if not SRC_ID.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an xr_src_NNNN id")
    return value


def _cited_in_arg(value: str) -> CitationRef:
    register, sep, local_id = value.partition(":")
    if not sep or not register or not local_id:
        raise argparse.ArgumentTypeError(f"--cited-in expects register:local_id, got {value!r}")
    try:
        return CitationRef(register=register, local_id=local_id, ref_type="record_mention")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _live_pins(xw: Crosswalk) -> list[Source]:
    """Every pin that still claims to be current, in id order: `check`'s default target set.

    One pin per *citation* was the earlier rule, and it was wrong as soon as a citation named a
    proceeding rather than a text. An FEC MUR is one citation over several documents — the
    certification of the vote, the General Counsel's report, the closing notification — and they
    are not versions of each other, so picking the "latest" silently dropped the other two from
    every drift run. A pin nothing supersedes is a document this repo still stands behind, and
    standing behind it is exactly what `check` re-tests.

    Liveness is `registry.live_sources`, the same predicate the duplicate rule and the load-path
    invariant use, so a pin `add` would refuse as a live duplicate is a pin `check` re-fetches.
    """
    return sorted(live_sources(xw.sources).values(), key=lambda s: s.xr_id)


@dataclass(frozen=True)
class AddOutcome:
    """What `add_source` did, in the shape both callers need to report it.

    A frozen dataclass rather than an exception per refusal: a refusal here is an ordinary answer
    ("that document is already pinned"), not a failure of the program, and the console has to put
    it on a page as readily as the CLI puts it on stderr. `message` is the CLI's own wording,
    unchanged, so the two cannot describe the same refusal differently.
    """

    status: Literal["written", "refused", "archive_failed"]
    message: str
    source: Source | None = None
    path: Path | None = None
    ledger: str | None = None


def add_source(
    spec: PinSpec,
    *,
    data_dir: Path,
    archive: bool = False,
    supersedes: str | None = None,
    cited_in: list[CitationRef] | None = None,
    notes: str | None = None,
    blob_dir: Path | None = None,
    fetch: FetchFn = default_fetch,
    # Bound at definition time, where `archive` is still the module-level function; the `archive`
    # parameter above only shadows it inside the body.
    archive_fn: ArchiveFn = archive,
) -> AddOutcome:
    """Guard, fetch, archive and write one pin. The whole write side of `add`, minus argparse.

    Extracted from `_cmd_add` so the console reaches the guards through the same code rather than
    around it (working rule 5 of the console handoff). `_cmd_add` keeps only what is genuinely
    about the command line: the two refusals that can be decided from flags alone, and turning
    `args` into a `PinSpec`. Everything from the `duplicate_of` early exit to the write lives
    here, and every refusal string is the one `tests/test_pin_cli.py` already asserts.

    Returns rather than prints, and never calls `sys.exit`: the caller decides what a refusal
    looks like. `archive_failed` is kept distinct from `refused` because it is the one outcome the
    operator can fix by retrying, which is worth saying on a page with a button on it.
    """
    xw = Crosswalk(data_dir)

    # Cheap early exit on the one refusal we can reach without doing any work: re-pinning a
    # document version we already hold. Placed before pin() and before the archive step so a
    # duplicate `add` costs no document fetch and — more to the point — sends no Save Page Now
    # request to archive.org for a URL that is already pinned. It shares duplicate_of() with the
    # authoritative check below, so it is a shortcut, not a second opinion.
    already = duplicate_of(xw.sources, spec.canonical_url, spec.point_in_time)
    if already is not None:
        return AddOutcome(
            status="refused",
            message=(
                f"{spec.canonical_url} at point_in_time {spec.point_in_time} is already pinned "
                f"as {already}"
            ),
        )

    xr_id = next_source_id(data_dir)
    path = _sources_dir(data_dir) / f"{xr_id}.json"
    if path.exists():
        return AddOutcome(status="refused", message=f"refusing to overwrite {path}")

    source = pin(spec, next_id=xr_id, fetch=fetch, blob_dir=blob_dir)

    # The other duplicate rule: the same citation at the same drift value is the same document,
    # however its URL and point_in_time happen to read. It cannot join the early exit above,
    # because drift_value is only known once pin() has fetched. It must still run BEFORE the
    # archive step: a refused pin should not cost a Save Page Now capture, which is slow,
    # rate-limited, and leaves a public artifact behind for a pin that was never written.
    #
    # --supersedes is applied first, because naming a pin is the whole override — it makes that pin
    # not live, so the collision disappears. Asking over "every existing source plus this one" is
    # the same question check_source_invariants answers below, which is why the shortcut cannot
    # disagree with the authority; a hit on the new record's own id just means nothing else holds
    # this document.
    candidate = source.model_copy(update={"supersedes": supersedes})
    artifact = candidate.artifact
    held = same_document_of(
        {**xw.sources, candidate.xr_id: candidate},
        candidate.citation,
        artifact.drift_key,
        artifact.drift_value,
    )
    if held is not None and held != candidate.xr_id:
        return AddOutcome(
            status="refused",
            message=(
                f"{candidate.citation} with {artifact.drift_key} {artifact.drift_value} is already "
                f"pinned as {held} and the document has not changed; pass --supersedes {held} to "
                "chain a new pin anyway"
            ),
        )

    archives: list[ArchiveCopy] = []
    if archive:
        # The hash goes with the request so `archive()` can recognise a capture that already
        # holds these exact bytes and skip asking for a new one.
        result = archive_fn(source.canonical_url, expected_sha256=source.artifact.sha256)
        if not isinstance(result, ArchiveCopy):
            # --archive is a requirement, not a courtesy: the caller asked for a recoverable pin
            # and we could not make one, so there is nothing worth writing. What differs now is
            # that the operator is told WHICH failure it was — a transient 5xx reads differently
            # from a url Wayback will not take, and only one of them is worth retrying.
            #
            # A bare None is still accepted, from an injected fake that has no reason to give,
            # and keeps the wording this refusal has always had.
            reason = (
                result.reason
                if isinstance(result, ArchiveFailure)
                else f"no capture returned for {source.canonical_url}; nothing written"
            )
            return AddOutcome(status="archive_failed", message=f"archive step failed: {reason}")
        archives = [result]
    # pin() builds the document facts; the crosswalk facts (who cites it, what it replaces) are the
    # caller's. Re-validate the assembled node rather than trusting model_copy, which skips it.
    source = Source.model_validate(
        source.model_copy(
            update={
                "archives": archives,
                "cited_in": cited_in or [],
                "supersedes": supersedes,
                "notes": notes,
            }
        ).model_dump()
    )

    # The read path is the authority. Run the would-be record through exactly the invariants
    # Crosswalk applies on load — against every existing source plus this one — so `add` can never
    # leave behind a file that the next validate/check/ledger refuses to load. This is also where
    # a duplicate (canonical_url, point_in_time) is caught: one rule, one implementation, one
    # message, rather than a second copy of the check that can drift from the first.
    try:
        check_source_invariants({**xw.sources, source.xr_id: source}, xw.nodes)
    except ValueError as exc:
        return AddOutcome(status="refused", message=str(exc))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return AddOutcome(
        status="written",
        message=f"wrote {path}",
        source=source,
        path=path,
        ledger=to_ledger_markdown(source),
    )


def archive_source(
    xr_id: str,
    *,
    data_dir: Path,
    archive_fn: ArchiveFn = archive,
) -> AddOutcome:
    """Archive a pin that has none, after the fact. The other half of `--archive`.

    `add --archive` is all-or-nothing: the capture has to succeed or the pin is not written at all.
    That is right when the archive is a requirement — a `uscode` pin without one drifts straight to
    "DRIFT unrecoverable" — but it means a Wayback outage can cost a good fetch of a document that
    only wants an archive as a matter of practice. This is the other order: pin now, archive when
    the service is up, without re-fetching the document or re-deciding anything about it.

    It goes through the SAME `archive_fn` the CLI hands `add_source`, so the reuse-or-capture path
    is one implementation: an existing capture whose bytes hash to this artifact is adopted without
    asking Save Page Now for anything, exactly as it would be at pin time.

    Only the `archives` field is rewritten. Everything else in the file is left as the bytes it
    already was — this is an amendment to one fact about a record, not a re-serialisation of it,
    and a command that quietly reformatted a record while adding an archive would make every such
    run unreviewable.
    """
    xw = Crosswalk(data_dir)
    source = xw.sources.get(xr_id)
    if source is None:
        return AddOutcome(status="refused", message=f"unknown source {xr_id!r}")
    if source.archives:
        held = source.archives[0]
        return AddOutcome(
            status="refused",
            message=f"{xr_id} already has an archive copy: {held.url}",
        )

    result = archive_fn(source.canonical_url, expected_sha256=source.artifact.sha256)
    if not isinstance(result, ArchiveCopy):
        reason = (
            result.reason
            if isinstance(result, ArchiveFailure)
            else f"no capture returned for {source.canonical_url}; nothing written"
        )
        return AddOutcome(status="archive_failed", message=f"archive step failed: {reason}")

    updated = Source.model_validate(source.model_copy(update={"archives": [result]}).model_dump())
    # The read path is the authority here too: an archive can make a record invalid (a cited
    # source is REQUIRED to carry one, and the rules about what an archive may be live in the
    # same place), so the amended record goes through the same gate `add` uses before it is
    # written, against every existing source with this one replaced.
    try:
        check_source_invariants({**xw.sources, xr_id: updated}, xw.nodes)
    except ValueError as exc:
        return AddOutcome(status="refused", message=str(exc))

    path = _sources_dir(data_dir) / f"{xr_id}.json"
    # Read, touch one key, write. Not `model_dump_json` of the whole node: that would rewrite
    # every field and turn a one-fact amendment into a diff nobody can read.
    record = json.loads(path.read_text(encoding="utf-8"))
    record["archives"] = [result.model_dump(mode="json")]
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return AddOutcome(
        status="written",
        message=f"archived {xr_id}: {result.url}",
        source=updated,
        path=path,
        ledger=to_ledger_markdown(updated),
    )


def note_source(xr_id: str, text: str, *, data_dir: Path) -> AddOutcome:
    """Set a pin's `notes`, offline. The same one-field amendment `archive_source` makes.

    `notes` is a plain-language label — what the document IS in the matter, which the status page
    prints ahead of the title — and it is the one field of a record that is ours rather than a fact
    read off the document, so it is the one field worth being able to restate without re-pinning.
    Nothing is fetched and nothing else in the file is touched: read, replace one key, write.
    """
    text = text.strip()
    if not text:
        return AddOutcome(status="refused", message="a note must not be empty")
    xw = Crosswalk(data_dir)
    source = xw.sources.get(xr_id)
    if source is None:
        return AddOutcome(status="refused", message=f"unknown source {xr_id!r}")

    updated = Source.model_validate(source.model_copy(update={"notes": text}).model_dump())
    try:
        check_source_invariants({**xw.sources, xr_id: updated}, xw.nodes)
    except ValueError as exc:
        return AddOutcome(status="refused", message=str(exc))

    path = _sources_dir(data_dir) / f"{xr_id}.json"
    # Read, touch one key, write — as `archive_source` does, and for the same reason.
    record = json.loads(path.read_text(encoding="utf-8"))
    record["notes"] = text
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return AddOutcome(
        status="written",
        message=f"noted {xr_id}: {text}",
        source=updated,
        path=path,
    )


def _cmd_add(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    """argparse in, exit code out. The guards and the write live in `add_source`."""
    from . import fetchers

    # Refused before any network call: the load path requires a cited source to carry an archive,
    # so this combination could only ever produce a record that cannot be read back. --cited-in
    # does NOT imply --archive — archiving is a separate act with its own failure mode, and
    # silently performing it on the caller's behalf would hide that.
    #
    # This and the next refusal stay here rather than in add_source because both are decided from
    # the flags alone, before there is a spec to hand over — and because both name a flag, which
    # is a fact about the command line and not about pinning.
    if args.cited_in and not args.archive:
        print("cited sources must carry an archive copy; pass --archive", file=sys.stderr)
        return 1

    # Same shape, fetcher's own requirement: where the canonical URL cannot reproduce the pinned
    # document once it moves, the archive copy is the only thing that can, so the pin is refused
    # without one. Also before any network call.
    if fetchers.requires_archive(args.fetcher) and not args.archive:
        print(f"{args.fetcher} pins must carry an archive copy; pass --archive", file=sys.stderr)
        return 1

    module = fetchers.get(args.fetcher)
    spec = module.spec_from_args(args, fetch=fetch)

    outcome = add_source(
        spec,
        data_dir=args.data_dir,
        archive=args.archive,
        supersedes=args.supersedes,
        cited_in=args.cited_in,
        notes=args.notes,
        blob_dir=args.blob_dir,
        fetch=fetch,
        archive_fn=archive_fn,
    )
    if outcome.status != "written":
        print(outcome.message, file=sys.stderr)
        return 1
    print(outcome.message)
    print(outcome.ledger)
    return 0


def _cmd_archive(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    """argparse in, exit code out. The guards and the write live in `archive_source`."""
    del fetch  # the document is not re-fetched: this amends a record, it does not re-pin one
    outcome = archive_source(args.xr_id, data_dir=args.data_dir, archive_fn=archive_fn)
    if outcome.status != "written":
        print(outcome.message, file=sys.stderr)
        return 1
    print(outcome.message)
    print(outcome.ledger)
    return 0


def _cmd_note(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    """argparse in, exit code out. The guards and the write live in `note_source`."""
    del fetch, archive_fn  # offline: a label is restated, the document is not touched
    outcome = note_source(args.xr_id, args.text, data_dir=args.data_dir)
    if outcome.status != "written":
        print(outcome.message, file=sys.stderr)
        return 1
    print(outcome.message)
    return 0


def _cmd_check(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    del archive_fn  # check never archives
    xw = Crosswalk(args.data_dir)
    if args.only:
        source = xw.sources.get(args.only)
        if source is None:
            print(f"unknown source {args.only!r}", file=sys.stderr)
            return 1
        targets: Iterable[Source] = [source]
    elif args.all:
        # Literally every record on disk, superseded pins and `merged_into` losers alike: --all is
        # for asking whether the documents behind the whole history are still there, which is a
        # different question from whether what we stand behind today still matches.
        targets = sorted(xw.sources.values(), key=lambda s: s.xr_id)
    else:
        targets = _live_pins(xw)

    reports = [check(s, fetch=fetch) for s in targets]
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in reports], indent=2))
    else:
        for r in reports:
            label = r.status.upper()
            if r.status == "drift" and not r.archived:
                label = "DRIFT unrecoverable (no archive)"
            line = f"{label:<11} {r.xr_id}  {r.citation}"
            if r.detail:
                line += f"\n            {r.detail}"
            elif r.status == "drift":
                line += f"\n            expected {r.expected}, got {r.actual}"
            print(line)
    return max((_EXIT_CODES[r.status] for r in reports), default=0, key=_EXIT_RANK.get)


def _cmd_search(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    """Look a document up and print the `add` that would pin it. Reads; never writes.

    Deliberately does NOT touch `data/`: search answers "what is this document called and what is
    its id", which is a question about the publisher's index, not about what this repo holds.
    """
    del archive_fn  # search never archives
    from . import fetchers

    doc_type = args.doc_type or fetchers.search_types(args.fetcher)[0]
    try:
        hits = fetchers.search(
            args.fetcher, args.query, doc_type=doc_type, fetch=fetch, env=os.environ
        )
    except MissingKey as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([asdict(h) for h in hits], indent=2))
    else:
        for hit in hits:
            print(
                "  ".join(
                    (
                        hit.identifier or "-",
                        hit.date or "-",
                        hit.label or "-",
                        hit.docket_or_number or "-",
                        hit.citation or "-",
                    )
                )
            )
    if not hits:
        print(f"no {doc_type} matched {args.query!r}", file=sys.stderr)
        return 1
    # The round trip, and the reason this subcommand exists: the last line is the command that
    # pins the first hit, ready to paste. A hit with no pinnable identifier says so instead of
    # printing a command that would pin the wrong document.
    command = fetchers.add_command(args.fetcher, hits[0], doc_type)
    print()
    print(command or f"{len(hits)} hit(s); a {doc_type} hit is not pinnable directly")
    return 0


def _cmd_ledger(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    del fetch, archive_fn  # offline command; it only reads what is already pinned
    xw = Crosswalk(args.data_dir)
    sources = sorted(xw.sources.values(), key=lambda s: s.xr_id)
    if args.since is not None:
        sources = [s for s in sources if s.artifact.fetched_at.date() >= args.since]
    for source in sources:
        print(to_manifest_line(source) if args.format == "manifest" else to_ledger_markdown(source))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    from . import fetchers

    parser = argparse.ArgumentParser(prog="registers_crosswalk.pin", description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=DATA_DIR, help="crosswalk data/ dir (default: this repo's)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="pin a document and mint its xr_src node")
    add.set_defaults(handler=_cmd_add)
    add_sub = add.add_subparsers(dest="fetcher", required=True)
    for name in fetchers.NAMES:
        module = fetchers.get(name)
        fetcher_parser = add_sub.add_parser(name, help=module.HELP)
        module.add_arguments(fetcher_parser)
        fetcher_parser.add_argument(
            "--blob-dir",
            type=Path,
            default=None,
            help="write the bytes to <dir>/<sha256><ext>; must be OUTSIDE this repo",
        )
        fetcher_parser.add_argument(
            "--archive", action="store_true", help="also request a Wayback capture"
        )
        fetcher_parser.add_argument(
            "--supersedes", type=_src_id_arg, default=None, metavar="xr_src_NNNN"
        )
        fetcher_parser.add_argument(
            "--notes",
            default=None,
            metavar="TEXT",
            help="a plain-language label; the status page prints it ahead of the title",
        )
        fetcher_parser.add_argument(
            "--cited-in",
            type=_cited_in_arg,
            action="append",
            default=[],
            metavar="register:local_id",
            help="a register record that cites this document (repeatable)",
        )

    archive_parser = sub.add_parser(
        "archive", help="add an archive copy to a pin that has none (does not re-fetch)"
    )
    archive_parser.set_defaults(handler=_cmd_archive)
    archive_parser.add_argument("xr_id", type=_src_id_arg, metavar="xr_src_NNNN")

    note_parser = sub.add_parser(
        "note", help="set a pin's notes label (offline; nothing else moves)"
    )
    note_parser.set_defaults(handler=_cmd_note)
    note_parser.add_argument("xr_id", type=_src_id_arg, metavar="xr_src_NNNN")
    note_parser.add_argument("text", help="the plain-language label; replaces any existing one")

    check_parser = sub.add_parser("check", help="re-fetch pinned documents and report drift")
    check_parser.set_defaults(handler=_cmd_check)
    check_parser.add_argument("--only", default=None, metavar="xr_src_NNNN")
    check_parser.add_argument(
        "--all",
        action="store_true",
        help="check superseded pins too, not just the live ones",
    )
    check_parser.add_argument("--json", action="store_true")

    search_parser = sub.add_parser("search", help="find a document's identifier by name or number")
    search_parser.set_defaults(handler=_cmd_search)
    search_parser.add_argument("fetcher", choices=fetchers.SEARCHABLE)
    search_parser.add_argument("query", help="case name, docket number, respondent or matter name")
    search_parser.add_argument(
        "--type",
        dest="doc_type",
        default=None,
        metavar="KIND",
        help="; ".join(f"{n}: {'|'.join(fetchers.search_types(n))}" for n in fetchers.SEARCHABLE),
    )
    search_parser.add_argument("--json", action="store_true")

    ledger = sub.add_parser("ledger", help="emit entries for the New Gray ledgers")
    ledger.set_defaults(handler=_cmd_ledger)
    ledger.add_argument("--format", choices=("md", "manifest"), default="md")
    ledger.add_argument(
        "--since",
        type=date.fromisoformat,
        default=None,
        help="only pins fetched on/after YYYY-MM-DD",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    fetch: FetchFn = default_fetch,
    archive_fn: ArchiveFn = archive,
) -> int:
    args = _build_parser().parse_args(argv)
    return args.handler(args, fetch, archive_fn)


if __name__ == "__main__":
    sys.exit(main())
