import json

import pytest

from registers_crosswalk.models import ArchiveCopy
from registers_crosswalk.pin import main, sha256_hex

ARCHIVED = ArchiveCopy(service="wayback", url="https://web.archive.org/web/1/x")


def _ok_archive(url):
    return ARCHIVED


def _failing_archive(url):
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
    assert "- **Federal Election Commission, 2023-05-11 (B2)** — Final Opinion." in out
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


def test_check_defaults_to_the_latest_pin_per_citation(tmp_path, capsys):
    # Two pins of ONE citation: the default run checks the current one only, --all checks both.
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
    assert line == f"{sha256_hex(BODY)}  fec-advisory-opinion-2023-01.pdf"


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

    def counting_archive(url):
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
