"""Arc fitting has to be provably honest: it replaces geometry that will be cut.

Run with pytest, or directly with python.
"""

import math
import os
import sys

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
