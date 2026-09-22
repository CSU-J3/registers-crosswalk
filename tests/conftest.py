"""Fixtures shared across test modules."""

from __future__ import annotations

import argparse
import types

import pytest


@pytest.fixture
def unverified_fetcher(monkeypatch):
    """A fetcher module that has never been run live, registered for the length of one test.

    Every real fetcher is verified since govinfo's first live run on 2026-09-22, so the tests that
    say what an UNverified one does (it ships False, it stamps False onto what it mints, the console
    lists it as unverified and refuses to resolve through it) need one that is not real. Before
    this, each of them borrowed whichever real fetcher had not been run yet, and had to move every
    time one was.

    Its records carry `fetcher="manual"` because `Source.fetcher` is a Literal of the real names:
    the minted record has to validate, and what is under test is the stamp, not the name. It is
    registered under its own name so it never stands in for `manual` anywhere else.
    """
    from registers_crosswalk import fetchers
    from registers_crosswalk.models import Grade
    from registers_crosswalk.pin import PinSpec, default_fetch, sha256_hex

    module = types.ModuleType("unverified_fake")
    module.NAME = "unverified_fake"
    module.HELP = "a fetcher that has never been run live (test fixture)"
    module.DRIFT_KEY = "sha256"
    module.VERIFIED = False
    module.VERIFIED_AT = None

    def spec(*, url, fetch=default_fetch):
        del fetch  # nothing to look up: the url is the document
        return PinSpec(
            fetcher="manual",
            canonical_url=url,
            citation="X",
            title="X",
            grade=Grade(reliability="A", credibility=1),
            drift_value=sha256_hex,
            fetcher_verified=module.VERIFIED,
            verified_at=module.VERIFIED_AT,
        )

    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--url", required=True)

    def spec_from_args(args, *, fetch=default_fetch, env=None):
        del env
        return spec(url=args.url, fetch=fetch)

    module.spec = spec
    module.add_arguments = add_arguments
    module.spec_from_args = spec_from_args
    module.drift_value = sha256_hex

    monkeypatch.setitem(fetchers._MODULES, module.NAME, module)  # noqa: SLF001 - test registration
    monkeypatch.setattr(fetchers, "NAMES", (*fetchers.NAMES, module.NAME))
    return module
