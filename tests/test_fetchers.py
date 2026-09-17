"""Each fetcher builds the right URL, dates and grade from a trimmed inline fixture.

The fixtures hold only the keys `spec()` actually reads — a full API response would bury what is
being asserted and would rot the moment the service adds a field. Nothing here touches the network.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from registers_crosswalk.fetchers import (
    MissingKey,
    courtlistener,
    ecfr,
    federalregister,
    govinfo,
    manual,
    openfec,
    uscode,
)

REPO = Path(__file__).resolve().parents[1]


def _json_fetch(payload, media_type="application/json"):
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        return json.dumps(payload).encode(), media_type

    fetch.calls = calls
    return fetch


# --------------------------------------------------------------------------- ecfr


def test_ecfr_spec_builds_the_point_in_time_url():
    spec = ecfr.spec(title=11, part="114", as_of=date(2026, 9, 14))
    assert spec.canonical_url == (
        "https://www.ecfr.gov/api/versioner/v1/full/2026-09-14/title-11.xml?part=114"
    )
    assert spec.citation == "11 CFR Part 114"
    assert spec.point_in_time == date(2026, 9, 14)
    assert spec.published_at is None
    assert spec.grade.code() == "A1"
    assert spec.drift_key == "sha256"
    assert spec.publisher == "Office of the Federal Register"


def test_ecfr_versions_url():
    assert ecfr.versions_url(11, "114") == (
        "https://www.ecfr.gov/api/versioner/v1/versions/title-11.json?part=114"
    )


def test_ecfr_amended_since_reads_title_and_part_back_out_of_the_url():
    spec = ecfr.spec(title=11, part="114", as_of=date(2026, 9, 14))
    seen = []

    def fetch(url, headers=None):
        seen.append(url)
        return json.dumps({"content_versions": []}).encode(), "application/json"

    source = _as_source(spec)
    assert ecfr.amended_since(source, fetch=fetch) is None
    assert seen == [ecfr.versions_url("11", "114")]


# --------------------------------------------------------------------------- federalregister

FR_DOCUMENT = {
    "citation": "60 FR 7862",
    "title": "Express Advocacy; Independent Expenditures; Corporate and Labor Organization",
    "publication_date": "1995-02-09",
    "pdf_url": "https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf",
}


def test_federalregister_spec_reads_the_document_endpoint():
    fetch = _json_fetch(FR_DOCUMENT)
    spec = federalregister.spec(document_number="95-3162", fetch=fetch)
    assert fetch.calls[0][0] == ("https://www.federalregister.gov/api/v1/documents/95-3162.json")
    assert spec.canonical_url == FR_DOCUMENT["pdf_url"]
    assert spec.citation == "60 FR 7862"
    assert spec.published_at == date(1995, 2, 9)
    assert spec.point_in_time is None  # a published notice has no version axis
    assert spec.grade.code() == "A1"


def test_federalregister_falls_back_to_the_govinfo_pdf():
    spec = federalregister.spec(
        document_number="95-3162", fetch=_json_fetch({**FR_DOCUMENT, "pdf_url": None})
    )
    assert spec.canonical_url == (
        "https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf"
    )


# --------------------------------------------------------------------------- govinfo

GOVINFO_SUMMARY = {
    "title": "United States Code, 2023 Edition, Title 52",
    "dateIssued": "2024-01-08",
    "download": {"pdfLink": "https://www.govinfo.gov/content/pkg/USCODE-2023-title52/pdf/x.pdf"},
}


def test_govinfo_spec_keeps_the_key_off_canonical_url():
    fetch = _json_fetch(GOVINFO_SUMMARY)
    spec = govinfo.spec(
        package="USCODE-2023-title52",
        citation="52 U.S.C. (2023 ed.)",
        fetch=fetch,
        env={"GOVINFO_API_KEY": "SECRET"},
    )
    # the metadata call carries the key...
    assert fetch.calls[0][0] == (
        "https://api.govinfo.gov/packages/USCODE-2023-title52/summary?api_key=SECRET"
    )
    # ...what we store does not, and the fetch url re-attaches it
    assert spec.canonical_url == GOVINFO_SUMMARY["download"]["pdfLink"]
    assert "api_key" not in spec.canonical_url
    assert spec.fetch_url.endswith("?api_key=SECRET")
    assert spec.published_at == date(2024, 1, 8)
    assert spec.grade.code() == "A1"


def test_govinfo_granule_uses_the_same_path():
    fetch = _json_fetch(GOVINFO_SUMMARY)
    govinfo.spec(
        package="USCODE-2023-title52",
        granule="USCODE-2023-title52-subtitleIII",
        fetch=fetch,
        env={"GOVINFO_API_KEY": "SECRET"},
    )
    assert "/granules/USCODE-2023-title52-subtitleIII/summary" in fetch.calls[0][0]


def test_govinfo_strips_a_query_string_the_api_hands_back():
    payload = {
        **GOVINFO_SUMMARY,
        "download": {"pdfLink": "https://www.govinfo.gov/content/x.pdf?api_key=LEAKED"},
    }
    spec = govinfo.spec(package="X", fetch=_json_fetch(payload), env={"GOVINFO_API_KEY": "SECRET"})
    assert spec.canonical_url == "https://www.govinfo.gov/content/x.pdf"


def test_govinfo_without_a_key_raises_missing_key():
    with pytest.raises(MissingKey, match="GOVINFO_API_KEY"):
        govinfo.spec(package="X", fetch=_json_fetch(GOVINFO_SUMMARY), env={})


def test_govinfo_content_request_reattaches_the_key():
    url, headers = govinfo.content_request(
        "https://www.govinfo.gov/content/x.pdf", env={"GOVINFO_API_KEY": "SECRET"}
    )
    assert url == "https://www.govinfo.gov/content/x.pdf?api_key=SECRET"
    assert headers == {}


# --------------------------------------------------------------------------- uscode

USCODE_PAGE = b"""<html><body>
<p>Current through Pub. L. 119-20.</p>
<p>Text contains those laws in effect on January 5, 2026</p>
</body></html>"""


def test_uscode_spec_takes_its_point_in_time_from_the_page():
    def fetch(url, headers=None):
        return USCODE_PAGE, "text/html"

    spec = uscode.spec(title=52, section="30116", fetch=fetch)
    assert spec.canonical_url == (
        "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title52-section30116"
        "&num=0&edition=prelim"
    )
    assert spec.citation == "52 U.S.C. § 30116"
    assert spec.point_in_time == date(2026, 1, 5)
    assert spec.drift_key == "currency_date"
    assert spec.drift_value(USCODE_PAGE) == "2026-01-05"
    assert spec.grade.code() == "A1"


# --------------------------------------------------------------------------- openfec

OPENFEC_SEARCH = {
    "advisory_opinions": [
        {
            "no": "2023-01",
            "name": "Contributions by a federal contractor",
            "issue_date": "2023-05-11T00:00:00",
            "documents": [
                {"category": "AO Request", "url": "/files/legal/aos/2023-01/2023-01R.pdf"},
                {
                    "category": "Final Opinion",
                    "description": "Final Opinion",
                    "date": "2023-05-11T00:00:00",
                    "url": "/files/legal/aos/2023-01/2023-01.pdf",
                },
            ],
        }
    ]
}


def test_openfec_picks_the_final_opinion_and_joins_the_relative_url():
    fetch = _json_fetch(OPENFEC_SEARCH)
    spec = openfec.spec(number="2023-01", fetch=fetch, env={"OPENFEC_API_KEY": "SECRET"})
    assert fetch.calls[0][0] == (
        "https://api.open.fec.gov/v1/legal/search/"
        "?type=advisory_opinions&ao_no=2023-01&api_key=SECRET"
    )
    assert spec.canonical_url == "https://www.fec.gov/files/legal/aos/2023-01/2023-01.pdf"
    assert spec.citation == "FEC Advisory Opinion 2023-01"
    assert spec.published_at == date(2023, 5, 11)
    assert spec.grade.code() == "A1"


def test_openfec_can_pin_another_category():
    spec = openfec.spec(
        number="2023-01",
        category="AO Request",
        fetch=_json_fetch(OPENFEC_SEARCH),
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert spec.canonical_url.endswith("2023-01R.pdf")


def test_openfec_missing_category_raises():
    with pytest.raises(ValueError, match="no document with category"):
        openfec.spec(
            number="2023-01",
            category="Concurring Opinion",
            fetch=_json_fetch(OPENFEC_SEARCH),
            env={"OPENFEC_API_KEY": "SECRET"},
        )


def test_openfec_without_a_key_raises_missing_key():
    with pytest.raises(MissingKey, match="OPENFEC_API_KEY"):
        openfec.spec(number="2023-01", fetch=_json_fetch(OPENFEC_SEARCH), env={})


# --------------------------------------------------------------------------- courtlistener

CL_CLUSTER = {
    "case_name": "Citizens United v. FEC",
    "date_filed": "2010-01-21",
    "citations": [{"volume": 558, "reporter": "U.S.", "page": "310"}],
    "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/1/"],
    "court": "Supreme Court of the United States",
}


def _cl_fetch(cluster, opinion):
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        payload = opinion if "/opinions/" in url else cluster
        return json.dumps(payload).encode(), "application/json"

    fetch.calls = calls
    return fetch


def test_courtlistener_prefers_the_courts_own_pdf():
    fetch = _cl_fetch(CL_CLUSTER, {"download_url": "https://supremecourt.gov/opinions/08-205.pdf"})
    spec = courtlistener.spec(cluster_id=1, fetch=fetch, env={"COURTLISTENER_TOKEN": "T"})
    assert fetch.calls[0] == (
        "https://www.courtlistener.com/api/rest/v4/clusters/1/",
        {"Authorization": "Token T"},
    )
    assert fetch.calls[1][0] == CL_CLUSTER["sub_opinions"][0]
    assert spec.canonical_url == "https://supremecourt.gov/opinions/08-205.pdf"
    assert spec.citation == "558 U.S. 310"
    assert spec.published_at == date(2010, 1, 21)
    assert spec.grade.code() == "A1"


def test_courtlistener_falls_back_to_the_mirror_at_a_lower_grade():
    fetch = _cl_fetch(CL_CLUSTER, {"local_path": "pdf/2010/01/21/citizens_united.pdf"})
    spec = courtlistener.spec(cluster_id=1, fetch=fetch, env={"COURTLISTENER_TOKEN": "T"})
    assert spec.canonical_url == (
        "https://storage.courtlistener.com/pdf/2010/01/21/citizens_united.pdf"
    )
    assert spec.grade.code() == "B2"  # a faithful copy is still a copy


def test_courtlistener_tolerates_a_missing_date_filed():
    # Decision 9: date_filed placement is unverified, so its absence must not raise.
    cluster = {k: v for k, v in CL_CLUSTER.items() if k != "date_filed"}
    spec = courtlistener.spec(
        cluster_id=1,
        fetch=_cl_fetch(cluster, {"download_url": "https://supremecourt.gov/x.pdf"}),
        env={"COURTLISTENER_TOKEN": "T"},
    )
    assert spec.published_at is None


def test_courtlistener_without_a_token_raises_missing_key():
    with pytest.raises(MissingKey, match="COURTLISTENER_TOKEN"):
        courtlistener.spec(cluster_id=1, fetch=_cl_fetch(CL_CLUSTER, {}), env={})


# --------------------------------------------------------------------------- manual


def test_manual_takes_the_callers_grade():
    spec = manual.spec(
        url="https://www.fec.gov/updates/statement-of-the-chair/",
        citation="FEC Chair statement, 2026-04-02",
        title="Statement of the Chair",
        publisher="Federal Election Commission",
        published_at=date(2026, 4, 2),
        reliability="A",
        credibility=2,
    )
    assert spec.fetcher == "manual"
    assert spec.grade.code() == "A2"
    assert spec.drift_key == "sha256"


# --------------------------------------------------------------------------- key discipline


def _as_source(spec):
    from registers_crosswalk.models import Artifact, Source

    return Source(
        xr_id="xr_src_0001",
        kind="source",
        citation=spec.citation,
        title=spec.title,
        canonical_url=spec.canonical_url,
        fetcher=spec.fetcher,
        point_in_time=spec.point_in_time,
        published_at=spec.published_at,
        artifact=Artifact(
            sha256="a" * 64,
            byte_length=1,
            media_type="application/xml",
            fetched_at="2026-09-17T12:00:00Z",
            drift_key=spec.drift_key,
            drift_value="a" * 64,
        ),
        grade=spec.grade,
    )


def test_no_built_canonical_url_carries_a_key():
    specs = [
        ecfr.spec(title=11, part="114", as_of=date(2026, 9, 14)),
        federalregister.spec(document_number="95-3162", fetch=_json_fetch(FR_DOCUMENT)),
        govinfo.spec(
            package="X", fetch=_json_fetch(GOVINFO_SUMMARY), env={"GOVINFO_API_KEY": "SECRET"}
        ),
        uscode.spec(title=52, section="30116", fetch=lambda u, h=None: (USCODE_PAGE, "text/html")),
        openfec.spec(
            number="2023-01", fetch=_json_fetch(OPENFEC_SEARCH), env={"OPENFEC_API_KEY": "SECRET"}
        ),
        courtlistener.spec(
            cluster_id=1,
            fetch=_cl_fetch(CL_CLUSTER, {"download_url": "https://supremecourt.gov/x.pdf"}),
            env={"COURTLISTENER_TOKEN": "SECRET"},
        ),
    ]
    for spec in specs:
        lowered = spec.canonical_url.lower()
        assert "api_key=" not in lowered
        assert "access_key=" not in lowered
        assert "token=" not in lowered
        assert "SECRET" not in spec.canonical_url
        # and the model would refuse it even if a fetcher slipped
        _as_source(spec)


def test_no_committed_source_file_contains_an_api_key():
    # Decision 1, enforced on the committed data rather than only on the code path that writes it.
    for p in (REPO / "data" / "sources").glob("*.json"):
        text = p.read_text(encoding="utf-8").lower()
        assert "api_key=" not in text, p
        assert "access_key=" not in text, p
        assert "token=" not in text, p


# --------------------------------------------------------------- per-fetcher verification stamp


def test_every_fetcher_declares_its_verification_state():
    from registers_crosswalk import fetchers as reg

    for name in reg.NAMES:
        module = reg.get(name)
        assert isinstance(module.VERIFIED, bool), name
        # the claim and its date travel together at the module level too
        assert (module.VERIFIED_AT is not None) == module.VERIFIED, name


def test_courtlistener_ships_unverified():
    # Decision 9: no token was available, so its field mapping was never exercised.
    assert courtlistener.VERIFIED is False
    assert courtlistener.VERIFIED_AT is None
    spec = courtlistener.spec(
        cluster_id=1,
        fetch=_cl_fetch(CL_CLUSTER, {"download_url": "https://supremecourt.gov/x.pdf"}),
        env={"COURTLISTENER_TOKEN": "T"},
    )
    assert spec.fetcher_verified is False
    assert spec.verified_at is None


@pytest.mark.parametrize("module", [ecfr, manual])
def test_exercised_fetchers_ship_verified(module):
    # Only fetchers actually run from this tree: ecfr (a live `add` on 2026-09-17) and manual
    # (no field mapping exists to be wrong). Everything else waits for its own live run.
    assert module.VERIFIED is True
    assert module.VERIFIED_AT == date(2026, 9, 17)


@pytest.mark.parametrize("module", [federalregister, govinfo, uscode, openfec, courtlistener])
def test_unexercised_fetchers_ship_unverified(module):
    # A claim recorded in a handoff or a docstring is not a verification. These flip one at a
    # time, each on its own live `add` in this tree, dated the day it ran.
    assert module.VERIFIED is False
    assert module.VERIFIED_AT is None


def test_the_stamp_reaches_the_minted_record():
    from registers_crosswalk.pin import pin

    unverified = pin(
        courtlistener.spec(
            cluster_id=1,
            fetch=_cl_fetch(CL_CLUSTER, {"download_url": "https://supremecourt.gov/x.pdf"}),
            env={"COURTLISTENER_TOKEN": "T"},
        ),
        next_id="xr_src_0001",
        fetch=lambda u, h=None: (b"%PDF fake", "application/pdf"),
    )
    assert unverified.fetcher_verified is False
    assert unverified.verified_at is None
    assert unverified.model_dump()["fetcher_verified"] is False  # and it serializes

    verified = pin(
        ecfr.spec(title=11, part="114", as_of=date(2026, 9, 14)),
        next_id="xr_src_0002",
        fetch=lambda u, h=None: (b"<ECFR/>", "application/xml"),
    )
    assert verified.fetcher_verified is True
    assert verified.verified_at == date(2026, 9, 17)
