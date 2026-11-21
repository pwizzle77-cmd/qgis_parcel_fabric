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


SECTION9 = """Section 9: For a POINT OF BEGINNING, commence at the SE comer of the SW of the SW 1/4 Of
the NE Ii4 Of Section 9. Township 4 South, Range 16 East, thence run West along the South line Of said
SW 1/4 of the SW 1,'4 of the NE 1/4 a distance of 208 feet: thence run North parallel to the East line of the
SW 1/4 Of the SW 1,'4 of the NE 1/4, a distance of 208 feet; thence run East parallel to the South line Of the
SW 1/4 of the SW 1,'4 Of the NE 1/4 a distance of 208 feet to the East line of said SW 1,'.4 oftheSW 1/4 Of
the NE 114; thence run South along said East line a distance Of 208 feet and to the POINT OF
BEGINNING."""


def test_running_description_becomes_metes_and_bounds():
    _rows, courses, problems = parse(SECTION9, U.US_SURVEY_FOOT_M)
    assert not problems, problems
    assert len(courses) == 4
    bearings = [round(math.degrees(c.azimuth)) % 360 for c in courses]
    assert bearings == [270, 0, 90, 180]
    assert [c.distance for c in courses] == pytest.approx([208, 208, 208, 208])
    closed = C.compute((0.0, 0.0), courses)
    assert closed.misclose == pytest.approx(0, abs=0.01)
    assert closed.area == pytest.approx(208 * 208, rel=1e-6)


def test_along_phrase_without_distance_of():
    text = ("Beginning at a monument; thence North 45°00'00\" East along the property line 50 feet; "
            "thence South, 50 feet to the point of beginning.")
    _rows, courses, problems = parse(text, U.US_SURVEY_FOOT_M)
    assert not problems, problems
    assert len(courses) == 2
    assert courses[0].distance == pytest.approx(50)
    assert courses[1].distance == pytest.approx(50)


def test_two_distances_in_one_clause_are_not_merged():
    text = "thence North 100 feet East 50 feet"
    _rows, courses, problems = parse(text, U.US_SURVEY_FOOT_M)
    assert problems
    assert courses == []


def test_metres_convert_to_fabric_feet():
    text = "Beginning at a rod; thence North, 100 meters; thence East, 100 meters; " \
           "thence South, 100 meters; thence West, 100 meters to the point of beginning."
    _rows, courses, problems = parse(text, U.US_SURVEY_FOOT_M)
    assert not problems
    assert courses[0].distance == pytest.approx(328.0833333, abs=1e-6)
