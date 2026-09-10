"""Placing a layer aside, and the second zero it acquires.

Milling both sides of a board means two working areas, and the point that
matters for each is where its own X0 Y0 ended up -- that is what gets keyed
into the machine after the flip. The offset is real geometry rather than a
drawing offset, so what has to hold is that the marked origin agrees with where
the geometry actually went, through a chain of transforms and through a mirror.

Needs PySide6 and runs headless, so it skips where the GUI extra is absent.
"""

from __future__ import annotations

import os

import pytest

from aaltocam.core import ops
from aaltocam.core.graph import Document

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aaltocam.gui.app import BESIDE_GAP_MM, MainWindow  # noqa: E402

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "examples", "demo", "demo-F_Cu.gbr")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow()
    yield w
    w._mark(False)
    w.close()


def loaded(window):
    node = window.doc.add("load_gerber", path=DEMO, name="Top copper")
    window.refresh_list(select=node.id)
    window.recompute()
    return node


# --- where the origin goes ----------------------------------------------------

def test_an_untransformed_layer_has_no_second_origin():
    doc = Document()
    node = doc.add("load_gerber", path=DEMO)
    assert "origin" not in doc.evaluate(node.id).meta


def test_an_offset_moves_the_origin_with_the_geometry():
    doc = Document()
    src = doc.add("load_gerber", path=DEMO)
    moved = doc.add("transform", [src.id], offset_x=40.0, offset_y=7.0)
    assert doc.evaluate(moved.id).meta["origin"] == pytest.approx((40.0, 7.0))


def test_the_origin_shifts_by_the_same_delta_as_the_board():
    doc = Document()
    src = doc.add("load_gerber", path=DEMO)
    before = ops.payload_bounds(doc.evaluate(src.id))
    moved = doc.add("transform", [src.id], offset_x=40.0)
    after = ops.payload_bounds(doc.evaluate(moved.id))
    origin = doc.evaluate(moved.id).meta["origin"]
    assert after[0] - before[0] == pytest.approx(origin[0])


def test_a_transform_that_moves_nothing_leaves_no_origin():
    doc = Document()
    src = doc.add("load_gerber", path=DEMO)
    still = doc.add("transform", [src.id])
    assert "origin" not in doc.evaluate(still.id).meta


def test_chained_transforms_compose():
    doc = Document()
    src = doc.add("load_gerber", path=DEMO)
    one = doc.add("transform", [src.id], offset_x=10.0)
    two = doc.add("transform", [one.id], offset_x=5.0, offset_y=3.0)
    assert doc.evaluate(two.id).meta["origin"] == pytest.approx((15.0, 3.0))


def test_a_mirror_carries_the_origin_across():
    """The bottom side's zero is not where the top's was, and must not claim to be."""
    doc = Document()
    src = doc.add("load_gerber", path=DEMO)
    flipped = doc.add("transform", [src.id], mirror="y")
    origin = doc.evaluate(flipped.id).meta["origin"]
    # Mirrored about the layer's own centre, so zero lands on the far side.
    bounds = ops.payload_bounds(doc.evaluate(src.id))
    centre_x = (bounds[0] + bounds[2]) / 2
    assert origin[0] == pytest.approx(2 * centre_x)
    assert origin[1] == pytest.approx(0.0)


# --- the marker the view is given ---------------------------------------------

def test_the_view_is_told_about_the_second_origin(window):
    src = loaded(window)
    window.doc.add("transform", [src.id], offset_x=30.0, name="Bottom aside")
    window.refresh_list()
    window.recompute()

    spots = window.view.extra_origins()
    assert len(spots) == 1
    assert spots[0][0] == pytest.approx(30.0)
    assert spots[0][2] == "Bottom aside"


def test_layers_sharing_an_origin_are_marked_once(window):
    src = loaded(window)
    for i in range(3):
        window.doc.add("transform", [src.id], offset_x=30.0, name=f"Layer {i}")
    window.refresh_list()
    window.recompute()

    assert len(window.view.extra_origins()) == 1


def test_hiding_a_layer_removes_its_marker(window):
    src = loaded(window)
    moved = window.doc.add("transform", [src.id], offset_x=30.0)
    window.refresh_list()
    window.recompute()
    assert len(window.view.extra_origins()) == 1

    window.doc.nodes[moved.id].visible = False
    window.redraw()
    assert window.view.extra_origins() == []


# --- the action ---------------------------------------------------------------

def test_placing_aside_clears_the_board(window):
    src = loaded(window)
    before = ops.payload_bounds(window.results[src.id])
    window.node_list.setCurrentItem(window._items[src.id])

    window.place_beside()

    moved_id = window.doc.order[-1]
    after = ops.payload_bounds(window.results[moved_id])
    # Its left edge sits clear of the original's right edge, by the gap.
    assert after[0] == pytest.approx(before[2] + BESIDE_GAP_MM)
    assert after[0] > before[2]


def test_placing_aside_is_undoable(window):
    src = loaded(window)
    window.node_list.setCurrentItem(window._items[src.id])
    count = len(window.doc.order)

    window.place_beside()
    assert len(window.doc.order) == count + 1

    window.undo()
    assert len(window.doc.order) == count


def test_placing_aside_needs_a_selection(window):
    window.node_list.setCurrentItem(None)
    window.place_beside()          # must not raise
    assert window.doc.order == []


def test_placing_aside_nests_under_its_source(window):
    src = loaded(window)
    window.node_list.setCurrentItem(window._items[src.id])
    window.place_beside()

    moved_id = window.doc.order[-1]
    assert window.doc.nodes[moved_id].inputs[0] == src.id


# --- navigation ---------------------------------------------------------------

def test_cycling_visits_the_machine_zero_and_back(window):
    src = loaded(window)
    window.doc.add("transform", [src.id], offset_x=30.0)
    window.refresh_list()
    window.recompute()

    window._origin_focus = 0
    window.focus_next_origin()
    assert window._origin_focus == 1       # the placed one
    window.focus_next_origin()
    assert window._origin_focus == 0       # back to machine zero


def test_cycling_with_nothing_placed_does_nothing(window):
    loaded(window)
    window.focus_next_origin()             # must not raise
    assert window._origin_focus == 0
