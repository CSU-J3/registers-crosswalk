"""Every credential is read the same way, and none follows a redirect to another host.

`pin.read_key` strips a key's surrounding whitespace, as the console's `.env` parser does: a `.env`
saved with CRLF line endings and sourced by bash leaves a CR on every value. What is left is refused
if it still holds whitespace or a control character, and the refusal names the variable, never the
value. An Authorization header rides as an unredirected header, so urllib does not copy it onto the
request a redirect makes. Offline: the redirect tests run two servers on the loopback address.
"""

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from registers_crosswalk import fetchers
from registers_crosswalk import pin as pinmod
from registers_crosswalk.console import Console, render_page, state
from registers_crosswalk.fetchers import courtlistener, fecfiling, govinfo, openfec
from registers_crosswalk.pin import MalformedKey, MissingKey, archive, main

SENTINEL = "credential-sentinel-8Nw4Ks-do-not-leak"


class _Sent(Exception):
    """Raised by the recording fetch once it has the request, so no response has to be invented."""


def _refuse(*a, **k):
    pytest.fail("a request was made")


def _first_request(call):
    sent = []

    def fetch(url, headers=None):
        sent.append((url, headers))
        raise _Sent

    with pytest.raises(_Sent):
        call(fetch)
    [(url, headers)] = sent
    return url, headers or {}


# How each keyed call is made, and where its credential shows up in the request it sends.
KEYED = {
    "govinfo spec": (
        "GOVINFO_API_KEY",
        lambda f, env: govinfo.spec(package="X", fetch=f, env=env),
        lambda url, headers: url.split("api_key=", 1)[1],
    ),
    "openfec spec": (
        "OPENFEC_API_KEY",
        lambda f, env: openfec.spec(number="8098", doc_type="murs", fetch=f, env=env),
        lambda url, headers: url.split("api_key=", 1)[1],
    ),
    "openfec search": (
        "OPENFEC_API_KEY",
        lambda f, env: openfec.search("Osborn", doc_type="murs", fetch=f, env=env),
        lambda url, headers: url.split("api_key=", 1)[1],
    ),
    "fecfiling spec": (
        "OPENFEC_API_KEY",
        lambda f, env: fecfiling.spec(file_number=1, document="fec", fetch=f, env=env),
        lambda url, headers: url.split("api_key=", 1)[1],
    ),
    "courtlistener spec": (
        "COURTLISTENER_TOKEN",
        lambda f, env: courtlistener.spec(cluster_id=1, fetch=f, env=env),
        lambda url, headers: headers["Authorization"].removeprefix("Token "),
    ),
    "courtlistener search": (
        "COURTLISTENER_TOKEN",
        lambda f, env: courtlistener.search("Dunne", fetch=f, env=env),
        lambda url, headers: headers["Authorization"].removeprefix("Token "),
    ),
}


@pytest.mark.parametrize("raw", [f"{SENTINEL}\r", f"  {SENTINEL}\r\n", f"\t{SENTINEL} "])
@pytest.mark.parametrize("name", KEYED)
def test_a_key_is_sent_without_its_surrounding_whitespace(name, raw):
    var, call, credential_in = KEYED[name]
    url, headers = _first_request(lambda f: call(f, {var: raw}))
    assert credential_in(url, headers) == SENTINEL


@pytest.mark.parametrize(
    "inner",
    [" ", "\t", "\r", "\n", "\x00", "\x1b", "\x7f", "\x85", "\u00a0", "\u2028"],
    ids=["space", "tab", "cr", "lf", "nul", "esc", "del", "nel", "nbsp", "line-sep"],
)
@pytest.mark.parametrize("name", KEYED)
def test_a_key_still_holding_whitespace_or_a_control_character_is_refused(name, inner):
    var, call, _ = KEYED[name]
    value = f"{SENTINEL[:12]}{inner}{SENTINEL[12:]}"
    sent = []
    with pytest.raises(MalformedKey) as caught:
        call(lambda url, headers=None: sent.append(url), {var: f"  {value}\r\n"})
    assert sent == []  # refused before any request was built
    assert isinstance(caught.value, MissingKey)  # so every missing-key handler reports it
    assert var in str(caught.value)
    assert SENTINEL[:12] not in str(caught.value) and SENTINEL[12:] not in str(caught.value)


@pytest.mark.parametrize("raw", ["", "   ", "\r\n"])
def test_a_key_that_is_only_whitespace_is_missing(raw):
    with pytest.raises(MissingKey) as caught:
        govinfo.spec(
            package="X", fetch=lambda *a: pytest.fail("fetched"), env={"GOVINFO_API_KEY": raw}
        )
    assert not isinstance(caught.value, MalformedKey)


def test_pin_add_and_search_name_a_malformed_key_and_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENFEC_API_KEY", f"{SENTINEL[:8]} {SENTINEL[8:]}")
    line = (
        "openfec: environment variable OPENFEC_API_KEY contains whitespace or a control character\n"
    )
    argv = ["--data-dir", str(tmp_path), "add", "openfec", "--number", "8098"]
    assert main(argv, fetch=_refuse) == 2
    assert capsys.readouterr() == ("", line)
    assert main(["search", "openfec", "Osborn"], fetch=_refuse) == 2
    assert capsys.readouterr() == ("", line)


def test_the_console_names_a_malformed_key(tmp_path):
    (tmp_path / "sources").mkdir()
    env = {"OPENFEC_API_KEY": f"{SENTINEL}\x00"}
    console = Console(data_dir=tmp_path, env=env, fetch=lambda *a: pytest.fail("fetched"))
    status, payload = console.api_resolve({"fetcher": "openfec", "args": {"number": "8098"}})
    assert status == 400
    assert payload == {
        "error": "openfec: environment variable OPENFEC_API_KEY contains whitespace or a "
        "control character"
    }


def test_the_console_chip_judges_the_key_the_way_the_fetchers_read_it(tmp_path, monkeypatch):
    monkeypatch.setattr("registers_crosswalk.console._git_state", lambda d: {})
    env = {
        "COURTLISTENER_TOKEN": f"{SENTINEL[:8]} {SENTINEL[8:]}",  # set, but will be refused
        "GOVINFO_API_KEY": "  \r\n",  # only whitespace: not set
        "OPENFEC_API_KEY": f"{SENTINEL}\r",  # a CR from a CRLF .env: stripped, usable
    }
    snap = state(tmp_path, env)
    by_name = {f["name"]: f for f in snap["fetchers"]}
    assert (by_name["courtlistener"]["key_present"], by_name["courtlistener"]["key_malformed"]) == (
        False,
        True,
    )
    assert (by_name["govinfo"]["key_present"], by_name["govinfo"]["key_malformed"]) == (
        False,
        False,
    )
    assert (by_name["openfec"]["key_present"], by_name["openfec"]["key_malformed"]) == (True, False)
    page = render_page("token", snap)
    assert "key malformed" in page and "no key in .env" in page and "key present" in page
    assert SENTINEL[:8] not in page and SENTINEL[8:] not in page


def test_masking_uses_the_value_that_was_sent():
    # `check` masks the key out of an error; the key it sent is the stripped one.
    assert fetchers.credential("govinfo", {"GOVINFO_API_KEY": f" {SENTINEL}\r\n"}) == SENTINEL
    assert fetchers.credential("govinfo", {"GOVINFO_API_KEY": " \r\n"}) is None


# --------------------------------------------------------------------------- the Wayback keys


def test_the_wayback_keys_are_stripped_before_they_are_sent():
    seen = []

    def json_fn(url, headers, data):
        seen.append(headers)
        raise OSError("stop here")

    env = {"WAYBACK_ACCESS_KEY": " access\r\n", "WAYBACK_SECRET_KEY": "secret\r"}
    archive(
        "https://www.fec.gov/x.pdf",
        env=env,
        json_fn=json_fn,
        headers_fn=_refuse,  # the anonymous save: reached only if the keys went unrecognised
        capture_fn=_refuse,
        sleep_fn=lambda _: None,
    )
    assert seen and all(h["Authorization"] == "LOW access:secret" for h in seen)


@pytest.mark.parametrize("var", ["WAYBACK_ACCESS_KEY", "WAYBACK_SECRET_KEY"])
def test_a_malformed_wayback_key_is_named_and_nothing_is_sent(var):
    env = {"WAYBACK_ACCESS_KEY": "access", "WAYBACK_SECRET_KEY": "secret", var: "sec ret\r"}

    result = archive(
        "https://www.fec.gov/x.pdf",
        env=env,
        json_fn=_refuse,
        headers_fn=_refuse,
        capture_fn=_refuse,
    )
    assert isinstance(result, pinmod.ArchiveFailure)
    assert (
        result.reason
        == f"archive: environment variable {var} contains whitespace or a control character"
    )
    assert "sec ret" not in result.reason


# --------------------------------------------------------------------------- redirects


class _Recorder(BaseHTTPRequestHandler):
    """Answers GET and POST: a redirect to `target` if the server has one, otherwise JSON."""

    def _answer(self):
        self.server.seen.append(dict(self.headers))
        if self.server.target and self.path != getattr(self.server, "landed", None):
            self.send_response(302)
            self.send_header("Location", self.server.target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer  # noqa: N815 - BaseHTTPRequestHandler's own spelling
    do_POST = _answer  # noqa: N815

    def log_message(self, *args):
        pass


def _no_proxy_for_loopback(monkeypatch):
    # urllib binds its proxies when its opener is first built, which may be before this test, and
    # re-reads the bypass list on every request: so name loopback in it rather than only deleting
    # the proxy variables.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@pytest.fixture
def redirect_to_another_host(monkeypatch):
    """Two loopback servers on different ports: the first redirects every request to the second."""
    _no_proxy_for_loopback(monkeypatch)
    servers = []
    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.seen, server.target = [], None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    first, second = servers
    first.target = f"http://127.0.0.1:{second.server_address[1]}/elsewhere"
    try:
        yield f"http://127.0.0.1:{first.server_address[1]}/start", first, second
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


CALLS = {
    "default_fetch": lambda url, h: pinmod.default_fetch(url, h, timeout=10),
    "_response_headers": lambda url, h: pinmod._response_headers(url, h, timeout=10),
    "_json_call": lambda url, h: pinmod._json_call(url, h, b"url=x", timeout=10),
}


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_an_authorization_header_does_not_follow_a_redirect(call, redirect_to_another_host):
    url, first, second = redirect_to_another_host
    call(url, {"Authorization": f"Token {SENTINEL}", "X-Other": "kept"})
    [asked], [redirected] = first.seen, second.seen
    assert asked["Authorization"] == f"Token {SENTINEL}"  # the host that was asked got it
    assert "Authorization" not in redirected  # the host the redirect named did not
    # The control: an ordinary header does follow (a 302 turns _json_call's POST into a GET and
    # urllib keeps every header but Content-Length and Content-Type), so Authorization's absence
    # is the unredirection, not a redirect that dropped everything.
    assert redirected.get("X-Other") == "kept"


@pytest.fixture
def redirect_to_the_same_host(monkeypatch):
    """One loopback server that redirects `/start` to its own `/landed`."""
    _no_proxy_for_loopback(monkeypatch)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    port = server.server_address[1]
    server.seen, server.target = [], f"http://127.0.0.1:{port}/landed"
    server.landed = "/landed"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/start", server
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_an_authorization_header_does_not_follow_a_redirect_to_the_same_host(
    call, redirect_to_the_same_host
):
    # The documented price of unredirecting: the same host is not trusted either.
    url, server = redirect_to_the_same_host
    call(url, {"Authorization": f"Token {SENTINEL}", "X-Other": "kept"})
    asked, redirected = server.seen
    assert asked["Authorization"] == f"Token {SENTINEL}"
    assert "Authorization" not in redirected
    assert redirected.get("X-Other") == "kept"


def _wire(build):
    """The bytes urllib writes for the request `build` makes, header block and all, off loopback."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    got = {}

    def serve():
        conn, _ = listener.accept()
        with conn:
            raw = b""
            while b"\r\n\r\n" not in raw:
                raw += conn.recv(65536)
            head, _, body = raw.partition(b"\r\n\r\n")
            length = [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length:")]
            want = int(length[0].split(b":")[1]) if length else 0
            while len(body) < want:
                body += conn.recv(65536)
            # Each capture has its own port; write it as PORT so two captures compare byte for byte.
            port = str(listener.getsockname()[1]).encode()
            got["bytes"] = (head + b"\r\n\r\n" + body).replace(b":" + port, b":PORT")
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\n{}"
            )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/api/rest/v4/clusters/1481640/"
    with urllib.request.urlopen(build(url), timeout=10) as resp:
        resp.read()
    thread.join(timeout=10)
    listener.close()
    return got["bytes"]


# (what the old code built, what `_request` builds), for each shape of request the package sends.
WIRE = {
    "GET with a token (default_fetch, courtlistener)": (
        lambda u, h: urllib.request.Request(u, headers={**pinmod._BASE_HEADERS, **h}),
        lambda u, h: pinmod._request(u, h),
        {"Authorization": f"Token {SENTINEL}"},
    ),
    "POST with a token and a Content-Type (_json_call, SPN2)": (
        lambda u, h: urllib.request.Request(
            u, data=b"url=x", headers={"User-Agent": pinmod._UA, **h}
        ),
        lambda u, h: pinmod._request(u, h, base={"User-Agent": pinmod._UA}, data=b"url=x"),
        {
            "Authorization": "LOW access:secret",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    ),
    "POST with a token and no Content-Type": (
        lambda u, h: urllib.request.Request(
            u, data=b"url=x", headers={"User-Agent": pinmod._UA, **h}
        ),
        lambda u, h: pinmod._request(u, h, base={"User-Agent": pinmod._UA}, data=b"url=x"),
        {"Authorization": "LOW access:secret"},
    ),
    "GET with no credential": (
        lambda u, h: urllib.request.Request(u, headers={**pinmod._BASE_HEADERS, **h}),
        lambda u, h: pinmod._request(u, h),
        {},
    ),
}


@pytest.mark.parametrize(("old", "new", "headers"), WIRE.values(), ids=WIRE.keys())
def test_the_first_request_goes_on_the_wire_byte_for_byte_as_before(old, new, headers, monkeypatch):
    _no_proxy_for_loopback(monkeypatch)
    before = _wire(lambda url: old(url, headers))
    after = _wire(lambda url: new(url, headers))
    assert after == before  # every byte, Host included
    assert b"\r\nHost: 127.0.0.1:PORT\r\n" in after
    for name, value in headers.items():
        assert f"{name}: {value}".encode() in after
