"""Setting up the second side, and saying when a job is not on machine zero.

The offset warning is the important half. Place beside the board moves geometry
for real, which keeps the screen and the G-code agreeing -- but agreeing is not
the same as being right for the machine. A job built from geometry that was
moved aside for viewing will cut where the board is not, and the only thing
standing between that and a broken cutter is the job saying so.

Needs PySide6 for the action; the warning itself is checked without a GUI.
"""

from __future__ import annotations

import os
import re

import pytest

from aaltocam.core import ops
from aaltocam.core.graph import Document

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "examples", "demo", "demo-F_Cu.gbr")


def built(doc, node_id):
    return doc.evaluate(node_id)


def xs_of(text):
    """X values from motion lines only.

    The header now carries the offset as a comment, and counting that as a
    coordinate would have the file appear to reach somewhere it never moves to.
    """
    out = []
    for line in text.splitlines():
        if re.match(r"G0?[0-3]\b", line):
            out += [float(m) for m in re.findall(r"X(-?\d+\.?\d*)", line)]
    return out


# --- the offset warning -------------------------------------------------------

def chain(offset_x=0.0, mirror="none", aside=True):
    doc = Document()
    copper = doc.add("load_gerber", path=DEMO)
    moved = doc.add("transform", [copper.id], offset_x=offset_x, mirror=mirror,
                    aside=aside)
    routed = doc.add("isolate", [moved.id])
    job = doc.add("cnc_job", [routed.id])
    return doc, copper, job


def test_a_job_on_machine_zero_says_nothing_about_offsets():
    doc, _, job = chain(offset_x=0.0)
    out = doc.evaluate(job.id)
    assert not any("offset from machine zero" in w
                   for w in out.meta.get("warnings", []))


def test_an_offset_job_warns():
    doc, _, job = chain(offset_x=25.5)
    out = doc.evaluate(job.id)
    assert any("offset from machine zero" in w
               for w in out.meta.get("warnings", []))


def test_the_warning_names_where_zero_went():
    doc, _, job = chain(offset_x=25.5)
    warning = next(w for w in doc.evaluate(job.id).meta["warnings"]
                   if "offset" in w)
    assert "25.500" in warning


def test_the_gcode_itself_carries_the_offset():
    """By the time a file reaches the machine nobody is reading the screen."""
    doc, _, job = chain(offset_x=25.5)
    text = doc.evaluate(job.id).data
    header = text.splitlines()[:6]
    assert any("zero the machine at X25.5" in line for line in header)


def test_a_job_on_zero_has_no_such_comment():
    doc, _, job = chain(offset_x=0.0)
    assert "zero the machine at" not in doc.evaluate(job.id).data


def test_an_offset_not_meant_as_a_second_area_does_not_warn():
    """Nudging a layer is not parking it. Only 'aside' claims a second zero."""
    doc, _, job = chain(offset_x=25.5, aside=False)
    out = doc.evaluate(job.id)
    assert not any("offset from machine zero" in w
                   for w in out.meta.get("warnings", []))


def test_the_offset_is_real_in_the_coordinates():
    """The warning is not cosmetic: the cuts really are somewhere else."""
    on_zero, _, job_a = chain(offset_x=0.0)
    aside, _, job_b = chain(offset_x=25.5)
    here = xs_of(on_zero.evaluate(job_a.id).data)
    there = xs_of(aside.evaluate(job_b.id).data)
    assert min(there) - min(here) == pytest.approx(25.5, abs=0.05)


def test_mirroring_in_place_stays_on_the_board():
    """The case that must not warn: the bottom side, cut where the board is."""
    doc = Document()
    copper = doc.add("load_gerber", path=DEMO)
    pins = doc.add("alignment_holes", [copper.id])
    flipped = doc.add("transform", [copper.id, "", pins.id],
                      mirror="y", mirror_about="alignment holes")
    routed = doc.add("isolate", [flipped.id])
    job = doc.add("cnc_job", [routed.id])

    out = doc.evaluate(job.id)
    board = ops.payload_bounds(doc.evaluate(copper.id))
    cuts = xs_of(out.data)
    assert min(cuts) >= board[0] - 1 and max(cuts) <= board[2] + 1
    assert not any("offset from machine zero" in w
                   for w in out.meta.get("warnings", []))


# --- the action ---------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aaltocam.gui.app import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow()
    yield w
    w._mark(False)
    w.close()


def with_copper(window):
    node = window.doc.add("load_gerber", path=DEMO, name="Top copper")
    window.refresh_list(select=node.id)
    window.recompute()
    return node


def by_op(window, op):
    return [n for n in window.doc.order if window.doc.nodes[n].op == op]


def test_it_builds_the_whole_second_side(window):
    with_copper(window)
    window.setup_bottom_side()

    assert len(by_op(window, "alignment_holes")) == 1
    assert len(by_op(window, "transform")) == 1
    assert len(by_op(window, "isolate")) == 1
    # One job to drill the pins, one to cut the bottom.
    assert len(by_op(window, "cnc_job")) == 2


def test_the_mirror_is_locked_to_the_pins(window):
    with_copper(window)
    window.setup_bottom_side()

    flip = window.doc.nodes[by_op(window, "transform")[0]]
    pins = by_op(window, "alignment_holes")[0]
    assert flip.params["mirror"] == "y"
    assert flip.params["mirror_about"] == "alignment holes"
    assert flip.inputs[2] == pins


def test_the_bottom_side_is_not_moved_aside(window):
    """In place is what the machine needs after the board is flipped."""
    copper = with_copper(window)
    window.setup_bottom_side()

    flip = by_op(window, "transform")[0]
    board = ops.payload_bounds(window.results[copper.id])
    bottom = ops.payload_bounds(window.results[flip])
    assert bottom == pytest.approx(board, abs=0.01)


def test_everything_it_builds_evaluates(window):
    with_copper(window)
    window.setup_bottom_side()

    for node_id in window.doc.order:
        assert not isinstance(window.results.get(node_id), Exception), \
            f"{window.doc.nodes[node_id].name}: {window.results.get(node_id)}"


def test_it_is_one_undo_step(window):
    with_copper(window)
    before = len(window.doc.order)
    window.setup_bottom_side()
    assert len(window.doc.order) > before

    window.undo()
    assert len(window.doc.order) == before


def test_it_refuses_a_layer_that_is_not_copper(window):
    node = window.doc.add("load_heightmap", name="Height map")
    window.refresh_list(select=node.id)
    window.recompute()
    before = len(window.doc.order)

    window.setup_bottom_side()
    assert len(window.doc.order) == before


def test_it_needs_a_selection(window):
    window.node_list.setCurrentItem(None)
    window.setup_bottom_side()
    assert window.doc.order == []
