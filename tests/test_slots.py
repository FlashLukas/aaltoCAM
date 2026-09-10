"""Routing the slots an Excellon file asks for.

A slot is a hole that is not round: a tool travels from one end to the other
and the drill's diameter is the slot's width. Nothing plunges once and moves
on, which is why the drilling operations cannot make them and why they were
counted and ignored until now.

What has to hold is that the swept width comes out at the slot's width, not the
tool's -- an offset applied to the wrong side, or forgotten, gives a slot that
looks right in the file and is a tool diameter too narrow in the copper.
"""

from __future__ import annotations

import warnings

import pytest
from shapely.geometry import LineString
from shapely.ops import unary_union

from aaltocam.core import ops
from aaltocam.core.graph import Document

# Two slots and one plain hole. 15 mm long at 0.8 mm wide, and 10 mm at 1.2.
SLOTTED = """M48
METRIC
T1C0.800
T2C1.200
%
G90
G05
T1
X10.000Y10.000
X10.000Y20.000G85X25.000Y20.000
T2
X30.000Y30.000G85X30.000Y40.000
T0
M30
"""

# Same header, no G85 lines at all.
PLAIN = """M48
METRIC
T1C0.800
%
G90
G05
T1
X10.000Y10.000
T0
M30
"""


def loaded(tmp_path, text=SLOTTED, name="slots.drl"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    doc = Document()
    node = doc.add("load_excellon", path=str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return doc, node, doc.evaluate(node.id)


def milled(tmp_path, text=SLOTTED, **params):
    doc, drills, _ = loaded(tmp_path, text)
    node = doc.add("mill_slots", [drills.id], **params)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return doc.evaluate(node.id)


# --- reading them -------------------------------------------------------------

def test_slots_arrive_with_their_geometry(tmp_path):
    _, _, payload = loaded(tmp_path)
    slots = payload.meta["slots"]
    assert len(slots) == 2
    assert slots[0] == pytest.approx((10.0, 20.0, 25.0, 20.0, 0.8))


def test_slots_do_not_become_drills(tmp_path):
    """The drilling side must go on seeing only round holes."""
    _, _, payload = loaded(tmp_path)
    assert len(payload.data) == 1
    assert payload.data[0][:2] == pytest.approx((10.0, 10.0))


def test_a_file_without_slots_reports_none(tmp_path):
    _, _, payload = loaded(tmp_path, PLAIN, "plain.drl")
    assert payload.meta["slots"] == []


def test_the_extent_covers_the_slots(tmp_path):
    """A file can be all slots and no holes; the view still has to fit it."""
    _, _, payload = loaded(tmp_path)
    minx, miny, maxx, maxy = ops.payload_bounds(payload)
    # The far corner belongs to the second slot: it ends at (30, 40) and is
    # 1.2 mm wide, so it reaches 0.6 past each end.
    assert maxx == pytest.approx(30.6, abs=0.01)
    assert maxy == pytest.approx(40.6, abs=0.01)
    # And the near corner to the round hole at (10, 10), 0.8 mm across.
    assert minx == pytest.approx(9.6, abs=0.01)
    assert miny == pytest.approx(9.6, abs=0.01)


# --- cutting them -------------------------------------------------------------

def swept(paths, tool_dia):
    """What the cutter actually removes: its path, fattened by its radius."""
    return unary_union([p.buffer(tool_dia / 2) for p in paths])


def test_the_swept_width_is_the_slots_width_not_the_tools(tmp_path):
    out = milled(tmp_path, tool_dia=0.4)
    # The first slot runs along y = 20 from x = 10 to 25, 0.8 mm wide. Take
    # only its passes: the other slot is elsewhere and would set the bounds.
    first = [p for p in out.data if 19.0 < p.centroid.y < 21.0]
    assert first, "no passes cut along the first slot"
    minx, miny, maxx, maxy = swept(first, 0.4).bounds
    assert maxy - 20.0 == pytest.approx(0.4, abs=0.02)
    assert 20.0 - miny == pytest.approx(0.4, abs=0.02)


def test_the_slot_reaches_both_of_its_ends(tmp_path):
    out = milled(tmp_path, tool_dia=0.4)
    region = swept(out.data, 0.4)
    # Round ends, so the extreme is the centre plus half the slot width.
    assert region.bounds[0] == pytest.approx(10.0 - 0.4, abs=0.02)


def test_a_tool_the_width_of_the_slot_runs_down_the_middle(tmp_path):
    out = milled(tmp_path, tool_dia=0.8, clear_center=False)
    along = [p for p in out.data
             if p.bounds[1] == pytest.approx(20.0)
             and p.bounds[3] == pytest.approx(20.0)]
    assert along, "expected a pass straight along the centreline"
    assert along[0].length == pytest.approx(15.0, abs=0.01)


def test_a_tool_wider_than_the_slot_is_refused_not_fudged(tmp_path):
    out = milled(tmp_path, tool_dia=1.0)     # wider than the 0.8 slot
    assert any("narrower than" in w for w in out.meta.get("warnings", []))
    assert out.meta["slots"] == 1            # only the 1.2 mm one was cut


def test_clearing_leaves_no_slug_in_a_wide_slot(tmp_path):
    """A 1.2 mm slot cut with a 0.3 mm tool needs more than an outline."""
    outline = milled(tmp_path, tool_dia=0.3, clear_center=False)
    cleared = milled(tmp_path, tool_dia=0.3, clear_center=True)
    assert len(cleared.data) > len(outline.data)

    # The cleared version really covers the middle of the slot.
    from shapely.geometry import Point
    middle = Point(30.0, 35.0)               # centre of the vertical slot
    assert swept(cleared.data, 0.3).contains(middle)
    assert not swept(outline.data, 0.3).contains(middle)


def test_both_slots_are_cut(tmp_path):
    out = milled(tmp_path, tool_dia=0.4)
    assert out.meta["slots"] == 2


def test_a_file_with_no_slots_says_so_rather_than_failing(tmp_path):
    out = milled(tmp_path, PLAIN)
    assert out.data == []
    assert any("no slots" in w for w in out.meta.get("warnings", []))


def test_the_output_is_toolpaths_a_job_can_take(tmp_path):
    doc, drills, _ = loaded(tmp_path)
    node = doc.add("mill_slots", [drills.id], tool_dia=0.4)
    job = doc.add("cnc_job", [node.id])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        text = doc.evaluate(job.id).data
    assert "G1 X" in text
    assert text.strip().endswith("M2")


def test_the_declared_dependencies_can_actually_read_a_slot():
    """The floor is not a preference, it is what G85 costs.

    gerbonara below 1.6 does not know the statement at all, and 1.6 needs
    Python 3.12. Loosening either lets pip resolve an install where a drill
    file containing slots fails to open -- the whole file, not merely its
    slots -- which is how this was found, in CI, one release too late.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]

    assert project["requires-python"] == ">=3.12"
    gerbonara = next(d for d in project["dependencies"]
                     if d.startswith("gerbonara"))
    assert gerbonara == "gerbonara>=1.6"


def test_cut_length_is_reported(tmp_path):
    out = milled(tmp_path, tool_dia=0.4)
    # Two slots, 15 mm and 10 mm, each cut as a loop around the centreline.
    assert out.meta["cut_length"] > 40.0
