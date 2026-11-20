"""The file that is exported is the job as it stands now.

Since evaluation moved to a worker thread, an edit no longer produces its
G-code on the spot: the debounce waits 180 ms, the worker takes as long as the
board takes, and until it lands the previous result is still sitting in
`results`. Export used to write whatever was there. Change Cut Z from -0.10 to
-0.05, press Ctrl+E before the worker answers, and the file said Z-0.1 -- a
well-formed program for a depth nobody asked for, discovered in the copper.

The same thing happened three other ways, each pinned down below: the save
dialog took the result before it opened, so anything that changed while it was
up was ignored; a number typed into a spin box is not committed until Enter or
focus loss, and a shortcut gives neither; and the control channel's `export`
and `gcode` verbs read `results` exactly as the menu did.

Needs PySide6 and runs headless, so it skips where the GUI extra is absent.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from aaltocam.core import settings                    # noqa: E402
from aaltocam.core.graph import Document              # noqa: E402
from aaltocam.gui import remote                       # noqa: E402
from aaltocam.gui.app import MainWindow               # noqa: E402

DEMO_COPPER = str(Path(__file__).resolve().parents[1]
                  / "examples" / "demo" / "demo-F_Cu.gbr")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def board(app, tmp_path, monkeypatch):
    """Copper, one isolation pass and a job cutting at -0.10, evaluated.

    The job keeps its own feeds, so Cut Z on the job is the depth that is cut
    and no tool in the library can quietly take it over.
    """
    monkeypatch.setattr(settings, "config_path",
                        lambda: str(tmp_path / "settings.toml"))
    window = MainWindow()
    copper = window.doc.add("load_gerber", path=DEMO_COPPER)
    isolation = window.doc.add("isolate", [copper.id], passes=1)
    job = window.doc.add("cnc_job", [isolation.id], cut_z=-0.1,
                         feeds_from_tool=False)
    window.refresh_list(select=job.id)
    window.recompute()
    assert deepest(window.results[job.id].data) == -0.1
    yield window, job
    window._mark(False)   # a modal "unsaved work" dialog never answers itself
    window.close()


def deepest(gcode: str) -> float:
    """The lowest Z any move in the program goes to."""
    depths = [float(z) for line in gcode.splitlines()
              if re.match(r"G0?[0-3]\b", line)
              for z in re.findall(r"Z(-?\d+(?:\.\d+)?)", line)]
    return min(depths)


def save_to(monkeypatch, path, while_open=None):
    """Answer the save dialog with `path`, doing `while_open` first.

    The real dialog runs its own event loop, so timers fire and worker results
    land while it is up. `while_open` stands in for whatever happened then.
    """
    opened = []

    def dialog(*_args, **_kwargs):
        opened.append(True)
        if while_open is not None:
            while_open()
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", dialog)
    return opened


def collect_warnings(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *args, **_kw: shown.append(args))
    return shown


# --- the menu ----------------------------------------------------------------

def test_an_edit_still_waiting_for_its_debounce_is_exported(board, monkeypatch, tmp_path):
    window, job = board
    window._on_param_changed(job.id, "cut_z", -0.05)
    assert window.timer.isActive()      # nothing has evaluated it yet

    out = tmp_path / "pending.nc"
    save_to(monkeypatch, out)
    window.export_gcode()

    assert deepest(out.read_text(encoding="utf-8")) == -0.05


def test_a_change_made_while_the_dialog_is_open_is_exported(board, monkeypatch, tmp_path):
    window, job = board
    commands = remote.Commands(window)

    # The control channel answers on the GUI thread off a timer, and the save
    # dialog's own event loop keeps timers running.
    out = tmp_path / "during.nc"
    save_to(monkeypatch, out, lambda: commands.run(
        {"command": "set", "id": job.id, "params": {"cut_z": -0.05}}))
    window.export_gcode()

    assert deepest(out.read_text(encoding="utf-8")) == -0.05


def test_a_worker_landing_while_the_dialog_is_open_is_not_ignored(board, app, monkeypatch, tmp_path):
    window, job = board
    window._on_param_changed(job.id, "cut_z", -0.05)

    def worker_lands():
        window.timer.stop()
        window.start_recompute()
        for worker in list(window._workers):
            worker.wait(10000)
        app.processEvents()

    out = tmp_path / "landed.nc"
    save_to(monkeypatch, out, worker_lands)
    window.export_gcode()

    assert deepest(out.read_text(encoding="utf-8")) == -0.05


def test_a_number_typed_but_not_entered_is_exported(board, monkeypatch, tmp_path):
    window, job = board
    spin = window.form._widgets["cut_z"]
    # Keyboard tracking is off, so typing alone changes nothing until Enter
    # or focus loss -- and Ctrl+E is neither.
    # Typed the way the box writes it, which follows the locale: a Finnish
    # desktop shows -0,050, and a full stop there is not a number at all.
    spin.lineEdit().setText(spin.textFromValue(-0.05))
    assert job.params["cut_z"] == -0.1

    out = tmp_path / "typed.nc"
    save_to(monkeypatch, out)
    window.export_gcode()

    assert job.params["cut_z"] == -0.05
    assert deepest(out.read_text(encoding="utf-8")) == -0.05


def test_a_job_that_no_longer_evaluates_is_not_written(board, monkeypatch, tmp_path):
    window, job = board
    copper = window.doc.nodes[job.inputs[0]].inputs[0]
    window._on_param_changed(copper, "path", str(tmp_path / "missing.gbr"))
    # The last good result is still in `results`; it must not be what is cut.

    out = tmp_path / "existing.nc"
    out.write_text("the previous file", encoding="utf-8")
    opened = save_to(monkeypatch, out)
    shown = collect_warnings(monkeypatch)
    window.export_gcode()

    assert out.read_text(encoding="utf-8") == "the previous file"
    assert shown
    assert not opened      # refused before asking where to put it


def test_breaking_the_job_while_the_dialog_is_open_writes_nothing(board, monkeypatch, tmp_path):
    window, job = board
    copper = window.doc.nodes[job.inputs[0]].inputs[0]

    out = tmp_path / "existing.nc"
    out.write_text("the previous file", encoding="utf-8")
    save_to(monkeypatch, out, lambda: window.doc.set_param(
        copper, "path", str(tmp_path / "missing.gbr")))
    shown = collect_warnings(monkeypatch)
    window.export_gcode()

    assert out.read_text(encoding="utf-8") == "the previous file"
    assert shown


def test_cancelling_writes_nothing(board, monkeypatch, tmp_path):
    window, job = board
    window._on_param_changed(job.id, "cut_z", -0.05)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    window.export_gcode()

    assert list(tmp_path.glob("*.nc")) == []


def test_an_older_worker_cannot_replace_what_was_exported(board, app, monkeypatch, tmp_path):
    window, job = board

    # A worker starts on the -0.10 job and is held there...
    started = threading.Event()
    allowed = threading.Event()
    real = Document.evaluate_all

    def held(self):
        if threading.current_thread() is not threading.main_thread():
            started.set()
            allowed.wait(10.0)
        return real(self)

    monkeypatch.setattr(Document, "evaluate_all", held)
    window.doc.set_param(job.id, "travel_z", 3.0)
    window.start_recompute()
    assert started.wait(5.0)

    # ...the depth is changed and exported while it is still running...
    window._on_param_changed(job.id, "cut_z", -0.05)
    out = tmp_path / "fresh.nc"
    save_to(monkeypatch, out)
    window.export_gcode()

    # ...and when it lands, it is the stale one and is dropped.
    allowed.set()
    for worker in list(window._workers):
        worker.wait(10000)
    app.processEvents()

    assert deepest(out.read_text(encoding="utf-8")) == -0.05
    assert deepest(window.results[job.id].data) == -0.05


def test_an_up_to_date_job_is_not_evaluated_again(board, monkeypatch, tmp_path):
    """Exporting what is already current costs nothing.

    A real board takes half a minute to evaluate. Doing that on every export,
    on the GUI thread, would bring back the frozen window the worker was
    written to remove.
    """
    window, job = board
    calls = []
    real = Document.evaluate_all
    monkeypatch.setattr(Document, "evaluate_all",
                        lambda self: calls.append(1) or real(self))

    out = tmp_path / "current.nc"
    save_to(monkeypatch, out)
    window.export_gcode()

    assert deepest(out.read_text(encoding="utf-8")) == -0.1
    assert calls == []


# --- the control channel -------------------------------------------------------

def test_the_control_channel_exports_an_edit_made_in_the_window(board, tmp_path):
    window, job = board
    window._on_param_changed(job.id, "cut_z", -0.05)
    commands = remote.Commands(window)

    out = tmp_path / "remote.nc"
    answer = commands.run({"command": "export", "id": job.id, "path": str(out)})

    assert answer["ok"] is True
    assert deepest(out.read_text(encoding="utf-8")) == -0.05


def test_the_control_channel_reads_an_edit_made_in_the_window(board):
    window, job = board
    window._on_param_changed(job.id, "cut_z", -0.05)
    commands = remote.Commands(window)

    answer = commands.run({"command": "gcode", "id": job.id})

    assert answer["ok"] is True
    assert deepest(answer["gcode"]) == -0.05
