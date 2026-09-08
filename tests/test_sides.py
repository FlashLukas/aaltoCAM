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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
