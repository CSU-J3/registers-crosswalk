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
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

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
from .registry import DATA_DIR, Crosswalk, normalize_citation

REPO_ROOT = Path(__file__).resolve().parents[2]

# A fetch is always injectable so tests never touch the network (working rule 5).
# (url, headers) -> (body, media_type)
FetchFn = Callable[[str, Mapping[str, str] | None], tuple[bytes, str]]
# archive() needs RESPONSE HEADERS rather than a body (Wayback answers in Content-Location), so it
# gets its own injection point of a different shape. Same rule, different signature.
HeadersFn = Callable[[str, Mapping[str, str] | None], Mapping[str, str]]

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
_WAYBACK_TS = re.compile(r"/web/(\d{14})/")


def _response_headers(
    url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 90
) -> Mapping[str, str]:
    req = urllib.request.Request(url, headers={**_BASE_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return dict(resp.headers)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return value
    return None


def archive(
    url: str,
    *,
    headers_fn: HeadersFn | None = None,
    timeout: float = 90,
    env: Mapping[str, str] | None = None,
) -> ArchiveCopy | None:
    """Ask the Wayback Machine to capture `url`; return the capture, or None.

    Best-effort by design: archiving is a courtesy copy, not the pin. Any failure — service down,
    rate-limited, URL refused — returns None and never raises, so a slow archive can't cost you a
    good fetch. With WAYBACK_ACCESS_KEY/WAYBACK_SECRET_KEY set it uses the authenticated SPN2
    endpoint, which is rate-limited far less aggressively.

    TODO: Perma.cc as a second service (needs an API key and a registrar account).
    """
    env = os.environ if env is None else env
    fn = headers_fn or functools.partial(_response_headers, timeout=timeout)
    auth: dict[str, str] = {}
    access, secret = env.get("WAYBACK_ACCESS_KEY"), env.get("WAYBACK_SECRET_KEY")
    if access and secret:
        auth["Authorization"] = f"LOW {access}:{secret}"
    try:
        headers = fn(f"{_WAYBACK_SAVE}{url}", auth or None)
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
    # currency line, an API changed shape). The fetch worked, so it is a content problem: exit 1.
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
    if source.point_in_time is not None:
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


def _cmd_add(args: argparse.Namespace, fetch: FetchFn) -> int:
    from . import fetchers

    data_dir: Path = args.data_dir
    module = fetchers.get(args.fetcher)
    spec = module.spec_from_args(args, fetch=fetch)

    xw = Crosswalk(data_dir)
    for existing in xw.sources.values():
        if (existing.canonical_url, existing.point_in_time) == (
            spec.canonical_url,
            spec.point_in_time,
        ):
            print(
                f"refusing: {spec.canonical_url} at point_in_time {spec.point_in_time} is "
                f"already pinned as {existing.xr_id}",
                file=sys.stderr,
            )
            return 1

    xr_id = next_source_id(data_dir)
    path = _sources_dir(data_dir) / f"{xr_id}.json"
    if path.exists():
        print(f"refusing to overwrite {path}", file=sys.stderr)
        return 1

    source = pin(spec, next_id=xr_id, fetch=fetch, blob_dir=args.blob_dir)
    archives = []
    if args.archive:
        copy = archive(source.canonical_url)
        if copy is None:
            print("archive: no capture (the pin does not depend on it)", file=sys.stderr)
        else:
            archives = [copy]
    # pin() builds the document facts; the crosswalk facts (who cites it, what it replaces) are the
    # caller's. Re-validate the assembled node rather than trusting model_copy, which skips it.
    source = Source.model_validate(
        source.model_copy(
            update={"archives": archives, "cited_in": args.cited_in, "supersedes": args.supersedes}
        ).model_dump()
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    print(to_ledger_markdown(source))
    return 0


def _cmd_check(args: argparse.Namespace, fetch: FetchFn) -> int:
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


def _cmd_ledger(args: argparse.Namespace, fetch: FetchFn) -> int:
    del fetch  # offline command; it only reads what is already pinned
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


def main(argv: list[str] | None = None, *, fetch: FetchFn = default_fetch) -> int:
    args = _build_parser().parse_args(argv)
    return args.handler(args, fetch)


if __name__ == "__main__":
    sys.exit(main())
