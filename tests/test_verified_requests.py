"""A `spec()` edit keeps VERIFIED only when the requests it sends are provably unchanged.

The rule is the `fetcher_verified` convention in docs/operations.md. On 2026-09-25 govinfo, openfec
and fecfiling moved their keyed calls onto `apikey.keyed_fetch`, which URL-encodes every parameter,
and all three, with courtlistener, moved reading their key onto `pin.read_key`. All four kept
`VERIFIED = True` on the strength of this file. It holds the templates they had before, copied
verbatim from `b06bfde`, as the oracle, and checks the current code against them two ways. Both
compare what is handed to the fetch function; tests/test_credentials.py holds the wire below it.

1. End to end, every request each verification run made: `pin add` through the real CLI, served
   the captured responses, then the keyless `check` the run ended with where one was recorded
   (none was after courtlistener's), compared in order. That is the keyed metadata call, the
   keyless content fetch and the check's keyless re-fetch, full URL including parameter order,
   and which of them carries the key.
2. Old URL against new for every keyed builder. govinfo's summary URL and openfec's number search
   put operator input and the key into the URL raw, so for those the old template is an oracle
   only on inputs from the unreserved set `[A-Za-z0-9._~-]`; outside it they sent a malformed URL,
   which is the bug the move fixed. openfec's free-text query and fecfiling's filings query already
   went through `urlencode`, so their old templates are exact oracles on any input, and are held
   to that. govinfo's `content_request` is compared on both of its branches: the content host,
   which is what every `check` of a govinfo pin takes, and the API host, which no pin is on.

If this file fails, the fetcher it names now sends something its verification run never sent. Its
`VERIFIED` goes back to False until a live run earns it again.
"""

import json
import random
import re
import string
from pathlib import Path
from urllib.parse import urlencode, urljoin

import pytest

from registers_crosswalk.fetchers import courtlistener, fecfiling, govinfo, openfec
from registers_crosswalk.models import ArchiveCopy
from registers_crosswalk.pin import main

FIXTURES = Path(__file__).parent / "fixtures"

# Unreserved characters only, so the old templates build it correctly, and a value that appears
# nowhere else in the repo, so finding it anywhere means the key went there.
SENTINEL = "SENTINEL-key.0123456789_abcdefghijklmn~XYZ"

SEC_30116 = "USCODE-2024-title52-subtitleIII-chap301-subchapI-sec30116"

# --------------------------------------------------------------------------- the oracle
#
# The templates as they stood at b06bfde, verbatim. Do not "fix" these: they are the record of what
# the verification runs sent.

_OLD_GOVINFO_API = "https://api.govinfo.gov/packages"
_OLD_GOVINFO_CONTENT = "https://www.govinfo.gov/content/pkg"
_OLD_OPENFEC_SEARCH = "https://api.open.fec.gov/v1/legal/search/"
_OLD_OPENFEC_NUMBER_PARAM = {"advisory_opinions": "ao_no", "murs": "case_no"}
_OLD_FEC_BASE = "https://www.fec.gov"
_OLD_FILINGS = "https://api.open.fec.gov/v1/filings/"


def old_govinfo_summary_url(package, granule=None):
    if granule:
        return f"{_OLD_GOVINFO_API}/{package}/granules/{granule}/summary"
    return f"{_OLD_GOVINFO_API}/{package}/summary"


def old_govinfo_content_url(package, granule=None):
    return f"{_OLD_GOVINFO_CONTENT}/{package}/pdf/{granule or package}.pdf"


def old_govinfo_with_key(url, key):
    return f"{url}{'&' if '?' in url else '?'}api_key={key}"


def old_openfec_search_url(number, doc_type, key):
    return (
        f"{_OLD_OPENFEC_SEARCH}?type={doc_type}&{_OLD_OPENFEC_NUMBER_PARAM[doc_type]}={number}"
        f"&api_key={key}"
    )


def old_openfec_free_text_search_url(query, doc_type, key):
    return f"{_OLD_OPENFEC_SEARCH}?{urlencode({'q': query, 'type': doc_type, 'api_key': key})}"


def old_fecfiling_filings_url(file_number, key):
    return f"{_OLD_FILINGS}?{urlencode({'file_number': file_number, 'api_key': key})}"


# --------------------------------------------------------------------------- 1. the runs


def _capture(stem):
    [path] = sorted(FIXTURES.glob(f"{stem}_*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def _recording(capture):
    """Serve the captured response to the API host and document bytes to everything else."""
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        if url.startswith(("https://api.govinfo.gov/", "https://api.open.fec.gov/")):
            return json.dumps(capture).encode(), "application/json"
        return b"%PDF-1.4 a document", "application/pdf"

    fetch.calls = calls
    return fetch


def _must_not_archive(url, **_):
    raise AssertionError(f"the verification runs did not archive, and nor may this: {url}")


def _govinfo_run(stem, package, granule=None):
    argv = ["add", "govinfo", "--package", package]
    if granule:
        argv += ["--granule", granule]
    expected = [
        (old_govinfo_with_key(old_govinfo_summary_url(package, granule), SENTINEL), None),
        (old_govinfo_content_url(package, granule), None),
    ]
    return pytest.param("GOVINFO_API_KEY", stem, argv, expected, id=f"govinfo-{granule or package}")


def _openfec_run(stem, number, document_id):
    record = _capture(stem)["murs"][0]
    [document] = [d for d in record["documents"] if str(d["document_id"]) == document_id]
    argv = ["add", "openfec", "--number", number, "--type", "murs", "--document", document_id]
    expected = [
        (old_openfec_search_url(number, "murs", SENTINEL), None),
        (urljoin(_OLD_FEC_BASE, document["url"]), None),
    ]
    return pytest.param("OPENFEC_API_KEY", stem, argv, expected, id=f"openfec-mur-{number}")


def _fecfiling_run(stem, file_number):
    [filing] = _capture(stem)["results"]
    argv = ["add", "fecfiling", "--file-number", str(file_number), "--document", "fec"]
    expected = [
        (old_fecfiling_filings_url(file_number, SENTINEL), None),
        (filing["fec_url"], None),
    ]
    return pytest.param("OPENFEC_API_KEY", stem, argv, expected, id=f"fecfiling-{file_number}")


# One entry per request a verification run made from a capture this repo holds. The scratch runs
# themselves were govinfo's § 30116 granule (2026-09-22), openfec's MUR 8098 certification
# (2026-09-21) and fecfiling's file 1903438 `.fec` (2026-09-23); each also captured a second
# response, read live the same day, and those are replayed too.
VERIFICATION_RUNS = [
    _govinfo_run(
        "govinfo_summary_USCODE-2024-title52-subtitleIII-chap301-subchapI-sec30116",
        "USCODE-2024-title52",
        SEC_30116,
    ),
    _govinfo_run("govinfo_summary_USCODE-2024-title52", "USCODE-2024-title52"),
    _openfec_run("openfec_search_murs_8098", "8098", "100512215"),
    _openfec_run("openfec_search_murs_8111", "8111", "100512230"),
    _fecfiling_run("fecfiling_filings_1903438", 1903438),
    _fecfiling_run("fecfiling_filings_1997103", 1997103),
]


@pytest.mark.parametrize(("env_key", "stem", "argv", "expected"), VERIFICATION_RUNS)
def test_a_verification_run_sends_exactly_what_it_sent(
    env_key, stem, argv, expected, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(env_key, SENTINEL)
    fetch = _recording(_capture(stem))
    code = main(["--data-dir", str(tmp_path), *argv], fetch=fetch, archive_fn=_must_not_archive)
    assert code == 0, capsys.readouterr().err
    assert fetch.calls == expected
    # The metadata call carries the key; the document fetch, and the record written, do not.
    (metadata_url, _), *rest = fetch.calls
    assert SENTINEL in metadata_url
    assert rest and all(SENTINEL not in url and not headers for url, headers in rest)
    [record] = (tmp_path / "sources").glob("*.json")
    assert SENTINEL not in record.read_text(encoding="utf-8")

    # Each run ended with a `check` with no key in the environment. Replay it, then once more with
    # the key set: the re-fetch is the stored URL, as the old code sent it (`content_request`
    # returned `url, {}` off the API host, and the dispatcher `url, {}` for the other two), and it
    # never picks the key up.
    for key in (None, SENTINEL):
        for name in ("GOVINFO_API_KEY", "OPENFEC_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        if key is not None:
            monkeypatch.setenv(env_key, key)
        fetch.calls.clear()
        code = main(["--data-dir", str(tmp_path), "check", "--json"], fetch=fetch)
        out, err = capsys.readouterr()
        assert code == 0, err
        assert fetch.calls == [(expected[-1][0], {})]
        assert SENTINEL not in out + err


# --------------------------------------------------------------------------- 2. old against new

_UNRESERVED = string.ascii_letters + string.digits + "._~-"


def _random(rng, n, lo, hi):
    seen: list[str] = []
    while len(seen) < n:
        value = "".join(rng.choice(_UNRESERVED) for _ in range(rng.randint(lo, hi)))
        if value not in seen:
            seen.append(value)
    return seen


def _values(seed, n=60):
    """The empty string, every unreserved character alone, then random strings of them."""
    rng = random.Random(seed)
    return ["", *_UNRESERVED, *_random(rng, n, 2, 30)]


# The sentinel, then keys of api.data.gov's length and of odd lengths either side of it.
KEYS = [SENTINEL, *_random(random.Random("keys"), 11, 1, 45)]

# Every seed the old-against-new comparisons draw from, in one place so the check below covers
# exactly what they use.
_SEEDS = [
    "govinfo-package",
    "govinfo-granule",
    "govinfo-content",
    *(f"openfec-{kind}-{t}" for kind in ("number", "query") for t in ("advisory_opinions", "murs")),
]


class _Sent(Exception):
    """Raised by the recording fetch once it has the URL, so no response has to be invented."""


def _sent_url(call):
    urls = []

    def fetch(url, headers=None):
        urls.append((url, headers))
        raise _Sent

    with pytest.raises(_Sent):
        call(fetch)
    [(url, headers)] = urls
    assert headers is None
    return url


@pytest.mark.parametrize("key", KEYS)
def test_govinfo_summary_url_is_unchanged_on_unreserved_input(key):
    packages = _values(_SEEDS[0])
    granules = _values(_SEEDS[1])[: len(packages)]
    pairs = [(p, None) for p in packages] + list(zip(packages, granules, strict=True))
    for package, granule in pairs:
        sent = _sent_url(
            lambda f, p=package, g=granule: govinfo.spec(
                package=p, granule=g, fetch=f, env={"GOVINFO_API_KEY": key}
            )
        )
        assert sent == old_govinfo_with_key(old_govinfo_summary_url(package, granule), key)


@pytest.mark.parametrize("key", [None, *KEYS])
def test_govinfo_content_host_request_is_unchanged_and_key_free(key):
    # The branch every `check` of a govinfo pin takes: the stored URL as it is, no key, no headers,
    # whether or not a key is set.
    env = {} if key is None else {"GOVINFO_API_KEY": key}
    packages = _values(_SEEDS[2])
    for package, granule in [(p, None) for p in packages] + list(
        zip(packages, packages[::-1], strict=True)
    ):
        url = old_govinfo_content_url(package, granule)
        assert govinfo.content_request(url, env=env) == (url, {})


@pytest.mark.parametrize("key", KEYS)
def test_govinfo_content_request_is_unchanged_on_unreserved_input(key):
    for value in _values(_SEEDS[2]):
        for url in (
            f"https://api.govinfo.gov/packages/{value}/pdf",
            f"https://api.govinfo.gov/packages/{value}/granules?offsetMark={value}",
        ):
            new, headers = govinfo.content_request(url, env={"GOVINFO_API_KEY": key})
            assert (new, headers) == (old_govinfo_with_key(url, key), {})


@pytest.mark.parametrize("doc_type", ["advisory_opinions", "murs"])
@pytest.mark.parametrize("key", KEYS)
def test_openfec_search_url_is_unchanged_on_unreserved_input(key, doc_type):
    for number in _values(f"openfec-number-{doc_type}"):  # in _SEEDS
        sent = _sent_url(
            lambda f, n=number: openfec.spec(
                number=n, doc_type=doc_type, fetch=f, env={"OPENFEC_API_KEY": key}
            )
        )
        assert sent == old_openfec_search_url(number, doc_type, key)


@pytest.mark.parametrize("doc_type", ["advisory_opinions", "murs"])
@pytest.mark.parametrize("key", KEYS)
def test_openfec_free_text_url_is_unchanged_on_unreserved_input(key, doc_type):
    for query in _values(f"openfec-query-{doc_type}"):  # in _SEEDS
        sent = _sent_url(
            lambda f, q=query: openfec.search(
                q, doc_type=doc_type, fetch=f, env={"OPENFEC_API_KEY": key}
            )
        )
        assert sent == old_openfec_free_text_search_url(query, doc_type, key)


# The two templates that already went through `urlencode` are exact oracles on any input, so they
# are held to it on the input an operator actually types: spaces, reserved characters, non-ASCII.
_ARBITRARY = ["Cory Mills", "a/b", "a&b=c", "x+y", "#frag", "100%", "é", "中文", " lead", "trail "]
# No whitespace: a key holding any is refused before a request is built (tests/test_credentials.py).
_ANY_KEYS = [*KEYS, "k+y/&=é", "%41%", "#?"]


def _arbitrary(seed, n=60):
    rng = random.Random(seed)
    alphabet = [chr(c) for c in range(0x20, 0x7F)] + list("éß中 😀")
    return [
        *_ARBITRARY,
        *("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 20))) for _ in range(n)),
    ]


@pytest.mark.parametrize("doc_type", ["advisory_opinions", "murs"])
@pytest.mark.parametrize("key", _ANY_KEYS)
def test_openfec_free_text_url_is_unchanged_on_any_input(key, doc_type):
    for query in _arbitrary(f"openfec-query-any-{doc_type}"):
        sent = _sent_url(
            lambda f, q=query: openfec.search(
                q, doc_type=doc_type, fetch=f, env={"OPENFEC_API_KEY": key}
            )
        )
        assert sent == old_openfec_free_text_search_url(query, doc_type, key)


@pytest.mark.parametrize("key", _ANY_KEYS)
def test_fecfiling_filings_url_is_unchanged(key):
    rng = random.Random("fecfiling")
    numbers = [0, 1, 1903438, 1997103, *(rng.randint(1, 10**10) for _ in range(60))]
    for file_number in numbers:
        sent = _sent_url(
            lambda f, n=file_number: fecfiling.spec(
                file_number=n, document="fec", fetch=f, env={"OPENFEC_API_KEY": key}
            )
        )
        assert sent == old_fecfiling_filings_url(file_number, key)


def test_the_generated_inputs_really_are_unreserved_and_really_vary():
    # A generator that quietly produced only letters, or one fixed length, would test less than the
    # docstring claims. The single characters `_values` puts first would hide that, so this looks at
    # the random part alone, for every seed the comparisons use.
    assert re.fullmatch(r"[A-Za-z0-9._~-]{66}", _UNRESERVED) and len(set(_UNRESERVED)) == 66
    for seed in _SEEDS:
        values = _values(seed)
        assert values[: 1 + len(_UNRESERVED)] == ["", *_UNRESERVED]
        generated = values[1 + len(_UNRESERVED) :]
        assert len(set(generated)) == len(generated) == 60
        assert set("".join(generated)) == set(_UNRESERVED), seed
        assert len({len(v) for v in generated}) > 10, seed
    assert all(set(k) <= set(_UNRESERVED) for k in KEYS) and len(set(KEYS)) == len(KEYS)


# --------------------------------------------------------------------------- courtlistener
#
# Reading the token through `pin.read_key` put an edit on courtlistener's `spec()` path too, so its
# verification run is held to the same proof: `add courtlistener --cluster-id 1481640 --archive`
# on 2026-09-20, four authenticated API calls and then the document. No `check` after it is
# recorded, so none is replayed.

_OLD_CL_API = "https://www.courtlistener.com/api/rest/v4"
_OLD_CL_STORAGE = "https://storage.courtlistener.com/"


def old_courtlistener_cluster_url(cluster_id):
    return f"{_OLD_CL_API}/clusters/{cluster_id}/"


def old_courtlistener_auth_headers(token):
    return {"Authorization": f"Token {token}"}


def test_the_courtlistener_verification_run_sends_exactly_what_it_sent(
    tmp_path, monkeypatch, capsys
):
    cluster = _capture("courtlistener_cluster_1481640")
    docket = _capture("courtlistener_docket_2577633")
    served = {
        old_courtlistener_cluster_url(1481640): cluster,
        cluster["sub_opinions"][0]: _capture("courtlistener_opinion_1481640"),
        cluster["docket"]: docket,
        docket["court"]: _capture("courtlistener_court_ca8"),
    }
    calls, archived = [], []

    def fetch(url, headers=None):
        calls.append((url, headers))
        if url in served:
            return json.dumps(served[url]).encode(), "application/json"
        return b"%PDF-1.4 dunne", "application/pdf"

    def archive_fn(url, **_):
        archived.append(url)
        return ArchiveCopy(service="wayback", url=f"https://web.archive.org/web/1/{url}")

    monkeypatch.setenv("COURTLISTENER_TOKEN", SENTINEL)
    argv = ["--data-dir", str(tmp_path), "add", "courtlistener", "--cluster-id", "1481640"]
    code = main([*argv, "--archive"], fetch=fetch, archive_fn=archive_fn)
    assert code == 0, capsys.readouterr().err
    auth = old_courtlistener_auth_headers(SENTINEL)
    document = urljoin(_OLD_CL_STORAGE, cluster["filepath_pdf_harvard"])
    assert calls == [
        (old_courtlistener_cluster_url(1481640), auth),
        (cluster["sub_opinions"][0], auth),
        (cluster["docket"], auth),
        (docket["court"], auth),
        (document, None),  # the document itself goes without the token
    ]
    assert archived == [document]


@pytest.mark.parametrize("key", KEYS)
def test_courtlistener_auth_header_is_unchanged(key):
    assert courtlistener.auth_headers({"COURTLISTENER_TOKEN": key}) == (
        old_courtlistener_auth_headers(key)
    )
