from __future__ import annotations

import sys

from .registry import Crosswalk


def main() -> int:
    xw = Crosswalk()
    print(f"ok: {len(xw.nodes)} node(s)")
    for node in xw.nodes.values():
        refs = ", ".join(f"{r.register}:{r.local_id}({r.ref_type})" for r in node.registers)
        print(f"  {node.xr_id}  {node.canonical_name}  [{refs}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
