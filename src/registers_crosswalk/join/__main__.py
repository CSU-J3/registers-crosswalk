from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import join
from .resolvers import CPResolver, SovereignResolver, VIResolver

_RESOLVERS = {r.register: r for r in (VIResolver(), CPResolver(), SovereignResolver())}


def _parse_roots(pairs: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for pair in pairs:
        register, sep, path = pair.partition("=")
        if not sep or not register or not path:
            raise SystemExit(f"--root expects register=path, got {pair!r}")
        roots[register] = Path(path).expanduser()
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="registers_crosswalk.join")
    parser.add_argument("xr_id", help="e.g. xr_holder_0001")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="register=path",
        help="checkout root for a register, e.g. vi=~/src/Vested-Interests (repeatable)",
    )
    parser.add_argument("--state", choices=("working_tree", "committed"), default="working_tree")
    parser.add_argument("--data-dir", default=None, help="crosswalk data dir override")
    args = parser.parse_args(argv)

    view = join(
        args.xr_id,
        _RESOLVERS,
        _parse_roots(args.root),
        source_state=args.state,
        data_dir=Path(args.data_dir) if args.data_dir else None,
    )
    print(view.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
