"""`pin search`: a name or number in, the identifier `add` needs out.

Offline. The courtlistener tests read responses captured live on 2026-09-20; openfec's search has
never been run (the ledger cites no MUR or AO), so the only thing asserted about it is what a
capture is not needed for — that the query URL is built right and the key never escapes it.
"""

import json
from pathlib import Path

import pytest

from registers_crosswalk import fetchers
from registers_crosswalk.fetchers import courtlistener, openfec
from registers_crosswalk.pin import MissingKey, SearchHit, main

FIXTURES = Path(__file__).parent / "fixtures"
CL_TOKEN = {"COURTLISTENER_TOKEN": "T"}

OPINIONS_STEM = "courtlistener_search_dunne-v-united-states"
DOCKETS_STEM = "courtlistener_search_phang-v-blanche-dockets"

# The query each capture came from, so a re-capture can reproduce it exactly.
OPINIONS_QUERY = "Dunne v. United States"
DOCKETS_QUERY = "Phang v. Blanche"

# Every top-level key the live v4 search endpoint returned on 2026-09-20, for both types.
LIVE_SEARCH_KEYS = {"count", "next", "previous", "results"}

# The ledger's case, and the thing this whole subcommand exists to produce.
DUNNE_CLUSTER_ID = "1481640"


def load_search(stem: str) -> dict:
    """Load a CAPTURED live search response, same discipline as test_fetchers.load_fixture."""
    matches = sorted(FIXTURES.glob(f"{stem}_*.json"))
    if not matches:
        raise AssertionError(f"no captured fixture for {stem!r} under {FIXTURES}")
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def _fetch(payload):
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        return json.dumps(payload).encode(), "application/json"

    fetch.calls = calls
    return fetch


def _hit(hits, identifier):
    found = [h for h in hits if h.identifier == identifier]
    assert len(found) == 1, f"{identifier} appears {len(found)} times"
    return found[0]


# --------------------------------------------------------------------------- the captures


@pytest.mark.parametrize("stem", [OPINIONS_STEM, DOCKETS_STEM])
def test_captured_search_matches_the_observed_live_key_set(stem):
    # Guards both directions: an invented field fails, a dropped field fails. If CourtListener
    # changes its payload, this is where it surfaces, and the fixture must be RE-CAPTURED.
    assert set(load_search(stem)) == LIVE_SEARCH_KEYS


# --------------------------------------------------------------------------- courtlistener


def test_opinion_search_builds_the_v4_url_and_sends_the_token():
    fetch = _fetch(load_search(OPINIONS_STEM))
    courtlistener.search(OPINIONS_QUERY, doc_type="opinions", fetch=fetch, env=CL_TOKEN)
    url, headers = fetch.calls[0]
    assert url == (
        "https://www.courtlistener.com/api/rest/v4/search/?q=Dunne+v.+United+States&type=o"
    )
    assert headers == {"Authorization": "Token T"}


def test_docket_search_asks_for_type_d():
    fetch = _fetch(load_search(DOCKETS_STEM))
    courtlistener.search(DOCKETS_QUERY, doc_type="dockets", fetch=fetch, env=CL_TOKEN)
    assert fetch.calls[0][0].endswith("?q=Phang+v.+Blanche&type=d")


def test_opinion_search_finds_the_ledgers_case():
    # The whole point: the ledger says "Dunne v. United States, 138 F.2d 137 (8th Cir. 1943)" and
    # what comes back is the cluster id `add` takes.
    hits = courtlistener.search(
        OPINIONS_QUERY, doc_type="opinions", fetch=_fetch(load_search(OPINIONS_STEM)), env=CL_TOKEN
    )
    hit = _hit(hits, DUNNE_CLUSTER_ID)
    assert hit.label == "Dunne v. United States"
    assert hit.court_or_office == "Court of Appeals for the Eighth Circuit"
    assert hit.date == "1943-09-20"
    assert hit.docket_or_number == "12195"
    assert hit.citation == "138 F.2d 137"
    assert hit.url == "https://www.courtlistener.com/opinion/1481640/dunne-v-united-states/"


def test_a_citation_list_collapses_to_the_reporter_citation():
    # v4 returns `citation` as a list; Dunne's carries the reporter cite and a LEXIS cite, and the
    # reporter one — the one the ledger writes — comes first.
    raw = next(
        r for r in load_search(OPINIONS_STEM)["results"] if str(r["cluster_id"]) == DUNNE_CLUSTER_ID
    )
    assert raw["citation"] == ["138 F.2d 137", "1943 U.S. App. LEXIS 2440"]
    assert courtlistener._first_citation(raw) == "138 F.2d 137"


def test_docket_search_reads_the_docket_url_key():
    # Docket results call it `docket_absolute_url`; opinion results call it `absolute_url`. Reading
    # only the latter left every docket hit's url empty — found on the live run, 2026-09-20.
    raw = load_search(DOCKETS_STEM)["results"][0]
    assert "absolute_url" not in raw
    assert raw["docket_absolute_url"]
    hits = courtlistener.search(
        DOCKETS_QUERY, doc_type="dockets", fetch=_fetch(load_search(DOCKETS_STEM)), env=CL_TOKEN
    )
    hit = _hit(hits, "73246595")
    assert hit.label == "PHANG v. BLANCHE"
    assert hit.docket_or_number == "1:26-cv-01417"
    assert hit.url == "https://www.courtlistener.com/docket/73246595/phang-v-blanche/"
    assert hit.citation is None  # a docket has no reporter citation


def test_search_without_a_token_raises_missing_key():
    with pytest.raises(MissingKey, match="COURTLISTENER_TOKEN"):
        courtlistener.search(OPINIONS_QUERY, doc_type="opinions", fetch=_fetch({}), env={})


def test_an_opinion_hit_offers_the_add_that_pins_it():
    hits = courtlistener.search(
        OPINIONS_QUERY, doc_type="opinions", fetch=_fetch(load_search(OPINIONS_STEM)), env=CL_TOKEN
    )
    assert courtlistener.add_command(_hit(hits, DUNNE_CLUSTER_ID), "opinions") == (
        "pin add courtlistener --cluster-id 1481640 --archive"
    )


def test_a_docket_hit_offers_no_add_because_its_id_is_not_a_cluster_id():
    hits = courtlistener.search(
        DOCKETS_QUERY, doc_type="dockets", fetch=_fetch(load_search(DOCKETS_STEM)), env=CL_TOKEN
    )
    assert courtlistener.add_command(hits[0], "dockets") is None


# --------------------------------------------------------------------------- openfec


def test_openfec_search_url_carries_the_key_and_the_hit_never_does():
    # openfec's search has never been run live, so nothing here claims to know its response shape.
    # What IS testable without a capture: the key goes in the query and comes out of nothing else.
    url = openfec.free_text_search_url("Osborn", "murs", "SECRET")
    assert url.startswith("https://api.open.fec.gov/v1/legal/search/?")
    assert "q=Osborn" in url and "type=murs" in url and "api_key=SECRET" in url
    record = {"no": "7700", "name": "X", "url": "/legal/matter-under-review/7700/"}
    hit = openfec._hit(record, "murs")
    assert "SECRET" not in (hit.url or "")
    assert hit.url == "https://www.fec.gov/legal/matter-under-review/7700/"
    assert hit.citation == "FEC MUR 7700"


def test_openfec_search_without_a_key_raises_missing_key():
    with pytest.raises(MissingKey, match="OPENFEC_API_KEY"):
        openfec.search("Osborn", doc_type="murs", fetch=_fetch({}), env={})


# --------------------------------------------------------------------------- dispatch


def test_searchable_is_derived_not_hand_listed():
    # Adding SEARCH_TYPES to a module is the only thing a new searchable fetcher has to do.
    assert set(fetchers.SEARCHABLE) == {
        n for n in fetchers.NAMES if hasattr(fetchers.get(n), "SEARCH_TYPES")
    }
    assert set(fetchers.SEARCHABLE) == {"courtlistener", "openfec"}
    assert fetchers.search_types("ecfr") == ()


def test_a_fetcher_without_search_says_so():
    with pytest.raises(ValueError, match="has no search"):
        fetchers.search("ecfr", "x", doc_type="opinions", fetch=_fetch({}), env={})


# --------------------------------------------------------------------------- the CLI


def _run(argv, payload, monkeypatch, capsys):
    monkeypatch.setenv("COURTLISTENER_TOKEN", "T")
    code = main(argv, fetch=_fetch(payload))
    return code, capsys.readouterr()


def test_cli_prints_a_line_per_hit_and_ends_with_the_add(monkeypatch, capsys):
    code, out = _run(
        ["search", "courtlistener", OPINIONS_QUERY], load_search(OPINIONS_STEM), monkeypatch, capsys
    )
    assert code == 0
    lines = [line for line in out.out.splitlines() if line.strip()]
    assert len(lines) == len(load_search(OPINIONS_STEM)["results"]) + 1
    assert lines[0].split("  ")[0] == "5142005"
    assert any(DUNNE_CLUSTER_ID in line and "138 F.2d 137" in line for line in lines)
    # The round trip: the last line is the command, ready to paste.
    assert lines[-1] == "pin add courtlistener --cluster-id 5142005 --archive"


def test_cli_says_a_docket_hit_is_not_pinnable(monkeypatch, capsys):
    code, out = _run(
        ["search", "courtlistener", DOCKETS_QUERY, "--type", "dockets"],
        load_search(DOCKETS_STEM),
        monkeypatch,
        capsys,
    )
    assert code == 0
    assert out.out.splitlines()[-1] == "20 hit(s); a dockets hit is not pinnable directly"
    assert "pin add" not in out.out


def test_cli_json_round_trips_the_hits(monkeypatch, capsys):
    code, out = _run(
        ["search", "courtlistener", OPINIONS_QUERY, "--json"],
        load_search(OPINIONS_STEM),
        monkeypatch,
        capsys,
    )
    assert code == 0
    payload = json.loads(out.out[: out.out.rindex("]") + 1])
    assert [SearchHit(**row) for row in payload] == courtlistener.search(
        OPINIONS_QUERY, doc_type="opinions", fetch=_fetch(load_search(OPINIONS_STEM)), env=CL_TOKEN
    )


def test_cli_reports_no_hits_without_pretending_to_pin(monkeypatch, capsys):
    code, out = _run(
        ["search", "courtlistener", "no such case"], {"results": []}, monkeypatch, capsys
    )
    assert code == 1
    assert "no opinions matched" in out.err
    assert "pin add" not in out.out


def test_cli_search_never_touches_the_data_dir(tmp_path, monkeypatch, capsys):
    # search answers a question about the publisher's index, not about what this repo holds.
    code, _ = _run(
        ["--data-dir", str(tmp_path), "search", "courtlistener", OPINIONS_QUERY],
        load_search(OPINIONS_STEM),
        monkeypatch,
        capsys,
    )
    assert code == 0
    assert list(tmp_path.iterdir()) == []


def test_cli_missing_key_exits_two(monkeypatch, capsys):
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    code = main(["search", "courtlistener", OPINIONS_QUERY], fetch=_fetch({}))
    assert code == 2
    assert "COURTLISTENER_TOKEN" in capsys.readouterr().err
