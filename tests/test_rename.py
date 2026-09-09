"""Renaming an operation, from the list and from the parameter panel.

The interesting parts are not that a name changes. They are that one Qt signal
carries two different edits -- the checkbox and the in-place text edit -- and
has to tell them apart; that a rename is undoable like every other edit; and
that editingFinished fires on focus loss, so a panel field that has not
actually changed must not land an entry on the undo stack.

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
    # Every test here dirties the document, and closing a dirty window asks
    # about unsaved work. A modal dialog never answers itself under offscreen
    # Qt, so the suite would hang rather than fail. Clear the flag first.
    w._mark(False)
    w.close()


def add_node(window, op="load_gerber"):
    node = window.doc.add(op)
    window.refresh_list(select=node.id)
    return node


def row_for(window, node_id):
    for row in range(window.node_list.count()):
        item = window.node_list.item(row)
        if item.data(Qt.UserRole) == node_id:
            return item
    raise AssertionError(f"no row for {node_id}")


# --- renaming from the list --------------------------------------------------

def test_editing_the_row_renames_the_node(window):
    node = add_node(window)
    row_for(window, node.id).setText("Top copper")
    assert window.doc.nodes[node.id].name == "Top copper"


def test_a_blank_name_falls_back_to_the_id(window):
    node = add_node(window)
    item = row_for(window, node.id)
    item.setText("   ")
    assert window.doc.nodes[node.id].name == node.id
    # The row must show the fallback, not the blank the user typed.
    assert item.text() == node.id


def test_renaming_is_undoable(window):
    node = add_node(window)
    original = node.name
    row_for(window, node.id).setText("Isolation pass")
    assert window.doc.nodes[node.id].name == "Isolation pass"
    window.undo()
    assert window.doc.nodes[node.id].name == original


# --- the checkbox must still work --------------------------------------------

def test_unchecking_still_hides_without_renaming(window):
    node = add_node(window)
    item = row_for(window, node.id)
    before = window.doc.nodes[node.id].name
    item.setCheckState(Qt.Unchecked)
    assert window.doc.nodes[node.id].visible is False
    assert window.doc.nodes[node.id].name == before
    item.setCheckState(Qt.Checked)
    assert window.doc.nodes[node.id].visible is True


def test_renaming_does_not_disturb_visibility(window):
    node = add_node(window)
    row_for(window, node.id).setCheckState(Qt.Unchecked)
    row_for(window, node.id).setText("Hidden thing")
    assert window.doc.nodes[node.id].visible is False


# --- renaming from the parameter panel ---------------------------------------

def test_panel_rename_reaches_the_list(window):
    node = add_node(window)
    window._on_rename(node.id, "From the panel")
    assert window.doc.nodes[node.id].name == "From the panel"
    assert row_for(window, node.id).text() == "From the panel"


def test_unchanged_panel_name_is_not_an_undo_step(window):
    node = add_node(window)
    depth = len(window._undo)
    # editingFinished fires whenever the field loses focus, changed or not.
    window._on_rename(node.id, node.name)
    assert len(window._undo) == depth


def test_panel_rename_is_one_undo_step(window):
    node = add_node(window)
    depth = len(window._undo)
    window._on_rename(node.id, "Named once")
    assert len(window._undo) == depth + 1


# --- the name survives a round trip ------------------------------------------

def test_name_survives_save_and_load(window):
    from aaltocam.core.graph import Document

    node = add_node(window)
    row_for(window, node.id).setText("Persisted name")
    restored = Document.from_dict(window.doc.to_dict())
    assert restored.nodes[node.id].name == "Persisted name"
