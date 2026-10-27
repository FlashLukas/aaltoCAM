"""Evaluating without freezing the window.

A real board with V-bit rest machining takes about half a minute to evaluate,
and while it did so on the GUI thread the window was simply gone: no panning,
no reading the G-code that was already there, no answering the control channel
-- a command sent over that channel timed out and reported failure for work
that had in fact succeeded, because nothing was awake to answer.

So the debounced recompute now runs on a worker thread. The three things that
can go wrong with that, and are pinned down here, are: it evaluates a copy, so
an edit made while it runs cannot change the graph underneath it; it carries
the content-hash cache across, because a thread that rebuilds everything from
scratch on each keystroke is no improvement; and a result that arrives after a
newer run has started is dropped rather than painted over fresher geometry.

`recompute` stays synchronous and is not touched by any of this. Callers that
ask for an answer and read it on the next line -- the control channel, the
tests -- still get one.

Needs PySide6 and runs headless, so it skips where the GUI extra is absent.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication            # noqa: E402

from aaltocam.core.graph import Document              # noqa: E402
from aaltocam.gui.app import MainWindow               # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow()
    yield w
    w._mark(False)        # a modal "unsaved work" dialog never answers itself
    w.close()


def pump(app, predicate, timeout=10.0):
    """Run the event loop until `predicate` holds, or give up and say so."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def gate(monkeypatch):
    """Make evaluation block until released, and record where it ran.

    Returns (release, record). `record["thread"]` is the thread evaluation
    actually happened on, which is the whole point of the exercise.
    """
    started = threading.Event()
    allowed = threading.Event()
    record: dict = {}
    real = Document.evaluate_all

    def waiting(self):
        record["thread"] = threading.get_ident()
        started.set()
        allowed.wait(10.0)
        return real(self)

    monkeypatch.setattr(Document, "evaluate_all", waiting)
    return started, allowed, record


# --- it does not happen on the GUI thread ------------------------------------

def test_the_window_is_not_blocked_while_it_evaluates(window, app, monkeypatch):
    started, allowed, record = gate(monkeypatch)

    window.start_recompute()
    # The call has to come straight back; that is the entire feature.
    assert started.wait(5.0)
    assert record["thread"] != threading.get_ident()

    allowed.set()
    assert pump(app, lambda: not window._workers)


def test_recompute_still_answers_on_the_calling_thread(window, monkeypatch):
    seen = {}
    real = Document.evaluate_all

    def noting(self):
        seen["thread"] = threading.get_ident()
        return real(self)

    monkeypatch.setattr(Document, "evaluate_all", noting)
    window.recompute()
    # Synchronous by design: the control channel reads `results` next line.
    assert seen["thread"] == threading.get_ident()
    assert window.results is not None


# --- what the worker is given --------------------------------------------------

def test_the_worker_gets_a_copy_with_the_cache_carried_across(window, monkeypatch):
    window.doc.base_dir = "somewhere"
    window.doc._cache["x1"] = "cached payload"
    window.doc._keys["x1"] = "key"

    handed = {}
    monkeypatch.setattr(Document, "evaluate_all",
                        lambda self: handed.setdefault("doc", self) and {})

    window.start_recompute()
    for worker in list(window._workers):
        worker.wait(5000)
    snapshot = handed["doc"]

    assert snapshot is not window.doc          # an edit cannot change it
    # Without the cache a move to a worker thread would have traded a frozen
    # window for a full rebuild on every keystroke.
    assert snapshot._cache["x1"] == "cached payload"
    assert snapshot._keys["x1"] == "key"
    assert snapshot.base_dir == "somewhere"    # files still resolve


# --- a stale answer is thrown away --------------------------------------------

def test_a_superseded_result_is_discarded(window, app):
    window.results = {"sentinel": "fresh"}
    stale = {"sentinel": "stale"}

    generation = window._eval_generation
    window._eval_generation += 1               # as a later edit would
    window._evaluation_done(generation, stale, {}, {})

    assert window.results == {"sentinel": "fresh"}


def test_a_current_result_is_applied(window, app):
    window.results = {}
    window._eval_generation += 1
    window._evaluation_done(window._eval_generation, {"n1": "payload"},
                            {"n1": "payload"}, {"n1": "key"})

    assert window.results == {"n1": "payload"}
    assert window.doc._cache["n1"] == "payload"


def test_recompute_supersedes_a_run_already_in_flight(window, app, monkeypatch):
    started, allowed, _ = gate(monkeypatch)
    window.start_recompute()
    assert started.wait(5.0)

    monkeypatch.undo()                 # let the synchronous one through
    window.recompute()
    window.results["sentinel"] = "from recompute"
    allowed.set()

    for worker in list(window._workers):
        worker.wait(5000)
    app.processEvents()
    # The worker's answer lands after recompute's and must not replace it.
    # Anything else means a synchronous evaluation -- a control-channel
    # command, say -- can be quietly undone by a run that started earlier.
    assert window.results.get("sentinel") == "from recompute"


def test_the_debounce_timer_takes_the_worker_path(window):
    # The timer is what typing in the panel starts, so this is the path that
    # has to be the non-blocking one. Checked by disconnecting: PySide6
    # warns when asked to disconnect a slot that was never connected, and
    # says nothing when the disconnection works.
    window.schedule()
    assert window.timer.isActive()
    window.timer.stop()

    window.timer.timeout.disconnect(window.start_recompute)   # connected
    with pytest.warns(RuntimeWarning):
        window.timer.timeout.disconnect(window.recompute)      # and not this
    window.timer.timeout.connect(window.start_recompute)
