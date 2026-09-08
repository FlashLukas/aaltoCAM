"""Headless evaluation.

The CLI and the GUI drive the same document, so a project built by clicking
can be re-run from a Makefile after the KiCad plot changes.

The input can be a project .toml, a folder of plotted Gerbers, or a
.kicad_pcb -- the last of which is re-plotted through kicad-cli first, so a
Makefile can go from board file to G-code in one step.
"""

from __future__ import annotations

import argparse
import os
import sys

from .core import discover, kicad
from .core import project as project_io


def _open(args):
    """Accept a project, a plot folder, or a KiCad board."""
    source = args.project
    if source.lower().endswith(".kicad_pcb"):
        doc, notes = kicad.open_board(source, side=args.side, cli=args.kicad_cli,
                                      force=args.replot)
    elif os.path.isdir(source):
        doc, notes = discover.build_board(source, args.side)
    else:
        return project_io.load(source)

    for note in notes:
        print(note, file=sys.stderr)
    if not doc.order:
        raise RuntimeError(f"nothing recognised in {source}")
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aaltocam", description=__doc__)
    parser.add_argument("project",
                        help="project .toml, a folder of Gerbers, or a .kicad_pcb")
    parser.add_argument("-o", "--outdir", default=".", help="where to write G-code")
    parser.add_argument("-n", "--node", action="append", default=[],
                        help="only evaluate these node ids (repeatable)")
    parser.add_argument("-l", "--list", action="store_true", help="list nodes and exit")
    parser.add_argument("--side", default="top", choices=("top", "bottom"),
                        help="which copper side to build, when starting from a board")
    parser.add_argument("--kicad-cli", default=None,
                        help="path to kicad-cli, if it is not on PATH")
    parser.add_argument("--replot", action="store_true",
                        help="force a fresh plot even if the existing one looks current")
    parser.add_argument("--dialect", default=None,
                        help="override the postprocessor on every CNC job node")
    args = parser.parse_args(argv)

    try:
        doc = _open(args)
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.dialect:
        for node in doc.nodes.values():
            if "dialect" in node.params:
                node.params["dialect"] = args.dialect

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
        for warning in result.meta.get("warnings", []):
            print(f"  warning: {warning}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
