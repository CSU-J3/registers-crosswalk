"""The pinning console: one local page over `search` and `add`, served on the loopback interface.

Type a case name, see what document would be pinned and at what grade, pin it. This is the WRITE
side of the register, so it runs on the operator's machine behind the operator's keys and is never
published. The status page (`registers_crosswalk.status`) stays the read side, and the two are the
only pages this project has.

Nothing here reimplements a rule. Search goes through `fetchers.search`, resolving goes through the
fetcher's own `spec_from_args`, and pinning goes through `pin.add_source` — the same function
`pin add` calls, with every guard in place and every refusal reported in the CLI's own words. If a
guard exists in `add`, the console reaches it; it cannot route around one.

Keys: this module reads `.env` from the repo root ITSELF, into a private mapping it passes as `env`
to the calls that need one. `os.environ` is never modified, and no value from `.env` is logged,
printed, or rendered into the page — only the NAME of the variable and whether it is set.

CLI: `python -m registers_crosswalk.console [--data-dir DIR] [--port N] [--no-browser]`.
"""

from __future__ import annotations

import argparse
import functools
import html
import json
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

from . import fetchers
from .pin import (
    AddOutcome,
    FetchFn,
    MissingKey,
    PinSpec,
    add_source,
    archive,
    default_fetch,
    to_manifest_line,
)
from .registry import DATA_DIR, Crosswalk, normalize_citation

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# --------------------------------------------------------------------------- pacing
#
# CourtListener throttles a new free account to 5 API requests a minute, 50 an hour, 125 a day. A
# single resolve spends four. The console is a page with buttons on it, so the operator can click
# faster than the terminal ever made them type — the ceiling has to be enforced by the code rather
# than by the operator's restraint. 12 seconds between calls is 5 a minute exactly.
#
# Only the API host is paced. `storage.courtlistener.com` serves the pinned PDF, is not the API,
# and is not counted against the quota — pacing it would slow the one fetch that matters for no
# reason.
PACING: dict[str, float] = {"www.courtlistener.com": 12.0}

# Display only, both of them. The label is what the page calls a paced host; the call count is how
# many API calls that fetcher's `spec()` is expected to make, so a wait can be reported as "call 2
# of 4" rather than "call 2". Neither is load-bearing: an unknown host falls back to its hostname,
# an unknown or exceeded count drops the "of N", and pacing itself is driven entirely by PACING.
PACED_LABEL: dict[str, str] = {"www.courtlistener.com": "CourtListener"}
SPEC_CALLS: dict[str, int] = {"courtlistener": 4}


def paced(
    fetch: FetchFn = default_fetch,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    pacing: Mapping[str, float] = PACING,
    on_call: Callable[[str, float], None] | None = None,
) -> FetchFn:
    """Wrap `fetch` so calls to a paced host are spaced by at least the table's interval.

    The clock and sleep are injected so the test can drive this with a fake clock and assert the
    spacing without spending the wall-clock time it describes. A lock, because the server is
    threaded and two browser tabs would otherwise both pass the check and fire together.
    """
    last: dict[str, float] = {}
    lock = threading.Lock()
    counts: dict[str, int] = {}

    def wrapper(url: str, headers: Mapping[str, str] | None = None) -> tuple[bytes, str]:
        host = urlsplit(url).hostname or ""
        interval = pacing.get(host)
        with lock:
            counts[host] = counts.get(host, 0) + 1
            wait = 0.0
            now = clock()
            if interval is not None:
                previous = last.get(host)
                if previous is not None:
                    wait = max(0.0, interval - (now - previous))
            # Reported BEFORE the sleep, not after, because the whole point is to say what the
            # page is waiting for while it is still waiting. A caller that reports after the
            # sleep describes a wait that has already ended, which is no better than silence.
            if on_call is not None:
                on_call(host, wait)
            if interval is not None:
                if wait > 0:
                    sleep(wait)
                    now = clock()
                last[host] = now
        return fetch(url, headers)

    wrapper.counts = counts  # type: ignore[attr-defined]
    return wrapper


# --------------------------------------------------------------------------- .env


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a `KEY=VALUE` file into a mapping. Never touches `os.environ`.

    Deliberately small and stdlib-only: blank lines and `#` comments skipped, `export ` prefix
    tolerated because that is how `docs/operations.md` tells the operator to write a file they also
    `source`, one pair of surrounding quotes stripped. A malformed line is skipped rather than
    raised on — a typo in a key file should not stop the console from starting and telling the
    operator which key is missing.

    A missing file is an empty mapping, not an error: the keyless fetchers work without one.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


# --------------------------------------------------------------------------- state


UNVERIFIED_REFUSAL = (
    "fetcher {name} is unverified; its first live run goes through the terminal, "
    "per docs/operations.md"
)


def _git_state(data_dir: Path) -> dict[str, Any]:
    """The branch and what is uncommitted under `data/sources/`, or `{}` when git isn't available.

    Read-only: `git status --porcelain` and `git branch --show-current`, nothing else. The console
    writes records; git is how they leave this machine, and the footer exists so a pin made and
    then forgotten is visible on the page that made it.
    """

    def run(args: list[str]) -> str | None:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
                ["git", *args],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    branch = run(["branch", "--show-current"])
    if branch is None:
        return {}
    try:
        rel = data_dir.resolve().relative_to(REPO_ROOT)
    except ValueError:
        # A tmp data dir in a test, or an operator pointing elsewhere: git has nothing to say
        # about paths outside the repo, so don't ask it.
        return {"branch": branch.strip(), "pending": []}
    status = run(["status", "--porcelain", "--", str(rel / "sources")]) or ""
    pending = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    return {"branch": branch.strip(), "pending": pending}


def state(data_dir: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Everything the page needs to draw itself, and nothing that could leak a key.

    `key_present` is a bool and `env_key` is a NAME. No value from `env` reaches this dict, which
    is the whole reason the console parses `.env` into a private mapping instead of exporting it.
    """
    modules = []
    for name in fetchers.NAMES:
        module = fetchers.get(name)
        env_key = getattr(module, "ENV_KEY", None)
        verified_at = getattr(module, "VERIFIED_AT", None)
        modules.append(
            {
                "name": name,
                "help": module.HELP,
                "verified": bool(getattr(module, "VERIFIED", False)),
                "verified_at": verified_at.isoformat() if verified_at else None,
                "searchable": name in fetchers.SEARCHABLE,
                "search_types": list(fetchers.search_types(name)),
                "requires_archive": fetchers.requires_archive(name),
                "env_key": env_key,
                "key_present": bool(env.get(env_key)) if env_key else None,
            }
        )
    xw = Crosswalk(data_dir)
    pins = [
        {"xr_id": s.xr_id, "citation": s.citation, "title": s.title}
        for s in sorted(xw.sources.values(), key=lambda s: s.xr_id)
    ]
    return {"fetchers": modules, "pins": pins, "git": _git_state(data_dir)}


def _form_fields(name: str) -> list[dict[str, Any]]:
    """The fetcher's own argparse actions, as fields for the page.

    Built from `add_arguments` rather than hand-listed per fetcher, so the form and the terminal
    cannot diverge: a new flag on a fetcher appears on the page with its own help text, and a
    renamed one renames itself here.
    """
    parser = argparse.ArgumentParser(add_help=False)
    fetchers.get(name).add_arguments(parser)
    fields = []
    for action in parser._actions:  # noqa: SLF001 - argparse exposes its actions no other way
        if not action.option_strings:
            continue
        fields.append(
            {
                "dest": action.dest,
                "flag": action.option_strings[-1],
                "help": action.help or "",
                "required": bool(action.required),
            }
        )
    return fields


# --------------------------------------------------------------------------- the server


class Console:
    """The state behind the handler: data dir, keys, the fetch to use, and resolved specs.

    A class rather than module globals so a test can stand one up per temp directory, and so the
    resolved `PinSpec`s live for exactly as long as the server does. They are held in memory and
    never written anywhere: a spec is the answer to "what would be pinned", and re-deriving it
    would spend four more API calls to learn what we already know.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        env: Mapping[str, str],
        fetch: FetchFn,
        archive_fn: Callable[..., Any] | None = None,
        pacing: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.data_dir = data_dir
        self.env = env
        # `pacing` is opt-in rather than on by default so a test can hand in a fake fetch and get
        # it back unwrapped; `main` passes PACING. Wrapping here rather than in `main` is what
        # lets the wait be reported into this console's own progress record.
        self._progress: dict[str, dict[str, Any]] = {}
        self._local = threading.local()
        self.fetch = (
            paced(fetch, clock=clock, sleep=sleep, pacing=pacing, on_call=self.note_call)
            if pacing
            else fetch
        )
        # `add_source` calls `archive_fn(url)` and nothing else, so an unbound `archive` would
        # take `env=None` and read `os.environ` — which is exactly what this module refuses to
        # put the keys in. Every console pin therefore took the ANONYMOUS Wayback path with the
        # operator's keys sitting unused in `.env`, and failed. Binding the private mapping here
        # is the whole fix, and the reason this default is computed rather than written in the
        # signature: it needs `self.env`.
        self.archive_fn = functools.partial(archive, env=env) if archive_fn is None else archive_fn
        self.token = secrets.token_urlsafe(32)
        self._specs: dict[str, PinSpec] = {}
        self._lock = threading.Lock()

    # -- progress -------------------------------------------------------------

    def note_call(self, host: str, wait: float) -> None:
        """One paced call is about to happen; record it against the resolve that caused it.

        A resolve is one blocking POST, so without this the page has nothing to say for the
        forty-odd seconds CourtListener's rate limit costs — it sat on "Resolving..." looking
        indistinguishable from a hang, which is the same complaint the uncaught fetch rejection
        drew. The resolve is identified by a thread-local, because ThreadingHTTPServer gives each
        request its own thread and two browser tabs must not write into one another's record.
        """
        progress_id = getattr(self._local, "progress_id", None)
        if progress_id is None:
            return
        with self._lock:
            record = self._progress.setdefault(progress_id, {"calls": 0, "fetcher": None})
            record["calls"] += 1
            expected = SPEC_CALLS.get(record.get("fetcher") or "")
            where = f"call {record['calls']}"
            if expected and record["calls"] <= expected:
                where += f" of {expected}"
            if wait > 0:
                label = PACED_LABEL.get(host, host)
                record["message"] = f"{where}, waiting {wait:.0f} s for {label}'s rate limit"
            else:
                record["message"] = where
            record["waiting"] = wait > 0

    def api_progress(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        progress_id = (query.get("id") or [""])[0]
        with self._lock:
            record = self._progress.get(progress_id)
        return 200, (dict(record) if record else {})

    # -- the endpoints, each returning (status, payload) -----------------------

    def api_state(self) -> tuple[int, dict[str, Any]]:
        return 200, state(self.data_dir, self.env)

    def api_search(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        name = (query.get("fetcher") or [""])[0]
        if name not in fetchers.SEARCHABLE:
            return 400, {"error": f"{name or '(none)'} has no search"}
        types = fetchers.search_types(name)
        doc_type = (query.get("type") or [types[0]])[0]
        if doc_type not in types:
            return 400, {"error": f"{name} has no {doc_type!r} search"}
        text = (query.get("q") or [""])[0].strip()
        if not text:
            return 400, {"error": "empty query"}
        # Searching an UNVERIFIED fetcher is allowed on purpose: a search reads somebody else's
        # index and writes nothing. It is `spec()` — the call that decides what would be pinned and
        # at what grade — that a first live run has to witness, and that is what resolve refuses.
        try:
            hits = fetchers.search(name, text, doc_type=doc_type, fetch=self.fetch, env=self.env)
        except MissingKey as exc:
            return 400, {"error": str(exc)}
        except (ValueError, OSError) as exc:
            return 502, {"error": f"{type(exc).__name__}: {exc}"}
        return 200, {
            "hits": [
                {**asdict(hit), "add_command": fetchers.add_command(name, hit, doc_type)}
                for hit in hits
            ]
        }

    def api_resolve(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        progress_id = str(body.get("progress_id") or "") or None
        if progress_id is not None:
            with self._lock:
                self._progress[progress_id] = {
                    "calls": 0,
                    "fetcher": str(body.get("fetcher") or ""),
                    "message": "starting",
                    "waiting": False,
                }
            self._local.progress_id = progress_id
        try:
            return self._resolve(body)
        finally:
            self._local.progress_id = None
            if progress_id is not None:
                with self._lock:
                    self._progress.pop(progress_id, None)

    def _resolve(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        name = str(body.get("fetcher") or "")
        try:
            module = fetchers.get(name)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        # Working rule 5. An unverified fetcher's spec() has never been run against the real
        # endpoint, and the console is not the place to find out what it does: the first live run
        # is a deliberate act at a terminal, with its output read, per docs/operations.md.
        if not getattr(module, "VERIFIED", False):
            return 409, {"error": UNVERIFIED_REFUSAL.format(name=name)}

        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        module.add_arguments(parser)
        argv: list[str] = []
        for field in _form_fields(name):
            value = body.get("args", {}).get(field["dest"])
            if value is None or str(value).strip() == "":
                continue
            argv += [field["flag"], str(value).strip()]
        try:
            ns = parser.parse_args(argv)
        except (argparse.ArgumentError, SystemExit) as exc:
            return 400, {"error": f"bad arguments: {exc}"}

        try:
            spec = module.spec_from_args(ns, fetch=self.fetch, env=self.env)
        except MissingKey as exc:
            return 400, {"error": str(exc)}
        except ValueError as exc:
            # The cluster has no pinnable document. Not an error in the console: it is the
            # answer, and the page says so where the Pin button would have been.
            return 200, {"pinnable": False, "message": str(exc)}
        except OSError as exc:
            return 502, {"error": f"{type(exc).__name__}: {exc}"}

        from .pin import duplicate_of

        xw = Crosswalk(self.data_dir)
        already = duplicate_of(xw.sources, spec.canonical_url, spec.point_in_time)
        resolve_id = secrets.token_urlsafe(12)
        with self._lock:
            self._specs[resolve_id] = spec
        return 200, {
            "pinnable": True,
            "resolve_id": resolve_id,
            "fetcher": name,
            "citation": spec.citation,
            "title": spec.title,
            "publisher": spec.publisher,
            "grade": spec.grade.code(),
            "canonical_url": spec.canonical_url,
            "point_in_time": spec.point_in_time.isoformat() if spec.point_in_time else None,
            "published_at": spec.published_at.isoformat() if spec.published_at else None,
            "drift_key": spec.drift_key,
            "requires_archive": fetchers.requires_archive(name),
            "already_pinned": already,
            "supersedes_candidates": [
                s.xr_id
                for s in sorted(xw.sources.values(), key=lambda s: s.xr_id)
                if normalize_citation(s.citation) == normalize_citation(spec.citation)
            ],
        }

    def api_pin(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        resolve_id = str(body.get("resolve_id") or "")
        with self._lock:
            spec = self._specs.get(resolve_id)
        if spec is None:
            return 400, {"error": "unknown or expired resolve_id; resolve again"}
        # The fetcher's own requirement outranks the checkbox. `add_source` does not re-check this
        # — `_cmd_add` refuses it from the flags before there is a spec — so the console enforces
        # it here, which is the one guard it owns rather than borrows. Forcing rather than refusing
        # because the operator's intent ("pin this") is unambiguous and the archive is not
        # optional; a refusal would only make them tick a box they were never offered a choice on.
        wants_archive = bool(body.get("archive"))
        if fetchers.requires_archive(spec.fetcher):
            wants_archive = True
        notes = body.get("notes") or None
        supersedes = body.get("supersedes") or None
        try:
            outcome = add_source(
                spec,
                data_dir=self.data_dir,
                archive=wants_archive,
                supersedes=supersedes,
                cited_in=[],
                notes=str(notes).strip() if notes else None,
                fetch=self.fetch,
                archive_fn=self.archive_fn,
            )
        except (ValueError, OSError) as exc:
            return 200, {"status": "refused", "message": f"{type(exc).__name__}: {exc}"}
        if outcome.status == "written":
            with self._lock:
                self._specs.pop(resolve_id, None)
        return 200, _outcome_payload(outcome)


def _outcome_payload(outcome: AddOutcome) -> dict[str, Any]:
    source = outcome.source
    return {
        "status": outcome.status,
        "message": outcome.message,
        "xr_id": source.xr_id if source else None,
        "path": str(outcome.path) if outcome.path else None,
        "ledger": outcome.ledger,
        "manifest": to_manifest_line(source) if source else None,
        "archived": bool(source.archives) if source else False,
    }


# --------------------------------------------------------------------------- HTTP


def make_handler(console: Console) -> type[BaseHTTPRequestHandler]:
    """The request handler, closed over one `Console`.

    Every `/api/` request carries the session token in `X-Console-Token`. The page has it because
    the server rendered it into the page; nothing else does. A page served from any other origin
    can still POST here — the browser will send the request — but it cannot read this token, so it
    cannot drive the console. That is the whole of the defence, and it is enough for a server bound
    to the loopback interface with one user on it.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "registers-crosswalk-console"

        def log_message(self, fmt: str, *args: Any) -> None:
            # The default logs every request line to stderr. A request line can carry a search
            # query, and a query is the operator's business; nothing about this server needs a
            # transcript of it. Silence by default, and never the headers, which carry the token.
            del fmt, args

        # -- plumbing ---------------------------------------------------------

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The page loads nothing off this machine or any other, so the policy is the
            # strictest one that still lets it work. `connect-src 'self'` is load-bearing and was
            # missing: `default-src 'none'` covers fetch(), so the browser blocked every call the
            # page makes to its own /api/ and the console did nothing at all. There is a test on
            # this header for that reason — the failure was silent everywhere but the Network tab.
            # `img-src 'self'` is here so the favicon request is not a console error on every load.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'self'; img-src 'self'; "
                "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "form-action 'none'; base-uri 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _authorised(self) -> bool:
            sent = self.headers.get("X-Console-Token", "")
            return secrets.compare_digest(sent, console.token)

        def _read_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > 64 * 1024:
                return None
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        # -- routes -----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own spelling
            parsed = urlparse(self.path)
            if parsed.path == "/":
                page = render_page(console.token, state(console.data_dir, console.env))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            # Browsers ask for this unprompted. Answering 204 rather than 404 keeps one red line
            # out of the console on every load; there is no icon to serve and none is wanted.
            if parsed.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if not parsed.path.startswith("/api/"):
                self._json(404, {"error": "no such path"})
                return
            if not self._authorised():
                self._json(401, {"error": "missing or bad console token"})
                return
            if parsed.path == "/api/state":
                self._json(*console.api_state())
                return
            if parsed.path == "/api/search":
                self._json(*console.api_search(parse_qs(parsed.query)))
                return
            if parsed.path == "/api/progress":
                self._json(*console.api_progress(parse_qs(parsed.query)))
                return
            self._json(404, {"error": "no such path"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own spelling
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._json(404, {"error": "no such path"})
                return
            if not self._authorised():
                self._json(401, {"error": "missing or bad console token"})
                return
            body = self._read_body()
            if body is None:
                self._json(400, {"error": "expected a JSON object body"})
                return
            if parsed.path == "/api/resolve":
                self._json(*console.api_resolve(body))
                return
            if parsed.path == "/api/pin":
                self._json(*console.api_pin(body))
                return
            self._json(404, {"error": "no such path"})

    return Handler


def serve(console: Console, *, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind and return the server, without serving. Port 0 asks the OS for a free one.

    Returning rather than serving is what lets the test run one in a thread and read the port it
    actually got, and what lets `main` print the URL before it blocks.
    """
    return ThreadingHTTPServer((HOST, port), make_handler(console))


# --------------------------------------------------------------------------- the page
#
# The design tokens are the status page's, copied rather than imported: `status.py` renders one
# static file and this renders a live one, and giving them a shared stylesheet module would tie
# two pages with different jobs to one another for the sake of forty lines. They are meant to read
# as one tool, which is a matter of the values agreeing, and the values are listed in the console
# handoff so a change to either is a deliberate change to both.

_CSS = """
:root{--ground:#F4F1EA;--surface:#FFFFFF;--ink:#1C1B18;--muted:#5F5B53;--rule:#D9D3C6;
--hairline:#E7E2D8;--ok:#1F6A4A;--ok-bg:#E3EFE8;--drift:#A0491A;--drift-bg:#F3E3DA;
--grey:#5F5B53;--grey-bg:#EEEBE3;--link:#8B4A1C;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
font:400 16px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1160px;margin:0 auto;padding:40px 24px 64px}
a{color:var(--link)}
.mono{font-family:var(--mono)}
.muted{color:var(--muted)}
.warn{color:var(--drift)}
.head{padding-bottom:20px;border-bottom:1px solid var(--rule)}
.eyebrow{margin:0 0 6px;color:var(--muted);font-size:12px;letter-spacing:.1em;
text-transform:uppercase;font-family:var(--mono)}
h1{margin:0;font:600 40px/1.1 Fraunces,Georgia,"Times New Roman",serif;letter-spacing:-.01em}
.local{margin:10px 0 0;color:var(--muted);font-size:14px}
.chips{margin:24px 0 0;display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-block;padding:4px 10px;border-radius:999px;font-size:13px}
.chip-ok{color:var(--ok);background:var(--ok-bg)}
.chip-grey{color:var(--grey);background:var(--grey-bg)}
.chip .mono{font-weight:500}
.key{margin-left:6px;font-size:12px}
.card{background:var(--surface);border:1px solid var(--hairline);border-radius:6px;padding:18px;
margin-top:24px}
.card h2{margin:0 0 14px;font:500 11px/1.2 var(--mono);color:var(--muted);
letter-spacing:.1em;text-transform:uppercase}
.row{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px;
font-family:var(--mono);letter-spacing:.06em;text-transform:uppercase}
input[type=text],select{min-height:44px;padding:0 10px;background:var(--surface);
color:var(--ink);border:1px solid var(--rule);border-radius:4px;font:inherit;font-size:15px}
input[type=text]{min-width:280px}
.grow{flex:1 1 280px}
.grow input{width:100%}
button{min-height:44px;padding:0 16px;background:var(--surface);color:var(--link);
border:1px solid var(--rule);border-radius:4px;font:inherit;font-size:15px;cursor:pointer}
button:hover{background:var(--grey-bg)}
button:focus-visible{outline:2px solid var(--link);outline-offset:2px}
button.primary{background:var(--ok);border-color:var(--ok);color:#fff}
button.primary:hover{filter:brightness(1.08)}
button[disabled]{opacity:.5;cursor:not-allowed}
table{width:100%;border-collapse:collapse;margin-top:6px}
th{text-align:left;padding:10px 12px;border-bottom:1px solid var(--rule);color:var(--muted);
font-size:11px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;
font-family:var(--mono)}
td{padding:12px;border-bottom:1px solid var(--hairline);vertical-align:top;font-size:15px}
tr:last-child td{border-bottom:0}
dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:8px 18px}
dt{color:var(--muted);font-size:12px;font-family:var(--mono);letter-spacing:.06em;
text-transform:uppercase;padding-top:2px}
dd{margin:0}
.cite{font-weight:600}
pre{margin:10px 0 0;padding:12px;background:var(--grey-bg);border-radius:4px;
font-family:var(--mono);font-size:13px;white-space:pre-wrap;word-break:break-word}
.fields{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.opts{display:flex;flex-wrap:wrap;gap:18px;align-items:center;margin-top:16px}
.opts label{display:inline-flex;align-items:center;gap:8px;margin:0;font-size:14px;
font-family:inherit;letter-spacing:0;text-transform:none;color:var(--ink)}
.note{margin:12px 0 0;color:var(--muted);font-size:13px}
.foot{margin-top:40px;padding-top:18px;border-top:1px solid var(--rule);font-size:13px;
color:var(--muted)}
.foot ul{margin:8px 0 0;padding-left:18px;font-family:var(--mono);font-size:12px}
.hidden{display:none}
@media (max-width:720px){
.wrap{padding:24px 16px 48px}
h1{font-size:30px}
input[type=text]{min-width:0}
.grow{flex-basis:100%}
button{width:100%}
dl{grid-template-columns:1fr;gap:2px 0}
dt{margin-top:10px}
}
"""

# Every value the page puts on screen goes in through `textContent` or as an attribute set with
# `setAttribute`, never through innerHTML. API JSON is data — a case name from somebody else's
# search index, a refusal message built from a citation — and the one place it could become markup
# is the one place this page refuses to let it.
_JS = """
(function () {
  var TOKEN = document.getElementById('t').getAttribute('data-token');
  var STATE = JSON.parse(document.getElementById('s').textContent);
  var resolved = null;

  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null && text !== '') { n.textContent = String(text); }
    if (cls) { n.className = cls; }
    return n;
  }
  function byId(id) { return document.getElementById(id); }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function say(node, text, cls) { clear(node); node.appendChild(el('p', text, cls || 'note')); }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = { 'X-Console-Token': TOKEN, 'Content-Type': 'application/json' };
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
    });
  }

  // api() resolves only when a response ARRIVED. A request that never got one — the server
  // stopped, the policy blocked it, the JSON did not parse — rejects, and an uncaught rejection
  // left the last status line on screen forever. That is how a blocked fetch read as a console
  // that hung: 'Searching...' and nothing else, with the reason only in the Network tab. Every
  // caller ends in this, in its own status slot.
  function failed(node, err) {
    say(node, 'request failed: ' + ((err && err.message) || String(err)), 'note warn');
  }

  function fetcherByName(name) {
    for (var i = 0; i < STATE.fetchers.length; i++) {
      if (STATE.fetchers[i].name === name) { return STATE.fetchers[i]; }
    }
    return null;
  }

  // -- search ---------------------------------------------------------------

  var searchFetcher = byId('search-fetcher');
  var searchType = byId('search-type');
  var searchResults = byId('search-results');

  function fillTypes() {
    var f = fetcherByName(searchFetcher.value);
    clear(searchType);
    (f ? f.search_types : []).forEach(function (t) {
      searchType.appendChild(new Option(t, t));
    });
  }
  searchFetcher.addEventListener('change', fillTypes);
  fillTypes();

  byId('search-go').addEventListener('click', function () {
    var q = byId('search-q').value.trim();
    if (!q) { return; }
    say(searchResults, 'Searching\\u2026');
    var url = '/api/search?fetcher=' + encodeURIComponent(searchFetcher.value) +
      '&type=' + encodeURIComponent(searchType.value) + '&q=' + encodeURIComponent(q);
    api(url).then(function (r) {
      if (!r.ok) { say(searchResults, r.body.error || 'search failed', 'note warn'); return; }
      var hits = r.body.hits || [];
      if (!hits.length) { say(searchResults, 'No hits.'); return; }
      clear(searchResults);
      var table = el('table');
      var head = el('tr');
      ['identifier', 'date', 'label', 'docket / number', 'citation', ''].forEach(function (h) {
        head.appendChild(el('th', h));
      });
      table.appendChild(el('thead')).appendChild(head);
      var body = el('tbody');
      hits.forEach(function (hit) {
        var tr = el('tr');
        tr.appendChild(el('td', hit.identifier || '\\u2014')).className = 'mono';
        tr.appendChild(el('td', hit.date || '\\u2014'));
        var label = el('td');
        if (hit.url) {
          var a = el('a', hit.label || hit.identifier);
          a.setAttribute('href', hit.url);
          a.setAttribute('rel', 'noreferrer');
          a.setAttribute('target', '_blank');
          label.appendChild(a);
        } else {
          label.textContent = hit.label || '\\u2014';
        }
        tr.appendChild(label);
        tr.appendChild(el('td', hit.docket_or_number || '\\u2014'));
        tr.appendChild(el('td', hit.citation || '\\u2014'));
        var act = el('td');
        if (hit.add_command) {
          var b = el('button', 'Resolve');
          b.addEventListener('click', function () {
            resolve(searchFetcher.value, { cluster_id: hit.identifier });
          });
          act.appendChild(b);
        } else {
          act.appendChild(el('span', 'not pinnable directly', 'muted'));
        }
        tr.appendChild(act);
        body.appendChild(tr);
      });
      table.appendChild(body);
      searchResults.appendChild(table);
    }).catch(function (err) { failed(searchResults, err); });
  });

  // -- pin by identifier ----------------------------------------------------

  var idFetcher = byId('id-fetcher');
  var idFields = byId('id-fields');

  function fillFields() {
    var f = fetcherByName(idFetcher.value);
    clear(idFields);
    (f ? f.fields : []).forEach(function (field) {
      var wrap = el('div');
      var lab = el('label', field.flag + (field.required ? ' *' : ''));
      lab.setAttribute('for', 'f-' + field.dest);
      var input = el('input');
      input.setAttribute('type', 'text');
      input.setAttribute('id', 'f-' + field.dest);
      input.setAttribute('data-dest', field.dest);
      if (field.help) { input.setAttribute('title', field.help); }
      wrap.appendChild(lab);
      wrap.appendChild(input);
      idFields.appendChild(wrap);
    });
  }
  idFetcher.addEventListener('change', fillFields);
  fillFields();

  byId('id-go').addEventListener('click', function () {
    var args = {};
    idFields.querySelectorAll('input').forEach(function (input) {
      args[input.getAttribute('data-dest')] = input.value;
    });
    resolve(idFetcher.value, args);
  });

  // -- resolve --------------------------------------------------------------

  var card = byId('resolve-card');

  var GRADES = {
    A1: 'publisher of record',
    B1: 'institutional archive, page image',
    B2: 'mirror copy'
  };

  function resolve(fetcher, args) {
    resolved = null;
    card.classList.remove('hidden');
    say(card, 'Resolving\\u2026');
    // A resolve is one blocking POST that can sit for forty seconds behind CourtListener's
    // rate limit. The page opens a second, cheap channel and polls it, so the wait has a
    // reason on screen while it is happening rather than after it ends.
    var progressId = String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    var ticking = setInterval(function () {
      api('/api/progress?id=' + encodeURIComponent(progressId)).then(function (p) {
        if (!resolved && p.ok && p.body && p.body.message) { say(card, p.body.message); }
      }).catch(function () { /* a lost tick is not news; the resolve reports its own end */ });
    }, 1000);
    function stop() { clearInterval(ticking); }
    api('/api/resolve', {
      method: 'POST',
      body: JSON.stringify({ fetcher: fetcher, args: args, progress_id: progressId })
    })
      .then(function (r) {
        stop();
        if (!r.ok) { say(card, r.body.error || 'resolve failed', 'note warn'); return; }
        if (!r.body.pinnable) {
          say(card, 'No pinnable document: ' + r.body.message, 'note warn');
          return;
        }
        resolved = r.body;
        drawResolved(r.body);
      })
      .catch(function (err) { stop(); failed(card, err); });
  }

  function drawResolved(d) {
    clear(card);
    card.appendChild(el('h2', 'Would pin'));
    var dl = el('dl');
    function pair(k, v, cls) {
      if (v === null || v === undefined || v === '') { return; }
      dl.appendChild(el('dt', k));
      dl.appendChild(el('dd', v, cls));
    }
    pair('citation', d.citation, 'cite');
    pair('title', d.title);
    pair('publisher', d.publisher);
    pair('grade', d.grade + (GRADES[d.grade] ? '  \\u00b7  ' + GRADES[d.grade] : ''));
    pair('version', d.point_in_time ? 'as of ' + d.point_in_time
      : (d.published_at ? 'published ' + d.published_at : 'no version axis'));
    pair('drift key', d.drift_key);
    dl.appendChild(el('dt', 'document'));
    var dd = el('dd');
    var a = el('a', d.canonical_url);
    a.setAttribute('href', d.canonical_url);
    a.setAttribute('rel', 'noreferrer');
    a.setAttribute('target', '_blank');
    dd.appendChild(a);
    dl.appendChild(dd);
    card.appendChild(dl);

    if (d.already_pinned) {
      card.appendChild(el('p', 'Already pinned as ' + d.already_pinned + '.', 'note warn'));
      return;
    }

    var opts = el('div', null, 'opts');
    var archLab = el('label');
    var arch = el('input');
    arch.setAttribute('type', 'checkbox');
    arch.setAttribute('id', 'opt-archive');
    if (d.requires_archive) {
      arch.checked = true;
      arch.disabled = true;
      archLab.appendChild(arch);
      archLab.appendChild(el('span', 'Archive (required by ' + d.fetcher + ')'));
    } else {
      arch.checked = d.fetcher === 'courtlistener';
      archLab.appendChild(arch);
      archLab.appendChild(el('span', 'Archive to the Wayback Machine'));
    }
    opts.appendChild(archLab);

    if (d.supersedes_candidates && d.supersedes_candidates.length) {
      var supWrap = el('div');
      supWrap.appendChild(el('label', 'supersedes'));
      var sup = el('select');
      sup.setAttribute('id', 'opt-supersedes');
      sup.appendChild(new Option('\\u2014 none \\u2014', ''));
      d.supersedes_candidates.forEach(function (id) { sup.appendChild(new Option(id, id)); });
      supWrap.appendChild(sup);
      opts.appendChild(supWrap);
    }

    var noteWrap = el('div', null, 'grow');
    noteWrap.appendChild(el('label', 'notes'));
    var notes = el('input');
    notes.setAttribute('type', 'text');
    notes.setAttribute('id', 'opt-notes');
    notes.setAttribute('placeholder', 'plain-language label, shown ahead of the title');
    noteWrap.appendChild(notes);
    opts.appendChild(noteWrap);
    card.appendChild(opts);

    var go = el('button', 'Pin', 'primary');
    go.setAttribute('id', 'pin-go');
    go.addEventListener('click', doPin);
    var bar = el('div', null, 'opts');
    bar.appendChild(go);
    card.appendChild(bar);
  }

  // -- pin ------------------------------------------------------------------

  var outcome = byId('outcome-card');

  function doPin() {
    if (!resolved) { return; }
    var go = byId('pin-go');
    if (go) { go.disabled = true; go.textContent = 'Pinning\\u2026'; }
    var sup = byId('opt-supersedes');
    var body = {
      resolve_id: resolved.resolve_id,
      archive: byId('opt-archive').checked,
      supersedes: sup ? (sup.value || null) : null,
      notes: byId('opt-notes').value || null
    };
    api('/api/pin', { method: 'POST', body: JSON.stringify(body) }).then(function (r) {
      outcome.classList.remove('hidden');
      if (!r.ok) { say(outcome, r.body.error || 'pin failed', 'note warn'); return; }
      var d = r.body;
      clear(outcome);
      outcome.appendChild(el('h2', d.status === 'written' ? 'Pinned' : 'Not pinned'));
      if (d.status !== 'written') {
        outcome.appendChild(el('p', d.message, 'note warn'));
        if (go) { go.disabled = false; go.textContent = 'Pin'; }
        return;
      }
      outcome.appendChild(el('p', d.message, 'note'));
      outcome.appendChild(el('pre', d.ledger));
      var copy = el('button', 'Copy ledger entry');
      copy.addEventListener('click', function () {
        navigator.clipboard.writeText(d.ledger).then(function () {
          copy.textContent = 'Copied';
        }, function () { copy.textContent = 'Copy failed'; });
      });
      var bar = el('div', null, 'opts');
      bar.appendChild(copy);
      outcome.appendChild(bar);
      outcome.appendChild(el('pre', d.manifest));
      outcome.appendChild(el('p', 'Next: git add ' + d.path, 'note mono'));
      resolved = null;
      card.classList.add('hidden');
    }).catch(function (err) {
      outcome.classList.remove('hidden');
      failed(outcome, err);
      // The pin may or may not have happened; the button goes back so the operator can look and
      // retry, and add_source refuses a duplicate if it did.
      if (go) { go.disabled = false; go.textContent = 'Pin'; }
    });
  }
})();
"""


def render_page(token: str, snapshot: dict[str, Any]) -> str:
    """The whole console, one document. Server-side escaping on every interpolated value.

    The fetcher chips and the selects are rendered here rather than by the script so the page says
    something true before any JavaScript runs; everything downstream of a click is the script's.
    """
    e = html.escape

    chips = []
    for f in snapshot["fetchers"]:
        if f["verified"]:
            tone, label = "chip-ok", f"verified {f['verified_at']}"
        else:
            tone, label = "chip-grey", "unverified"
        key = ""
        if f["env_key"]:
            key = (
                f'<span class="key">{"key present" if f["key_present"] else "no key in .env"}'
                "</span>"
            )
        chips.append(
            f'<span class="chip {tone}"><span class="mono">{e(f["name"])}</span> {e(label)}'
            f"{key}</span>"
        )

    searchable = [f for f in snapshot["fetchers"] if f["searchable"]]
    # The select is in module order, which puts `openfec` first — unverified, keyless, and unable
    # to pin, so the console opened on the one searchable fetcher that can do the least. The
    # options stay in module order; only the DEFAULT moves, to the first searchable fetcher that
    # has actually been run live. Nothing is hard-coded: if openfec is verified one day it becomes
    # the default again by being first, and if nothing is verified the list falls back to its head.
    default_search = next(
        (f for f in searchable if f["verified"]), searchable[0] if searchable else None
    )
    search_options = "".join(
        f'<option value="{e(f["name"])}"'
        f"{' selected' if default_search and f['name'] == default_search['name'] else ''}"
        f">{e(f['name'])}</option>"
        for f in searchable
    )
    id_options = "".join(
        f'<option value="{e(f["name"])}">{e(f["name"])} · {e(f["help"])}</option>'
        for f in snapshot["fetchers"]
    )

    git = snapshot.get("git") or {}
    pending = git.get("pending") or []
    if not git:
        foot = "<p>Not a git checkout, or git is unavailable.</p>"
    elif pending:
        items = "".join(f"<li>{e(p)}</li>" for p in pending)
        foot = (
            f'<p class="warn">Uncommitted under data/sources/ on branch '
            f'<span class="mono">{e(git.get("branch") or "?")}</span>:</p><ul>{items}</ul>'
        )
    else:
        foot = (
            f"<p>Nothing uncommitted under data/sources/ on branch "
            f'<span class="mono">{e(git.get("branch") or "?")}</span>.</p>'
        )

    # The snapshot rides in a JSON script block rather than in the script body: `</script>` inside
    # a string would end the block early, and `<` is escaped here so no value from data/ or from a
    # fetcher's HELP can do that.
    payload = json.dumps(
        {
            "fetchers": [{**f, "fields": _form_fields(f["name"])} for f in snapshot["fetchers"]],
            "pins": snapshot["pins"],
        }
    ).replace("<", "\\u003c")

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="robots" content="noindex,nofollow">\n'
        "<title>Pinning console</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        f'<div id="t" data-token="{e(token)}"></div>\n'
        f'<script type="application/json" id="s">{payload}</script>\n'
        '<main class="wrap">\n'
        '<header class="head">\n'
        '<p class="eyebrow">registers-crosswalk</p>\n'
        "<h1>Pinning console</h1>\n"
        '<p class="local">Local only. Writes to data/sources/ on this machine; commits stay in '
        "git.</p>\n"
        f'<div class="chips">{"".join(chips)}</div>\n'
        "</header>\n"
        '<section class="card">\n<h2>Search</h2>\n'
        '<div class="row">\n'
        f'<div><label for="search-fetcher">fetcher</label>'
        f'<select id="search-fetcher">{search_options}</select></div>\n'
        '<div><label for="search-type">type</label><select id="search-type"></select></div>\n'
        '<div class="grow"><label for="search-q">query</label>'
        '<input type="text" id="search-q" placeholder="case name, docket number, matter"></div>\n'
        '<div><button id="search-go">Search</button></div>\n'
        "</div>\n"
        '<div id="search-results"></div>\n'
        "</section>\n"
        '<section class="card">\n<h2>Pin by identifier</h2>\n'
        '<div class="row">\n'
        f'<div><label for="id-fetcher">fetcher</label>'
        f'<select id="id-fetcher">{id_options}</select></div>\n'
        "</div>\n"
        '<div class="fields" id="id-fields"></div>\n'
        '<div class="opts"><button id="id-go">Resolve</button></div>\n'
        "</section>\n"
        '<section class="card hidden" id="resolve-card"></section>\n'
        '<section class="card hidden" id="outcome-card"></section>\n'
        f'<footer class="foot">{foot}</footer>\n'
        "</main>\n"
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="registers_crosswalk.console", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--env-file", type=Path, default=REPO_ROOT / ".env", help="default: .env in the repo root"
    )
    args = parser.parse_args(argv)

    env = load_dotenv(args.env_file)
    # `pacing` here rather than a pre-wrapped fetch: Console does the wrapping so each paced
    # wait is reported into the progress record the page polls.
    console = Console(data_dir=args.data_dir, env=env, fetch=default_fetch, pacing=PACING)
    try:
        server = serve(console, port=args.port)
    except OSError as exc:
        print(f"cannot bind {HOST}:{args.port}: {exc}", file=sys.stderr)
        return 1

    url = f"http://{HOST}:{server.server_address[1]}/?t={console.token}"
    # The token is printed once, here, and never logged again. It is in the URL so the operator can
    # paste it into a browser that did not open on its own; the page reads it from the element the
    # server rendered, not from the query string.
    print(f"pinning console on {url}")
    print(f"data dir: {args.data_dir}")
    named = [f"{k}" for k in sorted(env) if env[k]]
    print(f"keys loaded from {args.env_file}: {', '.join(named) if named else '(none)'}")
    print("ctrl-c to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
