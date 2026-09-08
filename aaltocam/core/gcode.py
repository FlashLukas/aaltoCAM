"""G-code generation.

Postprocessors are small classes with one hook per machine event. Adding a
dialect means subclassing and overriding two or three methods -- no changes
anywhere else in the program.

All output is metric, absolute, linear-only (G0/G1). Arcs are already
flattened in the geometry layer, so no dialect has to support G2/G3.

Note on height compensation: nothing here modulates Z along a cut. If your
controller applies its own surface compensation (Wegstr, bCNC autolevel,
Candle height map), that stays correct. Compensating twice is a real hazard.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    def __init__(self, p: JobParams):
        self.p = p
        self.lines: list[str] = []

    # -- helpers -----------------------------------------------------------

    def emit(self, line: str):
        if line:
            self.lines.append(line)

    def n(self, value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    # -- hooks -------------------------------------------------------------

    def header(self, meta: dict):
        self.emit(f"({meta.get('title', 'aaltocam job')})")
        if self.p.tool_diameter:
            self.emit(f"(tool {self.p.tool_number}: dia {self.n(self.p.tool_diameter)} mm)")
        self.emit("G21 (mm)")
        self.emit("G90 (absolute)")
        self.emit("G94 (units per minute)")
        for line in self.p.start_code.splitlines():
            self.emit(line.strip())
        self.emit(f"G0 Z{self.n(self.p.travel_z)}")

    def spindle_on(self):
        self.emit(f"M3 S{int(self.p.spindle)}")
        if self.p.dwell > 0:
            self.emit(f"G4 P{self.n(self.p.dwell)}")

    def spindle_off(self):
        self.emit("M5")

    def rapid(self, x, y):
        self.emit(f"G0 X{self.n(x)} Y{self.n(y)}")

    def rapid_z(self, z):
        self.emit(f"G0 Z{self.n(z)}")

    def plunge(self, z):
        self.emit(f"G1 Z{self.n(z)} F{self.n(self.p.feed_z)}")

    def cut(self, x, y):
        self.emit(f"G1 X{self.n(x)} Y{self.n(y)}")

    def feed_move(self):
        self.emit(f"F{self.n(self.p.feed_xy)}")

    def tool_change(self, number: int, diameter: float):
        self.emit(f"G0 Z{self.n(self.p.toolchange_z)}")
        self.emit("M5")
        self.emit(f"(change to tool {number}, dia {self.n(diameter)} mm)")
        self.emit("M0")

    def footer(self):
        self.emit(f"G0 Z{self.n(self.p.travel_z)}")
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
    """Conservative dialect for controllers that run their own surface
    compensation and accept only the simplest motion vocabulary.

    Flat Z, no tool-length offsets, no M6, pauses with M0 instead. Verify the
    first job against your controller before trusting it with a real board.
    """

    name = "wegstr"
    description = "Minimal dialect for Wegstr-style controllers (M0 pauses, flat Z)."

    def header(self, meta):
        self.emit(f"({meta.get('title', 'aaltocam job')})")
        self.emit("G21")
        self.emit("G90")
        for line in self.p.start_code.splitlines():
            self.emit(line.strip())
        self.emit(f"G0 Z{self.n(self.p.travel_z)}")

    def tool_change(self, number, diameter):
        self.emit(f"G0 Z{self.n(self.p.toolchange_z)}")
        self.emit("M5")
        self.emit(f"(insert tool dia {self.n(diameter)} mm, then resume)")
        self.emit("M0")


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
                   groups=None) -> str:
    """Turn ordered LineStrings into G-code.

    `groups` is an optional list of (tool_diameter, paths) from a rest-machining
    operation. When more than one tool is present, a tool change is emitted
    between groups.
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
    return post.text()


def drills_to_gcode(hits, p: JobParams, dialect: str = "grbl", meta: dict | None = None) -> str:
    """hits: list of (x, y, diameter), already grouped and ordered."""
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
    return post.text()


def estimate_minutes(source, p: JobParams, rapid_rate: float = 2000.0) -> float:
    """Rough run time: cutting, rapids, plunges and retracts.

    Ignores acceleration, so it under-estimates on boards made of very short
    segments. Useful for comparing two parameter choices, not for promising
    anyone a finish time.
    """
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

    minutes += (cut * passes) / max(p.feed_xy, 1e-6)
    minutes += travel / max(rapid_rate, 1e-6)

    # Every path start plunges to depth and retracts to travel height once
    # per depth pass.
    plunge = abs(p.cut_z) + p.travel_z
    minutes += starts * passes * plunge / max(p.feed_z, 1e-6) / 2
    minutes += starts * passes * p.travel_z / max(rapid_rate, 1e-6)
    return minutes
