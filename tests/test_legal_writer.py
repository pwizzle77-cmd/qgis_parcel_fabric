import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import cogo as C
from core import geomops as G
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


def _prepare(pts, tol=0.02):
    """Same order as the description dialog: find arcs, then clockwise from the northwest."""
    ring = W.find_arcs(poly(pts), tol=tol)
    ring = W.merge_collinear(ring)
    ring = W.orient(ring, clockwise=True)
    return W.rotate_to(ring, W.northwest_index(ring))


def _arc_sweep_deg(seg):
    return abs(math.degrees(G.arc_sweep(*seg[1:])[3]))


def test_rectangle_is_not_read_as_a_curve():
    found = W.find_arcs(poly([(0, 0), (200, 0), (200, 100), (0, 100)]))
    assert len(found) == 4 and all(s[0] == "line" for s in found)


def test_full_circle_is_two_arcs():
    radius = 100.0
    pts = [(radius * math.sin(math.radians(a)), radius * math.cos(math.radians(a))) for a in range(0, 360, 10)]
    ring = _prepare(pts, tol=0.05)
    arcs = [s for s in ring if s[0] == "arc"]
    assert len(arcs) == 2 and len(ring) == 2
    assert abs(W.signed_area(ring)) == pytest.approx(math.pi * radius * radius, rel=1e-4)
    text, area = W.write(ring)
    assert area == pytest.approx(math.pi * radius * radius, rel=1e-6)
    r = round_trip(text)
    assert r.misclose == pytest.approx(0, abs=0.05)
    assert r.area == pytest.approx(math.pi * radius * radius, rel=1e-3)


def test_rounded_rectangle_keeps_each_corner():
    width, height, radius = 200.0, 100.0, 20.0
    pts = [(radius, 0.0)]
    pts.append((width - radius, 0.0))
    pts += _arc((width - radius, radius), radius, -90, 0)
    pts.append((width, height - radius))
    pts += _arc((width - radius, height - radius), radius, 0, 90)
    pts.append((radius, height))
    pts += _arc((radius, height - radius), radius, 90, 180)
    pts.append((0.0, radius))
    pts += _arc((radius, radius), radius, 180, 270)
    pts = _dedupe(pts)
    # start in the middle of the southeast fillet, where the ring join used to split it
    start = min(range(len(pts)), key=lambda i: math.hypot(pts[i][0] - width, pts[i][1] - 10))
    pts = pts[start:] + pts[:start]
    ring = _prepare(pts)
    arcs = [s for s in ring if s[0] == "arc"]
    assert len(arcs) == 4
    assert all(_arc_sweep_deg(s) == pytest.approx(90, abs=1) for s in arcs)
    assert all(G.arc_sweep(*s[1:])[1] == pytest.approx(radius, abs=0.05) for s in arcs)
    expect = width * height - (4 - math.pi) * radius * radius
    text, area = W.write(ring)
    assert text.count("non-tangent") == 0 and text.count("central angle of 90°00'00\"") == 4
    assert area == pytest.approx(expect, rel=1e-6)
    r = round_trip(text)
    assert r.misclose == pytest.approx(0, abs=0.05)
    assert r.area == pytest.approx(expect, rel=1e-3)


def test_bulge_and_bite_name_the_centre():
    radius = 40.0
    half = math.pi * radius * radius / 2.0
    # Semicircle of radius 40 on the east side, between (200, 90) and (200, 10).
    bulge = [
        ("line", (0, 100), (200, 100)),
        ("line", (200, 100), (200, 90)),
        ("arc", (200, 90), (240, 50), (200, 10)),
        ("line", (200, 10), (200, 0)),
        ("line", (200, 0), (0, 0)),
        ("line", (0, 0), (0, 100)),
    ]
    bite = [
        ("line", (0, 100), (200, 100)),
        ("line", (200, 100), (200, 90)),
        ("arc", (200, 90), (160, 50), (200, 10)),
        ("line", (200, 10), (200, 0)),
        ("line", (200, 0), (0, 0)),
        ("line", (0, 0), (0, 100)),
    ]
    bulge_text, bulge_area = W.write(W.orient(bulge, clockwise=True))
    bite_text, bite_area = W.write(W.orient(bite, clockwise=True))
    assert "curve to the right" in bulge_text and "concave Westerly" in bulge_text
    assert "curve to the left" in bite_text and "concave Easterly" in bite_text
    assert "central angle of 180°00'00\"" in bulge_text and "central angle of 180°00'00\"" in bite_text
    assert bulge_area == pytest.approx(20000 + half, rel=1e-9)
    assert bite_area == pytest.approx(20000 - half, rel=1e-9)
    for text, expect in ((bulge_text, 20000 + half), (bite_text, 20000 - half)):
        r = round_trip(text)
        assert r.misclose == pytest.approx(0, abs=0.05)
        assert r.area == pytest.approx(expect, rel=1e-3)


def test_major_arc_round_trips():
    radius = 100.0
    start = (radius, 0.0)
    mid = (radius * math.cos(math.radians(135)), radius * math.sin(math.radians(135)))
    end = (0.0, -radius)
    ring = W.orient([("arc", start, mid, end), ("line", end, start)], clockwise=True)
    text, area = W.write(ring)
    assert "central angle of 270°" in text
    r = round_trip(text)
    assert r.misclose == pytest.approx(0, abs=0.05)
    assert r.area == pytest.approx(area, rel=1e-3)


def _arc(center, radius, a0, a1, step=5.0):
    pts = []
    a = a0
    while a < a1 - 1e-9:
        a += step
        if a > a1:
            a = a1
        pts.append((center[0] + radius * math.cos(math.radians(a)),
                    center[1] + radius * math.sin(math.radians(a))))
    return pts


def _dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= 1e-6:
        out.pop()
    return out


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
