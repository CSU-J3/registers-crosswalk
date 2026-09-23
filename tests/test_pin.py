import gzip
import json
import socket
import urllib.error
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

from registers_crosswalk import pin as pinmod
from registers_crosswalk.fetchers import ecfr, uscode
from registers_crosswalk.models import ArchiveCopy, Grade, Source
from registers_crosswalk.pin import (
    ArchiveFailure,
    PinSpec,
    add_source,
    archive,
    check,
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
        seen.update(req.headers)
        return _Resp()

    monkeypatch.setattr(pinmod.urllib.request, "urlopen", _urlopen)
    default_fetch(ECFR_URL, {"Authorization": "Token x"})
    # urllib title-cases header names
    assert seen["Accept-encoding"] == "gzip"
    assert "registers-crosswalk" in seen["User-agent"]
    assert seen["Authorization"] == "Token x"


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


# ------------------------------------------- reusing a capture that already holds these bytes

ARCHIVED_BODY = b"%PDF-1.4 the pinned bytes"
ARCHIVED_TS = "20260920190851"
AVAILABLE_URL = "https://archive.org/wayback/available?url=" + quote(ECFR_URL, safe="")


def _available(url, timestamp=ARCHIVED_TS, *, present=True):
    """The availability API's answer, in the shape it actually returns (captured 2026-09-20)."""
    if not present:
        return {"url": url, "archived_snapshots": {}}
    return {
        "url": url,
        "archived_snapshots": {
            "closest": {
                "status": "200",
                "available": True,
                "url": f"http://web.archive.org/web/{timestamp}/{url}",
                "timestamp": timestamp,
            }
        },
    }


def _spn2_success(url, data):
    if data is not None:
        return {"job_id": "spn2-abc"}
    return {"status": "success", "timestamp": "20260919120000"}


def test_archive_reuses_an_existing_capture_whose_bytes_match():
    """No Save Page Now request at all: the archive already holds exactly what we pinned.

    SPN2 would not have taken a new capture anyway for anything archived in the last hour, so the
    round trip buys nothing and spends a write against a rate-limited service.
    """
    calls = []

    def json_fn(url, headers=None, data=None):
        calls.append(url)
        if url.startswith("https://archive.org/wayback/available"):
            return _available(ECFR_URL)
        raise AssertionError(f"Save Page Now was called: {url}")

    def fetch_fn(url, headers=None):
        calls.append(url)
        return ARCHIVED_BODY, "application/pdf"

    copy = archive(
        ECFR_URL,
        expected_sha256=sha256_hex(ARCHIVED_BODY),
        json_fn=json_fn,
        fetch_fn=fetch_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert copy.service == "wayback"
    assert copy.url == f"https://web.archive.org/web/{ARCHIVED_TS}/{ECFR_URL}"
    assert copy.captured_at == datetime(2026, 9, 20, 19, 8, 51, tzinfo=UTC)
    # the availability probe, then the capture's own bytes through the id_ form, and nothing else.
    # `id_` matters: without it Wayback serves the document wrapped in its toolbar and rewritten,
    # which would never hash to what we pinned and would make this check always fail.
    assert calls == [
        AVAILABLE_URL,
        f"https://web.archive.org/web/{ARCHIVED_TS}id_/{ECFR_URL}",
    ]


def test_archive_falls_through_to_spn2_when_the_existing_capture_does_not_match():
    """A capture of that url is not the same claim as a capture of these bytes.

    A url that served something else last year has a capture; reusing it would attach a
    recoverable copy of the wrong document to the record.
    """
    posted = []

    def json_fn(url, headers=None, data=None):
        if url.startswith("https://archive.org/wayback/available"):
            return _available(ECFR_URL)
        posted.append(url)
        return _spn2_success(url, data)

    def fetch_fn(url, headers=None):
        return b"some other document entirely", "application/pdf"

    copy = archive(
        ECFR_URL,
        expected_sha256=sha256_hex(ARCHIVED_BODY),
        json_fn=json_fn,
        fetch_fn=fetch_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert posted, "SPN2 was never asked for a capture"
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


def test_archive_falls_through_to_spn2_when_there_is_no_capture():
    posted = []

    def json_fn(url, headers=None, data=None):
        if url.startswith("https://archive.org/wayback/available"):
            return _available(ECFR_URL, present=False)
        posted.append(url)
        return _spn2_success(url, data)

    def fetch_fn(url, headers=None):
        raise AssertionError("nothing to fetch: there is no capture")

    copy = archive(
        ECFR_URL,
        expected_sha256=sha256_hex(ARCHIVED_BODY),
        json_fn=json_fn,
        fetch_fn=fetch_fn,
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert posted
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


def test_archive_does_not_reuse_without_a_hash_to_check_against():
    """No hash, no reuse. Reuse is conditional on the bytes matching, and nothing would match."""
    asked = []

    def json_fn(url, headers=None, data=None):
        asked.append(url)
        return _spn2_success(url, data)

    archive(ECFR_URL, json_fn=json_fn, sleep_fn=lambda _s: None, env=KEYS)
    assert not any("wayback/available" in url for url in asked)


def test_a_broken_availability_api_cannot_refuse_a_pin():
    """The probe is an optimisation; if it dies the pin goes the long way round, as before."""
    posted = []

    def json_fn(url, headers=None, data=None):
        if url.startswith("https://archive.org/wayback/available"):
            raise OSError("availability api down")
        posted.append(url)
        return _spn2_success(url, data)

    copy = archive(
        ECFR_URL,
        expected_sha256=sha256_hex(ARCHIVED_BODY),
        json_fn=json_fn,
        fetch_fn=lambda u, h=None: (b"x", "application/pdf"),
        sleep_fn=lambda _s: None,
        env=KEYS,
    )
    assert posted
    assert copy.url == f"https://web.archive.org/web/20260919120000/{ECFR_URL}"


def test_reuse_is_tried_on_the_anonymous_path_too():
    """Nothing about skipping a capture depends on holding keys."""
    calls = []

    def json_fn(url, headers=None, data=None):
        calls.append(url)
        return _available(ECFR_URL)

    def headers_fn(url, headers=None):
        raise AssertionError("the anonymous save was called")

    copy = archive(
        ECFR_URL,
        expected_sha256=sha256_hex(ARCHIVED_BODY),
        json_fn=json_fn,
        fetch_fn=lambda u, h=None: (ARCHIVED_BODY, "application/pdf"),
        headers_fn=headers_fn,
        sleep_fn=lambda _s: None,
        env={},
    )
    assert copy.url == f"https://web.archive.org/web/{ARCHIVED_TS}/{ECFR_URL}"
    assert calls == [AVAILABLE_URL]


def test_add_source_hands_the_artifact_hash_to_the_archiver(tmp_path):
    """The hash has to reach `archive()` or none of the above can happen from a real pin."""
    (tmp_path / "sources").mkdir()
    seen = {}

    def archive_fn(url, *, expected_sha256=None):
        seen["url"] = url
        seen["expected_sha256"] = expected_sha256
        return ArchiveCopy(service="wayback", url="https://web.archive.org/web/1/x")

    outcome = add_source(
        _spec(), data_dir=tmp_path, archive=True, fetch=_fetch(), archive_fn=archive_fn
    )
    assert outcome.status == "written"
    assert seen["url"] == ECFR_URL
    assert seen["expected_sha256"] == sha256_hex(BODY)
    assert seen["expected_sha256"] == outcome.source.artifact.sha256


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
