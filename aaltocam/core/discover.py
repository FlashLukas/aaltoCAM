"""Recognise a folder of plotted manufacturing files and build a starting graph.

Filename matching is deliberately our own rather than gerbonara's autoguesser:
the guesser needs a full layer set and raises on a partial one, and a partial
set (top copper and a drill file) is the normal case for milling.
"""

from __future__ import annotations

import os
import re

from .graph import Document

TOP_COPPER = [r"f[_.-]?cu", r"\.gtl$", r"top[_.-]?copper", r"copper[_.-]?top", r"\.top$"]
BOTTOM_COPPER = [r"b[_.-]?cu", r"\.gbl$", r"bottom[_.-]?copper", r"copper[_.-]?bottom", r"\.bot$"]
OUTLINE = [r"edge[_.-]?cuts", r"\.gko$", r"\.gm1$", r"outline", r"boardoutline", r"\.gml$"]
DRILL = [r"\.drl$", r"\.xln$", r"\.txt$", r"\.exc$", r"drill"]
SKIP = [r"paste", r"mask", r"silk", r"\.gbrjob$", r"report", r"readme", r"\.d356$"]

GERBER_EXT = (".gbr", ".ger", ".gtl", ".gbl", ".gko", ".gm1", ".gml", ".art", ".pho")
DRILL_EXT = (".drl", ".xln", ".txt", ".exc")


def _matches(name: str, patterns) -> bool:
    lowered = name.lower()
    return any(re.search(p, lowered) for p in patterns)


def classify(directory: str) -> dict:
    """Sort a directory's files into the roles aaltocam cares about."""
    found = {"top": None, "bottom": None, "outline": None, "drills": [], "unknown": []}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found

    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        lowered = name.lower()
        if _matches(lowered, SKIP):
            continue
        if lowered.endswith(DRILL_EXT) and _matches(lowered, DRILL):
            found["drills"].append(path)
        elif lowered.endswith(GERBER_EXT) or lowered.endswith(".gbr"):
            if _matches(lowered, OUTLINE):
                found["outline"] = found["outline"] or path
            elif _matches(lowered, TOP_COPPER):
                found["top"] = found["top"] or path
            elif _matches(lowered, BOTTOM_COPPER):
                found["bottom"] = found["bottom"] or path
            else:
                found["unknown"].append(path)
    return found


def build_board(directory: str, side: str = "top",
                origin: bool = False) -> tuple[Document, list[str]]:
    """Build a ready-to-cut graph from a plot folder.

    Returns the document and a list of notes about what was and was not
    recognised, so the caller can say something useful instead of silently
    producing an empty project.

    `origin` puts the board's bottom-left corner on X0 Y0, which is where a
    Gerber plotted on KiCad's absolute origin is least useful: it arrives
    wherever it sat on the sheet, with negative Y.

    Both the mirror for the bottom side and the move to the origin are done by
    one Transform per layer, all against the *same* reference -- the board
    outline where there is one. That shared reference is the point: measuring
    each layer against its own bounding box looks right on screen and drills
    through the wrong pads, because copper and drills have different extents.
    """
    found = classify(directory)
    notes: list[str] = []
    doc = Document()
    doc.base_dir = os.path.abspath(directory)
    doc.source = {"side": side, "origin": bool(origin)}

    wanted = found[side]
    if wanted is None and found[side] is None:
        other = "bottom" if side == "top" else "top"
        wanted = found[other]
        if wanted is not None:
            notes.append(f"No {side} copper in this folder; built the {other} side instead.")
            side = other
            doc.source["side"] = side
    copper_path = wanted or found["top"] or found["bottom"]
    if copper_path is None:
        notes.append("No copper layer found.")
        return doc, notes

    def rel(path):
        return os.path.relpath(path, doc.base_dir)

    copper = doc.add("load_gerber", path=rel(copper_path),
                     name=os.path.basename(copper_path))
    notes.append(f"Copper: {os.path.basename(copper_path)}")

    outline_id = ""
    if found["outline"]:
        outline = doc.add("load_gerber", path=rel(found["outline"]),
                          name=os.path.basename(found["outline"]))
        outline_id = outline.id
        notes.append(f"Outline: {os.path.basename(found['outline'])}")

    flip = side == "bottom"
    # One shared reference for every layer, so they all move by the same delta.
    reference_id = outline_id or copper.id
    reference_name = "the outline" if outline_id else "the copper"

    def placed(source_id: str, label: str) -> str:
        """Flip and/or zero one layer. Returns the layer itself when neither."""
        if not (flip or origin):
            return source_id
        what = "Flip" if flip else "Zero"
        if flip and origin:
            what = "Flip and zero"
        node = doc.add("transform", [source_id, reference_id],
                       mirror="y" if flip else "none",
                       align="bottom left" if origin else "none",
                       name=f"{what} {label}")
        # The untransformed layer still sits where the plot put it, so leaving
        # it drawn shows the board twice, in two places. Hide it; the checkbox
        # in the list turns it back on.
        doc.nodes[source_id].visible = False
        return node.id

    if flip:
        notes.append(f"Bottom side: every layer mirrored about Y against {reference_name}, "
                     "so the board is turned over left to right.")
    if origin:
        notes.append(f"Moved to the origin: the bottom-left corner of {reference_name} "
                     "is now X0 Y0, and every layer shifted with it.")

    copper_id = placed(copper.id, "copper")
    cut_outline_id = placed(outline_id, "outline") if outline_id else ""

    isolate = doc.add("isolate", [copper_id], tool_shape="v", passes=2, name="Isolation")
    doc.add("cnc_job", [isolate.id], cut_z=-0.1, feed_xy=150, name="Isolation job")

    if found["drills"]:
        drill_path = found["drills"][0]
        excellon = doc.add("load_excellon", path=rel(drill_path),
                           name=os.path.basename(drill_path))
        notes.append(f"Drills: {os.path.basename(drill_path)}")
        drills_id = placed(excellon.id, "drills")
        holes = doc.add("drill_holes", [drills_id], name="Small holes (drill)")
        doc.add("cnc_job", [holes.id], cut_z=-1.8, multidepth=True, depth_per_pass=0.6,
                name="Drill job")
        milled = doc.add("mill_holes", [drills_id], tool_dia=0.8, name="Large holes (mill)")
        doc.add("cnc_job", [milled.id], cut_z=-1.8, multidepth=True, depth_per_pass=0.3,
                feed_xy=150, name="Hole milling job")
        if len(found["drills"]) > 1:
            extra = ", ".join(os.path.basename(p) for p in found["drills"][1:])
            notes.append(f"Other drill files not loaded: {extra}")

    cutout = doc.add("cutout", [copper_id, cut_outline_id], name="Board cutout",
                     shape="outline input" if cut_outline_id else "rectangle")
    doc.add("cnc_job", [cutout.id], cut_z=-1.8, multidepth=True, depth_per_pass=0.4,
            feed_xy=200, name="Cutout job")

    if found["unknown"]:
        notes.append("Not recognised: " +
                     ", ".join(os.path.basename(p) for p in found["unknown"]))
    return doc, notes


def sides_present(directory: str) -> list[str]:
    """Which copper sides this folder actually has, for asking the user once."""
    found = classify(directory)
    return [name for name in ("top", "bottom") if found[name]]
