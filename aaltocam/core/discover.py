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


def build_board(directory: str, side: str = "top") -> tuple[Document, list[str]]:
    """Build a ready-to-cut graph from a plot folder.

    Returns the document and a list of notes about what was and was not
    recognised, so the caller can say something useful instead of silently
    producing an empty project.
    """
    found = classify(directory)
    notes: list[str] = []
    doc = Document()
    doc.base_dir = os.path.abspath(directory)

    copper_path = found[side] or found["top"] or found["bottom"]
    if copper_path is None:
        notes.append("No copper layer found.")
        return doc, notes

    def rel(path):
        return os.path.relpath(path, doc.base_dir)

    copper = doc.add("load_gerber", path=rel(copper_path),
                     name=os.path.basename(copper_path))
    notes.append(f"Copper: {os.path.basename(copper_path)}")

    isolate = doc.add("isolate", [copper.id], tool_shape="v", passes=2, name="Isolation")
    doc.add("cnc_job", [isolate.id], cut_z=-0.1, feed_xy=150, name="Isolation job")

    if found["drills"]:
        drill_path = found["drills"][0]
        excellon = doc.add("load_excellon", path=rel(drill_path),
                           name=os.path.basename(drill_path))
        notes.append(f"Drills: {os.path.basename(drill_path)}")
        holes = doc.add("drill_holes", [excellon.id], name="Small holes (drill)")
        doc.add("cnc_job", [holes.id], cut_z=-1.8, multidepth=True, depth_per_pass=0.6,
                name="Drill job")
        milled = doc.add("mill_holes", [excellon.id], tool_dia=0.8, name="Large holes (mill)")
        doc.add("cnc_job", [milled.id], cut_z=-1.8, multidepth=True, depth_per_pass=0.3,
                feed_xy=150, name="Hole milling job")
        if len(found["drills"]) > 1:
            extra = ", ".join(os.path.basename(p) for p in found["drills"][1:])
            notes.append(f"Other drill files not loaded: {extra}")

    outline_id = ""
    if found["outline"]:
        outline = doc.add("load_gerber", path=rel(found["outline"]),
                          name=os.path.basename(found["outline"]))
        outline_id = outline.id
        notes.append(f"Outline: {os.path.basename(found['outline'])}")

    cutout = doc.add("cutout", [copper.id, outline_id], name="Board cutout",
                     shape="outline input" if outline_id else "rectangle")
    doc.add("cnc_job", [cutout.id], cut_z=-1.8, multidepth=True, depth_per_pass=0.4,
            feed_xy=200, name="Cutout job")

    if found["unknown"]:
        notes.append("Not recognised: " +
                     ", ".join(os.path.basename(p) for p in found["unknown"]))
    return doc, notes
