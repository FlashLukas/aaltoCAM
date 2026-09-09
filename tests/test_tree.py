"""The operation list's shape.

A project is a DAG, not a tree, and the list has to draw it as one anyway. The
rule is that nesting follows the primary input -- slot 0 -- so every node
appears exactly once. What is worth pinning down is the awkward shapes: a node
feeding two branches, a node feeding several consumers through their *second*
input, one whose primary input is not connected at all, and a cycle, which
should still be listed rather than vanishing.

Needs PySide6 and runs headless, so it skips where the GUI extra is absent.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
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


def shape(window):
    """The tree as {node id: parent node id or None}."""
    out = {}

    def walk(item, parent_id):
        node_id = item.data(0, Qt.UserRole)
        out[node_id] = parent_id
        for i in range(item.childCount()):
            walk(item.child(i), node_id)

    root = window.node_list.invisibleRootItem()
    for i in range(root.childCount()):
        walk(root.child(i), None)
    return out


def note_for(window, node_id):
    return window._items[node_id].text(1)


# --- the ordinary case --------------------------------------------------------

def test_a_chain_nests(window):
    cu = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [cu.id])
    job = window.doc.add("cnc_job", [iso.id])
    window.refresh_list()

    assert shape(window) == {cu.id: None, iso.id: cu.id, job.id: iso.id}


def test_one_source_can_feed_two_branches(window):
    cu = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [cu.id])
    cut = window.doc.add("cutout", [cu.id])
    window.refresh_list()

    tree = shape(window)
    assert tree[iso.id] == cu.id and tree[cut.id] == cu.id
    # Once, not once per consumer.
    assert list(tree).count(cu.id) == 1


def test_every_node_appears_exactly_once(window):
    cu = window.doc.add("load_gerber")
    window.doc.add("isolate", [cu.id])
    window.doc.add("cutout", [cu.id])
    window.refresh_list()

    seen = []

    def walk(item):
        seen.append(item.data(0, Qt.UserRole))
        for i in range(item.childCount()):
            walk(item.child(i))

    root = window.node_list.invisibleRootItem()
    for i in range(root.childCount()):
        walk(root.child(i))
    assert len(seen) == len(set(seen)) == len(window.doc.order)


# --- secondary inputs ---------------------------------------------------------

def test_a_secondary_input_does_not_move_the_node(window):
    cu = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [cu.id])
    job = window.doc.add("cnc_job", [iso.id])
    hm = window.doc.add("load_heightmap", name="Height map")
    job.inputs = [iso.id, hm.id]
    window.refresh_list()

    tree = shape(window)
    # The job stays under its toolpaths; the map stays a root of its own.
    assert tree[job.id] == iso.id
    assert tree[hm.id] is None


def test_a_secondary_input_is_named_beside_the_node(window):
    cu = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [cu.id])
    job = window.doc.add("cnc_job", [iso.id])
    hm = window.doc.add("load_heightmap", name="Bed map")
    job.inputs = [iso.id, hm.id]
    window.refresh_list()

    assert "Bed map" in note_for(window, job.id)


def test_one_map_shared_by_three_jobs_appears_once(window):
    hm = window.doc.add("load_heightmap", name="Bed map")
    cu = window.doc.add("load_gerber")
    jobs = []
    for _ in range(3):
        iso = window.doc.add("isolate", [cu.id])
        job = window.doc.add("cnc_job", [iso.id])
        job.inputs = [iso.id, hm.id]
        jobs.append(job)
    window.refresh_list()

    tree = shape(window)
    assert tree[hm.id] is None
    assert sum(1 for k in tree if k == hm.id) == 1
    for job in jobs:
        assert "Bed map" in note_for(window, job.id)


def test_renaming_updates_the_note_on_its_consumers(window):
    cu = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [cu.id])
    job = window.doc.add("cnc_job", [iso.id])
    hm = window.doc.add("load_heightmap", name="Bed map")
    job.inputs = [iso.id, hm.id]
    window.refresh_list()

    window._items[hm.id].setText(0, "Probed surface")
    assert "Probed surface" in note_for(window, job.id)


# --- half-wired and broken graphs ---------------------------------------------

def test_an_unconnected_primary_input_makes_a_root(window):
    cu = window.doc.add("load_gerber")
    orphan = window.doc.add("isolate")          # nothing wired in
    window.refresh_list()

    tree = shape(window)
    assert tree[orphan.id] is None
    assert tree[cu.id] is None


def test_a_node_wired_only_on_its_second_slot_is_a_root(window):
    hm = window.doc.add("load_heightmap")
    job = window.doc.add("cnc_job")
    job.inputs = ["", hm.id]
    window.refresh_list()

    assert shape(window)[job.id] is None


def test_a_dangling_reference_does_not_lose_the_node(window):
    iso = window.doc.add("isolate")
    iso.inputs = ["deleted_node_7"]
    window.refresh_list()

    assert shape(window)[iso.id] is None


def test_a_cycle_is_still_listed(window):
    a = window.doc.add("transform")
    b = window.doc.add("transform")
    a.inputs = [b.id]
    b.inputs = [a.id]
    window.refresh_list()

    tree = shape(window)
    assert a.id in tree and b.id in tree


# --- the tree stays in step ---------------------------------------------------

def test_rewiring_moves_the_branch(window):
    first = window.doc.add("load_gerber")
    second = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [first.id])
    window.refresh_list()
    assert shape(window)[iso.id] == first.id

    window._on_input_changed(iso.id, 0, second.id)
    assert shape(window)[iso.id] == second.id


def test_selection_survives_a_rebuild(window):
    cu = window.doc.add("load_gerber")
    iso = window.doc.add("isolate", [cu.id])
    window.refresh_list(select=iso.id)
    assert window.current_node().id == iso.id


def test_children_are_expanded_so_nothing_hides(window):
    cu = window.doc.add("load_gerber")
    window.doc.add("isolate", [cu.id])
    window.refresh_list()
    assert window._items[cu.id].isExpanded()
