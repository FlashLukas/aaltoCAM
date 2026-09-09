"""Excellon units, and which drill file a folder import picks.

Both of these failed silently on a real KiCad export. An inch drill file loaded
as though it were millimetres yields perfectly well-formed geometry a factor of
25.4 away from the copper, and the folder importer chose the alphabetically
first drill file, which for KiCad is NPTH -- the mounting holes, or on a board
without any, nothing at all.

Every fixture in the suite was metric before this, which is exactly why neither
was caught. The inch file below is the header KiCad 8 actually writes.
"""

from __future__ import annotations

import warnings

import pytest

from aaltocam.core import discover, ops
from aaltocam.core.graph import Document

# KiCad 8, inches. 0.0276 in = 0.70104 mm, 0.0728 in = 1.849 mm.
INCH_DRL = """M48
; DRILL file {KiCad 8.0.8} date 2025-01-27T11:59:12+0200
; FORMAT={-:-/ absolute / inch / decimal}
FMAT,2
INCH
T1C0.0276
T2C0.0728
%
G90
G05
T1
X3.7205Y-3.1299
X3.7205Y-3.2480
T2
X4.0000Y-3.5000
T0
M30
"""

# The same three holes, written in millimetres.
MM_DRL = """M48
FMAT,2
METRIC
T1C0.70104
T2C1.84912
%
G90
G05
T1
X94.5007Y-79.4995
X94.5007Y-82.4992
T2
X101.6000Y-88.9000
T0
M30
"""


def load(tmp_path, text, name):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    doc = Document()
    doc.base_dir = str(tmp_path)
    node = doc.add("load_excellon", path=str(path))
    with warnings.catch_warnings():
        # gerbonara warns about KiCad's bare INCH statement; not our problem.
        warnings.simplefilter("ignore")
        return doc.evaluate(node.id)


# --- units -------------------------------------------------------------------

def test_an_inch_file_is_converted_to_mm(tmp_path):
    hits = load(tmp_path, INCH_DRL, "inch.drl").data
    assert len(hits) == 3
    xs = [h[0] for h in hits]
    # 3.7205 in is 94.5 mm, not 3.72.
    assert min(xs) == pytest.approx(94.5007, abs=1e-3)
    assert max(xs) == pytest.approx(101.6, abs=1e-3)


def test_inch_diameters_are_converted_too(tmp_path):
    hits = load(tmp_path, INCH_DRL, "inch.drl").data
    dias = sorted({round(h[2], 4) for h in hits})
    # 0.0276 in and 0.0728 in, not 0.0276 mm and 0.0728 mm.
    assert dias == [pytest.approx(0.7010, abs=1e-3), pytest.approx(1.8491, abs=1e-3)]


def test_a_metric_file_is_unchanged(tmp_path):
    hits = load(tmp_path, MM_DRL, "mm.drl").data
    assert min(h[0] for h in hits) == pytest.approx(94.5007, abs=1e-3)


def test_inch_and_metric_files_agree(tmp_path):
    inch = sorted(load(tmp_path, INCH_DRL, "inch.drl").data)
    metric = sorted(load(tmp_path, MM_DRL, "mm.drl").data)
    assert len(inch) == len(metric) == 3
    for a, b in zip(inch, metric):
        assert a[0] == pytest.approx(b[0], abs=1e-3)
        assert a[1] == pytest.approx(b[1], abs=1e-3)
        assert a[2] == pytest.approx(b[2], abs=1e-3)


def test_the_diameter_filter_works_in_mm(tmp_path):
    """The filter's units follow the holes, so an inch file filters sanely.

    Before the conversion a 0.70 mm hole measured 0.0276, and a filter set to
    exclude anything under 0.5 mm silently threw away every hole in the file.
    """
    path = tmp_path / "inch.drl"
    path.write_text(INCH_DRL, encoding="utf-8")
    doc = Document()
    doc.base_dir = str(tmp_path)
    node = doc.add("load_excellon", path=str(path), min_dia=1.0, max_dia=10.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        hits = doc.evaluate(node.id).data
    assert len(hits) == 1                      # only the 1.85 mm holes
    assert hits[0][2] == pytest.approx(1.8491, abs=1e-3)


def test_holes_land_on_the_board(tmp_path):
    """The failure as it was actually seen: holes nowhere near the copper."""
    hits = load(tmp_path, INCH_DRL, "inch.drl").data
    x0, y0, x1, y1 = ops.payload_bounds(load(tmp_path, INCH_DRL, "inch.drl"))
    # A board of this size sits in the tens of millimetres, not single digits.
    assert x1 > 90 and x0 > 90


# --- which drill file ---------------------------------------------------------

def test_plated_wins_over_non_plated():
    chosen = discover._preferred_drill([
        "/board/thing-NPTH.drl",
        "/board/thing-PTH.drl",
    ])
    assert chosen.endswith("PTH.drl") and "NPTH" not in chosen


def test_order_on_disk_does_not_matter():
    both = ["/board/thing-PTH.drl", "/board/thing-NPTH.drl"]
    assert discover._preferred_drill(both) == discover._preferred_drill(both[::-1])


def test_npth_is_used_when_it_is_the_only_one():
    assert discover._preferred_drill(["/board/thing-NPTH.drl"]).endswith("NPTH.drl")


def test_unrelated_names_stay_alphabetical():
    chosen = discover._preferred_drill(["/board/b.drl", "/board/a.drl"])
    assert chosen.endswith("a.drl")


def test_the_importer_picks_the_plated_file(tmp_path):
    (tmp_path / "board-F_Cu.gbr").write_text("%FSLAX46Y46*%\n%MOMM*%\nM02*\n", encoding="utf-8")
    (tmp_path / "board-NPTH.drl").write_text(MM_DRL, encoding="utf-8")
    (tmp_path / "board-PTH.drl").write_text(MM_DRL, encoding="utf-8")
    found = discover.classify(str(tmp_path))
    assert len(found["drills"]) == 2
    assert discover._preferred_drill(found["drills"]).endswith("PTH.drl")
    assert "NPTH" not in discover._preferred_drill(found["drills"])
