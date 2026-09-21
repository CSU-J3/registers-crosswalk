"""The pinning console: the same guards as `add`, reached over HTTP.

Offline throughout. Every test stands a real server up on port 0 in a thread and talks to it with
`urllib`, so what is exercised is the handler and the routing, not a hand-called function. The
fetch and the archive are fakes; the courtlistener ones serve the responses captured live on
2026-09-20 under `tests/fixtures/`.

The sentinel discipline: the fake `.env` holds values that appear nowhere else in the repo, and
several tests assert those values are absent from every byte the server sends. A console that
leaked a key into its own page would be a worse tool than no console.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from registers_crosswalk import console as consolemod
from registers_crosswalk.console import PACING, Console, load_dotenv, paced, serve
from registers_crosswalk.models import ArchiveCopy
from registers_crosswalk.pin import ArchiveFailure, sha256_hex, to_ledger_markdown
from registers_crosswalk.registry import Crosswalk

FIXTURES = Path(__file__).parent / "fixtures"

# Values that exist nowhere else in this repository. If one of these turns up in a response body,
# the console put it there.
SENTINEL_TOKEN = "cl-sentinel-8Qv2rX-do-not-leak"
SENTINEL_FEC = "fec-sentinel-7Lm9pW-do-not-leak"
SENTINELS = (SENTINEL_TOKEN, SENTINEL_FEC)

ENV = {"COURTLISTENER_TOKEN": SENTINEL_TOKEN, "OPENFEC_API_KEY": SENTINEL_FEC}

CL_CLUSTER = "courtlistener_cluster_1481640"
CL_OPINION = "courtlistener_opinion_1481640"
CL_DOCKET = "courtlistener_docket_2577633"
CL_COURT = "courtlistener_court_ca8"
CL_SEARCH = "courtlistener_search_dunne-v-united-states"

DUNNE_PDF = "https://storage.courtlistener.com/harvard_pdf/1481640.pdf"
PDF_BYTES = b"%PDF-1.4 dunne"


def load_fixture(stem: str) -> dict:
    """Same discipline as tests/test_fetchers.py: captured, dated, never authored."""
    matches = sorted(FIXTURES.glob(f"{stem}_*.json"))
    if not matches:
        raise AssertionError(f"no captured fixture for {stem!r} under {FIXTURES}")
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def cl_fetch(cluster=None, document=PDF_BYTES):
    """Route every call the console makes for courtlistener to a captured response.

    The four `spec()` calls plus the search, plus the document fetch `pin()` does — which is not
    an API call and is served from `storage.courtlistener.com`, a different host, which is also
    why the pacing test cares about the two separately.
    """
    payloads = {
        "/search/": load_fixture(CL_SEARCH),
        "/clusters/": load_fixture(CL_CLUSTER) if cluster is None else cluster,
        "/opinions/": load_fixture(CL_OPINION),
        "/dockets/": load_fixture(CL_DOCKET),
        "/courts/": load_fixture(CL_COURT),
    }
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        if url.startswith("https://storage.courtlistener.com/"):
            return document, "application/pdf"
        for marker, payload in payloads.items():
            if marker in url:
                return json.dumps(payload).encode(), "application/json"
        raise AssertionError(f"unexpected URL: {url}")

    fetch.calls = calls
    return fetch


def fake_archive():
    calls = []

    def archive_fn(url, **_):
        calls.append(url)
        return ArchiveCopy(service="wayback", url=f"https://web.archive.org/web/1/{url}")

    archive_fn.calls = calls
    return archive_fn


class Client:
    """A running console and the three calls the page makes against it."""

    def __init__(self, console, server):
        self.console = console
        self.server = server
        self.port = server.server_address[1]

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def raw(self, path, *, token=None, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url(path), data=data, method=method)
        if token is not None:
            req.add_header("X-Console-Token", token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed localhost
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def get(self, path, **kw):
        status, text = self.raw(path, token=self.console.token, **kw)
        return status, json.loads(text)

    def post(self, path, body):
        return self.get(path, method="POST", body=body)

    def page(self):
        status, text = self.raw("/")
        assert status == 200
        return text

    def headers(self, path):
        """The response headers, which the CSP test needs and nothing else did."""
        req = urllib.request.Request(self.url(path))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed localhost
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers)


@pytest.fixture
def client(tmp_path):
    """A console over a temp data dir, serving in a background thread on an OS-assigned port."""
    (tmp_path / "sources").mkdir()
    console = Console(data_dir=tmp_path, env=ENV, fetch=cl_fetch(), archive_fn=fake_archive())
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(console, server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _resolve_dunne(client):
    status, body = client.post(
        "/api/resolve", {"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}}
    )
    assert status == 200, body
    return body


USCODE_PAGE = sorted(FIXTURES.glob("uscode_page_title1_section1_*.html"))[-1].read_bytes()


def _later_currency_date(page: bytes) -> bytes:
    """OLRC's site-wide date advanced, the source credit untouched. Same splice as test_pin_cli.

    This is what makes the second duplicate rule reachable: the currency date moves, so
    `point_in_time` moves, so `(canonical_url, point_in_time)` sees a new version where there is
    none — and only `same_document_of`, comparing last_amended, can tell that it is the same
    document.
    """
    moved = page.replace(
        b"laws in effect on September 17, 2026", b"laws in effect on December 1, 2026", 1
    )
    assert moved != page
    return moved


class _Serving:
    """A uscode server whose page the test can swap between one resolve and the next."""

    def __init__(self, page):
        self.page = page

    def __call__(self, url, headers=None):
        return self.page, "text/html"


def _uscode_console(tmp_path, serving):
    (tmp_path / "sources").mkdir()
    console = Console(data_dir=tmp_path, env=ENV, fetch=serving, archive_fn=fake_archive())
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return console, server, thread


# --------------------------------------------------------------------------- the token


def test_an_api_request_without_the_token_is_refused(client):
    status, _ = client.raw("/api/state")
    assert status == 401


def test_an_api_request_with_the_token_is_served(client):
    status, body = client.get("/api/state")
    assert status == 200
    assert [f["name"] for f in body["fetchers"]]


def test_a_wrong_token_is_refused(client):
    status, _ = client.raw("/api/state", token="not-the-token")
    assert status == 401


# --------------------------------------------------------------------------- state and keys


def test_state_lists_every_fetcher_with_its_verified_flag(client):
    _, body = client.get("/api/state")
    by_name = {f["name"]: f for f in body["fetchers"]}
    assert by_name["courtlistener"]["verified"] is True
    assert by_name["courtlistener"]["verified_at"] == date(2026, 9, 20).isoformat()
    assert by_name["openfec"]["verified"] is False
    assert by_name["uscode"]["requires_archive"] is True
    assert by_name["courtlistener"]["searchable"] is True
    assert by_name["ecfr"]["searchable"] is False


def test_state_reports_key_presence_by_name_and_never_by_value(client):
    _, body = client.get("/api/state")
    by_name = {f["name"]: f for f in body["fetchers"]}
    assert by_name["courtlistener"]["env_key"] == "COURTLISTENER_TOKEN"
    assert by_name["courtlistener"]["key_present"] is True
    # govinfo's key is not in this fake .env, so it reports absent rather than raising.
    assert by_name["govinfo"]["env_key"] == "GOVINFO_API_KEY"
    assert by_name["govinfo"]["key_present"] is False
    assert by_name["ecfr"]["env_key"] is None


def test_no_key_value_reaches_any_response_or_the_page(client):
    """The whole reason the console parses .env itself instead of exporting it."""
    bodies = [client.page(), client.raw("/api/state", token=client.console.token)[1]]
    _, search = client.raw(
        "/api/search?fetcher=courtlistener&type=opinions&q=Dunne", token=client.console.token
    )
    bodies.append(search)
    _, resolve = client.raw(
        "/api/resolve",
        token=client.console.token,
        method="POST",
        body={"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}},
    )
    bodies.append(resolve)
    for text in bodies:
        for sentinel in SENTINELS:
            assert sentinel not in text


# --------------------------------------------------------------------------- search


def test_search_returns_the_dunne_hit_with_an_add_command(client):
    status, body = client.get("/api/search?fetcher=courtlistener&type=opinions&q=Dunne")
    assert status == 200
    hits = {h["identifier"]: h for h in body["hits"]}
    assert "1481640" in hits
    assert hits["1481640"]["add_command"] == (
        "pin add courtlistener --cluster-id 1481640 --archive"
    )


def test_search_is_allowed_for_an_unverified_fetcher(client):
    """Search reads somebody else's index and writes nothing; it is resolve that is gated."""
    status, body = client.get("/api/search?fetcher=openfec&type=murs&q=anything")
    assert status != 409, body


def test_search_refuses_a_fetcher_that_has_none(client):
    status, body = client.get("/api/search?fetcher=ecfr&type=opinions&q=x")
    assert status == 400
    assert "no search" in body["error"]


# --------------------------------------------------------------------------- resolve


def test_resolve_returns_what_spec_decided(client):
    body = _resolve_dunne(client)
    assert body["pinnable"] is True
    assert body["grade"] == "B1"
    assert body["canonical_url"] == DUNNE_PDF
    assert body["citation"] == "138 F.2d 137"
    assert body["title"] == "Dunne v. United States"
    assert body["already_pinned"] is None
    assert body["resolve_id"]


def test_resolve_reports_a_document_already_pinned(client, tmp_path):
    first = _resolve_dunne(client)
    status, outcome = client.post("/api/pin", {"resolve_id": first["resolve_id"]})
    assert outcome["status"] == "written", outcome
    again = _resolve_dunne(client)
    assert again["already_pinned"] == outcome["xr_id"]


def test_resolve_says_so_when_the_cluster_has_no_document(tmp_path):
    """The one case where `pinnable` is false: spec() raises for want of anything to pin."""
    (tmp_path / "sources").mkdir()
    cluster = {**load_fixture(CL_CLUSTER), "filepath_pdf_harvard": None}
    console = Console(
        data_dir=tmp_path, env=ENV, fetch=cl_fetch(cluster=cluster), archive_fn=fake_archive()
    )
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Client(console, server)
        status, body = c.post(
            "/api/resolve", {"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}}
        )
        assert status == 200
        assert body["pinnable"] is False
        assert "no document" in body["message"]
        assert "resolve_id" not in body
        # and nothing can be pinned from it
        status, pinned = c.post("/api/pin", {"resolve_id": "anything"})
        assert pinned["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_resolve_refuses_an_unverified_fetcher(client):
    status, body = client.post("/api/resolve", {"fetcher": "openfec", "args": {"number": "1"}})
    assert status == 409
    assert body["error"] == (
        "fetcher openfec is unverified; its first live run goes through the terminal, "
        "per docs/operations.md"
    )


# --------------------------------------------------------------------------- pin


def test_pin_writes_one_record_and_returns_its_ledger(client, tmp_path):
    resolved = _resolve_dunne(client)
    status, body = client.post(
        "/api/pin", {"resolve_id": resolved["resolve_id"], "archive": True, "notes": "the ledger"}
    )
    assert status == 200
    assert body["status"] == "written"
    written = sorted((tmp_path / "sources").glob("xr_src_*.json"))
    assert len(written) == 1
    xw = Crosswalk(tmp_path)
    source = xw.sources[body["xr_id"]]
    assert body["ledger"] == to_ledger_markdown(source)
    assert source.notes == "the ledger"
    assert source.artifact.sha256 == sha256_hex(PDF_BYTES)
    assert client.console.archive_fn.calls == [DUNNE_PDF]


def test_pin_does_not_archive_when_the_box_is_unchecked(client):
    resolved = _resolve_dunne(client)
    status, body = client.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": False})
    assert body["status"] == "written", body
    assert client.console.archive_fn.calls == []
    assert body["archived"] is False


def test_pinning_the_same_document_twice_is_refused_in_the_cli_s_words(client):
    first = _resolve_dunne(client)
    status, written = client.post("/api/pin", {"resolve_id": first["resolve_id"]})
    assert written["status"] == "written"
    before = len(client.console.archive_fn.calls)

    second = _resolve_dunne(client)
    # The pre-fetch duplicate_of exit fires at resolve time too, so the page says so before the
    # button; the refusal below is the one add_source itself returns.
    assert second["already_pinned"] == written["xr_id"]
    status, refused = client.post("/api/pin", {"resolve_id": second["resolve_id"], "archive": True})
    assert status == 200
    assert refused["status"] == "refused"
    assert f"is already pinned as {written['xr_id']}" in refused["message"]
    # A refused pin costs no Save Page Now capture.
    assert len(client.console.archive_fn.calls) == before


def test_the_console_refuses_a_second_pin_of_an_unchanged_section(tmp_path):
    """The other duplicate rule, the one only `same_document_of` can catch.

    The currency date moves, so the URL rule waves the second pin through; the section was never
    amended, so it is one document about to be pinned twice. Reached through the console exactly
    as `pin add` reaches it, and refused in the CLI's own words.
    """
    serving = _Serving(USCODE_PAGE)
    console, server, thread = _uscode_console(tmp_path, serving)
    try:
        c = Client(console, server)
        args = {"fetcher": "uscode", "args": {"title": "1", "section": "1"}}
        _, first = c.post("/api/resolve", args)
        _, written = c.post("/api/pin", {"resolve_id": first["resolve_id"]})
        assert written["status"] == "written", written
        before = len(console.archive_fn.calls)

        serving.page = _later_currency_date(USCODE_PAGE)
        _, second = c.post("/api/resolve", args)
        # The URL rule does NOT catch this one: the point in time moved.
        assert second["already_pinned"] is None
        _, refused = c.post("/api/pin", {"resolve_id": second["resolve_id"]})
        assert refused["status"] == "refused"
        assert (
            f"1 U.S.C. § 1 with last_amended 2012-12-28 is already pinned "
            f"as {written['xr_id']}" in refused["message"]
        )
        assert (
            f"pass --supersedes {written['xr_id']} to chain a new pin anyway"
            in (refused["message"])
        )
        # A refused pin costs no Save Page Now capture, through the console as through the CLI.
        assert len(console.archive_fn.calls) == before
        assert len(sorted((tmp_path / "sources").glob("xr_src_*.json"))) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_uscode_pin_is_archived_even_when_the_body_says_otherwise(tmp_path):
    """REQUIRES_ARCHIVE outranks the checkbox: the console forces it rather than refusing."""
    (tmp_path / "sources").mkdir()
    page = (FIXTURES / "uscode_page_title1_section1_2026-09-18.html").read_bytes()

    def fetch(url, headers=None):
        return page, "text/html"

    archive_fn = fake_archive()
    console = Console(data_dir=tmp_path, env=ENV, fetch=fetch, archive_fn=archive_fn)
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Client(console, server)
        status, resolved = c.post(
            "/api/resolve", {"fetcher": "uscode", "args": {"title": "1", "section": "1"}}
        )
        assert status == 200, resolved
        assert resolved["requires_archive"] is True
        status, body = c.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": False})
        assert body["status"] == "written", body
        assert len(archive_fn.calls) == 1
        assert body["archived"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- pacing


def test_pacing_holds_courtlistener_to_five_api_calls_a_minute():
    """A fake clock, so the test asserts the spacing without spending it."""
    now = [0.0]
    slept = []

    def clock():
        return now[0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    def fetch(url, headers=None):
        return b"{}", "application/json"

    wrapped = paced(fetch, clock=clock, sleep=sleep)
    for _ in range(3):
        wrapped("https://www.courtlistener.com/api/rest/v4/clusters/1/")
    assert slept == [12.0, 12.0]
    assert now[0] == 24.0


def test_pacing_leaves_the_document_host_alone():
    """storage.courtlistener.com is not the API and is not counted against the quota."""
    slept = []

    def fetch(url, headers=None):
        return b"x", "application/pdf"

    wrapped = paced(fetch, clock=lambda: 0.0, sleep=slept.append)
    for _ in range(5):
        wrapped("https://storage.courtlistener.com/harvard_pdf/1481640.pdf")
    assert slept == []


def test_pacing_does_not_wait_when_the_interval_has_already_passed():
    now = [0.0]
    slept = []

    def fetch(url, headers=None):
        return b"{}", "application/json"

    wrapped = paced(fetch, clock=lambda: now[0], sleep=slept.append)
    wrapped("https://www.courtlistener.com/api/rest/v4/clusters/1/")
    now[0] = 30.0
    wrapped("https://www.courtlistener.com/api/rest/v4/clusters/2/")
    assert slept == []


# --------------------------------------------------------------------------- .env parsing


def test_load_dotenv_reads_pairs_and_ignores_the_rest(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "COURTLISTENER_TOKEN=plain",
                'GOVINFO_API_KEY="quoted"',
                "OPENFEC_API_KEY='single'",
                "export WAYBACK_ACCESS_KEY=exported",
                "not a pair",
                "=novalue",
            ]
        ),
        encoding="utf-8",
    )
    values = load_dotenv(path)
    assert values == {
        "COURTLISTENER_TOKEN": "plain",
        "GOVINFO_API_KEY": "quoted",
        "OPENFEC_API_KEY": "single",
        "WAYBACK_ACCESS_KEY": "exported",
    }


def test_load_dotenv_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_load_dotenv_never_touches_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    path = tmp_path / ".env"
    path.write_text(f"COURTLISTENER_TOKEN={SENTINEL_TOKEN}\n", encoding="utf-8")
    import os

    load_dotenv(path)
    assert os.environ.get("COURTLISTENER_TOKEN") is None


# --------------------------------------------------------------------------- the page


def test_the_page_carries_the_token_and_the_fetcher_chips(client):
    page = client.page()
    assert client.console.token in page
    assert "Pinning console" in page
    assert "Local only." in page
    assert "courtlistener" in page and "verified 2026-09-20" in page
    assert "unverified" in page


def test_the_page_never_renders_api_json_as_markup(client):
    """`<` is escaped inside the JSON block, so no value can close the script element."""
    page = client.page()
    start = page.index('<script type="application/json" id="s">')
    end = page.index("</script>", start)
    assert "<" not in page[start + len('<script type="application/json" id="s">') : end]


# ------------------------------------------------------- archiving a pin that has none


def test_api_archive_adds_a_capture_through_the_same_function_the_cli_uses(client, tmp_path):
    """Pin without an archive, then archive it from the page. One code path, two front ends."""
    resolved = _resolve_dunne(client)
    _, written = client.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": False})
    assert written["status"] == "written"
    assert written["archived"] is False
    assert client.console.archive_fn.calls == []

    status, body = client.post("/api/archive", {"xr_id": written["xr_id"]})
    assert status == 200
    assert body["status"] == "written", body
    assert body["message"].startswith(f"archived {written['xr_id']}: ")
    assert client.console.archive_fn.calls == [DUNNE_PDF]

    source = Crosswalk(tmp_path).sources[written["xr_id"]]
    assert source.archives[0].url.startswith("https://web.archive.org/web/1/")


def test_api_archive_refuses_a_pin_that_already_has_one(client):
    resolved = _resolve_dunne(client)
    _, written = client.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": True})
    assert written["archived"] is True
    before = len(client.console.archive_fn.calls)

    status, body = client.post("/api/archive", {"xr_id": written["xr_id"]})
    assert status == 200
    assert body["status"] == "refused"
    assert "already has an archive copy" in body["message"]
    assert len(client.console.archive_fn.calls) == before  # and no capture was requested


def test_api_archive_refuses_an_unknown_id(client):
    status, body = client.post("/api/archive", {"xr_id": "xr_src_0404"})
    assert status == 200
    assert body["status"] == "refused"
    assert body["message"] == "unknown source 'xr_src_0404'"


def test_api_archive_reports_the_reason_when_the_capture_fails(tmp_path):
    (tmp_path / "sources").mkdir()

    def failing(url, **_):
        return ArchiveFailure("HTTP 429 from the Wayback Machine for " + url)

    console = Console(data_dir=tmp_path, env=ENV, fetch=cl_fetch(), archive_fn=failing)
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Client(console, server)
        _, resolved = c.post(
            "/api/resolve", {"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}}
        )
        _, written = c.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": False})
        assert written["status"] == "written"
        _, body = c.post("/api/archive", {"xr_id": written["xr_id"]})
        assert body["status"] == "archive_failed"
        assert body["message"] == (
            "archive step failed: HTTP 429 from the Wayback Machine for " + DUNNE_PDF
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_archive_needs_the_token(client):
    status, _ = client.raw("/api/archive", method="POST", body={"xr_id": "xr_src_0001"})
    assert status == 401


def test_state_says_which_pins_have_no_archive(client):
    resolved = _resolve_dunne(client)
    _, written = client.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": False})
    _, state_body = client.get("/api/state")
    pins = {p["xr_id"]: p for p in state_body["pins"]}
    assert pins[written["xr_id"]]["archived"] is False

    client.post("/api/archive", {"xr_id": written["xr_id"]})
    _, after = client.get("/api/state")
    assert {p["xr_id"]: p for p in after["pins"]}[written["xr_id"]]["archived"] is True


def test_the_page_carries_the_unarchived_list(client):
    page = client.page()
    assert 'id="unarchived-card"' in page
    assert "Pins with no archive copy" in page
    assert "Archive now" in page
    assert "/api/archive" in page


def _write_source(data_dir: Path, spec: dict) -> None:
    """One pinned record on disk, in the shape `Crosswalk` loads. Same helper as test_status."""
    spec = dict(spec)
    xr_id = spec.pop("xr_id")
    digest = xr_id[-1] * 64
    record = {
        "xr_id": xr_id,
        "kind": "source",
        "publisher": "Office of the Federal Register",
        "fetcher_verified": True,
        "verified_at": "2026-09-18",
        "artifact": {
            "sha256": digest,
            "byte_length": 10,
            "media_type": spec.pop("media_type", "application/xml"),
            "fetched_at": "2026-09-18T18:56:07Z",
            "drift_key": "sha256",
            "drift_value": digest,
        },
        "grade": {"reliability": "A", "credibility": 1},
        **spec,
    }
    (data_dir / "sources" / f"{xr_id}.json").write_text(json.dumps(record), encoding="utf-8")


# An eCFR pin, whose title is its citation plus the version qualifier, and a Federal Register pin,
# whose title is the document's own name. The two shapes the row has to tell apart.
ECFR_PIN = {
    "xr_id": "xr_src_9001",
    "citation": "5 CFR 2640.202",
    "title": "5 CFR 2640.202, as of 2026-09-15",
    "canonical_url": "https://www.ecfr.gov/api/versioner/v1/full/2026-09-15/title-5.xml"
    "?part=2640&section=2640.202",
    "fetcher": "ecfr",
    "point_in_time": "2026-09-15",
}
FR_PIN = {
    "xr_id": "xr_src_9002",
    "citation": "60 FR 7862",
    "title": "Expenditures; Reports by Political Committees",
    "canonical_url": "https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf",
    "fetcher": "federalregister",
    "published_at": "1995-02-09",
    "media_type": "application/pdf",
}


def test_an_unarchived_row_is_told_whether_its_title_says_anything(client):
    """The row printed "5 CFR 2640.202" and then "5 CFR 2640.202, as of 2026-09-15" under it.

    A `title !== citation` test in the browser is true for every eCFR and uscode pin, because that
    is how those fetchers build a title. The predicate that knows better is the one the status
    page's document cell already uses, and the answer travels with the pin rather than being
    guessed at again on the page.
    """
    _write_source(client.console.data_dir, ECFR_PIN)
    _write_source(client.console.data_dir, FR_PIN)
    _, body = client.get("/api/state")
    rows = {p["xr_id"]: p for p in body["pins"]}

    assert rows["xr_src_9001"]["title"] == "5 CFR 2640.202, as of 2026-09-15"
    assert rows["xr_src_9001"]["title_adds_anything"] is False
    assert rows["xr_src_9002"]["title_adds_anything"] is True
    # and the row is drawn from that flag, not from a comparison of its own
    page = client.page()
    assert "pin.title_adds_anything" in page
    assert "pin.title !== pin.citation" not in page


# ------------------------------------------------------- the page re-reads after it writes


def _nothing_committed_yet(data_dir):
    """What `git status --porcelain` says about a sources dir with no commit behind it.

    The temp data dir is outside the repo, where git has nothing to say about it at all. Standing
    in for git here keeps the test offline and holds the line either side of it: what the pin puts
    in the state, and what the footer is drawn from.
    """
    records = sorted((data_dir / "sources").glob("*.json"))
    return {"branch": "main", "pending": [f"data/sources/{p.name}" for p in records]}


def test_a_pin_lands_in_the_state_the_page_reads_back(client, monkeypatch):
    """After a pin the page re-reads /api/state. This is what that read has to contain."""
    monkeypatch.setattr(consolemod, "_git_state", _nothing_committed_yet)
    _, before = client.get("/api/state")
    assert before["pins"] == []
    assert before["git"]["pending"] == []

    resolved = _resolve_dunne(client)
    _, written = client.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": False})
    assert written["status"] == "written"

    _, after = client.get("/api/state")
    assert [p["xr_id"] for p in after["pins"] if not p["archived"]] == [written["xr_id"]]
    assert after["git"]["pending"] == [f"data/sources/{written['xr_id']}.json"]


def test_both_writes_redraw_the_page_from_that_state(client):
    """The pin path ended without a re-read, so the page kept saying what was true before it.

    Asserted on the page source, like the other browser-side rules here: the new pin was missing
    from the unarchived list and the footer still reported nothing uncommitted until the operator
    reloaded by hand. Both writes now end in the same re-read, and the footer is drawn from it.
    """
    page = client.page()
    assert page.count("refreshState();") == 2  # one after a pin, one after an archive
    assert "function refreshState()" in page
    assert "drawFoot(r.body.git)" in page
    # The footer is the script's, from the same state, so there is one renderer of it and not two.
    assert '<footer class="foot" id="foot"></footer>' in page
    assert "' under data/sources/ on branch '" in page


# ------------------------------------------------------- the wait has a reason on screen


def test_a_paced_resolve_reports_which_call_it_is_waiting_on(tmp_path):
    """The forty seconds CourtListener costs used to look exactly like a hang.

    Console builds its own paced fetch here, which is the wiring under test. The clock is fake and
    the sleep records instead of waiting — and because the wait is announced BEFORE the sleep, the
    sleep is exactly the moment to read the record back through /api/progress, which is the same
    endpoint and the same message the page polls for.
    """
    (tmp_path / "sources").mkdir()
    now = [0.0]
    announced = []
    console = None

    def sleep(seconds):
        # what the page would be showing while this wait is happening
        _, record = console.api_progress({"id": ["p1"]})
        announced.append((record.get("message"), seconds))
        now[0] += seconds

    console = Console(
        data_dir=tmp_path,
        env=ENV,
        fetch=cl_fetch(),
        archive_fn=fake_archive(),
        pacing=PACING,
        clock=lambda: now[0],
        sleep=sleep,
    )
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Client(console, server)
        status, body = c.post(
            "/api/resolve",
            {"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}, "progress_id": "p1"},
        )
        assert status == 200, body
        assert body["pinnable"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # spec() makes four API calls; the first needs no wait, the other three do.
    assert announced == [
        ("call 2 of 4, waiting 12 s for CourtListener's rate limit", 12.0),
        ("call 3 of 4, waiting 12 s for CourtListener's rate limit", 12.0),
        ("call 4 of 4, waiting 12 s for CourtListener's rate limit", 12.0),
    ]


def test_the_first_call_of_a_resolve_reports_no_wait(tmp_path):
    """Nothing to wait for yet, so the message is the position alone."""
    (tmp_path / "sources").mkdir()
    console = Console(data_dir=tmp_path, env=ENV, fetch=cl_fetch(), archive_fn=fake_archive())
    console._local.progress_id = "p2"
    console._progress["p2"] = {"calls": 0, "fetcher": "courtlistener"}
    console.note_call("www.courtlistener.com", 0.0)
    _, record = console.api_progress({"id": ["p2"]})
    assert record["message"] == "call 1 of 4"
    assert record["waiting"] is False


def test_the_progress_record_is_dropped_when_the_resolve_ends(tmp_path):
    """It is scratch state for one request, not something the server accumulates."""
    (tmp_path / "sources").mkdir()
    console = Console(data_dir=tmp_path, env=ENV, fetch=cl_fetch(), archive_fn=fake_archive())
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Client(console, server)
        c.post(
            "/api/resolve",
            {"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}, "progress_id": "p9"},
        )
        status, body = c.get("/api/progress?id=p9")
        assert status == 200
        assert body == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_progress_needs_the_token_like_every_other_api_call(client):
    status, _ = client.raw("/api/progress?id=anything")
    assert status == 401


def test_an_unpaced_host_is_reported_without_a_wait():
    """The count still moves for a host with no rate limit; there is just nothing to wait for."""
    seen = []
    wrapped = paced(
        lambda u, h=None: (b"x", "application/pdf"),
        clock=lambda: 0.0,
        sleep=lambda _s: None,
        pacing=PACING,
        on_call=lambda host, wait: seen.append((host, wait)),
    )
    wrapped("https://storage.courtlistener.com/harvard_pdf/1481640.pdf")
    assert seen == [("storage.courtlistener.com", 0.0)]


# ------------------------------------------------------- the keys actually reach the archiver


SENTINEL_WB_ACCESS = "wb-access-sentinel-5Kd3vN"
SENTINEL_WB_SECRET = "wb-secret-sentinel-6Tj8qB"
ENV_WITH_WAYBACK = {
    **ENV,
    "WAYBACK_ACCESS_KEY": SENTINEL_WB_ACCESS,
    "WAYBACK_SECRET_KEY": SENTINEL_WB_SECRET,
}


def test_a_console_pin_archives_with_the_keys_from_its_own_env(tmp_path, monkeypatch):
    """The bug: Console defaulted archive_fn to the bare `archive`, which reads os.environ.

    `add_source` calls `archive_fn(url)` and passes nothing else, so `env` stayed None, `archive`
    looked in an environment this module deliberately never writes to, found no Wayback keys, and
    took the ANONYMOUS path — with the operator's keys sitting unused in `.env`. Every console pin
    failed that way.

    Driven end to end through /api/pin with the real `archive` underneath and only its transport
    faked, so what is asserted is the header that actually went out.
    """
    monkeypatch.delenv("WAYBACK_ACCESS_KEY", raising=False)
    monkeypatch.delenv("WAYBACK_SECRET_KEY", raising=False)
    seen = []

    def fake_json_call(url, headers=None, data=None, *, timeout=90):
        seen.append((url, dict(headers or {}), data))
        if data is not None:
            return {"job_id": "spn2-console"}
        return {"status": "success", "timestamp": "20260920190851"}

    def no_anonymous(url, headers=None, *, timeout=90):
        raise AssertionError(f"the anonymous Wayback path was taken for {url}")

    # The seams one level below `archive`: its own default transports. Patching here means the
    # console's default archive_fn — the thing under test — is the code that runs. The anonymous
    # one is stubbed to fail loudly rather than left to reach the network, so this test stays
    # offline whichever path the code takes, and names the bug if it takes the wrong one.
    monkeypatch.setattr("registers_crosswalk.pin._json_call", fake_json_call)
    monkeypatch.setattr("registers_crosswalk.pin._response_headers", no_anonymous)

    (tmp_path / "sources").mkdir()
    console = Console(data_dir=tmp_path, env=ENV_WITH_WAYBACK, fetch=cl_fetch())
    server = serve(console, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Client(console, server)
        _, resolved = c.post(
            "/api/resolve", {"fetcher": "courtlistener", "args": {"cluster_id": "1481640"}}
        )
        _, body = c.post("/api/pin", {"resolve_id": resolved["resolve_id"], "archive": True})
        assert body["status"] == "written", body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    post = next((call for call in seen if call[2] is not None), None)
    assert post is not None, "the keyed SPN2 POST never happened; the anonymous path was taken"
    url, headers, _ = post
    assert url == "https://web.archive.org/save"
    assert headers["Authorization"] == f"LOW {SENTINEL_WB_ACCESS}:{SENTINEL_WB_SECRET}"

    # and the environment was never written to on the way
    import os

    assert os.environ.get("WAYBACK_ACCESS_KEY") is None
    assert os.environ.get("WAYBACK_SECRET_KEY") is None


def test_the_console_archive_default_is_bound_to_its_own_env(tmp_path):
    """Belt and braces on the binding itself, so a refactor cannot silently unbind it."""
    (tmp_path / "sources").mkdir()
    console = Console(data_dir=tmp_path, env=ENV_WITH_WAYBACK, fetch=cl_fetch())
    assert console.archive_fn.keywords["env"] is console.env


def test_an_explicit_archive_fn_still_wins(tmp_path):
    """The tests' own fake archive must still override the default."""
    (tmp_path / "sources").mkdir()
    fake = fake_archive()
    console = Console(data_dir=tmp_path, env=ENV_WITH_WAYBACK, fetch=cl_fetch(), archive_fn=fake)
    assert console.archive_fn is fake


# ------------------------------------------------------- the policy the page runs under


def _csp(header: str) -> dict[str, list[str]]:
    """Content-Security-Policy into {directive: [sources]}."""
    out = {}
    for part in header.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


def test_the_csp_lets_the_page_call_its_own_api(client):
    """The bug that made the console do nothing at all on its first run.

    `default-src 'none'` covers fetch(), so without `connect-src 'self'` the browser blocked every
    request the page makes to /api/ — and blocked it silently: the page sat on 'Searching...' and
    the only evidence was a CSP line in the Network tab. The server was fine, the tests were
    green, and nothing worked. Parsed rather than matched as a substring so that reordering or
    rewording the policy cannot pass this by accident.
    """
    status, headers = client.headers("/")
    assert status == 200
    policy = _csp(headers["Content-Security-Policy"])
    assert policy["connect-src"] == ["'self'"]


def test_the_csp_is_still_strict_everywhere_else(client):
    """Widening connect-src must not widen the rest: the page loads nothing off any machine."""
    _, headers = client.headers("/")
    policy = _csp(headers["Content-Security-Policy"])
    assert policy["default-src"] == ["'none'"]
    assert policy["img-src"] == ["'self'"]
    assert policy["form-action"] == ["'none'"]
    assert policy["base-uri"] == ["'none'"]
    # No remote origin is reachable from this page, by any directive.
    for sources in policy.values():
        for source in sources:
            assert "//" not in source, f"{source} would reach off this machine"


def test_the_csp_is_sent_on_api_responses_too(client):
    status, headers = client.headers("/api/state")
    assert status == 401
    assert "connect-src 'self'" in headers["Content-Security-Policy"]


def test_favicon_is_an_empty_204(client):
    """Browsers ask for it unprompted; 204 keeps one red line out of the console on every load."""
    status, headers = client.headers("/favicon.ico")
    assert status == 204
    assert headers.get("Content-Length") == "0"


# ------------------------------------------------------- the page's own defaults and failure text


def test_the_search_select_defaults_to_a_verified_fetcher(client):
    """Module order puts openfec first — unverified, keyless, and unable to pin.

    Only the default moves; the options stay in module order. If openfec is ever verified it
    becomes the default again by being first, with no change here.
    """
    page = client.page()
    start = page.index('<select id="search-fetcher">')
    end = page.index("</select>", start)
    block = page[start:end]
    assert 'value="openfec"' in block and 'value="courtlistener"' in block
    assert block.index('value="openfec"') < block.index('value="courtlistener"')
    assert 'value="courtlistener" selected' in block
    assert 'value="openfec" selected' not in block


def test_every_api_caller_handles_a_request_that_never_landed(client):
    """A rejected fetch used to leave the last status line on screen with no reason given.

    api() resolves only when a response arrived; a request that never got one rejects. Asserted on
    the page source because the three callers live in the browser, and an offline test can still
    hold the line that none of them is left without a catch.
    """
    page = client.page()
    # Every api() call site chains a catch. Four report the error to the operator — search,
    # resolve, pin, archive — and two are deliberately silent: the progress tick and the
    # state re-read, where a lost poll is not news and the operation that matters
    # reports its own end.
    assert page.count(".catch(function (err)") == 4
    assert page.count(".catch(function ()") == 2
    assert "function failed(node, err)" in page
    assert "'request failed: '" in page


def test_an_unknown_path_is_404(client):
    status, _ = client.raw("/nope")
    assert status == 404


# --------------------------------------------------------------------------- the CLI's messages


def test_the_console_reaches_add_through_the_same_function_the_cli_uses():
    """Working rule 5, asserted rather than assumed: one function, not two code paths."""
    import inspect

    source = inspect.getsource(consolemod.Console.api_pin)
    assert "add_source(" in source
    # and nothing in the console re-implements a refusal
    whole = Path(consolemod.__file__).read_text(encoding="utf-8")
    for message in ("refusing to overwrite", "is already pinned as", "archive step failed"):
        assert message not in whole
