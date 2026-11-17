import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import units as U
from core import cogo as C


def deg(az):
    return math.degrees(az)


def test_parse_bearing_forms():
    for t in ["N 45-30-15 E", "N45°30'15\"E", "n 45 30 15 e", "N45.3015E", "N 45d30m15s E"]:
        assert deg(U.parse_bearing(t)) == pytest.approx(45.50416667, abs=1e-6), t
    assert deg(U.parse_bearing("S 10-00-00 E")) == pytest.approx(170)
    assert deg(U.parse_bearing("S 10-00-00 W")) == pytest.approx(190)
    assert deg(U.parse_bearing("N 10-00-00 W")) == pytest.approx(350)
    assert deg(U.parse_bearing("Due West")) == pytest.approx(270)
    with pytest.raises(U.ParseError):
        U.parse_bearing("N 95 E")
    with pytest.raises(U.ParseError):
        U.parse_bearing("N 45-70-00 E")


def test_format_bearing_round_trip_and_rounding():
    for t in ["N 45°30'15\" E", "S 0°00'01\" W", "N 89°59'59\" W"]:
        assert U.azimuth_to_bearing(U.parse_bearing(t)) == t
    # 359°59'59.7" rounds to north, not N 0°00'60" W
    assert U.azimuth_to_bearing(math.radians(359.99992)) == "N 0°00'00\" E"


def test_distance_units():
    assert U.parse_distance("100") == 100
    assert U.parse_distance("2 ch") == pytest.approx(132)
    assert U.parse_distance("4rd") == pytest.approx(66)
    assert U.parse_distance("100 m") == pytest.approx(328.0833333, abs=1e-6)  # US survey ft
    assert U.parse_distance("100 m", foot_m=U.INTL_FOOT_M) == pytest.approx(328.0839895, abs=1e-6)


def square(close=True):
    b = U.parse_bearing
    cs = [C.Course(C.LINE, b("N 0 E"), 100), C.Course(C.LINE, b("N 90 E"), 100),
          C.Course(C.LINE, b("S 0 E"), 100), C.Course(C.LINE, b("S 90 W"), 100 if close else 100.05)]
    return cs


def test_closed_square():
    r = C.compute((1000.0, 2000.0), square())
    assert r.misclose == pytest.approx(0, abs=1e-9)
    assert r.area == pytest.approx(10000)
    assert r.perimeter == pytest.approx(400)


def test_compass_rule_closes():
    r = C.compute((0.0, 0.0), square(close=False))
    assert r.misclose == pytest.approx(0.05)
    adj = C.compass_rule(r, (0.0, 0.0))
    r2 = C.compute((0.0, 0.0), adj)
    assert r2.misclose == pytest.approx(0, abs=1e-9)


def test_curve_lot_area_and_closure():
    """Square lot whose east side is replaced by a curve."""
    b = U.parse_bearing
    R = 100.0
    # semicircle bulging east from (100,100) to (100,0): right curve going south
    arc = math.pi * R / 2  # quarter circle, chord = R*sqrt(2)
    # build a quarter-circle lot: north 100, curve (right) to (100,0)... use
    # radial geometry: centre at (0,0), start (0,100) end (100,0)
    cs = [C.Course(C.LINE, b("N 0 E"), 100),
          C.Course(C.CURVE, b("N 90 E"), None, R, arc, C.DIR_TANGENT),
          C.Course(C.LINE, b("S 90 W"), 100)]
    r = C.compute((0.0, 0.0), cs)
    assert r.misclose == pytest.approx(0, abs=1e-9)
    assert r.area == pytest.approx(math.pi * R * R / 4)   # quarter circle
    leg = r.legs[1]
    assert leg.center[0] == pytest.approx(0) and leg.center[1] == pytest.approx(0)
    assert math.hypot(*leg.mid) == pytest.approx(R)


def test_left_curve_area_reduces():
    """Same lot traversed counter-clockwise uses a LEFT curve."""
    b = U.parse_bearing
    R = 100.0
    arc = math.pi * R / 2
    cs = [C.Course(C.LINE, b("N 90 E"), 100),
          C.Course(C.CURVE, b("N 0 E"), None, -R, arc, C.DIR_TANGENT),
          C.Course(C.LINE, b("S 0 E"), 100)]
    r = C.compute((0.0, 0.0), cs)
    assert r.misclose == pytest.approx(0, abs=1e-9)
    assert r.area == pytest.approx(math.pi * R * R / 4)


def test_tangent_prev_and_radial():
    b = U.parse_bearing
    R = 50.0
    arc = 30.0
    c1 = [C.Course(C.LINE, b("N 0 E"), 10), C.Course(C.CURVE, None, None, R, arc, C.DIR_TANGENT_PREV)]
    c2 = [C.Course(C.LINE, b("N 0 E"), 10), C.Course(C.CURVE, b("N 90 E"), None, R, arc, C.DIR_RADIAL)]
    e1 = C.compute((0, 0), c1, close_to=False).end_point
    e2 = C.compute((0, 0), c2, close_to=False).end_point
    assert e1[0] == pytest.approx(e2[0]) and e1[1] == pytest.approx(e2[1])


def test_loop_misclose_with_reversed_lines():
    b = U.parse_bearing
    # lines stored in mixed directions
    legs = [(C.Course(C.LINE, b("N 0 E"), 100), False),
            (C.Course(C.LINE, b("S 90 W"), 100), True),   # stored W, walked E
            (C.Course(C.LINE, b("S 0 E"), 100), False),
            (C.Course(C.LINE, b("N 90 E"), 100.1), True)]  # stored E, walked W
    dx, dy, per, area = C.loop_misclose(legs)
    assert math.hypot(dx, dy) == pytest.approx(0.1)
    assert per == pytest.approx(400.1)


def test_curve_helpers():
    assert C.arc_length_from_delta(100, math.radians(90)) == pytest.approx(math.pi * 50)
    chord = 2 * 100 * math.sin(math.radians(45))
    assert C.arc_length_from_chord(100, chord) == pytest.approx(math.pi * 50)
    assert C.arc_length_from_tangent(100, 100) == pytest.approx(math.pi * 50)
