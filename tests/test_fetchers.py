"""Each fetcher builds the right URL, dates and grade from a trimmed inline fixture.

The fixtures hold only the keys `spec()` actually reads — a full API response would bury what is
being asserted and would rot the moment the service adds a field. Nothing here touches the network.
"""

import argparse
import json
from datetime import date
from pathlib import Path

import pytest

from registers_crosswalk.fetchers import (
    MissingKey,
    courtlistener,
    ecfr,
    fecfiling,
    federalregister,
    govinfo,
    manual,
    openfec,
    uscode,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(stem: str) -> dict:
    """Load a CAPTURED live API response. See docs/operations.md: fixtures for external APIs are
    captured and dated, never authored — an authored fixture tests the author's assumptions."""
    matches = sorted(FIXTURES.glob(f"{stem}_*.json"))
    if not matches:
        raise AssertionError(f"no captured fixture for {stem!r} under {FIXTURES}")
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def load_page(stem: str) -> bytes:
    """Load a CAPTURED live HTML page, as bytes. Same discipline as load_fixture: captured, dated,
    never authored, and never trimmed."""
    matches = sorted(FIXTURES.glob(f"{stem}_*.html"))
    if not matches:
        raise AssertionError(f"no captured page for {stem!r} under {FIXTURES}")
    return matches[-1].read_bytes()


def _versions_fetch(stem: str):
    def fetch(url, headers=None):
        return json.dumps(load_fixture(stem)).encode(), "application/json"

    return fetch


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

FR_DOCUMENT_STEM = "federalregister_document_95-3162"

# Every top-level key the live document endpoint returned for 95-3162 on 2026-09-18.
LIVE_FR_DOCUMENT_KEYS = {
    "abstract",
    "action",
    "agencies",
    "body_html_url",
    "cfr_references",
    "citation",
    "comment_url",
    "comments_close_on",
    "correction_of",
    "corrections",
    "dates",
    "disposition_notes",
    "docket_ids",
    "dockets",
    "document_number",
    "effective_on",
    "end_page",
    "executive_order_notes",
    "executive_order_number",
    "full_text_xml_url",
    "html_url",
    "images",
    "images_metadata",
    "json_url",
    "mods_url",
    "not_received_for_publication",
    "page_length",
    "page_views",
    "pdf_url",
    "presidential_document_number",
    "proclamation_number",
    "public_inspection_pdf_url",
    "publication_date",
    "raw_text_url",
    "regulation_id_number_info",
    "regulation_id_numbers",
    "regulations_dot_gov_info",
    "regulations_dot_gov_url",
    "significant",
    "signing_date",
    "start_page",
    "subtype",
    "title",
    "toc_doc",
    "toc_subject",
    "topics",
    "type",
    "volume",
}


def test_captured_fr_document_matches_the_observed_live_key_set():
    # Guards both directions: an invented field fails, a dropped field fails. If the Federal
    # Register really does change its payload, this is where it surfaces, and the fixture must be
    # RE-CAPTURED rather than edited by hand.
    assert set(load_fixture(FR_DOCUMENT_STEM)) == LIVE_FR_DOCUMENT_KEYS


def test_federalregister_spec_reads_the_document_endpoint():
    payload = load_fixture(FR_DOCUMENT_STEM)
    fetch = _json_fetch(payload)
    spec = federalregister.spec(document_number="95-3162", fetch=fetch)
    assert fetch.calls[0][0] == ("https://www.federalregister.gov/api/v1/documents/95-3162.json")
    assert spec.canonical_url == payload["pdf_url"]
    assert spec.citation == "60 FR 7862"
    assert spec.title == payload["title"]
    assert spec.published_at == date(1995, 2, 9)
    assert spec.point_in_time is None  # a published notice has no version axis
    assert spec.grade.code() == "A1"


def test_federalregister_falls_back_to_the_govinfo_pdf():
    # The live payload cannot produce this case: for 95-3162 the API's own pdf_url is already the
    # URL fallback_pdf_url() builds, so the fallback never fires against the real response. Mutate
    # a copy to null it out, which is the only way to exercise the construction.
    payload = {**load_fixture(FR_DOCUMENT_STEM), "pdf_url": None}
    assert set(payload) == LIVE_FR_DOCUMENT_KEYS  # the mutation stayed inside the observed shape
    spec = federalregister.spec(document_number="95-3162", fetch=_json_fetch(payload))
    assert spec.canonical_url == (
        "https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf"
    )


# --------------------------------------------------------------------------- govinfo

GOVINFO_PACKAGE_STEM = "govinfo_summary_USCODE-2024-title52"
GOVINFO_GRANULE_STEM = "govinfo_summary_USCODE-2024-title52-subtitleIII-chap301-subchapI-sec30116"
GOVINFO_SUMMARY = load_fixture(GOVINFO_PACKAGE_STEM)
GOVINFO_GRANULE_SUMMARY = load_fixture(GOVINFO_GRANULE_STEM)
SEC_30116 = "USCODE-2024-title52-subtitleIII-chap301-subchapI-sec30116"

# Every top-level key the live summary endpoint returned on 2026-09-22, package and granule.
LIVE_GOVINFO_PACKAGE_KEYS = {
    "branch",
    "category",
    "collectionCode",
    "collectionName",
    "dateIssued",
    "detailsLink",
    "docClass",
    "documentType",
    "download",
    "governmentAuthor1",
    "governmentAuthor2",
    "granulesLink",
    "lastModified",
    "otherIdentifier",
    "packageId",
    "pages",
    "publisher",
    "suDocClassNumber",
    "title",
    "titleNumber",
}
LIVE_GOVINFO_GRANULE_KEYS = {
    "category",
    "collectionCode",
    "collectionName",
    "dateIssued",
    "detailsLink",
    "docClass",
    "download",
    "graphicsInPDF",
    "granuleClass",
    "granuleId",
    "granuleNumber",
    "granulesLink",
    "heading",
    "lastModified",
    "leafRange",
    "packageId",
    "packageLink",
    "relatedLink",
    "title",
}


@pytest.mark.parametrize(
    ("stem", "keys"),
    [
        (GOVINFO_PACKAGE_STEM, LIVE_GOVINFO_PACKAGE_KEYS),
        (GOVINFO_GRANULE_STEM, LIVE_GOVINFO_GRANULE_KEYS),
    ],
)
def test_captured_govinfo_summary_matches_the_observed_live_key_set(stem, keys):
    # Guards both directions, as for eCFR and FR. If GovInfo changes its payload, the fixture must
    # be RE-CAPTURED rather than edited by hand.
    assert set(load_fixture(stem)) == keys


def test_govinfo_spec_reads_the_captured_granule_summary():
    fetch = _json_fetch(GOVINFO_GRANULE_SUMMARY)
    spec = govinfo.spec(
        package="USCODE-2024-title52",
        granule=SEC_30116,
        citation="52 U.S.C. § 30116 (2024 ed.)",
        fetch=fetch,
        env={"GOVINFO_API_KEY": "SECRET"},
    )
    assert fetch.calls[0][0] == (
        f"https://api.govinfo.gov/packages/USCODE-2024-title52/granules/{SEC_30116}/summary"
        "?api_key=SECRET"
    )
    assert spec.title == "Limitations on contributions and expenditures"
    assert spec.published_at == date(2024, 12, 31)
    assert spec.citation == "52 U.S.C. § 30116 (2024 ed.)"
    assert spec.publisher == "U.S. Government Publishing Office"
    # the capture's own pdfLink is on the key-bearing API host; what is stored is not
    assert GOVINFO_GRANULE_SUMMARY["download"]["pdfLink"].startswith("https://api.govinfo.gov/")
    assert spec.canonical_url == (
        f"https://www.govinfo.gov/content/pkg/USCODE-2024-title52/pdf/{SEC_30116}.pdf"
    )
    assert spec.fetch_url is None
    assert spec.fetcher_verified is True
    assert spec.verified_at == date(2026, 9, 22)
    assert spec.grade.code() == "A1"


def test_govinfo_spec_keeps_the_key_off_canonical_url():
    fetch = _json_fetch(GOVINFO_SUMMARY)
    spec = govinfo.spec(
        package="USCODE-2024-title52",
        citation="52 U.S.C. (2024 ed.)",
        fetch=fetch,
        env={"GOVINFO_API_KEY": "SECRET"},
    )
    # the metadata call carries the key...
    assert fetch.calls[0][0] == (
        "https://api.govinfo.gov/packages/USCODE-2024-title52/summary?api_key=SECRET"
    )
    # ...what we store and fetch is the content-host copy, which carries none
    assert spec.canonical_url == (
        "https://www.govinfo.gov/content/pkg/USCODE-2024-title52/pdf/USCODE-2024-title52.pdf"
    )
    assert spec.fetch_url is None
    assert spec.title == "VOTING AND ELECTIONS"
    assert spec.published_at == date(2024, 12, 31)


def test_govinfo_stores_no_query_string_whatever_pdflink_carries():
    # The API's own pdfLink is on the key-bearing API host; it is never what is stored.
    payload = {
        **GOVINFO_SUMMARY,
        "download": {"pdfLink": "https://api.govinfo.gov/packages/X/pdf?api_key=LEAKED"},
    }
    spec = govinfo.spec(package="X", fetch=_json_fetch(payload), env={"GOVINFO_API_KEY": "SECRET"})
    assert spec.canonical_url == "https://www.govinfo.gov/content/pkg/X/pdf/X.pdf"
    assert "?" not in spec.canonical_url


def test_govinfo_refuses_a_summary_that_lists_no_pdf():
    payload = {**GOVINFO_SUMMARY, "download": {}}
    with pytest.raises(ValueError, match="lists no PDF"):
        govinfo.spec(package="X", fetch=_json_fetch(payload), env={"GOVINFO_API_KEY": "SECRET"})


def test_govinfo_without_a_key_raises_missing_key():
    with pytest.raises(MissingKey, match="GOVINFO_API_KEY"):
        govinfo.spec(package="X", fetch=_json_fetch(GOVINFO_SUMMARY), env={})


def test_govinfo_content_request_needs_no_key_on_the_content_host():
    # What `check` re-fetches for every govinfo pin: no key attached, none demanded.
    url, headers = govinfo.content_request(
        "https://www.govinfo.gov/content/pkg/X/pdf/X.pdf", env={}
    )
    assert url == "https://www.govinfo.gov/content/pkg/X/pdf/X.pdf"
    assert headers == {}


def test_govinfo_content_request_still_needs_the_key_on_the_api_host():
    with pytest.raises(MissingKey, match="GOVINFO_API_KEY"):
        govinfo.content_request("https://api.govinfo.gov/packages/X/pdf", env={})
    url, headers = govinfo.content_request(
        "https://api.govinfo.gov/packages/X/pdf", env={"GOVINFO_API_KEY": "SECRET"}
    )
    assert url == "https://api.govinfo.gov/packages/X/pdf?api_key=SECRET"
    assert headers == {}


# --------------------------------------------------------------------------- uscode

USCODE_STEM = "uscode_page_title1_section1"

# What the captured 1 U.S.C. 1 page stated on 2026-09-18. The source credit runs from a 1947
# chapter law to a 2012 Pub. L., so it exercises both citation forms; the currency date is
# site-wide and eight months younger than the section's own latest amendment, which is the whole
# reason these are two different signals.
USCODE_LAST_AMENDED = "2012-12-28"
USCODE_CURRENCY = date(2026, 9, 17)


def test_captured_uscode_page_carries_both_anchors_the_parser_needs():
    # The HTML analogue of the key-set test: if OLRC drops either anchor, this is where it
    # surfaces, and the page must be RE-CAPTURED rather than edited by hand.
    page = load_page(USCODE_STEM)
    assert page.count(b'class="source-credit"') == 1
    assert b"laws in effect on" in page


def test_uscode_drift_value_is_the_source_credits_latest_date():
    page = load_page(USCODE_STEM)
    assert uscode.drift_value(page) == USCODE_LAST_AMENDED
    assert uscode.last_amended(page) == date(2012, 12, 28)


def test_uscode_source_credit_covers_chapter_and_public_law_forms():
    # Laws before 1957 are cited as chapters and carry no Pub. L. number, which is why the parser
    # keys on dates rather than on Pub. L. numbers.
    credit = uscode.source_credit(load_page(USCODE_STEM))
    assert "July 30, 1947, ch. 388" in credit
    assert "Pub. L. 112" in credit and "Dec. 28, 2012" in credit


def test_uscode_drift_value_ignores_later_dates_outside_the_source_credit():
    # The captured page carries many dates later than its credit's latest, in notes about laws
    # that did not amend the section. Scoping to the credit is what keeps them out.
    page = load_page(USCODE_STEM)
    assert b"2019" in page  # notes really do cite later years
    assert uscode.drift_value(page) == USCODE_LAST_AMENDED


def _with_credit_entry(page: bytes, entry: bytes) -> bytes:
    """Splice an entry into the captured page's source credit, just before it closes.

    The credit's text is broken up by <a> and <statuteAtLarge> tags, so a plain byte replace on a
    date would miss. Mutating a copy of the capture is the rule; this keeps the mutation inside the
    element under test.
    """
    start = page.index(b'class="source-credit"')
    end = page.index(b"</p>", start)
    return page[:end] + entry + page[end:]


def test_uscode_accepts_the_abbreviated_september_form():
    # The credits observed on 2026-09-18 write "Sept.", but "Sep." costs nothing to accept and an
    # unknown month form is now an error rather than a silent skip.
    page = _with_credit_entry(load_page(USCODE_STEM), b"; Pub. L. 119-1, Sep. 30, 2020")
    assert uscode.last_amended(page) == date(2020, 9, 30)


def test_uscode_raises_on_a_date_it_cannot_read():
    # The failure this guards: a month spelling the parser does not know used to vanish, which
    # LOWERS the latest date and hides the amendment it belongs to. Now it is an ERROR, which
    # `check` reports and a human looks at.
    page = _with_credit_entry(load_page(USCODE_STEM), b"; Pub. L. 119-2, Frob. 1, 2030")
    with pytest.raises(ValueError, match=r"Frob\. 1, 2030"):
        uscode.last_amended(page)


def test_uscode_raises_when_the_source_credit_is_gone():
    page = load_page(USCODE_STEM).replace(b'class="source-credit"', b'class="gone"', 1)
    with pytest.raises(ValueError, match="no source-credit element"):
        uscode.drift_value(page)


def test_uscode_spec_takes_its_point_in_time_from_the_page():
    page = load_page(USCODE_STEM)

    def fetch(url, headers=None):
        return page, "text/html"

    spec = uscode.spec(title=52, section="30116", fetch=fetch)
    assert spec.canonical_url == (
        "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title52-section30116"
        "&num=0&edition=prelim"
    )
    assert spec.citation == "52 U.S.C. § 30116"
    # the point in time is still the page's own "laws in effect on" claim...
    assert spec.point_in_time == USCODE_CURRENCY
    # ...but the drift signal is not
    assert spec.drift_key == "last_amended"
    assert spec.drift_value(page) == USCODE_LAST_AMENDED
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


# The MUR path, on the two responses captured live 2026-09-21 — FEC MURs 8098 and 8111, the Cory
# Mills matters the claims ledger cites. Untrimmed, per docs/operations.md. These are the first
# captured openfec fixtures; the advisory-opinion dict above is still the authored one it always
# was, and the AO path is unchanged by this work.

MUR_8098_STEM = "openfec_search_murs_8098"
MUR_8111_STEM = "openfec_search_murs_8111"

# Observed on both captures.
LIVE_MUR_RECORD_KEYS = {
    "case_serial",
    "close_date",
    "commission_votes",
    "dispositions",
    "doc_id",
    "document_highlights",
    "documents",
    "election_cycles",
    "highlights",
    "mur_type",
    "name",
    "no",
    "open_date",
    "participants",
    "published_flg",
    "respondents",
    "source",
    "subjects",
    "type",
    "url",
}
LIVE_MUR_DOCUMENT_KEYS = {
    "category",
    "description",
    "doc_order_id",
    "document_date",
    "document_id",
    "filename",
    "length",
    "url",
}

# The 2024-07-23 certification of the 6-0 dismissal vote, and the First General Counsel's Report.
CERT_8098 = "100512215"
FGCR_8098 = "100512224"
CERT_8111 = "100512230"


def _mur_fetch(stem):
    return _json_fetch(load_fixture(stem))


@pytest.mark.parametrize("stem", [MUR_8098_STEM, MUR_8111_STEM])
def test_captured_openfec_mur_fixture_matches_the_observed_live_key_set(stem):
    # Guards both directions: an invented field fails, a dropped field fails. If OpenFEC really
    # does change its payload, this is where it surfaces, and the fixture must be RE-CAPTURED
    # rather than edited by hand.
    payload = load_fixture(stem)
    assert set(payload) == {"murs", "total_all", "total_murs"}
    assert payload["total_murs"] == 1
    record = payload["murs"][0]
    assert set(record) == LIVE_MUR_RECORD_KEYS
    assert {k for d in record["documents"] for k in d} == LIVE_MUR_DOCUMENT_KEYS


def test_openfec_murs_query_by_case_no_not_mur_no():
    """`mur_no` is not rejected by the endpoint, it is ignored.

    Asked with `mur_no` the API answers 200 with the unfiltered first page of all 7,670 matters,
    and `_pick_document` would have pinned a document belonging to another MUR under the citation
    it was asked for. Observed 2026-09-21; this test is the guard on the parameter name.
    """
    fetch = _mur_fetch(MUR_8098_STEM)
    openfec.spec(
        number="8098",
        doc_type="murs",
        document_id=CERT_8098,
        fetch=fetch,
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert fetch.calls[0][0] == (
        "https://api.open.fec.gov/v1/legal/search/?type=murs&case_no=8098&api_key=SECRET"
    )


def test_openfec_selects_a_mur_document_by_id():
    spec = openfec.spec(
        number="8098",
        doc_type="murs",
        document_id=CERT_8098,
        fetch=_mur_fetch(MUR_8098_STEM),
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert spec.canonical_url == "https://www.fec.gov/files/legal/murs/8098/8098_12.pdf"
    assert spec.citation == "FEC MUR 8098"
    assert spec.title.startswith("Cory Mills; Cory Mills for Congress")
    assert spec.published_at == date(2024, 7, 23)  # from documents[].document_date
    assert spec.publisher == "Federal Election Commission"
    assert spec.grade.code() == "A1"


def test_openfec_reaches_both_documents_of_one_category_by_id():
    """A MUR repeats its categories; selection by category could reach only the first.

    Both of 8098's certifications are `Certifications`: the 2024-07-23 certification of the 6-0
    dismissal, and the 2024-08-06 substitution of a treasurer's name. The category rule returns
    whichever the capture lists first, which is why `--document` exists.
    """
    record = load_fixture(MUR_8098_STEM)["murs"][0]
    certs = [d for d in record["documents"] if d["category"] == "Certifications"]
    assert len(certs) == 2

    urls = {}
    for document in certs:
        spec = openfec.spec(
            number="8098",
            doc_type="murs",
            document_id=document["document_id"],
            fetch=_mur_fetch(MUR_8098_STEM),
            env={"OPENFEC_API_KEY": "SECRET"},
        )
        urls[str(document["document_id"])] = spec.canonical_url
    assert len(set(urls.values())) == 2
    assert urls[CERT_8098].endswith("8098_12.pdf")


def test_openfec_refuses_an_unknown_document_id():
    with pytest.raises(ValueError, match="no document with document_id"):
        openfec.spec(
            number="8098",
            doc_type="murs",
            document_id="not-a-document",
            fetch=_mur_fetch(MUR_8098_STEM),
            env={"OPENFEC_API_KEY": "SECRET"},
        )


def test_openfec_selection_by_category_still_works_on_a_mur():
    spec = openfec.spec(
        number="8098",
        doc_type="murs",
        category="Certifications",
        fetch=_mur_fetch(MUR_8098_STEM),
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert "/files/legal/murs/8098/" in spec.canonical_url


def test_openfec_stores_a_key_free_url_on_www_fec_gov():
    """The key is for the search call only; `check` re-fetches the stored URL without one."""
    for stem, number, document_id in [
        (MUR_8098_STEM, "8098", CERT_8098),
        (MUR_8111_STEM, "8111", CERT_8111),
    ]:
        spec = openfec.spec(
            number=number,
            doc_type="murs",
            document_id=document_id,
            fetch=_mur_fetch(stem),
            env={"OPENFEC_API_KEY": "SECRET"},
        )
        assert spec.canonical_url.startswith("https://www.fec.gov/files/legal/murs/")
        assert "api_key" not in spec.canonical_url
        assert "SECRET" not in spec.canonical_url


def test_openfec_citation_override_lands():
    spec = openfec.spec(
        number="8098",
        doc_type="murs",
        document_id=CERT_8098,
        citation="FEC MUR 8098 (Cory Mills)",
        fetch=_mur_fetch(MUR_8098_STEM),
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert spec.citation == "FEC MUR 8098 (Cory Mills)"


def test_openfec_mur_default_citation_is_the_number():
    spec = openfec.spec(
        number="8111",
        doc_type="murs",
        document_id=CERT_8111,
        fetch=_mur_fetch(MUR_8111_STEM),
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert spec.citation == "FEC MUR 8111"


def test_openfec_reads_the_document_date_not_a_record_date():
    """`document_date` is the MUR shape. No MUR record carries `issue_date` at all.

    The First General Counsel's Report is dated 2024-06-13 while the matter opened 2023-01-11 and
    closed 2024-09-05, so a fallback onto a record-level date would be visibly wrong here.
    """
    record = load_fixture(MUR_8098_STEM)["murs"][0]
    assert "issue_date" not in record
    assert record["open_date"].startswith("2023-01-11")
    spec = openfec.spec(
        number="8098",
        doc_type="murs",
        document_id=FGCR_8098,
        fetch=_mur_fetch(MUR_8098_STEM),
        env={"OPENFEC_API_KEY": "SECRET"},
    )
    assert spec.title == "First General Counsel's Report"
    assert spec.published_at == date(2024, 6, 13)


def test_openfec_murs_without_a_key_raises_missing_key():
    with pytest.raises(MissingKey, match="OPENFEC_API_KEY"):
        openfec.spec(
            number="8098",
            doc_type="murs",
            document_id=CERT_8098,
            fetch=_mur_fetch(MUR_8098_STEM),
            env={},
        )


def test_openfec_add_command_for_a_mur_names_the_document_flag():
    hit = openfec._hit(load_fixture(MUR_8098_STEM)["murs"][0], "murs")
    assert hit.identifier == "8098"
    assert hit.citation == "FEC MUR 8098"
    assert openfec.add_command(hit, "murs") == (
        "pin add openfec --number 8098 --type murs --document <document_id>"
    )


# --------------------------------------------------------------------------- courtlistener
#
# Captured live 2026-09-20 from cluster 1481640 — Dunne v. United States, 138 F.2d 137
# (8th Cir. 1943), the first court opinion New Gray's source-links ledger cites. Four responses,
# one per call `spec()` makes. Untrimmed, per docs/operations.md.

CL_CLUSTER_STEM = "courtlistener_cluster_1481640"
CL_OPINION_STEM = "courtlistener_opinion_1481640"
CL_DOCKET_STEM = "courtlistener_docket_2577633"
CL_COURT_STEM = "courtlistener_court_ca8"

CL_CLUSTER_URL = "https://www.courtlistener.com/api/rest/v4/clusters/1481640/"
CL_OPINION_URL = "https://www.courtlistener.com/api/rest/v4/opinions/1481640/"
CL_DOCKET_URL = "https://www.courtlistener.com/api/rest/v4/dockets/2577633/"
CL_COURT_URL = "https://www.courtlistener.com/api/rest/v4/courts/ca8/"
CL_TOKEN = {"COURTLISTENER_TOKEN": "T"}

# Every top-level key each live endpoint returned on 2026-09-20.
LIVE_CL_CLUSTER_KEYS = {
    "absolute_url",
    "arguments",
    "attorneys",
    "blocked",
    "case_name",
    "case_name_full",
    "case_name_short",
    "citation_count",
    "citations",
    "cluster_redirections",
    "correction",
    "cross_reference",
    "date_blocked",
    "date_created",
    "date_filed",
    "date_filed_is_approximate",
    "date_modified",
    "disposition",
    "docket",
    "docket_id",
    "filepath_json_harvard",
    "filepath_pdf_harvard",
    "filepath_pdf_scan",
    "filepath_xml_scan",
    "headmatter",
    "headnotes",
    "history",
    "id",
    "judges",
    "nature_of_suit",
    "non_participating_judges",
    "other_dates",
    "panel",
    "posture",
    "precedential_status",
    "procedural_history",
    "resource_uri",
    "scdb_decision_direction",
    "scdb_id",
    "scdb_votes_majority",
    "scdb_votes_minority",
    "slug",
    "source",
    "sub_opinions",
    "summary",
    "syllabus",
}
LIVE_CL_OPINION_KEYS = {
    "absolute_url",
    "author",
    "author_id",
    "author_str",
    "cluster",
    "cluster_id",
    "date_created",
    "date_modified",
    "download_url",
    "extracted_by_ocr",
    "html",
    "html_anon_2020",
    "html_columbia",
    "html_lawbox",
    "html_with_citations",
    "id",
    "joined_by",
    "joined_by_str",
    "local_path",
    "main_version",
    "opinions_cited",
    "ordering_key",
    "page_count",
    "per_curiam",
    "plain_text",
    "resource_uri",
    "sha1",
    "type",
    "xml_harvard",
    "xml_scan",
}
LIVE_CL_COURT_KEYS = {
    "appeals_to",
    "citation_string",
    "date_last_pacer_contact",
    "date_modified",
    "end_date",
    "fjc_court_id",
    "full_name",
    "has_opinion_scraper",
    "has_oral_argument_scraper",
    "id",
    "in_use",
    "jurisdiction",
    "pacer_court_id",
    "pacer_has_rss_feed",
    "pacer_rss_entry_types",
    "parent_court",
    "position",
    "resource_uri",
    "short_name",
    "start_date",
    "url",
}


def _cl_fetch(cluster=None, opinion=None, docket=None, court=None):
    """Route the four calls `spec()` makes to the four captured responses.

    Any of them can be overridden with a mutated copy, which is how a branch the live payload
    cannot reach gets exercised — the same device as the federalregister fallback test above.
    """
    payloads = {
        "/clusters/": load_fixture(CL_CLUSTER_STEM) if cluster is None else cluster,
        "/opinions/": load_fixture(CL_OPINION_STEM) if opinion is None else opinion,
        "/dockets/": load_fixture(CL_DOCKET_STEM) if docket is None else docket,
        "/courts/": load_fixture(CL_COURT_STEM) if court is None else court,
    }
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        for marker, payload in payloads.items():
            if marker in url:
                return json.dumps(payload).encode(), "application/json"
        raise AssertionError(f"spec() asked for an unexpected URL: {url}")

    fetch.calls = calls
    return fetch


def _cl_spec(fetch, **over):
    return courtlistener.spec(cluster_id=1481640, fetch=fetch, env=CL_TOKEN, **over)


@pytest.mark.parametrize(
    ("stem", "keys"),
    [
        (CL_CLUSTER_STEM, LIVE_CL_CLUSTER_KEYS),
        (CL_OPINION_STEM, LIVE_CL_OPINION_KEYS),
        (CL_COURT_STEM, LIVE_CL_COURT_KEYS),
    ],
)
def test_captured_courtlistener_fixture_matches_the_observed_live_key_set(stem, keys):
    # Guards both directions: an invented field fails, a dropped field fails. If CourtListener
    # really does change its payload, this is where it surfaces, and the fixture must be
    # RE-CAPTURED rather than edited by hand.
    assert set(load_fixture(stem)) == keys


def test_courtlistener_pins_the_harvard_scan_when_there_is_no_court_pdf():
    # The real 2026-09-20 run, unmutated. A 1943 opinion has no court PDF and no CourtListener
    # mirror: the document is the Harvard Caselaw Access Project scan, named on the CLUSTER.
    fetch = _cl_fetch()
    spec = _cl_spec(fetch)
    assert [c[0] for c in fetch.calls] == [
        CL_CLUSTER_URL,
        CL_OPINION_URL,
        CL_DOCKET_URL,
        CL_COURT_URL,
    ]
    assert all(c[1] == {"Authorization": "Token T"} for c in fetch.calls)
    assert spec.canonical_url == "https://storage.courtlistener.com/harvard_pdf/1481640.pdf"
    assert spec.citation == "138 F.2d 137"  # the ledger's own citation, unedited
    assert spec.title == "Dunne v. United States"
    assert spec.published_at == date(1943, 9, 20)
    assert spec.grade.code() == "B1"


def test_courtlistener_names_the_deciding_court_not_the_archive():
    # A v4 cluster has no court key at all, so the publisher costs two more calls. Without them
    # this reads "CourtListener (Free Law Project)" on every record — the archive, not the court.
    assert "court" not in load_fixture(CL_CLUSTER_STEM)
    assert _cl_spec(_cl_fetch()).publisher == "Court of Appeals for the Eighth Circuit"


def test_courtlistener_prefers_the_courts_own_pdf():
    # The live capture cannot reach this branch — Dunne has no court PDF — so the opinion is
    # mutated to carry one. That tests OUR code, not their API; the claim that the API really
    # serves download_url this way is still owed a pin through a modern opinion.
    opinion = {
        **load_fixture(CL_OPINION_STEM),
        "download_url": "https://www.ca8.uscourts.gov/x.pdf",
    }
    assert set(opinion) == LIVE_CL_OPINION_KEYS  # the mutation stayed inside the observed shape
    spec = _cl_spec(_cl_fetch(opinion=opinion))
    assert spec.canonical_url == "https://www.ca8.uscourts.gov/x.pdf"
    assert spec.grade.code() == "A1"  # the publisher of record


def test_courtlistener_falls_back_to_the_mirror_at_a_lower_grade():
    # Same caveat: local_path has never been exercised against the live API, so this pins the
    # branch's behaviour, not the claim that storage.courtlistener.com is where it resolves.
    opinion = {**load_fixture(CL_OPINION_STEM), "local_path": "pdf/2003/04/01/dunne.pdf"}
    assert set(opinion) == LIVE_CL_OPINION_KEYS
    spec = _cl_spec(_cl_fetch(opinion=opinion))
    assert spec.canonical_url == "https://storage.courtlistener.com/pdf/2003/04/01/dunne.pdf"
    assert spec.grade.code() == "B2"  # a re-served copy, not an image of the cited page


def test_courtlistener_grades_the_harvard_scan_above_the_mirror():
    # The whole point of the third branch: both are served by the Free Law Project, so both are B,
    # but a page image of the reporter the citation names is better evidence than a re-served file.
    harvard = _cl_spec(_cl_fetch()).grade
    mirror = _cl_spec(
        _cl_fetch(opinion={**load_fixture(CL_OPINION_STEM), "local_path": "pdf/x.pdf"})
    ).grade
    assert harvard.reliability == mirror.reliability == "B"
    assert harvard.credibility < mirror.credibility


def test_courtlistener_raises_when_no_branch_has_a_document():
    cluster = {**load_fixture(CL_CLUSTER_STEM), "filepath_pdf_harvard": None}
    assert set(cluster) == LIVE_CL_CLUSTER_KEYS
    with pytest.raises(ValueError, match="no document"):
        _cl_spec(_cl_fetch(cluster=cluster))


def test_courtlistener_falls_back_to_the_archive_when_the_court_chain_breaks():
    cluster = {**load_fixture(CL_CLUSTER_STEM), "docket": None}
    fetch = _cl_fetch(cluster=cluster)
    spec = _cl_spec(fetch)
    assert spec.publisher == courtlistener.PUBLISHER
    # And it did not spend the two calls it could not use.
    assert [c[0] for c in fetch.calls] == [CL_CLUSTER_URL, CL_OPINION_URL]


def test_courtlistener_tolerates_a_missing_date_filed():
    # date_filed IS on the cluster (confirmed 2026-09-20), but absence must still not raise.
    cluster = {**load_fixture(CL_CLUSTER_STEM), "date_filed": None}
    assert set(cluster) == LIVE_CL_CLUSTER_KEYS
    assert _cl_spec(_cl_fetch(cluster=cluster)).published_at is None


def test_courtlistener_takes_an_explicit_citation_over_the_reporter():
    assert _cl_spec(_cl_fetch(), citation="Dunne v. United States (8th Cir. 1943)").citation == (
        "Dunne v. United States (8th Cir. 1943)"
    )


def test_courtlistener_without_a_token_raises_missing_key():
    with pytest.raises(MissingKey, match="COURTLISTENER_TOKEN"):
        courtlistener.spec(cluster_id=1481640, fetch=_cl_fetch(), env={})


def test_courtlistener_check_refetches_the_document_without_a_token():
    # What a courtlistener pin stores is a public file on storage.courtlistener.com, so re-fetching
    # it asks for no credential. That is what keeps such a pin checkable from a CI runner with no
    # secret set: an empty env must be fine here, and must not raise.
    url = "https://storage.courtlistener.com/harvard_pdf/1481640.pdf"
    assert url.startswith(courtlistener.STORAGE)
    assert courtlistener.content_request(url, env={}) == (url, {})
    # A court's own PDF is just as public.
    court_pdf = "https://www.ca8.uscourts.gov/opinions/12195.pdf"
    assert courtlistener.content_request(court_pdf, env={}) == (court_pdf, {})


def test_courtlistener_api_urls_still_need_a_token():
    # The other half of the rule: an API URL is authenticated, so a missing token stays an error
    # there rather than becoming a silent 401.
    api_url = courtlistener.cluster_url(1481640)
    with pytest.raises(MissingKey, match="COURTLISTENER_TOKEN"):
        courtlistener.content_request(api_url, env={})
    assert courtlistener.content_request(api_url, env={"COURTLISTENER_TOKEN": "T"}) == (
        api_url,
        {"Authorization": "Token T"},
    )


def test_the_committed_courtlistener_pin_needs_no_token_to_recheck():
    # The rule above, tied to the record that actually depends on it.
    from registers_crosswalk.registry import Crosswalk

    source = Crosswalk(REPO / "data").sources["xr_src_0006"]
    assert source.canonical_url.startswith(courtlistener.STORAGE)
    assert courtlistener.content_request(source.canonical_url, env={}) == (
        source.canonical_url,
        {},
    )


# --------------------------------------------------------------------------- fecfiling

# Committee-level metadata only. The Schedule A/B pages and the .fec contents Phase A read carry
# individuals' names and addresses and are deliberately NOT captured into this repo.
FECFILING_ORIGINAL_STEM = "fecfiling_filings_1903438"  # Q2 2025, amendment_indicator N
FECFILING_AMENDED_STEM = "fecfiling_filings_1997103"  # Q1 2026, amendment_indicator A
FECFILING_ORIGINAL = load_fixture(FECFILING_ORIGINAL_STEM)
FECFILING_AMENDED = load_fixture(FECFILING_AMENDED_STEM)
FEC_KEY = {"OPENFEC_API_KEY": "SECRET"}

# The result keys the live /v1/filings/ endpoint returned on 2026-09-23.
LIVE_FECFILING_RESULT_KEYS = {
    "additional_bank_names",
    "amendment_chain",
    "amendment_indicator",
    "amendment_version",
    "bank_depository_city",
    "bank_depository_name",
    "bank_depository_state",
    "bank_depository_street_1",
    "bank_depository_street_2",
    "bank_depository_zip",
    "beginning_image_number",
    "candidate_id",
    "candidate_name",
    "cash_on_hand_beginning_period",
    "cash_on_hand_end_period",
    "committee_id",
    "committee_name",
    "committee_type",
    "coverage_end_date",
    "coverage_start_date",
    "csv_url",
    "cycle",
    "debts_owed_by_committee",
    "debts_owed_to_committee",
    "document_description",
    "document_type",
    "document_type_full",
    "election_year",
    "ending_image_number",
    "fec_file_id",
    "fec_url",
    "file_number",
    "form_category",
    "form_type",
    "house_personal_funds",
    "html_url",
    "is_amended",
    "means_filed",
    "most_recent",
    "most_recent_file_number",
    "net_donations",
    "office",
    "opposition_personal_funds",
    "pages",
    "party",
    "pdf_url",
    "previous_file_number",
    "primary_general_indicator",
    "receipt_date",
    "report_type",
    "report_type_full",
    "report_year",
    "request_type",
    "senate_personal_funds",
    "state",
    "sub_id",
    "total_communication_cost",
    "total_disbursements",
    "total_independent_expenditures",
    "total_individual_contributions",
    "total_receipts",
    "treasurer_name",
    "update_date",
}


@pytest.mark.parametrize("stem", [FECFILING_ORIGINAL_STEM, FECFILING_AMENDED_STEM])
def test_captured_fecfiling_metadata_matches_the_observed_live_key_set(stem):
    payload = load_fixture(stem)
    assert set(payload) == {"api_version", "pagination", "results"}
    assert len(payload["results"]) == 1
    assert set(payload["results"][0]) == LIVE_FECFILING_RESULT_KEYS


def test_fecfiling_spec_pins_the_fec_file_of_the_original():
    fetch = _json_fetch(FECFILING_ORIGINAL)
    spec = fecfiling.spec(file_number=1903438, document="fec", fetch=fetch, env=FEC_KEY)
    assert fetch.calls[0][0] == (
        "https://api.open.fec.gov/v1/filings/?file_number=1903438&api_key=SECRET"
    )
    assert spec.fetcher == "fecfiling"
    assert spec.canonical_url == "https://docquery.fec.gov/dcdev/posted/1903438.fec"
    assert spec.fetch_url is None
    assert spec.title == "Form 3 electronic filing (.fec)"
    # the name as the metadata gives it; re-casing is the caller's --citation
    assert spec.citation == "OSBORN FOR SENATE, Form 3 Q2 2025-06-30, FEC file 1903438"
    assert spec.published_at == date(2025, 7, 15)  # receipt_date
    assert spec.point_in_time is None
    assert spec.publisher == "Federal Election Commission"
    assert spec.grade.code() == "A1"
    assert spec.drift_key == "sha256"
    assert spec.fetcher_verified is False
    assert spec.verified_at is None


def test_fecfiling_spec_pins_the_image_pdf_of_an_amendment_under_a_given_citation():
    spec = fecfiling.spec(
        file_number=1997103,
        document="pdf",
        citation="Osborn For Senate, Form 3 Q1 2026-03-31, FEC file 1997103",
        fetch=_json_fetch(FECFILING_AMENDED),
        env=FEC_KEY,
    )
    assert spec.canonical_url == (
        "https://docquery.fec.gov/pdf/521/202607159885311521/202607159885311521.pdf"
    )
    assert spec.title == "Form 3 image (PDF)"
    assert spec.citation == "Osborn For Senate, Form 3 Q1 2026-03-31, FEC file 1997103"
    assert spec.published_at == date(2026, 7, 15)


def test_fecfiling_refuses_a_result_for_another_file_number():
    # The filter could be ignored, as openfec's `mur_no` was: insist on the filing asked for.
    with pytest.raises(ValueError, match="0 results for file number 1903438"):
        fecfiling.spec(
            file_number=1903438, document="fec", fetch=_json_fetch(FECFILING_AMENDED), env=FEC_KEY
        )


@pytest.mark.parametrize(
    "pdf_url",
    [
        "https://docquery.fec.gov/pdf/x.pdf?api_key=LEAKED",
        "https://example.com/pdf/x.pdf",
        "http://docquery.fec.gov/pdf/x.pdf",
        None,
    ],
)
def test_fecfiling_stores_only_a_bare_docquery_url(pdf_url):
    # A mutated copy of the capture, one field changed.
    payload = json.loads(json.dumps(FECFILING_AMENDED))
    payload["results"][0]["pdf_url"] = pdf_url
    with pytest.raises(ValueError, match="docquery URL|lists no pdf_url"):
        fecfiling.spec(file_number=1997103, document="pdf", fetch=_json_fetch(payload), env=FEC_KEY)


def test_fecfiling_refuses_an_unknown_document_kind():
    with pytest.raises(ValueError, match="document must be one of fec, pdf"):
        fecfiling.spec(
            file_number=1997103, document="csv", fetch=_json_fetch(FECFILING_AMENDED), env=FEC_KEY
        )


def test_fecfiling_without_a_key_raises_missing_key():
    with pytest.raises(MissingKey, match="OPENFEC_API_KEY"):
        fecfiling.spec(
            file_number=1997103, document="fec", fetch=_json_fetch(FECFILING_AMENDED), env={}
        )


def test_fecfiling_check_needs_no_key():
    # docquery served both documents keyless on 2026-09-23, so `check` re-fetches the stored URL
    # as it is, with no key demanded of the environment.
    assert not hasattr(fecfiling, "content_request")


def test_fecfiling_cli_adapter():
    parser = argparse.ArgumentParser()
    fecfiling.add_arguments(parser)
    args = parser.parse_args(["--file-number", "1997103", "--document", "fec"])
    spec = fecfiling.spec_from_args(args, fetch=_json_fetch(FECFILING_AMENDED), env=FEC_KEY)
    assert spec.canonical_url == "https://docquery.fec.gov/dcdev/posted/1997103.fec"
    with pytest.raises(SystemExit):
        parser.parse_args(["--file-number", "1997103", "--document", "csv"])


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
        federalregister.spec(
            document_number="95-3162", fetch=_json_fetch(load_fixture(FR_DOCUMENT_STEM))
        ),
        govinfo.spec(
            package="X", fetch=_json_fetch(GOVINFO_SUMMARY), env={"GOVINFO_API_KEY": "SECRET"}
        ),
        uscode.spec(
            title=52, section="30116", fetch=lambda u, h=None: (load_page(USCODE_STEM), "text/html")
        ),
        openfec.spec(
            number="2023-01", fetch=_json_fetch(OPENFEC_SEARCH), env={"OPENFEC_API_KEY": "SECRET"}
        ),
        courtlistener.spec(
            cluster_id=1,
            fetch=_cl_fetch(),
            env={"COURTLISTENER_TOKEN": "SECRET"},
        ),
        fecfiling.spec(
            file_number=1997103, document="fec", fetch=_json_fetch(FECFILING_AMENDED), env=FEC_KEY
        ),
        fecfiling.spec(
            file_number=1997103, document="pdf", fetch=_json_fetch(FECFILING_AMENDED), env=FEC_KEY
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


def test_courtlistener_stamps_its_live_run_onto_every_record():
    # Exercised live 2026-09-20 on cluster 1481640, whose four captured responses these tests read.
    # Editing spec() voids the run and resets this to False — the convention in docs/operations.md.
    assert courtlistener.VERIFIED is True
    assert courtlistener.VERIFIED_AT == date(2026, 9, 20)
    spec = _cl_spec(_cl_fetch())
    assert spec.fetcher_verified is True
    assert spec.verified_at == date(2026, 9, 20)


@pytest.mark.parametrize(
    ("module", "verified_on"),
    [
        # ecfr: a live `add` through --section/--subpart/--as-of latest on 2026-09-18 (UTC).
        (ecfr, date(2026, 9, 18)),
        # federalregister: a scratch `add` on FR Doc. 95-3162, 2026-09-18 (UTC), whose captured
        # response the tests above now read.
        (federalregister, date(2026, 9, 18)),
        # manual: no field mapping exists to be wrong, so nothing to exercise.
        (manual, date(2026, 9, 17)),
        # uscode: a scratch `add --title 52 --section 30116 --archive` on 2026-09-19 (UTC), the
        # first capture this tree took through the keyed SPN2 path.
        (uscode, date(2026, 9, 19)),
        # courtlistener: a scratch `add --cluster-id 1481640 --archive` on 2026-09-20 (UTC), the
        # first run with a token in this tree. It corrected two things — see the module docstring.
        (courtlistener, date(2026, 9, 20)),
        # openfec: a scratch `add --number 8098 --type murs --document 100512215` on 2026-09-21
        # (UTC). It corrected the MUR number parameter, which was ignored rather than rejected.
        (openfec, date(2026, 9, 21)),
        # govinfo: a scratch `add --package USCODE-2024-title52 --granule ...-sec30116` on
        # 2026-09-22 (UTC). It moved the stored URL to the key-free content host.
        (govinfo, date(2026, 9, 22)),
    ],
)
def test_exercised_fetchers_ship_verified(module, verified_on):
    assert module.VERIFIED is True
    assert module.VERIFIED_AT == verified_on


def test_unexercised_fetchers_ship_unverified(unverified_fetcher):
    # A claim recorded in a handoff or a docstring is not a verification. Real fetchers flip one at
    # a time, each on its own live `add` in this tree, dated the day it ran; every one has now, so
    # the unverified shape is held by a fixture rather than by whichever fetcher is left.
    from registers_crosswalk.fetchers import NAMES, get

    unverified = [n for n in NAMES if not get(n).VERIFIED]
    # fecfiling until its first live run flips it
    assert unverified == ["fecfiling", unverified_fetcher.NAME]
    assert unverified_fetcher.VERIFIED is False
    assert unverified_fetcher.VERIFIED_AT is None


def test_the_stamp_reaches_the_minted_record(unverified_fetcher):
    from registers_crosswalk.pin import pin

    unverified = pin(
        unverified_fetcher.spec(url="https://example.gov/unverified.pdf"),
        next_id="xr_src_0001",
        fetch=lambda u, h=None: (b"%PDF fake", "application/pdf"),
    )
    assert unverified.fetcher_verified is False
    assert unverified.verified_at is None
    assert unverified.model_dump()["fetcher_verified"] is False  # and it serializes

    # ecfr is unverified again after its spec() changed, so `manual` is the verified example here.
    verified = pin(
        manual.spec(url="https://example.gov/x.pdf", citation="X", title="X"),
        next_id="xr_src_0002",
        fetch=lambda u, h=None: (b"%PDF fake", "application/pdf"),
    )
    assert verified.fetcher_verified is True
    assert verified.verified_at == date(2026, 9, 17)


def test_ecfr_stamps_its_live_run_onto_every_record():
    # The convention in docs/operations.md: editing spec() voids the run and resets this to False.
    # It is True again because the current code path was exercised live on 2026-09-18 (UTC).
    from registers_crosswalk.pin import pin

    source = pin(
        ecfr.spec(title=5, section="2640.202", as_of=date(2026, 9, 15)),
        next_id="xr_src_0003",
        fetch=lambda u, h=None: (b"<ECFR/>", "application/xml"),
    )
    assert source.fetcher_verified is True
    assert source.verified_at == date(2026, 9, 18)


# ------------------------------------------- ecfr granularity: part / section / subpart


def test_ecfr_part_url_and_citation():
    spec = ecfr.spec(title=5, part="2640", as_of=date(2026, 9, 15))
    assert spec.canonical_url == (
        "https://www.ecfr.gov/api/versioner/v1/full/2026-09-15/title-5.xml?part=2640"
    )
    assert spec.citation == "5 CFR Part 2640"
    assert spec.title == "5 CFR Part 2640, as of 2026-09-15"


def test_ecfr_section_derives_its_part_and_cites_as_the_document_does():
    spec = ecfr.spec(title=5, section="2640.202", as_of=date(2026, 9, 15))
    assert spec.canonical_url == (
        "https://www.ecfr.gov/api/versioner/v1/full/2026-09-15/title-5.xml"
        "?part=2640&section=2640.202"
    )
    assert spec.citation == "5 CFR 2640.202"  # not "5 CFR Part 2640"
    assert spec.point_in_time == date(2026, 9, 15)


def test_ecfr_subpart_url_and_citation():
    spec = ecfr.spec(title=5, subpart="2634/D", as_of=date(2026, 9, 15))
    assert spec.canonical_url == (
        "https://www.ecfr.gov/api/versioner/v1/full/2026-09-15/title-5.xml?part=2634&subpart=D"
    )
    assert spec.citation == "5 CFR 2634 subpart D"


def test_the_three_granularities_are_three_different_documents():
    # The point of the feature: each selector names its own document, so each gets its own pin.
    urls = {
        ecfr.spec(title=5, part="2640", as_of=date(2026, 9, 15)).canonical_url,
        ecfr.spec(title=5, section="2640.202", as_of=date(2026, 9, 15)).canonical_url,
        ecfr.spec(title=5, subpart="2634/D", as_of=date(2026, 9, 15)).canonical_url,
    }
    assert len(urls) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"part": "2640", "section": "2640.202"},
        {"part": "2640", "subpart": "2634/D"},
        {"section": "2640.202", "subpart": "2634/D"},
    ],
)
def test_ecfr_selectors_are_mutually_exclusive(kwargs):
    with pytest.raises(ValueError, match="exactly one of part/section/subpart"):
        ecfr.spec(title=5, as_of=date(2026, 9, 15), **kwargs)


@pytest.mark.parametrize("bad", ["2640", "abc"])
def test_ecfr_section_must_carry_its_part(bad):
    with pytest.raises(ValueError, match="full number like 2640.202"):
        ecfr.spec(title=5, section=bad, as_of=date(2026, 9, 15))


@pytest.mark.parametrize("bad", ["2634", "/D", "2634/"])
def test_ecfr_subpart_must_be_part_slash_letter(bad):
    with pytest.raises(ValueError, match="PART/LETTER like 2634/D"):
        ecfr.spec(title=5, subpart=bad, as_of=date(2026, 9, 15))


# ------------------------------------------------------- ecfr --as-of latest resolution


def _titles_fetch(payload=None):
    calls = []

    def fetch(url, headers=None):
        calls.append(url)
        body = load_fixture("ecfr_titles") if payload is None else payload
        return json.dumps(body).encode(), "application/json"

    fetch.calls = calls
    return fetch


def test_latest_issue_date_is_per_title():
    # Titles drift apart by months; a global "latest" would be wrong for all but one of them.
    fetch = _titles_fetch()
    assert ecfr.latest_issue_date(5, fetch=fetch) == date(2026, 9, 15)
    assert ecfr.latest_issue_date("11", fetch=fetch) == date(2026, 6, 8)
    assert fetch.calls == [ecfr.titles_url()] * 2


def test_latest_issue_date_unknown_title_raises():
    # 99 is not a CFR title; 42 IS one, and the captured index carries all 50.
    with pytest.raises(ValueError, match="title 99 not found"):
        ecfr.latest_issue_date(99, fetch=_titles_fetch())


def test_latest_issue_date_missing_field_raises():
    # Derived from the capture by blanking one field, rather than inventing a payload shape.
    payload = load_fixture("ecfr_titles")
    for entry in payload["titles"]:
        if entry["number"] == 5:
            entry["latest_issue_date"] = None
    with pytest.raises(ValueError, match="no latest_issue_date"):
        ecfr.latest_issue_date(5, fetch=_titles_fetch(payload))


def test_latest_never_reaches_the_url_or_the_record():
    # The whole point: "latest" is an input word. A stored URL or point_in_time carrying it would
    # mean something different every time the record was read.
    spec = ecfr.spec(title=5, section="2640.202", as_of="latest", fetch=_titles_fetch())
    assert "latest" not in spec.canonical_url
    assert spec.canonical_url == (
        "https://www.ecfr.gov/api/versioner/v1/full/2026-09-15/title-5.xml"
        "?part=2640&section=2640.202"
    )
    assert spec.point_in_time == date(2026, 9, 15)
    assert spec.title == "5 CFR 2640.202, as of 2026-09-15"

    source = _as_source(spec)
    assert "latest" not in source.model_dump_json()
    assert source.point_in_time == date(2026, 9, 15)


def test_latest_resolves_identically_to_passing_the_date():
    resolved = ecfr.spec(title=5, subpart="2634/D", as_of="latest", fetch=_titles_fetch())
    explicit = ecfr.spec(title=5, subpart="2634/D", as_of=date(2026, 9, 15))
    assert resolved.canonical_url == explicit.canonical_url
    assert resolved.point_in_time == explicit.point_in_time
    assert resolved.citation == explicit.citation


def test_a_concrete_date_makes_no_titles_call():
    fetch = _titles_fetch()
    ecfr.spec(title=5, part="2640", as_of=date(2026, 9, 15), fetch=fetch)
    assert fetch.calls == []  # nothing to resolve, so nothing is requested


def test_as_of_arg_accepts_latest_and_dates_and_rejects_junk():
    assert ecfr._as_of_arg("latest") == "latest"
    assert ecfr._as_of_arg("2026-09-15") == date(2026, 9, 15)
    with pytest.raises(argparse.ArgumentTypeError, match='expected YYYY-MM-DD or "latest"'):
        ecfr._as_of_arg("yesterday")


# ----------------------------------- amendment narrowing, against CAPTURED responses

# Observed on the live /versions endpoint 2026-09-18 (UTC day; see docs/operations.md). The
# earlier hand-written fixture carried a
# "section" key that the API has never returned, which hid a filter that matched nothing at all.
LIVE_VERSION_KEYS = {
    "amendment_date",
    "date",
    "identifier",
    "issue_date",
    "name",
    "part",
    "removed",
    "subpart",
    "substantive",
    "title",
    "type",
}


@pytest.mark.parametrize(
    "stem",
    ["ecfr_versions_title5_part2640", "ecfr_versions_title5_part2634"],
)
def test_captured_fixture_matches_the_observed_live_key_set(stem):
    # Guards both directions: an invented field fails, a dropped field fails. If eCFR really does
    # change its payload, this test is the place to find out, and the fixture must be RE-CAPTURED
    # rather than edited by hand.
    entries = load_fixture(stem)["content_versions"]
    assert entries
    for entry in entries:
        assert set(entry) == LIVE_VERSION_KEYS, entry.get("identifier")


def test_content_versions_has_no_section_key():
    # The bug this commit fixes, pinned so it cannot come back: matching on v["section"] silently
    # excluded every entry, so a section pin could never report AMENDED.
    for stem in ("ecfr_versions_title5_part2640", "ecfr_versions_title5_part2634"):
        for entry in load_fixture(stem)["content_versions"]:
            assert "section" not in entry


def test_section_is_matched_by_identifier_and_type():
    fetch = _versions_fetch("ecfr_versions_title5_part2640")
    # 2640.202 is a real section of the captured part, amended 2017-01-01
    assert ecfr.latest_amendment(5, "2640", section="2640.202", fetch=fetch) == date(2017, 1, 1)
    # a section that does not exist in the part matches nothing
    assert ecfr.latest_amendment(5, "2640", section="2640.999", fetch=fetch) is None
    # and the unnarrowed part query still answers
    assert ecfr.latest_amendment(5, "2640", fetch=fetch) == date(2017, 1, 1)


def test_section_matching_ignores_a_same_named_non_section_entry():
    # type is half the key: an appendix whose identifier happened to collide must not match.
    payload = load_fixture("ecfr_versions_title5_part2640")
    payload["content_versions"].append(
        {**payload["content_versions"][0], "type": "appendix", "amendment_date": "2030-01-01"}
    )

    def fetch(url, headers=None):
        return json.dumps(payload).encode(), "application/json"

    ident = payload["content_versions"][0]["identifier"]
    assert ecfr.latest_amendment(5, "2640", section=ident, fetch=fetch) == date(2017, 1, 1)


def test_subpart_is_matched_on_the_subpart_field():
    fetch = _versions_fetch("ecfr_versions_title5_part2634")
    # subpart D really exists in the capture (30 entries); latest amendment there is 2019-01-01
    assert ecfr.latest_amendment(5, "2634", subpart="D", fetch=fetch) == date(2019, 1, 1)
    assert ecfr.latest_amendment(5, "2634", subpart="Z", fetch=fetch) is None
    # the whole part reaches later amendments that subpart D does not
    assert ecfr.latest_amendment(5, "2634", fetch=fetch) == date(2026, 7, 23)


def test_removed_entries_count_as_amendments():
    # Part 2634's capture carries three real removed:true appendices, struck 2019-01-01. A pin
    # whose text no longer exists must report, not read as unchanged.
    payload = load_fixture("ecfr_versions_title5_part2634")
    removed = [v for v in payload["content_versions"] if v["removed"]]
    assert removed, "capture no longer carries a removed entry; re-capture and revisit"
    assert {v["type"] for v in removed} == {"appendix"}

    # narrowed to the part, the removed entries are inside the answer's range
    fetch = _versions_fetch("ecfr_versions_title5_part2634")
    assert ecfr.latest_amendment(5, "2634", fetch=fetch) >= date(2019, 1, 1)

    # and a removal is not filtered out: strike everything else and it still answers
    only_removed = {**payload, "content_versions": removed}

    def fetch_removed(url, headers=None):
        return json.dumps(only_removed).encode(), "application/json"

    assert ecfr.latest_amendment(5, "2634", fetch=fetch_removed) == date(2019, 1, 1)


def test_non_substantive_entries_are_still_ignored():
    payload = load_fixture("ecfr_versions_title5_part2640")
    for entry in payload["content_versions"]:
        entry["substantive"] = False

    def fetch(url, headers=None):
        return json.dumps(payload).encode(), "application/json"

    assert ecfr.latest_amendment(5, "2640", fetch=fetch) is None


def test_amended_since_reads_the_selector_back_out_of_the_url():
    # The record stores only a URL, so the narrowing has to survive a round trip through it.
    spec = ecfr.spec(title=5, section="2640.202", as_of=date(2016, 1, 1))
    source = _as_source(spec)
    fetch = _versions_fetch("ecfr_versions_title5_part2640")
    assert ecfr.amended_since(source, fetch=fetch) == date(2017, 1, 1)

    # pinned after the amendment, nothing to report
    later = _as_source(ecfr.spec(title=5, section="2640.202", as_of=date(2020, 1, 1)))
    assert ecfr.amended_since(later, fetch=fetch) is None


def test_a_removed_section_reports_under_both_narrowings():
    """A repealed section must report through the narrowed queries, not just the part-level one.

    The capture's own removed entries are all part-level appendices (`subpart: null`), so they
    exercise the unnarrowed query alone. This mutates a copy to make 2634.401 — a real section of
    subpart D — a removal, which is the case a section pin has to survive: a pin that cannot
    report its own repeal is the same false negative in a different coat.

    The mutation flips `removed` and moves `amendment_date`; it adds no keys, so the mutated copy
    still satisfies the live key set.
    """
    struck = date(2030, 1, 1)
    payload = json.loads(json.dumps(load_fixture("ecfr_versions_title5_part2634")))
    entry = next(
        v
        for v in payload["content_versions"]
        if v["type"] == "section" and v["identifier"] == "2634.401"
    )
    entry["removed"] = True
    entry["amendment_date"] = struck.isoformat()

    # the mutation stayed within the observed shape
    assert set(entry) == LIVE_VERSION_KEYS
    assert all(set(v) == LIVE_VERSION_KEYS for v in payload["content_versions"])
    assert entry["subpart"] == "D"  # the section really is inside the subpart being queried

    def fetch(url, headers=None):
        return json.dumps(payload).encode(), "application/json"

    # narrowed to the section itself
    assert ecfr.latest_amendment(5, "2634", section="2634.401", fetch=fetch) == struck
    # and narrowed to the subpart that contains it
    assert ecfr.latest_amendment(5, "2634", subpart="D", fetch=fetch) == struck
    # unmutated, neither query reaches that date
    clean = _versions_fetch("ecfr_versions_title5_part2634")
    assert ecfr.latest_amendment(5, "2634", section="2634.401", fetch=clean) == date(2019, 1, 1)
    assert ecfr.latest_amendment(5, "2634", subpart="D", fetch=clean) == date(2019, 1, 1)
