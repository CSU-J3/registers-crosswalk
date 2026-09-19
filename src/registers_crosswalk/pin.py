"""Pin a document: fetch it once, hash it, and mint a source node that POINTS at it.

Nothing here ever writes a fetched document's bytes into this repo. `pin()` refuses a `blob_dir`
that resolves inside the repo root, and a `Source` holds only facts about the document as an object
(where it lives, when it was fetched, what it hashes to, who published it, when). Blobs live in the
consuming project or in the archive copy; the artifact hash verifies either.

CLI: `python -m registers_crosswalk.pin {add,check,ledger}`.
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
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    normalize_citation,
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
# The CLI's archiving step, injected for the same reason: tests stay offline.
ArchiveFn = Callable[[str], "ArchiveCopy | None"]

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
_WAYBACK_TS = re.compile(r"/web/(\d{14})/")
# How long to wait between polls of an SPN2 job, and the shape of the wait. Injected in tests so
# they neither sleep nor reach the network.
_SPN2_POLL_SECONDS = 5.0


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


def _archive_spn2(
    url: str,
    auth: Mapping[str, str],
    *,
    json_fn: JsonFn,
    sleep_fn: SleepFn,
    timeout: float,
) -> ArchiveCopy | None:
    """Capture `url` through Wayback's SPN2 job interface: POST the url, then poll the job.

    Raises nothing of its own; `archive()` owns the best-effort contract. `timeout` is the whole
    poll budget, not a per-request one — a job that is still pending when it runs out is a failed
    capture, the same as an error, because the caller has a pin to write or refuse now.
    """
    headers = {**auth, "Accept": "application/json"}
    started = json_fn(
        _WAYBACK_SPN2,
        {**headers, "Content-Type": "application/x-www-form-urlencoded"},
        urlencode({"url": url}).encode("utf-8"),
    )
    job_id = started.get("job_id")
    if not job_id:
        # SPN2 refuses some urls outright (robots, a host it will not fetch) and says so here
        # instead of handing back a job.
        return None
    waited = 0.0
    while waited < timeout:
        sleep_fn(_SPN2_POLL_SECONDS)
        waited += _SPN2_POLL_SECONDS
        state = json_fn(f"{_WAYBACK_SPN2_STATUS}{job_id}", headers, None)
        status = state.get("status")
        if status == "pending":
            continue
        if status != "success":
            # "error", or a status this code does not know: either way there is no capture.
            return None
        timestamp = state.get("timestamp")
        if not timestamp:
            return None
        return _capture_from_timestamp(str(state.get("original_url") or url), str(timestamp))
    return None


def archive(
    url: str,
    *,
    headers_fn: HeadersFn | None = None,
    json_fn: JsonFn | None = None,
    sleep_fn: SleepFn | None = None,
    timeout: float = 90,
    env: Mapping[str, str] | None = None,
) -> ArchiveCopy | None:
    """Ask the Wayback Machine to capture `url`; return the capture, or None.

    Best-effort by design: archiving is a courtesy copy, not the pin. Any failure — service down,
    rate-limited, URL refused — returns None and never raises, so a slow archive can't cost you a
    good fetch.

    Two interfaces, chosen by whether WAYBACK_ACCESS_KEY/WAYBACK_SECRET_KEY are both set. With
    keys, the documented SPN2 job interface: POST the url, poll the job, read `timestamp` from the
    success reply. Without them, the anonymous synchronous save, which answers in
    `Content-Location`. The keyed path is not merely the anonymous one with a header: sending the
    Authorization header on that GET was tried against the uscode section URL on 2026-09-19 and
    answered 500, as the anonymous GET had the day before.

    TODO: Perma.cc as a second service (needs an API key and a registrar account).
    """
    env = os.environ if env is None else env
    auth: dict[str, str] = {}
    access, secret = env.get("WAYBACK_ACCESS_KEY"), env.get("WAYBACK_SECRET_KEY")
    if access and secret:
        auth["Authorization"] = f"LOW {access}:{secret}"
    try:
        if auth:
            return _archive_spn2(
                url,
                auth,
                json_fn=json_fn or functools.partial(_json_call, timeout=timeout),
                sleep_fn=sleep_fn or time.sleep,
                timeout=timeout,
            )
        fn = headers_fn or functools.partial(_response_headers, timeout=timeout)
        headers = fn(f"{_WAYBACK_SAVE}{url}", None)
        location = _header(headers, "Content-Location")
        if not location:
            return None
        captured_at = None
        m = _WAYBACK_TS.search(location)
        if m is not None:
            captured_at = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        return ArchiveCopy(
            service="wayback",
            url=urljoin("https://web.archive.org", location),
            captured_at=captured_at,
        )
    except Exception:
        return None


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
    retrieved = f"Retrieved {source.artifact.fetched_at:%Y-%m-%d}"
    # Fetchers with a point in time put it in the title too ("11 CFR Part 114, as of 2026-09-14"),
    # so appending it unconditionally printed the same date twice on one line.
    if source.point_in_time is not None and source.point_in_time.isoformat() not in source.title:
        retrieved += f", as of {source.point_in_time.isoformat()}"
    archive_url = source.archives[0].url if source.archives else "pending"
    return (
        f"- **{head} ({source.grade.code()})** — {source.title}. "
        f"{retrieved}; sha256 {source.artifact.sha256[:16]}…; {source.xr_id}.\n"
        f"  {source.canonical_url}\n"
        f"  Archive: {archive_url}"
    )


def to_manifest_line(source: Source) -> str:
    """One line in `sha256sum` format, for verifying a consuming project's blob directory."""
    name = f"{_slug(source.citation)}{_extension(source.artifact.media_type)}"
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


def _latest_per_citation(xw: Crosswalk) -> list[Source]:
    """One pin per citation: the one resolve_source() would hand a consumer today."""
    wanted: dict[str, Source] = {}
    for source in xw.sources.values():
        if source.merged_into is not None:
            continue
        key = normalize_citation(source.citation)
        if key in wanted:
            continue
        latest = xw.resolve_source(source.citation)
        if latest is not None:
            wanted[key] = latest
    return sorted(wanted.values(), key=lambda s: s.xr_id)


def _cmd_add(args: argparse.Namespace, fetch: FetchFn, archive_fn: ArchiveFn) -> int:
    from . import fetchers

    data_dir: Path = args.data_dir
    # Refused before any network call: the load path requires a cited source to carry an archive,
    # so this combination could only ever produce a record that cannot be read back. --cited-in
    # does NOT imply --archive — archiving is a separate act with its own failure mode, and
    # silently performing it on the caller's behalf would hide that.
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
    xw = Crosswalk(data_dir)

    # Cheap early exit on the one refusal we can reach without doing any work: re-pinning a
    # document version we already hold. Placed before pin() and before the archive step so a
    # duplicate `add` costs no document fetch and — more to the point — sends no Save Page Now
    # request to archive.org for a URL that is already pinned. It shares duplicate_of() with the
    # authoritative check below, so it is a shortcut, not a second opinion.
    already = duplicate_of(xw.sources, spec.canonical_url, spec.point_in_time)
    if already is not None:
        print(
            f"{spec.canonical_url} at point_in_time {spec.point_in_time} is already pinned "
            f"as {already}",
            file=sys.stderr,
        )
        return 1

    xr_id = next_source_id(data_dir)
    path = _sources_dir(data_dir) / f"{xr_id}.json"
    if path.exists():
        print(f"refusing to overwrite {path}", file=sys.stderr)
        return 1

    source = pin(spec, next_id=xr_id, fetch=fetch, blob_dir=args.blob_dir)

    archives: list[ArchiveCopy] = []
    if args.archive:
        copy = archive_fn(source.canonical_url)
        if copy is None:
            # --archive is a requirement, not a courtesy: the caller asked for a recoverable pin
            # and we could not make one, so there is nothing worth writing.
            print(
                f"archive step failed: no capture returned for {source.canonical_url}; "
                "nothing written",
                file=sys.stderr,
            )
            return 1
        archives = [copy]
    # pin() builds the document facts; the crosswalk facts (who cites it, what it replaces) are the
    # caller's. Re-validate the assembled node rather than trusting model_copy, which skips it.
    source = Source.model_validate(
        source.model_copy(
            update={"archives": archives, "cited_in": args.cited_in, "supersedes": args.supersedes}
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
        print(str(exc), file=sys.stderr)
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    print(to_ledger_markdown(source))
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
        targets = sorted(xw.sources.values(), key=lambda s: s.xr_id)
    else:
        targets = _latest_per_citation(xw)

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
            "--cited-in",
            type=_cited_in_arg,
            action="append",
            default=[],
            metavar="register:local_id",
            help="a register record that cites this document (repeatable)",
        )

    check_parser = sub.add_parser("check", help="re-fetch pinned documents and report drift")
    check_parser.set_defaults(handler=_cmd_check)
    check_parser.add_argument("--only", default=None, metavar="xr_src_NNNN")
    check_parser.add_argument(
        "--all", action="store_true", help="check every pin, not just the latest per citation"
    )
    check_parser.add_argument("--json", action="store_true")

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
