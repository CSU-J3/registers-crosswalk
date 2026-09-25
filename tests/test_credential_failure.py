"""When api.data.gov refuses the key, the operator is told so in one line, and the run stops.

`CREDENTIAL FAILURE <fetcher> <status> <code>` on stderr and exit 4, from `pin add` and `pin
search`; the same line in the console's pane and on its terminal. Before this, a disabled, an
invalid and a missing key were each a traceback ending `HTTP Error 403: Forbidden`, byte-identical,
with the code that told them apart left unread in the body.

Offline: `urllib.request.urlopen` is replaced by one that answers the way api.data.gov does, so the
real `default_fetch` runs, gzip handling included. The bodies are api.data.gov's documented error
shape; the messages are the ones it sends.
"""

import email.message
import gzip
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from registers_crosswalk import apikey
from registers_crosswalk.apikey import CredentialFailure, keyed_fetch
from registers_crosswalk.console import Console
from registers_crosswalk.pin import EXIT_CREDENTIAL_FAILURE, main

SENTINEL = "credential-sentinel-3Hq8Zt-do-not-leak"
ENV = {"GOVINFO_API_KEY": SENTINEL, "OPENFEC_API_KEY": SENTINEL}

MESSAGES = {
    "API_KEY_DISABLED": "The api_key supplied has been disabled. Please contact us for assistance.",
    "API_KEY_INVALID": "An invalid api_key was supplied. Get one at https://api.data.gov:443",
    "API_KEY_MISSING": "No api_key was supplied. Get one at https://api.data.gov:443",
}

# (status, body code or None) -> the code the line must name
REFUSALS = [
    pytest.param(403, "API_KEY_DISABLED", id="403-disabled"),
    pytest.param(403, "API_KEY_INVALID", id="403-invalid"),
    pytest.param(403, "API_KEY_MISSING", id="403-missing"),
    pytest.param(401, None, id="bare-401"),
]

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


def _body(code, *, gzipped=False):
    if code is None:
        return b"", {}
    raw = json.dumps({"error": {"code": code, "message": MESSAGES.get(code, "")}}).encode()
    if gzipped:
        return gzip.compress(raw), {"Content-Encoding": "gzip"}
    return raw, {}


@pytest.fixture
def api_data_gov(monkeypatch):
    """Make every request answer with the refusal set on the returned object, and count them."""

    class Refuse:
        status = 403
        code = "API_KEY_DISABLED"
        gzipped = False
        calls: list[str] = []

        def urlopen(self, req, *args, **kwargs):
            self.calls.append(req.full_url)
            body, extra = _body(self.code, gzipped=self.gzipped)
            headers = email.message.Message()
            headers["Content-Type"] = "application/json"
            for name, value in extra.items():
                headers[name] = value
            reason = {401: "Unauthorized", 403: "Forbidden"}.get(self.status, "Error")
            raise urllib.error.HTTPError(
                req.full_url, self.status, reason, headers, io.BytesIO(body)
            )

    refuse = Refuse()
    refuse.calls = []
    monkeypatch.setattr(urllib.request, "urlopen", refuse.urlopen)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    return refuse


def _expect(fetcher, status, code):
    return f"CREDENTIAL FAILURE {fetcher} {status} {code or 'UNKNOWN'}\n"


# --------------------------------------------------------------------------- pin add / search


@pytest.mark.parametrize(("status", "code"), REFUSALS)
@pytest.mark.parametrize(("fetcher", "flags", "_args"), ADDS, ids=[a[0] for a in ADDS])
def test_pin_add_names_the_refusal_and_exits_4(
    fetcher, flags, _args, status, code, api_data_gov, tmp_path, capsys
):
    api_data_gov.status, api_data_gov.code = status, code
    assert main(["--data-dir", str(tmp_path), "add", fetcher, *flags]) == EXIT_CREDENTIAL_FAILURE
    out, err = capsys.readouterr()
    assert (out, err) == ("", _expect(fetcher, status, code))
    assert len(api_data_gov.calls) == 1  # not retried
    assert not (tmp_path / "sources").exists() or not any((tmp_path / "sources").iterdir())


@pytest.mark.parametrize(("status", "code"), REFUSALS)
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_pin_search_names_the_refusal_and_exits_4(status, code, as_json, api_data_gov, capsys):
    api_data_gov.status, api_data_gov.code = status, code
    argv = ["search", "openfec", "Osborn", *(["--json"] if as_json else [])]
    assert main(argv) == EXIT_CREDENTIAL_FAILURE
    out, err = capsys.readouterr()
    assert (out, err) == ("", _expect("openfec", status, code))
    assert len(api_data_gov.calls) == 1


def test_a_gzip_encoded_error_body_is_still_read(api_data_gov, tmp_path, capsys):
    # default_fetch asks for gzip, so api.data.gov may answer an error compressed.
    api_data_gov.code, api_data_gov.gzipped = "API_KEY_INVALID", True
    argv = ["--data-dir", str(tmp_path), "add", "govinfo", "--package", "X"]
    assert main(argv) == EXIT_CREDENTIAL_FAILURE
    assert capsys.readouterr().err == "CREDENTIAL FAILURE govinfo 403 API_KEY_INVALID\n"


def test_an_api_key_code_names_the_failure_whatever_the_status(api_data_gov, capsys):
    api_data_gov.status, api_data_gov.code = 400, "API_KEY_MISSING"
    assert main(["search", "openfec", "Osborn"]) == EXIT_CREDENTIAL_FAILURE
    assert capsys.readouterr().err == "CREDENTIAL FAILURE openfec 400 API_KEY_MISSING\n"


@pytest.mark.parametrize("code", ["api key disabled", "<b>x</b>", "API_KEY_" + "X" * 80, 7])
def test_only_a_code_shaped_value_is_echoed_from_the_body(code, api_data_gov, capsys):
    api_data_gov.code = code
    assert main(["search", "openfec", "Osborn"]) == EXIT_CREDENTIAL_FAILURE
    assert capsys.readouterr().err == "CREDENTIAL FAILURE openfec 403 UNKNOWN\n"


@pytest.mark.parametrize(
    ("status", "code"), [(500, None), (429, "OVER_RATE_LIMIT"), (404, "NOT_FOUND")]
)
def test_anything_else_is_not_called_a_credential_failure(status, code, api_data_gov, capsys):
    api_data_gov.status, api_data_gov.code = status, code
    with pytest.raises(urllib.error.HTTPError) as caught:
        main(["search", "openfec", "Osborn"])
    assert caught.value.code == status
    assert "CREDENTIAL FAILURE" not in capsys.readouterr().err


def test_the_failure_carries_no_key_and_no_chain(api_data_gov):
    from registers_crosswalk.fetchers import openfec

    with pytest.raises(CredentialFailure) as caught:
        openfec.search("Osborn", doc_type="murs", env=ENV)
    exc = caught.value
    assert (exc.fetcher, exc.status, exc.code) == ("openfec", 403, "API_KEY_DISABLED")
    assert exc.__suppress_context__ and exc.__cause__ is None
    assert SENTINEL not in repr(vars(exc)) + str(exc) + repr(exc.__context__.url)


def test_python_m_names_the_refusal_and_exits_4(tmp_path):
    # The real entry point, in a child process: `python -m registers_crosswalk.pin`.
    script = (
        "import io, runpy, sys, urllib.error, urllib.request, email.message\n"
        "def urlopen(req, *a, **k):\n"
        "    h = email.message.Message(); h['Content-Type'] = 'application/json'\n"
        '    body = b\'{"error": {"code": "API_KEY_DISABLED"}}\'\n'
        "    raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', h, io.BytesIO(body))\n"
        "urllib.request.urlopen = urlopen\n"
        f"sys.argv = ['pin', '--data-dir', {str(tmp_path)!r}, 'add', 'fecfiling',"
        " '--file-number', '1903438', '--document', 'fec']\n"
        "runpy.run_module('registers_crosswalk.pin', run_name='__main__', alter_sys=True)\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["OPENFEC_API_KEY"] = SENTINEL
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert (done.returncode, done.stdout) == (EXIT_CREDENTIAL_FAILURE, "")
    assert done.stderr == "CREDENTIAL FAILURE fecfiling 403 API_KEY_DISABLED\n"


def test_pin_add_with_no_key_is_one_line_and_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENFEC_API_KEY", raising=False)
    argv = ["--data-dir", str(tmp_path), "add", "openfec", "--number", "8098"]
    assert main(argv) == 2
    assert capsys.readouterr().err == "openfec: environment variable OPENFEC_API_KEY is not set\n"


# --------------------------------------------------------------------------- the console


def _console(tmp_path):
    (tmp_path / "sources").mkdir(exist_ok=True)

    def no_archive(url, **_):
        raise AssertionError(f"resolve and search never archive: {url}")

    # The console's fetch is the real default_fetch, so the patched urlopen answers it.
    from registers_crosswalk.pin import default_fetch

    return Console(data_dir=tmp_path, env=ENV, fetch=default_fetch, archive_fn=no_archive)


@pytest.mark.parametrize(("status", "code"), REFUSALS)
@pytest.mark.parametrize(("fetcher", "_flags", "args"), ADDS, ids=[a[0] for a in ADDS])
def test_console_resolve_shows_the_line_in_the_pane_and_on_the_terminal(
    fetcher, _flags, args, status, code, api_data_gov, tmp_path, capsys
):
    api_data_gov.status, api_data_gov.code = status, code
    got = _console(tmp_path).api_resolve({"fetcher": fetcher, "args": args})
    line = _expect(fetcher, status, code)
    assert got == (502, {"error": line.rstrip("\n")})
    out, err = capsys.readouterr()
    assert (out, err) == ("", line)


@pytest.mark.parametrize(("status", "code"), REFUSALS)
def test_console_search_shows_the_line_in_the_pane_and_on_the_terminal(
    status, code, api_data_gov, tmp_path, capsys
):
    api_data_gov.status, api_data_gov.code = status, code
    query = {"fetcher": ["openfec"], "q": ["Osborn"], "type": ["murs"]}
    got = _console(tmp_path).api_search(query)
    line = _expect("openfec", status, code)
    assert got == (502, {"error": line.rstrip("\n")})
    assert capsys.readouterr() == ("", line)


# --------------------------------------------------------------------------- the rule's edges


def _refusing(status, fp):
    def fetch(url, headers=None):
        h = email.message.Message()
        h["Content-Type"] = "application/json"
        raise urllib.error.HTTPError(url, status, "Forbidden", h, fp)

    return fetch


@pytest.mark.parametrize("status", [401, 403])
def test_a_bare_refusal_from_a_host_that_takes_no_key_stays_an_http_error(status):
    # Only api.govinfo.gov and api.open.fec.gov take the key, so only there does a bare 401 or 403
    # say the key was refused. Anywhere else it is the HTTPError it was, redacted.
    with pytest.raises(urllib.error.HTTPError) as caught:
        keyed_fetch(
            _refusing(status, io.BytesIO(b"")),
            "https://www.fec.gov/x",
            {},
            key=SENTINEL,
            fetcher="openfec",
        )
    assert not isinstance(caught.value, CredentialFailure)
    assert caught.value.code == status and SENTINEL not in caught.value.url


class _Counting(io.RawIOBase):
    def __init__(self, data):
        self._data, self.pos = memoryview(data), 0

    def readable(self):
        return True

    def readinto(self, b):
        n = min(len(b), len(self._data) - self.pos)
        b[:n] = self._data[self.pos : self.pos + n]
        self.pos += n
        return n


def test_the_error_body_is_read_no_further_than_the_limit():
    # The code sits behind padding that runs past the limit, so it must go unread: a body from
    # somewhere is never read whole into memory to look for four words.
    pad = "x" * (4 * apikey._BODY_LIMIT)
    body = json.dumps({"pad": pad, "error": {"code": "API_KEY_INVALID"}}).encode()
    raw = _Counting(body)
    with pytest.raises(urllib.error.HTTPError) as caught:
        keyed_fetch(
            _refusing(400, io.BufferedReader(raw)),
            "https://api.open.fec.gov/v1/x",
            {},
            key=SENTINEL,
            fetcher="openfec",
        )
    assert not isinstance(caught.value, CredentialFailure)
    assert raw.pos <= apikey._BODY_LIMIT + io.DEFAULT_BUFFER_SIZE
