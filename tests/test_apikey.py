"""The api.data.gov key stays out of every error a keyed metadata call can raise.

`apikey.keyed_fetch` is where govinfo, openfec and fecfiling attach their key. These tests hand it a
transport that fails by quoting the full URL it was asked for, the way urllib and http.client do,
and then look for the key everywhere an operator could see it: stdout, stderr, the traceback a real
process prints, and the console's JSON. Offline throughout. Two tests reach the real http.client
stack: the encoding test is stopped at `HTTPConnection.connect`, after the request line is built
and before any socket opens, and the console test connects only to the loopback server it starts.
"""

import email.message
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
from urllib.parse import quote, quote_plus

import pytest

from registers_crosswalk.apikey import keyed_fetch, keyed_url, redact
from registers_crosswalk.console import Console, serve
from registers_crosswalk.pin import main

# A value that appears nowhere else in the repo: finding it in any output means the key went there.
SENTINEL = "apikey-sentinel-5Rk2Wq-do-not-leak"
ENV = {"GOVINFO_API_KEY": SENTINEL, "OPENFEC_API_KEY": SENTINEL}


def _rendered(exc):
    """What a process prints for an uncaught `exc`: the traceback, the chain, and the notes."""
    return "".join(traceback.format_exception(exc))


# --------------------------------------------------------------------------- keyed_url


def test_keyed_url_encodes_every_value_and_puts_the_key_last():
    url = keyed_url(
        "https://api.open.fec.gov/v1/legal/search/",
        {"type": "murs", "case_no": "MUR 8098#&x=1"},
        "k y/+",
    )
    assert url == (
        "https://api.open.fec.gov/v1/legal/search/?type=murs&case_no=MUR+8098%23%26x%3D1"
        "&api_key=k+y%2F%2B"
    )


def test_keyed_url_appends_to_a_query_the_base_already_has():
    assert keyed_url("https://api.govinfo.gov/x?offsetMark=*", {}, "K") == (
        "https://api.govinfo.gov/x?offsetMark=*&api_key=K"
    )


@pytest.mark.parametrize(
    "base", ["https://api.govinfo.gov/a b", "https://x/\t", "https://x/\x7f", "https://x/é"]
)
def test_keyed_url_refuses_a_base_that_cannot_go_on_the_wire_and_names_only_the_base(base):
    with pytest.raises(ValueError, match="not a sendable URL") as caught:
        keyed_url(base, {}, SENTINEL)
    assert SENTINEL not in _rendered(caught.value)


# --------------------------------------------------------------------------- redact


class _Response(io.BytesIO):
    """Stands in for the http.client response an HTTPError wraps, which keeps its own `url`."""

    def __init__(self, url):
        super().__init__(b'{"error": {"code": "SOMETHING"}}')
        self.url = url


def _http_error(url, msg="Forbidden"):
    headers = email.message.Message()
    headers["Content-Type"] = "application/json"
    headers["Location"] = url
    return urllib.error.HTTPError(url, 403, msg, headers, _Response(url))


def _chained(url):
    try:
        raise OSError(f"send failed: {url}")
    except OSError:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as exc:
            return exc


def _noted(url):
    exc = RuntimeError("something went wrong")
    exc.add_note(f"while fetching {url}")
    return exc


def _caused(url):
    exc = RuntimeError("wrapped")
    exc.__cause__ = urllib.error.URLError(OSError(f"cannot reach {url}"))
    return exc


URL = f"https://api.open.fec.gov/v1/legal/search/?type=murs&case_no=8098&api_key={SENTINEL}"
SELECTOR = f"/v1/legal/search/?type=murs&case_no=MUR 8098&api_key={SENTINEL}"
REQUEST_LINE = f"GET /packages/X\xe9/summary?api_key={SENTINEL} HTTP/1.1"

# Each is built the way the standard library builds it, carrying the key where it carries it.
QUOTING = {
    "HTTPError url, filename, headers and response": lambda u: _http_error(u),
    "HTTPError message (a disallowed-scheme redirect)": lambda u: _http_error(
        u, f"Found - Redirection to url '{u}' is not allowed"
    ),
    "URLError around an OSError": lambda u: urllib.error.URLError(OSError(0, f"cannot reach {u}")),
    "InvalidURL": lambda u: http.client.InvalidURL(
        f"URL can't contain control characters. {SELECTOR!r} (found at least ' ')"
    ),
    "ValueError, unknown url type": lambda u: ValueError(f"unknown url type: {u!r}"),
    "OSError filename": lambda u: FileNotFoundError(2, "No such file", u),
    "a chained __context__": _chained,
    "an explicit __cause__": _caused,
    "a note": _noted,
}


def _all_text(exc):
    """Every string the exception, its chain and its attributes hold, flattened."""
    out, seen, todo = [], set(), [exc]
    while todo:
        e = todo.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen.add(id(e))
        out += [str(e), repr(e), repr(e.args), repr(vars(e))]
        for name in ("filename", "filename2", "object", "reason", "url"):
            out.append(repr(getattr(e, name, None)))
        for inner in ("fp", "file"):
            out.append(repr(getattr(getattr(e, inner, None), "url", None)))
        headers = getattr(e, "hdrs", None)
        if headers is not None:
            out.append(repr(headers.items()))
        todo += [e.__cause__, e.__context__, getattr(e, "reason", None)]
    return "\n".join(out)


@pytest.mark.parametrize("build", QUOTING.values(), ids=QUOTING.keys())
def test_redact_takes_the_key_out_of_every_place_it_sits(build):
    exc = build(URL)
    assert SENTINEL in _all_text(exc) + _rendered(exc)  # the case really does carry the key
    kind = type(exc)
    assert redact(exc, SENTINEL) is exc
    assert type(exc) is kind
    assert SENTINEL not in _all_text(exc)
    assert SENTINEL not in _rendered(exc)


def test_redact_keeps_a_unicode_error_pointing_at_the_character_that_failed():
    start = REQUEST_LINE.index("\xe9")
    exc = UnicodeEncodeError("ascii", REQUEST_LINE, start, start + 1, "ordinal not in range(128)")
    assert SENTINEL in repr(exc)
    redact(exc, SENTINEL)
    assert SENTINEL not in repr(exc) + str(exc) + exc.object
    assert exc.object[exc.start] == "\xe9"
    reason = "ordinal not in range(128)"
    assert str(exc) == f"'ascii' codec can't encode character '\\xe9' in position {start}: {reason}"


def test_redact_also_masks_the_key_url_encoded():
    key = "a key/with+specials"
    exc = ValueError(f"raw {key} plus {quote_plus(key)} path {quote(key, safe='')}")
    redact(exc, key)
    text = str(exc)
    assert key not in text and quote_plus(key) not in text and quote(key, safe="") not in text


# --------------------------------------------------------------------------- keyed_fetch


def _raising(build):
    """A transport that fails by quoting the full URL it was asked for."""
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        raise build(url)

    fetch.calls = calls
    return fetch


@pytest.mark.parametrize("build", QUOTING.values(), ids=QUOTING.keys())
def test_keyed_fetch_lets_nothing_out_with_the_key_in_it(build):
    fetch = _raising(build)
    kind = type(build(URL))
    with pytest.raises(kind) as caught:
        keyed_fetch(
            fetch, "https://api.open.fec.gov/v1/legal/search/", {"case_no": "8098"}, key=SENTINEL
        )
    assert fetch.calls == [
        (f"https://api.open.fec.gov/v1/legal/search/?case_no=8098&api_key={SENTINEL}", None)
    ]
    assert SENTINEL not in _all_text(caught.value)
    assert SENTINEL not in _rendered(caught.value)


def test_keyed_fetch_returns_what_the_transport_returns():
    def fetch(url, headers=None):
        return b"body", "application/json"

    assert keyed_fetch(fetch, "https://api.govinfo.gov/x", {}, key="K") == (
        b"body",
        "application/json",
    )


# --------------------------------------------------------------------------- the operator's view

# (fetcher, `pin add` flags, the console's resolve args)
ADDS = [
    ("govinfo", ["--package", "USCODE-2024-title52"], {"package": "USCODE-2024-title52"}),
    (
        "openfec",
        ["--number", "8098", "--type", "murs", "--document", "1"],
        {"number": "8098", "doc_type": "murs", "document_id": "1"},
    ),
    (
        "fecfiling",
        ["--file-number", "1903438", "--document", "fec"],
        {"file_number": "1903438", "document": "fec"},
    ),
]


@pytest.mark.parametrize("build", QUOTING.values(), ids=QUOTING.keys())
@pytest.mark.parametrize(("fetcher", "flags", "_args"), ADDS, ids=[a[0] for a in ADDS])
def test_pin_add_prints_no_key_whatever_the_transport_raises(
    fetcher, flags, _args, build, tmp_path, monkeypatch, capsys
):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(Exception) as caught:
        main(["--data-dir", str(tmp_path), "add", fetcher, *flags], fetch=_raising(build))
    out, err = capsys.readouterr()
    assert SENTINEL not in out + err + _rendered(caught.value)


@pytest.mark.parametrize("build", QUOTING.values(), ids=QUOTING.keys())
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_pin_search_prints_no_key_whatever_the_transport_raises(
    build, as_json, monkeypatch, capsys
):
    monkeypatch.setenv("OPENFEC_API_KEY", SENTINEL)
    argv = ["search", "openfec", "Osborn", *(["--json"] if as_json else [])]
    with pytest.raises(Exception) as caught:
        main(argv, fetch=_raising(build))
    out, err = capsys.readouterr()
    assert SENTINEL not in out + err + _rendered(caught.value)


def _no_archive(url, **_):
    raise AssertionError(f"resolve and search never archive: {url}")


def _console(tmp_path, build):
    (tmp_path / "sources").mkdir(exist_ok=True)
    return Console(data_dir=tmp_path, env=ENV, fetch=_raising(build), archive_fn=_no_archive)


@pytest.mark.parametrize("build", QUOTING.values(), ids=QUOTING.keys())
@pytest.mark.parametrize(("fetcher", "_flags", "args"), ADDS, ids=[a[0] for a in ADDS])
def test_console_resolve_shows_no_key_whatever_the_transport_raises(
    fetcher, _flags, args, build, tmp_path, capsys
):
    console = _console(tmp_path, build)
    try:
        status, payload = console.api_resolve({"fetcher": fetcher, "args": args})
    except Exception as exc:  # what escapes goes to socketserver's handler, which prints it
        shown = _rendered(exc)
    else:
        assert status >= 400 or payload.get("pinnable") is False
        shown = json.dumps(payload)
    out, err = capsys.readouterr()
    assert SENTINEL not in shown + out + err


@pytest.mark.parametrize("build", QUOTING.values(), ids=QUOTING.keys())
def test_console_search_shows_no_key_whatever_the_transport_raises(build, tmp_path, capsys):
    console = _console(tmp_path, build)
    try:
        _, payload = console.api_search({"fetcher": ["openfec"], "q": ["Osborn"], "type": ["murs"]})
    except Exception as exc:
        shown = _rendered(exc)
    else:
        shown = json.dumps(payload)
    out, err = capsys.readouterr()
    assert SENTINEL not in shown + out + err


def test_an_error_that_escapes_the_console_is_printed_without_the_key(tmp_path, capfd):
    # InvalidURL is neither ValueError nor OSError, so it gets past `_resolve` to socketserver,
    # which prints the traceback on the console's own stderr. That printout is what is checked.
    console = _console(tmp_path, QUOTING["InvalidURL"])
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"fetcher": "openfec", "args": ADDS[1][2]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/resolve", data=body, method="POST"
        )
        req.add_header("X-Console-Token", console.token)
        req.add_header("Content-Type", "application/json")
        with pytest.raises((http.client.RemoteDisconnected, urllib.error.URLError)):
            urllib.request.urlopen(req, timeout=10)  # noqa: S310 - fixed localhost
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    out, err = capfd.readouterr()
    assert "InvalidURL" in err  # the handler did print it
    assert SENTINEL not in out + err


def test_python_m_prints_no_key_when_the_transport_quotes_the_url(tmp_path):
    # The real entry point and the real excepthook, in a child process, with urlopen replaced by
    # one that fails quoting the URL it was given.
    script = (
        "import runpy, sys, urllib.error, urllib.request\n"
        "def urlopen(req, *a, **k):\n"
        "    raise urllib.error.URLError(OSError(0, f'cannot reach {req.full_url}'))\n"
        "urllib.request.urlopen = urlopen\n"
        f"sys.argv = ['pin', '--data-dir', {str(tmp_path)!r}, 'add', 'openfec', '--number', '8098',"
        " '--type', 'murs', '--document', '1']\n"
        "runpy.run_module('registers_crosswalk.pin', run_name='__main__', alter_sys=True)\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["OPENFEC_API_KEY"] = SENTINEL
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 1
    assert "URLError" in done.stderr
    assert SENTINEL not in done.stdout + done.stderr


# --------------------------------------------------------------------------- the real stack


@pytest.fixture
def no_network(monkeypatch):
    """http.client builds and validates the request line, then fails where it would connect."""

    def refuse(self):
        raise OSError("network blocked by the test")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("fetcher", "flags"),
    [
        # The inputs that, typed raw into the URL, made http.client refuse the request line with an
        # InvalidURL quoting the key, or fail to encode it with the key in the error's `object`.
        ("openfec", ["--number", "MUR 8098", "--type", "murs", "--document", "1"]),
        ("openfec", ["--number", "8098\t", "--type", "murs", "--document", "1"]),
        ("govinfo", ["--package", "USCODE-2024 title52"]),
        ("govinfo", ["--package", "USCODE-2024-title52", "--granule", "sec 30116"]),
        ("govinfo", ["--package", "USCODE–2024"]),
    ],
)
def test_operator_input_is_encoded_before_http_client_sees_it(
    fetcher, flags, no_network, tmp_path, monkeypatch, capsys
):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(urllib.error.URLError) as caught:
        main(["--data-dir", str(tmp_path), "add", fetcher, *flags])
    # It got as far as connecting, so the request line was accepted: no InvalidURL, no
    # UnicodeEncodeError, and nothing that quotes the key.
    assert "network blocked by the test" in str(caught.value.reason)
    out, err = capsys.readouterr()
    assert SENTINEL not in out + err + _rendered(caught.value) + _all_text(caught.value)


# --------------------------------------------------------------------------- check and blobs
#
# No pin sits on a keyed host, but govinfo's `content_request` still attaches the key to one that
# did, and `check` and `blobs` fetch that URL themselves and report what went wrong. These put a
# pin there to hold both to the same rule.

GOVINFO_SUMMARY = {
    "title": "United States Code, 2024 Edition, Title 52",
    "dateIssued": "2024-12-31",
    "download": {"pdfLink": "https://api.govinfo.gov/packages/X/pdf"},
}


def _two_govinfo_pins(tmp_path, monkeypatch, first_url):
    """Two govinfo pins, the first moved to `first_url` on the API host."""
    monkeypatch.setenv("GOVINFO_API_KEY", SENTINEL)

    def fetch(url, headers=None):
        if url.startswith("https://api.govinfo.gov/"):
            return json.dumps(GOVINFO_SUMMARY).encode(), "application/json"
        return b"%PDF-1.4 " + url.encode(), "application/pdf"

    for package in ("USCODE-2024-title52", "USCODE-2024-title2"):
        argv = ["--data-dir", str(tmp_path), "add", "govinfo", "--package", package]
        assert main(argv, fetch=fetch) == 0
    path = tmp_path / "sources" / "xr_src_0001.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["canonical_url"] = first_url
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return fetch


def _quoting_for_the_api_host(fallback):
    def fetch(url, headers=None):
        if url.startswith("https://api.govinfo.gov/"):
            raise urllib.error.URLError(OSError(0, f"cannot reach {url}"))
        return fallback(url, headers)

    return fetch


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_check_reports_a_keyed_refetch_failure_without_the_key(
    as_json, tmp_path, monkeypatch, capsys
):
    served = _two_govinfo_pins(tmp_path, monkeypatch, "https://api.govinfo.gov/packages/P/pdf")
    capsys.readouterr()
    argv = ["--data-dir", str(tmp_path), "check", *(["--json"] if as_json else [])]
    fetch = _quoting_for_the_api_host(served)
    code = main(argv, fetch=fetch, sleep_fn=lambda _: None)
    out, err = capsys.readouterr()
    assert code == 3  # the pin's own status is unchanged: FETCH_FAILED
    assert "cannot reach https://api.govinfo.gov/packages/P/pdf?api_key=" in out
    assert SENTINEL not in out + err


def test_blobs_reports_a_keyed_refetch_failure_without_the_key(tmp_path, monkeypatch, capsys):
    served = _two_govinfo_pins(tmp_path, monkeypatch, "https://api.govinfo.gov/packages/P/pdf")
    capsys.readouterr()
    argv = ["--data-dir", str(tmp_path), "blobs", "--out", str(tmp_path / "out")]
    assert main(argv, fetch=_quoting_for_the_api_host(served)) == 1
    out, err = capsys.readouterr()
    assert "FETCH_FAILED" in out and "WRITTEN" in out
    assert SENTINEL not in out + err


@pytest.mark.parametrize(
    "stored",
    ["https://api.govinfo.gov/packages/P Q/pdf", "https://api.govinfo.gov/packages/PÉ/pdf"],
    ids=["space", "non-ascii"],
)
def test_a_stored_url_no_key_can_be_attached_to_is_one_pins_error(
    stored, tmp_path, monkeypatch, capsys
):
    served = _two_govinfo_pins(tmp_path, monkeypatch, stored)
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "check", "--json"], fetch=served) == 1
    out, err = capsys.readouterr()
    reports = {r["xr_id"]: r for r in json.loads(out)}
    assert reports["xr_src_0001"]["status"] == "error"
    assert "not a sendable URL" in reports["xr_src_0001"]["detail"]
    assert reports["xr_src_0002"]["status"] == "ok"  # the run went on
    assert SENTINEL not in out + err

    argv = ["--data-dir", str(tmp_path), "blobs", "--out", str(tmp_path / "out")]
    assert main(argv, fetch=served) == 1
    out, err = capsys.readouterr()
    assert "ERROR" in out and "WRITTEN" in out
    assert SENTINEL not in out + err
