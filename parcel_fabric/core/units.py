# -*- coding: utf-8 -*-
"""Bearings, angles and distance units.

Pure Python (no QGIS imports) so it can be unit tested on its own.

Conventions used everywhere in the plugin:
  * Directions are stored internally as north azimuths in RADIANS,
    measured clockwise from grid north.
  * The Esri parcel fabric ``Direction`` field stores a north azimuth in
    DECIMAL DEGREES; use :func:`azimuth_to_esri` / :func:`esri_to_azimuth`.
  * Quadrant bearings are written N dd°mm'ss" E.
"""

import math
import re

# --------------------------------------------------------------------------
# Length units
# --------------------------------------------------------------------------
US_SURVEY_FOOT_M = 1200.0 / 3937.0      # 0.3048006096...
INTL_FOOT_M = 0.3048

FOOT_DEFINITIONS = {
    "us_survey": US_SURVEY_FOOT_M,
    "international": INTL_FOOT_M,
}

# Units a distance may be typed in, expressed as a multiple of ONE FOOT of the
# active foot definition. "m" is handled separately because it depends on the
# foot definition.
_FOOT_MULTIPLES = {
    "ft": 1.0, "'": 1.0, "feet": 1.0, "foot": 1.0, "usft": 1.0, "sft": 1.0,
    "ch": 66.0, "chain": 66.0, "chains": 66.0,
    "rd": 16.5, "rod": 16.5, "rods": 16.5, "pole": 16.5, "perch": 16.5,
    "lk": 0.66, "link": 0.66, "links": 0.66,
    "vr": 33.3333333333333, "vara": 33.3333333333333, "varas": 33.3333333333333,
}

AREA_UNITS = {
    # name -> square feet
    "sqft": 1.0,
    "acres": 43560.0,
}


class ParseError(ValueError):
    """Raised when a bearing, angle or distance cannot be read."""


def parse_distance(text, foot_m=US_SURVEY_FOOT_M, to_unit="ft"):
    """Parse a typed distance and return it in feet (or metres).

    ``"123.45"`` is taken as feet.  Suffixes: ft ' ch rd lk vr m.
    """
    if text is None:
        raise ParseError("No distance")
    s = str(text).strip().lower().replace(",", "")
    if not s:
        raise ParseError("No distance")
    m = re.match(r"^([+-]?\d*\.?\d+(?:e[+-]?\d+)?)\s*([a-z']*)\.?$", s)
    if not m:
        raise ParseError("Can't read distance '%s'" % text)
    value = float(m.group(1))
    unit = m.group(2) or "ft"
    if unit in ("m", "meter", "meters", "metre", "metres"):
        feet = value / foot_m
    elif unit in _FOOT_MULTIPLES:
        feet = value * _FOOT_MULTIPLES[unit]
    else:
        raise ParseError("Unknown distance unit '%s'" % unit)
    if value < 0:
        raise ParseError("Distance can't be negative")
    if to_unit == "m":
        return feet * foot_m
    return feet


def format_distance(feet, decimals=2):
    return ("%%.%df" % decimals) % feet


# --------------------------------------------------------------------------
# Angles
# --------------------------------------------------------------------------
_DMS_SPLIT = re.compile(r"[°º˚d\-:'’′\"”″\s]+")


def dms_to_deg(d, m=0.0, s=0.0):
    return abs(d) + m / 60.0 + s / 3600.0


def deg_to_dms(deg, sec_decimals=0):
    """Return (d, m, s) with rounding carried correctly (no 60")."""
    deg = abs(deg)
    total = round(deg * 3600.0, sec_decimals)
    d = int(total // 3600)
    total -= d * 3600
    m = int(total // 60)
    s = total - m * 60
    s = round(s, sec_decimals)
    if s >= 60:
        s -= 60
        m += 1
    if m >= 60:
        m -= 60
        d += 1
    return d, m, s


def parse_angle(text, packed_dms=True):
    """Parse an angle in degrees.

    Accepts ``45-30-15``, ``45°30'15"``, ``45 30 15``, ``45d30m15s`` and a
    single number.  A single number with a decimal point is read as packed
    DD.MMSS (the usual COGO keyboard form, ``45.3015`` = 45°30'15") when
    ``packed_dms`` is true, otherwise as decimal degrees.
    """
    s = str(text).strip().lower().replace("m", "-").replace("s", "")
    if not s:
        raise ParseError("No angle")
    parts = [p for p in _DMS_SPLIT.split(s) if p]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ParseError("Can't read angle '%s'" % text)
    if not nums or len(nums) > 3:
        raise ParseError("Can't read angle '%s'" % text)
    if len(nums) == 1:
        v = nums[0]
        if packed_dms and "." in parts[0]:
            d = int(v)
            rest = round((v - d) * 10000.0, 6)  # MMSS.ss
            mm = int(rest // 100)
            ss = rest - mm * 100
            if mm >= 60 or ss >= 60:
                raise ParseError("Minutes/seconds over 59 in '%s'" % text)
            return dms_to_deg(d, mm, ss)
        return v
    d = nums[0]
    mm = nums[1] if len(nums) > 1 else 0.0
    ss = nums[2] if len(nums) > 2 else 0.0
    if mm >= 60 or ss >= 60:
        raise ParseError("Minutes/seconds over 59 in '%s'" % text)
    return dms_to_deg(d, mm, ss)


def format_angle(deg, sec_decimals=0):
    d, m, s = deg_to_dms(deg, sec_decimals)
    if sec_decimals:
        sfmt = "%0" + str(3 + sec_decimals) + "." + str(sec_decimals) + "f"
    else:
        sfmt = "%02d"
        s = int(round(s))
    return ("%d°%02d'" + sfmt + '"') % (d, m, s)


# --------------------------------------------------------------------------
# Quadrant bearings <-> azimuths
# --------------------------------------------------------------------------
def parse_bearing(text, packed_dms=True):
    """Parse a quadrant bearing and return a north azimuth in radians.

    Examples: ``N 45-30-15 E``, ``S45°30'15"W``, ``n45.3015e``, ``N``, ``E``,
    ``Due West``.
    """
    if text is None:
        raise ParseError("No bearing")
    s = str(text).strip().upper().replace("DUE", "").strip()
    if not s:
        raise ParseError("No bearing")
    if s in ("N", "NORTH"):
        return 0.0
    if s in ("E", "EAST"):
        return math.pi / 2
    if s in ("S", "SOUTH"):
        return math.pi
    if s in ("W", "WEST"):
        return 1.5 * math.pi
    m = re.match(r"^([NS])\s*(.+?)\s*([EW])$", s)
    if not m:
        raise ParseError("Can't read bearing '%s' (use N 45-30-15 E)" % text)
    ns, ang, ew = m.groups()
    a = parse_angle(ang, packed_dms)
    if a > 90:
        raise ParseError("Bearing angle over 90° in '%s'" % text)
    if ns == "N" and ew == "E":
        az = a
    elif ns == "S" and ew == "E":
        az = 180.0 - a
    elif ns == "S" and ew == "W":
        az = 180.0 + a
    else:
        az = 360.0 - a
    return math.radians(az % 360.0)


def normalize_azimuth(az):
    return az % (2 * math.pi)


def azimuth_to_bearing(az, sec_decimals=0):
    """Format a north azimuth (radians) as ``N 45°30'15" E``."""
    deg = math.degrees(normalize_azimuth(az))
    # round first so 359°59'59.7" becomes N 0°00'00" E, not N 0°00'60" W
    total = round(deg * 3600.0, sec_decimals) / 3600.0
    total %= 360.0
    if total <= 90:
        ns, ew, a = "N", "E", total
    elif total <= 180:
        ns, ew, a = "S", "E", 180.0 - total
    elif total <= 270:
        ns, ew, a = "S", "W", total - 180.0
    else:
        ns, ew, a = "N", "W", 360.0 - total
    return "%s %s %s" % (ns, format_angle(a, sec_decimals), ew)


def azimuth_to_esri(az):
    """Esri ``Direction`` field: north azimuth, decimal degrees, [0, 360)."""
    return math.degrees(normalize_azimuth(az))


def esri_to_azimuth(deg):
    return math.radians(float(deg)) % (2 * math.pi)


def inverse(x1, y1, x2, y2):
    """Azimuth (radians) and distance from point 1 to point 2."""
    dx, dy = x2 - x1, y2 - y1
    return normalize_azimuth(math.atan2(dx, dy)), math.hypot(dx, dy)


def ratio_text(perimeter, misclose):
    if misclose <= 1e-9:
        return "Closed"
    return "1:%d" % int(round(perimeter / misclose))
