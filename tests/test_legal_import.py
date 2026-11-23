import math
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "parcel_fabric"))

import pytest

from core import cogo as C
from core import units as U
from core.legal_import import parse


def read(name):
    with open(os.path.join(HERE, "..", "examples", name), encoding="utf-8") as f:
        return f.read()


def test_rectangle_closes_with_right_area():
    rows, courses, problems = parse(read("legal_rectangle.txt"), U.US_SURVEY_FOOT_M)
    assert not problems
    assert len(courses) == 4
    r = C.compute((0.0, 0.0), courses)
    assert r.misclose == pytest.approx(0, abs=1e-6)
    assert r.area == pytest.approx(20000, rel=1e-9)


def test_curved_parcel_skips_commencement_and_closes():
    rows, courses, problems = parse(read("legal_curved_parcel.txt"), U.US_SURVEY_FOOT_M)
    assert not problems
    assert [c.kind for c in courses] == [C.CURVE, C.LINE]
    r = C.compute((0.0, 0.0), courses)
    assert r.misclose == pytest.approx(0, abs=1e-4)
    # half circle of radius 100 to the right of a west-east chord
    assert r.area == pytest.approx(math.pi * 100 * 100 / 2, rel=1e-6)


def test_metres_convert_to_fabric_feet():
    text = "Beginning at a rod; thence North, 100 meters; thence East, 100 meters; " \
           "thence South, 100 meters; thence West, 100 meters to the point of beginning."
    _rows, courses, problems = parse(text, U.US_SURVEY_FOOT_M)
    assert not problems
    assert courses[0].distance == pytest.approx(328.0833333, abs=1e-6)


def _closes(text, tol=0.01, n=None):
    _rows, courses, problems = parse(text, U.US_SURVEY_FOOT_M)
    assert not problems, problems
    if n:
        assert len(courses) == n
    r = C.compute((0.0, 0.0), courses)
    assert r.misclose == pytest.approx(0, abs=tol)
    return r


def test_commence_tie_then_boundary_with_more_or_less():
    r = _closes('Commence at the NW corner of Section 12, Township 4 South, Range 16 East; thence '
                'S 89°45\'20" E, along the North line of said Section 12, a distance of 1,320.50 feet to the '
                'POINT OF BEGINNING; thence S 00°14\'40" W, 660.25 feet, more or less; thence N 89°45\'20" W, '
                '330.00 feet; thence N 00°14\'40" E, 660.25 feet; thence S 89°45\'20" E, 330.00 feet to the '
                'POINT OF BEGINNING. Containing 5.00 acres, more or less.', n=4)
    assert r.area == pytest.approx(660.25 * 330.0, rel=1e-4)


def test_dotted_abbreviations_and_begin_without_at():
    _closes("Beginning; thence N. 45° 30' 15\" E. 150.00 ft.; thence S. 44° 29' 45\" E. 200.00 ft.; "
            "thence S. 45° 30' 15\" W. 150.00 ft.; thence N. 44° 29' 45\" W. 200.00 ft. to the P.O.B.", n=4)


def test_hyphen_dms_and_run():
    _closes("Begin at the SE corner; run thence N45-00-00E 100.00 feet; thence S45-00-00E 100.00 feet; "
            "thence S45-00-00W 100.00 feet; thence N45-00-00W 100.00 feet to the POB", n=4)


def test_unlabelled_closing_call_adds_closing_course():
    rows, courses, problems = parse("Beginning at a pin; thence N 00°00'00\" E 100.00 feet; thence S 90°00'00\" E "
                                    "100.00 feet; thence S 00°00'00\" W 100.00 feet; thence back to the Point of "
                                    "Beginning.", U.US_SURVEY_FOOT_M)
    assert not problems
    assert len(courses) == 4 and courses[-1].distance == pytest.approx(100.0)
    assert C.compute((0.0, 0.0), courses).misclose == pytest.approx(0, abs=1e-6)


def test_no_closing_course_when_a_call_was_dropped():
    _rows, courses, problems = parse("Beginning at a pin; thence N 00°00'00\" E 100.00 feet; thence S 90°00'00\" E "
                                     "along the creek; thence S 00°00'00\" W 100.00 feet; to the POB",
                                     U.US_SURVEY_FOOT_M)
    assert problems and len(courses) == 2


def test_curves_chord_tangent_and_left_close():
    for text in (
        'Beginning at a rod; thence N 00°00\'00" E 100.00 feet; thence along a curve to the right having a radius '
        'of 50.00 feet, an arc length of 78.54 feet, a chord bearing of N 45°00\'00" E and a chord length of '
        '70.71 feet; thence S 00°00\'00" E 150.00 feet; thence N 90°00\'00" W 50.00 feet to the point of beginning.',
        'Beginning at a rod; thence N 00°00\'00" E 100.00 feet; thence along a curve to the right with a tangent '
        'bearing of N 00°00\'00" E, radius 50.00 feet and delta 90°00\'00"; thence S 00°00\'00" E 150.00 feet; '
        'thence West 50.00 feet to the POB.',
        'Beginning at a pin; thence S 90°00\'00" E 50.00 feet; thence N 00 E 100 feet; thence curve to the left, '
        'radius of 50.00 feet, arc length 78.54 feet, chord bearing N 45°00\'00" W; thence S 00°00\'00" W '
        '150.00 feet to the POB.'):
        r = _closes(text, tol=0.01, n=4)
        assert r.area == pytest.approx(5000 + math.pi * 2500 / 4, abs=0.1)
