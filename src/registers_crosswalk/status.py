"""Render a read-only status page from `data/` plus the JSON output of `pin check`.

One HTML file that answers "is every pinned document still what we pinned?" and lets a reader open
each source, its archive copy, and copy its source-links ledger entry. It reads; it never writes
anything but the one file it is asked to emit, and it never touches the network — the only thing
that fetches is `pin check`, whose JSON is handed in.

Every string on the page comes from `data/`, from a `DriftReport`, or from the approved copy fixed
in the constants below. Nothing here describes a document in its own words.

CLI: `python -m registers_crosswalk.status [--drift-json PATH] [--out PATH]`.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import fetchers
from .models import Node, Source
from .pin import DriftReport, to_ledger_markdown
from .registry import DATA_DIR, Crosswalk, superseded_ids, title_adds_anything

# --------------------------------------------------------------------------- fixed copy
#
# The approved wording, in one place, so a wording change is a diff here rather than a hunt through
# the markup. Working rule 6: no string on the page is invented outside this block.

EYEBROW = "registers-crosswalk"
TITLE = "Pinned sources"
BAND_ALL_OK = "All pinned documents = 1:1"
BAND_SOME_OK = "{ok_count} of {n} pinned documents = 1:1"
BAND_UNCHECKED = "Not checked in this build"
BAND_CHECKED_AT = "Checked {when} UTC, from a GitHub runner."
BAND_BUILT_LOCALLY = "Built locally, not checked."
BAND_NEXT_CHECK = "Next check Mon {when}, 12:00 UTC."
COLUMNS = ("check", "document", "version pinned", "archive copy", "")
COPY_BUTTON = "Copy citation"
COPY_NOTE = (
    "Copy citation puts the source-links ledger entry on the clipboard, in the house format."
)
ARCHIVE_NONE = "none yet"
ARCHIVE_UNRECOVERABLE = "none, drift unrecoverable"
SHA_LINE = "unchanged if the bytes still hash the same"
SHA_LINE_PDF = "unchanged if the PDF still hashes the same"
AMEND_LINE = "unchanged unless a law amends it (last: {drift_value})"
SUPERSEDED_BY = "superseded by {xr_id}"
COUNTS = "{n} documents pinned, {m} actors resolved"
ACTORS_HEADING = "actors resolved across registers"
FETCHERS_HEADING = "where pins can come from"
FETCHERS_VERIFIED = "verified {when}"
FETCHERS_UNVERIFIED = "unverified"
FETCHERS_NOTE = (
    "Green ones have been run live and verified. Grey ones need a first live run before they can "
    "pin anything."
)
FOOT_LEFT = "This page only reads. Pinning and checking happen in the terminal."
FOOT_RIGHT = (
    "A red row means the document changed since it was pinned. The archive column says whether "
    "the old text can still be read."
)
FOOT_GENERATED = "Generated {when} UTC from data/ at {commit}."

# The two strings the copy button writes into the note line. Decision 2 asks the handler to confirm
# there, and the board does not fix that wording; these are the only page strings not drawn from it,
# and neither says anything about any document.
COPY_OK = "Copied {xr_id} to the clipboard."
COPY_FAILED = "Could not copy. Select the entry and copy it by hand."

# status -> the pill's label and its tone. `fetch_failed` is not on the board, which was drawn from
# the five statuses that matter to a reader, but `DriftStatus` has six and a transport failure has
# to render as something. It is a check that could not RUN, so it takes the same grey as
# `key_missing` rather than the rust that would read as "this document changed".
_PILLS: dict[str, tuple[str, str]] = {
    "ok": ("OK", "ok"),
    "drift": ("DRIFT", "drift"),
    "amended": ("AMENDED", "drift"),
    "key_missing": ("KEY MISSING", "grey"),
    "fetch_failed": ("FETCH FAILED", "grey"),
    "error": ("ERROR", "grey"),
}

_PDF = "application/pdf"


# --------------------------------------------------------------------------- helpers


def _e(value: object) -> str:
    """Escape for both text and attribute position. Citations carry §, URLs carry &."""
    return html.escape(str(value), quote=True)


def _attr(value: str) -> str:
    """Escape a multi-line string into an attribute. `html.unescape` reverses this exactly."""
    return _e(value).replace("\n", "&#10;")


def _is_pdf(source: Source) -> bool:
    return source.artifact.media_type.split(";")[0].strip() == _PDF


def next_check_after(moment: datetime) -> datetime:
    """The next Monday 12:00 UTC strictly after `moment` — the `sources-drift` cron."""
    target = moment.astimezone(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    target += timedelta(days=(0 - target.weekday()) % 7)
    if target <= moment:
        target += timedelta(days=7)
    return target


def _rows(sources: Mapping[str, Source]) -> list[Source]:
    """The pins the table shows, in id order: everything but a `merged_into` loser.

    A loser was wrong, not merely old, so it is not a pinned document at all. A superseded pin is
    history and stays on the page — that chain is one of the things the page exists to show.
    """
    return [s for s in sorted(sources.values(), key=lambda s: s.xr_id) if s.merged_into is None]


def _actors(nodes: Mapping[str, Node]) -> list[Node]:
    """The actor nodes the card shows, in id order. A `merged_into` loser drops, same reason."""
    return [n for n in sorted(nodes.values(), key=lambda n: n.xr_id) if n.merged_into is None]


def _successors(sources: Mapping[str, Source]) -> dict[str, str]:
    """Retired id -> the id that retired it, so a superseded row can name its successor.

    The retired set comes from `registry.superseded_ids`, so "superseded" means here exactly what
    it means to the load-path invariants and to `pin check`'s default target list.
    """
    retired = superseded_ids(sources)
    return {
        s.supersedes: s.xr_id
        for s in sources.values()
        if s.supersedes is not None and s.supersedes in retired
    }


# --------------------------------------------------------------------------- cells


def _check_cell(report: DriftReport | None, superseded: bool) -> str:
    # A superseded pin makes no claim about the current world, so it carries no verdict at all —
    # which is a different thing from a live pin this run simply did not check, and that one gets
    # an em dash rather than an empty cell.
    if superseded:
        return ""
    if report is None:
        return '<span class="none">—</span>'
    label, tone = _PILLS[report.status]
    return f'<span class="pill pill-{tone}">{_e(label)}</span>'


def _document_cell(source: Source, superseded_by: str | None) -> str:
    bits = [f'<span class="cite">{_e(source.citation)}</span>']
    if source.notes:
        bits.append(f' <span class="muted">· {_e(source.notes)}</span>')
    # A pin whose title restates its citation but which still wants a label sets `notes`, which
    # prints ahead of the title and is never suppressed by this rule.
    elif title_adds_anything(source):
        bits.append(f' <span class="muted">{_e(source.title)}</span>')
    if superseded_by is not None:
        bits.append(
            f'<br><span class="muted">{_e(SUPERSEDED_BY.format(xr_id=superseded_by))}</span>'
        )
    sub = []
    if source.publisher:
        sub.append(_e(source.publisher))
    sub.append(
        f'<a href="{_e(source.canonical_url)}">{"source PDF" if _is_pdf(source) else "source"}</a>'
    )
    sub.append(f'<span class="mono">{_e(source.xr_id)}</span>')
    bits.append(f'<br><span class="sub">{" · ".join(sub)}</span>')
    return "".join(bits)


def _version_cell(source: Source) -> str:
    pit = source.point_in_time
    if source.fetcher == "ecfr" and pit is not None:
        head = f"as of {pit.isoformat()}"
    elif source.fetcher == "uscode" and pit is not None:
        head = f"laws in effect on {pit.isoformat()}"
    elif source.published_at is not None:
        head = f"published {source.published_at.isoformat()}"
    elif pit is not None:
        head = f"as of {pit.isoformat()}"
    else:
        head = "—"
    if source.artifact.drift_key == "last_amended":
        sub = AMEND_LINE.format(drift_value=source.artifact.drift_value)
    else:
        sub = SHA_LINE_PDF if _is_pdf(source) else SHA_LINE
    return f'<span class="mono">{_e(head)}</span><br><span class="sub">{_e(sub)}</span>'


def _archive_cell(source: Source, report: DriftReport | None) -> str:
    if source.archives:
        copy = source.archives[0]
        label = copy.service.capitalize()
        if copy.captured_at is not None:
            label += f", {copy.captured_at.date().isoformat()}"
        return f'<a href="{_e(copy.url)}">{_e(label)}</a>'
    # An unarchived pin that has drifted is the one case this column has to shout about: the digest
    # is now the only surviving evidence that the old text existed, and it cannot say what it said.
    if report is not None and report.status == "drift":
        return f'<span class="warn">{_e(ARCHIVE_UNRECOVERABLE)}</span>'
    return f'<span class="muted">{_e(ARCHIVE_NONE)}</span>'


def _row(source: Source, report: DriftReport | None, superseded_by: str | None) -> str:
    drifted = report is not None and report.status in ("drift", "amended")
    cells = (
        _check_cell(report, superseded_by is not None),
        _document_cell(source, superseded_by),
        _version_cell(source),
        _archive_cell(source, report),
        f'<button type="button" class="copy" data-xr-id="{_e(source.xr_id)}" '
        f'data-ledger="{_attr(to_ledger_markdown(source))}">{_e(COPY_BUTTON)}</button>',
    )
    tds = "".join(
        f'<td data-label="{_e(col)}">{cell}</td>' for col, cell in zip(COLUMNS, cells, strict=True)
    )
    open_tag = '<tr class="drifted">' if drifted else "<tr>"
    return f"{open_tag}{tds}</tr>"


# --------------------------------------------------------------------------- sections


def _band(reports: list[DriftReport] | None, n: int, generated_at: datetime) -> str:
    if reports is None:
        state, tone = BAND_UNCHECKED, "grey"
        checked = BAND_BUILT_LOCALLY
    else:
        ok_count = sum(1 for r in reports if r.status == "ok")
        if ok_count == len(reports):
            state, tone = BAND_ALL_OK, "ok"
        else:
            state, tone = BAND_SOME_OK.format(ok_count=ok_count, n=n), "drift"
        checked = BAND_CHECKED_AT.format(when=f"{generated_at:%a %d %b %Y, %H:%M}")
    nxt = BAND_NEXT_CHECK.format(when=f"{next_check_after(generated_at):%d %b}")
    return (
        '<section class="band">'
        f'<p class="state"><span class="dot dot-{tone}"></span>{_e(state)}</p>'
        f'<div class="band-right"><p>{_e(checked)}</p><p>{_e(nxt)}</p></div>'
        "</section>"
    )


def _table(
    sources: Iterable[Source],
    by_id: Mapping[str, DriftReport],
    successors: Mapping[str, str],
) -> str:
    head = "".join(f"<th>{_e(col)}</th>" for col in COLUMNS)
    body = "".join(_row(s, by_id.get(s.xr_id), successors.get(s.xr_id)) for s in sources)
    return f'<table class="pins"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _actors_card(nodes: Iterable[Node]) -> str:
    items = []
    for node in nodes:
        bits = [f'<span class="name">{_e(node.canonical_name)}</span>']
        for ref in node.registers:
            mention = " (mention)" if ref.ref_type == "record_mention" else ""
            bits.append(
                f'<span class="sub">· {_e(ref.register.upper())} '
                f'<span class="mono">{_e(ref.local_id)}</span>{_e(mention)}</span>'
            )
        for ext in node.external_ids:
            text = f'{_e(ext.scheme)} <span class="mono">{_e(ext.value)}</span>'
            if ext.source_url:
                text = f'<a href="{_e(ext.source_url)}">{text}</a>'
            bits.append(f'<span class="sub">· {text}</span>')
        items.append(f"<li>{' '.join(bits)}</li>")
    return (
        f'<section class="card"><h2>{_e(ACTORS_HEADING)}</h2>'
        f'<ul class="actors">{"".join(items)}</ul></section>'
    )


def _fetchers_card() -> str:
    chips = []
    for name in fetchers.NAMES:
        module = fetchers.get(name)
        if module.VERIFIED:
            tone = "ok"
            tail = FETCHERS_VERIFIED.format(when=module.VERIFIED_AT.isoformat())
        else:
            tone, tail = "grey", FETCHERS_UNVERIFIED
        chips.append(
            f'<span class="chip chip-{tone}"><span class="mono">{_e(name)}</span> {_e(tail)}</span>'
        )
    return (
        f'<section class="card"><h2>{_e(FETCHERS_HEADING)}</h2>'
        f'<p class="chips">{"".join(chips)}</p>'
        f'<p class="muted">{_e(FETCHERS_NOTE)}</p></section>'
    )


# --------------------------------------------------------------------------- the page


def render_html(
    xw: Crosswalk,
    reports: list[DriftReport] | None,
    *,
    commit: str | None,
    generated_at: datetime,
) -> str:
    """The whole page as one string. Pure: it reads `xw` and `reports` and touches nothing else."""
    generated_at = generated_at.astimezone(UTC)
    sources = _rows(xw.sources)
    nodes = _actors(xw.nodes)
    by_id = {r.xr_id: r for r in reports or []}
    successors = _successors(xw.sources)
    commit_label = commit or "local"
    counts = COUNTS.format(n=len(sources), m=len(nodes))
    generated = FOOT_GENERATED.format(when=f"{generated_at:%Y-%m-%d %H:%M}", commit=commit_label)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(TITLE)} · {_e(EYEBROW)}</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
        "?family=Fraunces:opsz,wght@9..144,400;9..144,600"
        "&amp;family=IBM+Plex+Mono:wght@400;500"
        "&amp;family=IBM+Plex+Sans:wght@400;500;600"
        '&amp;display=swap">\n'
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        '<main class="wrap">\n'
        '<header class="head">'
        f'<div><p class="eyebrow">{_e(EYEBROW)}</p><h1>{_e(TITLE)}</h1></div>'
        f'<div class="head-right"><p>{_e(counts)}</p>'
        f'<p class="mono">{_e(commit_label)}</p></div>'
        "</header>\n"
        f"{_band(reports, len(sources), generated_at)}\n"
        f"{_table(sources, by_id, successors)}\n"
        f'<p class="copy-note" id="copy-note">{_e(COPY_NOTE)}</p>\n'
        f'<div class="cards">{_actors_card(nodes)}{_fetchers_card()}</div>\n'
        '<footer class="foot">'
        f"<p>{_e(FOOT_LEFT)}</p><p>{_e(FOOT_RIGHT)}</p>"
        f'<p class="muted">{_e(generated)}</p>'
        "</footer>\n"
        "</main>\n"
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


_CSS = """
:root{--ground:#F4F1EA;--surface:#FFFFFF;--ink:#1C1B18;--muted:#5F5B53;--rule:#D9D3C6;
--hairline:#E7E2D8;--ok:#1F6A4A;--ok-bg:#E3EFE8;--drift:#A0491A;--drift-bg:#F3E3DA;
--grey:#5F5B53;--grey-bg:#EEEBE3;--link:#8B4A1C;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
font:400 16px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-text-size-adjust:100%}
.wrap{max-width:1160px;margin:0 auto;padding:40px 24px 64px}
a{color:var(--link)}
a:hover{text-decoration:none}
.mono{font-family:var(--mono)}
.muted,.none{color:var(--muted)}
.warn{color:var(--drift)}
.head{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:flex-end;
padding-bottom:20px;border-bottom:1px solid var(--rule)}
.eyebrow{margin:0 0 6px;color:var(--muted);font-size:12px;letter-spacing:.1em;
text-transform:uppercase;font-family:var(--mono)}
h1{margin:0;font:600 40px/1.1 Fraunces,Georgia,"Times New Roman",serif;letter-spacing:-.01em}
.head-right{text-align:right}
.head-right p{margin:0;color:var(--muted);font-size:14px}
.head-right .mono{font-size:13px}
.band{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center;
margin:20px 0 28px;padding:14px 18px;background:var(--surface);
border:1px solid var(--hairline);border-radius:6px}
.state{margin:0;display:flex;align-items:center;gap:10px;font-size:17px;font-weight:500}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.dot-ok{background:var(--ok)}
.dot-drift{background:var(--drift)}
.dot-grey{background:var(--muted)}
.band-right{text-align:right}
.band-right p{margin:0;color:var(--muted);font-size:13px}
table.pins{width:100%;border-collapse:collapse;background:var(--surface);
border:1px solid var(--hairline);border-radius:6px}
.pins th{text-align:left;padding:12px 14px;border-bottom:1px solid var(--rule);
color:var(--muted);font-size:11px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;
font-family:var(--mono)}
.pins td{padding:16px 14px;border-bottom:1px solid var(--hairline);vertical-align:top}
.pins tbody tr:last-child td{border-bottom:0}
.pins tr.drifted td{background:var(--drift-bg)}
.cite{font-weight:600}
.sub{color:var(--muted);font-size:13px}
.pill{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:500;
letter-spacing:.06em;white-space:nowrap;font-family:var(--mono)}
.pill-ok{color:var(--ok);background:var(--ok-bg)}
.pill-drift{color:var(--drift);background:var(--drift-bg)}
.pill-grey{color:var(--grey);background:var(--grey-bg)}
button.copy{min-height:44px;padding:0 14px;background:var(--surface);color:var(--link);
border:1px solid var(--rule);border-radius:4px;font:inherit;font-size:14px;cursor:pointer;
white-space:nowrap}
button.copy:hover{background:var(--grey-bg)}
button.copy:focus-visible{outline:2px solid var(--link);outline-offset:2px}
.copy-note{margin:12px 2px 0;color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:36px;align-items:start}
.card{background:var(--surface);border:1px solid var(--hairline);border-radius:6px;padding:18px}
.card h2{margin:0 0 14px;font:500 11px/1.2 var(--mono);color:var(--muted);
letter-spacing:.1em;text-transform:uppercase}
.actors{margin:0;padding:0;list-style:none}
.actors li{padding:10px 0;border-bottom:1px solid var(--hairline)}
.actors li:last-child{border-bottom:0;padding-bottom:0}
.name{font-weight:600}
.chips{margin:0 0 12px;display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-block;padding:4px 10px;border-radius:999px;font-size:13px}
.chip-ok{color:var(--ok);background:var(--ok-bg)}
.chip-grey{color:var(--grey);background:var(--grey-bg)}
.foot{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;margin-top:40px;
padding-top:18px;border-top:1px solid var(--rule);font-size:13px;color:var(--muted)}
.foot p{margin:0;max-width:48ch}
.foot p.muted{flex-basis:100%;font-family:var(--mono);font-size:12px}
@media (max-width:720px){
.wrap{padding:24px 16px 48px}
h1{font-size:30px}
.head-right,.band-right,.foot{text-align:left}
.cards{grid-template-columns:1fr}
.pins,.pins thead,.pins tbody,.pins tr,.pins td{display:block;width:100%}
.pins{border:0;background:none;border-radius:0}
.pins thead{position:absolute;width:1px;height:1px;overflow:hidden;
clip:rect(0 0 0 0);white-space:nowrap}
.pins tr{background:var(--surface);border:1px solid var(--hairline);border-radius:6px;
margin-bottom:14px;padding:4px 14px}
.pins tr.drifted{background:var(--drift-bg)}
.pins tr.drifted td{background:none}
.pins td{border-bottom:1px solid var(--hairline);padding:12px 0}
.pins td:empty{display:none}
.pins tr td:last-child{border-bottom:0}
.pins td:before{content:attr(data-label);display:block;color:var(--muted);font-size:10px;
letter-spacing:.1em;text-transform:uppercase;margin-bottom:5px;font-family:var(--mono)}
.pins td[data-label=""]:before{content:none}
button.copy{width:100%}
/* Touch targets. Every link on the phone board sits inside a row card or the actors card, so
   giving it a 44px box costs nothing but the line box it already owns. */
.pins a,.actors a{display:inline-block;min-height:44px;line-height:44px;vertical-align:middle}
}
"""

# The ledger text rides on the button in `data-ledger`, produced by `to_ledger_markdown`, so the
# page and `pin ledger` cannot disagree about what an entry looks like. The fallback path exists
# because `clipboard.writeText` is absent on a non-secure origin and rejects when the document
# is not focused.
_JS = """
(function () {
  var note = document.getElementById('copy-note');
  function say(text) { if (note) { note.textContent = text; } }
  function fallback(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  Array.prototype.forEach.call(document.querySelectorAll('button.copy'), function (btn) {
    var text = btn.getAttribute('data-ledger');
    var done = __COPY_OK__.replace('{xr_id}', btn.getAttribute('data-xr-id'));
    btn.addEventListener('click', function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { say(done); }, function () {
          say(fallback(text) ? done : __COPY_FAILED__);
        });
      } else {
        say(fallback(text) ? done : __COPY_FAILED__);
      }
    });
  });
})();
"""
_JS = _JS.replace("__COPY_OK__", json.dumps(COPY_OK)).replace(
    "__COPY_FAILED__", json.dumps(COPY_FAILED)
)


# --------------------------------------------------------------------------- CLI


def _load_reports(path: Path | None) -> list[DriftReport] | None:
    """The JSON `pin check --json` prints, back into models. None means "no check in this build"."""
    if path is None:
        return None
    return [
        DriftReport.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="registers_crosswalk.status",
        description="Render the read-only status page from data/ and a pin-check report.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DATA_DIR, help="crosswalk data/ dir (default: this repo's)"
    )
    parser.add_argument(
        "--drift-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON from `pin check --json`; without it the page says it was not checked",
    )
    parser.add_argument(
        "--commit", default=None, metavar="SHA", help="default: $GITHUB_SHA[:7], else 'local'"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("build/status/index.html"), help="the one file written"
    )
    args = parser.parse_args(argv)

    commit = args.commit or os.environ.get("GITHUB_SHA", "")[:7] or "local"
    page = render_html(
        Crosswalk(args.data_dir),
        _load_reports(args.drift_json),
        commit=commit,
        generated_at=datetime.now(UTC),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
