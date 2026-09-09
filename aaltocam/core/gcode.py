"""G-code generation.

Postprocessors are small classes with one hook per machine event. Adding a
dialect means subclassing and overriding two or three methods -- no changes
anywhere else in the program.

All output is metric and absolute. Dialects that declare `supports_arcs` are
given G02/G03 for runs that fit a circle; the rest get line segments only, so
adding a dialect never means implementing arcs.

Note on height compensation: Z is flat along a cut unless a height map is
passed to `paths_to_gcode`, and then it follows the probed surface. Doing this
when the controller already compensates -- Wegstr, bCNC autolevel, a Candle
height map -- applies the correction twice and doubles the error, so a dialect
that levels for itself refuses unless the caller says otherwise in as many
words.

A dialect may also describe the machine behind it -- a feed ceiling, a rapid
rate, a travel envelope, how many decimals are worth writing. Those are used to
clamp what would otherwise be silently wrong and to warn instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from . import arcfit
from . import heightmap


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
    #: False when the controller ignores S and the spindle is set by hand.
    commands_spindle = True
    #: Whether the controller takes G02/G03 with I/J in the XY plane.
    supports_arcs = False
    #: True when the machine's own software levels to a probed surface. A
    #: compensated file sent to one of these is corrected twice.
    self_levelling = False
    #: Radius bounds for an emitted arc, mm.
    min_arc_radius = 0.05
    max_arc_radius = 1000.0

    def __init__(self, p: JobParams):
        self.p = p
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self._low = [math.inf] * 3
        self._high = [-math.inf] * 3
        self._feeds_warned: set[float] = set()
        self._pos = (0.0, 0.0)

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
        self._pos = (x, y)
        self.emit(f"G0 X{self.n(x)} Y{self.n(y)}")

    def rapid_z(self, z):
        self.track(z=z)
        self.emit(f"G0 Z{self.n(z)}")

    def plunge(self, z):
        self.track(z=z)
        self.emit(f"G1 Z{self.n(z)} F{self.n(self.feed(self.p.feed_z))}")

    def cut(self, x, y, z=None):
        """Feed move. A z of None leaves the axis where it already is.

        Compensation writes Z only where it has actually moved, so most cuts
        stay two words wide even on a mapped job.
        """
        self.track(x=x, y=y, z=z)
        self._pos = (x, y)
        if z is None:
            self.emit(f"G1 X{self.n(x)} Y{self.n(y)}")
        else:
            self.emit(f"G1 X{self.n(x)} Y{self.n(y)} Z{self.n(z)}")

    def arc(self, x, y, cx, cy, clockwise):
        """Circular move to (x, y) about absolute centre (cx, cy).

        I and J are incremental from the current point, which is the convention
        every controller here uses and the reason the centre is passed absolute:
        the postprocessor knows where it is, the caller should not have to.
        """
        i, j = cx - self._pos[0], cy - self._pos[1]
        self.track(x=x, y=y)
        self._pos = (x, y)
        self.emit(f"{'G2' if clockwise else 'G3'} X{self.n(x)} Y{self.n(y)} "
                  f"I{self.n(i)} J{self.n(j)}")

    def feed_move(self):
        self.emit(f"F{self.n(self.feed(self.p.feed_xy))}")

    def tool_note(self, tool):
        """One comment describing the cutter about to be used.

        The spindle line is the only place the operator sees the recorded rpm on
        a machine that cannot be told it over G-code.
        """
        if tool is None:
            return
        bits = [tool.name or tool.id, f"dia {self.n(tool.diameter)} mm"]
        if tool.spindle_rpm:
            bits.append(f"spindle {tool.spindle_rpm} rpm"
                        + ("" if self.commands_spindle else " - set by hand"))
        if tool.notes:
            bits.append(tool.notes)
        self.emit(self.comment(", ".join(bits)))

    def tool_change(self, number: int, diameter: float, tool=None):
        self.rapid_z(self.p.toolchange_z)
        self.emit("M5")
        self.emit(self.comment(f"change to tool {number}, dia {self.n(diameter)} mm"))
        self.tool_note(tool)
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
    supports_arcs = True

    def header(self, meta):
        super().header(meta)
        self.emit("G17")

    def tool_change(self, number, diameter, tool=None):
        self.emit(f"G0 Z{self.n(self.p.toolchange_z)}")
        self.emit("M5")
        self.emit(f"T{number} M6")
        self.emit(f"(dia {self.n(diameter)} mm)")
        self.tool_note(tool)


class LinuxCncPost(Postprocessor):
    name = "linuxcnc"
    description = "LinuxCNC. Emits G43 tool length offsets."
    supports_arcs = True

    def tool_change(self, number, diameter, tool=None):
        self.emit(f"G0 Z{self.n(self.p.toolchange_z)}")
        self.emit("M5")
        self.emit(f"T{number} M6")
        self.emit(f"G43 H{number}")
        self.tool_note(tool)


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

    The Wegstr software applies its own surface compensation, so this dialect
    declares itself self-levelling and refuses a height map: the correction
    would otherwise be applied twice and double the error it exists to remove.
    """

    name = "wegstr"
    description = "Wegstr CNC. 170 mm/min ceiling, M06 + M00 tool change, flat Z."

    self_levelling = True

    decimals = 3
    max_feed = 170.0
    rapid_rate = 170.0
    envelope = (140.0, 200.0, 40.0)
    commands_spindle = False
    supports_arcs = True
    #: From the vendor machine definition: arcs below 0.055 mm radius or above
    #: 200 mm are not executed.
    min_arc_radius = 0.055
    max_arc_radius = 200.0
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
        self._pos = (x, y)
        self.emit(f"G00 X{self.n(x)} Y{self.n(y)}")

    def rapid_z(self, z):
        self.track(z=z)
        self.emit(f"G00 Z{self.n(z)}")

    def plunge(self, z):
        self.track(z=z)
        self.emit(f"G01 Z{self.n(z)} F{self.n(self.feed(self.p.feed_z))}")

    def cut(self, x, y, z=None):
        self.track(x=x, y=y, z=z)
        self._pos = (x, y)
        if z is None:
            self.emit(f"G01 X{self.n(x)} Y{self.n(y)}")
        else:
            self.emit(f"G01 X{self.n(x)} Y{self.n(y)} Z{self.n(z)}")

    def arc(self, x, y, cx, cy, clockwise):
        i, j = cx - self._pos[0], cy - self._pos[1]
        self.track(x=x, y=y)
        self._pos = (x, y)
        self.emit(f"{'G02' if clockwise else 'G03'} X{self.n(x)} Y{self.n(y)} "
                  f"I{self.n(i)} J{self.n(j)}")

    def tool_change(self, number, diameter, tool=None):
        self.rapid_z(self.p.toolchange_z)
        self.emit("M05")
        self.emit(self.comment(f"tool {number}, dia {self.n(diameter)} mm"))
        self.tool_note(tool)
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


def apply_tool(p: JobParams, tool) -> JobParams:
    """Overlay a tool's established feeds on a job's parameters.

    Only fields the tool actually carries are overlaid: an unset feed leaves the
    job's own value alone, so a tool that has geometry but no proven numbers yet
    still selects the right cutter without pretending to know how to run it.
    """
    if tool is None:
        return p
    changes = {"tool_diameter": tool.diameter}
    if tool.feed_xy > 0:
        changes["feed_xy"] = tool.feed_xy
    if tool.feed_z > 0:
        changes["feed_z"] = tool.feed_z
    if tool.cut_z < 0:
        changes["cut_z"] = tool.cut_z
    if tool.depth_per_pass > 0:
        changes["depth_per_pass"] = tool.depth_per_pass
        changes["multidepth"] = True
    if tool.spindle_rpm > 0:
        changes["spindle"] = int(tool.spindle_rpm)
    return replace(p, **changes)


def _emit_path(post, coords, arc_tolerance, hm=None, base_z=0.0,
               segment=1.0, z_tolerance=0.0):
    """Cut along one polyline, as arcs where the dialect and the shape allow."""
    if hm is not None:
        # No arcs here, and not by oversight: a G02/G03 holds Z across its whole
        # sweep, so a compensated arc is right at its two ends and wrong in the
        # middle. The caller has already forced arc fitting off.
        for x, y, z in heightmap.compensate(coords, hm, base_z, segment, z_tolerance):
            post.cut(x, y, z)
        return
    if not (arc_tolerance > 0 and post.supports_arcs):
        for x, y in coords[1:]:
            post.cut(x, y)
        return
    for move in arcfit.fit(coords, tolerance=arc_tolerance,
                           min_radius=post.min_arc_radius,
                           max_radius=post.max_arc_radius):
        if move[0] == "line":
            post.cut(move[1][0], move[1][1])
        else:
            (x, y), (cx, cy), clockwise = move[1], move[2], move[3]
            post.arc(x, y, cx, cy, clockwise)


def paths_to_gcode(paths, p: JobParams, dialect: str = "grbl", meta: dict | None = None,
                   groups=None, warnings: list[str] | None = None,
                   tool_lookup=None, arc_tolerance: float = 0.0,
                   hm=None, segment: float = 1.0, z_tolerance: float = 0.0,
                   allow_double_levelling: bool = False) -> str:
    """Turn ordered LineStrings into G-code.

    `groups` is an optional list of (tool_diameter, paths) from a rest-machining
    operation. When more than one tool is present, a tool change is emitted
    between groups.

    `tool_lookup` maps a diameter to a library tool, so each group of a
    rest-machining job runs at its own cutter's feeds rather than one set of
    numbers for all of them.

    `arc_tolerance` above zero refits circular runs into G02/G03 on dialects that
    support them, within that deviation in mm.

    Anything the dialect had to clamp or leave out is appended to `warnings`.
    """
    post = POSTPROCESSORS.get(dialect, Postprocessor)(p)
    base = p

    if hm is not None:
        if post.self_levelling and not allow_double_levelling:
            raise ValueError(
                f"The {post.name} controller applies its own surface compensation, "
                f"so a height map here would be applied twice and double the error. "
                f"Turn the machine's levelling off and tick 'compensate anyway', "
                f"or drop the height map from this job."
            )
        if arc_tolerance > 0:
            # Silently emitting arcs at one Z would look like it worked.
            post.warnings.append(
                "Arc fitting is off for this job: an arc holds Z across its "
                "sweep, so it cannot follow a probed surface.")
            arc_tolerance = 0.0
        if post.self_levelling:
            post.warnings.append(
                f"Compensating a {post.name} job by hand. The machine must have "
                f"its own surface levelling switched off.")

    batches = groups if groups else [(p.tool_diameter, paths)]
    first_tool = tool_lookup(batches[0][0]) if (tool_lookup and batches) else None
    post.p = apply_tool(base, first_tool)
    post.header(meta or {})
    post.tool_note(first_tool)

    tool_number = p.tool_number
    for index, (diameter, batch) in enumerate(batches):
        tool = tool_lookup(diameter) if tool_lookup else None
        post.p = apply_tool(base, tool) if tool is not None else replace(
            base, tool_diameter=diameter)
        if index > 0:
            post.tool_change(tool_number + index, diameter, tool)
        post.spindle_on()
        post.feed_move()

        for path in batch:
            coords = list(path.coords)
            if len(coords) < 2:
                continue
            for z in _depth_steps(post.p):
                post.rapid(coords[0][0], coords[0][1])
                # Each depth pass is compensated in its own right: the surface
                # is just as uneven on the second pass as on the first.
                post.plunge(heightmap.start_z(hm, z, coords[0]) if hm else z)
                post.feed_move()
                _emit_path(post, coords, arc_tolerance, hm, z,
                           segment, z_tolerance)
                post.rapid_z(post.p.travel_z)

    post.p = base
    post.footer()
    if warnings is not None:
        warnings.extend(post.finish())
    else:
        post.finish()
    return post.text()


def drills_to_gcode(hits, p: JobParams, dialect: str = "grbl", meta: dict | None = None,
                    warnings: list[str] | None = None, tool_lookup=None) -> str:
    """hits: list of (x, y, diameter), already grouped and ordered.

    Each diameter is looked up as its own drill, so a board with five hole sizes
    gets five sets of feeds rather than one.
    """
    base = p
    post = POSTPROCESSORS.get(dialect, Postprocessor)(p)

    def tool_for(diameter):
        return tool_lookup(diameter) if tool_lookup else None

    # The header names the tool the operator has to fit first, so it has to be
    # the first hole's drill, not whatever diameter the input payload carried.
    first_tool = tool_for(hits[0][2]) if hits else None
    if hits:
        post.p = apply_tool(replace(base, tool_diameter=hits[0][2]), first_tool)
    post.header(meta or {})
    post.tool_note(first_tool)
    post.spindle_on()

    current_dia = None
    tool_number = p.tool_number
    for x, y, dia in hits:
        if current_dia is None:
            current_dia = dia
        elif abs(dia - current_dia) > 1e-6:
            tool_number += 1
            tool = tool_for(dia)
            post.p = apply_tool(replace(base, tool_diameter=dia), tool)
            post.tool_change(tool_number, dia, tool)
            post.spindle_on()
            current_dia = dia
        post.rapid(x, y)
        for z in _depth_steps(post.p):
            post.plunge(z)
            post.rapid_z(post.p.travel_z)

    post.p = base
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
