"""FEC filings — a committee's report as filed, the document behind a Schedule B line.

Verified live 2026-09-23, on the nine Form 3 filings of committee C00901355 (Osborn For Senate)
behind its Schedule B payments to Helix Campaigns:
  * `https://api.open.fec.gov/v1/filings/?file_number={n}&api_key=...` returns exactly one result
    for each of the nine file numbers queried. The result carries `committee_name`, `form_type`
    (`F3`), `report_type` (`Q2`, `YE`, `12P`, ...), `coverage_end_date`, `receipt_date`,
    `amendment_indicator`, `amendment_chain`, and the two document URLs, `fec_url`
    (`https://docquery.fec.gov/dcdev/posted/{n}.fec`) and `pdf_url` (the image PDF under
    `https://docquery.fec.gov/pdf/...`). The captures are `tests/fixtures/fecfiling_filings_*`.
  * docquery serves both documents with no key: the `.fec` as `binary/octet-stream`, the PDF as
    `application/pdf`. So the key is used for the metadata call only, and `check` needs no key.
  * The processed `/schedules/schedule_b/` rows are derived data. Their `amendment_indicator`
    disagreed with the filing's own on all five reports that itemized the payee, which is why
    they are used only to FIND a report and never cited: the pin is the report as filed.

The `.fec` file is the filing as the committee submitted it and the primary object. The PDF is
the FEC's rendering of it; a PDF pin that drifts while its `.fec` pin does not is a re-render, not
an amendment. An amendment is a NEW filing with its own file number, so it is a new pin that
`--supersedes` the previous one of the same document kind; nothing here ever overwrites.

Decision 1: the key NEVER enters `canonical_url`. Only the two docquery URLs the metadata names
are stored, and only when they are on docquery with no query string.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import date
from urllib.parse import urlsplit

from ..apikey import keyed_fetch
from ..models import Grade
from ..pin import FetchFn, MissingKey, PinSpec, default_fetch, sha256_hex

NAME = "fecfiling"
HELP = "a committee's FEC filing as filed: the .fec file or its image PDF (by file number)"
DRIFT_KEY = "sha256"
# A superseded filing (the original, or an earlier amendment) is still a fixed document at its own
# URL, so `pin check` keeps re-testing it. See `fetchers.check_superseded`.
CHECK_SUPERSEDED = True
# Exercised against the live API on 2026-09-23 (UTC) through the current code path: a scratch
# `add fecfiling --file-number 1903438 --document fec` into a data dir outside the repo, every
# mapped field compared against the captured metadata (tests/fixtures/fecfiling_filings_*), the
# hash against an independent keyless fetch of the same .fec, and a `check` with no key in the
# environment exited 0. Any further edit to spec() or its parsing resets this to False — see the
# convention in docs/operations.md — unless a test proves the requests byte-identical for every
# recorded verification input. The move onto `apikey.keyed_fetch` on 2026-09-25 kept it that way:
# tests/test_verified_requests.py.
VERIFIED = True
VERIFIED_AT = date(2026, 9, 23)
PUBLISHER = "Federal Election Commission"
ENV_KEY = "OPENFEC_API_KEY"
FILINGS = "https://api.open.fec.gov/v1/filings/"
DOCQUERY_HOST = "docquery.fec.gov"
DOCUMENTS = ("fec", "pdf")
_URL_FIELD = {"fec": "fec_url", "pdf": "pdf_url"}
_TITLE = {"fec": "Form {} electronic filing (.fec)", "pdf": "Form {} image (PDF)"}


def _key(env: Mapping[str, str]) -> str:
    key = env.get(ENV_KEY)
    if not key:
        raise MissingKey(ENV_KEY, NAME)
    return key


def filings_params(file_number: int) -> dict[str, int]:
    """The metadata query's parameters; `keyed_fetch` encodes them and adds the key."""
    return {"file_number": file_number}


def _the_filing(payload: Mapping, file_number: int) -> Mapping:
    # Insist on the file number asked for rather than trusting the filter: openfec's `mur_no`
    # was silently ignored and answered 200 with unrelated matters (see openfec.py).
    hits = [r for r in payload.get("results") or [] if r.get("file_number") == file_number]
    if len(hits) != 1:
        raise ValueError(f"FEC filings: {len(hits)} results for file number {file_number}")
    return hits[0]


def _document_url(filing: Mapping, document: str) -> str:
    url = filing.get(_URL_FIELD[document])
    if not url:
        raise ValueError(f"FEC filing {filing['file_number']} lists no {_URL_FIELD[document]}")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != DOCQUERY_HOST or parts.query:
        raise ValueError(f"FEC filing {filing['file_number']}: {url!r} is not a docquery URL")
    return url


def default_citation(filing: Mapping) -> str:
    """`{committee_name}, Form 3 {report_type} {coverage_end_date}, FEC file {file_number}`, from
    the metadata as given: the committee's name is not re-cased here."""
    form = str(filing["form_type"]).removeprefix("F")
    return (
        f"{filing['committee_name']}, Form {form} {filing['report_type']} "
        f"{str(filing['coverage_end_date'])[:10]}, FEC file {filing['file_number']}"
    )


def spec(
    *,
    file_number: int,
    document: str,
    citation: str | None = None,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    if document not in DOCUMENTS:
        raise ValueError(f"document must be one of {', '.join(DOCUMENTS)}, not {document!r}")
    key = _key(os.environ if env is None else env)
    body, _ = keyed_fetch(fetch, FILINGS, filings_params(file_number), key=key, fetcher=NAME)
    filing = _the_filing(json.loads(body), file_number)
    form = str(filing["form_type"]).removeprefix("F")
    return PinSpec(
        fetcher=NAME,
        canonical_url=_document_url(filing, document),
        citation=citation or default_citation(filing),
        title=_TITLE[document].format(form),
        publisher=PUBLISHER,
        # The day the FEC received the filing: the date the document as filed came to exist.
        published_at=date.fromisoformat(str(filing["receipt_date"])[:10]),
        grade=Grade(reliability="A", credibility=1),
        drift_key=DRIFT_KEY,
        fetcher_verified=VERIFIED,
        verified_at=VERIFIED_AT,
        drift_value=sha256_hex,
    )


def drift_value(body: bytes) -> str:
    return sha256_hex(body)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file-number", type=int, required=True, help="e.g. 1997103")
    parser.add_argument(
        "--document",
        choices=DOCUMENTS,
        required=True,
        help="fec: the filing as submitted; pdf: the FEC's image of it",
    )
    parser.add_argument(
        "--citation",
        default=None,
        help="default: '<committee>, Form 3 <report> <coverage end>, FEC file <n>'",
    )


def spec_from_args(
    args: argparse.Namespace,
    *,
    fetch: FetchFn = default_fetch,
    env: Mapping[str, str] | None = None,
) -> PinSpec:
    return spec(
        file_number=args.file_number,
        document=args.document,
        citation=args.citation,
        fetch=fetch,
        env=env,
    )
