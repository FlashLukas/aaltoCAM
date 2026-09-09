"""A drawn board outline is a stroke, not a board.

KiCad plots Edge.Cuts with a thin aperture, so the profile arrives as a ribbon
a few hundredths of a millimetre wide tracing where the edge goes. Taken at
face value it is a polygon whose hole is the whole board, and cutting its
boundary sends the router round the outside of the part and then again a tool
width inside it -- straight through the middle. These tests pin the ribbon
down: what encloses what, where the true edge sits inside the ink, and that
only the waste side gets cut.
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, Polygon, box

from aaltocam.core import geometry as geo
from aaltocam.core import ops


STROKE = 0.05   # KiCad's default Edge.Cuts aperture, in mm


def ribbon(shape, width: float = STROKE):
    """What a plotter makes of a drawn outline: ink along the boundary."""
    return LineString(shape.exterior.coords).buffer(width / 2,
                                                    quad_segs=geo.BUFFER_SEGMENTS)


class FakeNode:
    def __init__(self, **params):
        self.params = params


def cut(region, dia=0.6, margin=0.5, side="outside"):
    node = FakeNode(side=side, margin=margin, gaps="none", gap_size=2.0,
                    milling_type="conventional", optimize=False)
    return ops._contour_paths(region, node, dia)


# --- what the ribbon actually is --------------------------------------------

def test_a_stroked_outline_is_mostly_hole():
    board = box(0, 0, 20, 30)
    ink = ribbon(board)
    assert len(ink.interiors) == 1
    assert ink.area < 6.0                      # a hair of ink
    assert Polygon(ink.interiors[0]).area > 500  # wrapped round the whole board


def test_cutting_the_ribbon_would_cut_the_board_in_half():
    """The bug, stated as a test: two contours where there should be one."""
    lines, contours, _, _ = cut(ribbon(box(0, 0, 20, 30)))
    assert contours == 1
    assert len(lines) == 2, "expected the ribbon to yield an inside pass too"
    inner = min(lines, key=lambda ln: ln.length)
    assert inner.bounds[0] > 0, "the spurious pass runs inside the board"


# --- the fix -----------------------------------------------------------------

def test_enclosed_region_recovers_the_board():
    board = box(0, 0, 20, 30)
    region = geo.enclosed_region(ribbon(board))
    assert len(region.geoms) == 1
    assert not region.geoms[0].interiors
    assert math.isclose(region.area, board.area, rel_tol=1e-5)


def test_the_true_edge_is_the_centre_of_the_stroke():
    """A drawn edge means its centreline, so half the ink lies outside it."""
    region = geo.enclosed_region(ribbon(box(0, 0, 20, 30), width=0.4))
    minx, miny, maxx, maxy = region.bounds
    assert math.isclose(minx, 0.0, abs_tol=0.01)
    assert math.isclose(maxx, 20.0, abs_tol=0.01)
    assert math.isclose(maxy, 30.0, abs_tol=0.01)


def test_midline_off_keeps_the_outer_edge_of_the_ink():
    region = geo.enclosed_region(ribbon(box(0, 0, 20, 30), width=0.4),
                                 midline=False)
    assert math.isclose(region.bounds[2], 20.2, abs_tol=0.01)


def test_a_solid_region_passes_through_untouched():
    """A drawn region, or copper, has no ribbon to measure."""
    solid = box(0, 0, 20, 30)
    region = geo.enclosed_region(solid)
    assert math.isclose(region.area, solid.area, rel_tol=1e-9)


def test_only_the_waste_side_is_cut():
    region = geo.enclosed_region(ribbon(box(0, 0, 20, 30)))
    lines, contours, _, _ = cut(region, dia=0.6, margin=0.5)
    assert contours == 1
    assert len(lines) == 1
    minx, miny, maxx, maxy = lines[0].bounds
    # Tool centre sits a radius plus the margin clear of the edge, all round.
    assert math.isclose(minx, -0.8, abs_tol=0.02)
    assert math.isclose(miny, -0.8, abs_tol=0.02)
    assert math.isclose(maxx, 20.8, abs_tol=0.02)
    assert math.isclose(maxy, 30.8, abs_tol=0.02)


# --- windows -----------------------------------------------------------------

def test_a_window_drawn_on_the_profile_becomes_a_hole():
    """Two loops on Edge.Cuts: the board, and a window inside it."""
    ink = geo.to_multipolygon(
        ribbon(box(0, 0, 20, 30)).union(ribbon(box(5, 5, 15, 15))))
    region = geo.enclosed_region(ink)
    assert len(region.geoms) == 1
    assert len(region.geoms[0].interiors) == 1
    assert math.isclose(region.area, 600 - 100, abs_tol=0.1)


def test_an_island_inside_a_window_survives():
    """Even-odd, counted rather than assumed: board, window, island."""
    ink = geo.to_multipolygon(
        ribbon(box(0, 0, 20, 30))
        .union(ribbon(box(5, 5, 15, 15)))
        .union(ribbon(box(8, 8, 12, 12))))
    region = geo.enclosed_region(ink)
    assert math.isclose(region.area, 600 - 100 + 16, abs_tol=0.2)


def test_a_window_is_cut_from_its_own_waste_side():
    """The tool runs inside a window, so the finished opening is full size."""
    ink = geo.to_multipolygon(
        ribbon(box(0, 0, 20, 30)).union(ribbon(box(5, 5, 15, 15))))
    lines, _, _, _ = cut(geo.enclosed_region(ink), dia=0.6, margin=0.0)
    assert len(lines) == 2
    inner = min(lines, key=lambda ln: ln.length)
    minx, miny, maxx, maxy = inner.bounds
    # A radius inside the drawn window on every side: cutting on that line
    # leaves material starting exactly at the window edge.
    assert math.isclose(minx, 5.3, abs_tol=0.02)
    assert math.isclose(maxx, 14.7, abs_tol=0.02)


# --- the real board ----------------------------------------------------------

def test_cutout_op_uses_the_enclosed_region():
    """End to end through the operation, not just the helper."""
    ink = ribbon(box(0, 0, 20, 30))
    node = FakeNode(shape="outline input", side="outside", margin=0.5,
                    gaps="none", gap_size=2.0, milling_type="conventional",
                    optimize=False, tool_dia=0.6, tool="")
    result = ops.op_cutout(None, node,
                           ops.Payload("copper", box(1, 1, 19, 29), {}),
                           ops.Payload("region", ink, {}))
    assert result.meta["contours"] == 1
    assert len(result.data) == 1
    assert math.isclose(result.data[0].bounds[0], -0.8, abs_tol=0.02)
