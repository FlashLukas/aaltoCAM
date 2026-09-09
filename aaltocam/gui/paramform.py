"""Parameter editor.

The form is generated from each operation's Param list, so operations stay
editable forever and adding a parameter needs no widget code. This is the
whole point of the program: nothing is baked at creation time.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.params import DEFAULT_TOOL, normalize_shapes, normalize_tools


class ShapeCoordinates(QDialog):
    """Exact coordinates for one shape, for when dragging is not precise
    enough -- a 3.50 mm slot is easier typed than dragged."""

    def __init__(self, shape, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Shape coordinates")
        self.shape = {"type": shape["type"], "points": [list(p) for p in shape["points"]]}
        layout = QVBoxLayout(self)

        points = self.shape["points"]
        self.table = QTableWidget(len(points), 2)
        self.table.setHorizontalHeaderLabels(["X mm", "Y mm"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row, (x, y) in enumerate(points):
            for column, value in ((0, x), (1, y)):
                box = QDoubleSpinBox()
                box.setDecimals(3)
                box.setSingleStep(0.1)
                box.setRange(-10000, 10000)
                box.setValue(float(value))
                self.table.setCellWidget(row, column, box)
        layout.addWidget(self.table)

        if shape["type"] == "rect":
            layout.addWidget(QLabel("Opposite corners of the rectangle."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_shape(self):
        points = []
        for row in range(self.table.rowCount()):
            points.append([self.table.cellWidget(row, 0).value(),
                           self.table.cellWidget(row, 1).value()])
        return {"type": self.shape["type"], "points": points}


class ShapeList(QWidget):
    """Editor for a drawn region: a list of shapes plus the draw buttons.

    Drawing happens on the board view, so the buttons only ask for a mode;
    the main window arms the canvas and feeds finished shapes back here.
    """

    changed = Signal(object)
    draw_requested = Signal(str)

    def __init__(self, shapes, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.shapes = normalize_shapes(shapes)
        self.list = QListWidget()
        self.list.setMinimumHeight(90)
        self.list.itemDoubleClicked.connect(self._edit_coordinates)
        layout.addWidget(self.list)

        row = QHBoxLayout()
        rect = QPushButton("Draw rectangle")
        poly = QPushButton("Draw polygon")
        rect.clicked.connect(lambda: self.draw_requested.emit("rect"))
        poly.clicked.connect(lambda: self.draw_requested.emit("poly"))
        row.addWidget(rect)
        row.addWidget(poly)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        remove = QPushButton("Delete")
        clear = QPushButton("Clear all")
        remove.clicked.connect(self._remove)
        clear.clicked.connect(self._clear)
        row2.addWidget(remove)
        row2.addWidget(clear)
        row2.addStretch(1)
        layout.addLayout(row2)

        self._refresh()

    @staticmethod
    def _describe(shape) -> str:
        points = shape["points"]
        if shape["type"] == "rect":
            (x1, y1), (x2, y2) = points
            return (f"Rectangle {abs(x2 - x1):.2f} × {abs(y2 - y1):.2f} mm "
                    f"at ({min(x1, x2):.2f}, {min(y1, y2):.2f})")
        return f"Polygon, {len(points)} points"

    def _refresh(self):
        self.list.clear()
        for shape in self.shapes:
            self.list.addItem(self._describe(shape))

    def add_shape(self, shape):
        self.shapes = normalize_shapes(self.shapes + [shape])
        self._refresh()
        self.changed.emit(list(self.shapes))

    def set_shapes(self, shapes):
        """Refresh after the shape was dragged on the board view."""
        self.shapes = normalize_shapes(shapes)
        self._refresh()

    def _edit_coordinates(self, _item):
        row = self.list.currentRow()
        if not (0 <= row < len(self.shapes)):
            return
        dialog = ShapeCoordinates(self.shapes[row], self)
        if dialog.exec() == QDialog.Accepted:
            updated = normalize_shapes([dialog.result_shape()])
            if updated:
                self.shapes[row] = updated[0]
                self._refresh()
                self.changed.emit(list(self.shapes))

    def _remove(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.shapes):
            del self.shapes[row]
            self._refresh()
            self.changed.emit(list(self.shapes))

    def _clear(self):
        if self.shapes:
            self.shapes = []
            self._refresh()
            self.changed.emit([])


class ToolTable(QWidget):
    """Ordered tool list for rest machining. Largest first; the order is
    enforced on every edit so the geometry code never has to re-sort."""

    changed = Signal(object)

    HEADERS = ["Ø mm", "Shape", "Tip Ø", "Angle"]

    def __init__(self, tools, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(110)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        row = QHBoxLayout()
        add = QPushButton("Add tool")
        remove = QPushButton("Remove")
        add.clicked.connect(self._add_row)
        remove.clicked.connect(self._remove_row)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)

        self._loading = True
        for tool in normalize_tools(tools):
            self._add_row(tool)
        self._loading = False

    def _spin(self, value, step, maximum, decimals=3):
        box = QDoubleSpinBox()
        box.setDecimals(decimals)
        box.setSingleStep(step)
        box.setRange(0.0, maximum)
        box.setValue(float(value))
        box.setKeyboardTracking(False)
        box.valueChanged.connect(self._emit)
        return box

    def _add_row(self, tool=None):
        tool = tool if isinstance(tool, dict) else dict(DEFAULT_TOOL)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setCellWidget(row, 0, self._spin(tool["dia"], 0.05, 20))
        shape = QComboBox()
        shape.addItems(["flat", "v"])
        shape.setCurrentText(tool["shape"])
        shape.currentIndexChanged.connect(self._emit)
        self.table.setCellWidget(row, 1, shape)
        self.table.setCellWidget(row, 2, self._spin(tool["tip_dia"], 0.01, 5))
        self.table.setCellWidget(row, 3, self._spin(tool["tip_angle"], 1.0, 180, 1))
        self._emit()

    def _remove_row(self):
        row = self.table.currentRow()
        if row < 0:
            row = self.table.rowCount() - 1
        if self.table.rowCount() > 1 and row >= 0:
            self.table.removeRow(row)
            self._emit()

    def tools(self) -> list[dict]:
        out = []
        for row in range(self.table.rowCount()):
            out.append({
                "dia": self.table.cellWidget(row, 0).value(),
                "shape": self.table.cellWidget(row, 1).currentText(),
                "tip_dia": self.table.cellWidget(row, 2).value(),
                "tip_angle": self.table.cellWidget(row, 3).value(),
            })
        return normalize_tools(out)

    def _emit(self, *_args):
        if not self._loading:
            self.changed.emit(self.tools())


class ParamForm(QScrollArea):
    param_changed = Signal(str, str, object)  # node id, param name, value
    draw_requested = Signal(str, str)  # node id, shape kind
    input_changed = Signal(str, int, str)  # node id, slot, source node id
    rename_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._node_id = None
        self._widgets: dict[str, QWidget] = {}
        self._params: dict[str, object] = {}
        self._loading = False
        self.setWidget(QWidget())

    def clear(self):
        self._node_id = None
        self._widgets.clear()
        self._params.clear()
        self.setWidget(QWidget())

    def show_node(self, doc, node):
        self._loading = True
        self._node_id = node.id
        self._widgets.clear()
        self._params.clear()

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        operation = node.operation()

        header = QGroupBox(operation.label)
        head_form = QFormLayout(header)
        name_edit = QLineEdit(node.name)
        name_edit.editingFinished.connect(
            lambda: self.rename_requested.emit(node.id, name_edit.text()))
        head_form.addRow("Name", name_edit)

        for slot, kind in enumerate(operation.inputs):
            combo = QComboBox()
            combo.addItem("(none)" if operation.slot_optional(slot) else "(not connected)", "")
            for other_id in doc.order:
                other = doc.nodes[other_id]
                if other_id == node.id:
                    continue
                combo.addItem(f"{other.name}  [{other_id}]", other_id)
            current = node.inputs[slot] if slot < len(node.inputs) else ""
            index = combo.findData(current)
            combo.setCurrentIndex(max(index, 0))
            combo.currentIndexChanged.connect(
                lambda _i, s=slot, c=combo: self._emit_input(s, c))
            label = operation.slot_label(slot)
            if operation.slot_optional(slot):
                label += " (optional)"
            head_form.addRow(label, combo)
        outer.addWidget(header)

        groups: dict[str, QFormLayout] = {}
        for param in operation.params:
            group_name = param.group or "Parameters"
            if group_name not in groups:
                box = QGroupBox(group_name)
                groups[group_name] = QFormLayout(box)
                outer.addWidget(box)
            widget = self._build(param, node.params.get(param.name, param.default))
            self._widgets[param.name] = widget
            self._params[param.name] = param
            label = QLabel(f"{param.label} [{param.unit}]" if param.unit else param.label)
            if param.help:
                label.setToolTip(param.help)
                widget.setToolTip(param.help)
            if param.kind in ("tools", "shapes"):
                groups[group_name].addRow(label)
                groups[group_name].addRow(widget)
            else:
                groups[group_name].addRow(label, widget)

        outer.addStretch(1)
        self.setWidget(container)
        self._loading = False
        self._update_enabled()

    # -- widget construction ----------------------------------------------

    def _build(self, param, value):
        if param.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(param.decimals)
            w.setSingleStep(param.step)
            w.setRange(param.minimum if param.minimum is not None else -1e9,
                       param.maximum if param.maximum is not None else 1e9)
            w.setValue(float(value))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, n=param.name: self._emit(n, float(v)))
            return w
        if param.kind == "int":
            w = QSpinBox()
            w.setRange(int(param.minimum or 0), int(param.maximum or 10**6))
            w.setValue(int(value))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v, n=param.name: self._emit(n, int(v)))
            return w
        if param.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            w.stateChanged.connect(
                lambda s, n=param.name: self._emit(n, s == Qt.Checked.value))
            return w
        if param.kind == "choice":
            w = QComboBox()
            for choice in param.choices:
                w.addItem(str(choice), choice)
            index = w.findData(value)
            w.setCurrentIndex(max(index, 0))
            w.currentIndexChanged.connect(
                lambda _i, n=param.name, c=w: self._emit(n, c.currentData()))
            return w
        if param.kind == "shapes":
            w = ShapeList(value)
            w.changed.connect(lambda v, n=param.name: self._emit(n, v))
            w.draw_requested.connect(
                lambda kind: self.draw_requested.emit(self._node_id or "", kind))
            return w
        if param.kind == "tools":
            w = ToolTable(value)
            w.changed.connect(lambda v, n=param.name: self._emit(n, v))
            return w
        if param.kind == "path":
            w = QWidget()
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(str(value))
            button = QPushButton("Browse")
            row.addWidget(edit, 1)
            row.addWidget(button)
            edit.editingFinished.connect(
                lambda n=param.name, e=edit: self._emit(n, e.text()))
            button.clicked.connect(lambda _c=False, n=param.name, e=edit: self._browse(n, e))
            w.line_edit = edit
            return w
        w = QLineEdit(str(value))
        w.editingFinished.connect(lambda n=param.name, e=w: self._emit(n, e.text()))
        return w

    def _browse(self, name, edit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select file", edit.text() or "",
            "Gerber and drill files (*.gbr *.ger *.gtl *.gbl *.drl *.xln *.txt);;All files (*)")
        if path:
            edit.setText(path)
            self._emit(name, path)

    # -- signals -----------------------------------------------------------

    def _emit(self, name, value):
        if self._loading or not self._node_id:
            return
        self.param_changed.emit(self._node_id, name, value)
        self._update_enabled()

    def _emit_input(self, slot, combo):
        if self._loading or not self._node_id:
            return
        self.input_changed.emit(self._node_id, slot, combo.currentData() or "")

    def value_of(self, name):
        widget = self._widgets.get(name)
        if widget is None:
            return None
        if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
            return widget.value()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QLineEdit):
            return widget.text()
        if isinstance(widget, ToolTable):
            return widget.tools()
        if isinstance(widget, ShapeList):
            return list(widget.shapes)
        return None

    def _update_enabled(self):
        """Grey out parameters whose controlling parameter is off."""
        for name, param in self._params.items():
            rule = getattr(param, "depends_on", None)
            if not rule:
                continue
            invert = rule.startswith("!")
            rule = rule[1:] if invert else rule
            if ":" in rule:
                other, expected = rule.split(":", 1)
                active = str(self.value_of(other)) == expected
            else:
                active = bool(self.value_of(rule))
            if invert:
                active = not active
            widget = self._widgets.get(name)
            if widget is not None:
                widget.setEnabled(active)

    def set_enabled(self, name: str, on: bool):
        """Grey one field out for a reason the parameters cannot express."""
        widget = self._widgets.get(name)
        if widget is not None:
            widget.setEnabled(on)


    def set_shapes(self, shapes) -> bool:
        for widget in self._widgets.values():
            if isinstance(widget, ShapeList):
                widget.set_shapes(shapes)
                return True
        return False

    def add_shape(self, shape) -> bool:
        """Feed a shape drawn on the board view into the shapes editor."""
        for widget in self._widgets.values():
            if isinstance(widget, ShapeList):
                widget.add_shape(shape)
                return True
        return False
