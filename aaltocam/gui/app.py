"""aaltocam main window."""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import REGISTRY, Document
from ..core import discover
from ..core import project as project_io
from .canvas import BoardView
from .paramform import ParamForm

RECOMPUTE_DELAY_MS = 180


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("aaltocam")
        self.resize(1400, 900)
        self.doc = Document()
        self.path: str | None = None
        self.results: dict = {}
        self._undo: list[dict] = []
        self._redo: list[dict] = []

        self.view = BoardView()
        self.gcode_view = QPlainTextEdit()
        self.gcode_view.setReadOnly(True)
        self.gcode_view.setFont(QFont("monospace", 10))

        tabs = QTabWidget()
        tabs.addTab(self.view, "Board")
        tabs.addTab(self.gcode_view, "G-code")
        self.tabs = tabs
        self.setCentralWidget(tabs)

        self._build_node_dock()
        self._build_param_dock()
        self._build_menus()

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.recompute)

        self.view.shape_drawn.connect(self._on_shape_drawn)
        self.view.shapes_edited.connect(self._on_shapes_edited)
        self.view.draw_finished.connect(
            lambda: self.statusBar().showMessage("Drawing cancelled", 2000))
        self.view.cursor_moved.connect(
            lambda x, y: self.statusBar().showMessage(f"X {x:.3f}   Y {y:.3f}", 2000))
        self.statusBar().showMessage("New project. Add a Gerber file to start.")

    # -- docks -------------------------------------------------------------

    def _build_node_dock(self):
        dock = QDockWidget("Operations", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        self.node_list = QListWidget()
        self.node_list.currentItemChanged.connect(self._on_select)
        self.node_list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.node_list, 1)

        row = QHBoxLayout()
        add_button = QPushButton("Add")
        add_button.setMenu(self._add_menu())
        duplicate = QPushButton("Duplicate")
        duplicate.clicked.connect(self.duplicate_node)
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete_node)
        for widget in (add_button, duplicate, delete):
            row.addWidget(widget)
        layout.addLayout(row)

        dock.setWidget(panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _add_menu(self) -> QMenu:
        menu = QMenu(self)
        by_category: dict[str, list] = {}
        for name, operation in REGISTRY.items():
            by_category.setdefault(operation.category, []).append((name, operation))
        for category in ("Source", "CAM", "Edit", "Output"):
            entries = by_category.get(category)
            if not entries:
                continue
            sub = menu.addMenu(category)
            for name, operation in sorted(entries, key=lambda e: e[1].label):
                action = sub.addAction(operation.label)
                action.triggered.connect(lambda _c=False, n=name: self.add_node(n))
        return menu

    def _build_param_dock(self):
        dock = QDockWidget("Parameters", self)
        self.form = ParamForm()
        self.form.param_changed.connect(self._on_param_changed)
        self.form.input_changed.connect(self._on_input_changed)
        self.form.rename_requested.connect(self._on_rename)
        self.form.draw_requested.connect(self._start_draw)
        dock.setWidget(self.form)
        dock.setMinimumWidth(400)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        for label, slot, shortcut in (
            ("New project", self.new_project, QKeySequence.New),
            ("Open project...", self.open_project, QKeySequence.Open),
            ("Save project", self.save_project, QKeySequence.Save),
            ("Save project as...", self.save_project_as, QKeySequence.SaveAs),
        ):
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            file_menu.addAction(action)
        open_dir = QAction("Open board folder...", self)
        open_dir.setShortcut("Ctrl+Shift+O")
        open_dir.triggered.connect(self.open_board_folder)
        file_menu.addAction(open_dir)
        file_menu.addSeparator()
        export = QAction("Export G-code...", self)
        export.setShortcut("Ctrl+E")
        export.triggered.connect(self.export_gcode)
        file_menu.addAction(export)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        undo = QAction("Undo", self)
        undo.setShortcut(QKeySequence.Undo)
        undo.triggered.connect(self.undo)
        redo = QAction("Redo", self)
        redo.setShortcut(QKeySequence.Redo)
        redo.triggered.connect(self.redo)
        edit_menu.addAction(undo)
        edit_menu.addAction(redo)

        view_menu = self.menuBar().addMenu("&View")
        fit = QAction("Fit to board", self)
        fit.setShortcut("F")
        fit.triggered.connect(self.view.fit)
        view_menu.addAction(fit)
        travel = QAction("Show travel moves", self, checkable=True, checked=True)
        travel.triggered.connect(self._toggle_travel)
        view_menu.addAction(travel)
        snap = QAction("Snap shapes to 0.1 mm", self, checkable=True, checked=True)
        snap.triggered.connect(lambda on: setattr(self.view, "snap", 0.1 if on else 0.0))
        view_menu.addAction(snap)

    # -- undo --------------------------------------------------------------

    def push_undo(self):
        """Snapshot before a change. Every mutation funnels through the
        handlers below, so one call site per handler covers the lot."""
        self._undo.append(self.doc.to_dict())
        del self._undo[:-100]
        self._redo.clear()

    def _restore(self, snapshot, counterpart):
        counterpart.append(self.doc.to_dict())
        selected = self.current_node()
        base = self.doc.base_dir
        self.doc = Document.from_dict(snapshot)
        self.doc.base_dir = base
        self.refresh_list(select=selected.id if selected else None)
        self.recompute()

    def undo(self):
        if not self._undo:
            self.statusBar().showMessage("Nothing to undo", 2000)
            return
        self._restore(self._undo.pop(), self._redo)

    def redo(self):
        if not self._redo:
            self.statusBar().showMessage("Nothing to redo", 2000)
            return
        self._restore(self._redo.pop(), self._undo)

    # -- project -----------------------------------------------------------

    def new_project(self):
        self.doc = Document()
        self.path = None
        self.results = {}
        self.form.clear()
        self.refresh_list()
        self.view.clear_all()
        self.gcode_view.clear()
        self.setWindowTitle("aaltocam")

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", "aaltocam project (*.toml);;All files (*)")
        if not path:
            return
        try:
            self.doc = project_io.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not open project", str(exc))
            return
        self.path = path
        self.setWindowTitle(f"aaltocam - {os.path.basename(path)}")
        self.refresh_list()
        self.recompute()
        self.view.fit()

    def open_board_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "Open board folder")
        if not directory:
            return
        doc, notes = discover.build_board(directory)
        if not doc.order:
            QMessageBox.warning(self, "Open board folder", "\n".join(notes) or
                                "No Gerber or drill files recognised in that folder.")
            return
        self.doc = doc
        self.path = None
        self._undo.clear()
        self._redo.clear()
        self.setWindowTitle(f"aaltocam - {os.path.basename(directory.rstrip(os.sep))}")
        self.refresh_list()
        self.recompute()
        self.view.fit()
        QMessageBox.information(self, "Board loaded", "\n".join(notes))

    def save_project(self):
        if not self.path:
            return self.save_project_as()
        project_io.save(self.doc, self.path)
        self.statusBar().showMessage(f"Saved {os.path.basename(self.path)}", 4000)

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", "board.toml", "aaltocam project (*.toml)")
        if not path:
            return
        self.path = path
        self.setWindowTitle(f"aaltocam - {os.path.basename(path)}")
        self.save_project()

    def export_gcode(self):
        node = self.current_node()
        if node is None or node.operation().output != "gcode":
            QMessageBox.information(
                self, "Export G-code", "Select a CNC job first.")
            return
        result = self.results.get(node.id)
        if not hasattr(result, "data"):
            QMessageBox.warning(self, "Export G-code", "This job has not evaluated cleanly.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export G-code", f"{node.id}.nc", "G-code (*.nc *.gcode *.ngc);;All files (*)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(result.data)
        self.statusBar().showMessage(f"Wrote {os.path.basename(path)}", 5000)

    # -- node handling -----------------------------------------------------

    def current_node(self):
        item = self.node_list.currentItem()
        if item is None:
            return None
        return self.doc.nodes.get(item.data(Qt.UserRole))

    def add_node(self, op_name: str):
        operation = REGISTRY[op_name]
        inputs = []
        if operation.inputs:
            # Wire the first slot only; optional slots stay empty on purpose.
            kind = operation.slot_kind(0)
            current = self.current_node()
            source = ""
            if current is not None and self._accepts(current.operation().output, kind):
                source = current.id
            else:
                source = self._find_source(kind)
            inputs = [source] + [""] * (len(operation.inputs) - 1)
        self.push_undo()
        node = self.doc.add(op_name, inputs)
        self.refresh_list(select=node.id)
        self.schedule()

    @staticmethod
    def _accepts(output: str, kind: str) -> bool:
        if output == "any":
            return True
        if kind == "any":
            # "any" means any geometry; G-code is a dead end, never a source.
            return output != "gcode"
        return output == kind

    def _find_source(self, kind: str) -> str:
        for node_id in reversed(self.doc.order):
            if self._accepts(self.doc.nodes[node_id].operation().output, kind):
                return node_id
        return ""

    def duplicate_node(self):
        node = self.current_node()
        if node is None:
            return
        self.push_undo()
        clone = self.doc.add(node.op, list(node.inputs), name=f"{node.name} copy", **node.params)
        self.refresh_list(select=clone.id)
        self.schedule()

    def delete_node(self):
        node = self.current_node()
        if node is None:
            return
        self.push_undo()
        self.view.clear_node(node.id)
        self.doc.remove(node.id)
        self.form.clear()
        self.refresh_list()
        self.schedule()

    def refresh_list(self, select: str | None = None):
        self.node_list.blockSignals(True)
        self.node_list.clear()
        for node_id in self.doc.order:
            node = self.doc.nodes[node_id]
            item = QListWidgetItem(f"{node.name}")
            item.setData(Qt.UserRole, node_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if node.visible else Qt.Unchecked)
            self.node_list.addItem(item)
        self.node_list.blockSignals(False)
        if select:
            for row in range(self.node_list.count()):
                if self.node_list.item(row).data(Qt.UserRole) == select:
                    self.node_list.setCurrentRow(row)
                    break

    def _on_select(self, current, _previous):
        node = self.current_node()
        if node is None:
            self.form.clear()
            return
        self.form.show_node(self.doc, node)
        self._sync_editable(node)
        result = self.results.get(node.id)
        if hasattr(result, "kind") and result.kind == "gcode":
            self.gcode_view.setPlainText(result.data)
        self._show_stats(node)

    def _on_item_changed(self, item):
        node = self.doc.nodes.get(item.data(Qt.UserRole))
        if node is None:
            return
        node.visible = item.checkState() == Qt.Checked
        self.redraw()

    def _on_param_changed(self, node_id, name, value):
        param = self.doc.nodes[node_id].operation().param(name)
        if param is not None:
            value = param.coerce(value)
        self.push_undo()
        if self.doc.set_param(node_id, name, value):
            self.schedule()

    def _on_input_changed(self, node_id, slot, source_id):
        self.push_undo()
        node = self.doc.nodes[node_id]
        while len(node.inputs) <= slot:
            node.inputs.append("")
        node.inputs[slot] = source_id
        self.doc.invalidate(node_id)
        self.schedule()

    def _on_rename(self, node_id, name):
        self.doc.nodes[node_id].name = name or node_id
        self.refresh_list(select=node_id)

    def _toggle_travel(self, checked):
        self.view.show_travel = checked
        self.redraw()

    # -- drawing -----------------------------------------------------------

    def _start_draw(self, node_id, kind):
        self._draw_target = node_id
        self.tabs.setCurrentWidget(self.view)
        self.view.start_draw(kind)
        if kind == "rect":
            self.statusBar().showMessage("Drag a rectangle on the board. Escape cancels.")
        else:
            self.statusBar().showMessage(
                "Click polygon points. Double-click or right-click to close, Escape cancels.")

    def _sync_editable(self, node):
        """Handles appear only for the Region node you have selected."""
        if node is not None and "shapes" in node.params:
            self.view.set_editable(node.id, node.params.get("shapes", []))
        else:
            self.view.set_editable(None, [])

    def _on_shapes_edited(self, shapes):
        node_id = self.view._edit_node
        if not node_id or node_id not in self.doc.nodes:
            return
        self.push_undo()
        self.doc.set_param(node_id, "shapes", shapes)
        if self.form._node_id == node_id:
            self.form.set_shapes(shapes)
        self.schedule()

    def _on_shape_drawn(self, shape):
        node_id = getattr(self, "_draw_target", None)
        node = self.doc.nodes.get(node_id) if node_id else None
        if node is None:
            return
        shapes = list(node.params.get("shapes", [])) + [shape]
        self.push_undo()
        self.doc.set_param(node_id, "shapes", shapes)
        if self.form._node_id == node_id:
            self.form.add_shape(shape)
        self.view.set_editable(node_id, shapes)
        self.schedule()
        self.statusBar().showMessage(f"{len(shapes)} shape(s) in {node.name}", 4000)

    # -- evaluation --------------------------------------------------------

    def schedule(self):
        self.timer.start(RECOMPUTE_DELAY_MS)

    def recompute(self):
        start = time.perf_counter()
        QApplication.setOverrideCursor(Qt.BusyCursor)
        try:
            self.results = self.doc.evaluate_all()
        finally:
            QApplication.restoreOverrideCursor()
        elapsed = (time.perf_counter() - start) * 1000
        self.redraw()

        errors = [f"{self.doc.nodes[k].name}: {v}"
                  for k, v in self.results.items() if isinstance(v, Exception)]
        node = self.current_node()
        if node is not None:
            result = self.results.get(node.id)
            if hasattr(result, "kind") and result.kind == "gcode":
                self.gcode_view.setPlainText(result.data)
        if errors:
            self.statusBar().showMessage(errors[0])
        else:
            self.statusBar().showMessage(f"Evaluated in {elapsed:.0f} ms")
        if node is not None and not errors:
            self._show_stats(node)

    def _show_stats(self, node):
        result = self.results.get(node.id)
        if isinstance(result, Exception):
            self.statusBar().showMessage(f"{node.name}: {result}")
            return
        if not hasattr(result, "meta"):
            return
        meta = result.meta
        bits = []
        if "tool_diameter" in meta:
            bits.append(f"cut width {meta['tool_diameter']:.3f} mm")
        if "cut_length" in meta:
            bits.append(f"cutting {meta['cut_length']:.0f} mm")
        if "travel_length" in meta:
            bits.append(f"travel {meta['travel_length']:.0f} mm")
        if "holes" in meta:
            bits.append(f"{meta['holes']} holes")
        if "count" in meta:
            bits.append(f"{meta['count']} holes")
        if "openings" in meta:
            bits.append(f"{meta['openings']} opening(s)")
        if "contours" in meta:
            bits.append(f"{meta['contours']} contour(s)")
        if meta.get("too_small"):
            bits.append(f"{meta['too_small']} too small for the tool")
        if meta.get("no_room_for_tabs"):
            bits.append(f"{meta['no_room_for_tabs']} too small for tabs, cut fully")
        if "shorts" in meta:
            if meta["shorts"]:
                worst = meta["spots"][0] if meta.get("spots") else None
                where = f" (worst near X{worst[0]} Y{worst[1]})" if worst else ""
                bits.append(f"{meta['shorts']} unmillable gap(s){where}")
            else:
                bits.append("no unmillable gaps")
            if meta.get("rounded_corners"):
                bits.append(f"{meta['rounded_corners']} corners will be rounded")
        if meta.get("slots"):
            bits.append(f"{meta['slots']} slot(s) in file, not cut")
        if "minutes" in meta:
            bits.append(f"about {meta['minutes']:.1f} min")
        if "shapes" in meta:
            bits.append(f"{meta['shapes']} shape(s), {meta.get('area', 0):.1f} mm2")
        if meta.get("sent_to_milling"):
            bits.append(f"{meta['sent_to_milling']} sent to milling")
        if "holes_milled" in meta:
            bits.append(f"{meta['holes_milled']} holes milled")
        if meta.get("holes_too_small"):
            bits.append(f"{meta['holes_too_small']} too small for the tool")
        if meta.get("groups") and len(meta["groups"]) > 1:
            tools = ", ".join(f"{d:.2f}" for d, _ in meta["groups"])
            bits.insert(0, f"tools {tools} mm")
        if meta.get("uncleared_area", 0) > 0.01:
            bits.append(f"{meta['uncleared_area']:.1f} mm2 unreachable")
        if bits:
            self.statusBar().showMessage("   ".join(bits))

    def redraw(self):
        self.view.clear_all()
        for node_id in self.doc.order:
            node = self.doc.nodes[node_id]
            result = self.results.get(node_id)
            if not node.visible or not hasattr(result, "kind"):
                continue
            if result.kind == "copper":
                self.view.show_copper(node_id, result.data)
            elif result.kind == "region":
                self.view.show_region(node_id, result.data)
            elif result.kind == "alert":
                self.view.show_alert(node_id, result.data)
            elif result.kind == "paths":
                groups = result.meta.get("groups")
                if groups and len(groups) > 1:
                    self.view.show_path_groups(node_id, groups)
                else:
                    self.view.show_paths(node_id, result.data,
                                         result.meta.get("tool_diameter", 0.0))
            elif result.kind == "drills":
                self.view.show_drills(node_id, result.data)
        # clear_all() removed the edit handles; put them back on top.
        self.view.refresh_handles()


def dark_palette() -> QPalette:
    palette = QPalette()
    base = QColor("#171B21")
    panel = QColor("#1E242C")
    text = QColor("#DCE3EA")
    accent = QColor("#C87137")
    palette.setColor(QPalette.Window, panel)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, panel)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, panel)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.Highlight, accent)
    palette.setColor(QPalette.HighlightedText, QColor("#12161C"))
    palette.setColor(QPalette.ToolTipBase, panel)
    palette.setColor(QPalette.ToolTipText, text)
    return palette


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    window = MainWindow()

    files = [a for a in argv[1:] if not a.startswith("-")]
    if files and os.path.exists(files[0]):
        try:
            window.doc = project_io.load(files[0])
            window.path = files[0]
            window.refresh_list()
            window.recompute()
            window.view.fit()
        except Exception as exc:
            QMessageBox.critical(window, "Could not open project", str(exc))

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
