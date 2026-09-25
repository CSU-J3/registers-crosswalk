import email.message
import gzip
import json
import re
import socket
import urllib.error
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest

from registers_crosswalk import pin as pinmod
from registers_crosswalk.fetchers import ecfr, uscode
from registers_crosswalk.models import ArchiveCopy, Grade, Source
from registers_crosswalk.pin import (
    ArchiveFailure,
    ExpectedDrift,
    PinSpec,
    add_source,
    archive,
    check,
    check_all,
    default_fetch,
    pin,
    sha256_hex,
    to_ledger_markdown,
    to_manifest_line,
)

REPO = Path(__file__).resolve().parents[1]
BODY = b"<ECFR><PART>114</PART></ECFR>"
ECFR_URL = "https://www.ecfr.gov/api/versioner/v1/full/2026-09-14/title-11.xml?part=114"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _fetch(body=BODY, media_type="application/xml"):
    """A fetch that records what it was asked for and answers with fixed bytes."""
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        return body, media_type

    fetch.calls = calls
    return fetch


def _spec(**over) -> PinSpec:
    base = dict(
        fetcher="ecfr",
        canonical_url=ECFR_URL,
        citation="11 C.F.R. Part 114",
        title="11 CFR Part 114, as of 2026-09-14",
        publisher="Office of the Federal Register",
        point_in_time=date(2026, 9, 14),
        grade=Grade(reliability="A", credibility=1),
        drift_key="sha256",
        drift_value=sha256_hex,
    )
    base.update(over)
    return PinSpec(**base)


def _pinned(**over) -> Source:
    return pin(_spec(**over), next_id="xr_src_0001", fetch=_fetch(), now=NOW)


# --------------------------------------------------------------------------- hashing


def test_pin_hashes_the_body():
    source = _pinned()
    assert source.artifact.sha256 == sha256_hex(BODY)
    assert source.artifact.byte_length == len(BODY)
    assert source.artifact.drift_value == sha256_hex(BODY)
    assert source.artifact.fetched_at == NOW
    assert source.canonical_url == ECFR_URL
    assert source.grade.code() == "A1"


def test_pin_requests_fetch_url_but_stores_canonical_url():
    # govinfo's case: the key rides on the request and never on what we store.
    fetch = _fetch()
    source = pin(
        _spec(
            fetcher="govinfo",
            canonical_url="https://www.govinfo.gov/content/pkg/X/pdf/X.pdf",
            fetch_url="https://www.govinfo.gov/content/pkg/X/pdf/X.pdf?api_key=SECRET",
        ),
        next_id="xr_src_0001",
        fetch=fetch,
        now=NOW,
    )
    assert fetch.calls[0][0].endswith("api_key=SECRET")
    assert "api_key" not in source.canonical_url
    assert "SECRET" not in source.model_dump_json()


def test_pin_refuses_a_url_carrying_a_key():
    fetch = _fetch()
    with pytest.raises(ValueError, match="carries a credential"):
        pin(
            _spec(canonical_url="https://api.govinfo.gov/packages/X/pdf?api_key=abc"),
            next_id="xr_src_0001",
            fetch=fetch,
        )
    assert fetch.calls == []  # refused before any request went out


def test_pin_refuses_an_empty_body():
    with pytest.raises(ValueError, match="empty body"):
        pin(_spec(), next_id="xr_src_0001", fetch=_fetch(body=b""))


def test_default_fetch_decompresses_gzip_before_hashing(monkeypatch):
    class _Resp:
        headers = {"Content-Encoding": "gzip", "Content-Type": "application/xml; charset=utf-8"}

        def read(self):
            return gzip.compress(BODY)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(pinmod.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    body, media_type = default_fetch(ECFR_URL)
    assert body == BODY
    assert media_type == "application/xml"  # parameters dropped
    assert sha256_hex(body) == sha256_hex(BODY)


def test_default_fetch_sends_gzip_and_a_ua(monkeypatch):
    seen = {}

    class _Resp:
        headers = {"Content-Type": "text/html"}

        def read(self):
            return BODY

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        seen.update(req.header_items())  # every header the request sends
        seen["redirected"] = dict(req.headers)  # the ones a redirect would carry on
        return _Resp()

    monkeypatch.setattr(pinmod.urllib.request, "urlopen", _urlopen)
    default_fetch(ECFR_URL, {"Authorization": "Token x"})
    # urllib title-cases header names
    assert seen["Accept-encoding"] == "gzip"
    assert "registers-crosswalk" in seen["User-agent"]
    assert seen["Authorization"] == "Token x"
    # Sent, but unredirected: a redirect to another host does not carry the credential on.
    assert "Authorization" not in seen["redirected"]


# --------------------------------------------------------------------------- blobs


def test_pin_writes_nothing_under_data(tmp_path):
    before = sorted(p.name for p in (REPO / "data").rglob("*"))
    _pinned()
    assert sorted(p.name for p in (REPO / "data").rglob("*")) == before


def test_blob_dir_written_once_for_identical_bytes(tmp_path):
    blob_dir = tmp_path / "pins"
    pin(_spec(), next_id="xr_src_0001", fetch=_fetch(), blob_dir=blob_dir, now=NOW)
    blob = blob_dir / f"{sha256_hex(BODY)}.xml"
    assert blob.read_bytes() == BODY
    stamp = blob.stat().st_mtime_ns

    pin(_spec(), next_id="xr_src_0002", fetch=_fetch(), blob_dir=blob_dir, now=NOW)
    assert blob.stat().st_mtime_ns == stamp
    assert len(list(blob_dir.iterdir())) == 1


def test_blob_extension_falls_back_to_bin(tmp_path):
    blob_dir = tmp_path / "pins"
    pin(
        _spec(),
        next_id="xr_src_0001",
        fetch=_fetch(media_type="application/x-weird"),
        blob_dir=blob_dir,
    )
    assert (blob_dir / f"{sha256_hex(BODY)}.bin").exists()


@pytest.mark.parametrize("inside", ["data", "data/sources", "."])
def test_blob_dir_inside_the_repo_refused(inside):
    fetch = _fetch()
    with pytest.raises(ValueError, match="never stores document bytes"):
        pin(_spec(), next_id="xr_src_0001", fetch=fetch, blob_dir=REPO / inside)
    assert fetch.calls == []  # refused before any request went out


# --------------------------------------------------------------------------- drift


def _fr_source(**over) -> Source:
    base = dict(
        xr_id="xr_src_0001",
        kind="source",
        citation="60 FR 7862",
        title="Notice",
        canonical_url="https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf",
        fetcher="federalregister",
        published_at="1995-02-09",
        artifact={
            "sha256": sha256_hex(BODY),
            "byte_length": len(BODY),
            "media_type": "application/pdf",
            "fetched_at": "2026-09-17T12:00:00Z",
            "drift_key": "sha256",
            "drift_value": sha256_hex(BODY),
        },
        grade={"reliability": "A", "credibility": 1},
    )
    base.update(over)
    return Source.model_validate(base)


def test_check_reports_ok_on_identical_bytes():
    report = check(_fr_source(), fetch=_fetch(), env={})
    assert report.status == "ok"
    assert report.expected == report.actual == sha256_hex(BODY)


def test_check_reports_drift_on_changed_bytes():
    report = check(_fr_source(), fetch=_fetch(body=b"different"), env={})
    assert report.status == "drift"
    assert report.expected == sha256_hex(BODY)
    assert report.actual == sha256_hex(b"different")


def test_check_reports_error_when_the_fetch_raises_a_non_transport_error():
    # Transport failures get their own status (see the fetch_failed tests below); anything else a
    # fetch callable throws is an ordinary error.
    def boom(url, headers=None):
        raise ValueError("malformed fetcher configuration")

    report = check(_fr_source(), fetch=boom, env={})
    assert report.status == "error"
    assert "ValueError" in report.detail


# govinfo re-fetches its content-host pins with no key; only a URL on the API host needs one, so
# that is the URL these two use to reach the key paths in `check`.
GOVINFO_API_PDF = "https://api.govinfo.gov/packages/X/granules/X/pdf"


def test_check_reports_key_missing_rather_than_ok():
    source = _fr_source(fetcher="govinfo", canonical_url=GOVINFO_API_PDF)
    report = check(source, fetch=_fetch(), env={})
    assert report.status == "key_missing"
    assert "GOVINFO_API_KEY" in report.detail


def test_check_reattaches_the_key_from_env():
    source = _fr_source(fetcher="govinfo", canonical_url=GOVINFO_API_PDF)
    fetch = _fetch()
    report = check(source, fetch=fetch, env={"GOVINFO_API_KEY": "SECRET"})
    assert report.status == "ok"
    assert fetch.calls[0][0].endswith("?api_key=SECRET")


def test_check_needs_no_key_for_a_govinfo_content_url():
    source = _fr_source(
        fetcher="govinfo", canonical_url="https://www.govinfo.gov/content/pkg/X/pdf/X.pdf"
    )
    fetch = _fetch()
    report = check(source, fetch=fetch, env={})
    assert report.status == "ok"
    assert "api_key" not in fetch.calls[0][0]


USCODE_URL = "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title52-section30116&num=0&edition=prelim"
FIXTURES = Path(__file__).parent / "fixtures"


def load_page(stem: str) -> bytes:
    """A CAPTURED live HTML page, as bytes. Captured, dated, never authored, never trimmed."""
    matches = sorted(FIXTURES.glob(f"{stem}_*.html"))
    if not matches:
        raise AssertionError(f"no captured page for {stem!r} under {FIXTURES}")
    return matches[-1].read_bytes()


USCODE_PAGE = load_page("uscode_page_title1_section1")
USCODE_LAST_AMENDED = "2012-12-28"


def _append_to_source_credit(page: bytes, entry: bytes) -> bytes:
    """Splice an entry into the captured page's source credit, just before it closes.

    The credit's own text is broken up by <a> and <statuteAtLarge> tags, so a plain byte replace on
    a date would miss. Mutating a copy of the capture is the rule; this keeps the mutation inside
    the element it is meant to test.
    """
    start = page.index(b'class="source-credit"')
    end = page.index(b"</p>", start)
    return page[:end] + entry + page[end:]


def _uscode_source(**over) -> Source:
    return _fr_source(
        citation="52 U.S.C. § 30116",
        canonical_url=USCODE_URL,
        fetcher="uscode",
        published_at=None,
        point_in_time="2026-09-17",
        artifact={
            "sha256": sha256_hex(USCODE_PAGE),
            "byte_length": len(USCODE_PAGE),
            "media_type": "text/html",
            "fetched_at": "2026-09-18T12:00:00Z",
            "drift_key": "last_amended",
            "drift_value": USCODE_LAST_AMENDED,
        },
        **over,
    )


def test_uscode_check_ignores_markup_churn():
    rerendered = USCODE_PAGE.replace(b"<body", b"<body><nav>new nav</nav>", 1)
    rerendered += b"<!-- build 2 -->"
    report = check(_uscode_source(), fetch=_fetch(body=rerendered, media_type="text/html"), env={})
    assert report.status == "ok"
    assert report.drift_key == "last_amended"
    assert sha256_hex(rerendered) != sha256_hex(USCODE_PAGE)  # the bytes really did change


def test_uscode_check_is_ok_when_only_the_currency_date_advances():
    # The test that encodes the 2026-09-18 redesign. OLRC's "laws in effect on" date is site-wide,
    # so it moves on their publishing schedule for sections nobody touched. Under the old
    # currency_date key this reported DRIFT; under last_amended it must not.
    advanced = USCODE_PAGE.replace(
        b"laws in effect on September 17, 2026", b"laws in effect on December 1, 2026", 1
    )
    assert advanced != USCODE_PAGE
    report = check(_uscode_source(), fetch=_fetch(body=advanced, media_type="text/html"), env={})
    assert report.status == "ok"
    assert report.actual == USCODE_LAST_AMENDED


def test_uscode_check_reports_drift_when_the_source_credit_gains_a_later_law():
    # A law amended the section: the credit gains an entry. This is the finding worth a human.
    amended = _append_to_source_credit(USCODE_PAGE, b"; Pub. L. 119-40, Mar. 4, 2026")
    assert amended != USCODE_PAGE
    report = check(_uscode_source(), fetch=_fetch(body=amended, media_type="text/html"), env={})
    assert report.status == "drift"
    assert report.expected == USCODE_LAST_AMENDED
    assert report.actual == "2026-03-04"


def test_uscode_check_ignores_a_later_date_in_a_note():
    # Notes cite laws that did NOT amend the section. Scoping the parse to the source credit is
    # what keeps an effective-date note from reading as an amendment.
    note = b"<p>Amendment by Pub. L. 119-40, Mar. 4, 2026.</p></body>"
    noted = USCODE_PAGE.replace(b"</body>", note, 1)
    assert noted != USCODE_PAGE
    report = check(_uscode_source(), fetch=_fetch(body=noted, media_type="text/html"), env={})
    assert report.status == "ok"
    assert report.actual == USCODE_LAST_AMENDED


def test_uscode_check_errors_when_the_source_credit_is_gone():
    gone = USCODE_PAGE.replace(b'class="source-credit"', b'class="gone"', 1)
    report = check(_uscode_source(), fetch=_fetch(body=gone, media_type="text/html"), env={})
    assert report.status == "error"


def test_uscode_last_amended_parsed_from_the_page():
    assert uscode.drift_value(USCODE_PAGE) == USCODE_LAST_AMENDED
    with pytest.raises(ValueError, match="no source-credit element"):
        uscode.drift_value(b"<html>nothing here</html>")


def _ecfr_source(**over) -> Source:
    return _fr_source(
        citation="11 C.F.R. Part 114",
        canonical_url=ECFR_URL,
        fetcher="ecfr",
        published_at=None,
        point_in_time="2026-09-14",
        artifact={
            "sha256": sha256_hex(BODY),
            "byte_length": len(BODY),
            "media_type": "application/xml",
            "fetched_at": "2026-09-17T12:00:00Z",
            "drift_key": "sha256",
            "drift_value": sha256_hex(BODY),
        },
        **over,
    )


def _ecfr_fetch(versions):
    def fetch(url, headers=None):
        if "/versions/" in url:
            return json.dumps(versions).encode(), "application/json"
        return BODY, "application/xml"

    return fetch


def test_ecfr_check_reports_amended_even_when_the_bytes_match():
    # The point-in-time URL keeps returning the same bytes after an amendment, so a matching hash
    # is not the whole answer for eCFR.
    fetch = _ecfr_fetch(
        {"content_versions": [{"amendment_date": "2026-09-30", "substantive": True}]}
    )
    report = check(_ecfr_source(), fetch=fetch, env={})
    assert report.status == "amended"
    assert "2026-09-30" in report.detail


def test_ecfr_check_ignores_a_cosmetic_amendment():
    fetch = _ecfr_fetch(
        {
            "content_versions": [
                {"amendment_date": "2026-09-30", "substantive": False},
                {"amendment_date": "2026-01-02", "substantive": True},
            ]
        }
    )
    assert check(_ecfr_source(), fetch=fetch, env={}).status == "ok"


def test_ecfr_check_reports_a_removal_as_an_amendment():
    # A removal is an amendment, and the most consequential kind: the pinned text no longer
    # exists. Earlier this was filtered out alongside cosmetic edits, which read as "ok".
    fetch = _ecfr_fetch(
        {
            "content_versions": [
                {"amendment_date": "2026-10-01", "substantive": True, "removed": True},
            ]
        }
    )
    report = check(_ecfr_source(), fetch=fetch, env={})
    assert report.status == "amended"
    assert "2026-10-01" in report.detail


def test_ecfr_latest_amendment_reads_the_versions_endpoint():
    fetch = _ecfr_fetch(
        {
            "content_versions": [
                {"amendment_date": "2024-01-02", "substantive": True},
                {"amendment_date": "2026-09-30", "substantive": True},
            ]
        }
    )
    assert ecfr.latest_amendment(11, "114", fetch=fetch) == date(2026, 9, 30)
    assert ecfr.latest_amendment(11, "114", fetch=_ecfr_fetch({"content_versions": []})) is None


# --------------------------------------------------------------------------- archiving


def test_archive_returns_the_capture():
    def headers_fn(url, headers=None):
        assert url.endswith(ECFR_URL)
        return {"Content-Location": "/web/20260917120000/https://example.gov/doc.pdf"}

    copy = archive(ECFR_URL, headers_fn=headers_fn, env={})
    assert copy.service == "wayback"
    assert copy.url == "https://web.archive.org/web/20260917120000/https://example.gov/doc.pdf"
    assert copy.captured_at == datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def test_archive_reports_the_reason_when_the_service_raises():
    # Still no exception out of archive() — the best-effort contract is unchanged. What changed
    # is that the caller is told what happened instead of being handed one bare None for
    # everything, so `add_source` can print something the operator can act on.
    def headers_fn(url, headers=None):
        raise OSError("boom")

    result = archive(ECFR_URL, headers_fn=headers_fn, env={})
    assert isinstance(result, ArchiveFailure)
    assert "OSError" in result.reason and "boom" in result.reason


def test_archive_reports_a_missing_content_location():
    result = archive(ECFR_URL, headers_fn=lambda u, h=None: {"Server": "nginx"}, env={})
    assert isinstance(result, ArchiveFailure)
    assert "no Content-Location" in result.reason


def test_an_anonymous_failure_says_no_keys_were_loaded():
    # 2026-09-23: three archive attempts from a shell that had not loaded .env failed with HTTP
    # 500 and read as a Wayback outage; with the keys loaded all three succeeded at once.
    def headers_fn(url, headers=None):
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    result = archive(ECFR_URL, headers_fn=headers_fn, sleep_fn=lambda _s: None, env={})
    assert isinstance(result, ArchiveFailure)
    assert result.reason.startswith("HTTP 500 from the Wayback Machine for ")
    assert result.reason.endswith(
        "no WAYBACK keys in this environment, so the anonymous save path was used; "
        "it has answered 500 for these hosts before"
    )


KEYS = {"WAYBACK_ACCESS_KEY": "k", "WAYBACK_SECRET_KEY": "s"}


def _spn2(*replies, calls=None):
    """A fake SPN2 transport: hands back `replies` in order, recording every call."""
    queue = list(replies)

    def json_fn(url, headers=None, data=None):
        if calls is not None:
            calls.append((url, dict(headers or {}), data))
        return queue.pop(0) if queue else {}

    return json_fn


def test_archive_spn2_returns_the_capture_when_the_job_succeeds():
    copy = archive(
        ECFR_URL,
        json_fn=_spn2(
            {"job_id": "spn2-abc"},
            {"status": "success", "timestamp": "20260919120000", "original_url": ECFR_URL},
        ),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert copy.service == "wayback"
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"
    assert copy.captured_at == datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def test_archive_spn2_polls_until_the_job_stops_pending():
    slept = []
    copy = archive(
        ECFR_URL,
        json_fn=_spn2(
            {"job_id": "spn2-abc"},
            {"status": "pending"},
            {"status": "pending"},
            {"status": "success", "timestamp": "20260919120000"},
        ),
        sleep_fn=slept.append,
        env=KEYS,
    )
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"
    assert len(slept) == 3


def test_archive_spn2_reports_what_the_job_said_when_it_failed():
    # SPN2's own words, carried through: status, status_ext and message are what tell a blocked
    # url apart from a url that simply could not be reached today.
    result = archive(
        ECFR_URL,
        json_fn=_spn2(
            {"job_id": "spn2-abc"},
            {"status": "error", "status_ext": "error:blocked", "message": "no capture"},
        ),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert "status=error" in result.reason
    assert "status_ext=error:blocked" in result.reason
    assert "message=no capture" in result.reason


def test_archive_spn2_reports_a_job_that_never_finishes():
    # A job still pending when the budget runs out is a failed capture: the caller has a pin to
    # write or refuse now, and --archive refuses it rather than writing an unrecoverable one.
    slept = []

    def json_fn(url, headers=None, data=None):
        return {"job_id": "spn2-abc"} if data is not None else {"status": "pending"}

    result = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS, timeout=20)
    assert isinstance(result, ArchiveFailure)
    assert "still pending after 20s" in result.reason
    assert sum(slept) >= 20


def test_archive_spn2_reports_a_post_that_hands_back_no_job():
    result = archive(
        ECFR_URL,
        json_fn=_spn2({"message": "url refused"}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert "save refused" in result.reason
    assert "message=url refused" in result.reason


def _http_error(code):
    return urllib.error.HTTPError("https://web.archive.org/save", code, "boom", {}, None)


def test_archive_retries_a_5xx_and_succeeds_on_a_later_attempt():
    """The 2026-09-20 outage: /save answered 503 with an HTML page, and the next request worked.

    Before the retry, that one-minute blip refused an otherwise good pin — and for `uscode`,
    where --archive is required, the pin could not be made at all until someone tried by hand.
    """
    slept = []
    attempts = []

    def json_fn(url, headers=None, data=None):
        attempts.append(url)
        if data is not None and len(attempts) == 1:
            raise _http_error(503)
        return (
            {"job_id": "spn2-abc"}
            if data is not None
            else {
                "status": "success",
                "timestamp": "20260919120000",
            }
        )

    copy = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"
    assert slept[0] == 10.0  # the first retry wait, before the poll waits


def test_archive_gives_up_on_a_5xx_after_three_retries_and_says_so():
    slept = []

    def json_fn(url, headers=None, data=None):
        raise _http_error(503)

    result = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert isinstance(result, ArchiveFailure)
    assert "HTTP 503" in result.reason
    # three retries, about a minute of waiting
    assert slept == [10.0, 20.0, 30.0]
    assert sum(slept) == 60.0


def test_archive_does_not_retry_a_4xx():
    """A 4xx is Wayback saying no; asking again cannot change the answer."""
    slept = []
    calls = []

    def json_fn(url, headers=None, data=None):
        calls.append(url)
        raise _http_error(403)

    result = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert isinstance(result, ArchiveFailure)
    assert "HTTP 403" in result.reason
    assert len(calls) == 1
    assert slept == []


def test_archive_spn2_posts_the_url_with_the_keyed_authorization():
    calls = []
    archive(
        ECFR_URL,
        json_fn=_spn2(
            {"job_id": "spn2-abc"},
            {"status": "success", "timestamp": "20260919120000"},
            calls=calls,
        ),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    post_url, post_headers, post_data = calls[0]
    assert post_url == "https://web.archive.org/save"
    assert post_headers["Authorization"] == "LOW k:s"
    assert post_headers["Accept"] == "application/json"
    assert post_data == urlencode({"url": ECFR_URL}).encode("utf-8")
    poll_url, poll_headers, poll_data = calls[1]
    assert poll_url == "https://web.archive.org/save/status/spn2-abc"
    assert poll_headers["Authorization"] == "LOW k:s"
    assert poll_data is None


def test_archive_without_keys_never_touches_spn2():
    def json_fn(url, headers=None, data=None):
        raise AssertionError("the anonymous path must not call SPN2")

    copy = archive(
        ECFR_URL,
        headers_fn=lambda u, h=None: {"Content-Location": "/web/20260917120000/x"},
        json_fn=json_fn,
        env={},
    )
    assert copy.url == "https://web.archive.org/web/20260917120000/x"


def test_add_source_prints_the_reason_the_archiver_gave(tmp_path):
    """The reason has to survive the trip, or carrying it was pointless.

    `add_source` composes "archive step failed: " + whatever the archiver said, so a transient 503
    and a url Wayback will not take read differently to the operator — on the page and in the
    terminal alike, since both go through this one function.
    """
    (tmp_path / "sources").mkdir()
    outcome = add_source(
        _spec(),
        data_dir=tmp_path,
        archive=True,
        fetch=_fetch(),
        archive_fn=lambda url, **_: ArchiveFailure("HTTP 503 from the Wayback Machine for " + url),
    )
    assert outcome.status == "archive_failed"
    assert outcome.message == (
        "archive step failed: HTTP 503 from the Wayback Machine for " + ECFR_URL
    )
    assert list((tmp_path / "sources").glob("*.json")) == []


def test_add_source_keeps_the_old_wording_for_an_archiver_that_gives_no_reason(tmp_path):
    """A bare None from an injected fake still reads as it always did, url and all."""
    (tmp_path / "sources").mkdir()
    outcome = add_source(
        _spec(), data_dir=tmp_path, archive=True, fetch=_fetch(), archive_fn=lambda url, **_: None
    )
    assert outcome.status == "archive_failed"
    assert outcome.message == (
        f"archive step failed: no capture returned for {ECFR_URL}; nothing written"
    )


# ------------------------------------ reusing a capture only when it reproduces the pin's drift

ARCHIVED_BODY = b"%PDF-1.4 the pinned bytes"
ARCHIVED_TS = "20260920190851"
EXPECTED = ExpectedDrift("ecfr", "sha256", sha256_hex(ARCHIVED_BODY))
CDX_URL = "https://web.archive.org/cdx/search/cdx?" + urlencode(
    {"url": ECFR_URL, "output": "json", "fl": "timestamp,statuscode,digest"}
)


def _cdx(*captures):
    """The CDX API's answer: a header row, then one row per (timestamp, statuscode, digest)."""
    return [["timestamp", "statuscode", "digest"], *[list(c) for c in captures]] if captures else []


def _memento(timestamp):
    when = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    return format_datetime(when, usegmt=True)


def _served(bodies, *, redirect=None, calls=None):
    """A fake `id_` fetch. `bodies` maps a timestamp to the bytes Wayback holds there; `redirect`
    maps a timestamp it does not hold to the one it serves instead, as Wayback does."""

    def capture_fn(url):
        if calls is not None:
            calls.append(url)
        asked = re.search(r"/web/(\d{14})id_/", url).group(1)
        served = (redirect or {}).get(asked, asked)
        final = url.replace(f"/web/{asked}id_/", f"/web/{served}id_/")
        return bodies[served], final, {"memento-datetime": _memento(served)}

    return capture_fn


def _spn2_success(url, data):
    if data is not None:
        return {"job_id": "spn2-abc"}
    return {"status": "success", "timestamp": "20260919120000"}


def _no_save(url, headers=None, data=None):
    """A JSON seam that answers the CDX query only; any Save Page Now call fails the test."""
    raise AssertionError(f"Save Page Now was called: {url}")


def _cdx_then_spn2(cdx_rows, *, posted=None):
    def json_fn(url, headers=None, data=None):
        if url.startswith("https://web.archive.org/cdx/"):
            return cdx_rows
        if posted is not None:
            posted.append(url)
        if data is not None:
            return {"job_id": "spn2-abc"}
        return {"status": "success", "timestamp": "20260919120000"}

    return json_fn


def test_archive_reuses_the_newest_capture_that_reproduces_the_pin():
    """No Save Page Now request at all: Wayback already holds a capture of what we pinned.

    The CDX API lists every capture; they are tried newest first, each at its exact timestamp, and
    the first that reproduces the pin is reused. A newer capture of other bytes is passed over.
    """
    calls = []
    rows = _cdx((ARCHIVED_TS, "200", "AAA"), ("20260922101010", "200", "BBB"))

    def json_fn(url, headers=None, data=None):
        calls.append(url)
        if url == CDX_URL:
            return rows
        return _no_save(url)

    capture_fn = _served(
        {ARCHIVED_TS: ARCHIVED_BODY, "20260922101010": b"a later, different document"},
        calls=calls,
    )
    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=json_fn,
        capture_fn=capture_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert copy.url == f"https://web.archive.org/web/{ARCHIVED_TS}/{ECFR_URL}"
    assert copy.captured_at == datetime(2026, 9, 20, 19, 8, 51, tzinfo=UTC)
    # the listing, then the newest capture, then the one that matched; `id_` every time, because
    # without it Wayback wraps the document in its toolbar and it could never reproduce the pin
    assert calls == [
        CDX_URL,
        f"https://web.archive.org/web/20260922101010id_/{ECFR_URL}",
        f"https://web.archive.org/web/{ARCHIVED_TS}id_/{ECFR_URL}",
    ]


def test_a_prelim_capture_is_reused_by_its_last_amendment_not_its_bytes():
    """A U.S. Code prelim page carries per-request session data, so its bytes never match twice.

    The rule is the pin's drift value, which for uscode is the section's last amendment: a capture
    with other bytes and the same amendment is reused; one showing a later amendment is not.
    """
    expected = ExpectedDrift("uscode", "last_amended", USCODE_LAST_AMENDED)
    same_law = USCODE_PAGE + b"<!-- jsessionid=another-request -->"
    amended = _append_to_source_credit(USCODE_PAGE, b"; Pub. L. 119-1, Jan. 5, 2026")
    rows = _cdx(("20260919194240", "200", "OLD"), ("20260923014054", "200", "NEW"))
    copy = archive(
        USCODE_URL,
        expected=expected,
        json_fn=lambda url, h=None, d=None: rows if "/cdx/" in url else _no_save(url),
        capture_fn=_served({"20260923014054": amended, "20260919194240": same_law}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert sha256_hex(same_law) != sha256_hex(USCODE_PAGE)  # reused although the bytes differ
    assert copy.url == f"https://web.archive.org/web/20260919194240/{USCODE_URL}"


def test_reuse_passes_over_a_timestamp_wayback_serves_as_another_capture():
    """The xr_src_0012 case: CDX names a timestamp, Wayback redirects it to an older capture.

    Drift values are compared only once the served timestamp is the one asked for, so the
    redirected one is not reused under its own timestamp; the capture that is served is.
    """
    rows = _cdx(("20260711041727", "200", "YUS"), ("20260922175016", "200", "YUS"))
    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=lambda url, h=None, d=None: rows if "/cdx/" in url else _no_save(url),
        capture_fn=_served(
            {"20260711041727": ARCHIVED_BODY}, redirect={"20260922175016": "20260711041727"}
        ),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert copy.url == f"https://web.archive.org/web/20260711041727/{ECFR_URL}"


def test_a_rejected_digest_is_not_fetched_again():
    """A digest names a payload: once one capture of it fails to reproduce the pin, all do."""
    calls = []
    rows = _cdx(*[(f"2026092{n}000000", "200", "SAME") for n in range(1, 5)])
    posted = []
    archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(rows, posted=posted),
        capture_fn=_served(
            {
                **{f"2026092{n}000000": b"not the pinned bytes" for n in range(1, 5)},
                "20260919120000": ARCHIVED_BODY,
            },
            calls=calls,
        ),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    reuse_fetches = [c for c in calls if "20260919120000" not in c]
    assert len(reuse_fetches) == 1  # the newest; the other three share its digest
    assert posted, "Save Page Now was never asked for a capture"


def test_archive_attaches_a_new_capture_that_reproduces_the_pin():
    posted = []
    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx(), posted=posted),
        capture_fn=_served({"20260919120000": ARCHIVED_BODY}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert posted
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


def test_a_new_capture_that_does_not_reproduce_the_pin_is_refused():
    """Save Page Now's word that it took a capture is not a check of what it took."""
    slept = []
    result = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=_served({"20260919120000": b"an error page, captured faithfully"}),
        sleep_fn=slept.append,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert result.reason.startswith("new capture does not reproduce the pin's sha256")
    assert result.reason.endswith("nothing attached")
    assert 300.0 not in slept  # a capture that is served and wrong is refused at once


def test_a_prelim_new_capture_is_refused_when_its_last_amendment_differs():
    expected = ExpectedDrift("uscode", "last_amended", USCODE_LAST_AMENDED)
    amended = _append_to_source_credit(USCODE_PAGE, b"; Pub. L. 119-1, Jan. 5, 2026")
    result = archive(
        USCODE_URL,
        expected=expected,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=_served({"20260919120000": amended}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert "does not reproduce the pin's last_amended" in result.reason


def test_a_new_capture_not_yet_served_is_retried_then_reported():
    """Three tries over ten minutes; if Wayback still serves an older capture, attach nothing."""
    slept = []
    result = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=_served(
            {"20260711041727": ARCHIVED_BODY}, redirect={"20260919120000": "20260711041727"}
        ),
        sleep_fn=slept.append,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert result.reason.startswith("capture not stored at returned timestamp")
    assert "3 tries over 10 minutes" in result.reason
    assert slept.count(300.0) == 2


def test_nothing_is_attached_when_nothing_is_stored_at_the_returned_timestamp():
    """Save Page Now returned a timestamp, and on every try Wayback has nothing stored there:
    it answers 404, or redirects to a different capture, which here even holds the pinned bytes.
    Only the served-timestamp comparison stops that other capture being attached under the
    returned timestamp."""
    tries = []
    redirected = _served(
        {"20260711041727": ARCHIVED_BODY}, redirect={"20260919120000": "20260711041727"}
    )

    def capture_fn(url):
        tries.append(url)
        if len(tries) == 3:  # the last try decides the reason, so it is the 404
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return redirected(url)

    result = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=capture_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert result.reason.startswith("capture not stored at returned timestamp: ")
    assert "(HTTP 404)" in result.reason
    assert result.reason.endswith("nothing attached")
    assert len(tries) == 3


def test_a_404_on_every_try_is_reported_as_nothing_stored():
    def capture_fn(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    result = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=capture_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert result.reason.startswith("capture not stored at returned timestamp: ")


def test_the_new_capture_attached_is_the_pin_url_at_the_checked_timestamp():
    """SPN2 may spell the url differently in `original_url`; what is attached is what's checked."""

    def json_fn(url, headers=None, data=None):
        if "/cdx/" in url:
            return _cdx()
        if data is not None:
            return {"job_id": "spn2-abc"}
        return {"status": "success", "timestamp": "20260919120000", "original_url": "http://x/y"}

    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=json_fn,
        capture_fn=_served({"20260919120000": ARCHIVED_BODY}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


@pytest.mark.parametrize(
    "rows",
    [
        [["timestamp", "statuscode", "digest"], 5],  # a row that is not a row
        [["timestamp", "statuscode", "digest"], ["20260920190851", "200", ["not", "a", "digest"]]],
        [5, ["20260920190851", "200", "AAA"]],  # a header that is not a header
    ],
)
def test_a_malformed_cdx_listing_cannot_raise_out_of_archive(rows):
    posted = []

    def json_fn(url, headers=None, data=None):
        if "/cdx/" in url:
            return rows
        return _cdx_then_spn2(_cdx(), posted=posted)(url, headers, data)

    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=json_fn,
        capture_fn=_served({"20260919120000": ARCHIVED_BODY, "20260920190851": b"x"}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(copy, ArchiveCopy)


def test_a_transport_error_is_not_reported_as_nothing_stored():
    def capture_fn(url):
        raise TimeoutError("read timed out")

    result = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=capture_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert isinstance(result, ArchiveFailure)
    assert result.reason.startswith("new capture could not be checked: ")


def test_a_new_capture_served_on_a_later_try_is_attached():
    served = {"n": 0}
    later = _served({"20260919120000": ARCHIVED_BODY})
    early = _served({"20260711041727": b"x"}, redirect={"20260919120000": "20260711041727"})

    def capture_fn(url):
        served["n"] += 1
        return (early if served["n"] == 1 else later)(url)

    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=_cdx_then_spn2(_cdx()),
        capture_fn=capture_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"
    assert served["n"] == 2


def test_a_broken_cdx_api_cannot_refuse_a_pin():
    """The listing is an optimisation; if it dies the pin goes the long way round, as before."""
    posted = []

    def json_fn(url, headers=None, data=None):
        if url.startswith("https://web.archive.org/cdx/"):
            raise OSError("cdx api down")
        return _cdx_then_spn2(_cdx(), posted=posted)(url, headers, data)

    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=json_fn,
        capture_fn=_served({"20260919120000": ARCHIVED_BODY}),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert posted
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


def test_reuse_is_tried_on_the_anonymous_path_too():
    """Nothing about skipping a capture depends on holding keys."""

    def headers_fn(url, headers=None):
        raise AssertionError("the anonymous save was called")

    copy = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=lambda url, h=None, d=None: _cdx((ARCHIVED_TS, "200", "AAA")),
        capture_fn=_served({ARCHIVED_TS: ARCHIVED_BODY}),
        headers_fn=headers_fn,
        sleep_fn=lambda _s: None,
        env={},
    )
    assert copy.url == f"https://web.archive.org/web/{ARCHIVED_TS}/{ECFR_URL}"


def test_an_anonymous_new_capture_is_verified_too():
    result = archive(
        ECFR_URL,
        expected=EXPECTED,
        json_fn=lambda url, h=None, d=None: _cdx(),
        headers_fn=lambda u, h=None: {"Content-Location": f"/web/20260919120000/{ECFR_URL}"},
        capture_fn=_served({"20260919120000": b"not the pinned bytes"}),
        sleep_fn=lambda _s: None,
        env={},
    )
    assert isinstance(result, ArchiveFailure)
    assert "does not reproduce" in result.reason


def test_archive_without_an_expected_drift_neither_reuses_nor_checks():
    """No drift value, nothing to check against: no listing, and the capture comes back as-is."""
    asked = []

    def json_fn(url, headers=None, data=None):
        asked.append(url)
        return _cdx_then_spn2(_cdx())(url, headers, data)

    def capture_fn(url):
        raise AssertionError("a capture was fetched with nothing to check it against")

    copy = archive(
        ECFR_URL, json_fn=json_fn, capture_fn=capture_fn, sleep_fn=lambda _s: None, env=KEYS
    )
    assert not any("/cdx/" in url for url in asked)
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


def test_add_source_hands_the_pins_drift_value_to_the_archiver(tmp_path):
    """The drift value has to reach `archive()` or none of the above can happen from a real pin."""
    (tmp_path / "sources").mkdir()
    seen = {}

    def archive_fn(url, *, expected=None):
        seen["url"] = url
        seen["expected"] = expected
        return ArchiveCopy(service="wayback", url="https://web.archive.org/web/1/x")

    outcome = add_source(
        _spec(), data_dir=tmp_path, archive=True, fetch=_fetch(), archive_fn=archive_fn
    )
    assert outcome.status == "written"
    assert seen["url"] == ECFR_URL
    assert seen["expected"] == ExpectedDrift("ecfr", "sha256", outcome.source.artifact.sha256)


# ------------------------------------------------ response headers are read case-insensitively


class _FakeResponse:
    """What urlopen returns, with headers exactly as Wayback sends them: `content-encoding`."""

    def __init__(self, body, headers, url):
        self._body, self._url = body, url
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key] = value

    def read(self):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_decoding_reads_a_lowercase_content_encoding():
    """The discarded 2026-09-24 run: a plain-dict lookup of "Content-Encoding" missed Wayback's
    lowercase header and hashed still-gzipped bytes, which read as seven mismatches."""
    raw = gzip.compress(ARCHIVED_BODY)
    assert pinmod._decoded(raw, {"content-encoding": "gzip"}) == ARCHIVED_BODY  # noqa: SLF001
    assert pinmod._decoded(raw, {"Content-Encoding": "gzip"}) == ARCHIVED_BODY  # noqa: SLF001


def test_a_capture_fetch_decodes_lowercase_headers_and_says_what_was_served(monkeypatch):
    final = f"https://web.archive.org/web/{ARCHIVED_TS}id_/{ECFR_URL}"
    response = _FakeResponse(
        gzip.compress(ARCHIVED_BODY),
        {"content-encoding": "gzip", "memento-datetime": _memento(ARCHIVED_TS)},
        final,
    )
    monkeypatch.setattr(pinmod.urllib.request, "urlopen", lambda req, timeout=None: response)
    body, served_url, headers = pinmod._fetch_capture(final)  # noqa: SLF001
    assert body == ARCHIVED_BODY
    assert served_url == final
    assert pinmod._served_timestamps(served_url, headers) == {ARCHIVED_TS}  # noqa: SLF001


def test_default_fetch_decodes_a_lowercase_content_encoding(monkeypatch):
    response = _FakeResponse(
        gzip.compress(BODY),
        {"content-encoding": "gzip", "content-type": "application/xml"},
        ECFR_URL,
    )
    monkeypatch.setattr(pinmod.urllib.request, "urlopen", lambda req, timeout=None: response)
    assert default_fetch(ECFR_URL) == (BODY, "application/xml")


# ------------------------------------------------------------------- 429 is a rate, not a fault


def _rate_limited(retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(ECFR_URL, 429, "slow down", headers, None)


def test_archive_honours_retry_after_on_a_429():
    slept = []
    attempts = []

    def json_fn(url, headers=None, data=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise _rate_limited("7")
        return _spn2_success(url, data)

    copy = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"
    assert slept[0] == 7.0  # the service's own number, not ours


def test_archive_waits_a_minute_on_a_429_with_no_retry_after():
    slept = []
    attempts = []

    def json_fn(url, headers=None, data=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise _rate_limited()
        return _spn2_success(url, data)

    archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert slept[0] == 60.0


def test_archive_gives_up_after_three_429s_and_says_why():
    slept = []

    def json_fn(url, headers=None, data=None):
        raise _rate_limited("5")

    result = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert isinstance(result, ArchiveFailure)
    assert "HTTP 429" in result.reason
    assert "still rate limited after 3 tries" in result.reason
    assert slept == [5.0, 5.0]  # three tries, two waits between them


def test_retry_after_accepts_the_http_date_form():
    """The spec allows a date as well as a count of seconds; ignoring it would substitute our own
    number for the one the service actually asked for."""
    when = datetime.now(tz=UTC) + timedelta(seconds=30)
    exc = urllib.error.HTTPError(
        ECFR_URL, 429, "slow down", {"Retry-After": format_datetime(when, usegmt=True)}, None
    )
    seconds = pinmod._retry_after(exc)
    assert 20 <= seconds <= 31


def test_a_429_and_a_5xx_do_not_share_a_budget():
    """Each means something different, so each gets its own count of attempts."""
    slept = []
    seen = []

    def json_fn(url, headers=None, data=None):
        seen.append(len(seen))
        if len(seen) == 1:
            raise _rate_limited("5")
        if len(seen) == 2:
            raise urllib.error.HTTPError(url, 503, "offline", {}, None)
        return _spn2_success(url, data)

    copy = archive(ECFR_URL, json_fn=json_fn, sleep_fn=slept.append, env=KEYS)
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"
    assert slept[:2] == [5.0, 10.0]  # the 429's own wait, then the first 5xx wait


# --------------------------------------------------------------------------- emitting


def test_ledger_markdown_format():
    # The title already carries the as-of date, so the Retrieved clause must not repeat it.
    source = _pinned()
    assert to_ledger_markdown(source).splitlines() == [
        "- **Office of the Federal Register, 2026-09-14 (A1)** — 11 CFR Part 114, as of "
        "2026-09-14. Retrieved 2026-09-17 UTC; "
        f"sha256 {sha256_hex(BODY)[:16]}…; xr_src_0001.",
        f"  {ECFR_URL}",
        "  Archive: pending",
    ]


def test_ledger_markdown_adds_as_of_when_the_title_does_not_carry_it():
    # The other branch. A title that doesn't state its date still gets one, so the point in time
    # is never left unsaid.
    source = _pinned().model_copy(update={"title": "11 CFR Part 114"})
    line = to_ledger_markdown(source).splitlines()[0]
    assert "11 CFR Part 114. Retrieved 2026-09-17 UTC, as of 2026-09-14;" in line


def test_ledger_markdown_leaves_a_title_that_already_carries_its_citation_alone():
    # The ecfr shape, and the uscode one with it: the title opens with the citation, so appending
    # the citation would set it down twice. Note the two spell it differently — the title says
    # "11 CFR", the citation "11 C.F.R." — which is the case the normalized comparison exists for
    # and the one a raw prefix test would get wrong.
    line = to_ledger_markdown(_pinned()).splitlines()[0]
    assert "— 11 CFR Part 114, as of 2026-09-14. Retrieved" in line
    assert "11 C.F.R. Part 114" not in line


def test_ledger_markdown_appends_the_citation_to_a_federal_register_title():
    # A Federal Register title is the document's own name and never states its citation, so
    # without the rule the entry would name a document it never cites.
    source = _pinned().model_copy(
        update={
            "fetcher": "federalregister",
            "citation": "60 FR 7862",
            "title": (
                "Expenditures; Reports by Political Committees; Personal Use of Campaign Funds"
            ),
            "published_at": date(1995, 2, 9),
            "point_in_time": None,
        }
    )
    line = to_ledger_markdown(source).splitlines()[0]
    assert "— Expenditures; Reports by Political Committees; " in line
    assert "Personal Use of Campaign Funds, 60 FR 7862. Retrieved 2026-09-17 UTC;" in line


def test_ledger_markdown_appends_the_citation_to_a_case_name():
    # The courtlistener shape. Same rule, and the one that prompted it: a case name cites nothing.
    source = _pinned().model_copy(
        update={
            "fetcher": "courtlistener",
            "citation": "138 F.2d 137",
            "title": "Dunne v. United States",
            "publisher": "Court of Appeals for the Eighth Circuit",
            "published_at": date(1943, 9, 20),
            "point_in_time": None,
        }
    )
    line = to_ledger_markdown(source).splitlines()[0]
    assert line == (
        "- **Court of Appeals for the Eighth Circuit, 1943-09-20 (A1)** — "
        "Dunne v. United States, 138 F.2d 137. "
        f"Retrieved 2026-09-17 UTC; sha256 {sha256_hex(BODY)[:16]}…; xr_src_0001."
    )


def test_ledger_markdown_uses_the_archive_when_there_is_one():
    source = _pinned().model_copy(
        update={"archives": [ArchiveCopy(service="wayback", url="https://web.archive.org/web/1/x")]}
    )
    assert "Archive: https://web.archive.org/web/1/x" in to_ledger_markdown(source)


def test_manifest_line_is_sha256sum_format():
    assert to_manifest_line(_pinned()) == f"{sha256_hex(BODY)}  xr_src_0001-11-c-f-r-part-114.xml"


def _pinned_as(url, media_type, **kw):
    return pin(
        _spec(canonical_url=url), next_id="xr_src_0001", fetch=_fetch(media_type=media_type), **kw
    )


@pytest.mark.parametrize("generic", ["binary/octet-stream", "application/octet-stream"])
def test_a_generic_type_takes_the_urls_own_suffix(generic, tmp_path):
    # docquery's shape: an FEC filing served as binary/octet-stream under a `.fec` URL.
    url = "https://docquery.fec.gov/dcdev/posted/1903438.fec"
    source = _pinned_as(url, generic, blob_dir=tmp_path / "pins", now=NOW)
    assert to_manifest_line(source).endswith("-11-c-f-r-part-114.fec")
    assert (tmp_path / "pins" / f"{sha256_hex(BODY)}.fec").exists()


def test_a_generic_type_with_no_url_suffix_is_bin():
    source = _pinned_as("https://example.gov/documents/1903438", "binary/octet-stream", now=NOW)
    assert to_manifest_line(source).endswith(".bin")


def test_a_specific_type_is_never_renamed_by_the_url():
    # The served type wins whenever it says something: an HTML page at a `.pdf` url stays `.html`.
    source = _pinned_as("https://example.gov/doc.pdf", "text/html", now=NOW)
    assert to_manifest_line(source).endswith(".html")


def test_no_committed_pin_with_a_specific_type_changes_its_manifest_name():
    from registers_crosswalk.registry import Crosswalk

    for source in Crosswalk(REPO / "data").sources.values():
        if source.artifact.media_type in pinmod._GENERIC_TYPES:  # noqa: SLF001
            continue
        ext = pinmod._EXTENSIONS.get(source.artifact.media_type, ".bin")  # noqa: SLF001
        assert to_manifest_line(source).endswith(ext), source.xr_id


# ------------------------------------------------- transport failure is NOT drift (exit 3, not 1)


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None),  # non-2xx
        urllib.error.URLError("connection refused"),
        socket.gaierror("Name or service not known"),  # DNS
        TimeoutError("read timed out"),
    ],
)
def test_transport_failure_reports_fetch_failed_not_drift(exc):
    def boom(url, headers=None):
        raise exc

    report = check(_fr_source(), fetch=boom, env={})
    assert report.status == "fetch_failed"
    assert report.actual is None  # we learned nothing about the document
    assert type(exc).__name__ in report.detail


def test_parse_failure_is_content_not_transport():
    # The fetch worked; the document no longer carries its source credit. Exit 1, not 3.
    report = check(_uscode_source(), fetch=_fetch(body=b"<html>no date here</html>"), env={})
    assert report.status == "error"


def test_ecfr_amendment_history_timeout_is_fetch_failed():
    # The bytes matched, but the versions endpoint was unreachable — we cannot say "ok".
    def fetch(url, headers=None):
        if "/versions/" in url:
            raise TimeoutError("read timed out")
        return BODY, "application/xml"

    report = check(_ecfr_source(), fetch=fetch, env={})
    assert report.status == "fetch_failed"
    assert "amendment history unreachable" in report.detail


# ------------------------------------------ one retry for a transport failure, and for nothing else


class _Clock:
    """A fake clock: `sleep` advances it instead of waiting, and `now` is what a fetch records."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _flaky(clock, failures, body=BODY, *, only=None):
    """A fetch that times out on its first `failures` calls to a url containing `only` (every url
    when None), then answers. It records the clock time of each call."""
    calls, failed = [], []

    def fetch(url, headers=None):
        calls.append((url, clock.now))
        if (only is None or only in url) and len(failed) < failures:
            failed.append(url)
            raise TimeoutError("read timed out")
        if "/versions/" in url:
            return json.dumps({"content_versions": []}).encode(), "application/json"
        return body, "application/xml"

    fetch.calls = calls
    return fetch


def test_a_fetch_that_fails_once_then_succeeds_is_ok():
    clock = _Clock()
    fetch = _flaky(clock, 1)
    [report] = check_all([_fr_source()], fetch=fetch, env={}, sleep_fn=clock.sleep)
    assert report.status == "ok"
    assert [at for _url, at in fetch.calls] == [0.0, 30.0]  # the retry comes after the wait


def test_a_fetch_that_fails_twice_is_fetch_failed():
    clock = _Clock()
    fetch = _flaky(clock, 2)
    [report] = check_all([_fr_source()], fetch=fetch, env={}, sleep_fn=clock.sleep)
    assert report.status == "fetch_failed"
    assert [at for _url, at in fetch.calls] == [0.0, 30.0]  # one retry, not a loop
    assert "retried once after 30s" in report.detail


def test_a_drifted_document_is_fetched_once_and_reported_drift():
    clock = _Clock()
    fetch = _flaky(clock, 0, body=b"changed")
    [report] = check_all([_fr_source()], fetch=fetch, env={}, sleep_fn=clock.sleep)
    assert report.status == "drift"
    assert len(fetch.calls) == 1
    assert clock.slept == []


def test_amended_and_key_missing_are_never_retried():
    clock = _Clock()
    amended = _ecfr_fetch(
        {"content_versions": [{"amendment_date": "2026-09-30", "substantive": True}]}
    )
    keyed = _fr_source(fetcher="govinfo", canonical_url=GOVINFO_API_PDF)
    [a] = check_all([_ecfr_source()], fetch=amended, env={}, sleep_fn=clock.sleep)
    [k] = check_all([keyed], fetch=_fetch(), env={}, sleep_fn=clock.sleep)
    assert (a.status, k.status) == ("amended", "key_missing")
    assert clock.slept == []


def test_many_failing_pins_share_one_wait():
    # One host down must not cost 30s per pin: 20 of the live pins sit on docquery.fec.gov.
    clock = _Clock()
    sources = [_fr_source(xr_id=f"xr_src_{n:04d}") for n in range(1, 6)]
    fetch = _flaky(clock, 5)
    reports = check_all(sources, fetch=fetch, env={}, sleep_fn=clock.sleep)
    assert [r.status for r in reports] == ["ok"] * 5
    assert clock.slept == [30.0]


def test_an_unreachable_ecfr_amendment_history_is_retried_too():
    clock = _Clock()
    fetch = _flaky(clock, 1, only="/versions/")
    [report] = check_all([_ecfr_source()], fetch=fetch, env={}, sleep_fn=clock.sleep)
    assert report.status == "ok"
    assert clock.slept == [30.0]


# --------------------------------------------------- archive visibility on a drift report


def test_drift_report_marks_an_unarchived_pin():
    report = check(_fr_source(), fetch=_fetch(body=b"changed"), env={})
    assert report.status == "drift"
    assert report.archived is False


def test_drift_report_marks_an_archived_pin():
    archived = _fr_source(
        archives=[{"service": "wayback", "url": "https://web.archive.org/web/1/x"}]
    )
    report = check(archived, fetch=_fetch(body=b"changed"), env={})
    assert report.status == "drift"
    assert report.archived is True


def test_archived_flag_serializes_for_json_output():
    report = check(_fr_source(), fetch=_fetch(body=b"changed"), env={})
    assert report.model_dump(mode="json")["archived"] is False
