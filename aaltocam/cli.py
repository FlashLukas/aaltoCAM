"""Headless evaluation.

The CLI and the GUI drive the same document, so a project built by clicking
can be re-run from a Makefile after the KiCad plot changes.
"""

from __future__ import annotations

import argparse
import os
import sys

from .core import project as project_io


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aaltocam", description=__doc__)
    parser.add_argument("project", help="project .toml file")
    parser.add_argument("-o", "--outdir", default=".", help="where to write G-code")
    parser.add_argument("-n", "--node", action="append", default=[],
                        help="only evaluate these node ids (repeatable)")
    parser.add_argument("-l", "--list", action="store_true", help="list nodes and exit")
    args = parser.parse_args(argv)

    doc = project_io.load(args.project)

    if args.list:
        for node_id in doc.order:
            node = doc.nodes[node_id]
            deps = ", ".join(node.inputs) or "-"
            print(f"{node_id:20s} {node.operation().label:22s} inputs: {deps}")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    wanted = args.node or [i for i in doc.order if doc.nodes[i].operation().output == "gcode"]
    if not wanted:
        print("nothing to do: no CNC job nodes in project", file=sys.stderr)
        return 1

    status = 0
    for node_id in wanted:
        node = doc.nodes[node_id]
        try:
            result = doc.evaluate(node_id)
        except Exception as exc:
            print(f"{node_id}: {exc}", file=sys.stderr)
            status = 1
            continue
        if result.kind != "gcode":
            stats = ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                              for k, v in result.meta.items())
            print(f"{node_id}: {result.kind}  {stats}")
            continue
        out = os.path.join(args.outdir, f"{node_id}.nc")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(result.data)
        cut = result.meta.get("cut_length")
        travel = result.meta.get("travel_length")
        extra = ""
        if cut is not None:
            extra = f"  cutting {cut:.0f} mm, travel {travel:.0f} mm"
        print(f"{out}  ({result.meta.get('lines', 0)} lines){extra}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
