"""Arc fitting has to be provably honest: it replaces geometry that will be cut.

Run with pytest, or directly with python.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shapely.geometry import LineString, Point  # noqa: E402

from aaltocam.core import arcfit  # noqa: E402

TOL = 0.002


def circle(radius, count, turns=1.0, cx=0.0, cy=0.0):
    return [(cx + radius * math.cos(2 * math.pi * turns * k / count),
             cy + radius * math.sin(2 * math.pi * turns * k / count))
            for k in range(count + 1)]


def max_deviation(coords, moves):
    """Largest distance from an original vertex to the path actually emitted."""
    worst = 0.0
    start = coords[0]
    index = 1
    for move in moves:
        if move[0] == "line":
            segment = LineString([start, move[1]])
            while index < len(coords):
                worst = max(worst, segment.distance(Point(coords[index])))
                index += 1
                if coords[index - 1] == move[1]:
                    break
            start = move[1]
            continue
        end, center, _clockwise = move[1], move[2], move[3]
        radius = math.dist(center, start)
        while index < len(coords):
            worst = max(worst, abs(math.dist(center, coords[index]) - radius))
            index += 1
            if math.dist(coords[index - 1], end) < 1e-12:
                break
        start = end
    return worst


def worst_bow(coords, moves):
    """How far an emitted arc strays from the straight segment it replaced.

    `max_deviation` above measures the original vertices against the emitted
    path, and that is exactly the blind spot that let a 66 mm board edge be
    emitted as a 168 mm arc: every vertex sat on the circle, so nothing
    complained, while the path between two of them bowed 3.2 mm out of the
    line. Between two input points the path is a straight chord, so what has
    to be measured is the sagitta.
    """
    worst = 0.0
    i = 0
    for move in moves:
        end = move[1]
        j = i
        while j < len(coords) - 1 and math.dist(coords[j], end) > 1e-9:
            j += 1
        if move[0] == "arc":
            radius = math.dist(move[2], coords[i])
            for a, b in zip(coords[i:j], coords[i + 1:j + 1]):
                half = math.dist(a, b) / 2
                worst = max(worst, radius - math.sqrt(max(radius * radius - half * half, 0.0)))
        i = j
    return worst


def test_a_long_segment_is_never_swallowed_by_an_arc():
    """Points can lie exactly on a circle and still not be an arc.

    Three points 0.05 mm apart and a fourth 66 mm along the same 168 mm
    circle: every vertex is on it to the last decimal, but the polyline
    between the last two is a straight chord, and an arc there leaves the cut
    3.2 mm from where it was asked to be.
    """
    radius = 168.0
    angles = [0.0, 0.0003, 0.0006, 0.0009]
    angles.append(66.0 / radius + angles[-1])
    pts = [(radius * math.cos(a), radius * math.sin(a)) for a in angles]

    moves = arcfit.fit(pts, tolerance=TOL)
    long_chord = math.dist(pts[-2], pts[-1])
    assert long_chord > 60                      # the segment really is long
    for move in moves:
        if move[0] == "arc":
            assert math.dist(move[2], pts[0]) < 10, "arc across the long edge"
    assert worst_bow(pts, moves) <= TOL


def test_a_corner_followed_by_a_long_edge_keeps_the_edge_straight():
    """The shape this was found on: an offset board corner, then the edge."""
    r, spacing, n, edge = 0.5, 0.05, 6, 66.0
    step = spacing / r
    pts = [(r * math.cos(k * step), r * math.sin(k * step)) for k in range(n)]
    tx, ty = -math.sin((n - 1) * step), math.cos((n - 1) * step)
    pts.append((pts[-1][0] + tx * edge, pts[-1][1] + ty * edge))

    moves = arcfit.fit(pts, tolerance=TOL)
    assert moves[-1][0] == "line", "the long edge must stay a line"
    assert worst_bow(pts, moves) <= TOL


def test_a_pad_outline_still_becomes_arcs():
    """The fix must not cost the reduction it exists alongside."""
    pts = circle(1.0, 64)
    moves = arcfit.fit(pts, tolerance=TOL)
    assert all(m[0] == "arc" for m in moves)
    assert len(moves) == 2


def test_no_emitted_arc_bows_off_the_path():
    """The invariant, over every shape here: arcs approximate the path, not
    merely the corners."""
    shapes = {
        "pad": circle(1.0, 64),
        "small pad": circle(0.1, 32),
        "slot": list(LineString([(0, 0), (10, 0)]).buffer(0.5, quad_segs=16).exterior.coords),
        "square": [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        "sparse circle": circle(50.0, 8),
    }
    for name, pts in shapes.items():
        moves = arcfit.fit(pts, tolerance=TOL)
        assert worst_bow(pts, moves) <= TOL, name


def test_the_chord_bound_is_the_sagitta_solved_for_length():
    """A chord exactly at the bound bows exactly the tolerance; longer bows more."""
    for radius in (0.5, 5.0, 168.0):
        longest = arcfit._max_chord(radius, TOL)
        half = longest / 2
        assert radius - math.sqrt(radius * radius - half * half) == pytest.approx(TOL, rel=1e-6)
        over = (longest * 1.05) / 2
        assert radius - math.sqrt(radius * radius - over * over) > TOL


def test_circle_becomes_two_arcs():
    for radius, count in ((0.1, 32), (1.0, 64), (5.0, 128)):
        pts = circle(radius, count)
        moves = arcfit.fit(pts, tolerance=TOL)
        assert all(m[0] == "arc" for m in moves), radius
        # Half a turn is the sweep cap, so a full circle is exactly two arcs.
        assert len(moves) == 2, (radius, len(moves))
        assert max_deviation(pts, moves) <= TOL


def test_straight_lines_are_left_alone():
    square = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    assert [m[0] for m in arcfit.fit(square, tolerance=TOL)] == ["line"] * 4


def test_noise_is_never_fitted():
    import random
    random.seed(7)
    noise = [(k * 0.1, random.uniform(-1, 1)) for k in range(300)]
    assert not any(m[0] == "arc" for m in arcfit.fit(noise, tolerance=TOL))


def test_rounded_slot_keeps_its_flats():
    ring = list(LineString([(0, 0), (10, 0)]).buffer(0.5, quad_segs=16).exterior.coords)
    moves = arcfit.fit(ring, tolerance=TOL)
    kinds = [m[0] for m in moves]
    assert kinds.count("arc") == 2, kinds       # the two end caps
    assert kinds.count("line") == 2, kinds      # the two flats
    assert max_deviation(ring, moves) <= TOL


def test_tiny_radius_is_refused():
    """Below the dialect's minimum an arc must stay a line, not be emitted
    as something the controller will reject or round away."""
    pts = circle(0.02, 32)
    moves = arcfit.fit(pts, tolerance=TOL, min_radius=0.055)
    assert not any(m[0] == "arc" for m in moves)


def test_direction_is_preserved():
    """Climb versus conventional is expressed by path direction, so an arc must
    keep the direction the points had."""
    ccw = circle(1.0, 64)
    cw = list(reversed(ccw))
    assert all(m[3] is False for m in arcfit.fit(ccw, tolerance=TOL) if m[0] == "arc")
    assert all(m[3] is True for m in arcfit.fit(cw, tolerance=TOL) if m[0] == "arc")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
