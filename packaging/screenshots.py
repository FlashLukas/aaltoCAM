"""Regenerate the screenshots in docs/screenshots/.

Rendered offscreen from the demo project, so the images are reproducible and
stay in step with the interface rather than being whatever happened to be on
someone's desktop the day they grabbed a window.

    python packaging/screenshots.py

Needs the GUI extra installed, and a real desktop session: it deliberately does
not force QT_QPA_PLATFORM=offscreen, because the offscreen platform has no
fonts and renders every label as empty boxes. The graphics come out fine and
the text does not, which is easy to miss until the images are already
committed.

Writes PNGs; commit them alongside the change that altered the interface.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aaltocam.core import project as project_io  # noqa: E402
from aaltocam.gui.app import MainWindow  # noqa: E402

OUT = REPO / "docs" / "screenshots"
DEMO = REPO / "examples" / "demo" / "demo.toml"
SIZE = (1500, 950)


def settle(app, rounds=6):
    """Let layout, the recompute timer and painting catch up."""
    for _ in range(rounds):
        app.processEvents()


def select(window, app, predicate):
    """Select the first node matching a predicate on its Node. Returns its id."""
    for node_id in window.doc.order:
        if predicate(window.doc.nodes[node_id]):
            window.refresh_list(select=node_id)
            settle(app)
            return node_id
    raise SystemExit("no node matched; has the demo project changed?")


def save(widget, app, name):
    settle(app)
    path = OUT / name
    if not widget.grab().save(str(path)):
        raise SystemExit(f"could not write {path}")
    print(f"  {name:<24} {path.stat().st_size / 1024:6.0f} KB")


def main() -> int:
    if not DEMO.exists():
        print(f"missing {DEMO}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(*SIZE)
    window.show()

    window.doc = project_io.load(str(DEMO))
    window.path = str(DEMO)
    window._mark(False, subject=DEMO.name)
    window.refresh_list()
    window.recompute()
    settle(app, 12)
    window.view.fit()

    print("writing screenshots:")

    # The board, with an isolation pass selected so the toolpath is on screen
    # next to the parameters that produced it.
    select(window, app, lambda n: n.op == "isolate")
    window.tabs.setCurrentWidget(window.view)
    save(window, app, "board.png")

    # The parameter panel on its own, where it is actually legible.
    save(window.form.parentWidget() or window.form, app, "parameters.png")

    # The operations list: the graph, and what you rename.
    save(window.node_list.parentWidget() or window.node_list, app, "operations.png")

    # G-code for a CNC job, the other half of the tabbed view.
    select(window, app, lambda n: n.operation().output == "gcode")
    window.tabs.setCurrentWidget(window.gcode_view)
    save(window, app, "gcode.png")

    window._mark(False)
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
