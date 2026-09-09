"""Geometry layer: Gerber/Excellon -> Shapely, and toolpath helpers.

Everything in here works in millimetres and is pure geometry: no GUI, no
G-code, no file format knowledge beyond what gerbonara hands us.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from gerbonara import graphic_primitives as gp
from gerbonara.utils import MM
from shapely import unary_union
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.ops import linemerge

ARC_TOLERANCE = 0.002  # mm, chord error when flattening arcs
BUFFER_SEGMENTS = 16  # quadrant segments for round joins


# --------------------------------------------------------------------------
# Gerber -> Shapely
# --------------------------------------------------------------------------


def _primitive_to_polygon(prim):
    """Convert one gerbonara graphic primitive to a Shapely polygon."""
    if isinstance(prim, gp.Circle):
        if prim.r <= 0:
            return None
        return Point(prim.x, prim.y).buffer(prim.r, quad_segs=BUFFER_SEGMENTS)

    if isinstance(prim, gp.Rectangle):
        if prim.w <= 0 or prim.h <= 0:
            return None
        # Built here rather than via prim.to_arc_poly(), which in the released
        # gerbonara returns an axis-aligned box grown by the rotation instead of
        # a rotated rectangle -- a 2x1 pad at 0.3 rad comes out 71% oversized,
        # and oversized copper puts the isolation cut in the wrong place.
        sin, cos = math.sin(prim.rotation), math.cos(prim.rotation)
        half_w, half_h = prim.w / 2, prim.h / 2
        corners = []
        for dx, dy in ((-half_w, -half_h), (-half_w, half_h),
                       (half_w, half_h), (half_w, -half_h)):
            corners.append((prim.x + dx * cos - dy * sin,
                            prim.y + dx * sin + dy * cos))
        return Polygon(corners)

    if isinstance(prim, gp.Line):
        if math.isclose(prim.x1, prim.x2) and math.isclose(prim.y1, prim.y2):
            if prim.width <= 0:
                return None
            return Point(prim.x1, prim.y1).buffer(prim.width / 2, quad_segs=BUFFER_SEGMENTS)
        line = LineString([(prim.x1, prim.y1), (prim.x2, prim.y2)])
        return line.buffer(max(prim.width, 1e-6) / 2, quad_segs=BUFFER_SEGMENTS)

    if isinstance(prim, gp.Arc):
        pts = gp.approximate_arc(
            prim.cx, prim.cy, prim.x1, prim.y1, prim.x2, prim.y2,
            prim.clockwise, max_error=ARC_TOLERANCE,
        )
        pts = [(x, y) for x, y in pts]
        if len(pts) < 2:
            return None
        return LineString(pts).buffer(max(prim.width, 1e-6) / 2, quad_segs=BUFFER_SEGMENTS)

    if isinstance(prim, gp.ArcPoly):
        return _arcpoly_to_polygon(prim)

    return None


def _arcpoly_to_polygon(poly: gp.ArcPoly):
    """Flatten an arc-sided polygon to a Shapely polygon.

    Walks the outline segment by segment rather than calling gerbonara's own
    ArcPoly.approximate_arcs, which is broken in the released versions: it
    invokes `segments` as a method when it is a property, and its arc branch
    references names that were never bound. Every Gerber with a rectangular
    aperture goes through here, so this path has to work.
    """
    pts: list[tuple[float, float]] = []
    for (x1, y1), (x2, y2), (clockwise, (cx, cy)) in poly.segments:
        if clockwise is None:
            pts.append((x1, y1))
            continue
        arc = list(gp.approximate_arc(cx, cy, x1, y1, x2, y2, clockwise,
                                      max_error=ARC_TOLERANCE))
        if arc:
            arc.pop()  # the arc's end point is the next segment's start
        pts.extend((float(x), float(y)) for x, y in arc)

    if len(pts) < 3:
        return None
    geom = Polygon(pts)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom if not geom.is_empty else None


def gerber_to_polygons(gerber) -> MultiPolygon:
    """Render a gerbonara GerberFile into a single Shapely MultiPolygon.

    Dark primitives add copper, clear primitives remove it. Primitives are
    accumulated in batches so that we only pay for a boolean union when the
    polarity actually flips, which is far faster than folding one at a time.
    """
    result = Polygon()
    batch: list = []
    batch_dark = True

    def flush(current):
        nonlocal batch
        if not batch:
            return current
        merged = unary_union(batch)
        batch = []
        if batch_dark:
            return current.union(merged)
        return current.difference(merged)

    for obj in gerber.objects:
        for prim in obj.to_primitives(unit=MM):
            geom = _primitive_to_polygon(prim)
            if geom is None or geom.is_empty:
                continue
            if prim.polarity_dark != batch_dark:
                result = flush(result)
                batch_dark = prim.polarity_dark
            batch.append(geom)

    result = flush(result)
    return to_multipolygon(result)


def to_multipolygon(geom) -> MultiPolygon:
    if geom is None or geom.is_empty:
        return MultiPolygon()
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty]
        return MultiPolygon(polys)
    return MultiPolygon()


def enclosed_region(geom, midline: bool = True) -> MultiPolygon:
    """The area a drawn profile encloses, rather than the ink of the profile.

    A board outline does not arrive as a board. KiCad plots Edge.Cuts with a
    thin aperture, so what reaches us is a ribbon a few hundredths wide tracing
    where the edge goes -- a polygon whose hole is the entire board. Cut its
    boundary and you cut twice: once round the outside, correctly, and once
    round the inside, a tool width in from the edge, through the middle of the
    part. The second pass has no reason to exist and is the one that ruins the
    board.

    So fill each ribbon, then apply the even-odd rule to what is left: a filled
    shape sitting inside another is a window, not material. Nesting is counted
    rather than assumed, so a slot inside a window inside the board comes out
    right.

    With `midline` the result is pulled in by half the stroke width, because a
    drawn edge means its centreline -- the ribbon straddles the true edge. The
    width is measured from the ribbon itself (area over mean perimeter) rather
    than read from the file, since a profile may mix apertures, as KiCad's does
    when a footprint contributes to the edge. A shape that arrives already
    solid has no ribbon to measure, so it passes through untouched.
    """
    parts = [p for p in to_multipolygon(geom).geoms if not p.is_empty]
    if not parts:
        return MultiPolygon()

    filled = []
    for part in parts:
        outer = Polygon(part.exterior)
        # Ribbon area over mean perimeter: for anything long and thin that is
        # its width, and for a solid shape it is meaningless but unused.
        width = 0.0
        if part.interiors and part.length > 0:
            width = 2 * part.area / part.length
        shrunk = outer.buffer(-width / 2, quad_segs=BUFFER_SEGMENTS) if midline and width else outer
        for piece in to_multipolygon(shrunk).geoms:
            filled.append(piece)
    if not filled:
        return MultiPolygon()

    # Even-odd, by exclusive or: a point inside an odd number of loops is
    # material and an even number is not. Nesting therefore needs no counting
    # and no assumption about which loop is the board -- a slot inside a window
    # inside the board alternates on its own, and loops that merely sit side by
    # side simply add up.
    region = filled[0]
    for shape in filled[1:]:
        region = region.symmetric_difference(shape)
    return to_multipolygon(region)


# --------------------------------------------------------------------------
# Tool models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """A cutting tool. `shape` is either 'flat' or 'v'."""

    diameter: float = 0.2
    shape: str = "flat"
    tip_diameter: float = 0.02
    tip_angle: float = 30.0

    def cut_width(self, depth: float) -> float:
        """Effective width of cut at a given depth of cut (positive mm)."""
        if self.shape != "v":
            return self.diameter
        depth = abs(depth)
        half = math.radians(self.tip_angle) / 2
        width = self.tip_diameter + 2 * depth * math.tan(half)
        return min(width, self.diameter) if self.diameter > 0 else width


# --------------------------------------------------------------------------
# Toolpath helpers
# --------------------------------------------------------------------------


def boundary_lines(geom) -> list[LineString]:
    """All boundary rings of a polygonal geometry, as LineStrings."""
    out: list[LineString] = []
    for poly in to_multipolygon(geom).geoms:
        out.append(LineString(poly.exterior.coords))
        for ring in poly.interiors:
            out.append(LineString(ring.coords))
    return out


def explode_lines(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms]
    if isinstance(geom, GeometryCollection):
        out = []
        for g in geom.geoms:
            out.extend(explode_lines(g))
        return out
    return []


def merge_lines(lines: list[LineString]) -> list[LineString]:
    if not lines:
        return []
    merged = linemerge(MultiLineString([ln for ln in lines if ln.length > 0]))
    return explode_lines(merged)


def order_paths(paths: list[LineString], start=(0.0, 0.0), two_opt_passes: int = 2):
    """Order paths to minimise rapid travel.

    Greedy nearest-neighbour over both endpoints (paths may be reversed),
    followed by a bounded 2-opt improvement sweep. This is the single
    biggest wall-clock win over FlatCAM on dense boards.
    """
    remaining = [p for p in paths if p.length > 0 or len(p.coords) > 1]
    if not remaining:
        return []

    ordered: list[LineString] = []
    cur = start
    pool = list(remaining)
    while pool:
        best_i, best_d, best_rev = 0, float("inf"), False
        for i, path in enumerate(pool):
            coords = path.coords
            a, b = coords[0], coords[-1]
            da = (a[0] - cur[0]) ** 2 + (a[1] - cur[1]) ** 2
            if da < best_d:
                best_i, best_d, best_rev = i, da, False
            closed = abs(b[0] - a[0]) < 1e-9 and abs(b[1] - a[1]) < 1e-9
            if not closed:
                db = (b[0] - cur[0]) ** 2 + (b[1] - cur[1]) ** 2
                if db < best_d:
                    best_i, best_d, best_rev = i, db, True
        path = pool.pop(best_i)
        if best_rev:
            path = LineString(list(path.coords)[::-1])
        ordered.append(path)
        cur = path.coords[-1]

    for _ in range(max(0, two_opt_passes)):
        if not _two_opt_sweep(ordered, start):
            break
    return ordered


def _travel(a, b) -> float:
    return math.dist(a, b)


def _two_opt_sweep(paths: list[LineString], start) -> bool:
    """One pass of segment-reversal improvement. Returns True if improved."""
    n = len(paths)
    if n < 3:
        return False
    improved = False
    ends = [(p.coords[0], p.coords[-1]) for p in paths]

    def entry(i):
        return ends[i][0]

    def exit_(i):
        return ends[i][1]

    for i in range(n - 1):
        prev_exit = start if i == 0 else exit_(i - 1)
        for j in range(i + 1, n):
            next_entry = entry(j + 1) if j + 1 < n else None
            before = _travel(prev_exit, entry(i))
            after = _travel(prev_exit, exit_(j))
            if next_entry is not None:
                before += _travel(exit_(j), next_entry)
                after += _travel(entry(i), next_entry)
            if after + 1e-9 < before:
                chunk = paths[i:j + 1]
                chunk.reverse()
                chunk = [LineString(list(p.coords)[::-1]) for p in chunk]
                paths[i:j + 1] = chunk
                ends[i:j + 1] = [(p.coords[0], p.coords[-1]) for p in chunk]
                improved = True
                break
    return improved


def travel_length(paths: list[LineString], start=(0.0, 0.0)) -> float:
    """Total rapid (non-cutting) distance for an ordered path list."""
    total = 0.0
    cur = start
    for p in paths:
        total += _travel(cur, p.coords[0])
        cur = p.coords[-1]
    return total


def cut_length(paths: list[LineString]) -> float:
    return sum(p.length for p in paths)


def bounds_box(geom, margin: float = 0.0):
    if geom is None or geom.is_empty:
        return None
    minx, miny, maxx, maxy = geom.bounds
    return box(minx - margin, miny - margin, maxx + margin, maxy + margin)


def hatch_fill(region, step: float, angle_deg: float = 0.0) -> list[LineString]:
    """Parallel-line fill of a polygonal region."""
    region = to_multipolygon(region)
    if region.is_empty or step <= 0:
        return []
    minx, miny, maxx, maxy = region.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    diag = math.hypot(maxx - minx, maxy - miny) / 2 + step
    ang = math.radians(angle_deg)
    dx, dy = math.cos(ang), math.sin(ang)
    nx, ny = -dy, dx

    lines = []
    offset = -diag
    while offset <= diag:
        ox, oy = cx + nx * offset, cy + ny * offset
        seg = LineString([(ox - dx * diag, oy - dy * diag), (ox + dx * diag, oy + dy * diag)])
        clipped = seg.intersection(region)
        lines.extend(explode_lines(clipped))
        offset += step
    return lines


def concentric_fill(region, step: float, max_rings: int = 400) -> list[LineString]:
    """Inward offset ('follow') fill of a polygonal region."""
    region = to_multipolygon(region)
    if region.is_empty or step <= 0:
        return []
    lines: list[LineString] = []
    current = region
    for _ in range(max_rings):
        if current.is_empty:
            break
        lines.extend(boundary_lines(current))
        current = to_multipolygon(current.buffer(-step, quad_segs=BUFFER_SEGMENTS))
    return lines
