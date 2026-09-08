"""Refit circular runs in a polyline so they can be emitted as G02/G03.

Everything upstream is Shapely, which has no notion of an arc: a pad outline
buffered by the tool radius arrives here as a 64-sided polygon. Writing that out
as 64 line segments is what makes isolation files enormous, and on a controller
that interpolates in software it is also slower to run.

So we put the arcs back. This is a fit, not a recovery -- we do not know which
points were once an arc, only which ones lie on a common circle within a
tolerance we choose. That tolerance is a real deviation from the path that was
asked for, so it belongs below the machine's own resolution: a Wegstr steps in
0.004 mm, and the default is half of that. Loosening it to 0.005 buys about ten
percent more reduction for more than twice the error, which is a bad trade.

Straight runs and anything that does not fit stay as line segments, so the
output is never worse than the input.
"""

from __future__ import annotations

import math

#: Longest run of points considered for one arc. Bounds the O(k^2) window check
#: on pathological inputs; no real toolpath has a single arc this long.
MAX_WINDOW = 256


def _circle(p1, p2, p3):
    """Centre and radius through three points, or None if they are collinear."""
    (x1, y1), (x2, y2), (x3, y3) = p1, p2, p3
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    s1 = x1 * x1 + y1 * y1
    s2 = x2 * x2 + y2 * y2
    s3 = x3 * x3 + y3 * y3
    cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    return (cx, cy), math.dist((cx, cy), p1)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _sweep(center, start, end, clockwise):
    """Swept angle in radians, always positive, in the given direction."""
    a1 = math.atan2(start[1] - center[1], start[0] - center[0])
    a2 = math.atan2(end[1] - center[1], end[0] - center[0])
    delta = a1 - a2 if clockwise else a2 - a1
    while delta < 0:
        delta += 2 * math.pi
    return delta


def _fits(points, center, radius, tolerance):
    for point in points:
        if abs(math.dist(center, point) - radius) > tolerance:
            return False
    return True


def fit(coords, tolerance: float = 0.002, min_radius: float = 0.05,
        max_radius: float = 1000.0, min_points: int = 5,
        max_sweep: float = math.pi) -> list[tuple]:
    """Turn a polyline into a list of moves after its first point.

    Each move is either ``("line", (x, y))`` or
    ``("arc", (x, y), (cx, cy), clockwise)``.

    `min_points` is how many consecutive points must lie on the circle before an
    arc is worth emitting; below that an arc saves nothing and only adds a way to
    be wrong. `max_sweep` defaults to half a turn, which every controller that
    supports arcs at all can manage.
    """
    points = [tuple(map(float, c)) for c in coords]
    if len(points) < 2:
        return []

    moves: list[tuple] = []
    i = 0
    last = len(points) - 1

    while i < last:
        best = None
        limit = min(last, i + MAX_WINDOW)
        # Grow the window while the whole run still lies on one circle.
        for j in range(i + min_points - 1, limit + 1):
            window = points[i:j + 1]
            fitted = _circle(window[0], window[len(window) // 2], window[-1])
            if fitted is None:
                break
            center, radius = fitted
            if not (min_radius <= radius <= max_radius):
                break
            if not _fits(window, center, radius, tolerance):
                break

            # One consistent turn direction, no cusps, no reversals.
            turn = _cross(window[0], window[1], window[2])
            if turn == 0:
                break
            clockwise = turn < 0
            consistent = True
            for k in range(1, len(window) - 1):
                side = _cross(window[k - 1], window[k], window[k + 1])
                if side == 0 or (side < 0) != clockwise:
                    consistent = False
                    break
            if not consistent:
                break
            if _sweep(center, window[0], window[-1], clockwise) > max_sweep:
                break

            best = (j, center, clockwise)

        if best is None:
            i += 1
            moves.append(("line", points[i]))
            continue

        j, center, clockwise = best
        moves.append(("arc", points[j], center, clockwise))
        i = j

    return moves
