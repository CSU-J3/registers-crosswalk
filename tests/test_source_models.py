import pytest
from pydantic import ValidationError

from registers_crosswalk.models import Artifact, CitationRef, Grade, Node, Source

ARTIFACT = {
    "sha256": "a" * 64,
    "byte_length": 1234,
    "media_type": "application/pdf",
    "fetched_at": "2026-09-17T12:00:00Z",
    "drift_key": "sha256",
    "drift_value": "a" * 64,
}
GRADE = {"reliability": "A", "credibility": 1}


def _source(**over) -> Source:
    base = dict(
        xr_id="xr_src_0001",
        kind="source",
        citation="11 C.F.R. Part 114",
        title="11 CFR Part 114, as of 2026-09-14",
        publisher="Office of the Federal Register",
        canonical_url="https://www.ecfr.gov/api/versioner/v1/full/2026-09-14/title-11.xml?part=114",
        fetcher="ecfr",
        point_in_time="2026-09-14",
        artifact=dict(ARTIFACT),
        grade=dict(GRADE),
    )
    base.update(over)
    return Source.model_validate(base)


def test_grade_code():
    assert Grade(reliability="A", credibility=1).code() == "A1"
    assert Grade(reliability="B", credibility=2).code() == "B2"


def test_unknown_grade_letter_rejected():
    with pytest.raises(ValidationError):
        Grade(reliability="Z", credibility=1)


def test_credibility_outside_1_to_6_rejected():
    with pytest.raises(ValidationError):
        Grade(reliability="A", credibility=7)


def test_valid_source_round_trips():
    source = _source()
    assert source.xr_id == "xr_src_0001"
    assert Source.model_validate(source.model_dump()).artifact.sha256 == "a" * 64


def test_kind_must_agree_with_id():
    with pytest.raises(ValidationError, match="disagrees with kind"):
        _source(xr_id="xr_holder_0001")


def test_node_cannot_take_a_source_id():
    # _XR_ID accepts xr_src_ now, but Node._check still requires xr_{kind}_, so widening the
    # pattern cannot leak a document id onto an actor node.
    with pytest.raises(ValidationError, match="disagrees with kind"):
        Node(
            xr_id="xr_src_0001",
            kind="holder",
            canonical_name="x",
            registers=[
                {
                    "register": "vi",
                    "local_id": "vi_official_0001",
                    "ref_type": "identity",
                    "display_label": "x",
                }
            ],
        )


@pytest.mark.parametrize("field", ["supersedes", "merged_into"])
def test_source_links_must_be_source_ids(field):
    with pytest.raises(ValidationError, match="is not an xr_src_NNNN id"):
        _source(**{field: "xr_holder_0001"})
    assert getattr(_source(**{field: "xr_src_0002"}), field) == "xr_src_0002"


def test_empty_citation_rejected():
    with pytest.raises(ValidationError, match="empty citation"):
        _source(citation="   ")


@pytest.mark.parametrize(
    "bad",
    [
        "A" * 64,  # uppercase
        "a" * 63,  # too short
        "a" * 65,  # too long
        "z" * 64,  # not hex
    ],
)
def test_sha256_must_be_64_lowercase_hex(bad):
    with pytest.raises(ValidationError, match="64 lowercase hex"):
        Artifact.model_validate({**ARTIFACT, "sha256": bad})


def test_naive_fetched_at_rejected():
    with pytest.raises(ValidationError, match="timezone-aware"):
        Artifact.model_validate({**ARTIFACT, "fetched_at": "2026-09-17T12:00:00"})


def test_aware_fetched_at_normalized_to_utc():
    artifact = Artifact.model_validate({**ARTIFACT, "fetched_at": "2026-09-17T08:00:00-04:00"})
    assert artifact.fetched_at.isoformat() == "2026-09-17T12:00:00+00:00"


def test_unknown_drift_key_rejected():
    with pytest.raises(ValidationError):
        Artifact.model_validate({**ARTIFACT, "drift_key": "etag"})


def test_currency_date_drift_key_rejected():
    # Retired 2026-09-18: the "laws in effect on" date it read is site-wide, not per-section, so it
    # reported drift on OLRC's publishing schedule. Pinned here so the disproven key cannot come
    # back quietly through a record or a fetcher.
    with pytest.raises(ValidationError):
        Artifact.model_validate({**ARTIFACT, "drift_key": "currency_date"})


def test_cited_in_uses_record_mention_patterns():
    source = _source(
        cited_in=[
            {"register": "vi", "local_id": "vi_conflict_0001", "ref_type": "record_mention"},
            {"register": "sovereign", "local_id": "SC-007", "ref_type": "record_mention"},
            {"register": "cp", "local_id": "cp_filing_0003", "ref_type": "record_mention"},
        ]
    )
    assert [c.local_id for c in source.cited_in] == ["vi_conflict_0001", "SC-007", "cp_filing_0003"]


def test_cited_in_rejects_a_sovereign_identity_ref():
    # ref_type is Literal["record_mention"], so the Literal itself rejects this — no
    # MENTION_ONLY_REGISTERS interaction needed.
    with pytest.raises(ValidationError):
        CitationRef(register="sovereign", local_id="PIF", ref_type="identity")


def test_cited_in_rejects_an_identity_namespace_local_id():
    # vi_official_0001 is an identity id; a citation can only come from a record.
    with pytest.raises(ValidationError, match="fails"):
        CitationRef(register="vi", local_id="vi_official_0001", ref_type="record_mention")


def test_url_carrying_an_api_key_rejected():
    with pytest.raises(ValidationError, match="carries a credential"):
        _source(canonical_url="https://api.govinfo.gov/packages/X/pdf?api_key=abc123")


@pytest.mark.parametrize(
    "param",
    [
        "api_key",
        "API_KEY",
        "apikey",
        "access_key",
        "access-key",
        "token",
        "auth_token",
        "key",
        "sig",
        "signature",
        "secret",
    ],
)
def test_every_credential_parameter_name_rejected(param):
    with pytest.raises(ValidationError, match="carries a credential"):
        _source(canonical_url=f"https://example.gov/doc.pdf?{param}=redacted")


def test_presigned_url_rejected():
    # An S3 presigned URL is a bearer credential with an expiry — committing one publishes it.
    with pytest.raises(ValidationError, match="carries a credential"):
        _source(
            canonical_url="https://b.s3.amazonaws.com/o.pdf"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123"
        )


def test_credential_after_another_parameter_rejected():
    with pytest.raises(ValidationError, match="carries a credential"):
        _source(canonical_url="https://example.gov/doc.pdf?page=1&api_key=redacted")


@pytest.mark.parametrize(
    "url",
    [
        # the real fetcher URLs must keep working
        "https://www.ecfr.gov/api/versioner/v1/full/2026-09-14/title-11.xml?part=114",
        "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title52-section30116"
        "&num=0&edition=prelim",
        "https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf",
        "https://www.fec.gov/files/legal/aos/2023-01/2023-01.pdf",
        # ...and the pattern must not trip on words that merely CONTAIN a credential word
        "https://example.gov/doc.pdf?monkey=1",
        "https://example.gov/doc.pdf?turkey=1",
        "https://example.gov/doc.pdf?design=1",
    ],
)
def test_clean_urls_still_pass(url):
    assert _source(canonical_url=url).canonical_url == url


def test_non_http_url_rejected():
    with pytest.raises(ValidationError, match="must be http"):
        _source(canonical_url="file:///etc/passwd")


def test_source_is_frozen_and_forbids_extras():
    source = _source()
    with pytest.raises(ValidationError):
        source.citation = "other"
    with pytest.raises(ValidationError):
        _source(body="the document text goes here")


# --------------------------------------------------------- fetcher verification (typed, not prose)


def test_fetcher_verified_defaults_to_false():
    # A new fetcher is untrusted until someone runs it: the default must not be the optimistic one.
    source = _source()
    assert source.fetcher_verified is False
    assert source.verified_at is None


def test_verified_source_carries_its_date():
    source = _source(fetcher_verified=True, verified_at="2026-09-17")
    assert source.fetcher_verified is True
    assert source.verified_at.isoformat() == "2026-09-17"


def test_verified_without_a_date_rejected():
    with pytest.raises(ValidationError, match="carries no verified_at"):
        _source(fetcher_verified=True)


def test_date_without_the_claim_rejected():
    with pytest.raises(ValidationError, match="fetcher_verified is False"):
        _source(verified_at="2026-09-17")
