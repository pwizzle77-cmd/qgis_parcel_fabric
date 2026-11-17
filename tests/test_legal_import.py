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
