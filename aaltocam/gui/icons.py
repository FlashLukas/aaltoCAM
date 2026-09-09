"""Icons, drawn rather than shipped.

Every icon here is QPainter code on a 100x100 logical grid, scaled to whatever
size is asked for. That keeps them sharp at any DPI, keeps the package free of
binary assets, and lets the line art follow the palette so it stays legible in
a light or a dark theme.

The application mark is the program's own subject: a copper trace with a pad,
and the isolation channel milled around it. It is built the way the program
builds a toolpath -- the outline is the copper shape grown by a tool radius --
so the mark cannot drift away from what the software does.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPixmap)

GRID = 100.0

BOARD = QColor("#15232B")
COPPER = QColor("#D2853F")
COPPER_DIM = QColor("#8C5A2C")
CUT = QColor("#EAF0F2")

_cache: dict = {}


def _stroke(path: QPainterPath, width: float) -> QPainterPath:
    """The outline of a centreline grown by width -- the same operation the
    isolation op performs, which is why the mark looks like its own output."""
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.RoundCap)
    stroker.setJoinStyle(Qt.RoundJoin)
    return stroker.createStroke(path).simplified()


def _trace_path() -> QPainterPath:
    """The centreline of the mark's copper: in from the left, then up to a pad.

    A right angle rather than a diagonal. At 16 px a diagonal turns the whole
    thing into one smooth blob; a corner still reads as a track.
    """
    path = QPainterPath(QPointF(18, 74))
    path.lineTo(60, 74)
    path.lineTo(60, 44)
    return path


def _copper_shape(width: float, pad: float) -> QPainterPath:
    shape = _stroke(_trace_path(), width)
    disc = QPainterPath()
    disc.addEllipse(QPointF(60, 44), pad, pad)
    return shape.united(disc).simplified()


def app_pixmap(size: int, background: bool = True) -> QPixmap:
    """The application mark at one size."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(size / GRID, size / GRID)

    if background:
        board = QPainterPath()
        board.addRoundedRect(QRectF(4, 4, 92, 92), 20, 20)
        painter.fillPath(board, QBrush(BOARD))

    if size < 28:
        # A favicon has room for one idea. Keep the pad and the channel around
        # it; the track is what disappears first and costs least.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(CUT, 7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawEllipse(QPointF(50, 50), 31, 31)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(COPPER))
        painter.drawEllipse(QPointF(50, 50), 22, 22)
        painter.setBrush(QBrush(BOARD if background else CUT))
        painter.drawEllipse(QPointF(50, 50), 8, 8)
        painter.end()
        return pixmap

    # The milled channel: the copper grown by a tool radius, drawn as the line
    # the cutter would follow.
    gap = 8.0
    channel = _copper_shape(18 + 2 * gap, 16 + gap)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(CUT, 3.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawPath(channel)

    copper = _copper_shape(18, 16)
    painter.setPen(Qt.NoPen)
    painter.fillPath(copper, QBrush(COPPER))

    # The drilled via, so the pad reads as a pad and not a blob.
    hole = QPainterPath()
    hole.addEllipse(QPointF(60, 44), 6.5, 6.5)
    painter.fillPath(hole, QBrush(BOARD if background else CUT))

    painter.end()
    return pixmap


def app_icon() -> QIcon:
    key = "app"
    if key not in _cache:
        icon = QIcon()
        for size in (16, 24, 32, 48, 64, 128, 256):
            icon.addPixmap(app_pixmap(size))
        _cache[key] = icon
    return _cache[key]


# ---------------------------------------------------------------------------
# Operation glyphs
#
# Line art on the same 100x100 grid, in one accent and one ink colour so the
# set reads as one family. Each takes (painter, ink, accent).
# ---------------------------------------------------------------------------


def _pen(painter, colour, width=7.0, style=Qt.SolidLine):
    painter.setPen(QPen(colour, width, style, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)


def _gerber(p, ink, accent):
    _pen(p, ink, 6)
    p.drawRoundedRect(QRectF(16, 12, 68, 76), 8, 8)
    _pen(p, accent, 8)
    path = QPainterPath(QPointF(30, 66))
    path.lineTo(48, 66)
    path.lineTo(62, 48)
    p.drawPath(path)
    p.setBrush(QBrush(accent))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(62, 48), 9, 9)
    p.drawEllipse(QPointF(34, 32), 6, 6)


def _excellon(p, ink, accent):
    _pen(p, ink, 6)
    p.drawRoundedRect(QRectF(14, 22, 72, 56), 8, 8)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(accent))
    for x, r in ((33, 10), (52, 7), (68, 5)):
        p.drawEllipse(QPointF(x, 50), r, r)


def _region(p, ink, accent):
    _pen(p, accent, 6, Qt.DashLine)
    p.drawRect(QRectF(18, 24, 64, 52))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(ink))
    for x, y in ((18, 24), (82, 24), (18, 76), (82, 76)):
        p.drawRect(QRectF(x - 5, y - 5, 10, 10))


def _isolate(p, ink, accent):
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(accent))
    p.drawEllipse(QPointF(50, 50), 17, 17)
    _pen(p, ink, 6)
    p.drawEllipse(QPointF(50, 50), 30, 30)


def _clear(p, ink, accent):
    _pen(p, ink, 5)
    p.drawRect(QRectF(16, 22, 68, 56))
    _pen(p, accent, 5)
    for i in range(5):
        y = 30 + i * 11
        p.drawLine(QPointF(22, y), QPointF(78, y))


def _cutout(p, ink, accent):
    _pen(p, ink, 6)
    p.drawRoundedRect(QRectF(28, 28, 44, 44), 5, 5)
    _pen(p, accent, 6, Qt.DashLine)
    p.drawRoundedRect(QRectF(15, 15, 70, 70), 9, 9)


def _internal_cutout(p, ink, accent):
    _pen(p, ink, 6)
    p.drawRoundedRect(QRectF(15, 15, 70, 70), 9, 9)
    _pen(p, accent, 6, Qt.DashLine)
    p.drawRoundedRect(QRectF(36, 32, 28, 36), 5, 5)


def _drill(p, ink, accent):
    _pen(p, ink, 6)
    p.drawLine(QPointF(50, 14), QPointF(50, 40))
    p.drawLine(QPointF(50, 60), QPointF(50, 86))
    p.drawLine(QPointF(14, 50), QPointF(40, 50))
    p.drawLine(QPointF(60, 50), QPointF(86, 50))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(accent))
    p.drawEllipse(QPointF(50, 50), 12, 12)


def _mill_holes(p, ink, accent):
    _pen(p, ink, 6)
    p.drawEllipse(QPointF(50, 50), 15, 15)
    _pen(p, accent, 6, Qt.DashLine)
    p.drawEllipse(QPointF(50, 50), 30, 30)


def _transform(p, ink, accent):
    _pen(p, ink, 6, Qt.DashLine)
    p.drawLine(QPointF(50, 12), QPointF(50, 88))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(accent))
    left = QPainterPath(QPointF(42, 26))
    left.lineTo(42, 74)
    left.lineTo(14, 50)
    left.closeSubpath()
    p.drawPath(left)
    p.setBrush(QBrush(ink))
    right = QPainterPath(QPointF(58, 26))
    right.lineTo(58, 74)
    right.lineTo(86, 50)
    right.closeSubpath()
    p.drawPath(right)


def _panelize(p, ink, accent):
    p.setPen(Qt.NoPen)
    for row in range(2):
        for col in range(2):
            colour = accent if (row + col) == 0 else ink
            p.setBrush(QBrush(colour))
            p.drawRoundedRect(QRectF(16 + col * 38, 16 + row * 38, 30, 30), 4, 4)


def _clearance(p, ink, accent):
    _pen(p, ink, 7)
    p.drawEllipse(QPointF(44, 44), 26, 26)
    p.drawLine(QPointF(63, 63), QPointF(86, 86))
    _pen(p, accent, 7)
    p.drawLine(QPointF(44, 32), QPointF(44, 48))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(accent))
    p.drawEllipse(QPointF(44, 57), 4, 4)


def _cnc_job(p, ink, accent):
    _pen(p, ink, 6)
    path = QPainterPath(QPointF(14, 80))
    path.lineTo(34, 80)
    path.lineTo(34, 58)
    path.lineTo(66, 58)
    path.lineTo(66, 80)
    path.lineTo(86, 80)
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(accent))
    shank = QPainterPath(QPointF(42, 12))
    shank.lineTo(58, 12)
    shank.lineTo(58, 36)
    shank.lineTo(50, 48)
    shank.lineTo(42, 36)
    shank.closeSubpath()
    p.drawPath(shank)


GLYPHS = {
    "load_gerber": _gerber,
    "load_excellon": _excellon,
    "region": _region,
    "isolate": _isolate,
    "clear_copper": _clear,
    "cutout": _cutout,
    "internal_cutout": _internal_cutout,
    "drill_holes": _drill,
    "mill_holes": _mill_holes,
    "transform": _transform,
    "panelize": _panelize,
    "clearance_check": _clearance,
    "cnc_job": _cnc_job,
}


def _initials(p, ink, accent, text):
    """Fallback for an operation with no glyph yet, so a new one is never
    invisible in the palette."""
    p.setPen(QPen(ink))
    font = QFont()
    font.setPixelSize(46)
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRectF(0, 0, GRID, GRID), Qt.AlignCenter, text[:2].upper())


def op_pixmap(op: str, size: int, ink: QColor, accent: QColor) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(size / GRID, size / GRID)
    glyph = GLYPHS.get(op)
    if glyph is not None:
        glyph(painter, ink, accent)
    else:
        _initials(painter, ink, accent, op)
    painter.end()
    return pixmap


def op_icon(op: str, ink: QColor, accent: QColor = COPPER, size: int = 64) -> QIcon:
    key = (op, ink.name(), accent.name(), size)
    if key not in _cache:
        _cache[key] = QIcon(op_pixmap(op, size, ink, accent))
    return _cache[key]


ACTIONS = {
    "duplicate": lambda p, ink, accent: (
        _pen(p, ink, 6),
        p.drawRoundedRect(QRectF(16, 16, 48, 48), 6, 6),
        _pen(p, accent, 6),
        p.drawRoundedRect(QRectF(36, 36, 48, 48), 6, 6),
    ),
    "delete": lambda p, ink, accent: (
        _pen(p, ink, 7),
        p.drawLine(QPointF(18, 30), QPointF(82, 30)),
        p.drawPath(_bin_body()),
        _pen(p, accent, 7),
        p.drawLine(QPointF(42, 18), QPointF(58, 18)),
    ),
}


def _bin_body() -> QPainterPath:
    path = QPainterPath(QPointF(28, 30))
    path.lineTo(33, 84)
    path.lineTo(67, 84)
    path.lineTo(72, 30)
    return path


def action_icon(name: str, ink: QColor, accent: QColor = COPPER, size: int = 64) -> QIcon:
    key = ("action", name, ink.name(), accent.name(), size)
    if key not in _cache:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(size / GRID, size / GRID)
        ACTIONS[name](painter, ink, accent)
        painter.end()
        _cache[key] = QIcon(pixmap)
    return _cache[key]


def clear_cache():
    """Drop cached icons, so a palette change redraws them in the new ink."""
    app = _cache.get("app")
    _cache.clear()
    if app is not None:
        _cache["app"] = app


def write_files(directory: str) -> list[str]:
    """Write the mark out as .ico and .png, for shortcuts and packaging.

    Generated from the same drawing code as the running window icon, so the two
    cannot drift. Qt writes a single-image .ico; Windows scales it down happily
    for a shortcut, and the in-app icon still uses the hand-tuned small sizes.
    """
    import os

    os.makedirs(directory, exist_ok=True)
    written = []
    ico = os.path.join(directory, "aaltocam.ico")
    if app_pixmap(256).save(ico, "ICO"):
        written.append(ico)
    png = os.path.join(directory, "aaltocam.png")
    if app_pixmap(256).save(png, "PNG"):
        written.append(png)
    return written


if __name__ == "__main__":
    import sys

    from PySide6.QtWidgets import QApplication

    QApplication(sys.argv)
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    for path in write_files(target):
        print(path)
