"""Probed surface heights: reading them, and asking what Z is at a point.

A height map is a set of measured (x, y, z) points, usually probed on a grid
before cutting. Copper-clad board is never flat -- it bows, the tape under it
is uneven, the bed is not square to the spindle -- and an isolation pass cutting
0.1 mm deep will cut air over a high spot and through the substrate over a low
one. Compensation adds the measured deviation to the commanded Z as the cut
moves.

Two decisions worth stating, because both could reasonably have gone the other
way:

**Outside the probed area the nearest edge value is held**, not extrapolated.
Extrapolating a warped surface past the measurements invents depth that was
never measured, and the error grows with distance exactly where there is no
evidence for it. Holding the edge is wrong by a bounded amount instead.

**A full rectangular grid is interpolated bilinearly; anything else falls back
to inverse-distance weighting** over the four nearest points. Probe files are
grids in practice, and a grid can be interpolated exactly and cheaply. Scattered
points are supported so a hand-measured file is not rejected, but the result is
smoother than the data deserves and the plot will show it.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass

#: Values closer together than this are the same grid line, in mm. Probe files
#: carry rounded coordinates, so exact equality is too strict.
GRID_TOL = 1e-6

_COMMENT_STARTS = ("#", ";", "(", "%", "//")


def parse_points(text: str) -> tuple[list[tuple[float, float, float]], list[str]]:
    """Pull (x, y, z) triples out of a text file. Returns points and notes.

    Deliberately tolerant: any line carrying three numbers is a point, whether
    they are separated by spaces, tabs, commas or semicolons. The awkward case
    is a comma, which is a separator in one file and a decimal mark in another,
    so both readings are tried and the one that yields three numbers wins.
    """
    points: list[tuple[float, float, float]] = []
    notes: list[str] = []
    skipped = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(_COMMENT_STARTS):
            continue
        values = _three_numbers(line)
        if values is None:
            skipped += 1
            continue
        points.append(values)

    if skipped:
        notes.append(f"{skipped} line(s) did not hold three numbers and were ignored")

    trimmed = _drop_bcnc_header(points)
    if trimmed is not points:
        notes.append("bCNC probe header recognised and skipped")
        points = trimmed

    return points, notes


def _three_numbers(line: str) -> tuple[float, float, float] | None:
    # Comma as a separator first, since that is the commoner file.
    for pattern, prepared in ((r"[\s,;]+", line), (r"[\s;]+", line.replace(",", "."))):
        fields = [f for f in re.split(pattern, prepared) if f]
        if len(fields) != 3:
            continue
        try:
            return (float(fields[0]), float(fields[1]), float(fields[2]))
        except ValueError:
            continue
    return None


def _drop_bcnc_header(points):
    """bCNC writes two extent lines that read as points. Recognise and drop them.

    The header is `xmin xmax xn` then `ymin ymax yn`, so it is only stripped
    when those counts multiply out to exactly the number of points that follow.
    Anything else is left alone rather than guessed at.
    """
    if len(points) < 3:
        return points
    nx, ny = points[0][2], points[1][2]
    if not (nx.is_integer() and ny.is_integer() and nx >= 2 and ny >= 2):
        return points
    if len(points) - 2 != int(nx) * int(ny):
        return points
    return points[2:]


def _axis(values: list[float]) -> list[float]:
    """Distinct sorted values, merging anything within GRID_TOL."""
    out: list[float] = []
    for value in sorted(values):
        if not out or value - out[-1] > GRID_TOL:
            out.append(value)
    return out


def _index(axis: list[float], value: float) -> int:
    """Position of `value` on an axis built by _axis."""
    i = bisect.bisect_left(axis, value - GRID_TOL)
    return min(i, len(axis) - 1)


@dataclass
class HeightMap:
    """A probed surface, interpolated on demand."""

    points: list[tuple[float, float, float]]
    xs: list[float]
    ys: list[float]
    #: grid[row][col] over ys and xs, or None when the points are scattered.
    grid: list[list[float]] | None
    offset: float = 0.0

    # -- construction ------------------------------------------------------

    @staticmethod
    def from_points(points, zero: str = "raw") -> "HeightMap":
        if len(points) < 2:
            raise ValueError("a height map needs at least two points")

        xs = _axis([p[0] for p in points])
        ys = _axis([p[1] for p in points])
        grid = None

        if len(xs) >= 2 and len(ys) >= 2 and len(xs) * len(ys) == len(points):
            table: list[list[float | None]] = [[None] * len(xs) for _ in ys]
            for x, y, z in points:
                table[_index(ys, y)][_index(xs, x)] = z
            if all(cell is not None for row in table for cell in row):
                grid = [[float(cell) for cell in row] for row in table]

        hm = HeightMap(points=list(points), xs=xs, ys=ys, grid=grid)
        hm.offset = hm._zero_offset(zero)
        return hm

    def _zero_offset(self, zero: str) -> float:
        """What to subtract from every reading.

        A probe file may hold deviations about zero, or absolute heights in
        machine coordinates. Adding the second kind straight onto the cut depth
        would drive the tool somewhere alarming, so the choice is explicit.
        """
        if zero == "mean":
            return sum(p[2] for p in self.points) / len(self.points)
        if zero == "origin":
            # The reading at the job's zero, so compensation is relative to
            # wherever Z0 was actually set.
            saved, self.offset = self.offset, 0.0
            try:
                return self.at(0.0, 0.0)
            finally:
                self.offset = saved
        return 0.0

    # -- queries -----------------------------------------------------------

    @property
    def regular(self) -> bool:
        return self.grid is not None

    def bounds(self) -> tuple[float, float, float, float]:
        return (self.xs[0], self.ys[0], self.xs[-1], self.ys[-1])

    def z_range(self) -> tuple[float, float]:
        zs = [p[2] - self.offset for p in self.points]
        return (min(zs), max(zs))

    def at(self, x: float, y: float) -> float:
        """Height at a point. Outside the probed area, the nearest edge."""
        if self.grid is not None:
            return self._bilinear(x, y) - self.offset
        return self._inverse_distance(x, y) - self.offset

    def _bilinear(self, x: float, y: float) -> float:
        # Clamping here is what holds the edge value outside the probed area:
        # the sample simply walks back onto the boundary.
        x = min(max(x, self.xs[0]), self.xs[-1])
        y = min(max(y, self.ys[0]), self.ys[-1])

        i = max(0, min(bisect.bisect_right(self.xs, x) - 1, len(self.xs) - 2))
        j = max(0, min(bisect.bisect_right(self.ys, y) - 1, len(self.ys) - 2))

        x0, x1 = self.xs[i], self.xs[i + 1]
        y0, y1 = self.ys[j], self.ys[j + 1]
        tx = 0.0 if x1 - x0 < GRID_TOL else (x - x0) / (x1 - x0)
        ty = 0.0 if y1 - y0 < GRID_TOL else (y - y0) / (y1 - y0)

        z00, z10 = self.grid[j][i], self.grid[j][i + 1]
        z01, z11 = self.grid[j + 1][i], self.grid[j + 1][i + 1]
        return ((z00 * (1 - tx) + z10 * tx) * (1 - ty)
                + (z01 * (1 - tx) + z11 * tx) * ty)

    def _inverse_distance(self, x: float, y: float, count: int = 4) -> float:
        """Scattered fallback: inverse square distance over the nearest few.

        Clamping to the bounding box first keeps the behaviour outside the
        probed area the same as the grid case -- the sample is taken at the
        nearest edge rather than drifting toward the overall mean.
        """
        x = min(max(x, self.xs[0]), self.xs[-1])
        y = min(max(y, self.ys[0]), self.ys[-1])

        nearest = sorted(
            ((x - px) ** 2 + (y - py) ** 2, pz) for px, py, pz in self.points
        )[:count]
        if nearest[0][0] < GRID_TOL ** 2:
            return nearest[0][1]
        weights = [(1.0 / d2, z) for d2, z in nearest]
        total = sum(w for w, _ in weights)
        return sum(w * z for w, z in weights) / total

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        low, high = self.z_range()
        x0, y0, x1, y1 = self.bounds()
        shape = (f"{len(self.xs)} x {len(self.ys)} grid" if self.regular
                 else f"{len(self.points)} scattered points")
        return (f"{shape}, {x1 - x0:.1f} x {y1 - y0:.1f} mm, "
                f"Z {low:+.3f} to {high:+.3f} mm (span {high - low:.3f})")


def load(path: str, zero: str = "raw") -> tuple[HeightMap, list[str]]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    points, notes = parse_points(text)
    if not points:
        raise ValueError("no (x, y, z) points found in the file")
    return HeightMap.from_points(points, zero=zero), notes


def start_z(hm: HeightMap, base_z: float, point) -> float:
    """Depth to plunge to at the start of a path."""
    return base_z + hm.at(point[0], point[1])


def compensate(coords, hm: HeightMap, base_z: float,
               segment: float, tolerance: float):
    """Walk a polyline from its second point, yielding (x, y, z_or_None).

    A `z` of None means cut to that XY without a Z word, because the axis is
    already close enough to where it should be.

    Long moves are split before sampling: correcting only at the ends of a
    20 mm segment leaves its middle uncompensated, which is exactly where a
    bowed board deviates most. `segment` caps the distance between samples.

    A split point that needs no Z correction is dropped rather than emitted,
    since it lies on the straight line between the real vertices and the tool
    would pass through it anyway. So the output is the original vertices, plus
    extra points only where Z genuinely has to step -- which is what keeps the
    file from tripling in size.

    `tolerance` is measured against the last Z actually *written*, not the last
    computed, so a long ramp cannot creep away one sub-threshold step at a time.
    """
    if len(coords) < 2:
        return
    step = max(segment, 1e-3)
    last_written = start_z(hm, base_z, coords[0])

    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        span = math.hypot(x1 - x0, y1 - y0)
        pieces = max(1, math.ceil(span / step))
        for k in range(1, pieces + 1):
            t = k / pieces
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            z = base_z + hm.at(x, y)
            moved = abs(z - last_written) > tolerance
            if k == pieces:
                # A real vertex: the XY move has to happen either way.
                yield (x, y, z if moved else None)
                if moved:
                    last_written = z
            elif moved:
                yield (x, y, z)
                last_written = z
