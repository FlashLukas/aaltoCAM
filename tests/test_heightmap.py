"""Reading a probe file, interpolating it, and compensating G-code with it.

The things worth pinning down are the ones that would be wrong quietly. A
misread separator turns 1,5 into two numbers. A bCNC header read as data puts a
phantom point at the grid extents. Compensation that only samples the ends of a
long move looks correct in the file and cuts through the board in the middle.
And a compensated file sent to a machine that levels for itself is corrected
twice, which is the one failure here that costs a board rather than a minute.
"""

from __future__ import annotations

import math
import re

import pytest
from shapely.geometry import LineString

from aaltocam.core import gcode as gc
from aaltocam.core import heightmap as hmap


def bowl(nx=5, ny=5, span=20.0, peak=0.10):
    """A grid bowed upward in the middle: zero at the edges, `peak` at centre."""
    points = []
    for j in range(ny):
        for i in range(nx):
            x = span * i / (nx - 1)
            y = span * j / (ny - 1)
            z = peak * (1 - ((x - span / 2) / (span / 2)) ** 2) \
                     * (1 - ((y - span / 2) / (span / 2)) ** 2)
            points.append((x, y, z))
    return points


@pytest.fixture
def hm():
    return hmap.HeightMap.from_points(bowl())


# --- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "1.0 2.0 0.05",
    "1.0,2.0,0.05",
    "1.0;2.0;0.05",
    "1.0\t2.0\t0.05",
    "  1.0   2.0   0.05  ",
])
def test_separators(line):
    points, _ = hmap.parse_points(line)
    assert points == [(1.0, 2.0, 0.05)]


def test_decimal_comma_is_not_read_as_a_separator():
    # "1,0 2,0 0,05" is three numbers, not six.
    points, _ = hmap.parse_points("1,0 2,0 0,05")
    assert points == [(1.0, 2.0, 0.05)]


def test_comments_and_blank_lines_are_skipped():
    text = "# probe\n\n; note\n( g-code style )\n1 2 0.1\n"
    points, _ = hmap.parse_points(text)
    assert points == [(1.0, 2.0, 0.1)]


def test_bcnc_header_is_recognised_and_dropped():
    body = "\n".join(f"{i*10} {j*10} 0.0{i+j}" for j in range(3) for i in range(3))
    points, notes = hmap.parse_points(f"0 20 3\n0 20 3\n-1 1\n{body}")
    assert len(points) == 9
    assert any("bCNC" in n for n in notes)


def test_a_header_that_does_not_add_up_is_left_alone():
    # Three real points whose first two rows happen to end in whole numbers.
    text = "0 20 3\n0 20 3\n1 1 1\n"
    points, _ = hmap.parse_points(text)
    assert len(points) == 3


def test_junk_lines_are_reported_not_silently_dropped():
    points, notes = hmap.parse_points("1 2 0.1\nnonsense here\n")
    assert len(points) == 1
    assert any("three numbers" in n for n in notes)


# --- grid detection and interpolation ----------------------------------------

def test_a_full_grid_is_recognised(hm):
    assert hm.regular
    assert len(hm.xs) == 5 and len(hm.ys) == 5


def test_scattered_points_are_not_a_grid():
    scattered = hmap.HeightMap.from_points(
        [(0, 0, 0.0), (10, 1, 0.1), (3, 7, 0.05), (9, 9, 0.02)])
    assert not scattered.regular


def test_interpolation_is_exact_at_the_nodes(hm):
    for x, y, z in hm.points:
        assert hm.at(x, y) == pytest.approx(z, abs=1e-12)


def test_interpolation_between_nodes_is_bounded(hm):
    # Bilinear never leaves the range of the four corners it sits between.
    low, high = hm.z_range()
    for x in (1.0, 7.5, 12.3, 19.0):
        for y in (0.5, 6.6, 13.1, 18.8):
            assert low - 1e-12 <= hm.at(x, y) <= high + 1e-12


# --- behaviour outside the probed area ---------------------------------------

def test_outside_the_area_holds_the_nearest_edge(hm):
    # Far away in x, the value is whatever the edge of the map says at that y.
    assert hm.at(-500, 10) == pytest.approx(hm.at(0, 10))
    assert hm.at(500, 10) == pytest.approx(hm.at(20, 10))
    assert hm.at(10, -500) == pytest.approx(hm.at(10, 0))
    assert hm.at(10, 500) == pytest.approx(hm.at(10, 20))


def test_a_far_corner_holds_the_corner_value(hm):
    assert hm.at(-999, -999) == pytest.approx(hm.at(0, 0))


def test_scattered_maps_clamp_too():
    scattered = hmap.HeightMap.from_points(
        [(0, 0, 0.0), (10, 0, 0.2), (0, 10, 0.4), (10, 10, 0.6), (5, 5, 0.3)])
    assert scattered.at(-100, 5) == pytest.approx(scattered.at(0, 5))


# --- zeroing -----------------------------------------------------------------

def test_raw_zero_adds_the_reading_as_it_stands():
    points = [(0, 0, 1.0), (1, 0, 1.0), (0, 1, 1.0), (1, 1, 1.0)]
    assert hmap.HeightMap.from_points(points, zero="raw").at(0.5, 0.5) == pytest.approx(1.0)


def test_mean_zero_centres_the_map():
    points = [(0, 0, 1.0), (1, 0, 1.0), (0, 1, 2.0), (1, 1, 2.0)]
    hm = hmap.HeightMap.from_points(points, zero="mean")
    assert hm.at(0, 0) == pytest.approx(-0.5)
    assert hm.at(0, 1) == pytest.approx(0.5)


def test_origin_zero_makes_the_reading_at_zero_zero():
    points = [(0, 0, 1.0), (1, 0, 1.2), (0, 1, 1.4), (1, 1, 1.6)]
    hm = hmap.HeightMap.from_points(points, zero="origin")
    assert hm.at(0, 0) == pytest.approx(0.0)


# --- compensation ------------------------------------------------------------

def test_long_moves_are_split_so_the_middle_is_corrected(hm):
    # One 20 mm move straight through the peak of the bowl. Sampling only the
    # ends would miss the middle entirely, which is the whole point.
    coords = [(0.0, 10.0), (20.0, 10.0)]
    out = list(hmap.compensate(coords, hm, -0.1, segment=1.0, tolerance=0.0))
    zs = [z for _, _, z in out if z is not None]
    assert len(out) > 2
    assert max(zs) > -0.1 + 0.05      # the peak was actually seen


def test_tolerance_suppresses_z_words(hm):
    coords = [(0.0, 10.0), (20.0, 10.0)]
    fine = list(hmap.compensate(coords, hm, -0.1, 1.0, 0.0))
    coarse = list(hmap.compensate(coords, hm, -0.1, 1.0, 0.05))
    assert len(coarse) < len(fine)


def test_a_huge_tolerance_still_reaches_the_vertices(hm):
    coords = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0)]
    out = list(hmap.compensate(coords, hm, -0.1, 1.0, 999.0))
    assert [(x, y) for x, y, _ in out] == [(20.0, 0.0), (20.0, 20.0)]
    assert all(z is None for _, _, z in out)


def test_tolerance_is_measured_against_the_last_written_z():
    # A steady ramp. Comparing against the last computed value would let it
    # creep away one sub-threshold step at a time and never write Z at all.
    points = [(0, 0, 0.0), (100, 0, 1.0), (0, 1, 0.0), (100, 1, 1.0)]
    ramp = hmap.HeightMap.from_points(points)
    out = list(hmap.compensate([(0.0, 0.5), (100.0, 0.5)], ramp, 0.0,
                               segment=1.0, tolerance=0.05))
    written = [z for _, _, z in out if z is not None]
    # Against the last computed value the steps are 0.01 apart and Z would
    # never be written at all. Against the last written one it steps about
    # twenty times over the 1 mm rise.
    assert len(written) >= 9
    # The promise is that the axis is never further than the tolerance from
    # where it should be -- not that it lands exactly on the final value.
    assert abs(written[-1] - 1.0) <= 0.05 + 1e-9


# --- G-code ------------------------------------------------------------------

PATHS = [LineString([(2, 2), (18, 2), (18, 18)])]
PARAMS = gc.JobParams(cut_z=-0.1, travel_z=2.0, feed_xy=120, feed_z=60)


def cut_lines(text):
    return [l for l in text.splitlines() if l.startswith(("G1 X", "G01 X"))]


def test_without_a_map_no_cut_carries_z():
    text = gc.paths_to_gcode(PATHS, PARAMS, "grbl")
    assert all(" Z" not in line for line in cut_lines(text))


def test_with_a_map_cuts_carry_z(hm):
    text = gc.paths_to_gcode(PATHS, PARAMS, "grbl", hm=hm, segment=1.0,
                             z_tolerance=0.005)
    assert any(" Z" in line for line in cut_lines(text))


def test_arc_fitting_is_turned_off_and_said_so(hm):
    warnings: list[str] = []
    text = gc.paths_to_gcode(PATHS, PARAMS, "grbl", warnings=warnings, hm=hm,
                             segment=1.0, z_tolerance=0.005, arc_tolerance=0.01)
    # Anchored on a word boundary, because the units line G21 also starts "G2".
    assert not any(re.match(r"G0?[23]\b", line) for line in text.splitlines())
    assert any("Arc fitting is off" in w for w in warnings)


def test_a_self_levelling_dialect_refuses(hm):
    with pytest.raises(ValueError, match="own surface compensation"):
        gc.paths_to_gcode(PATHS, PARAMS, "wegstr", hm=hm, segment=1.0,
                          z_tolerance=0.005)


def test_the_override_lets_it_through_but_warns(hm):
    warnings: list[str] = []
    text = gc.paths_to_gcode(PATHS, PARAMS, "wegstr", warnings=warnings, hm=hm,
                             segment=1.0, z_tolerance=0.005,
                             allow_double_levelling=True)
    assert any(" Z" in line for line in cut_lines(text))
    assert any("levelling switched off" in w for w in warnings)


def test_every_depth_pass_is_compensated(hm):
    p = gc.JobParams(cut_z=-0.2, travel_z=2.0, feed_xy=120, feed_z=60,
                     multidepth=True, depth_per_pass=0.1)
    text = gc.paths_to_gcode(PATHS, p, "grbl", hm=hm, segment=2.0, z_tolerance=0.005)
    plunges = [l for l in text.splitlines() if l.startswith("G1 Z")]
    # Two passes per path start, and neither plunges to the bare nominal depth
    # because the surface under the start point is not at zero.
    assert len(plunges) >= 2
    assert len({l for l in plunges}) >= 2


def test_compensated_z_follows_the_surface(hm):
    text = gc.paths_to_gcode([LineString([(0, 10), (20, 10)])], PARAMS, "grbl",
                             hm=hm, segment=1.0, z_tolerance=0.0)
    zs = []
    for line in cut_lines(text):
        if " Z" in line:
            zs.append(float(line.split(" Z")[1].split()[0]))
    # Rises into the bowl's peak and comes back down.
    assert zs[len(zs) // 2] > zs[0] + 0.05
    assert zs[-1] < zs[len(zs) // 2] - 0.05
