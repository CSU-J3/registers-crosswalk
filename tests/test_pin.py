import gzip
import json
import socket
import urllib.error
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from registers_crosswalk import pin as pinmod
from registers_crosswalk.fetchers import ecfr, uscode
from registers_crosswalk.models import ArchiveCopy, Grade, Source
from registers_crosswalk.pin import (
    PinSpec,
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


def test_check_reports_key_missing_rather_than_ok():
    source = _fr_source(
        fetcher="govinfo", canonical_url="https://www.govinfo.gov/content/pkg/X/pdf/X.pdf"
    )
    report = check(source, fetch=_fetch(), env={})
    assert report.status == "key_missing"
    assert "GOVINFO_API_KEY" in report.detail


def test_check_reattaches_the_key_from_env():
    source = _fr_source(
        fetcher="govinfo", canonical_url="https://www.govinfo.gov/content/pkg/X/pdf/X.pdf"
    )
    fetch = _fetch()
    report = check(source, fetch=fetch, env={"GOVINFO_API_KEY": "SECRET"})
    assert report.status == "ok"
    assert fetch.calls[0][0].endswith("?api_key=SECRET")


USCODE_URL = "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title52-section30116&num=0&edition=prelim"
USCODE_PAGE = b"<html><p>Text contains those laws in effect on January 5, 2026</p></html>"


def _uscode_source(**over) -> Source:
    return _fr_source(
        citation="52 U.S.C. § 30116",
        canonical_url=USCODE_URL,
        fetcher="uscode",
        published_at=None,
        point_in_time="2026-01-05",
        artifact={
            "sha256": sha256_hex(USCODE_PAGE),
            "byte_length": len(USCODE_PAGE),
            "media_type": "text/html",
            "fetched_at": "2026-09-17T12:00:00Z",
            "drift_key": "currency_date",
            "drift_value": "2026-01-05",
        },
        **over,
    )


def test_uscode_check_ignores_markup_churn_when_the_currency_date_holds():
    rerendered = (
        b"<html><nav>new nav</nav><p>Text contains those laws in effect on January 5, 2026</p>"
    )
    report = check(_uscode_source(), fetch=_fetch(body=rerendered, media_type="text/html"), env={})
    assert report.status == "ok"
    assert report.drift_key == "currency_date"
    assert sha256_hex(rerendered) != sha256_hex(USCODE_PAGE)  # the bytes really did change


def test_uscode_check_reports_drift_when_the_currency_date_advances():
    advanced = b"<html>Text contains those laws in effect on March 3, 2026</html>"
    report = check(_uscode_source(), fetch=_fetch(body=advanced, media_type="text/html"), env={})
    assert report.status == "drift"
    assert report.actual == "2026-03-03"


def test_uscode_currency_date_parsed_from_the_page():
    assert uscode.drift_value(USCODE_PAGE) == "2026-01-05"
    with pytest.raises(ValueError, match="no 'laws in effect on'"):
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


def test_archive_returns_none_when_the_service_raises():
    def headers_fn(url, headers=None):
        raise OSError("503")

    assert archive(ECFR_URL, headers_fn=headers_fn, env={}) is None


def test_archive_returns_none_without_a_content_location():
    assert archive(ECFR_URL, headers_fn=lambda u, h=None: {"Server": "nginx"}, env={}) is None


def test_archive_sends_spn2_auth_when_keys_are_set():
    seen = {}

    def headers_fn(url, headers=None):
        seen["headers"] = headers
        return {}

    archive(
        ECFR_URL,
        headers_fn=headers_fn,
        env={"WAYBACK_ACCESS_KEY": "k", "WAYBACK_SECRET_KEY": "s"},
    )
    assert seen["headers"] == {"Authorization": "LOW k:s"}


# --------------------------------------------------------------------------- emitting


def test_ledger_markdown_format():
    # The title already carries the as-of date, so the Retrieved clause must not repeat it.
    source = _pinned()
    assert to_ledger_markdown(source).splitlines() == [
        "- **Office of the Federal Register, 2026-09-14 (A1)** — 11 CFR Part 114, as of "
        "2026-09-14. Retrieved 2026-09-17; "
        f"sha256 {sha256_hex(BODY)[:16]}…; xr_src_0001.",
        f"  {ECFR_URL}",
        "  Archive: pending",
    ]


def test_ledger_markdown_adds_as_of_when_the_title_does_not_carry_it():
    # The other branch. A title that doesn't state its date still gets one, so the point in time
    # is never left unsaid.
    source = _pinned().model_copy(update={"title": "11 CFR Part 114"})
    line = to_ledger_markdown(source).splitlines()[0]
    assert "11 CFR Part 114. Retrieved 2026-09-17, as of 2026-09-14;" in line


def test_ledger_markdown_uses_the_archive_when_there_is_one():
    source = _pinned().model_copy(
        update={"archives": [ArchiveCopy(service="wayback", url="https://web.archive.org/web/1/x")]}
    )
    assert "Archive: https://web.archive.org/web/1/x" in to_ledger_markdown(source)


def test_manifest_line_is_sha256sum_format():
    assert to_manifest_line(_pinned()) == f"{sha256_hex(BODY)}  11-c-f-r-part-114.xml"


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
    # The fetch worked; the document no longer states its currency date. That is exit 1, not 3.
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
