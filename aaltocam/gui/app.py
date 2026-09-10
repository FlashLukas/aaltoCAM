"""aaltocam main window."""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtCore import QUrl
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices,
                           QFont, QKeySequence, QPalette)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QRadioButton,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStyledItemDelegate,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import REGISTRY, Document, ops
from ..core import discover
from ..core import kicad
from ..core import tools as toollib
from ..core import project as project_io
from ..core import settings
from . import canvas
from . import icons
from .canvas import BoardView
from .paramform import ParamForm

RECOMPUTE_DELAY_MS = 180

#: Clearance left between a layer placed aside and everything already on the
#: bed. Small enough to keep both sides on screen at a working zoom, large
#: enough that the two never look like one board.
BESIDE_GAP_MM = 5.0


class _NameColumnOnly(QStyledItemDelegate):
    """Editing renames, and only the name column is a name.

    The second column notes which other operations feed this one. It is
    derived, so letting it be typed into would offer an edit that silently
    does nothing.
    """

    def createEditor(self, parent, option, index):
        if index.column() != 0:
            return None
        return super().createEditor(parent, option, index)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("aaltocam")
        self.setWindowIcon(icons.app_icon())
        self.resize(1400, 900)
        self.doc = Document()
        self.path: str | None = None
        self.results: dict = {}
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        #: Unsaved changes, so closing can offer to save them.
        self._dirty = False
        #: Label for the title bar when there is no project file yet.
        self._subject = ""

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
        self.view.cursor_moved.connect(self._show_cursor)
        self.view.measured.connect(self._show_measure)
        self.statusBar().showMessage("New project. Add a Gerber file to start.")

    # -- docks -------------------------------------------------------------

    def _build_node_dock(self):
        dock = QDockWidget("Operations", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        self.node_list = QTreeWidget()
        self.node_list.setColumnCount(2)
        self.node_list.setHeaderHidden(True)
        self.node_list.setIndentation(14)
        self.node_list.setUniformRowHeights(True)
        # Double-click renames rather than collapsing the branch. Expanding is
        # still on the arrow, and everything starts expanded anyway.
        self.node_list.setExpandsOnDoubleClick(False)
        self.node_list.setItemDelegate(_NameColumnOnly(self.node_list))
        # Not SelectedClicked, which starts an edit on a plain click of the row
        # you already had selected.
        self.node_list.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        header = self.node_list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        # Interactive, not ResizeToContents, so _fit_note_column can cap it.
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        self.node_list.currentItemChanged.connect(self._on_select)
        self.node_list.itemChanged.connect(self._on_item_changed)
        self._items: dict[str, QTreeWidgetItem] = {}
        self._origin_focus = 0
        layout.addWidget(self.node_list, 1)

        layout.addWidget(self._toolbox())

        row = QHBoxLayout()
        row.addStretch(1)
        for name, slot, tip in (
            ("duplicate", self.duplicate_node, "Duplicate the selected operation"),
            ("delete", self.delete_node, "Delete the selected operation"),
        ):
            button = QToolButton()
            button.setIcon(icons.action_icon(name, self._ink()))
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(30, 30)
            button.setAutoRaise(True)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)

        dock.setWidget(panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

    def _ink(self):
        """Line colour for the icons, taken from the running palette so the set
        stays legible whichever theme Qt picked."""
        return self.palette().color(QPalette.WindowText)

    #: Palette sections, in the order work actually flows: bring geometry in,
    #: move it about, cut it, write it out. The registry's own category names
    #: are an implementation detail, so they are renamed for the panel.
    TOOLBOX_GROUPS = (
        ("Source", "Add"),
        ("Edit", "Transform"),
        ("CAM", "CAM"),
        ("Output", "Export"),
    )

    def _toolbox(self) -> QWidget:
        """The operation palette: click a picture instead of hunting a submenu.

        Four to a row under a labelled rule, with the name and the help text on
        the tooltip -- a grid of unlabelled icons is only friendly once you
        already know it, and the rules are what stop thirteen pictures reading
        as one undifferentiated block.
        """
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 4, 0, 2)
        column.setSpacing(0)

        by_category: dict[str, list] = {}
        for name, operation in REGISTRY.items():
            by_category.setdefault(operation.category, []).append((name, operation))

        ink = self._ink()
        for position, (category, title) in enumerate(self.TOOLBOX_GROUPS):
            entries = by_category.get(category)
            if not entries:
                continue
            column.addWidget(self._toolbox_heading(title, first=position == 0))

            grid = QGridLayout()
            grid.setSpacing(2)
            grid.setContentsMargins(0, 2, 0, 6)
            for index, (name, operation) in enumerate(
                    sorted(entries, key=lambda e: e[1].label)):
                button = QToolButton()
                button.setIcon(icons.op_icon(name, ink))
                button.setIconSize(QSize(26, 26))
                button.setFixedSize(34, 34)
                button.setAutoRaise(True)
                tip = operation.label
                doc = (operation.func.__doc__ or "").strip().split("\n")[0]
                if doc:
                    tip += f"\n{doc}"
                button.setToolTip(tip)
                button.clicked.connect(lambda _c=False, n=name: self.add_node(n))
                grid.addWidget(button, index // 4, index % 4)
            grid.setColumnStretch(4, 1)
            column.addLayout(grid)
        return box

    def _toolbox_heading(self, title: str, first: bool = False) -> QWidget:
        """A section label with a rule running off to the right of it."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0 if first else 6, 2, 0)
        line.setSpacing(6)

        label = QLabel(title.upper())
        font = label.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        font.setBold(True)
        label.setFont(font)
        label.setStyleSheet("color: palette(bright-text);")
        line.addWidget(label)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setFrameShadow(QFrame.Plain)
        rule.setFixedHeight(1)
        rule.setStyleSheet("color: palette(mid); background: palette(mid);")
        line.addWidget(rule, 1)
        return row

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
        open_pcb = QAction("Open KiCad board...", self)
        open_pcb.setShortcut("Ctrl+Shift+K")
        open_pcb.triggered.connect(self.open_kicad_board)
        file_menu.addAction(open_pcb)
        replot = QAction("Re-plot from KiCad", self)
        replot.setShortcut("Ctrl+Shift+R")
        replot.triggered.connect(self.replot_kicad_board)
        file_menu.addAction(replot)
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
        rename = QAction("Rename operation", self)
        rename.setShortcut("F2")
        rename.triggered.connect(self.rename_current)
        beside = QAction("Place beside the board", self)
        beside.setShortcut("Ctrl+Shift+B")
        beside.triggered.connect(self.place_beside)
        edit_menu.addAction(undo)
        edit_menu.addAction(redo)
        edit_menu.addSeparator()
        edit_menu.addAction(rename)
        # Here rather than under View: it moves the geometry, so it belongs
        # with the things that change the project, not the camera.
        edit_menu.addAction(beside)

        tools_menu = self.menuBar().addMenu("&Tools")
        edit_tools = QAction("Edit tool library...", self)
        edit_tools.triggered.connect(self.edit_tool_library)
        tools_menu.addAction(edit_tools)
        reload_tools = QAction("Reload tool library", self)
        reload_tools.setShortcut("Ctrl+Shift+T")
        reload_tools.triggered.connect(self.reload_tool_library)
        tools_menu.addAction(reload_tools)

        view_menu = self.menuBar().addMenu("&View")
        fit = QAction("Fit to board", self)
        fit.setShortcut("F")
        fit.triggered.connect(self.view.fit)
        view_menu.addAction(fit)
        next_origin = QAction("Go to next origin", self)
        next_origin.setShortcut("O")
        next_origin.triggered.connect(self.focus_next_origin)
        view_menu.addAction(next_origin)

        view_menu.addSeparator()
        theme_menu = view_menu.addMenu("Theme")
        current = settings.load().get("theme", "dark")
        self._theme_actions = QActionGroup(self)
        self._theme_actions.setExclusive(True)
        for name in settings.THEMES:
            action = QAction(name.capitalize(), self, checkable=True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _c=False, n=name: self.choose_theme(n))
            self._theme_actions.addAction(action)
            theme_menu.addAction(action)
        travel = QAction("Show travel moves", self, checkable=True, checked=True)
        travel.triggered.connect(self._toggle_travel)
        view_menu.addAction(travel)
        snap = QAction("Snap shapes to 0.1 mm", self, checkable=True, checked=True)
        snap.triggered.connect(lambda on: setattr(self.view, "snap", 0.1 if on else 0.0))
        view_menu.addAction(snap)

        view_menu.addSeparator()
        self.measure_action = QAction("Measure", self, checkable=True)
        self.measure_action.setShortcut("M")
        self.measure_action.setToolTip(
            "Click two points. Shift constrains to one axis, right-click or "
            "Escape clears.")
        self.measure_action.triggered.connect(self._toggle_measure)
        view_menu.addAction(self.measure_action)
        self.clip_action = QAction("Measure from origin", self, checkable=True)
        self.clip_action.setToolTip("Pin one end of the measurement to X0 Y0.")
        self.clip_action.triggered.connect(self.view.set_clip_to_origin)
        view_menu.addAction(self.clip_action)

    # -- undo --------------------------------------------------------------

    def _update_title(self):
        name = os.path.basename(self.path) if self.path else (self._subject or "")
        star = "*" if self._dirty else ""
        self.setWindowTitle(f"aaltocam{' - ' + name if name else ''}{star}")

    def _mark(self, dirty=True, subject=None):
        """Record whether there is unsaved work, and retitle the window."""
        self._dirty = dirty
        if subject is not None:
            self._subject = subject
        self._update_title()

    def board_path(self) -> str:
        """The .kicad_pcb this project came from, remembered in the document
        itself so it survives save, load and undo."""
        return project_io.board_path(self.doc)

    def push_undo(self):
        """Snapshot before a change. Every mutation funnels through the
        handlers below, so one call site per handler covers the lot."""
        self._undo.append(self.doc.to_dict())
        del self._undo[:-100]
        self._redo.clear()
        self._mark(True)

    def _restore(self, snapshot, counterpart):
        counterpart.append(self.doc.to_dict())
        selected = self.current_node()
        base = self.doc.base_dir
        self.doc = Document.from_dict(snapshot)
        self.doc.base_dir = base
        self.refresh_list(select=selected.id if selected else None)
        self.recompute()
        self._mark(True)

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
        if not self._confirm_discard("Start a new project"):
            return
        self.doc = Document()
        self.path = None
        self.results = {}
        self.form.clear()
        self.refresh_list()
        self.view.clear_all()
        self.gcode_view.clear()
        self._mark(False, subject="")

    def open_project(self):
        if not self._confirm_discard("Open another project"):
            return
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
        self._mark(False, subject=os.path.basename(path))
        self.refresh_list()
        self.recompute()
        self.view.fit()

    def _ask_import(self, sides, title="Import board") -> dict | None:
        """Side and placement, asked once. None means the user cancelled.

        Both questions are about the same thing -- where the geometry ends up --
        so they belong in one dialog rather than a queue of message boxes.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)

        both = "top" in sides and "bottom" in sides
        top_button = QRadioButton("Top side")
        bottom_button = QRadioButton("Bottom side")
        top_button.setChecked("top" in sides)
        bottom_button.setChecked("bottom" in sides and "top" not in sides)
        if both:
            layout.addWidget(QLabel("This board has copper on both sides."))
            layout.addWidget(top_button)
            layout.addWidget(bottom_button)
            hint = QLabel("The bottom side is mirrored about Y, so the board is "
                          "turned over left to right.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray")
            layout.addWidget(hint)

        origin = QCheckBox("Move everything so the board's bottom-left corner is X0 Y0")
        origin.setChecked(True)
        layout.addWidget(origin)
        note = QLabel("Gerbers come out on KiCad's absolute origin, so the board "
                      "sits wherever it sat on the sheet, with negative Y. Copper, "
                      "outline and drills are all shifted by the same amount, "
                      "measured from the outline, and the untouched layers are "
                      "hidden so the board is not drawn twice.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        layout.addWidget(note)

        operations = QCheckBox("Also add the usual operations")
        operations.setChecked(True)
        layout.addWidget(operations)
        built = QLabel("Isolation, drilling, hole milling and the board cutout, "
                       "each with a CNC job. Turn this off to just load the files "
                       "and build the toolpaths yourself from the palette.")
        built.setWordWrap(True)
        built.setStyleSheet("color: gray")
        layout.addWidget(built)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None
        return {"side": "bottom" if bottom_button.isChecked() else "top",
                "origin": origin.isChecked(),
                "operations": operations.isChecked()}

    def open_board_folder(self):
        if not self._confirm_discard("Open a board folder"):
            return
        directory = QFileDialog.getExistingDirectory(self, "Open board folder")
        if not directory:
            return
        options = self._ask_import(discover.sides_present(directory),
                                   "Open board folder")
        if options is None:
            return
        doc, notes = discover.build_board(directory, options["side"],
                                          origin=options["origin"],
                                          operations=options["operations"])
        if not doc.order:
            QMessageBox.warning(self, "Open board folder", "\n".join(notes) or
                                "No Gerber or drill files recognised in that folder.")
            return
        self.doc = doc
        self.path = None
        self._undo.clear()
        self._redo.clear()
        self._mark(True, subject=os.path.basename(directory.rstrip(os.sep)))
        self.refresh_list()
        self.recompute()
        self.view.fit()
        QMessageBox.information(self, "Board loaded", "\n".join(notes))

    def open_kicad_board(self):
        if not self._confirm_discard("Open a KiCad board"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open KiCad board", "", "KiCad board (*.kicad_pcb);;All files (*)")
        if not path:
            return
        self._load_kicad_board(path, force=False)

    def replot_kicad_board(self):
        """Re-run the plot for the board this project came from.

        Use after editing in KiCad: the plot is refreshed and the graph rebuilt
        from it, so nothing is left pointing at yesterday's copper.
        """
        board = self.board_path()
        if not board:
            QMessageBox.information(self, "Re-plot", "This project did not come from a "
                                                     "KiCad board.")
            return
        if not os.path.isfile(board):
            QMessageBox.warning(self, "Re-plot",
                                f"The board file has moved or gone:\n{board}")
            return
        self._load_kicad_board(board, force=True, side=self.doc.source.get("side"),
                               origin=bool(self.doc.source.get("origin")),
                               operations=bool(self.doc.source.get("operations", True)))

    def _load_kicad_board(self, path, force, side=None, origin=False, operations=True):
        if side is None:
            # Plot first, then ask, because until KiCad has produced the
            # Gerbers there is no way to know whether there is a bottom side.
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                outdir, _ = kicad.plot(path, force=force)
            except kicad.KicadCliMissing as exc:
                QMessageBox.warning(self, "KiCad not found", str(exc))
                return
            except Exception as exc:
                QMessageBox.critical(self, "Could not plot the board", str(exc))
                return
            finally:
                QApplication.restoreOverrideCursor()
            options = self._ask_import(discover.sides_present(outdir),
                                       "Open KiCad board")
            if options is None:
                return
            side = options["side"]
            origin = options["origin"]
            operations = options["operations"]
            force = False   # already plotted a moment ago

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            doc, notes = kicad.open_board(path, side=side, force=force, origin=origin,
                                          operations=operations)
        except kicad.KicadCliMissing as exc:
            QMessageBox.warning(self, "KiCad not found", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Could not plot the board", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not doc.order:
            QMessageBox.warning(self, "Open KiCad board", "\n".join(notes) or
                                "KiCad plotted the board but nothing was recognised.")
            return
        self.doc = doc
        self.path = None
        self._undo.clear()
        self._redo.clear()
        self._mark(True, subject=os.path.basename(path))
        self.refresh_list()
        self.recompute()
        self.view.fit()
        QMessageBox.information(self, "Board loaded", "\n".join(notes))

    def edit_tool_library(self):
        """Open tools.toml in whatever the desktop uses for .toml files.

        A grid editor would be nicer, but the file is short, commented, and
        lives somewhere the user should know about anyway.
        """
        path = toollib.library().path
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.statusBar().showMessage(f"Tool library: {path}", 8000)

    def reload_tool_library(self):
        """Re-read tools.toml after an edit and refresh every tool dropdown."""
        library = toollib.library(reload=True)
        self.form.clear()
        self.refresh_list()
        node = self.current_node()
        if node is not None:
            self.form.show_node(self.doc, node)
        self.recompute()
        self.statusBar().showMessage(
            f"Reloaded {len(library.tools)} tools from {library.path}", 6000)

    def save_project(self):
        if not self.path:
            return self.save_project_as()
        project_io.save(self.doc, self.path)
        self._mark(False)
        self.statusBar().showMessage(f"Saved {os.path.basename(self.path)}", 4000)

    def save_project_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", "board.toml", "aaltocam project (*.toml)")
        if not path:
            return
        self.path = path
        self._mark(self._dirty, subject=os.path.basename(path))
        self.save_project()

    def _confirm_discard(self, what: str) -> bool:
        """Ask before throwing away unsaved work. True means carry on.

        Saving can itself be cancelled at the file dialog, so the answer is
        taken from whether the document actually came out clean, not from
        which button was pressed.
        """
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unsaved changes")
        name = os.path.basename(self.path) if self.path else "This project"
        box.setText(f"{name} has unsaved changes.")
        box.setInformativeText(f"{what} without saving them?")
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Save)
        answer = box.exec()
        if answer == QMessageBox.Save:
            self.save_project()
            return not self._dirty
        return answer == QMessageBox.Discard

    def closeEvent(self, event):
        if self._confirm_discard("Quit"):
            event.accept()
        else:
            event.ignore()

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
        return self.doc.nodes.get(item.data(0, Qt.UserRole))

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

    def _secondary_note(self, node) -> tuple[str, str]:
        """What feeds this node besides its primary input, as (short, full).

        Those sources live elsewhere in the tree -- as roots, or under whatever
        produced them -- so without saying so here the connection is invisible.

        The short form is only the source's name: the slot label is usually the
        same word as the node it points at, and a column reading "Height map:
        Height map" earns its width twice over while squeezing the names that
        are the actual content. The full form goes on the tooltip.
        """
        operation = node.operation()
        names, full = [], []
        for slot in range(1, len(node.inputs)):
            source = node.inputs[slot]
            if not source or source not in self.doc.nodes:
                continue
            label = (operation.slot_label(slot)
                     if slot < len(operation.inputs) else f"Input {slot + 1}")
            names.append(self.doc.nodes[source].name)
            full.append(f"{label}: {self.doc.nodes[source].name}")
        if not names:
            return "", ""
        return "+ " + ", ".join(names), "\n".join(full)

    def _make_item(self, node_id, parent) -> QTreeWidgetItem:
        node = self.doc.nodes[node_id]
        item = QTreeWidgetItem(parent if parent is not None else self.node_list)
        item.setText(0, node.name)
        item.setData(0, Qt.UserRole, node_id)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
        item.setCheckState(0, Qt.Checked if node.visible else Qt.Unchecked)
        note, full = self._secondary_note(node)
        if note:
            item.setText(1, note)
            item.setToolTip(1, full)
            item.setForeground(1, self.palette().color(
                QPalette.Disabled, QPalette.WindowText))
        self._items[node_id] = item
        return item

    def refresh_list(self, select: str | None = None):
        """Rebuild the operation tree.

        Nesting follows the *primary* input, slot 0, so every node appears
        exactly once. The graph is a DAG rather than a tree: a height map can
        feed three CNC jobs and a region can mask two isolations. Nesting such
        a node under each consumer would duplicate it, and choosing one
        consumer would be arbitrary, so it stays where it belongs and the
        consumers note it in their second column instead.
        """
        self.node_list.blockSignals(True)
        self.node_list.clear()
        self._items = {}

        children: dict[str, list[str]] = {nid: [] for nid in self.doc.order}
        roots: list[str] = []
        for node_id in self.doc.order:
            inputs = self.doc.nodes[node_id].inputs
            parent = inputs[0] if inputs else ""
            # A node whose primary input is empty or dangling is a root, which
            # is also how a half-wired operation makes itself obvious.
            if parent and parent in children:
                children[parent].append(node_id)
            else:
                roots.append(node_id)

        placed: set[str] = set()

        def place(node_id, parent_item):
            if node_id in placed:
                return
            placed.add(node_id)
            item = self._make_item(node_id, parent_item)
            for child in children[node_id]:
                place(child, item)

        for node_id in roots:
            place(node_id, None)
        # A cycle leaves nodes unreachable from any root. They should still be
        # listed -- being unable to see an operation is worse than seeing it in
        # the wrong place.
        for node_id in self.doc.order:
            place(node_id, None)

        self.node_list.expandAll()
        self.node_list.blockSignals(False)
        self._fit_note_column()
        if select and select in self._items:
            self.node_list.setCurrentItem(self._items[select])

    def _fit_note_column(self):
        """Size the note column to its contents, but never past a third.

        Left to itself the note takes whatever it wants and the names -- which
        are the thing being read, and the thing being renamed -- get elided to
        make room for a derived hint.
        """
        self.node_list.resizeColumnToContents(1)
        limit = max(48, int(self.node_list.viewport().width() / 3))
        if self.node_list.columnWidth(1) > limit:
            self.node_list.setColumnWidth(1, limit)

    def _on_select(self, current, _previous):
        node = self.current_node()
        if node is None:
            self.form.clear()
            return
        self.form.show_node(self.doc, node)
        self._grey_tool_fields(node)
        self._sync_editable(node)
        result = self.results.get(node.id)
        if hasattr(result, "kind") and result.kind == "gcode":
            self.gcode_view.setPlainText(result.data)
        self._show_stats(node)

    def _on_item_changed(self, item, _column=0):
        # One signal covers two edits: the checkbox toggles visibility, and an
        # in-place edit of the row renames the node.
        node = self.doc.nodes.get(item.data(0, Qt.UserRole))
        if node is None:
            return

        text = item.text(0).strip()
        if text != node.name:
            self.push_undo()
            self._apply_name(node, text)
            # refresh_list would delete the very item whose signal this is, so
            # correct the one row instead, and only when a blank name fell back
            # to the id.
            if item.text(0) != node.name:
                self.node_list.blockSignals(True)
                item.setText(0, node.name)
                self.node_list.blockSignals(False)
            # A rename shows up in whatever reads this node as a secondary
            # input, so those rows have to be refreshed too.
            self._refresh_secondary_notes()
            if self.current_node() is node:
                self.form.show_node(self.doc, node)
            return

        visible = item.checkState(0) == Qt.Checked
        if visible != node.visible:
            node.visible = visible
            self.redraw()

    def _refresh_secondary_notes(self):
        """Re-derive the second column without rebuilding the tree."""
        self.node_list.blockSignals(True)
        for node_id, item in self._items.items():
            node = self.doc.nodes.get(node_id)
            if node is not None:
                note, full = self._secondary_note(node)
                item.setText(1, note)
                item.setToolTip(1, full)
        self.node_list.blockSignals(False)
        self._fit_note_column()

    def _on_param_changed(self, node_id, name, value):
        node = self.doc.nodes[node_id]
        param = node.operation().param(name)
        if param is not None:
            value = param.coerce(value)
        self.push_undo()
        changed = self.doc.set_param(node_id, name, value)

        # Choosing a tool rewrites the fields it governs, so the form has to be
        # rebuilt: updating one widget in place would leave the diameter beside
        # it still showing the previous cutter.
        if name in ("tool", "feeds_from_tool"):
            if ops.sync_tool_params(node, self._effective_tool(node)):
                self.doc.invalidate(node_id)
                changed = True
            self.form.show_node(self.doc, node)
            self._grey_tool_fields(node)
        if changed:
            self.schedule()

    def _effective_tool(self, node):
        """The cutter a node is actually working with.

        A CNC job usually inherits it from the geometry upstream rather than
        naming one itself, so look there when the node has no choice of its own.
        """
        own = ops.selected_tool(node)
        if own is not None or node.op != "cnc_job":
            return own
        for source_id in node.inputs:
            result = self.results.get(source_id)
            if hasattr(result, "meta"):
                found = toollib.get(str(result.meta.get("tool", "") or ""))
                if found is not None:
                    return found
        return None

    def _adopt_inherited_tools(self) -> bool:
        """Copy each CNC job's inherited feeds into its own fields.

        Without this the job would show one feed and write another as soon as
        the tool was chosen on the isolation node rather than on the job.
        """
        touched = False
        for node in list(self.doc.nodes.values()):
            if node.op != "cnc_job" or not node.params.get("feeds_from_tool", True):
                continue
            tool = self._effective_tool(node)
            if tool is None or not tool.has_feeds:
                continue
            if ops.sync_tool_params(node, tool):
                self.doc.invalidate(node.id)
                touched = True
        if touched and self.form._node_id in self.doc.nodes:
            self.form.show_node(self.doc, self.doc.nodes[self.form._node_id])
            self._grey_tool_fields(self.doc.nodes[self.form._node_id])
        return touched

    def _grey_tool_fields(self, node):
        """Feeds supplied by a tool are shown but not editable here.

        The CNC job cannot express this with depends_on, because the tool it
        obeys may have come down the graph rather than being chosen on the node.
        """
        if node is None or node.op != "cnc_job":
            return
        tool = self._effective_tool(node) if node.params.get("feeds_from_tool", True) else None
        governed = tool is not None and tool.has_feeds
        for name in ("feed_xy", "feed_z", "cut_z", "depth_per_pass", "multidepth", "spindle"):
            self.form.set_enabled(name, not governed)

    def _on_input_changed(self, node_id, slot, source_id):
        self.push_undo()
        node = self.doc.nodes[node_id]
        while len(node.inputs) <= slot:
            node.inputs.append("")
        node.inputs[slot] = source_id
        self.doc.invalidate(node_id)
        # Rewiring is what the tree is drawing, so the shape has to follow:
        # slot 0 moves the branch, a later slot changes the note beside it.
        self.refresh_list(select=node_id)
        self.schedule()

    def _apply_name(self, node, name):
        """A blank name leaves nothing to click on, so fall back to the id."""
        node.name = name.strip() or node.id

    def _on_rename(self, node_id, name):
        node = self.doc.nodes.get(node_id)
        if node is None:
            return
        # editingFinished fires on focus loss as well as on Enter, so without
        # this every click away from the field lands another undo entry.
        if (name.strip() or node.id) == node.name:
            return
        self.push_undo()
        self._apply_name(node, name)
        self.refresh_list(select=node_id)

    def choose_theme(self, name: str):
        """Record the theme. It is worn at the next start, not this one.

        Restyling a running window means rebuilding every drawn icon and every
        item already in the scene, and doing that halfway through a job is a
        good way to lose the view someone was working in. Writing the choice
        down and saying so plainly is honest and costs one restart.
        """
        try:
            settings.set_theme(name)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save the theme", str(exc))
            return
        self.statusBar().showMessage(
            f"{name.capitalize()} theme saved. It is applied the next time "
            f"aaltocam starts.", 8000)

    def focus_next_origin(self):
        """Centre the view on the next zero, the machine's included.

        Two sides placed side by side are two working areas, and at a useful
        zoom only one of them is on screen. This is how you get between them
        without hunting.
        """
        spots = [(0.0, 0.0, "Machine zero")] + self.view.extra_origins()
        if len(spots) == 1:
            self.statusBar().showMessage(
                "Only the machine zero so far. Edit → Place beside the board "
                "moves a layer aside and gives it one of its own.", 6000)
            return
        self._origin_focus = (self._origin_focus + 1) % len(spots)
        x, y, label = spots[self._origin_focus]
        self.view.centre_on(x, y)
        self.statusBar().showMessage(f"{label}   X {x:.3f}   Y {y:.3f}", 5000)

    def place_beside(self):
        """Move the selected layer clear of the board, with its own zero.

        The offset is real, not a drawing trick: the geometry moves, so the
        cursor readout, the measuring tool and the G-code all agree, and the
        layer is milled by zeroing the machine on its new origin.
        """
        node = self.current_node()
        if node is None:
            self.statusBar().showMessage("Select the layer to move first.", 4000)
            return
        result = self.results.get(node.id)
        mine = ops.payload_bounds(result) if hasattr(result, "kind") else None
        if mine is None:
            self.statusBar().showMessage(
                f"{node.name} has no geometry to place yet.", 4000)
            return

        # Clear everything on screen rather than just this layer, so the two
        # working areas cannot overlap whatever else happens to be loaded.
        right = mine[2]
        for other_id in self.doc.order:
            if other_id == node.id or not self.doc.nodes[other_id].visible:
                continue
            other = self.results.get(other_id)
            bounds = ops.payload_bounds(other) if hasattr(other, "kind") else None
            if bounds is not None:
                right = max(right, bounds[2])

        shift = right - mine[0] + BESIDE_GAP_MM
        self.push_undo()
        moved = self.doc.add("transform", [node.id],
                             name=f"{node.name} aside", offset_x=shift)
        self.refresh_list(select=moved.id)
        self.recompute()
        self.view.centre_on(shift, 0.0)
        self.statusBar().showMessage(
            f"{moved.name} placed at X {shift:.3f} mm. For the other side of "
            f"the board, set Mirror to 'y' in the panel.", 9000)

    def rename_current(self):
        """Start an in-place edit of the selected operation."""
        item = self.node_list.currentItem()
        if item is not None:
            self.node_list.setFocus()
            self.node_list.editItem(item, 0)

    def _show_cursor(self, x, y):
        # While measuring, the points carry their own labels and the reading
        # owns the status bar; a cursor readout would just flicker over it.
        if self.measure_action.isChecked():
            return
        self.statusBar().showMessage(f"X {x:.3f}   Y {y:.3f}", 2000)

    def _toggle_measure(self, on):
        self.view.start_measure(on)
        if on:
            self.statusBar().showMessage(
                "Measuring: click two points. Shift locks to one axis, "
                "right-click clears, M leaves.")
        else:
            self.statusBar().clearMessage()

    def _show_measure(self, result):
        """Keep the reading in the status bar too, so it survives a pan."""
        if result is None:
            return
        distance, dx, dy = result
        self.statusBar().showMessage(
            f"Distance {distance:.3f} mm    Δx {dx:+.3f}    Δy {dy:+.3f}")

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
            # A CNC job usually learns its cutter from the geometry above it,
            # which is only known once that geometry has been evaluated. Bring
            # its fields into line and evaluate the affected jobs again; the
            # sync is idempotent, so this settles in one extra pass.
            if self._adopt_inherited_tools():
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
        if "summary" in meta:
            # A height map's own description: shape, extent and Z span. Reading
            # the span is how you notice a file of absolute heights loaded as
            # though it held deviations.
            bits.append(meta["summary"])
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
        if meta.get("tool"):
            bits.append(f"tool {meta['tool']}")
        if meta.get("chip_load_um"):
            bits.append(f"{meta['chip_load_um']:.1f} um/tooth")
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
        for warning in reversed(meta.get("warnings", [])):
            bits.insert(0, warning)
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
            elif result.kind == "heightmap":
                self.view.show_heightmap(node_id, result.data)
        self.view.set_extra_origins(self._secondary_origins())
        # clear_all() removed the edit handles; put them back on top.
        self.view.refresh_handles()

    def _secondary_origins(self):
        """Every zero other than the machine's, one marker per position.

        A whole side -- copper, drills, outline -- is moved by one shared
        transform, so half a dozen layers land on the same zero. Marking it
        once keeps the view readable; the label is whichever layer got there
        first, which is enough to say which side it belongs to.
        """
        seen, out = set(), []
        for node_id in self.doc.order:
            node = self.doc.nodes[node_id]
            result = self.results.get(node_id)
            if not node.visible or not hasattr(result, "meta"):
                continue
            spot = result.meta.get("origin")
            if not spot:
                continue
            key = (round(spot[0], 6), round(spot[1], 6))
            if key in seen:
                continue
            seen.add(key)
            out.append((float(spot[0]), float(spot[1]), node.name))
        return out


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


def light_palette() -> QPalette:
    """The light theme's window chrome.

    Not the dark palette inverted: the accent stays a copper that reads as
    copper on white rather than the pale orange that would come of flipping
    lightness, and highlighted text goes to white because the accent is dark
    enough to carry it.
    """
    palette = QPalette()
    base = QColor("#FFFFFF")
    panel = QColor("#F1F2F4")
    text = QColor("#1B2129")
    accent = QColor("#B26A33")
    palette.setColor(QPalette.Window, panel)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, panel)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, panel)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.Highlight, accent)
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ToolTipBase, base)
    palette.setColor(QPalette.ToolTipText, text)
    # The palette's section headings are styled palette(bright-text), which
    # means "legible against a dark background" and is therefore exactly wrong
    # here. Left to Fusion's default it comes out near-white on near-white.
    palette.setColor(QPalette.BrightText, QColor("#59667A"))
    return palette


def apply_theme(app, name: str):
    """Dress the application in one theme. Once, before any window is built.

    The board view's colours are module globals read by every painter, and the
    icons are drawn from the running palette, so both have to be settled before
    anything asks for a colour -- which is why this is a start-up decision and
    not a live toggle.
    """
    canvas.use_theme(name)
    app.setPalette(light_palette() if name == "light" else dark_palette())


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setStyle("Fusion")
    apply_theme(app, settings.load().get("theme", "dark"))
    app.setApplicationName("aaltocam")
    app.setWindowIcon(icons.app_icon())
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
