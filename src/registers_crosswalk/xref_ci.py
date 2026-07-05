from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .refresolve import check_refs

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def main(argv: list[str] | None = None) -> int:
    """CI cross-repo ref check: assert every crosswalk ref exists in the sibling checkout given.

    Unlike the pre-commit hook (working tree, skip-if-absent), this REQUIRES all three sibling
    roots — CI checks them out from committed main (or a pinned ref) and any missing ref fails.
    """
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--vi", type=Path, required=True, help="checkout root of Vested-Interests")
    ap.add_argument("--cp", type=Path, required=True, help="checkout root of Connected-Procurement")
    ap.add_argument(
        "--sovereign", type=Path, required=True, help="checkout root of Sovereign-Connections"
    )
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR, help="crosswalk data/ dir")
    args = ap.parse_args(argv)

    roots = {"vi": args.vi, "cp": args.cp, "sovereign": args.sovereign}
    missing = [reg for reg, root in roots.items() if not root.is_dir()]
    if missing:
        print(f"cross-repo ref check: sibling checkout(s) missing: {missing}")
        return 2

    problems = check_refs(args.data_dir, roots)
    if problems:
        print("cross-repo ref check FAILED (refs absent from sibling committed state):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("cross-repo ref check: all refs resolve against sibling committed checkouts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
