# -*- coding: utf-8 -*-
"""Getting COGO calls out of a deed with help from a local AI model.

Pure Python (no QGIS imports).  The rules:

* The deterministic COGO Workbench parser always goes first.
* The model is only asked about clauses the parser couldn't read, and it
  must quote the deed words each call came from.  A call is rejected if its
  quote isn't in the deed text, or if its numbers aren't in the quote.
* Nothing is drawn until a person has reviewed every call.
"""

import math
import re

from . import cogo as C
from . import units as U

OCR_PROMPTS = {
    "deepseek-ocr": "Free OCR.",
    "default": ("Transcribe every word of this scanned page exactly as written, line by line. Keep "
                "bearings, degree/minute/second symbols and distances exactly. Do not summarize, "
                "explain or add anything."),
}

FIND_PROMPT = (
    "Below is the text of a deed. Copy out, word for word, ONLY the legal description of the land: "
    "from where it starts (e.g. 'Commence at' or 'Begin at') through the last call that returns to the "
    "point of beginning, including any 'containing ... acres' line. Do not change any character.\n\n"
    "DEED TEXT:\n%s")

CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "kind": {"type": "string", "enum": ["line", "curve"]},
                    "bearing": {"type": "string"},
                    "distance": {"type": ["number", "null"]},
                    "radius": {"type": ["number", "null"]},
                    "arc_length": {"type": ["number", "null"]},
                    "chord_length": {"type": ["number", "null"]},
                    "delta": {"type": ["string", "null"]},
                    "side": {"type": ["string", "null"], "enum": ["left", "right", None]},
                    "bearing_is": {"type": ["string", "null"], "enum": ["chord", "tangent", None]},
                },
                "required": ["quote", "kind", "bearing"],
            },
        }
    },
    "required": ["calls"],
}

CALLS_PROMPT = (
    "You read land surveying legal descriptions and rewrite them as metes and bounds. From the TEXT "
    "below, list each boundary course in order, one course per call. "
    "'thence run West along the South line of the quarter a distance of 208 feet' is ONE course: "
    "bearing West, distance 208. 'Along the south line' or 'parallel to the east line' names the line "
    "being followed. It is not another course. If the text really does contain two courses, return both.\n"
    "For each call give:\n"
    "- quote: the exact words of that one course copied from the text\n"
    "- kind: line or curve\n"
    "- bearing: the direction of travel as N 45°30'15\" E, or a cardinal N, S, E or W (for a curve, the "
    "chord bearing or tangent bearing that is given)\n"
    "- distance (feet) for a line; for a curve radius, and arc_length or chord_length or delta, side "
    "(left/right) and bearing_is (chord/tangent)\n"
    "Only use numbers that appear in the text. If a value isn't stated, use null. Skip commencement "
    "ties (calls before the point of beginning).\n\nTEXT:\n%s")


def ocr_prompt(model):
    base = model.split(":")[0]
    return OCR_PROMPTS.get(base, OCR_PROMPTS["default"])


# --------------------------------------------------------------------------
# Finding the description in a whole deed
# --------------------------------------------------------------------------
_START = re.compile(r"\b(commenc(?:e|ing)\s+at|begin(?:ning)?\s+at)\b", re.I)
_END = re.compile(r"(point\s+of\s+beginning|P\.?\s*O\.?\s*B\.?)[^.;]*[.;]?(\s*containing\s+[\d.,]+\s+acres?[^.]*\.?)?",
                  re.I)


def find_description(text):
    """The legal description part of a deed's text, or '' if not found.

    From the first 'Commence at' / 'Begin at' to the last 'point of
    beginning' after it (plus a 'containing N acres' sentence).
    """
    m = _START.search(text)
    if not m:
        return ""
    start = m.start()
    last = None
    for e in _END.finditer(text, m.end()):
        last = e
    if last is None:
        return ""
    return text[start:last.end()].strip()


# --------------------------------------------------------------------------
# Checking what the model says
# --------------------------------------------------------------------------
def _norm(s):
    s = s.lower()
    s = s.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "−": "-",
                                   "º": "°", "˚": "°"}))
    return re.sub(r"[^a-z0-9°'\".]+", " ", s).strip()


def _numbers(s):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", s)]


def quote_in_text(quote, text):
    q = _norm(quote)
    return bool(q) and q in _norm(text)


def number_in(value, numbers, tol=1e-9):
    return any(abs(value - n) <= tol for n in numbers)


def check_call(call, text, foot_m=U.US_SURVEY_FOOT_M):
    """Turn one model call into a fabric Course, or raise ValueError saying why not."""
    quote = call.get("quote") or ""
    if not quote_in_text(quote, text):
        raise ValueError("its quote isn't in the deed text")
    nums = _numbers(quote)
    try:
        az = U.parse_bearing(call.get("bearing", ""))
    except U.ParseError as e:
        raise ValueError("bad bearing: %s" % e)
    # every digit of the bearing must be in the quote
    for n in _numbers(call.get("bearing", "")):
        if not number_in(n, nums):
            raise ValueError("bearing number %g isn't in its quote" % n)
    if call.get("kind") == "curve":
        r = call.get("radius")
        if not r or not number_in(float(r), nums):
            raise ValueError("curve radius missing or not in its quote")
        side = (call.get("side") or "").lower()
        if side not in ("left", "right"):
            raise ValueError("curve has no left/right")
        if call.get("arc_length") and number_in(float(call["arc_length"]), nums):
            arc = float(call["arc_length"])
        elif call.get("chord_length") and number_in(float(call["chord_length"]), nums):
            arc = C.arc_length_from_chord(float(r), float(call["chord_length"]))
        elif call.get("delta"):
            d = U.parse_angle(call["delta"], packed_dms=False)
            for n in _numbers(call["delta"]):
                if not number_in(n, nums):
                    raise ValueError("delta number %g isn't in its quote" % n)
            arc = C.arc_length_from_delta(float(r), math.radians(d))
        else:
            raise ValueError("curve needs an arc length, chord or delta from its quote")
        kind = C.DIR_TANGENT if (call.get("bearing_is") or "").lower() == "tangent" else C.DIR_CHORD
        radius = float(r) if side == "right" else -float(r)
        return C.Course(C.CURVE, az, None, radius, arc, kind)
    d = call.get("distance")
    if d is None or not number_in(float(d), nums):
        raise ValueError("distance missing or not in its quote")
    return C.Course(C.LINE, az, float(d))
