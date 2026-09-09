"""The measuring tool's arithmetic and its mode behaviour.

Painting is checked by eye; what is worth pinning down here is the part that
would be wrong quietly -- which end is which after you turn origin-clipping on
halfway through, what Shift drops, and that a third click starts over rather
than accumulating a polyline nobody asked for.

Needs PySide6 and runs the widget headless, so it skips where the GUI extra is
not installed.
"""

from __future__ import annotations

import math
import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aaltocam.gui.canvas import BoardView  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(app):
    widget = BoardView()
    widget.resize(400, 300)
    return widget


# --- axis lock ---------------------------------------------------------------

def test_shift_keeps_the_longer_axis():
    assert BoardView._axis_lock((0, 0), (10, 2)) == (10, 0)
    assert BoardView._axis_lock((0, 0), (2, 10)) == (0, 10)


def test_axis_lock_is_relative_to_the_anchor():
    assert BoardView._axis_lock((5, 7), (9, 7.5)) == (9, 7)


def test_a_diagonal_ties_to_horizontal():
    """Arbitrary, but it has to go somewhere and it must not wobble."""
    assert BoardView._axis_lock((0, 0), (4, 4)) == (4, 0)


# --- clicking ----------------------------------------------------------------

def test_two_clicks_make_a_measurement(view):
    view.start_measure(True)
    view._measure_click((1.0, 2.0), False)
    view._measure_click((4.0, 6.0), False)
    assert view._measure == [(1.0, 2.0), (4.0, 6.0)]


def test_a_third_click_starts_over(view):
    view.start_measure(True)
    view._measure_click((1.0, 2.0), False)
    view._measure_click((4.0, 6.0), False)
    view._measure_click((9.0, 9.0), False)
    assert view._measure == [(9.0, 9.0)]


def test_shift_on_the_second_click_locks_the_axis(view):
    view.start_measure(True)
    view._measure_click((1.0, 2.0), False)
    view._measure_click((11.0, 2.4), True)
    assert view._measure[1] == (11.0, 2.0)


def test_the_reading_is_the_distance_and_its_components(view):
    seen = []
    view.measured.connect(seen.append)
    view.start_measure(True)
    view._measure_click((1.0, 2.0), False)
    view._measure_click((4.0, 6.0), False)
    distance, dx, dy = seen[-1]
    assert math.isclose(distance, 5.0)
    assert (dx, dy) == (3.0, 4.0)


def test_one_end_alone_reads_nothing(view):
    seen = []
    view.measured.connect(seen.append)
    view.start_measure(True)
    view._measure_click((1.0, 2.0), False)
    assert seen[-1] is None


# --- clipping to the origin --------------------------------------------------

def test_clipping_puts_one_end_on_zero(view):
    view.set_clip_to_origin(True)
    view.start_measure(True)
    assert view._measure == [(0.0, 0.0)]
    view._measure_click((3.0, 4.0), False)
    assert view._measure == [(0.0, 0.0), (3.0, 4.0)]


def test_clipping_mid_measurement_keeps_the_point_you_picked(view):
    """Turning it on should re-reference your click, not discard it."""
    view.start_measure(True)
    view._measure_click((1.0, 1.0), False)
    view._measure_click((3.0, 4.0), False)
    view.set_clip_to_origin(True)
    assert view._measure == [(0.0, 0.0), (3.0, 4.0)]


def test_unclipping_drops_the_origin_end(view):
    view.set_clip_to_origin(True)
    view.start_measure(True)
    view._measure_click((3.0, 4.0), False)
    view.set_clip_to_origin(False)
    assert view._measure == [(3.0, 4.0)]


def test_clipped_restart_keeps_the_origin(view):
    view.set_clip_to_origin(True)
    view.start_measure(True)
    view._measure_click((3.0, 4.0), False)
    view._measure_click((5.0, 0.0), False)   # a third click restarts
    assert view._measure == [(0.0, 0.0), (5.0, 0.0)]


def test_clearing_leaves_the_origin_end_when_clipped(view):
    view.set_clip_to_origin(True)
    view.start_measure(True)
    view._measure_click((3.0, 4.0), False)
    view.clear_measure()
    assert view._measure == [(0.0, 0.0)]


def test_clearing_empties_it_when_not_clipped(view):
    view.start_measure(True)
    view._measure_click((3.0, 4.0), False)
    view.clear_measure()
    assert view._measure == []


# --- mode ---------------------------------------------------------------------

def test_leaving_measure_mode_clears_the_measurement(view):
    view.start_measure(True)
    view._measure_click((3.0, 4.0), False)
    view.start_measure(False)
    assert view._measure == []
    assert not view._measuring


def test_measuring_cancels_a_drawing_in_progress(view):
    view.start_draw("poly")
    view.start_measure(True)
    assert view._draw_kind is None


def test_the_live_end_follows_the_cursor_until_clicked(view):
    view.start_measure(True)
    view._measure_click((1.0, 1.0), False)
    view._measure_cursor = (4.0, 5.0)
    assert view._measure_ends() == [(1.0, 1.0), (4.0, 5.0)]
    view._measure_click((4.0, 5.0), False)
    assert view._measure_cursor is None
