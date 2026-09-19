from __future__ import annotations

import sys

from .registry import Crosswalk, unarchived_sources, unverified_sources


def main() -> int:
    xw = Crosswalk()
    print(f"ok: {len(xw.nodes)} node(s)")
    for node in xw.nodes.values():
        refs = ", ".join(f"{r.register}:{r.local_id}({r.ref_type})" for r in node.registers)
        print(f"  {node.xr_id}  {node.canonical_name}  [{refs}]")
    unverified = unverified_sources(xw.sources)
    unarchived = unarchived_sources(xw.sources)
    print(
        f"ok: {len(xw.sources)} source(s), {len(unverified)} unverified-fetcher, "
        f"{len(unarchived)} unarchived"
    )
    for source in xw.sources.values():
        pit = source.point_in_time.isoformat() if source.point_in_time else "-"
        # The marker rides on the line rather than in a footnote: a record whose fetcher was never
        # exercised should be impossible to read past.
        flag = "  [unverified fetcher]" if not source.fetcher_verified else ""
        if not source.archives:
            flag += "  [unarchived]"
        print(
            f"  {source.xr_id}  {source.grade.code()}  {source.citation}  "
            f"{pit}  {source.artifact.sha256[:12]}{flag}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
