"""Board to Wegstr G-code in one command, with the cutters chosen from the library.

    python examples/wegstr_job.py board.kicad_pcb -o gcode/
    python examples/wegstr_job.py plot-folder/ -o gcode/ --side bottom

The plain CLI builds the same graph but leaves every tool unchosen, so its jobs
run on placeholder feeds. This picks a library tool for each operation, sets
every CNC job to the wegstr dialect, saves the project beside the G-code so it
can be opened in the GUI and checked, and refuses to call the run clean when
the clearance check finds a short or a job leaves the machine's envelope.

It writes files and nothing else. It never talks to the machine.

Defaults are the tools in examples/tools/wegstr-lpkf.toml that carry measured
feeds. Run with AALTOCAM_TOOLS pointing at that file, or with the same ids in
your own library.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from aaltocam.core import discover, kicad, ops
from aaltocam.core import project as project_io
from aaltocam.core import tools as toollib

ENVELOPE = (140.0, 200.0)       # Wegstr X, Y travel in mm


def build(args):
    if args.board.lower().endswith(".kicad_pcb"):
        doc, notes = kicad.open_board(args.board, side=args.side, cli=args.kicad_cli,
                                      force=args.replot, origin=True)
    else:
        doc, notes = discover.build_board(args.board, args.side, origin=True)
    if not doc.order:
        raise SystemExit(f"nothing recognised in {args.board}")
    return doc, notes


def need(tool_id: str) -> toollib.Tool:
    tool = toollib.get(tool_id)
    if tool is None:
        raise SystemExit(
            f"tool '{tool_id}' is not in the library at {toollib.library().path}. "
            f"Set AALTOCAM_TOOLS to examples/tools/wegstr-lpkf.toml or pass another id.")
    return tool


def choose_tools(doc, args):
    """Put a library tool on every geometry node and line its job up with it."""
    picks = {
        "isolate": need(args.isolation_tool),
        "cutout": need(args.cutout_tool),
        "mill_holes": need(args.hole_mill_tool),
    }
    for node in list(doc.nodes.values()):
        tool = picks.get(node.op)
        if tool is None:
            continue
        node.params["tool"] = tool.id
        if node.op == "isolate":
            node.params["passes"] = args.passes
            if tool.cut_z < 0:
                node.params["cut_depth"] = -tool.cut_z
        if node.op == "cutout":
            node.params["gaps"] = args.tabs
        ops.sync_tool_params(node, tool)

    # The GUI does this once the geometry has evaluated: a CNC job takes its
    # cutter, and with it its feeds and depth, from the node feeding it.
    for node in doc.nodes.values():
        if node.op != "cnc_job":
            continue
        node.params["dialect"] = "wegstr"
        node.params["feeds_from_tool"] = True
        source = doc.nodes.get(node.inputs[0]) if node.inputs else None
        tool = toollib.get(str(source.params.get("tool", ""))) if source else None
        if tool is not None:
            ops.sync_tool_params(node, tool)

    # Shorts the isolation cutter cannot clear, found before anything is cut.
    iso = next((n for n in doc.nodes.values() if n.op == "isolate"), None)
    if iso is not None:
        tool = picks["isolate"]
        doc.add("clearance_check", [iso.inputs[0]], name="Clearance check",
                tool_dia=tool.diameter, tool_shape=tool.shape,
                tip_dia=tool.tip_diameter or 0.02, tip_angle=tool.tip_angle or 30.0,
                cut_depth=iso.params.get("cut_depth", 0.1))
    doc.invalidate()


WORD = re.compile(r"([XYZF])(-?\d+(?:\.\d+)?)")


def survey(gcode: str):
    """Lowest Z, highest feed and the XY extent actually written."""
    low_z, top_feed, xs, ys = 0.0, 0.0, [], []
    for line in gcode.splitlines():
        if line.startswith("("):
            continue
        for letter, value in WORD.findall(line):
            v = float(value)
            if letter == "Z":
                low_z = min(low_z, v)
            elif letter == "F":
                top_feed = max(top_feed, v)
            elif letter == "X":
                xs.append(v)
            else:
                ys.append(v)
    extent = (min(xs, default=0), max(xs, default=0), min(ys, default=0), max(ys, default=0))
    return low_z, top_feed, extent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", help=".kicad_pcb, or a folder of plotted Gerbers")
    parser.add_argument("-o", "--outdir", default="gcode")
    parser.add_argument("--side", default="top", choices=("top", "bottom"))
    parser.add_argument("--isolation-tool", default="lpkf-rf-0.15")
    parser.add_argument("--passes", type=int, default=2,
                        help="isolation passes; more widens the gap round each track")
    parser.add_argument("--cutout-tool", default="router-1.0")
    parser.add_argument("--hole-mill-tool", default="lpkf-end-1.0")
    parser.add_argument("--tabs", default="4", choices=("0", "2", "4", "8"))
    parser.add_argument("--thickness", type=float, default=1.6,
                        help="board thickness in mm; drilling, hole milling and the "
                             "cutout must reach at least this deep")
    parser.add_argument("--kicad-cli", default=None)
    parser.add_argument("--replot", action="store_true")
    args = parser.parse_args(argv)

    doc, notes = build(args)
    for note in notes:
        print(f"note: {note}")
    choose_tools(doc, args)

    os.makedirs(args.outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(os.path.normpath(args.board)))[0]
    project_path = os.path.join(args.outdir, f"{stem}-{args.side}.toml")
    project_io.save(doc, project_path)
    print(f"project: {project_path}")

    problems = 0
    for node_id in doc.order:
        node = doc.nodes[node_id]
        try:
            result = doc.evaluate(node_id)
        except Exception as exc:
            print(f"ERROR {node.name}: {exc}")
            problems += 1
            continue

        if node.op == "clearance_check":
            shorts = result.meta.get("shorts", 0)
            print(f"clearance: {shorts} short(s), "
                  f"{result.meta.get('rounded_corners', 0)} rounded corner(s) "
                  f"with a {result.meta['tool_diameter']:.3f} mm cut")
            for x, y, area in result.meta.get("spots", [])[:10]:
                print(f"  short near X{x} Y{y} ({area} mm2)")
            problems += bool(shorts)
            continue
        if result.kind != "gcode":
            continue

        out = os.path.join(args.outdir, f"{stem}-{args.side}-{node_id}.nc")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(result.data)
        low_z, top_feed, (x0, x1, y0, y1) = survey(result.data)
        source = doc.nodes.get(node.inputs[0]) if node.inputs else None
        through = source is not None and source.op in ("drill_holes", "mill_holes",
                                                       "cutout", "mill_slots")
        tool = result.meta.get("tool") or (
            "tool per hole size" if source is not None and source.op == "drill_holes"
            else "no library tool")
        print(f"{out}\n  {node.name}: {tool}, {result.meta.get('lines', 0)} lines, "
              f"~{result.meta.get('minutes', 0):.0f} min, lowest Z {low_z:.3f}, "
              f"top feed {top_feed:.0f}, X {x0:.2f}..{x1:.2f} Y {y0:.2f}..{y1:.2f}")
        for warning in result.meta.get("warnings", []):
            print(f"  warning: {warning}")
        if through and -args.thickness < low_z < 0:
            # The tool's measured cut_z wins over the job's own field, so a
            # depth proven on a thinner board would quietly stop short here.
            print(f"  PROBLEM: deepest cut is Z{low_z:.3f}, which does not get "
                  f"through a {args.thickness:g} mm board")
            problems += 1
        if x1 - x0 > ENVELOPE[0] or y1 - y0 > ENVELOPE[1]:
            print(f"  PROBLEM: spans {x1 - x0:.1f} x {y1 - y0:.1f} mm, more than the "
                  f"Wegstr's {ENVELOPE[0]:g} x {ENVELOPE[1]:g} mm travel")
            problems += 1
        elif x0 < 0 or y0 < 0:
            # An outside cutout runs a tool radius and the margin beyond the
            # board edge, so it goes below zero by design.
            print(f"  note: reaches X{x0:.2f} Y{y0:.2f}; zero the machine with at "
                  f"least that much room to the left and in front")

    print("clean" if not problems else f"{problems} problem(s): read them before cutting")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
