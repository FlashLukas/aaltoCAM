"""G-code generation.

Postprocessors are small classes with one hook per machine event. Adding a
dialect means subclassing and overriding two or three methods -- no changes
anywhere else in the program.

All output is metric, absolute, linear-only (G0/G1). Arcs are already
flattened in the geometry layer, so no dialect has to support G2/G3.

Note on height compensation: nothing here modulates Z along a cut. If your
controller applies its own surface compensation (Wegstr, bCNC autolevel,
Candle height map), that stays correct. Compensating twice is a real hazard.

A dialect may also describe the machine behind it -- a feed ceiling, a rapid
rate, a travel envelope, how many decimals are worth writing. Those are used to
clamp what would otherwise be silently wrong and to warn instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace


@dataclass
class JobParams:
    cut_z: float = -0.1
    travel_z: float = 2.0
    feed_xy: float = 120.0
    feed_z: float = 60.0
    spindle: int = 12000
    dwell: float = 0.0
    multidepth: bool = False
    depth_per_pass: float = 0.05
    toolchange: bool = False
    toolchange_z: float = 20.0
    tool_number: int = 1
    tool_diameter: float = 0.2
    start_code: str = ""
    end_code: str = ""


class Postprocessor:
    """Base dialect: plain linear G-code that almost anything will accept."""

    name = "generic"
    description = "Linear G0/G1 only, no arcs, no canned cycles."

    #: Digits after the decimal point. More than the machine can resolve is noise.
    decimals = 4
    #: Feed ceiling in mm/min, or None when the controller has no useful one.
    max_feed: float | None = None
    #: Rapid rate in mm/min when the machine pins it; overrides the estimate's value.
    rapid_rate: float | None = None
    #: Usable travel (x, y, z) in mm from machine zero, or None when unknown.
    envelope: tuple[float, float, float] | None = None

    def __init__(self, p: JobParams):
        self.p = p
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self._low = [math.inf] * 3
        self._high = [-math.inf] * 3
        self._feeds_warned: set[float] = set()

    # -- helpers -----------------------------------------------------------

    def emit(self, line: str):
        if line:
            self.lines.append(line)

    def n(self, value: float) -> str:
        return f"{value:.{self.decimals}f}".rstrip("0").rstrip(".") or "0"

    def comment(self, text: str) -> str:
        """Parentheses inside a comment break naive parsers. Drop them."""
        return "(" + str(text).replace("(", "").replace(")", "").strip() + ")"

    def feed(self, value: float) -> float:
        """Clamp a feed to what the machine can actually run, warning once."""
        if self.max_feed is not None and value > self.max_feed:
            if value not in self._feeds_warned:
                self._feeds_warned.add(value)
                self.warnings.append(
                    f"Feed {value:g} mm/min is above the {self.max_feed:g} mm/min ceiling "
                    f"of the {self.name} controller; written as {self.max_feed:g}."
                )
            return self.max_feed
        return value

    def track(self, x=None, y=None, z=None):
        for index, value in enumerate((x, y, z)):
            if value is None:
                continue
            self._low[index] = min(self._low[index], value)
            self._high[index] = max(self._high[index], value)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def finish(self) -> list[str]:
        """Called once after the last hook. Returns the accumulated warnings.

        Only the span is checked, never the absolute position: zero is wherever
        the operator jogged to, so negative coordinates are ordinary and a job
        that spans more than the travel is the only thing that cannot be placed.
        """
        if self.envelope and self._high[0] > -math.inf:
            for index, axis in enumerate("XYZ"):
                span = self._high[index] - self._low[index]
                travel = self.envelope[index]
                if span > travel:
                    self.warnings.append(
                        f"{axis} span {span:.1f} mm does not fit the {travel:g} mm "
                        f"travel of the {self.name} machine."
                    )
        return self.warnings

    # -- hooks -------------------------------------------------------------

    def header(self, meta: dict):
        self.emit(self.comment(meta.get("title", "aaltocam job")))
        if self.p.tool_diameter:
            self.emit(self.comment(
                f"tool {self.p.tool_number}: dia {self.n(self.p.tool_diameter)} mm"))
        self.emit("G21 (mm)")
        self.emit("G90 (absolute)")
        self.emit("G94 (units per minute)")
        for line in self.p.start_code.splitlines():
            self.emit(line.strip())
        self.rapid_z(self.p.travel_z)

    def spindle_on(self):
        self.emit(f"M3 S{int(self.p.spindle)}")
        if self.p.dwell > 0:
            self.emit(f"G4 P{self.n(self.p.dwell)}")

    def spindle_off(self):
        self.emit("M5")

    def rapid(self, x, y):
        self.track(x=x, y=y)
        self.emit(f"G0 X{self.n(x)} Y{self.n(y)}")

    def rapid_z(self, z):
        self.track(z=z)
        self.emit(f"G0 Z{self.n(z)}")

    def plunge(self, z):
        self.track(z=z)
        self.emit(f"G1 Z{self.n(z)} F{self.n(self.feed(self.p.feed_z))}")

    def cut(self, x, y):
        self.track(x=x, y=y)
        self.emit(f"G1 X{self.n(x)} Y{self.n(y)}")

    def feed_move(self):
        self.emit(f"F{self.n(self.feed(self.p.feed_xy))}")

    def tool_change(self, number: int, diameter: float):
        self.rapid_z(self.p.toolchange_z)
        self.emit("M5")
        self.emit(self.comment(f"change to tool {number}, dia {self.n(diameter)} mm"))
        self.emit("M0")

    def footer(self):
        self.rapid_z(self.p.travel_z)
        self.spindle_off()
        for line in self.p.end_code.splitlines():
            self.emit(line.strip())
        self.emit("M2")


class GrblPost(Postprocessor):
    name = "grbl"
    description = "GRBL / grblHAL. Uses M6 for tool changes."

    def header(self, meta):
        super().header(meta)
        self.emit("G17")

    def tool_change(self, number, diameter):
        self.emit(f"G0 Z{self.n(self.p.toolchange_z)}")
        self.emit("M5")
        self.emit(f"T{number} M6")
        self.emit(f"(dia {self.n(diameter)} mm)")


class LinuxCncPost(Postprocessor):
    name = "linuxcnc"
    description = "LinuxCNC. Emits G43 tool length offsets."

    def tool_change(self, number, diameter):
        self.emit(f"G0 Z{self.n(self.p.toolchange_z)}")
        self.emit("M5")
        self.emit(f"T{number} M6")
        self.emit(f"G43 H{number}")


class WegstrPost(Postprocessor):
    """Wegstr CNC, matched to what the machine's own software actually reads.

    The controller understands G00, G01, G02, G03, the G73/G81/G82/G83 drilling
    cycles, and M00, M03, M04, M05, M06, M47. Everything else is skipped
    silently, so nothing here relies on it: no G4 dwell, no G43, no M2.

    Its parser deletes spaces, cuts the first ``(``...``)`` pair, and then reads
    only the *first* G word on a line. So one G word per line, and no stray
    parentheses inside comments.

    The machine runs at most 170 mm/min, and rapids are no faster -- G00 and G01
    share the ceiling. One step is 0.004 mm, so a fourth decimal is noise.

    Z stays flat: the Wegstr software applies its own surface compensation, and
    a height-mapped file would be compensated twice.
    """

    name = "wegstr"
    description = "Wegstr CNC. 170 mm/min ceiling, M06 + M00 tool change, flat Z."

    decimals = 3
    max_feed = 170.0
    rapid_rate = 170.0
    envelope = (140.0, 200.0, 40.0)
    #: Smallest motion the controller can make, 10 mm / 2500 counts per axis.
    step = 0.004

    def header(self, meta):
        self.emit(self.comment(meta.get("title", "aaltocam job")))
        if self.p.tool_diameter:
            self.emit(self.comment(
                f"tool {self.p.tool_number}: dia {self.n(self.p.tool_diameter)} mm"))
        self.emit("G21")
        self.emit("G90")
        for line in self.p.start_code.splitlines():
            self.emit(line.strip())
        self.rapid_z(self.p.travel_z)

    def spindle_on(self):
        # S is advisory only -- the Wegstr parser ignores it and the spindle is
        # set by hand -- but the vendor's own posts carry it, so keep it.
        if self.p.spindle:
            self.emit(f"S{int(self.p.spindle)} M03")
        else:
            self.emit("M03")
        if self.p.dwell > 0:
            self.warnings.append(
                "Wegstr has no G4 dwell; the spindle dwell was left out of the file."
            )

    def spindle_off(self):
        self.emit("M05")

    def feed_move(self):
        # A bare "F120" line parses, but pairing it with a motion word is what
        # the machine sees from every other CAM tool.
        self.emit(f"G01 F{self.n(self.feed(self.p.feed_xy))}")

    def rapid(self, x, y):
        self.track(x=x, y=y)
        self.emit(f"G00 X{self.n(x)} Y{self.n(y)}")

    def rapid_z(self, z):
        self.track(z=z)
        self.emit(f"G00 Z{self.n(z)}")

    def plunge(self, z):
        self.track(z=z)
        self.emit(f"G01 Z{self.n(z)} F{self.n(self.feed(self.p.feed_z))}")

    def cut(self, x, y):
        self.track(x=x, y=y)
        self.emit(f"G01 X{self.n(x)} Y{self.n(y)}")

    def tool_change(self, number, diameter):
        self.rapid_z(self.p.toolchange_z)
        self.emit("M05")
        self.emit(self.comment(f"tool {number}, dia {self.n(diameter)} mm"))
        # M06 raises the tool-change dialog; the machine requires M00 after it.
        self.emit(f"T{number} M06")
        self.emit("M00")

    def footer(self):
        self.rapid_z(self.p.travel_z)
        self.spindle_off()
        for line in self.p.end_code.splitlines():
            self.emit(line.strip())
        self.emit("M30")


POSTPROCESSORS = {
    p.name: p for p in (Postprocessor, GrblPost, LinuxCncPost, WegstrPost)
}


def _depth_steps(p: JobParams) -> list[float]:
    if not p.multidepth or p.depth_per_pass <= 0:
        return [p.cut_z]
    total = abs(p.cut_z)
    step = abs(p.depth_per_pass)
    steps, depth = [], step
    while depth < total - 1e-9:
        steps.append(-depth)
        depth += step
    steps.append(p.cut_z)
    return steps


def paths_to_gcode(paths, p: JobParams, dialect: str = "grbl", meta: dict | None = None,
                   groups=None, warnings: list[str] | None = None) -> str:
    """Turn ordered LineStrings into G-code.

    `groups` is an optional list of (tool_diameter, paths) from a rest-machining
    operation. When more than one tool is present, a tool change is emitted
    between groups.

    Anything the dialect had to clamp or leave out is appended to `warnings`.
    """
    post = POSTPROCESSORS.get(dialect, Postprocessor)(p)
    post.header(meta or {})

    batches = groups if groups else [(p.tool_diameter, paths)]
    tool_number = p.tool_number
    for index, (diameter, batch) in enumerate(batches):
        if index > 0:
            post.tool_change(tool_number + index, diameter)
        post.spindle_on()
        post.feed_move()

        for path in batch:
            coords = list(path.coords)
            if len(coords) < 2:
                continue
            for z in _depth_steps(p):
                post.rapid(coords[0][0], coords[0][1])
                post.plunge(z)
                post.feed_move()
                for x, y in coords[1:]:
                    post.cut(x, y)
                post.rapid_z(p.travel_z)

    post.footer()
    if warnings is not None:
        warnings.extend(post.finish())
    else:
        post.finish()
    return post.text()


def drills_to_gcode(hits, p: JobParams, dialect: str = "grbl", meta: dict | None = None,
                    warnings: list[str] | None = None) -> str:
    """hits: list of (x, y, diameter), already grouped and ordered."""
    # The header names the tool the operator has to fit first, so it has to be
    # the first hole's drill, not whatever diameter the input payload carried.
    if hits:
        p = replace(p, tool_diameter=hits[0][2])
    post = POSTPROCESSORS.get(dialect, Postprocessor)(p)
    post.header(meta or {})
    post.spindle_on()

    current_dia = None
    tool_number = p.tool_number
    for x, y, dia in hits:
        if current_dia is None:
            current_dia = dia
        elif abs(dia - current_dia) > 1e-6:
            tool_number += 1
            post.tool_change(tool_number, dia)
            post.spindle_on()
            current_dia = dia
        post.rapid(x, y)
        for z in _depth_steps(p):
            post.plunge(z)
            post.rapid_z(p.travel_z)

    post.footer()
    if warnings is not None:
        warnings.extend(post.finish())
    else:
        post.finish()
    return post.text()


def estimate_minutes(source, p: JobParams, rapid_rate: float = 2000.0,
                     dialect: str | None = None) -> float:
    """Rough run time: cutting, rapids, plunges and retracts.

    Ignores acceleration, so it under-estimates on boards made of very short
    segments. Useful for comparing two parameter choices, not for promising
    anyone a finish time.

    When the dialect pins the machine's rapid rate or feed ceiling -- Wegstr
    traverses at the same 170 mm/min it cuts at -- those win over the values
    passed in, otherwise the estimate is off by an order of magnitude.
    """
    post = POSTPROCESSORS.get(dialect or "", Postprocessor)
    if post.rapid_rate is not None:
        rapid_rate = post.rapid_rate
    feed_xy, feed_z = p.feed_xy, p.feed_z
    if post.max_feed is not None:
        feed_xy = min(feed_xy, post.max_feed)
        feed_z = min(feed_z, post.max_feed)

    passes = max(1, len(_depth_steps(p)))
    minutes = 0.0

    if source.kind == "paths":
        cut = source.meta.get("cut_length", 0.0)
        travel = source.meta.get("travel_length", 0.0)
        starts = len(source.data)
    else:
        cut = 0.0
        travel = source.meta.get("travel_length", 0.0)
        starts = len(source.data)

    minutes += (cut * passes) / max(feed_xy, 1e-6)
    minutes += travel / max(rapid_rate, 1e-6)

    # Every path start plunges to depth and retracts to travel height once
    # per depth pass.
    plunge = abs(p.cut_z) + p.travel_z
    minutes += starts * passes * plunge / max(feed_z, 1e-6) / 2
    minutes += starts * passes * p.travel_z / max(rapid_rate, 1e-6)
    return minutes
