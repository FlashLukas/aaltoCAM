"""The bottom side is where a CAM tool can be confidently, invisibly wrong.

Mirroring each layer about its own bounding box looks right on screen and
drills through the wrong pads. These tests pin the property that matters:
every layer moves by the same rule, about the same axis.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aaltocam.core.discover import build_board  # noqa: E402

BOARD = os.environ.get("AALTOCAM_TEST_PLOT")   # a folder with F_Cu, B_Cu, Edge_Cuts, drills


def _needs_board():
    if not BOARD or not os.path.isdir(BOARD):
        import unittest
        raise unittest.SkipTest("set AALTOCAM_TEST_PLOT to a two-sided plot folder")


def test_top_side_has_no_transforms():
    _needs_board()
    doc, _ = build_board(BOARD, "top")
    assert not [n for n in doc.nodes.values() if n.op == "transform"]
    assert doc.source["side"] == "top"


def test_bottom_side_mirrors_every_layer_about_one_axis():
    _needs_board()
    doc, _ = build_board(BOARD, "bottom")
    flips = [n for n in doc.nodes.values() if n.op == "transform"]
    assert flips, "bottom side must mirror"

    # One shared reference, or the layers drift apart.
    references = {n.inputs[1] for n in flips}
    assert len(references) == 1, references
    assert all(n.params["mirror"] == "y" for n in flips)

    reference = doc.evaluate(list(references)[0])
    bounds = reference.data.bounds
    axis = (bounds[0] + bounds[2]) / 2

    for node in flips:
        before = doc.evaluate(node.inputs[0])
        after = doc.evaluate(node.id)
        if before.kind == "drills":
            for (x, y, _d), (nx, ny, _nd) in zip(before.data, after.data):
                assert abs(nx - (2 * axis - x)) < 1e-9
                assert abs(ny - y) < 1e-9
        else:
            b, a = before.data.bounds, after.data.bounds
            assert abs(a[0] - (2 * axis - b[2])) < 1e-6
            assert abs(a[2] - (2 * axis - b[0])) < 1e-6
            assert abs(a[1] - b[1]) < 1e-6 and abs(a[3] - b[3]) < 1e-6
            assert abs(before.data.area - after.data.area) < 1e-9


def test_cam_reads_the_flipped_layers():
    """A mirror nobody is wired to is worse than no mirror at all."""
    _needs_board()
    doc, _ = build_board(BOARD, "bottom")
    flipped = {n.id for n in doc.nodes.values() if n.op == "transform"}
    for op in ("isolate", "cutout", "drill_holes", "mill_holes"):
        for node in [n for n in doc.nodes.values() if n.op == op]:
            assert set(node.inputs) & flipped, f"{op} still reads unmirrored geometry"


def test_origin_puts_the_outline_corner_on_zero():
    _needs_board()
    for side in ("top", "bottom"):
        doc, _ = build_board(BOARD, side, origin=True)
        outline_node = [n for n in doc.nodes.values()
                        if n.op == "cutout"][0].inputs[1]
        bounds = doc.evaluate(outline_node).data.bounds
        assert abs(bounds[0]) < 1e-6, (side, bounds)
        assert abs(bounds[1]) < 1e-6, (side, bounds)


def test_origin_moves_every_layer_by_the_same_delta():
    _needs_board()
    doc, _ = build_board(BOARD, "top", origin=True)
    moves = [n for n in doc.nodes.values() if n.op == "transform"]
    assert moves, "origin must place the layers"

    deltas = set()
    for node in moves:
        before, after = doc.evaluate(node.inputs[0]), doc.evaluate(node.id)
        if before.kind == "drills":
            dx = after.data[0][0] - before.data[0][0]
            dy = after.data[0][1] - before.data[0][1]
        else:
            dx = after.data.bounds[0] - before.data.bounds[0]
            dy = after.data.bounds[1] - before.data.bounds[1]
        deltas.add((round(dx, 9), round(dy, 9)))
    assert len(deltas) == 1, deltas


def test_a_hole_lands_opposite_itself_on_the_other_side():
    """The test that matters at the machine: flip the board over and the same
    via has to be under the same drill."""
    _needs_board()
    top, _ = build_board(BOARD, "top", origin=True)
    bottom, _ = build_board(BOARD, "bottom", origin=True)

    def holes(doc):
        node = [n for n in doc.nodes.values() if n.op == "drill_holes"][0]
        return sorted(doc.evaluate(node.inputs[0]).data)

    def width(doc):
        node = [n for n in doc.nodes.values() if n.op == "cutout"][0].inputs[1]
        return doc.evaluate(node).data.bounds[2]

    board_width = width(top)
    assert abs(board_width - width(bottom)) < 1e-9

    top_holes = holes(top)
    bottom_holes = holes(bottom)
    assert len(top_holes) == len(bottom_holes)
    mirrored = sorted((board_width - x, y, d) for x, y, d in top_holes)
    for (ax, ay, _ad), (bx, by, _bd) in zip(mirrored, bottom_holes):
        assert abs(ax - bx) < 1e-6 and abs(ay - by) < 1e-6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
