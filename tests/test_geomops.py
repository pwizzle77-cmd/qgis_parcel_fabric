import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import geomops as G


def close(a, b, tol=1e-9):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def test_arc_center_and_length():
    s, m, e = (100, 0), (0, 100), (-100, 0)        # upper half circle, CCW
    c, r = G.arc_center(s, m, e)
    assert close(c, (0, 0)) and r == pytest.approx(100)
    assert G.arc_length(s, m, e) == pytest.approx(math.pi * 100)
    # clockwise version of the same arc
    assert G.arc_length(e, m, s) == pytest.approx(math.pi * 100)
    assert G.arc_center((0, 0), (1, 1), (2, 2)) is None


def test_split_arc_both_directions():
    r = 100.0
    for s, m, e in [((r, 0), (0, r), (-r, 0)), ((-r, 0), (0, r), (r, 0))]:
        p = (r * math.cos(math.radians(45)), r * math.sin(math.radians(45)))
        a, b = G.split_arc(s, m, e, p)
        assert close(a[0], s) and close(b[2], e) and close(a[2], b[0])
        la, lb = G.arc_length(*a), G.arc_length(*b)
        assert la + lb == pytest.approx(math.pi * r)
        # the mid points really are on the circle
        assert math.hypot(*a[1]) == pytest.approx(r) and math.hypot(*b[1]) == pytest.approx(r)
    # going CCW from (r,0), 45 deg is a quarter of the way
    a, _ = G.split_arc((r, 0), (0, r), (-r, 0), (70.7106781, 70.7106781))
    assert G.arc_length(*a) == pytest.approx(math.pi * r / 4, rel=1e-6)


def test_split_polyline():
    first, second, f = G.split_polyline([(0, 0), (10, 0), (10, 10)], (10, 4))
    assert first == [(0, 0), (10, 0), (10.0, 4.0)]
    assert second == [(10.0, 4.0), (10, 10)]
    assert f == pytest.approx(14 / 20)
    assert G.split_polyline([(0, 0), (10, 0)], (0, 0)) is None


def test_offsets():
    a, b = G.offset_segment((0, 0), (0, 10), 5)        # going north, right = east
    assert close(a, (5, 0)) and close(b, (5, 10))
    s, m, e = G.offset_arc((100, 0), (0, 100), (-100, 0), 10)   # CCW, right = outward
    assert math.hypot(*s) == pytest.approx(110)
    s, m, e = G.offset_arc((-100, 0), (0, 100), (100, 0), 10)   # CW, right = inward
    assert math.hypot(*s) == pytest.approx(90)
    with pytest.raises(ValueError):
        G.offset_arc((-100, 0), (0, 100), (100, 0), 150)


def test_prorate():
    assert G.prorate(100.04, 100.0, 25.0) == pytest.approx(25.01)
    assert G.prorate(None, 100.0, 25.0) == 25.0
