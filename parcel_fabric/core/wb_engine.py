"""Planar COGO mathematics. No QGIS imports; azimuths run clockwise from north.

Copied from COGO Workbench (same author, GPL-2.0-or-later). Keep in sync by hand.
"""

from dataclasses import dataclass
import math
import re


def number(value, label="Value", positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} must be {'positive and ' if positive else ''}finite.")
    return result


def angle(text):
    """Decimal degrees or separated DMS (never ambiguous packed DD.MMSS)."""
    text = str(text).strip()
    parts = re.fullmatch(r"(\d+(?:\.\d+)?)(?:[\s:°-]+(\d+(?:\.\d+)?)(?:[\s:'′-]+(\d+(?:\.\d+)?))?[\s\"″'′]*)?", text)
    if not parts:
        raise ValueError(f"Invalid angle: {text!r}. Use decimal degrees or 45:30:00.")
    d, m, s = (float(v or 0) for v in parts.groups())
    if m >= 60 or s >= 60 or (parts[2] and not d.is_integer()) or (parts[3] and not m.is_integer()):
        raise ValueError("DMS minutes and seconds must be below 60; only the last part may have decimals.")
    return d + m / 60 + s / 3600


def bearing(text):
    text = str(text).strip().upper()
    if text in {"N", "E", "S", "W"}:
        return {"N": 0, "E": 90, "S": 180, "W": 270}[text]
    quadrant = re.fullmatch(r"([NS])\s*(.*?)\s*([EW])", text)
    if quadrant:
        first, value, last = quadrant.groups()
        a = angle(value)
        if a > 90:
            raise ValueError("Quadrant bearings must be between 0 and 90 degrees.")
        return ({("N", "E"): a, ("S", "E"): 180 - a,
                 ("S", "W"): 180 + a, ("N", "W"): 360 - a}[first, last]) % 360
    a = angle(text)
    if a > 360:
        raise ValueError("Azimuth must be between 0 and 360 degrees.")
    return a % 360


def format_bearing(azimuth):
    a = azimuth % 360
    if a <= 90:
        first, d, last = "N", a, "E"
    elif a <= 180:
        first, d, last = "S", 180 - a, "E"
    elif a <= 270:
        first, d, last = "S", a - 180, "W"
    else:
        first, d, last = "N", 360 - a, "W"
    seconds = round(d * 3600, 2)
    degrees, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{first}{int(degrees):02d}°{int(minutes):02d}'{seconds:05.2f}\"{last}"


def forward(point, azimuth, distance):
    a = math.radians(azimuth)
    return point[0] + distance * math.sin(a), point[1] + distance * math.cos(a)


def inverse(start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance == 0:
        raise ValueError("Coincident points have no direction.")
    return math.degrees(math.atan2(dx, dy)) % 360, distance


@dataclass
class Course:
    kind: str
    direction: str
    distance: str
    radius: str = ""
    measure: str = "Arc"
    side: str = "R"
    basis: str = "Tangent"


@dataclass
class Segment:
    start: tuple
    end: tuple
    direction: float
    distance: float
    mid: tuple = None
    radius: float = None
    arc: float = None
    delta: float = None
    side: str = ""


def traverse(start, courses, factor=1.0, offset=0.0):
    """factor converts entered lengths to grid units, offset adds to azimuths."""
    point = tuple(number(v, "Start coordinate") for v in start)
    factor = number(factor, "Length factor", True)
    offset = number(offset, "Direction offset")
    result = []
    for row, course in enumerate(courses, 1):
        try:
            a = (bearing(course.direction) + offset) % 360
            value = (angle(course.distance) if course.kind == "Curve" and course.measure == "Delta"
                     else number(course.distance, "Distance / curve value", True))
            if course.kind == "Line":
                distance = value * factor
                segment = Segment(point, forward(point, a, distance), a, distance)
            elif course.kind == "Curve":
                r = number(course.radius, "Radius", True) * factor
                if course.measure == "Arc":
                    delta = value * factor / r
                elif course.measure == "Chord":
                    if value * factor > 2 * r:
                        raise ValueError("Chord cannot exceed the diameter.")
                    delta = 2 * math.asin(value * factor / (2 * r))
                elif course.measure == "Delta":
                    delta = math.radians(angle(course.distance))
                else:
                    raise ValueError("Curve measure must be Arc, Chord or Delta.")
                if not 0 < delta < 2 * math.pi:
                    raise ValueError("Curve delta must be greater than 0 and below 360 degrees.")
                if course.side not in ("R", "L") or course.basis not in ("Tangent", "Chord"):
                    raise ValueError("Choose R/L and Tangent/Chord for the curve.")
                sign = 1 if course.side == "R" else -1
                tangent = a if course.basis == "Tangent" else a - sign * math.degrees(delta) / 2
                chord_a = (tangent + sign * math.degrees(delta) / 2) % 360
                chord = 2 * r * math.sin(delta / 2)
                end = forward(point, chord_a, chord)
                mid = forward(point, tangent + sign * math.degrees(delta) / 4,
                              2 * r * math.sin(delta / 4))
                segment = Segment(point, end, chord_a, chord, mid, r, r * delta,
                                  math.degrees(delta), course.side)
            else:
                raise ValueError("Course type must be Line or Curve.")
            if not all(math.isfinite(v) for v in (*segment.end, segment.distance, segment.arc or 0)):
                raise ValueError("Course exceeds numeric range.")
            result.append(segment)
            point = segment.end
        except ValueError as error:
            raise ValueError(f"Course {row}: {error}") from None
    if not result:
        raise ValueError("Enter at least one course.")
    return result


def closure(segments, target):
    dx, dy = segments[-1].end[0] - target[0], segments[-1].end[1] - target[1]
    error = math.hypot(dx, dy)
    perimeter = sum(s.arc if s.arc is not None else s.distance for s in segments)
    return dx, dy, error, perimeter, perimeter / error if error else math.inf


def bowditch(segments, target):
    if any(s.mid is not None for s in segments):
        raise ValueError("Bowditch adjustment supports straight courses only; curves must retain their record radius.")
    dx, dy, _, length, _ = closure(segments, target)
    point, result = segments[0].start, []
    for s in segments:
        weight = s.distance / length
        end = (point[0] + s.end[0] - s.start[0] - dx * weight,
               point[1] + s.end[1] - s.start[1] - dy * weight)
        a, distance = inverse(point, end)
        result.append(Segment(point, end, a, distance))
        point = end
    return result


def circle_parameters(start, mid, end):
    """Recover a directed circular arc from three non-collinear coordinates."""
    # Work near the origin to avoid cancellation with State Plane coordinates.
    bx, by = mid[0] - start[0], mid[1] - start[1]
    cx, cy = end[0] - start[0], end[1] - start[1]
    cross = bx * cy - by * cx
    if abs(cross) <= 1e-12 * max(math.hypot(bx, by) * math.hypot(cx, cy), 1e-300):
        raise ValueError("Arc points are collinear or coincident.")
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    ux, uy = (cy * b2 - by * c2) / (2 * cross), (bx * c2 - cx * b2) / (2 * cross)
    radius = math.hypot(ux, uy)
    first = math.atan2(-uy, -ux)
    last = math.atan2(cy - uy, cx - ux)
    delta = (last - first) % (2 * math.pi) if cross > 0 else (first - last) % (2 * math.pi)
    if delta == 0:
        raise ValueError("Full-circle COGO features are unsupported; split into arcs.")
    return radius, radius * delta, math.degrees(delta), "L" if cross > 0 else "R"
