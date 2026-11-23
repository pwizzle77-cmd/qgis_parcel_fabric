# -*- coding: utf-8 -*-
"""Turn COGO Workbench's reviewed legal-description calls into fabric courses.

Pure Python.  Uses the copied parser in ``wb_legal`` / ``wb_engine``.
"""

import math
import re

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
            row.issue = str(e)
            problems.append("%s - %s" % (row.source[:80], e))
    _add_closing(rows, courses, problems)
    return rows, courses, problems


def tie_courses(rows, fabric_foot_m, plain_feet_m=None):
    """The calls from the point of commencement to the POB, or [] when any of them can't be used."""
    ties = [r for r in rows if r.phase == "Commencement"]
    unit = next((r.unit for r in rows if r.unit), "")
    try:
        if any(r.issue for r in ties):
            return []
        return [to_course(r.course, r.unit or unit, fabric_foot_m, plain_feet_m) for r in ties if r.course]
    except ValueError:
        return []


CLOSING = re.compile(r"(?:run\s+|thence\s+)?(?:back\s+|returning\s+)?(?:to|at)\s+(?:the\s+)?(?:true\s+)?"
                     r"(?:point\s+of\s+beginning|P\.?\s*O\.?\s*B\.?)", re.I)


def _add_closing(rows, courses, problems):
    """A last call that just says "to the Point of Beginning" (no bearing) closes the parcel:
    add the straight line from where the courses end back to the start."""
    bnd = [r for r in rows if r.phase == "Boundary" and (r.course or CLOSING.fullmatch(r.source.strip(" ,;.")))]
    if not courses or not bnd or bnd[-1].course or not CLOSING.fullmatch(bnd[-1].source.strip(" ,;.")):
        return
    src = bnd[-1].source[:80]
    if any(not p.startswith(src) for p in problems):
        return      # other calls were dropped, so a computed closing line would be wrong
    end = C.compute((0.0, 0.0), courses, close_to=False).legs[-1].end
    if math.hypot(*end) < 0.005:
        bnd[-1].issue = ""
        return
    az, dist = U.inverse(end[0], end[1], 0.0, 0.0)
    bnd[-1].closing = C.Course(C.LINE, az, dist, note="closing course to POB (computed)")
    bnd[-1].issue = ""
    courses.append(bnd[-1].closing)
    problems[:] = [p for p in problems if not p.startswith(bnd[-1].source[:80])]
