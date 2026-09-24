"""The status page renders from `data/` and a drift report, and invents nothing.

Offline by construction: `status` has no fetch of its own, and every report here is hand-built.
"""

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from registers_crosswalk.pin import DriftReport, DriftStatus, to_ledger_markdown
from registers_crosswalk.registry import Crosswalk
from registers_crosswalk.status import _PILLS, main, next_check_after, render_html

GRADE = {"reliability": "A", "credibility": 1}
FETCHED = "2026-09-18T18:56:07Z"
WAYBACK = [
    {
        "service": "wayback",
        "url": "https://web.archive.org/web/20260919194240/https://uscode.house.gov/view.xhtml",
        "captured_at": "2026-09-19T19:42:40Z",
    }
]
ECFR_URL = "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-{title}.xml?part={part}"
# Every pin the fixture writes. Deliberately one per interesting shape: two eCFR pins (sha256 drift
# key, `as of` version line), a Federal Register PDF (published, no point_in_time), a uscode pin
# (last_amended, archived) and one more eCFR pin so the drift case reads "4 of 5".
SOURCES = [
    {
        "xr_id": "xr_src_0001",
        "citation": "11 C.F.R. Part 114",
        "title": "11 CFR Part 114, as of 2026-09-14",
        "canonical_url": ECFR_URL.format(date="2026-09-14", title="11", part="114"),
        "fetcher": "ecfr",
        "point_in_time": "2026-09-14",
    },
    {
        "xr_id": "xr_src_0002",
        "citation": "5 CFR 2640.202",
        "title": "5 CFR 2640.202, as of 2026-09-15",
        "canonical_url": ECFR_URL.format(date="2026-09-15", title="5", part="2640")
        + "&section=2640.202",
        "fetcher": "ecfr",
        "point_in_time": "2026-09-15",
    },
    {
        "xr_id": "xr_src_0003",
        "citation": "5 CFR 2634 subpart D",
        "title": "5 CFR 2634 subpart D, as of 2026-09-15",
        "canonical_url": ECFR_URL.format(date="2026-09-15", title="5", part="2634") + "&subpart=D",
        "fetcher": "ecfr",
        "point_in_time": "2026-09-15",
    },
    {
        "xr_id": "xr_src_0004",
        "citation": "60 FR 7862",
        "title": "Expenditures; Reports by Political Committees",
        "canonical_url": "https://www.govinfo.gov/content/pkg/FR-1995-02-09/pdf/95-3162.pdf",
        "fetcher": "federalregister",
        "published_at": "1995-02-09",
        "media_type": "application/pdf",
        "archives": WAYBACK,
    },
    {
        "xr_id": "xr_src_0005",
        "citation": "52 U.S.C. § 30116",
        "title": "52 U.S.C. § 30116 (prelim, laws in effect on 2026-09-18)",
        "canonical_url": "https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title52"
        "-section30116&num=0&edition=prelim",
        "fetcher": "uscode",
        "point_in_time": "2026-09-18",
        "drift_key": "last_amended",
        "drift_value": "2014-12-16",
        "archives": WAYBACK,
    },
]
NOW = datetime(2026, 9, 19, 20, 11, tzinfo=UTC)


def _write_source(data: Path, spec: dict) -> None:
    spec = dict(spec)
    xr_id = spec.pop("xr_id")
    digest = spec.pop("sha256", None) or xr_id[-1] * 64
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
            "fetched_at": FETCHED,
            "drift_key": spec.pop("drift_key", "sha256"),
            "drift_value": spec.pop("drift_value", digest),
        },
        "grade": dict(GRADE),
        **spec,
    }
    (data / "sources").mkdir(parents=True, exist_ok=True)
    (data / "sources" / f"{xr_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_node(data: Path) -> None:
    node = {
        "xr_id": "xr_holder_0001",
        "kind": "holder",
        "canonical_name": "Donald J. Trump",
        "external_ids": [
            {
                "scheme": "cik",
                "value": "0001321655",
                "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?CIK=0001321655&a=b",
            }
        ],
        "registers": [
            {
                "register": "vi",
                "local_id": "vi_official_0001",
                "ref_type": "identity",
                "display_label": "covered official",
            },
            {
                "register": "sovereign",
                "local_id": "SC-007",
                "ref_type": "record_mention",
                "display_label": "record subject",
            },
        ],
    }
    (data / "holders").mkdir(parents=True, exist_ok=True)
    (data / "holders" / "xr_holder_0001.json").write_text(json.dumps(node), encoding="utf-8")


@pytest.fixture
def xw(tmp_path):
    for spec in SOURCES:
        _write_source(tmp_path, spec)
    _write_node(tmp_path)
    return Crosswalk(tmp_path)


def _report(source, status="ok", **over):
    return DriftReport(
        xr_id=source.xr_id,
        citation=source.citation,
        status=status,
        drift_key=source.artifact.drift_key,
        expected=source.artifact.drift_value,
        archived=bool(source.archives),
        **over,
    )


def _reports(xw, **statuses):
    return [_report(s, statuses.get(s.xr_id, "ok")) for s in xw.sources.values()]


def _page(xw, reports=None, commit="c04befc"):
    return render_html(xw, reports, commit=commit, generated_at=NOW)


def _row(page: str, xr_id: str) -> str:
    """The one <tr> for this pin. Matched on the copy button's `data-xr-id`, which is unique per
    row — a bare `xr_id in row` also hits the row that a pin SUPERSEDES."""
    marker = f'data-xr-id="{xr_id}"'
    rows = [r for r in re.findall(r"<tr\b.*?</tr>", page, flags=re.S) if marker in r]
    assert len(rows) == 1, f"{xr_id} appears in {len(rows)} rows"
    return rows[0]


def _cell(row: str, label: str) -> str:
    """One <td> of a row. Needed because the copy button's `data-ledger` carries the whole ledger
    entry — em dash, citation, URL and all — so a bare `in row` would match another column's text.
    """
    found = re.findall(rf'<td data-label="{label}">(.*?)</td>', row, flags=re.S)
    assert len(found) == 1, f"{label!r} appears {len(found)} times in the row"
    return found[0]


# --------------------------------------------------------------------------- the status band


def test_all_ok_band_and_every_row_pilled(xw):
    page = _page(xw, _reports(xw))
    assert "All pinned documents = 1:1" in page
    assert "pinned documents = 1:1" in page
    assert page.count('<span class="pill pill-ok">OK</span>') == len(xw.sources)
    assert "Checked Sat 19 Sep 2026, 20:11 UTC, from a GitHub runner." in page
    assert "Next check Mon 21 Sep, 12:00 UTC." in page


def test_one_drift_on_an_unarchived_pin(xw):
    page = _page(xw, _reports(xw, xr_src_0001="drift"))
    assert "4 of 5 pinned documents = 1:1" in page
    assert "All pinned documents = 1:1" not in page
    row = _row(page, "xr_src_0001")
    assert _cell(row, "check") == '<span class="pill pill-drift">DRIFT</span>'
    # The unrecoverable note is the point of this row: no archive means the digest is the only
    # surviving evidence that the old text existed.
    assert "none, drift unrecoverable" in _cell(row, "archive copy")
    assert "none yet" not in row
    # An archived pin that did not drift keeps its ordinary archive link.
    assert "Wayback, 2026-09-19" in _cell(_row(page, "xr_src_0005"), "archive copy")


def test_no_reports_says_not_checked_and_shows_no_pills(xw):
    page = _page(xw, None)
    assert "Not checked in this build" in page
    assert "Built locally, not checked." in page
    assert 'class="pill' not in page
    assert "from a GitHub runner" not in page


def test_next_check_is_the_monday_after(xw):
    # Monday 11:00 UTC is before that day's cron, so the next check is the same day.
    assert next_check_after(datetime(2026, 9, 21, 11, 0, tzinfo=UTC)) == datetime(
        2026, 9, 21, 12, 0, tzinfo=UTC
    )
    # Monday 12:00 exactly has already fired; the next one is a week out.
    assert next_check_after(datetime(2026, 9, 21, 12, 0, tzinfo=UTC)) == datetime(
        2026, 9, 28, 12, 0, tzinfo=UTC
    )


def test_every_drift_status_has_a_pill(xw):
    # A seventh status added to DriftStatus without a pill would KeyError the whole page, which is
    # the one way this module can take the site down over a change made elsewhere.
    assert set(DriftStatus.__args__) == set(_PILLS)
    for status in DriftStatus.__args__:
        source = xw.sources["xr_src_0001"]
        page = _page(xw, [_report(source, status)])
        assert _PILLS[status][0] in _cell(_row(page, "xr_src_0001"), "check")


# --------------------------------------------------------------------------- the version column


def test_uscode_row_reads_laws_in_effect_and_names_the_amending_law(xw):
    cell = _cell(_row(_page(xw), "xr_src_0005"), "version pinned")
    assert "laws in effect on 2026-09-18" in cell
    assert "unchanged unless a law amends it (last: 2014-12-16)" in cell


def test_ecfr_row_reads_as_of_and_federalregister_row_reads_published(xw):
    page = _page(xw)
    ecfr = _cell(_row(page, "xr_src_0001"), "version pinned")
    assert "as of 2026-09-14" in ecfr
    assert "unchanged if the bytes still hash the same" in ecfr
    fr = _row(page, "xr_src_0004")
    version = _cell(fr, "version pinned")
    assert "published 1995-02-09" in version
    assert "as of" not in version
    # The PDF says PDF, in both the link text and the drift line.
    assert "source PDF" in _cell(fr, "document")
    assert "unchanged if the PDF still hashes the same" in version


def test_a_title_that_only_restates_the_citation_is_not_shown(xw):
    page = _page(xw)
    # The eCFR fixture is the hard case on purpose: its citation is punctuated "11 C.F.R. Part 114"
    # and its title "11 CFR Part 114, as of 2026-09-14", so a raw prefix test would call them
    # different and print the citation twice in two spellings.
    ecfr = xw.sources["xr_src_0001"]
    cell = _cell(_row(page, "xr_src_0001"), "document")
    assert cell.count(html.escape(ecfr.citation, quote=True)) == 1
    assert html.escape(ecfr.title, quote=True) not in cell
    # uscode is the same shape, reached through § rather than through the dots.
    uscode = xw.sources["xr_src_0005"]
    assert html.escape(uscode.title, quote=True) not in _cell(_row(page, "xr_src_0005"), "document")
    # The Federal Register title is the document's own name and is the one that must survive.
    fr = xw.sources["xr_src_0004"]
    assert html.escape(fr.title, quote=True) in _cell(_row(page, "xr_src_0004"), "document")


def test_notes_print_ahead_of_a_title_that_adds_something(tmp_path):
    # A MUR pin carries both: `notes` labels which document of the proceeding this is, and the
    # title is the document's own name. Printing the notes INSTEAD of the title cost the row its
    # name; printing the title alone cost it the label. It gets both, notes first.
    _write_source(
        tmp_path,
        {
            "xr_id": "xr_src_0001",
            "citation": "FEC MUR 8098",
            # Disjoint from the notes on purpose: a title the notes happened to quote would let
            # both assertions below pass against the old `elif`, which printed no title at all.
            "title": "Cory Mills; Cory Mills for Congress and David Satterfield",
            "notes": "MUR 8098, Cory Mills (FL-07): certification of the 6-0 vote to dismiss",
            "canonical_url": "https://www.fec.gov/files/legal/murs/8098/8098_12.pdf",
            "fetcher": "openfec",
            "media_type": "application/pdf",
            "published_at": "2024-07-23",
        },
    )
    source = Crosswalk(tmp_path).sources["xr_src_0001"]
    page = render_html(Crosswalk(tmp_path), None, commit="c04befc", generated_at=NOW)
    cell = _cell(_row(page, "xr_src_0001"), "document")

    notes = html.escape(source.notes, quote=True)
    title = html.escape(source.title, quote=True)
    assert notes not in title and title not in notes, "the fixture stopped testing anything"
    assert notes in cell and title in cell
    assert cell.index(notes) < cell.index(title)


def test_notes_survive_a_title_that_only_restates_the_citation(tmp_path):
    # The other half: the title rule still suppresses a title built out of the citation, and it
    # does not take the label down with it.
    _write_source(
        tmp_path,
        {
            "xr_id": "xr_src_0001",
            "citation": "11 C.F.R. Part 114",
            "title": "11 CFR Part 114, as of 2026-09-14",
            "notes": "the corporate-contribution part, as cited by the AO",
            "canonical_url": ECFR_URL.format(date="2026-09-14", title="11", part="114"),
            "fetcher": "ecfr",
            "point_in_time": "2026-09-14",
        },
    )
    source = Crosswalk(tmp_path).sources["xr_src_0001"]
    page = render_html(Crosswalk(tmp_path), None, commit="c04befc", generated_at=NOW)
    cell = _cell(_row(page, "xr_src_0001"), "document")
    assert html.escape(source.notes, quote=True) in cell
    assert html.escape(source.title, quote=True) not in cell


# --------------------------------------------------------------------------- which pins show


def test_merged_loser_is_absent_and_a_superseded_pin_names_its_successor(tmp_path):
    for spec in SOURCES[:2]:
        _write_source(tmp_path, spec)
    _write_node(tmp_path)
    # A later pin of the same citation, superseding the first.
    _write_source(
        tmp_path,
        {
            **SOURCES[0],
            "xr_id": "xr_src_0006",
            "title": "11 CFR Part 114, as of 2026-09-18",
            "canonical_url": ECFR_URL.format(date="2026-09-18", title="11", part="114"),
            "point_in_time": "2026-09-18",
            "sha256": "6" * 64,
            "supersedes": "xr_src_0001",
        },
    )
    # A duplicate that was simply wrong.
    _write_source(
        tmp_path,
        {
            **SOURCES[1],
            "xr_id": "xr_src_0007",
            "canonical_url": ECFR_URL.format(date="2026-09-15", title="5", part="2640")
            + "&section=2640.202&dup=1",
            "sha256": "7" * 64,
            "merged_into": "xr_src_0002",
        },
    )
    page = _page(Crosswalk(tmp_path))
    assert "xr_src_0007" not in page
    assert "3 documents pinned" in page
    row = _row(page, "xr_src_0001")
    assert "superseded by xr_src_0006" in _cell(row, "document")
    # No verdict at all on a superseded row: it makes no claim about the current world. That is a
    # different cell from a live pin this run did not check, which gets an em dash.
    assert _cell(row, "check") == ""
    assert _cell(_row(page, "xr_src_0006"), "check") == '<span class="none">—</span>'


def test_a_superseded_pin_the_run_checked_shows_its_verdict(tmp_path):
    # fecfiling's superseded pins are in `check`'s default targets, so their reports reach the
    # band's count; the row has to show the same verdict. One the run did not check stays blank.
    _write_source(tmp_path, SOURCES[0])
    _write_node(tmp_path)
    _write_source(
        tmp_path,
        {
            **SOURCES[0],
            "xr_id": "xr_src_0006",
            "canonical_url": ECFR_URL.format(date="2026-09-18", title="11", part="114"),
            "point_in_time": "2026-09-18",
            "sha256": "6" * 64,
            "supersedes": "xr_src_0001",
        },
    )
    xw = Crosswalk(tmp_path)
    checked = _page(xw, _reports(xw, xr_src_0001="drift"))
    assert (
        _cell(_row(checked, "xr_src_0001"), "check") == '<span class="pill pill-drift">DRIFT</span>'
    )
    unchecked = _page(xw, [r for r in _reports(xw) if r.xr_id != "xr_src_0001"])
    assert _cell(_row(unchecked, "xr_src_0001"), "check") == ""


# --------------------------------------------------------------------------- the copy button


def test_copy_payload_is_the_ledger_entry_verbatim(xw):
    page = _page(xw)
    for source in xw.sources.values():
        row = _row(page, source.xr_id)
        attr = re.search(r'data-ledger="([^"]*)"', row).group(1)
        assert html.unescape(attr) == to_ledger_markdown(source)


def test_copy_note_line_is_present(xw):
    assert (
        "Copy citation puts the source-links ledger entry on the clipboard, in the house format."
        in _page(xw)
    )


# --------------------------------------------------------------------------- escaping and secrets


def test_section_sign_and_ampersand_survive_escaping(xw):
    page = _page(xw)
    # The citation's § reaches the page intact under the declared charset.
    assert '<meta charset="utf-8">' in page
    assert "52 U.S.C. § 30116" in page
    # Every & in a stored URL is escaped. A raw one would end the entity-less attribute value at
    # the first following ';' in a real parser, and silently truncate the link.
    raw = SOURCES[2]["canonical_url"]
    assert raw not in page
    assert html.escape(raw, quote=True) in page
    assert "&subpart=D" not in page
    assert "&amp;subpart=D" in page


def test_the_page_carries_no_credential(xw):
    page = _page(xw)
    for needle in ("api_key=", "access_key=", "token=", "Authorization"):
        assert needle not in page


# --------------------------------------------------------------------------- the real data dir


def test_real_data_dir_renders():
    # Mirrors test_real_data_dir_loads: every pin in the shipped data dir reaches the page. Nothing
    # here pins how many there are, so `pin add` keeps working without a test edit.
    xw = Crosswalk(Path(__file__).resolve().parents[1] / "data")
    page = render_html(xw, None, commit=None, generated_at=NOW)
    assert xw.sources
    for xr_id in xw.sources:
        assert xr_id in page
    for node in xw.nodes.values():
        assert html.escape(node.canonical_name, quote=True) in page
    assert "at local." in page


# --------------------------------------------------------------------------- the CLI


def test_cli_writes_exactly_one_file_and_creates_its_parents(tmp_path, capsys):
    for spec in SOURCES:
        _write_source(tmp_path, spec)
    out = tmp_path / "build" / "status" / "index.html"
    code = main(["--data-dir", str(tmp_path), "--commit", "abc1234", "--out", str(out)])
    assert code == 0
    assert [p.name for p in (tmp_path / "build").rglob("*") if p.is_file()] == ["index.html"]
    assert "abc1234" in out.read_text(encoding="utf-8")
    assert str(out) in capsys.readouterr().out


def test_cli_reads_a_drift_json(tmp_path):
    for spec in SOURCES:
        _write_source(tmp_path, spec)
    xw = Crosswalk(tmp_path)
    drift = tmp_path / "drift.json"
    reports = _reports(xw, xr_src_0001="drift")
    drift.write_text(json.dumps([r.model_dump(mode="json") for r in reports]), encoding="utf-8")
    out = tmp_path / "out" / "index.html"
    assert main(["--data-dir", str(tmp_path), "--drift-json", str(drift), "--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "4 of 5 pinned documents = 1:1" in page
    assert "from a GitHub runner" in page
