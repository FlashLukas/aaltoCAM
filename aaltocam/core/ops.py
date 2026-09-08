"""The operation library.

Each operation is a pure function of (parameters, input payloads). Nothing
here touches the GUI, and nothing mutates its inputs -- which is what makes
re-evaluating a changed parameter safe and cheap.
"""

from __future__ import annotations

import os

from gerbonara import ExcellonFile, GerberFile
from shapely import STRtree, affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from . import gcode as gc
from . import geometry as geo
from . import tools as toollib
from .graph import Payload, register
from .params import B, C, F, I, P, S, SH, T, normalize_shapes, normalize_tools

MILLING = ["conventional", "climb"]


def _resolve(doc, path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path) or not doc.base_dir:
        return path
    return os.path.join(doc.base_dir, path)


def selected_tool(node):
    """The library tool a node has been pointed at, if any."""
    return toollib.get(str(node.params.get("tool", "") or ""))


def _tool_width(node) -> float:
    """Cut width, from the library tool when one is selected.

    A selected tool wins over the typed diameter, so the physical cutter is
    described in exactly one place.
    """
    chosen = selected_tool(node)
    if chosen is not None:
        tool = geo.Tool(
            diameter=chosen.diameter,
            shape=chosen.shape,
            tip_diameter=chosen.tip_diameter or 0.02,
            tip_angle=chosen.tip_angle or 30.0,
        )
    else:
        tool = geo.Tool(
            diameter=float(node.params.get("tool_dia", 0.2)),
            shape=node.params.get("tool_shape", "flat"),
            tip_diameter=float(node.params.get("tip_dia", 0.02)),
            tip_angle=float(node.params.get("tip_angle", 30.0)),
        )
    return tool.cut_width(float(node.params.get("cut_depth", 0.1)))


def _tool_dia(node, key: str = "tool_dia") -> float:
    """Plain diameter for the ops that do not model a V-bit."""
    chosen = selected_tool(node)
    return chosen.diameter if chosen is not None else float(node.params.get(key, 1.0))


# Populate the shared choice list before the descriptors are built. CHOICES is
# mutated in place on reload, so every descriptor keeps pointing at it.
toollib.library()

TOOL_CHOICE = C("tool", "Tool", "", toollib.CHOICES, group="Tool",
                help="Pick a cutter from the tool library. Its diameter replaces "
                     "the one typed here, and the CNC job can take its feeds.")


TOOL_PARAMS = [
    TOOL_CHOICE,
    F("tool_dia", "Tool diameter", 0.2, unit="mm", minimum=0.001, maximum=20, group="Tool",
      help="For a V-bit this is the maximum usable width."),
    C("tool_shape", "Tool shape", "flat", ["flat", "v"], group="Tool"),
    F("tip_dia", "V tip diameter", 0.02, unit="mm", minimum=0.0, maximum=2, step=0.01,
      group="Tool", depends_on="tool_shape:v"),
    F("tip_angle", "V tip angle", 30.0, unit="deg", minimum=1, maximum=180, step=1,
      decimals=1, group="Tool", depends_on="tool_shape:v"),
    F("cut_depth", "Depth of cut", 0.1, unit="mm", minimum=0.001, maximum=5, step=0.01,
      group="Tool",
      help="Used to derive the V-bit cut width. Keep this equal to the CNC job's cut Z."),
]


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@register(
    "load_gerber", "Gerber file",
    [P("path", "File", "", help="Copper layer, RS-274X."),
     B("invert", "Invert polarity", False,
       help="Treat copper as empty space. Useful for negative-plane layers.")],
    inputs=[], output="copper", category="Source",
)
def op_load_gerber(doc, node):
    path = _resolve(doc, node.params["path"])
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Gerber not found: {path or '(no file selected)'}")
    gerber = GerberFile.open(path)
    polys = geo.gerber_to_polygons(gerber)
    if node.params.get("invert") and not polys.is_empty:
        polys = geo.to_multipolygon(geo.bounds_box(polys, 1.0).difference(polys))
    return Payload("copper", polys, {"source": os.path.basename(path)})


@register(
    "load_excellon", "Excellon drills",
    [P("path", "File", ""),
     F("min_dia", "Minimum diameter", 0.0, unit="mm", minimum=0, maximum=20),
     F("max_dia", "Maximum diameter", 10.0, unit="mm", minimum=0, maximum=20)],
    inputs=[], output="drills", category="Source",
)
def op_load_excellon(doc, node):
    path = _resolve(doc, node.params["path"])
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Excellon not found: {path or '(no file selected)'}")
    excellon = ExcellonFile.open(path)
    lo = float(node.params["min_dia"])
    hi = float(node.params["max_dia"])
    hits = []
    for obj in excellon.drills():
        dia = float(obj.tool.diameter) if obj.tool else 0.0
        if not (lo - 1e-9 <= dia <= hi + 1e-9):
            continue
        hits.append((float(obj.x), float(obj.y), dia))
    hits.sort(key=lambda h: (round(h[2], 4), h[1], h[0]))
    slots = sum(1 for _ in excellon.slots())
    return Payload("drills", hits, {"source": os.path.basename(path), "slots": slots})


# --------------------------------------------------------------------------
# Transforms (work on any payload kind)
# --------------------------------------------------------------------------


def _apply_affine(payload: Payload, fn) -> Payload:
    if payload.kind in ("copper", "region"):
        return Payload(payload.kind, geo.to_multipolygon(fn(payload.data)), dict(payload.meta))
    if payload.kind == "paths":
        return Payload("paths", [fn(p) for p in payload.data], dict(payload.meta))
    if payload.kind == "drills":
        moved = []
        for x, y, dia in payload.data:
            pt = fn(LineString([(x, y), (x + 1e-6, y)]))
            nx, ny = pt.coords[0]
            moved.append((nx, ny, dia))
        return Payload("drills", moved, dict(payload.meta))
    raise TypeError(f"cannot transform payload of kind {payload.kind}")


def as_polygons(payload: Payload):
    """Polygons from any geometry payload. Regions and copper are both
    polygonal, so a masking input accepts either."""
    if payload is None:
        return None
    if payload.kind in ("copper", "region"):
        return payload.data
    raise TypeError(f"expected copper or region, got {payload.kind}")


def _masked_area(base, region_payload, mode: str):
    """Restrict `base` to (or exclude) a region. Returns base untouched when
    no region is connected."""
    region = as_polygons(region_payload)
    if region is None or region.is_empty:
        return base
    if mode == "outside":
        return geo.to_multipolygon(base.difference(region))
    return geo.to_multipolygon(base.intersection(region))


@register(
    "region", "Region",
    [SH("shapes", "Shapes", [], group="Shapes",
        help="Draw rectangles and polygons on the board view, or edit the "
             "coordinates in the project file."),
     F("expand", "Grow / shrink", 0.0, unit="mm", minimum=-50, maximum=50, step=0.1,
       group="Shapes", help="Positive grows the region, negative shrinks it."),
     B("subtract", "Subtract from reference", False, group="Shapes",
       help="With a reference connected, use everything in the reference "
            "bounding box except the drawn shapes.")],
    inputs=["?any"], output="region", category="Source",
    input_labels=["Reference"],
)
def op_region(doc, node, reference: Payload = None):
    """A hand-drawn area, used to limit where an operation is allowed to cut."""
    shapes = normalize_shapes(node.params.get("shapes"))
    pieces = []
    for shape in shapes:
        points = shape["points"]
        if shape["type"] == "rect":
            (x1, y1), (x2, y2) = points[0], points[1]
            pieces.append(box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        else:
            poly = Polygon(points)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                pieces.append(poly)

    area = geo.to_multipolygon(unary_union(pieces)) if pieces else MultiPolygon()

    if node.params.get("subtract") and reference is not None:
        bounds = payload_bounds(reference)
        if bounds is not None:
            area = geo.to_multipolygon(box(*bounds).difference(area))

    expand = float(node.params.get("expand", 0.0))
    if expand and not area.is_empty:
        area = geo.to_multipolygon(area.buffer(expand, quad_segs=geo.BUFFER_SEGMENTS))

    return Payload("region", area, {"shapes": len(shapes), "area": area.area})


def payload_bounds(payload: Payload):
    """Bounding box of any payload, in mm."""
    if payload is None:
        return None
    if payload.kind in ("copper", "region"):
        return None if payload.data.is_empty else payload.data.bounds
    if payload.kind == "paths":
        if not payload.data:
            return None
        return unary_union(payload.data).bounds
    if payload.kind == "drills":
        if not payload.data:
            return None
        xs = [h[0] for h in payload.data]
        ys = [h[1] for h in payload.data]
        radii = [h[2] / 2 for h in payload.data]
        return (min(x - r for x, r in zip(xs, radii)),
                min(y - r for y, r in zip(ys, radii)),
                max(x + r for x, r in zip(xs, radii)),
                max(y + r for y, r in zip(ys, radii)))
    return None


ALIGN_CHOICES = ["none", "bottom left", "centre", "top left", "bottom right"]


def _align_delta(bounds, mode: str):
    if bounds is None or mode == "none":
        return 0.0, 0.0
    minx, miny, maxx, maxy = bounds
    if mode == "bottom left":
        return -minx, -miny
    if mode == "centre":
        return -(minx + maxx) / 2, -(miny + maxy) / 2
    if mode == "top left":
        return -minx, -maxy
    if mode == "bottom right":
        return -maxx, -miny
    return 0.0, 0.0


@register(
    "transform", "Transform",
    [C("align", "Move to origin", "none", ALIGN_CHOICES, group="Position",
       help="Put the chosen corner of the reference bounding box on X0 Y0."),
     F("offset_x", "Then offset X", 0.0, unit="mm", minimum=-1000, maximum=1000,
       group="Position"),
     F("offset_y", "Then offset Y", 0.0, unit="mm", minimum=-1000, maximum=1000,
       group="Position"),
     C("mirror", "Mirror", "none", ["none", "x", "y"], group="Orientation",
       help="Mirrored about the reference centre. Use 'y' for the bottom side."),
     F("rotate", "Rotate", 0.0, unit="deg", minimum=-360, maximum=360, step=1, decimals=2,
       group="Orientation"),
     F("scale", "Scale", 1.0, minimum=0.01, maximum=100, step=0.01, group="Orientation")],
    inputs=["any", "?any"], output="any", category="Edit",
    input_labels=["Geometry", "Reference"],
)
def op_transform(doc, node, source: Payload, reference: Payload = None):
    """Mirror, rotate, scale and position a layer.

    The optional reference input is what makes double-sided work survivable:
    every layer transformed against the same reference moves identically,
    instead of each one pivoting about its own bounding box and quietly
    drifting out of registration.
    """
    mirror = node.params["mirror"]
    rot = float(node.params["rotate"])
    scale = float(node.params["scale"])
    ox, oy = float(node.params["offset_x"]), float(node.params["offset_y"])
    align = node.params.get("align", "none")

    ref_bounds = payload_bounds(reference if reference is not None else source)
    if ref_bounds is None:
        anchor = (0.0, 0.0)
    else:
        anchor = ((ref_bounds[0] + ref_bounds[2]) / 2, (ref_bounds[1] + ref_bounds[3]) / 2)

    def orient(g):
        if mirror == "x":
            g = affinity.scale(g, xfact=1, yfact=-1, origin=anchor)
        elif mirror == "y":
            g = affinity.scale(g, xfact=-1, yfact=1, origin=anchor)
        if scale != 1.0:
            g = affinity.scale(g, xfact=scale, yfact=scale, origin=anchor)
        if rot:
            g = affinity.rotate(g, rot, origin=anchor)
        return g

    # Work out the alignment shift from the reference *after* orienting it,
    # so rotation and mirroring do not leave the result off-origin.
    dx, dy = 0.0, 0.0
    if align != "none" and ref_bounds is not None:
        ref_box = orient(box(*ref_bounds))
        dx, dy = _align_delta(ref_box.bounds, align)

    def fn(g):
        return affinity.translate(orient(g), dx + ox, dy + oy)

    result = _apply_affine(source, fn)
    result.kind = source.kind
    result.meta = dict(source.meta)
    return result


@register(
    "panelize", "Panelize",
    [I("columns", "Columns", 2, minimum=1, maximum=50),
     I("rows", "Rows", 1, minimum=1, maximum=50),
     F("spacing_x", "Spacing X", 5.0, unit="mm", minimum=0, maximum=500),
     F("spacing_y", "Spacing Y", 5.0, unit="mm", minimum=0, maximum=500)],
    inputs=["any"], output="any", category="Edit",
)
def op_panelize(doc, node, source: Payload):
    cols = int(node.params["columns"])
    rows = int(node.params["rows"])
    sx = float(node.params["spacing_x"])
    sy = float(node.params["spacing_y"])

    if source.kind in ("copper", "region"):
        bounds = source.data.bounds if not source.data.is_empty else (0, 0, 0, 0)
    elif source.kind == "paths":
        bounds = unary_union(source.data).bounds if source.data else (0, 0, 0, 0)
    else:
        xs = [h[0] for h in source.data] or [0]
        ys = [h[1] for h in source.data] or [0]
        bounds = (min(xs), min(ys), max(xs), max(ys))
    width = bounds[2] - bounds[0] + sx
    height = bounds[3] - bounds[1] + sy

    if source.kind == "drills":
        out = []
        for r in range(rows):
            for c in range(cols):
                for x, y, dia in source.data:
                    out.append((x + c * width, y + r * height, dia))
        return Payload("drills", out, dict(source.meta))

    pieces = []
    for r in range(rows):
        for c in range(cols):
            dx, dy = c * width, r * height
            if source.kind in ("copper", "region"):
                pieces.append(affinity.translate(source.data, dx, dy))
            else:
                pieces.extend(affinity.translate(p, dx, dy) for p in source.data)

    if source.kind in ("copper", "region"):
        return Payload(source.kind, geo.to_multipolygon(unary_union(pieces)), dict(source.meta))
    return Payload("paths", pieces, dict(source.meta))


# --------------------------------------------------------------------------
# CAM operations
# --------------------------------------------------------------------------


@register(
    "isolate", "Isolation routing",
    TOOL_PARAMS + [
        I("passes", "Passes", 1, minimum=1, maximum=40, group="Isolation"),
        F("overlap", "Pass overlap", 0.15, minimum=0.0, maximum=0.95, step=0.05,
          group="Isolation", help="Fraction of the cut width shared between passes."),
        C("milling_type", "Milling type", "conventional", MILLING, group="Isolation"),
        B("follow", "Follow trace centreline", False, group="Isolation",
          help="Ignore offsets and cut along the copper centreline instead."),
        F("simplify", "Simplify tolerance", 0.001, unit="mm", minimum=0.0, maximum=0.1,
          step=0.001, group="Isolation"),
        B("optimize", "Optimise travel order", True, group="Isolation"),
     C("region_mode", "Region", "inside", ["inside", "outside"], group="Region",
       help="With a region connected: cut only inside it, or everywhere except it."),
    ],
    inputs=["copper", "?any"], output="paths", category="CAM",
    input_labels=["Copper", "Region"],
)
def op_isolate(doc, node, copper: Payload, region: Payload = None):
    polys = copper.data
    if polys.is_empty:
        return Payload("paths", [], {"tool_diameter": _tool_width(node)})

    width = _tool_width(node)
    lines: list[LineString] = []

    if node.params.get("follow"):
        lines = geo.boundary_lines(polys)
    else:
        passes = int(node.params["passes"])
        overlap = float(node.params["overlap"])
        step = width * (1.0 - overlap)
        for i in range(passes):
            offset = width / 2 + i * step
            ring = polys.buffer(offset, quad_segs=geo.BUFFER_SEGMENTS)
            lines.extend(geo.boundary_lines(ring))

    mask = as_polygons(region)
    if mask is not None and not mask.is_empty:
        if node.params.get("region_mode", "inside") == "outside":
            clipped = [ln.difference(mask) for ln in lines]
        else:
            clipped = [ln.intersection(mask) for ln in lines]
        lines = [seg for c in clipped for seg in geo.explode_lines(c)]

    tol = float(node.params.get("simplify", 0.0))
    if tol > 0:
        lines = [ln.simplify(tol, preserve_topology=False) for ln in lines]
    lines = [ln for ln in lines if ln.length > 1e-9]

    if node.params.get("milling_type") == "climb":
        lines = [LineString(list(ln.coords)[::-1]) for ln in lines]

    if node.params.get("optimize", True):
        lines = geo.order_paths(lines)

    return Payload("paths", lines, {
        "tool_diameter": width,
        "tool": node.params.get("tool", ""),
        "cut_length": geo.cut_length(lines),
        "travel_length": geo.travel_length(lines),
    })


@register(
    "clear_copper", "Clear copper",
    [T("tools", "Tools", [{"dia": 1.0, "shape": "flat", "tip_dia": 0.02, "tip_angle": 30.0},
                          {"dia": 0.4, "shape": "flat", "tip_dia": 0.02, "tip_angle": 30.0}],
       group="Tools",
       help="Used largest first. Each tool only cuts what the previous ones "
            "could not reach (rest machining)."),
     F("cut_depth", "Depth of cut", 0.1, unit="mm", minimum=0.001, maximum=5, step=0.01,
       group="Tools", help="Used to derive V-bit cut widths. Match the CNC job's cut Z."),
     F("overlap", "Pass overlap", 0.25, minimum=0.0, maximum=0.95, step=0.05,
       group="Clearing"),
     F("clearance", "Keep-out around copper", 0.05, unit="mm", minimum=0.0, maximum=5,
       step=0.01, group="Clearing",
       help="Extra gap left between the cleared area and the copper."),
     F("margin", "Board margin", 1.0, unit="mm", minimum=-10, maximum=50, step=0.1,
       group="Clearing"),
     C("method", "Fill method", "concentric", ["concentric", "lines"], group="Clearing"),
     F("line_angle", "Line angle", 45.0, unit="deg", minimum=0, maximum=180, step=5,
       decimals=1, group="Clearing", depends_on="method:lines"),
     B("optimize", "Optimise travel order", True, group="Clearing"),
     C("region_mode", "Region", "inside", ["inside", "outside"], group="Region",
       help="With a region connected: cut only inside it, or everywhere except it."),
    ],
    inputs=["copper", "?any"], output="paths", category="CAM",
    input_labels=["Copper", "Region"],
)
def op_clear_copper(doc, node, copper: Payload, region: Payload = None):
    polys = copper.data
    tools = normalize_tools(node.params.get("tools"))
    depth = float(node.params.get("cut_depth", 0.1))
    widths = [geo.Tool(t["dia"], t["shape"], t["tip_dia"], t["tip_angle"]).cut_width(depth)
              for t in tools]
    if polys.is_empty:
        return Payload("paths", [], {"tool_diameter": widths[0], "groups": []})

    margin = float(node.params["margin"])
    clearance = float(node.params["clearance"])
    overlap = float(node.params["overlap"])
    area = _masked_area(geo.bounds_box(polys, margin), region,
                        node.params.get("region_mode", "inside"))
    remaining = geo.to_multipolygon(
        area.difference(polys.buffer(clearance, quad_segs=geo.BUFFER_SEGMENTS)))

    groups: list[tuple[float, list]] = []
    flat: list[LineString] = []
    for width in widths:
        if remaining.is_empty:
            break
        radius = width / 2
        workable = geo.to_multipolygon(
            remaining.buffer(-radius, quad_segs=geo.BUFFER_SEGMENTS))
        if workable.is_empty:
            continue

        step = max(width * (1.0 - overlap), 1e-4)
        if node.params["method"] == "lines":
            lines = geo.hatch_fill(workable, step, float(node.params["line_angle"]))
            lines = geo.merge_lines(lines) or lines
            lines.extend(geo.boundary_lines(workable))
        else:
            lines = geo.concentric_fill(workable, step)
        lines = [ln for ln in lines if ln.length > 1e-9]
        if node.params.get("optimize", True):
            lines = geo.order_paths(lines)
        if lines:
            groups.append((width, lines))
            flat.extend(lines)

        # Whatever this tool physically removed is no longer available to
        # the next, smaller one.
        removed = workable.buffer(radius, quad_segs=geo.BUFFER_SEGMENTS)
        remaining = geo.to_multipolygon(remaining.difference(removed))

    return Payload("paths", flat, {
        "tool_diameter": widths[0],
        "groups": groups,
        "cut_length": geo.cut_length(flat),
        "travel_length": geo.travel_length(flat),
        "uncleared_area": remaining.area,
    })


CUT_SIDES = ["outside", "on path", "inside"]
SIDE_SIGN = {"outside": 1.0, "on path": 0.0, "inside": -1.0}

GAP_CHOICES = ["none", "2lr", "2tb", "4", "8"]


def _tab_blocker(bounds, gaps: str, gap_size: float, thickness: float):
    """Rectangles that interrupt a contour, leaving holding tabs."""
    if gaps == "none":
        return None
    minx, miny, maxx, maxy = bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    g = gap_size / 2
    t = max(thickness, 1e-3)
    cuts = []
    if gaps in ("2lr", "4", "8"):
        cuts += [box(minx - t, cy - g, minx + t, cy + g),
                 box(maxx - t, cy - g, maxx + t, cy + g)]
    if gaps in ("2tb", "4", "8"):
        cuts += [box(cx - g, miny - t, cx + g, miny + t),
                 box(cx - g, maxy - t, cx + g, maxy + t)]
    if gaps == "8":
        qx1, qx2 = (minx + cx) / 2, (cx + maxx) / 2
        qy1, qy2 = (miny + cy) / 2, (cy + maxy) / 2
        cuts += [box(minx - t, qy1 - g, minx + t, qy1 + g),
                 box(minx - t, qy2 - g, minx + t, qy2 + g),
                 box(maxx - t, qy1 - g, maxx + t, qy1 + g),
                 box(maxx - t, qy2 - g, maxx + t, qy2 + g),
                 box(qx1 - g, miny - t, qx1 + g, miny + t),
                 box(qx2 - g, miny - t, qx2 + g, miny + t),
                 box(qx1 - g, maxy - t, qx1 + g, maxy + t),
                 box(qx2 - g, maxy - t, qx2 + g, maxy + t)]
    return unary_union(cuts) if cuts else None


def _contour_paths(polys, node, dia: float):
    """Offset each polygon to the requested side of the tool and cut its
    outline, one contour per polygon so holding tabs land per opening.

    Offsetting a whole polygon handles its holes correctly for free: a
    negative offset shrinks the outline and grows any hole inside it, which
    is exactly what cutting an opening to size requires.
    """
    side = node.params.get("side", "outside")
    sign = SIDE_SIGN.get(side, 1.0)
    margin = float(node.params.get("margin", 0.0))
    offset = sign * (dia / 2 + margin)
    gaps = node.params.get("gaps", "none")
    gap_size = float(node.params.get("gap_size", 2.0))

    lines: list[LineString] = []
    cut = collapsed = untabbed = 0
    for poly in geo.to_multipolygon(polys).geoms:
        geom = poly.buffer(offset, quad_segs=geo.BUFFER_SEGMENTS) if offset else poly
        geom = geo.to_multipolygon(geom)
        if geom.is_empty:
            collapsed += 1  # the tool does not fit inside this opening
            continue
        segs = geo.boundary_lines(geom)
        blocker = _tab_blocker(geom.bounds, gaps, gap_size, dia)
        if blocker is not None:
            tabbed = [seg for ln in segs for seg in geo.explode_lines(ln.difference(blocker))]
            tabbed = [seg for seg in tabbed if seg.length > 1e-9]
            if tabbed:
                segs = tabbed
            else:
                # The tabs swallowed the whole contour. Cutting it fully is
                # better than silently not cutting it; the slug is small.
                untabbed += 1
        lines.extend(seg for seg in segs if seg.length > 1e-9)
        cut += 1

    if node.params.get("milling_type") == "climb":
        lines = [LineString(list(ln.coords)[::-1]) for ln in lines]
    if node.params.get("optimize", True):
        lines = geo.order_paths(lines)

    return lines, cut, collapsed, untabbed


SIDE_PARAM = C(
    "side", "Tool side", "outside", CUT_SIDES, group="Cutout",
    help="Where the tool runs relative to the outline. 'outside' keeps the "
         "part full size, 'inside' keeps an opening full size, 'on path' "
         "puts the tool centre on the line and ignores the margin.")


@register(
    "cutout", "Board cutout",
    [TOOL_CHOICE,
     F("tool_dia", "Tool diameter", 1.0, unit="mm", minimum=0.05, maximum=10, group="Tool"),
     C("shape", "Outline shape", "rectangle",
       ["rectangle", "hull", "outline input"], group="Cutout",
       help="'outline input' uses the connected Edge.Cuts layer or drawn region."),
     SIDE_PARAM,
     F("margin", "Margin", 0.5, unit="mm", minimum=-10, maximum=50, step=0.1, group="Cutout"),
     C("gaps", "Holding tabs", "4", GAP_CHOICES, group="Cutout"),
     F("gap_size", "Tab width", 2.0, unit="mm", minimum=0.1, maximum=20, step=0.1,
       group="Cutout"),
     C("milling_type", "Milling type", "conventional", MILLING, group="Cutout"),
     B("optimize", "Optimise travel order", True, group="Cutout")],
    inputs=["copper", "?any"], output="paths", category="CAM",
    input_labels=["Copper", "Outline"],
)
def op_cutout(doc, node, copper: Payload, outline_input: Payload = None):
    polys = copper.data
    dia = _tool_dia(node)
    if polys.is_empty:
        return Payload("paths", [], {"tool_diameter": dia})

    shape = node.params["shape"]
    given = as_polygons(outline_input)
    if shape == "outline input" and given is not None and not given.is_empty:
        base = given
    elif shape == "hull":
        base = polys.convex_hull
    else:
        base = geo.bounds_box(polys, 0.0)

    lines, cut, collapsed, untabbed = _contour_paths(base, node, dia)
    return Payload("paths", lines, {
        "tool_diameter": dia,
        "tool": node.params.get("tool", ""),
        "contours": cut,
        "no_room_for_tabs": untabbed,
        "too_small": collapsed,
        "cut_length": geo.cut_length(lines),
        "travel_length": geo.travel_length(lines),
    })


@register(
    "internal_cutout", "Internal cutout",
    [TOOL_CHOICE,
     F("tool_dia", "Tool diameter", 1.0, unit="mm", minimum=0.05, maximum=10, group="Tool"),
     SIDE_PARAM,
     F("margin", "Margin", 0.0, unit="mm", minimum=-10, maximum=50, step=0.05,
       group="Cutout", help="Added in the compensation direction: positive makes "
                            "the opening larger, negative smaller."),
     C("gaps", "Holding tabs", "none", GAP_CHOICES, group="Cutout",
       help="Tabs are placed per opening, so a loose slug stays put until you "
            "break it out."),
     F("gap_size", "Tab width", 1.0, unit="mm", minimum=0.1, maximum=20, step=0.1,
       group="Cutout"),
     C("milling_type", "Milling type", "conventional", MILLING, group="Cutout"),
     B("optimize", "Optimise travel order", True, group="Cutout")],
    inputs=["any"], output="paths", category="CAM",
    input_labels=["Openings"],
)
def op_internal_cutout(doc, node, openings: Payload):
    """Slots, windows and mounting cutouts inside the board.

    Each polygon of the input is one opening. With 'inside' compensation the
    finished hole matches what you drew; openings too small for the tool are
    reported rather than skipped silently.
    """
    dia = _tool_dia(node)
    polys = as_polygons(openings)
    if polys is None or polys.is_empty:
        return Payload("paths", [], {"tool_diameter": dia})

    lines, cut, collapsed, untabbed = _contour_paths(polys, node, dia)
    return Payload("paths", lines, {
        "tool_diameter": dia,
        "tool": node.params.get("tool", ""),
        "openings": cut,
        "no_room_for_tabs": untabbed,
        "too_small": collapsed,
        "cut_length": geo.cut_length(lines),
        "travel_length": geo.travel_length(lines),
    })


@register(
    "drill_holes", "Drill holes",
    [F("min_dia", "Minimum diameter", 0.0, unit="mm", minimum=0, maximum=20),
     F("max_dia", "Maximum diameter", 10.0, unit="mm", minimum=0, maximum=20),
     B("split_large", "Send large holes to milling", True,
       help="Holes at or above the threshold are dropped here so a Mill holes "
            "node can cut them instead."),
     F("mill_threshold", "Mill above", 2.0, unit="mm", minimum=0.05, maximum=20, step=0.1,
       depends_on="split_large"),
     B("optimize", "Optimise travel order", True)],
    inputs=["drills"], output="drills", category="CAM",
)
def op_drill_holes(doc, node, drills: Payload):
    lo, hi = float(node.params["min_dia"]), float(node.params["max_dia"])
    hits = [h for h in drills.data if lo - 1e-9 <= h[2] <= hi + 1e-9]
    milled = 0
    if node.params.get("split_large", True):
        threshold = float(node.params["mill_threshold"])
        kept = [h for h in hits if h[2] < threshold - 1e-9]
        milled = len(hits) - len(kept)
        hits = kept
    if node.params.get("optimize", True):
        ordered = []
        by_dia: dict[float, list] = {}
        for x, y, dia in hits:
            by_dia.setdefault(round(dia, 4), []).append((x, y, dia))
        for dia in sorted(by_dia):
            group = by_dia[dia]
            cur = (0.0, 0.0)
            pool = list(group)
            while pool:
                i = min(range(len(pool)),
                        key=lambda k: (pool[k][0] - cur[0]) ** 2 + (pool[k][1] - cur[1]) ** 2)
                hit = pool.pop(i)
                ordered.append(hit)
                cur = (hit[0], hit[1])
        hits = ordered
    travel = 0.0
    cur = (0.0, 0.0)
    for x, y, _dia in hits:
        travel += ((x - cur[0]) ** 2 + (y - cur[1]) ** 2) ** 0.5
        cur = (x, y)
    return Payload("drills", hits, {"count": len(hits), "sent_to_milling": milled,
                                    "travel_length": travel})


def _circle_segments(radius: float) -> int:
    """Quadrant segments giving roughly ARC_TOLERANCE chord error.

    A fixed high count turns three holes into thousands of G-code lines for
    no gain, so resolution follows the radius.
    """
    import math as _m
    ratio = max(1e-9, min(1.0, geo.ARC_TOLERANCE / max(radius, 1e-6)))
    per_quadrant = _m.ceil((_m.pi / 2) / _m.acos(1 - ratio)) if ratio < 1 else 4
    return max(4, min(48, per_quadrant))


@register(
    "mill_holes", "Mill holes",
    [TOOL_CHOICE,
     F("tool_dia", "Tool diameter", 0.8, unit="mm", minimum=0.05, maximum=10, group="Tool"),
     F("mill_threshold", "Mill at or above", 2.0, unit="mm", minimum=0.05, maximum=30,
       step=0.1, group="Selection",
       help="Holes this size and larger are milled. Match the Drill holes threshold."),
     F("max_dia", "Ignore above", 30.0, unit="mm", minimum=0.1, maximum=100, step=0.5,
       group="Selection"),
     B("clear_center", "Clear the whole hole", False, group="Cutting",
       help="Off: one pass at the final radius, leaving a loose slug. On: spiral "
            "out from the centre so no slug is left."),
     F("overlap", "Pass overlap", 0.3, minimum=0.0, maximum=0.95, step=0.05,
       group="Cutting", depends_on="clear_center"),
     C("milling_type", "Milling type", "conventional", MILLING, group="Cutting"),
     B("optimize", "Optimise travel order", True, group="Cutting")],
    inputs=["drills"], output="paths", category="CAM",
)
def op_mill_holes(doc, node, drills: Payload):
    dia = _tool_dia(node)
    threshold = float(node.params["mill_threshold"])
    ceiling = float(node.params["max_dia"])
    overlap = float(node.params["overlap"])
    clear_center = bool(node.params.get("clear_center"))

    lines: list[LineString] = []
    milled = skipped = 0
    for x, y, hole in drills.data:
        if hole < threshold - 1e-9 or hole > ceiling + 1e-9:
            continue
        final_r = (hole - dia) / 2
        if final_r <= 1e-6:
            skipped += 1  # tool does not fit inside this hole
            continue
        radii = [final_r]
        if clear_center:
            step = max(dia * (1.0 - overlap), 1e-4)
            radii = []
            r = step / 2
            while r < final_r - 1e-9:
                radii.append(r)
                r += step
            radii.append(final_r)
        for r in radii:
            ring = Point(x, y).buffer(r, quad_segs=_circle_segments(r)).exterior
            lines.append(LineString(ring.coords))
        milled += 1

    if node.params.get("milling_type") == "climb":
        lines = [LineString(list(ln.coords)[::-1]) for ln in lines]
    if node.params.get("optimize", True):
        lines = geo.order_paths(lines)

    return Payload("paths", lines, {
        "tool_diameter": dia,
        "tool": node.params.get("tool", ""),
        "holes_milled": milled,
        "holes_too_small": skipped,
        "cut_length": geo.cut_length(lines),
        "travel_length": geo.travel_length(lines),
    })


@register(
    "clearance_check", "Clearance check",
    [F("tool_dia", "Tool diameter", 0.2, unit="mm", minimum=0.001, maximum=20, group="Tool"),
     C("tool_shape", "Tool shape", "flat", ["flat", "v"], group="Tool"),
     F("tip_dia", "V tip diameter", 0.02, unit="mm", minimum=0.0, maximum=2, step=0.01,
       group="Tool", depends_on="tool_shape:v"),
     F("tip_angle", "V tip angle", 30.0, unit="deg", minimum=1, maximum=180, step=1,
       decimals=1, group="Tool", depends_on="tool_shape:v"),
     F("cut_depth", "Depth of cut", 0.1, unit="mm", minimum=0.001, maximum=5, step=0.01,
       group="Tool"),
     F("ignore_below", "Ignore slivers below", 0.001, unit="mm2", minimum=0.0, maximum=10,
       step=0.001, group="Check",
       help="Numerical noise on curved pads produces slivers of a few square "
            "microns. Anything smaller than this is not reported.")],
    inputs=["copper"], output="alert", category="CAM",
    input_labels=["Copper"],
)
def op_clearance_check(doc, node, copper: Payload):
    """Find gaps the isolation tool cannot enter.

    A morphological closing by the tool radius fills every gap narrower than
    the cut width. Whatever the closing added that was not copper is exactly
    the material the tool will fail to remove -- a short between traces. This
    is the failure that ruins a milled board, and it is invisible until you
    look at the finished copper.
    """
    polys = copper.data
    width = _tool_width(node)
    if polys.is_empty or width <= 0:
        return Payload("alert", MultiPolygon(), {"tool_diameter": width, "shorts": 0})

    radius = width / 2
    closed = polys.buffer(radius, quad_segs=geo.BUFFER_SEGMENTS).buffer(
        -radius, quad_segs=geo.BUFFER_SEGMENTS)
    blocked = geo.to_multipolygon(closed.difference(polys))

    threshold = float(node.params.get("ignore_below", 0.0))
    slivers = [p for p in blocked.geoms if p.area > threshold]

    # A sliver bridging two separate copper features is a short. A sliver
    # tucked into the concave corner where a trace meets its own pad is not:
    # the tool just leaves the corner slightly rounded. Only the first kind
    # is worth stopping for, so classify by how many copper components each
    # sliver touches.
    components = list(polys.geoms)
    tree = STRtree(components)
    shorts, corners = [], 0
    for sliver in slivers:
        probe = sliver.buffer(1e-6)
        touching = sum(1 for i in tree.query(probe) if components[i].intersects(probe))
        if touching > 1:
            shorts.append(sliver)
        else:
            corners += 1

    blocked = MultiPolygon(shorts)
    spots = []
    for poly in shorts:
        point = poly.representative_point()
        spots.append((round(point.x, 3), round(point.y, 3), round(poly.area, 5)))
    spots.sort(key=lambda s: -s[2])

    return Payload("alert", blocked, {
        "tool_diameter": width,
        "shorts": len(shorts),
        "rounded_corners": corners,
        "blocked_area": blocked.area,
        "spots": spots,
    })


@register(
    "cnc_job", "CNC job",
    [F("cut_z", "Cut Z", -0.1, unit="mm", minimum=-50, maximum=0, step=0.01, group="Depth"),
     F("travel_z", "Travel Z", 2.0, unit="mm", minimum=0, maximum=100, step=0.5, group="Depth"),
     B("multidepth", "Multiple depth passes", False, group="Depth"),
     F("depth_per_pass", "Depth per pass", 0.05, unit="mm", minimum=0.001, maximum=10,
       step=0.01, group="Depth", depends_on="multidepth"),
     F("feed_xy", "Feed rate XY", 120.0, unit="mm/min", minimum=1, maximum=10000, step=10,
       decimals=1, group="Feeds"),
     F("feed_z", "Feed rate Z", 60.0, unit="mm/min", minimum=1, maximum=5000, step=10,
       decimals=1, group="Feeds"),
     I("spindle", "Spindle speed", 12000, unit="rpm", minimum=0, maximum=100000, group="Feeds"),
     F("dwell", "Spindle dwell", 0.0, unit="s", minimum=0, maximum=60, step=0.5, decimals=2,
       group="Feeds"),
     F("rapid_rate", "Rapid rate", 2000.0, unit="mm/min", minimum=1, maximum=30000,
       step=100, decimals=0, group="Feeds",
       help="Only used for the run-time estimate, never written to the file. "
            "Ignored for machines that pin their own rapid rate, such as Wegstr."),
     C("dialect", "Postprocessor", "grbl", sorted(gc.POSTPROCESSORS), group="Output"),
     B("feeds_from_tool", "Take feeds from the tool", True, group="Feeds",
       help="Use the feeds, depth and spindle recorded for the tool the input "
            "was cut with. Tools with no feeds established leave these fields "
            "in charge and the job says so."),
     C("tool", "Tool override", "", toollib.CHOICES, group="Feeds",
       help="Force a particular tool instead of the one the input carries."),
     F("arc_tolerance", "Arc fitting", 0.002, unit="mm", minimum=0.0, maximum=0.5,
       step=0.001, decimals=4, group="Output",
       help="Refit circular runs as G02/G03 within this deviation, on dialects "
            "that support arcs. Zero writes line segments only."),
     F("toolchange_z", "Tool change Z", 20.0, unit="mm", minimum=0, maximum=200, step=1,
       group="Output"),
     I("tool_number", "Tool number", 1, minimum=1, maximum=99, group="Output"),
     S("start_code", "Start G-code", "", group="Output"),
     S("end_code", "End G-code", "", group="Output")],
    inputs=["any"], output="gcode", category="Output",
)
def op_cnc_job(doc, node, source: Payload):
    if source.kind not in ("paths", "drills"):
        raise TypeError("CNC job needs toolpaths or drills as input")

    params = gc.JobParams(
        cut_z=float(node.params["cut_z"]),
        travel_z=float(node.params["travel_z"]),
        feed_xy=float(node.params["feed_xy"]),
        feed_z=float(node.params["feed_z"]),
        spindle=int(node.params["spindle"]),
        dwell=float(node.params["dwell"]),
        multidepth=bool(node.params["multidepth"]),
        depth_per_pass=float(node.params["depth_per_pass"]),
        toolchange_z=float(node.params["toolchange_z"]),
        tool_number=int(node.params["tool_number"]),
        tool_diameter=float(source.meta.get("tool_diameter", 0.0)),
        start_code=node.params.get("start_code", ""),
        end_code=node.params.get("end_code", ""),
    )
    dialect = node.params["dialect"]
    meta = {"title": node.name}
    warnings: list[str] = []
    arc_tolerance = float(node.params.get("arc_tolerance", 0.0))

    # Which cutter's numbers to use: an explicit override on the job, else the
    # tool the geometry upstream was generated for.
    groups = source.meta.get("groups") if source.kind == "paths" else None
    lookup = None
    named = None
    if node.params.get("feeds_from_tool", True):
        library = toollib.library()
        named = (toollib.get(str(node.params.get("tool", "") or ""))
                 or toollib.get(str(source.meta.get("tool", "") or "")))
        if groups or source.kind == "drills":
            # One cutter per group or per hole size: match each diameter back to
            # a tool in the library.
            lookup = library.by_diameter
        elif named is not None:
            # A single-tool job. The payload's diameter is a cut width, which for
            # a V-bit is not the tool's diameter, so never match on it -- the
            # named tool is the tool.
            lookup = lambda _diameter, _tool=named: _tool

    if source.kind == "paths":
        text = gc.paths_to_gcode(source.data, params, dialect, meta, groups=groups,
                                 warnings=warnings, tool_lookup=lookup,
                                 arc_tolerance=arc_tolerance)
        stats = {
            "cut_length": source.meta.get("cut_length", geo.cut_length(source.data)),
            "travel_length": source.meta.get("travel_length", geo.travel_length(source.data)),
        }
    else:
        text = gc.drills_to_gcode(source.data, params, dialect, meta, warnings=warnings,
                                  tool_lookup=lookup)
        stats = {"holes": len(source.data)}

    if named is not None:
        stats["tool"] = named.id
        if named.has_feeds:
            params = gc.apply_tool(params, named)
            load = named.chip_load(
                params.feed_z if named.kind == "drill" else params.feed_xy)
            if load is not None:
                stats["chip_load_um"] = load * 1000
            elif named.spindle_rpm <= 0:
                warnings.append(
                    f"No spindle speed recorded for {named.id}, so there is no "
                    f"chip-load figure. Add the rpm you actually dial in.")
        else:
            warnings.append(
                f"Tool {named.id} has no feeds recorded yet, so this job's own "
                f"feed fields were used. Add them to the tool library once they "
                f"have proven themselves.")

    stats["lines"] = text.count("\n")
    stats["minutes"] = gc.estimate_minutes(source, params,
                                           float(node.params.get("rapid_rate", 2000.0)),
                                           dialect)
    if warnings:
        stats["warnings"] = warnings
    return Payload("gcode", text, stats)
