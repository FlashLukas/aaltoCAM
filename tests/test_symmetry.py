"""Alignment holes, and mirroring about the line the board actually turns on.

The failure this exists to prevent is quiet and expensive. A board flipped
about a pair of pins but mirrored about its bounding-box centre comes out
offset by twice the distance between those two lines -- geometry that looks
perfectly well formed, registers nowhere, and is only found after the copper is
cut.

So what is checked is the arithmetic of exactly that: a feature on the axis must
not move, one off the axis must land the same distance the other side, and the
three ways of naming the axis must agree where they should and be refused where
they cannot.
"""

from __future__ import annotations

import pytest
from shapely.geometry import box

from aaltocam.core import ops
from aaltocam.core.graph import Payload


class Node:
    """Params and a name, which is all these operations read off a node."""

    def __init__(self, **params):
        defaults = {
            "align": "none", "offset_x": 0.0, "offset_y": 0.0,
            "mirror": "none", "rotate": 0.0, "scale": 1.0,
            "mirror_about": "reference centre", "mirror_at": 0.0,
            "axis": "y", "place": "board centre", "at": 0.0,
            "diameter": 2.0, "margin": 5.0,
        }
        defaults.update(params)
        self.params = defaults
        self.name = "test"


def board(minx=0.0, miny=0.0, maxx=20.0, maxy=10.0) -> Payload:
    """A rectangle standing in for copper."""
    return Payload("region", box(minx, miny, maxx, maxy), {})


def holes(**params):
    return ops.op_alignment_holes(None, Node(**params), board())


def mirrored(source, **params):
    return ops.op_transform(None, Node(mirror="y", **params), source)


# --- where the holes go -------------------------------------------------------

def test_a_vertical_axis_puts_holes_above_and_below():
    out = holes(axis="y", margin=5.0)
    assert len(out.data) == 2
    assert sorted({round(h[0], 6) for h in out.data}) == [10.0]   # on the axis
    assert sorted(h[1] for h in out.data) == [-5.0, 15.0]         # clear of it


def test_a_horizontal_axis_puts_holes_left_and_right():
    out = holes(axis="x", margin=4.0)
    assert sorted({round(h[1], 6) for h in out.data}) == [5.0]
    assert sorted(h[0] for h in out.data) == [-4.0, 24.0]


def test_the_axis_can_be_named_outright():
    out = holes(axis="y", place="coordinate", at=3.0)
    assert {round(h[0], 6) for h in out.data} == {3.0}


def test_the_axis_is_published_for_the_mirror():
    assert holes(axis="y").meta["axis"] == ("y", 10.0)
    assert holes(axis="x").meta["axis"] == ("x", 5.0)


def test_the_diameter_reaches_the_holes():
    assert {h[2] for h in holes(diameter=3.2).data} == {3.2}


def test_a_board_with_no_extent_is_refused():
    with pytest.raises(ValueError, match="extent"):
        ops.op_alignment_holes(None, Node(), Payload("drills", [], {}))


# --- mirroring about a named line ---------------------------------------------

def test_the_default_still_mirrors_about_the_reference_centre():
    """Projects built before this change must not move."""
    out = mirrored(board(0, 0, 20, 10))
    assert out.data.bounds == pytest.approx((0.0, 0.0, 20.0, 10.0))


def test_mirroring_about_a_coordinate_moves_the_board():
    # About x = 0, a board spanning 0..20 lands on -20..0.
    out = mirrored(board(0, 0, 20, 10), mirror_about="coordinate", mirror_at=0.0)
    assert out.data.bounds == pytest.approx((-20.0, 0.0, 0.0, 10.0))


def test_a_feature_on_the_axis_does_not_move():
    out = mirrored(board(5, 0, 15, 10), mirror_about="coordinate", mirror_at=10.0)
    assert out.data.bounds == pytest.approx((5.0, 0.0, 15.0, 10.0))


def test_a_feature_off_the_axis_lands_the_same_distance_the_other_side():
    # The left edge is 8 mm left of x = 10, so it must end 8 mm to its right.
    out = mirrored(board(2, 0, 6, 10), mirror_about="coordinate", mirror_at=10.0)
    assert out.data.bounds[0] == pytest.approx(14.0)
    assert out.data.bounds[2] == pytest.approx(18.0)


def test_mirroring_twice_about_one_line_is_the_identity():
    once = mirrored(board(2, 0, 6, 10), mirror_about="coordinate", mirror_at=10.0)
    twice = mirrored(once, mirror_about="coordinate", mirror_at=10.0)
    assert twice.data.bounds == pytest.approx((2.0, 0.0, 6.0, 10.0))


def test_an_x_mirror_uses_the_coordinate_as_a_y_line():
    out = ops.op_transform(
        None, Node(mirror="x", mirror_about="coordinate", mirror_at=0.0),
        board(0, 0, 20, 10))
    assert out.data.bounds == pytest.approx((0.0, -10.0, 20.0, 0.0))


# --- mirroring about the pins -------------------------------------------------

def pins(axis="y", at=3.0):
    return Payload("drills", [], {"axis": (axis, at)})


def test_the_pins_line_is_used_when_wired():
    out = ops.op_transform(
        None, Node(mirror="y", mirror_about="alignment holes"),
        board(0, 0, 20, 10), None, pins(at=3.0))
    # About x = 3, a board spanning 0..20 lands on -14..6.
    assert out.data.bounds == pytest.approx((-14.0, 0.0, 6.0, 10.0))


def test_the_pins_and_the_same_coordinate_agree():
    by_pins = ops.op_transform(
        None, Node(mirror="y", mirror_about="alignment holes"),
        board(0, 0, 20, 10), None, pins(at=7.5))
    by_number = mirrored(board(0, 0, 20, 10),
                         mirror_about="coordinate", mirror_at=7.5)
    assert by_pins.data.bounds == pytest.approx(by_number.data.bounds)


def test_holes_and_mirror_round_trip_through_the_graph():
    """What the two operations are for: pins placed, then mirrored about them."""
    plate = board(0, 0, 20, 10)
    drilled = ops.op_alignment_holes(None, Node(axis="y"), plate)
    out = ops.op_transform(
        None, Node(mirror="y", mirror_about="alignment holes"),
        plate, None, drilled)
    # The pins sit on the board's centre line, so the board maps onto itself.
    assert out.data.bounds == pytest.approx(plate.data.bounds)


def test_forgetting_to_wire_the_pins_is_an_error():
    with pytest.raises(ValueError, match="Mirror axis input"):
        ops.op_transform(
            None, Node(mirror="y", mirror_about="alignment holes"), board())


def test_pins_on_the_wrong_axis_are_refused():
    """Flipping about one line and mirroring about the other cannot register."""
    with pytest.raises(ValueError, match="cannot be mirrored"):
        ops.op_transform(
            None, Node(mirror="y", mirror_about="alignment holes"),
            board(), None, pins(axis="x", at=5.0))


# --- the mirror line must not disturb anything else ---------------------------

def test_a_named_line_changes_nothing_when_no_mirror_is_selected():
    plain = ops.op_transform(None, Node(rotate=90.0), board(0, 0, 20, 10))
    with_line = ops.op_transform(
        None, Node(rotate=90.0, mirror_about="coordinate", mirror_at=-50.0),
        board(0, 0, 20, 10))
    assert with_line.data.bounds == pytest.approx(plain.data.bounds)


def test_rotation_still_pivots_on_the_reference_centre():
    """Scale and rotation keep the old anchor; only the mirror gets its own."""
    out = ops.op_transform(
        None, Node(mirror="y", mirror_about="coordinate", mirror_at=100.0,
                   rotate=180.0),
        board(0, 0, 20, 10))
    # Mirroring about x = 100 sends 0..20 to 180..200. The rotation then turns
    # that about the *reference* centre at x = 10 -- not about the mirrored
    # result's own centre -- landing it on -180..-160. Naming a mirror line
    # must not quietly move the pivot everything else turns on.
    assert out.data.bounds == pytest.approx((-180.0, 0.0, -160.0, 10.0))
