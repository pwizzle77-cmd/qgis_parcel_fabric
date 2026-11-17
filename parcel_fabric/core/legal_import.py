# -*- coding: utf-8 -*-
"""Turn COGO Workbench's reviewed legal-description calls into fabric courses.

Pure Python.  Uses the copied parser in ``wb_legal`` / ``wb_engine``.
"""

import math

from . import cogo as C
from . import units as U
from .wb_engine import angle as wb_angle, bearing as wb_bearing, number as wb_number
from .wb_legal import parse_description

# COGO Workbench unit labels -> metres per unit (None = needs a choice)
UNIT_M = {
    "US survey feet": U.US_SURVEY_FOOT_M,
    "International feet": U.INTL_FOOT_M,
    "Meters": 1.0,
    "Feet (choose definition)": None,
    "": None,
}


def to_course(wb_course, unit_label, fabric_foot_m, plain_feet_m=None):
    """One COGO Workbench Course -> a fabric Course in fabric feet.

    ``plain_feet_m`` says which foot a plain "feet" call means (default: the
    fabric's own foot).
    """
    per = UNIT_M.get(unit_label)
    if per is None:
        per = plain_feet_m or fabric_foot_m
    k = per / fabric_foot_m           # record units -> fabric feet
    az = math.radians(wb_bearing(wb_course.direction))
    if wb_course.kind == "Line":
        return C.Course(C.LINE, az, wb_number(wb_course.distance, "Distance") * k)
    r = wb_number(wb_course.radius, "Radius", True) * k
    if wb_course.measure == "Arc":
        arc = wb_number(wb_course.distance, "Arc length", True) * k
    elif wb_course.measure == "Chord":
        arc = C.arc_length_from_chord(r, wb_number(wb_course.distance, "Chord", True) * k)
    else:  # Delta
        arc = C.arc_length_from_delta(r, math.radians(wb_angle(wb_course.distance)))
    signed = r if wb_course.side == "R" else -r
    kind = C.DIR_TANGENT if wb_course.basis == "Tangent" else C.DIR_CHORD
    return C.Course(C.CURVE, az, None, signed, arc, kind)


def parse(text, fabric_foot_m, boundary_only=False, plain_feet_m=None):
    """Parse a description. Returns (rows, courses, problems).

    ``rows`` are COGO Workbench LegalCall rows (for the review table);
    ``courses`` are fabric courses for the boundary calls that parsed;
    ``problems`` are messages for boundary rows that need attention.
    """
    rows = parse_description(text, boundary_only)
    courses, problems = [], []
    for row in rows:
        if row.phase != "Boundary":
            continue
        if row.issue:
            problems.append("%s - %s" % (row.source[:80], row.issue))
            continue
        if row.course is None:
            continue
        try:
            courses.append(to_course(row.course, row.unit, fabric_foot_m, plain_feet_m))
        except ValueError as e:
            problems.append("%s - %s" % (row.source[:80], e))
    return rows, courses, problems
