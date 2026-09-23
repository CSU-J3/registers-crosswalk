import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from registers_crosswalk.models import ArchiveCopy
from registers_crosswalk.pin import ArchiveFailure, main, sha256_hex, to_manifest_line
from registers_crosswalk.registry import Crosswalk

DATA = Path(__file__).resolve().parents[1] / "data"

ARCHIVED = ArchiveCopy(service="wayback", url="https://web.archive.org/web/1/x")


def _ok_archive(url, **_):
    return ARCHIVED


def _failing_archive(url, **_):
    return None  # archive() never raises; it returns None on any failure


BODY = b"a document"
URL = "https://www.fec.gov/files/legal/aos/2023-01/2023-01.pdf"


def _fetch(body=BODY, media_type="application/pdf"):
    def fetch(url, headers=None):
        return body, media_type

    return fetch


def _add(
    tmp_path,
    url=URL,
    citation="FEC Advisory Opinion 2023-01",
    extra=(),
    fetch=None,
    archive_fn=_ok_archive,
):
    argv = [
        "--data-dir",
        str(tmp_path),
        "add",
        "manual",
        "--url",
        url,
        "--citation",
        citation,
        "--title",
        "Final Opinion",
        "--publisher",
        "Federal Election Commission",
        "--published-at",
        "2023-05-11",
        *extra,
    ]
    return main(argv, fetch=fetch or _fetch(), archive_fn=archive_fn)


def _sources(tmp_path):
    return sorted(p.name for p in (tmp_path / "sources").glob("*.json"))


def test_add_mints_sequential_ids(tmp_path, capsys):
    assert _add(tmp_path) == 0
    assert _add(tmp_path, url=URL.replace("2023-01", "2023-02"), citation="AO 2023-02") == 0
    assert _sources(tmp_path) == ["xr_src_0001.json", "xr_src_0002.json"]

    written = json.loads((tmp_path / "sources/xr_src_0001.json").read_text(encoding="utf-8"))
    assert written["xr_id"] == "xr_src_0001"
    assert written["kind"] == "source"
    assert written["canonical_url"] == URL
    assert written["artifact"]["sha256"] == sha256_hex(BODY)
    assert written["grade"] == {"reliability": "B", "credibility": 2}
    assert "xr_src_0001" in capsys.readouterr().out


def test_add_prints_the_ledger_entry(tmp_path, capsys):
    _add(tmp_path)
    out = capsys.readouterr().out
    # "Final Opinion" names no document on its own, so the citation is appended — the same
    # branch the Federal Register and courtlistener entries take.
    assert (
        "- **Federal Election Commission, 2023-05-11 (B2)** — "
        "Final Opinion, FEC Advisory Opinion 2023-01." in out
    )
    assert URL in out
    assert "Archive: pending" in out


def test_add_refuses_a_duplicate_url_at_the_same_point_in_time(tmp_path, capsys):
    assert _add(tmp_path) == 0
    assert _add(tmp_path) == 1
    assert "is already pinned as xr_src_0001" in capsys.readouterr().err
    assert _sources(tmp_path) == ["xr_src_0001.json"]


def test_add_records_citations_and_supersession(tmp_path):
    _add(tmp_path)
    assert (
        _add(
            tmp_path,
            url=URL.replace("2023-01", "2023-02"),
            citation="AO 2023-02",
            extra=[
                "--archive",
                "--cited-in",
                "vi:vi_conflict_0001",
                "--cited-in",
                "sovereign:SC-007",
                "--supersedes",
                "xr_src_0001",
            ],
        )
        == 0
    )
    written = json.loads((tmp_path / "sources/xr_src_0002.json").read_text(encoding="utf-8"))
    assert written["supersedes"] == "xr_src_0001"
    assert [c["local_id"] for c in written["cited_in"]] == ["vi_conflict_0001", "SC-007"]
    assert {c["ref_type"] for c in written["cited_in"]} == {"record_mention"}
    assert written["archives"][0]["url"] == ARCHIVED.url


def test_add_rejects_a_malformed_citation_ref(tmp_path):
    with pytest.raises(SystemExit):
        _add(tmp_path, extra=["--cited-in", "vi:not_a_conflict_id"])


def test_add_rejects_a_non_source_supersedes(tmp_path):
    with pytest.raises(SystemExit):
        _add(tmp_path, extra=["--supersedes", "xr_holder_0001"])


def test_add_refuses_a_blob_dir_inside_the_repo(tmp_path):
    with pytest.raises(ValueError, match="never stores document bytes"):
        _add(tmp_path, extra=["--blob-dir", "data"])


def test_check_exits_0_when_nothing_drifted(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch()) == 0
    assert "OK" in capsys.readouterr().out


def test_check_exits_1_on_drift(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch(body=b"changed")) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert sha256_hex(b"changed") in out


def _move_govinfo_pin_to_the_api_host(tmp_path, xr_id="xr_src_0001"):
    """govinfo stores its key-free content URL, which `check` re-fetches without a key. A URL on
    the API host is the one place a key is still needed, so these tests put the pin there to
    reach the KEY_MISSING path."""
    path = tmp_path / "sources" / f"{xr_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["canonical_url"] = "https://api.govinfo.gov/packages/P/granules/P/pdf"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def test_check_exits_2_when_a_key_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    argv = [
        "--data-dir",
        str(tmp_path),
        "add",
        "govinfo",
        "--package",
        "USCODE-2023-title52",
    ]
    monkeypatch.setenv("GOVINFO_API_KEY", "SECRET")
    summary = {
        "title": "United States Code, 2023 Edition, Title 52",
        "dateIssued": "2024-01-08",
        "download": {"pdfLink": "https://www.govinfo.gov/content/pkg/X/pdf/x.pdf"},
    }

    def fetch(url, headers=None):
        if "api.govinfo.gov" in url:
            return json.dumps(summary).encode(), "application/json"
        return BODY, "application/pdf"

    assert main(argv, fetch=fetch) == 0
    capsys.readouterr()
    _move_govinfo_pin_to_the_api_host(tmp_path)
    monkeypatch.delenv("GOVINFO_API_KEY")
    assert main(["--data-dir", str(tmp_path), "check"], fetch=fetch) == 2
    assert "KEY_MISSING" in capsys.readouterr().out


def test_check_only_one_pin(tmp_path, capsys):
    _add(tmp_path)
    _add(tmp_path, url=URL.replace("2023-01", "2023-02"), citation="AO 2023-02")
    capsys.readouterr()  # drop what `add` printed
    assert (
        main(["--data-dir", str(tmp_path), "check", "--only", "xr_src_0002"], fetch=_fetch()) == 0
    )
    out = capsys.readouterr().out
    assert "xr_src_0002" in out and "xr_src_0001" not in out


def test_check_unknown_id(tmp_path, capsys):
    assert (
        main(["--data-dir", str(tmp_path), "check", "--only", "xr_src_9999"], fetch=_fetch()) == 1
    )
    assert "unknown source" in capsys.readouterr().err


def test_check_defaults_to_every_live_pin_sharing_a_citation(tmp_path, capsys):
    # Three documents of ONE citation, the shape an FEC MUR has: a certification, a General
    # Counsel's report and a closing notification are not versions of each other, so none of them
    # may be dropped from a drift run in favour of whichever one sorts latest.
    for n, body in ((1, b"certification"), (2, b"gc report"), (3, b"notification")):
        assert (
            _add(
                tmp_path,
                url=URL.replace("2023-01.pdf", f"mur-8098-{n}.pdf"),
                citation="FEC MUR 8098",
                fetch=_fetch(body=body),
                extra=["--published-at", f"2024-0{n}-01"],
            )
            == 0
        )
    capsys.readouterr()  # drop what `add` printed

    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch(body=b"certification")) == 1
    default_run = capsys.readouterr().out
    assert all(x in default_run for x in ("xr_src_0001", "xr_src_0002", "xr_src_0003"))


def test_check_skips_a_superseded_pin_by_default_and_all_includes_it(tmp_path, capsys):
    _add(tmp_path, extra=["--point-in-time", "2023-05-11"])
    _add(
        tmp_path,
        url=URL.replace("2023-01", "2023-01-revised"),
        extra=["--point-in-time", "2024-05-11", "--supersedes", "xr_src_0001"],
    )
    capsys.readouterr()  # drop what `add` printed
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch()) == 0
    default_run = capsys.readouterr().out
    assert "xr_src_0002" in default_run and "xr_src_0001" not in default_run

    assert main(["--data-dir", str(tmp_path), "check", "--all"], fetch=_fetch()) == 0
    all_run = capsys.readouterr().out
    assert "xr_src_0001" in all_run and "xr_src_0002" in all_run


def test_check_json_is_machine_readable(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    main(["--data-dir", str(tmp_path), "check", "--json"], fetch=_fetch())
    reports = json.loads(capsys.readouterr().out)
    assert reports[0]["xr_id"] == "xr_src_0001"
    assert reports[0]["status"] == "ok"
    assert reports[0]["expected"] == sha256_hex(BODY)


def test_ledger_formats(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    main(["--data-dir", str(tmp_path), "ledger"], fetch=_fetch())
    assert "- **Federal Election Commission, 2023-05-11 (B2)**" in capsys.readouterr().out

    main(["--data-dir", str(tmp_path), "ledger", "--format", "manifest"], fetch=_fetch())
    line = capsys.readouterr().out.strip()
    assert line == f"{sha256_hex(BODY)}  xr_src_0001-fec-advisory-opinion-2023-01.pdf"


def test_every_manifest_filename_in_the_real_data_dir_is_unique():
    names = [to_manifest_line(s).split("  ", 1)[1] for s in Crosswalk(DATA).sources.values()]
    assert names and len(names) == len(set(names))


def test_two_pins_sharing_a_citation_get_two_manifest_filenames(tmp_path, capsys):
    # The six MUR pins' shape: one citation, several documents.
    _add(tmp_path, citation="FEC MUR 8098", fetch=_fetch(b"certification"))
    _add(tmp_path, url=URL + "?2", citation="FEC MUR 8098", fetch=_fetch(b"factual and legal"))
    capsys.readouterr()
    main(["--data-dir", str(tmp_path), "ledger", "--format", "manifest"], fetch=_fetch())
    names = [line.split("  ", 1)[1] for line in capsys.readouterr().out.splitlines()]
    assert names == ["xr_src_0001-fec-mur-8098.pdf", "xr_src_0002-fec-mur-8098.pdf"]


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs coreutils sha256sum")
def test_the_manifest_verifies_with_real_sha256sum(tmp_path, capsys):
    data, pins = tmp_path / "data", tmp_path / "pins"
    pins.mkdir()
    bodies = {b"one document": URL, b"another": URL + "?2", b"a third": URL + "?3"}
    for body, url in bodies.items():
        # two of the three share a citation, the case the id in the filename exists for
        citation = "FEC MUR 8098" if url != URL else "FEC Advisory Opinion 2023-01"
        assert _add(data, url=url, citation=citation, fetch=_fetch(body)) == 0
    capsys.readouterr()
    main(["--data-dir", str(data), "ledger", "--format", "manifest"], fetch=_fetch())
    manifest = capsys.readouterr().out
    by_hash = {sha256_hex(body): body for body in bodies}
    for line in manifest.splitlines():
        digest, name = line.split("  ", 1)
        (pins / name).write_bytes(by_hash[digest])
    (tmp_path / "SHA256SUMS").write_text(manifest, encoding="utf-8", newline="\n")

    def verify():
        return subprocess.run(
            ["sha256sum", "-c", "--strict", str(tmp_path / "SHA256SUMS")],
            cwd=pins,
            capture_output=True,
        ).returncode

    assert verify() == 0
    victim = next(pins.iterdir())
    victim.write_bytes(b"X" + victim.read_bytes()[1:])  # one byte changed
    assert verify() == 1


def test_ledger_since_filters_on_the_fetch_date(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    main(["--data-dir", str(tmp_path), "ledger", "--since", "2099-01-01"], fetch=_fetch())
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------- the four exit codes


def test_exit_3_when_the_transport_fails(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()

    def boom(url, headers=None):
        raise TimeoutError("read timed out")

    assert main(["--data-dir", str(tmp_path), "check"], fetch=boom) == 3
    assert "FETCH_FAILED" in capsys.readouterr().out


def test_transport_failure_does_not_read_as_drift(tmp_path, capsys):
    # The whole point of splitting 3 from 1: an unreachable endpoint must never be reported as a
    # changed document, because the two demand opposite responses.
    _add(tmp_path)
    capsys.readouterr()

    def boom(url, headers=None):
        raise ConnectionRefusedError("refused")

    assert main(["--data-dir", str(tmp_path), "check"], fetch=boom) == 3
    out = capsys.readouterr().out
    assert "DRIFT" not in out


def test_key_missing_outranks_a_transport_failure(tmp_path, monkeypatch, capsys):
    # Two pins, two different problems: fix the environment before chasing the network.
    monkeypatch.setenv("GOVINFO_API_KEY", "SECRET")
    summary = {
        "title": "t",
        "dateIssued": "2024-01-08",
        "download": {"pdfLink": "https://www.govinfo.gov/content/pkg/X/pdf/x.pdf"},
    }

    def setup_fetch(url, headers=None):
        if "api.govinfo.gov" in url:
            return json.dumps(summary).encode(), "application/json"
        return BODY, "application/pdf"

    assert (
        main(["--data-dir", str(tmp_path), "add", "govinfo", "--package", "P"], fetch=setup_fetch)
        == 0
    )
    _add(tmp_path)
    capsys.readouterr()
    _move_govinfo_pin_to_the_api_host(tmp_path)
    monkeypatch.delenv("GOVINFO_API_KEY")

    def boom(url, headers=None):
        raise TimeoutError("read timed out")

    assert main(["--data-dir", str(tmp_path), "check", "--all"], fetch=boom) == 2
    out = capsys.readouterr().out
    assert "KEY_MISSING" in out and "FETCH_FAILED" in out


def test_transport_failure_outranks_drift(tmp_path, capsys):
    _add(tmp_path)
    _add(tmp_path, url=URL.replace("2023-01", "2023-02"), citation="AO 2023-02")
    capsys.readouterr()

    def mixed(url, headers=None):
        if "2023-02" in url:
            raise TimeoutError("read timed out")
        return b"changed", "application/pdf"

    assert main(["--data-dir", str(tmp_path), "check", "--all"], fetch=mixed) == 3


def test_clean_run_still_exits_0(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch()) == 0


# ------------------------------------------------- the two drift rows (archived vs not)


def _archive_the_pin(tmp_path, xr_id="xr_src_0001"):
    p = tmp_path / "sources" / f"{xr_id}.json"
    record = json.loads(p.read_text(encoding="utf-8"))
    record["archives"] = [{"service": "wayback", "url": "https://web.archive.org/web/1/x"}]
    p.write_text(json.dumps(record), encoding="utf-8")


def test_drift_row_on_an_unarchived_pin_says_unrecoverable(tmp_path, capsys):
    _add(tmp_path)
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch(body=b"changed")) == 1
    out = capsys.readouterr().out
    assert "DRIFT unrecoverable (no archive)" in out


def test_drift_row_on_an_archived_pin_is_plain_drift(tmp_path, capsys):
    _add(tmp_path)
    _archive_the_pin(tmp_path)
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch(body=b"changed")) == 1
    out = capsys.readouterr().out
    assert "DRIFT " in out
    assert "unrecoverable" not in out


def test_the_archive_distinction_does_not_move_the_exit_code(tmp_path, capsys):
    # Same severity, different recoverability. Exit 1 either way.
    _add(tmp_path)
    capsys.readouterr()
    unarchived_code = main(["--data-dir", str(tmp_path), "check"], fetch=_fetch(body=b"changed"))
    _archive_the_pin(tmp_path)
    capsys.readouterr()
    archived_code = main(["--data-dir", str(tmp_path), "check"], fetch=_fetch(body=b"changed"))
    assert unarchived_code == archived_code == 1


# ------------------------------- add must never write a record the read path rejects


def test_add_refuses_cited_in_without_archive(tmp_path, capsys):
    assert _add(tmp_path, extra=["--cited-in", "vi:vi_conflict_0001"]) == 1
    assert "cited sources must carry an archive copy; pass --archive" in capsys.readouterr().err
    assert not (tmp_path / "sources").exists()


def test_add_refuses_cited_in_without_archive_before_fetching(tmp_path):
    # Refused up front: the fetch never happens, because no answer it could give would help.
    def boom(url, headers=None):
        raise AssertionError("must not fetch")

    assert _add(tmp_path, extra=["--cited-in", "vi:vi_conflict_0001"], fetch=boom) == 1


def test_add_refuses_a_uscode_pin_without_archive(tmp_path, capsys):
    # uscode sets REQUIRES_ARCHIVE: its canonical URL serves whatever is current, so once the
    # section moves only the archive copy can reproduce what was pinned.
    def boom(url, headers=None):
        raise AssertionError("must not fetch")

    code = main(
        ["--data-dir", str(tmp_path), "add", "uscode", "--title", "52", "--section", "30116"],
        fetch=boom,
        archive_fn=_ok_archive,
    )
    assert code == 1
    assert "uscode pins must carry an archive copy; pass --archive" in capsys.readouterr().err
    assert not (tmp_path / "sources").exists()


def test_add_writes_a_uscode_pin_when_archive_is_passed(tmp_path):
    page = (Path(__file__).parent / "fixtures").glob("uscode_page_title1_section1_*.html")
    body = sorted(page)[-1].read_bytes()
    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "add",
            "uscode",
            "--title",
            "52",
            "--section",
            "30116",
            "--archive",
        ],  # fmt: skip
        fetch=lambda u, h=None: (body, "text/html"),
        archive_fn=_ok_archive,
    )
    assert code == 0
    record = json.loads((tmp_path / "sources" / "xr_src_0001.json").read_text(encoding="utf-8"))
    assert record["artifact"]["drift_key"] == "last_amended"
    assert record["artifact"]["drift_value"] == "2012-12-28"
    assert record["archives"][0]["url"] == ARCHIVED.url


def test_cited_in_does_not_imply_archive(tmp_path, capsys):
    # The refusal is the point. Silently archiving on the caller's behalf would hide a step that
    # has its own failure mode.
    _add(tmp_path, extra=["--cited-in", "vi:vi_conflict_0001"], archive_fn=_ok_archive)
    assert not (tmp_path / "sources").exists()
    assert "pass --archive" in capsys.readouterr().err


def test_add_writes_nothing_when_the_archive_step_fails(tmp_path, capsys):
    assert _add(tmp_path, extra=["--archive"], archive_fn=_failing_archive) == 1
    err = capsys.readouterr().err
    assert "archive step failed" in err
    assert URL in err  # names what could not be archived
    assert not (tmp_path / "sources").exists()


def test_add_succeeds_when_citing_with_a_working_archive(tmp_path):
    assert _add(tmp_path, extra=["--archive", "--cited-in", "vi:vi_conflict_0001"]) == 0
    written = json.loads((tmp_path / "sources/xr_src_0001.json").read_text(encoding="utf-8"))
    assert written["cited_in"][0]["local_id"] == "vi_conflict_0001"
    assert written["archives"][0]["url"] == ARCHIVED.url


def test_duplicate_costs_no_fetch_and_no_archive(tmp_path, capsys):
    # The early duplicate_of() check earns its keep here: re-pinning a version we already hold
    # must not download the document again, and must not send a Save Page Now request to
    # archive.org for a URL that is already pinned.
    calls = {"fetch": 0, "archive": 0}

    def counting_fetch(url, headers=None):
        calls["fetch"] += 1
        return BODY, "application/pdf"

    def counting_archive(url, **_):
        calls["archive"] += 1
        return ARCHIVED

    assert (
        _add(tmp_path, extra=["--archive"], fetch=counting_fetch, archive_fn=counting_archive) == 0
    )
    assert calls == {"fetch": 1, "archive": 1}
    capsys.readouterr()

    assert (
        _add(tmp_path, extra=["--archive"], fetch=counting_fetch, archive_fn=counting_archive) == 1
    )
    assert calls == {"fetch": 1, "archive": 1}  # unchanged: nothing was requested
    assert "is already pinned as xr_src_0001" in capsys.readouterr().err
    assert _sources(tmp_path) == ["xr_src_0001.json"]


def test_everything_add_writes_can_be_read_back(tmp_path, capsys):
    # The property the pre-write check exists to guarantee, asserted end to end.
    _add(tmp_path, extra=["--archive", "--cited-in", "vi:vi_conflict_0001"])
    _add(tmp_path, url=URL.replace("2023-01", "2023-02"), citation="AO 2023-02")
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "ledger"], fetch=_fetch()) == 0
    assert main(["--data-dir", str(tmp_path), "check"], fetch=_fetch()) == 0


# -------------------------- the second duplicate rule: one live pin per (citation, drift value)

FIXTURES = Path(__file__).parent / "fixtures"
USCODE_PAGE = sorted(FIXTURES.glob("uscode_page_title1_section1_*.html"))[-1].read_bytes()


def _later_currency_date(page: bytes) -> bytes:
    """The capture re-served with OLRC's site-wide date advanced and the credit untouched.

    That is the whole footgun. "Laws in effect on" moves every few days for sections nobody
    amended, so the (canonical_url, point_in_time) rule sees a new version where there is none.
    """
    moved = page.replace(
        b"laws in effect on September 17, 2026", b"laws in effect on December 1, 2026", 1
    )
    assert moved != page
    return moved


def _amended_source_credit(page: bytes) -> bytes:
    """The capture re-served with a law spliced into the source credit: a real amendment.

    Same splice as tests/test_pin.py uses — the credit's text is broken up by <a> and
    <statuteAtLarge> tags, so the entry goes in just before the element closes.
    """
    start = page.index(b'class="source-credit"')
    end = page.index(b"</p>", start)
    return page[:end] + b"; Pub. L. 119-40, Mar. 4, 2026" + page[end:]


def _add_uscode(tmp_path, body, *, extra=(), archive_fn=_ok_archive, fetch=None):
    def serve(url, headers=None):
        return body, "text/html"

    argv = [
        "--data-dir",
        str(tmp_path),
        "add",
        "uscode",
        "--title",
        "1",
        "--section",
        "1",
        "--archive",
        *extra,
    ]
    return main(argv, fetch=fetch or serve, archive_fn=archive_fn)


def test_add_refuses_a_second_live_pin_of_an_unchanged_section(tmp_path, capsys):
    # The footgun end to end: pin the section, wait for OLRC to republish, run the same `add`
    # again. The URL is unchanged and the date moved, so the URL rule waves it through; the
    # section was never amended, so this is one document about to be pinned twice.
    assert _add_uscode(tmp_path, USCODE_PAGE) == 0
    capsys.readouterr()

    def must_not_archive(url, **_):
        raise AssertionError("archive was called")

    code = _add_uscode(tmp_path, _later_currency_date(USCODE_PAGE), archive_fn=must_not_archive)
    assert code == 1
    err = capsys.readouterr().err
    assert "1 U.S.C. § 1 with last_amended 2012-12-28 is already pinned as xr_src_0001" in err
    assert "pass --supersedes xr_src_0001 to chain a new pin anyway" in err
    assert _sources(tmp_path) == ["xr_src_0001.json"]


def test_supersedes_is_how_you_re_pin_an_unchanged_section_on_purpose(tmp_path):
    # The override falls out of the definition rather than being a flag: naming the pin makes it
    # not live, so the collision is gone. No --force, no --allow-duplicate.
    assert _add_uscode(tmp_path, USCODE_PAGE) == 0
    captures = []

    def counting_archive(url, **_):
        captures.append(url)
        return ARCHIVED

    code = _add_uscode(
        tmp_path,
        _later_currency_date(USCODE_PAGE),
        extra=["--supersedes", "xr_src_0001"],
        archive_fn=counting_archive,
    )
    assert code == 0
    written = json.loads((tmp_path / "sources/xr_src_0002.json").read_text(encoding="utf-8"))
    assert written["supersedes"] == "xr_src_0001"
    assert written["point_in_time"] == "2026-12-01"
    assert len(captures) == 1


def test_an_amended_section_needs_no_supersedes(tmp_path):
    # The credit gained a law, so the document really did change: a new version, not a duplicate.
    # The currency date moves with it, because OLRC republishing is what carried the amendment in.
    assert _add_uscode(tmp_path, USCODE_PAGE) == 0
    amended = _amended_source_credit(_later_currency_date(USCODE_PAGE))
    assert _add_uscode(tmp_path, amended) == 0
    written = json.loads((tmp_path / "sources/xr_src_0002.json").read_text(encoding="utf-8"))
    assert written["artifact"]["drift_value"] == "2026-03-04"
    assert written["supersedes"] is None


def test_the_url_rule_still_fires_first_and_before_the_hashing_fetch(tmp_path, capsys):
    # Both rules on the table at once: same URL, same date, same document. The URL rule is the
    # cheaper one and keeps its place — it answers before pin() fetches the bytes to hash, and
    # before anything is archived.
    calls = {"fetch": 0, "archive": 0}

    def counting_fetch(url, headers=None):
        calls["fetch"] += 1
        return USCODE_PAGE, "text/html"

    def counting_archive(url, **_):
        calls["archive"] += 1
        return ARCHIVED

    first = _add_uscode(tmp_path, USCODE_PAGE, fetch=counting_fetch, archive_fn=counting_archive)
    assert first == 0
    # spec() reads the currency date, then pin() fetches the bytes it hashes.
    assert calls == {"fetch": 2, "archive": 1}
    capsys.readouterr()

    code = _add_uscode(tmp_path, USCODE_PAGE, fetch=counting_fetch, archive_fn=counting_archive)
    assert code == 1
    assert "at point_in_time 2026-09-17 is already pinned as xr_src_0001" in capsys.readouterr().err
    # only spec()'s read: no second hashing fetch, no capture requested
    assert calls == {"fetch": 3, "archive": 1}
    assert _sources(tmp_path) == ["xr_src_0001.json"]


# ------------------------------------------------- `pin archive`: the other half of --archive


CAPTURE = ArchiveCopy(
    service="wayback",
    url="https://web.archive.org/web/20260920190851/x",
    captured_at=datetime(2026, 9, 20, 19, 8, 51, tzinfo=UTC),
)


def _record(tmp_path, xr_id="xr_src_0001"):
    return json.loads((tmp_path / "sources" / f"{xr_id}.json").read_text(encoding="utf-8"))


def _archive(tmp_path, xr_id="xr_src_0001", archive_fn=None):
    def capturing(url, **_):
        return CAPTURE

    return main(
        ["--data-dir", str(tmp_path), "archive", xr_id],
        fetch=_fetch(),
        archive_fn=archive_fn or capturing,
    )


def test_archive_adds_a_capture_to_a_pin_that_has_none(tmp_path, capsys):
    assert _add(tmp_path) == 0  # `add` with no --archive: a pin, unarchived
    capsys.readouterr()
    assert _record(tmp_path)["archives"] == []

    assert _archive(tmp_path) == 0
    out = capsys.readouterr().out
    assert "archived xr_src_0001: https://web.archive.org/web/20260920190851/x" in out
    # the ledger is reprinted with the archive on it, which is the point of doing this at all
    assert "Archive: https://web.archive.org/web/20260920190851/x" in out

    archives = _record(tmp_path)["archives"]
    assert archives == [
        {
            "service": "wayback",
            "url": "https://web.archive.org/web/20260920190851/x",
            "captured_at": "2026-09-20T19:08:51Z",
        }
    ]


def test_archive_is_handed_the_artifact_hash_so_a_matching_capture_is_reused(tmp_path, capsys):
    """Same reuse-or-capture path `add` uses: one implementation, not two."""
    assert _add(tmp_path) == 0
    capsys.readouterr()
    seen = {}

    def archive_fn(url, *, expected_sha256=None):
        seen["url"] = url
        seen["expected_sha256"] = expected_sha256
        return CAPTURE

    assert _archive(tmp_path, archive_fn=archive_fn) == 0
    assert seen["url"] == URL
    assert seen["expected_sha256"] == sha256_hex(BODY)


def test_archive_changes_only_the_archives_field(tmp_path, capsys):
    """An amendment to one fact, not a re-serialisation of the record.

    The record on disk is deliberately perturbed first — its keys are reordered, which is valid
    JSON and loads identically — because a re-serialisation would snap the order back to the
    model's and this test would otherwise pass whether or not the write was surgical. With the
    perturbation it can tell the two apart, which is the whole claim being made.
    """
    assert _add(tmp_path) == 0
    capsys.readouterr()
    path = tmp_path / "sources" / "xr_src_0001.json"

    original = json.loads(path.read_text(encoding="utf-8"))
    shuffled = {k: original[k] for k in reversed(list(original))}
    path.write_text(json.dumps(shuffled, indent=2) + "\n", encoding="utf-8")
    before = json.loads(path.read_text(encoding="utf-8"))
    assert list(before) != list(original)  # the perturbation took

    assert _archive(tmp_path) == 0
    after = json.loads(path.read_text(encoding="utf-8"))

    # same keys in the same (perturbed) order: nothing was re-serialised
    assert list(after) == list(before)
    assert after["archives"] != before["archives"]
    for key in before:
        if key != "archives":
            assert after[key] == before[key], key


def test_archive_refuses_a_pin_that_already_has_one(tmp_path, capsys):
    assert _add(tmp_path, extra=("--archive",)) == 0
    capsys.readouterr()
    before = _record(tmp_path)

    def must_not_archive(url, **_):
        raise AssertionError("archive was called for a pin that already has one")

    assert _archive(tmp_path, archive_fn=must_not_archive) == 1
    err = capsys.readouterr().err
    assert "xr_src_0001 already has an archive copy: https://web.archive.org/web/1/x" in err
    assert _record(tmp_path) == before  # and nothing was written


def test_archive_refuses_an_unknown_id(tmp_path, capsys):
    assert _add(tmp_path) == 0
    capsys.readouterr()
    assert _archive(tmp_path, xr_id="xr_src_0404") == 1
    assert "unknown source 'xr_src_0404'" in capsys.readouterr().err


def test_archive_reports_the_reason_when_the_capture_fails(tmp_path, capsys):
    assert _add(tmp_path) == 0
    capsys.readouterr()
    before = _record(tmp_path)

    def failing(url, **_):
        return ArchiveFailure("HTTP 429 from the Wayback Machine for " + url)

    assert _archive(tmp_path, archive_fn=failing) == 1
    err = capsys.readouterr().err
    assert err.strip() == ("archive step failed: HTTP 429 from the Wayback Machine for " + URL)
    assert _record(tmp_path) == before  # a failed capture writes nothing


def test_archive_keeps_the_old_wording_for_an_archiver_with_no_reason(tmp_path, capsys):
    assert _add(tmp_path) == 0
    capsys.readouterr()
    assert _archive(tmp_path, archive_fn=_failing_archive) == 1
    err = capsys.readouterr().err
    assert f"archive step failed: no capture returned for {URL}" in err


def test_archive_does_not_re_fetch_the_document(tmp_path, capsys):
    """It amends a record; it does not re-pin one. The bytes are not read again."""
    assert _add(tmp_path) == 0
    capsys.readouterr()

    def must_not_fetch(url, headers=None):
        raise AssertionError("the document was re-fetched")

    def capturing(url, **_):
        return CAPTURE

    assert (
        main(
            ["--data-dir", str(tmp_path), "archive", "xr_src_0001"],
            fetch=must_not_fetch,
            archive_fn=capturing,
        )
        == 0
    )


def test_an_archived_record_still_loads(tmp_path, capsys):
    """The file this writes has to be readable by everything downstream of it."""
    assert _add(tmp_path) == 0
    capsys.readouterr()
    assert _archive(tmp_path) == 0
    xw = Crosswalk(tmp_path)
    source = xw.sources["xr_src_0001"]
    assert source.archives[0].url == "https://web.archive.org/web/20260920190851/x"
    assert source.archives[0].captured_at == datetime(2026, 9, 20, 19, 8, 51, tzinfo=UTC)


# ------------------------------------------------- `pin note`: restate a label, offline


def _note(tmp_path, text, xr_id="xr_src_0001"):
    def must_not_fetch(url, headers=None):
        raise AssertionError("pin note fetched something")

    def must_not_archive(url, **_):
        raise AssertionError("pin note asked for a capture")

    return main(
        ["--data-dir", str(tmp_path), "note", xr_id, text],
        fetch=must_not_fetch,
        archive_fn=must_not_archive,
    )


def test_note_sets_the_notes_field_offline(tmp_path, capsys):
    assert _add(tmp_path) == 0
    capsys.readouterr()
    assert _note(tmp_path, "the advisory opinion itself") == 0
    assert "noted xr_src_0001: the advisory opinion itself" in capsys.readouterr().out
    assert _record(tmp_path)["notes"] == "the advisory opinion itself"
    assert Crosswalk(tmp_path).sources["xr_src_0001"].notes == "the advisory opinion itself"


def test_note_replaces_an_existing_note(tmp_path, capsys):
    assert _add(tmp_path, extra=("--notes", "old label")) == 0
    capsys.readouterr()
    assert _record(tmp_path)["notes"] == "old label"
    assert _note(tmp_path, "new label") == 0
    assert _record(tmp_path)["notes"] == "new label"


def test_note_changes_only_the_notes_field(tmp_path, capsys):
    """Same proof as `test_archive_changes_only_the_archives_field`: keys perturbed first, so a
    re-serialisation would snap them back and the test could tell."""
    assert _add(tmp_path, extra=("--notes", "old label")) == 0
    capsys.readouterr()
    path = tmp_path / "sources" / "xr_src_0001.json"

    original = json.loads(path.read_text(encoding="utf-8"))
    shuffled = {k: original[k] for k in reversed(list(original))}
    path.write_text(json.dumps(shuffled, indent=2) + "\n", encoding="utf-8")
    before = json.loads(path.read_text(encoding="utf-8"))
    assert list(before) != list(original)  # the perturbation took

    assert _note(tmp_path, "new label") == 0
    after = json.loads(path.read_text(encoding="utf-8"))

    # same keys in the same (perturbed) order: nothing was re-serialised
    assert list(after) == list(before)
    assert after["notes"] == "new label"
    for key in before:
        if key != "notes":
            assert after[key] == before[key], key


def test_note_leaves_non_ascii_as_written(tmp_path, capsys):
    """`add` writes `§` as itself; a one-key amendment must not re-escape it to `\\u00a7`, or the
    diff of a note carries a citation line that did not change."""
    assert _add(tmp_path) == 0
    capsys.readouterr()
    assert _note(tmp_path, "the companion to 52 U.S.C. § 30118") == 0
    text = (tmp_path / "sources" / "xr_src_0001.json").read_text(encoding="utf-8")
    assert "§ 30118" in text
    assert "\\u00a7" not in text


def test_note_refuses_an_unknown_id(tmp_path, capsys):
    assert _add(tmp_path) == 0
    capsys.readouterr()
    assert _note(tmp_path, "x", xr_id="xr_src_0404") == 1
    assert "unknown source 'xr_src_0404'" in capsys.readouterr().err


def test_note_refuses_an_empty_label(tmp_path, capsys):
    assert _add(tmp_path, extra=("--notes", "keep me")) == 0
    capsys.readouterr()
    before = _record(tmp_path)
    assert _note(tmp_path, "   ") == 1
    assert "a note must not be empty" in capsys.readouterr().err
    assert _record(tmp_path) == before  # nothing written
