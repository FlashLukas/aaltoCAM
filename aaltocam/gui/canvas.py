"""Board view.

One QGraphicsPathItem per layer rather than one item per polygon: a dense
board is tens of thousands of segments, and Qt handles that comfortably as a
single path but not as thousands of items.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsScene, QGraphicsView

COPPER = QColor("#B4622C")
COPPER_EDGE = QColor("#D98A4E")
TOOLPATH = QColor("#4FA3E3")
TRAVEL = QColor("#2E5C82")
DRILL = QColor("#E8E4DC")
REGION = QColor("#5FD3A8")
ALERT = QColor("#E5484D")
ALERT_FILL = QColor(229, 72, 77, 90)
REGION_FILL = QColor(95, 211, 168, 38)
BACKGROUND = QColor("#12161C")
GRID = QColor("#232B36")
GRID_FINE = QColor("#1A202A")
AXIS = QColor("#39485C")          # the two lines through zero, under the board
ORIGIN = QColor("#F0EDE6")        # the marker at zero, over everything
MEASURE = QColor("#F2C14E")
MEASURE_DIM = QColor(242, 193, 78, 120)
LABEL_BG = QColor(9, 12, 16, 216)

# Height map: a diverging pair, because what matters is which side of the
# reference a point sits on, not just how far. Neither end is red or green.
HEIGHT_LOW = QColor("#4F8FE3")    # below the reference
HEIGHT_HIGH = QColor("#E39A4F")   # above it
HEIGHT_ZERO = QColor("#59626E")   # flat, and close enough to the board to fade
HEIGHT_EDGE = QColor("#7C8794")   # the probed boundary, beyond which Z is held


def _height_colour(value: float) -> QColor:
    """Map a deviation in -1..1 onto the diverging ramp."""
    value = max(-1.0, min(1.0, value))
    end = HEIGHT_HIGH if value >= 0 else HEIGHT_LOW
    t = abs(value)
    return QColor(
        round(HEIGHT_ZERO.red() + (end.red() - HEIGHT_ZERO.red()) * t),
        round(HEIGHT_ZERO.green() + (end.green() - HEIGHT_ZERO.green()) * t),
        round(HEIGHT_ZERO.blue() + (end.blue() - HEIGHT_ZERO.blue()) * t),
        210,
    )


class BoardView(QGraphicsView):
    cursor_moved = Signal(float, float)
    shape_drawn = Signal(object)
    shapes_edited = Signal(object)
    draw_finished = Signal()
    #: (distance, dx, dy) while a measurement has two ends, else None.
    measured = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setBackgroundBrush(QBrush(BACKGROUND))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        # Millimetres up, pixels down.
        self.scale(1, -1)
        self._items: dict[str, list[QGraphicsPathItem]] = {}
        self.show_travel = True
        self._draw_kind: str | None = None
        self._draw_points: list[tuple[float, float]] = []
        self._preview: QGraphicsPathItem | None = None
        self._edit_node: str | None = None
        self._edit_shapes: list = []
        self._handles: list[QGraphicsPathItem] = []
        self._drag = None
        self._pan = None
        self.snap = 0.1
        self.setFocusPolicy(Qt.StrongFocus)
        #: Measuring: ends already clicked, the live cursor, and whether one
        #: end is pinned to zero.
        self._measuring = False
        self._measure: list[tuple[float, float]] = []
        self._measure_cursor: tuple[float, float] | None = None
        self.clip_to_origin = False

    # -- painting ----------------------------------------------------------

    def drawBackground(self, painter, rect: QRectF):
        super().drawBackground(painter, rect)
        r = rect.normalized()

        # The two lines through zero, drawn under the board so they orient you
        # without ever obscuring copper. The marker itself goes on top, in
        # drawForeground, so it stays findable over a filled pour.
        axis = QPen(AXIS)
        axis.setCosmetic(True)
        painter.setPen(axis)
        if r.left() <= 0 <= r.right():
            painter.drawLine(QPointF(0.0, r.top()), QPointF(0.0, r.bottom()))
        if r.top() <= 0 <= r.bottom():
            painter.drawLine(QPointF(r.left(), 0.0), QPointF(r.right(), 0.0))

        scale = abs(self.transform().m11())
        if scale < 1.0:
            return
        steps = [(10.0, GRID)]
        if scale > 12:
            steps.insert(0, (1.0, GRID_FINE))
        for step, color in steps:
            pen = QPen(color)
            pen.setCosmetic(True)
            painter.setPen(pen)
            x = math.floor(r.left() / step) * step
            while x <= r.right():
                painter.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                x += step
            y = math.floor(r.top() / step) * step
            while y <= r.bottom():
                painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
                y += step

    def drawForeground(self, painter, rect: QRectF):
        """The origin marker and any measurement, painted in device pixels.

        Resetting the transform is what makes this bearable. The view is
        scaled and flipped so millimetres run upwards, which would leave text
        mirrored and marker arms growing with the zoom; painting in viewport
        coordinates instead keeps every label the right way up and every
        annotation the same size at any magnification.
        """
        super().drawForeground(painter, rect)
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._paint_origin(painter)
        self._paint_measure(painter)
        painter.restore()

    def _paint_origin(self, painter):
        centre = self.mapFromScene(QPointF(0.0, 0.0))
        margin = 30
        area = self.viewport().rect().adjusted(-margin, -margin, margin, margin)
        if not area.contains(centre):
            return
        pen = QPen(ORIGIN)
        pen.setWidthF(1.3)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        x, y = centre.x(), centre.y()
        arm, gap, ring = 11.0, 4.0, 4.5
        painter.drawLine(QPointF(x - arm, y), QPointF(x - gap, y))
        painter.drawLine(QPointF(x + gap, y), QPointF(x + arm, y))
        painter.drawLine(QPointF(x, y - arm), QPointF(x, y - gap))
        painter.drawLine(QPointF(x, y + gap), QPointF(x, y + arm))
        painter.drawEllipse(QPointF(x, y), ring, ring)

    # -- measuring ---------------------------------------------------------

    def _measure_ends(self):
        """The two ends to draw: those clicked, plus the cursor if still live."""
        ends = list(self._measure)
        if self._measuring and len(ends) < 2 and self._measure_cursor:
            ends.append(self._measure_cursor)
        return ends[:2]

    def _paint_measure(self, painter):
        ends = self._measure_ends()
        if not ends:
            return
        font = painter.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() - 0.5))
        painter.setFont(font)
        screen = [self.mapFromScene(QPointF(x, y)) for x, y in ends]

        if len(ends) == 2:
            settled = len(self._measure) == 2
            pen = QPen(MEASURE if settled else MEASURE_DIM)
            pen.setWidthF(1.4)
            pen.setStyle(Qt.SolidLine if settled else Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(screen[0]), QPointF(screen[1]))

        for point, place in zip(ends, screen):
            self._paint_end(painter, point, place)

        if len(ends) == 2:
            (x0, y0), (x1, y1) = ends
            dx, dy = x1 - x0, y1 - y0
            distance = math.hypot(dx, dy)
            mid = QPointF((screen[0].x() + screen[1].x()) / 2,
                          (screen[0].y() + screen[1].y()) / 2)
            angle = math.degrees(math.atan2(dy, dx))
            self._chip(painter, mid,
                       [f"{distance:.3f} mm",
                        f"Δx {dx:+.3f}   Δy {dy:+.3f}",
                        f"{angle:+.2f}°"],
                       MEASURE, centred=True)

    def _paint_end(self, painter, point, place):
        pen = QPen(MEASURE)
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(place, 3.5, 3.5)
        x, y = point
        self._chip(painter, QPointF(place.x() + 9, place.y() - 9),
                   [f"X {x:.3f}", f"Y {y:.3f}"], MEASURE)

    def _chip(self, painter, at: QPointF, lines, colour, centred=False):
        """A label with a backing plate, nudged to stay inside the viewport."""
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines)
        height = metrics.height() * len(lines)
        pad = 4.0
        box = QRectF(at.x(), at.y() - (height + 2 * pad) / 2 if centred else at.y(),
                     width + 2 * pad, height + 2 * pad)
        if centred:
            box.moveLeft(at.x() - box.width() / 2)
            box.moveTop(at.y() - box.height() - 8)

        view = QRectF(self.viewport().rect()).adjusted(2, 2, -2, -2)
        if box.right() > view.right():
            box.moveRight(view.right())
        if box.left() < view.left():
            box.moveLeft(view.left())
        if box.bottom() > view.bottom():
            box.moveBottom(view.bottom())
        if box.top() < view.top():
            box.moveTop(view.top())

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(LABEL_BG))
        painter.drawRoundedRect(box, 3, 3)
        painter.setPen(QPen(colour))
        top = box.top() + pad
        for index, line in enumerate(lines):
            painter.drawText(QRectF(box.left() + pad, top + index * metrics.height(),
                                    width, metrics.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, line)

    def start_measure(self, on: bool = True):
        """Enter or leave measuring. Clicks set the ends; Escape clears."""
        self._measuring = on
        self._measure = []
        self._measure_cursor = None
        if on:
            if self._draw_kind:
                self.cancel_draw()
            self.setDragMode(QGraphicsView.NoDrag)
            self.viewport().setCursor(Qt.CrossCursor)
            if self.clip_to_origin:
                self._measure = [(0.0, 0.0)]
            self.setFocus()
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().setCursor(Qt.ArrowCursor)
        self.measured.emit(None)
        self.viewport().update()

    def set_clip_to_origin(self, on: bool):
        """Pin one end of the measurement to X0 Y0.

        Distances from the machine origin are the ones you actually key into
        the controller, and clicking zero by hand never lands on it.
        """
        self.clip_to_origin = on
        if not self._measuring:
            return
        if on:
            # Keep whichever end was clicked second, so turning this on mid
            # measurement re-references what you already picked rather than
            # throwing it away.
            kept = self._measure[-1] if self._measure else None
            self._measure = [(0.0, 0.0)] + ([kept] if kept and kept != (0.0, 0.0) else [])
        elif self._measure and self._measure[0] == (0.0, 0.0):
            self._measure = self._measure[1:]
        self._emit_measure()
        self.viewport().update()

    def clear_measure(self):
        self._measure = [(0.0, 0.0)] if (self._measuring and self.clip_to_origin) else []
        self._measure_cursor = None
        self.measured.emit(None)
        self.viewport().update()

    def _emit_measure(self):
        if len(self._measure) == 2:
            (x0, y0), (x1, y1) = self._measure
            dx, dy = x1 - x0, y1 - y0
            self.measured.emit((math.hypot(dx, dy), dx, dy))
        else:
            self.measured.emit(None)

    def _measure_click(self, point, constrain: bool):
        if len(self._measure) >= 2:
            self._measure = [(0.0, 0.0)] if self.clip_to_origin else []
        if constrain and self._measure:
            point = self._axis_lock(self._measure[-1], point)
        self._measure.append(point)
        self._measure_cursor = None
        self._emit_measure()
        self.viewport().update()

    @staticmethod
    def _axis_lock(anchor, point):
        """Shift held: drop whichever of dx, dy is the smaller."""
        dx, dy = point[0] - anchor[0], point[1] - anchor[1]
        if abs(dx) >= abs(dy):
            return (point[0], anchor[1])
        return (anchor[0], point[1])

    # -- content -----------------------------------------------------------

    def clear_node(self, node_id: str):
        for item in self._items.pop(node_id, []):
            self.scene().removeItem(item)

    def clear_all(self):
        for node_id in list(self._items):
            self.clear_node(node_id)

    def _add(self, node_id, path, pen, brush=None, z=0.0):
        item = QGraphicsPathItem(path)
        item.setPen(pen)
        item.setBrush(brush or QBrush(Qt.NoBrush))
        item.setZValue(z)
        self.scene().addItem(item)
        self._items.setdefault(node_id, []).append(item)

    def show_copper(self, node_id, polys):
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        for poly in polys.geoms:
            _append_ring(path, poly.exterior.coords)
            for ring in poly.interiors:
                _append_ring(path, ring.coords)
        pen = QPen(COPPER_EDGE)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        self._add(node_id, path, pen, QBrush(COPPER), z=0)

    def show_path_groups(self, node_id, groups):
        """Rest-machining groups, one hue step per tool."""
        for index, (width, lines) in enumerate(groups):
            color = TOOLPATH.lighter(100 + index * 22) if index else TOOLPATH
            self.show_paths(node_id, lines, width, color=color,
                            travel=(index == len(groups) - 1))

    def show_paths(self, node_id, lines, width=0.0, color=None, travel=True):
        path = QPainterPath()
        for line in lines:
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            path.moveTo(coords[0][0], coords[0][1])
            for x, y in coords[1:]:
                path.lineTo(x, y)
        pen = QPen(color or TOOLPATH)
        if width > 0.02:
            pen.setWidthF(width)
        else:
            pen.setCosmetic(True)
            pen.setWidthF(1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        self._add(node_id, path, pen, z=2)

        if travel and self.show_travel and len(lines) > 1:
            travel = QPainterPath()
            cur = (0.0, 0.0)
            for line in lines:
                coords = list(line.coords)
                travel.moveTo(cur[0], cur[1])
                travel.lineTo(coords[0][0], coords[0][1])
                cur = coords[-1]
            tpen = QPen(TRAVEL)
            tpen.setCosmetic(True)
            tpen.setWidthF(1.0)
            tpen.setStyle(Qt.DotLine)
            self._add(node_id, travel, tpen, z=1)

    def show_region(self, node_id, polys):
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        for poly in polys.geoms:
            _append_ring(path, poly.exterior.coords)
            for ring in poly.interiors:
                _append_ring(path, ring.coords)
        pen = QPen(REGION)
        pen.setCosmetic(True)
        pen.setWidthF(1.4)
        pen.setStyle(Qt.DashLine)
        self._add(node_id, path, pen, QBrush(REGION_FILL), z=-1)

    def show_alert(self, node_id, polys):
        """Places the tool cannot reach. Drawn on top of everything, in red,
        because this is the layer you are meant to notice."""
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        for poly in polys.geoms:
            _append_ring(path, poly.exterior.coords)
            for ring in poly.interiors:
                _append_ring(path, ring.coords)
        pen = QPen(ALERT)
        pen.setCosmetic(True)
        pen.setWidthF(1.6)
        self._add(node_id, path, pen, QBrush(ALERT_FILL), z=8)

    def show_heightmap(self, node_id, hm):
        """Probed points as a diverging field, with the probed boundary marked.

        Diverging rather than sequential because the quantity of interest is
        deviation from flat and its sign matters: blue below the reference,
        amber above, and anything near zero fading toward the board so a flat
        map disappears instead of shouting.

        A grid is drawn as swatches slightly larger than the sample spacing, so
        it reads as the continuous field it is interpolated into. Scattered
        points stay discrete dots, because between them there is nothing but a
        guess and the picture should not pretend otherwise.
        """
        low, high = hm.z_range()
        extent = max(abs(low), abs(high), 1e-6)

        if hm.regular and len(hm.xs) > 1 and len(hm.ys) > 1:
            # Each axis gets its own half-width. One shared size makes a grid
            # with unequal spacing band along the coarser axis.
            wx = max((hm.xs[-1] - hm.xs[0]) / (len(hm.xs) - 1) * 0.5, 1e-3)
            wy = max((hm.ys[-1] - hm.ys[0]) / (len(hm.ys) - 1) * 0.5, 1e-3)
            square = True
        else:
            x0, y0, x1, y1 = hm.bounds()
            wx = wy = max(max(x1 - x0, y1 - y0) / 40.0, 0.2)
            square = False

        for x, y, z in hm.points:
            path = QPainterPath()
            if square:
                path.addRect(QRectF(x - wx, y - wy, wx * 2, wy * 2))
            else:
                path.addEllipse(QPointF(x, y), wx, wy)
            colour = _height_colour((z - hm.offset) / extent)
            pen = QPen(colour.lighter(130))
            pen.setCosmetic(True)
            pen.setWidthF(0.8)
            self._add(node_id, path, pen, QBrush(colour), z=-0.5)

        x0, y0, x1, y1 = hm.bounds()
        frame = QPainterPath()
        frame.addRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        edge = QPen(HEIGHT_EDGE)
        edge.setCosmetic(True)
        edge.setWidthF(1.0)
        edge.setStyle(Qt.DashLine)
        self._add(node_id, frame, edge, None, z=-0.4)

    def show_drills(self, node_id, hits):
        path = QPainterPath()
        for x, y, dia in hits:
            r = max(dia, 0.05) / 2
            path.addEllipse(QPointF(x, y), r, r)
        pen = QPen(DRILL)
        pen.setCosmetic(True)
        pen.setWidthF(1.2)
        self._add(node_id, path, pen, QBrush(BACKGROUND), z=3)

    # -- drawing -----------------------------------------------------------

    def start_draw(self, kind: str):
        """kind is 'rect' or 'poly'. Drag for a rectangle; click points and
        double-click (or right-click) to close a polygon. Escape cancels."""
        self._draw_kind = kind
        self._draw_points = []
        self.setDragMode(QGraphicsView.NoDrag)
        self.viewport().setCursor(Qt.CrossCursor)
        self.setFocus()

    def cancel_draw(self):
        self._draw_kind = None
        self._draw_points = []
        self._clear_preview()
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.viewport().setCursor(Qt.ArrowCursor)
        self.draw_finished.emit()

    def _clear_preview(self):
        if self._preview is not None:
            self.scene().removeItem(self._preview)
            self._preview = None

    def _draw_preview(self, points, closed=False):
        self._clear_preview()
        if len(points) < 2:
            return
        path = QPainterPath()
        path.moveTo(points[0][0], points[0][1])
        for x, y in points[1:]:
            path.lineTo(x, y)
        if closed:
            path.closeSubpath()
        pen = QPen(REGION)
        pen.setCosmetic(True)
        pen.setWidthF(1.6)
        pen.setStyle(Qt.DashLine)
        item = QGraphicsPathItem(path)
        item.setPen(pen)
        item.setZValue(10)
        self.scene().addItem(item)
        self._preview = item

    def _emit_shape(self, kind, points):
        self.shape_drawn.emit({"type": kind, "points": [[x, y] for x, y in points]})
        self.cancel_draw()

    def keyPressEvent(self, event):
        if self._measuring and event.key() == Qt.Key_Escape:
            self.clear_measure()
            return
        if self._draw_kind and event.key() == Qt.Key_Escape:
            self.cancel_draw()
            return
        if self._draw_kind == "poly" and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if len(self._draw_points) >= 3:
                self._emit_shape("poly", self._draw_points)
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # Middle-drag pans in every mode. Left-drag is spoken for while
        # measuring or drawing, and a mode you cannot pan out of is a trap.
        if event.button() == Qt.MiddleButton:
            self._pan = event.position().toPoint()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        if self._measuring:
            pos = self.mapToScene(event.position().toPoint())
            if event.button() == Qt.RightButton:
                self.clear_measure()
                return
            self._measure_click((pos.x(), pos.y()),
                                bool(event.modifiers() & Qt.ShiftModifier))
            return
        if not self._draw_kind and self._edit_node:
            pos = self.mapToScene(event.position().toPoint())
            point = (pos.x(), pos.y())
            hit = self._hit_handle(point)
            if event.button() == Qt.RightButton:
                if hit and self._delete_vertex(*hit):
                    return
                return
            if hit:
                self._drag = ("handle", hit[0], hit[1], None)
                return
            body = self._hit_body(point)
            if body is not None:
                self._drag = ("body", body, None, point)
                return
        if not self._draw_kind:
            return super().mousePressEvent(event)
        pos = self.mapToScene(event.position().toPoint())
        point = (pos.x(), pos.y())
        if self._draw_kind == "rect":
            self._draw_points = [point]
        else:
            if event.button() == Qt.RightButton:
                if len(self._draw_points) >= 3:
                    self._emit_shape("poly", self._draw_points)
                else:
                    self.cancel_draw()
                return
            self._draw_points.append(point)
            self._draw_preview(self._draw_points)

    def mouseReleaseEvent(self, event):
        if self._pan is not None and event.button() == Qt.MiddleButton:
            self._pan = None
            self.viewport().setCursor(
                Qt.CrossCursor if (self._measuring or self._draw_kind) else Qt.ArrowCursor)
            return
        if self._measuring:
            return
        if self._drag:
            self._drag = None
            self._commit()
            return
        if self._draw_kind == "rect" and self._draw_points:
            pos = self.mapToScene(event.position().toPoint())
            start = self._draw_points[0]
            if abs(pos.x() - start[0]) > 1e-3 and abs(pos.y() - start[1]) > 1e-3:
                self._emit_shape("rect", [start, (pos.x(), pos.y())])
            return
        if self._draw_kind:
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._measuring:
            return
        if not self._draw_kind and self._edit_node:
            pos = self.mapToScene(event.position().toPoint())
            if self._insert_vertex((pos.x(), pos.y())):
                return
        if self._draw_kind == "poly" and len(self._draw_points) >= 3:
            self._emit_shape("poly", self._draw_points)
            return
        super().mouseDoubleClickEvent(event)

    # -- editing -----------------------------------------------------------

    def set_editable(self, node_id, shapes):
        """Show drag handles for a Region node's shapes. Pass None to stop."""
        self._edit_node = node_id
        self._edit_shapes = [
            {"type": s["type"], "points": [list(p) for p in s["points"]]}
            for s in (shapes or [])
        ]
        self.refresh_handles()

    def _clear_handles(self):
        for item in self._handles:
            self.scene().removeItem(item)
        self._handles = []

    @staticmethod
    def shape_points(shape):
        """Draggable points of a shape, in drawing order."""
        if shape["type"] == "rect":
            (x0, y0), (x1, y1) = shape["points"]
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(x, y) for x, y in shape["points"]]

    def _scene_px(self, pixels: float) -> float:
        scale = abs(self.transform().m11()) or 1.0
        return pixels / scale

    def refresh_handles(self):
        self._clear_handles()
        if not self._edit_node or not self._edit_shapes:
            return
        half = self._scene_px(4)
        outline = QPainterPath()
        handles = QPainterPath()
        for shape in self._edit_shapes:
            points = self.shape_points(shape)
            if len(points) < 2:
                continue
            outline.moveTo(points[0][0], points[0][1])
            for x, y in points[1:]:
                outline.lineTo(x, y)
            outline.closeSubpath()
            for x, y in points:
                handles.addRect(x - half, y - half, half * 2, half * 2)

        pen = QPen(REGION)
        pen.setCosmetic(True)
        pen.setWidthF(1.2)
        for path, brush in ((outline, None), (handles, QBrush(REGION))):
            item = QGraphicsPathItem(path)
            item.setPen(pen)
            item.setBrush(brush or QBrush(Qt.NoBrush))
            item.setZValue(9)
            self.scene().addItem(item)
            self._handles.append(item)

    def _snap(self, x, y):
        if not self.snap:
            return x, y
        step = self.snap
        return round(x / step) * step, round(y / step) * step

    def _hit_handle(self, pos):
        tol = self._scene_px(7)
        for si, shape in enumerate(self._edit_shapes):
            for pi, (x, y) in enumerate(self.shape_points(shape)):
                if abs(x - pos[0]) <= tol and abs(y - pos[1]) <= tol:
                    return si, pi
        return None

    def _hit_body(self, pos):
        point = QPointF(*pos)
        for si, shape in enumerate(self._edit_shapes):
            path = QPainterPath()
            points = self.shape_points(shape)
            if len(points) < 3:
                continue
            path.moveTo(points[0][0], points[0][1])
            for x, y in points[1:]:
                path.lineTo(x, y)
            path.closeSubpath()
            if path.contains(point):
                return si
        return None

    def _move_point(self, si, pi, x, y):
        shape = self._edit_shapes[si]
        x, y = self._snap(x, y)
        if shape["type"] == "rect":
            (p0, p1) = shape["points"]
            # Handle order is p0, (p1.x, p0.y), p1, (p0.x, p1.y).
            if pi == 0:
                p0[0], p0[1] = x, y
            elif pi == 1:
                p1[0], p0[1] = x, y
            elif pi == 2:
                p1[0], p1[1] = x, y
            else:
                p0[0], p1[1] = x, y
        else:
            shape["points"][pi] = [x, y]

    def _move_shape(self, si, dx, dy):
        # Snap the shape's first point and shift the rest by the same
        # corrected delta, so moving never distorts the geometry.
        points = self._edit_shapes[si]["points"]
        ax, ay = points[0]
        nx, ny = self._snap(ax + dx, ay + dy)
        ddx, ddy = nx - ax, ny - ay
        for point in points:
            point[0] += ddx
            point[1] += ddy

    def _insert_vertex(self, pos):
        """Add a polygon vertex on the nearest edge under the cursor."""
        tol = self._scene_px(8)
        for si, shape in enumerate(self._edit_shapes):
            if shape["type"] != "poly":
                continue
            points = shape["points"]
            count = len(points)
            for i in range(count):
                ax, ay = points[i]
                bx, by = points[(i + 1) % count]
                vx, vy = bx - ax, by - ay
                length2 = vx * vx + vy * vy
                if length2 < 1e-12:
                    continue
                t = ((pos[0] - ax) * vx + (pos[1] - ay) * vy) / length2
                t = max(0.0, min(1.0, t))
                px, py = ax + t * vx, ay + t * vy
                if math.dist((px, py), pos) <= tol:
                    points.insert(i + 1, list(self._snap(px, py)))
                    self._commit()
                    return True
        return False

    def _delete_vertex(self, si, pi):
        shape = self._edit_shapes[si]
        if shape["type"] != "poly" or len(shape["points"]) <= 3:
            return False
        del shape["points"][pi]
        self._commit()
        return True

    def _commit(self):
        self.refresh_handles()
        self.shapes_edited.emit([
            {"type": s["type"], "points": [list(p) for p in s["points"]]}
            for s in self._edit_shapes
        ])

    # -- navigation --------------------------------------------------------

    def fit(self):
        rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            return
        self.fitInView(rect.adjusted(-2, -2, 2, 2), Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        self.refresh_handles()  # handles are a fixed size on screen

    def mouseMoveEvent(self, event):
        if self._pan is not None:
            here = event.position().toPoint()
            delta = here - self._pan
            self._pan = here
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            return
        pos = self.mapToScene(event.position().toPoint())
        self.cursor_moved.emit(pos.x(), pos.y())
        if self._measuring:
            point = (pos.x(), pos.y())
            if self._measure and (event.modifiers() & Qt.ShiftModifier):
                point = self._axis_lock(self._measure[-1], point)
            self._measure_cursor = point
            self.viewport().update()
            return super().mouseMoveEvent(event)
        if self._drag:
            kind, si, pi, last = self._drag
            if kind == "handle":
                self._move_point(si, pi, pos.x(), pos.y())
            else:
                self._move_shape(si, pos.x() - last[0], pos.y() - last[1])
                self._drag = ("body", si, None, (pos.x(), pos.y()))
            self.refresh_handles()
            return
        if self._draw_kind and self._draw_points:
            if self._draw_kind == "rect":
                (x1, y1) = self._draw_points[0]
                self._draw_preview([(x1, y1), (pos.x(), y1), (pos.x(), pos.y()),
                                    (x1, pos.y())], closed=True)
            else:
                self._draw_preview(self._draw_points + [(pos.x(), pos.y())])
        super().mouseMoveEvent(event)


def _append_ring(path: QPainterPath, coords):
    pts = list(coords)
    if len(pts) < 3:
        return
    path.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
