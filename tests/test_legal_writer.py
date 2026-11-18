import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import cogo as C
from core import legal_writer as W
from core import units as U
from core.legal_import import parse


def poly(pts):
    return [("line", a, b) for a, b in zip(pts, pts[1:] + pts[:1])]


def round_trip(text):
    """Read our own description back with the COGO Workbench parser."""
    _rows, courses, problems = parse(text, U.US_SURVEY_FOOT_M, boundary_only=False)
    assert not problems, problems
    return C.compute((0.0, 0.0), courses)


def test_square_clockwise_from_northwest():
    ring = poly([(0, 0), (200, 0), (200, 100), (0, 100)])         # counter-clockwise input
    ring = W.orient(ring, clockwise=True)
    ring = W.rotate_to(ring, W.northwest_index(ring))
    assert ring[0][1] == (0, 100)
    text, area = W.write(ring)
    assert area == pytest.approx(20000)
    assert "N 90°00'00\" E, a distance of 200.00 feet" in text
    assert "Containing 0.459 acres (20,000 square feet)" in text
    r = round_trip(text)
    assert r.misclose == pytest.approx(0, abs=0.01)
    assert r.area == pytest.approx(20000, rel=1e-4)


def test_collinear_vertices_are_merged():
    ring = poly([(0, 0), (100, 0), (200, 0.00001), (200, 100), (0, 100)])
    merged = W.merge_collinear(ring, angle_tol_sec=60)
    assert len(merged) == 4


def test_curve_description_round_trips():
    R = 100.0
    # quarter-circle lot: north 100, curve right to (100, 0), west 100 back
    s, m, e = (0, 100), (R * math.sin(math.radians(45)), R * math.cos(math.radians(45))), (100, 0)
    ring = [("line", (0, 0), (0, 100)), ("arc", s, m, e), ("line", (100, 0), (0, 0))]
    ring = W.orient(ring, clockwise=True)
    text, area = W.write(ring)
    assert area == pytest.approx(math.pi * R * R / 4, rel=1e-9)
    assert "non-tangent curve to the right" in text     # north line meets the arc at 90 degrees
    assert "radius of 100.00 feet" in text and "central angle of 90°00'00\"" in text
    r = round_trip(text)
    assert r.misclose == pytest.approx(0, abs=0.01)
    assert r.area == pytest.approx(math.pi * R * R / 4, rel=1e-3)


def test_densified_arc_is_found_again():
    R = 100.0
    arc_pts = [(R * math.sin(math.radians(a)), R * math.cos(math.radians(a))) for a in range(0, 91, 5)]
    pts = [(0, 0)] + arc_pts                  # (0,0) -> (0,100) -> ... arc ... -> (100,0)
    ring = poly(pts)
    found = W.find_arcs(ring, tol=0.01)
    kinds = [s[0] for s in found]
    assert kinds.count("arc") == 1 and len(found) == 3
    assert abs(W.signed_area(found)) == pytest.approx(math.pi * R * R / 4, rel=1e-6)


def test_commencement_and_hole():
    ring = W.orient(poly([(0, 0), (300, 0), (300, 300), (0, 300)]), clockwise=True)
    ring = W.rotate_to(ring, W.northwest_index(ring))
    hole = W.orient(poly([(100, 100), (150, 100), (150, 150), (100, 150)]), clockwise=True)
    hole = W.rotate_to(hole, W.northwest_index(hole))
    text, area = W.write(ring, commence=("the Northwest corner of Section 12", (0, 400)), holes=[hole])
    assert text.startswith("Commence at the Northwest corner of Section 12; thence S 0°00'00\" E, a distance of 100.00 feet")
    r = round_trip(text.split("\n\nLESS AND EXCEPT")[0])     # the outer boundary on its own
    assert r.misclose == pytest.approx(0, abs=0.01)
    assert r.area == pytest.approx(90000, rel=1e-4)
    assert "LESS AND EXCEPT" in text
    assert area == pytest.approx(90000 - 2500)
